"""FastAPI routes, startup wiring, and template rendering."""

import json
import logging
import os
import secrets
import asyncio
import contextlib
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from markupsafe import Markup
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from app.auth import (
    DuplicateEmailError,
    authenticate,
    create_user,
    get_csrf_token,
    get_current_user,
    get_personal_gemini_key,
    get_session_id,
    get_user_by_id,
    log_in_session,
    log_out_session,
    update_personal_gemini_settings,
    verify_csrf_token,
)
from app.cache import (
    AI_SUMMARY_COOLDOWN,
    CooldownActiveError,
    add_favorite,
    ensure_ai_summary,
    get_cached_ai_summary,
    get_object_by_simbad_id,
    get_object_id_by_simbad_id,
    get_or_resolve,
    is_favorited,
    list_favorites,
    list_recent_objects,
    regenerate_ai_summary,
    remove_favorite,
    save_user_summary_snapshot,
)
from app.database import engine, init_db
from app.models import User
from app.narrative import GeminiGenerationError, GeminiRateLimitedError, render_summary_markdown
from app.ratelimit import RateLimitExceededError, check_limit, purge_old_rate_limit_events, record_usage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Rate limit applied to /search and /api/resolve, separate from the
# AI-summary rate limit (which uses subject_type "user"/"session"). These
# routes hit shared external services (SIMBAD/NASA Exoplanet Archive) on
# every novel query, so they get their own, higher ceiling.
RESOLVE_RATE_LIMIT = 60
RESOLVE_RATE_LIMIT_WINDOW = timedelta(hours=1)

# Auth attempts are tracked per anonymous session id (there's no user id yet
# at this point). Login only counts *failed* attempts, so a legitimate user
# who mistypes their password once isn't penalised; registration counts
# every attempt since there's no equivalent "don't punish success" case.
AUTH_RATE_LIMIT = 10
AUTH_RATE_LIMIT_WINDOW = timedelta(minutes=15)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """On startup, verify the database schema is present.

    Historically this unconditionally called init_db() (create_all()) on
    every startup, which happily created any *missing* tables even in
    environments meant to be managed exclusively by Alembic migrations --
    silently masking a forgotten `alembic upgrade head` instead of failing
    loudly. create_all() is still available as an explicit, opt-in dev
    convenience via DEV_AUTO_CREATE_SCHEMA=1 (docker-compose/local dev
    without Alembic set up); everywhere else, startup now does a cheap
    read-only check that a known table exists and fails fast with a clear
    error if it doesn't, instead of silently drifting the schema.

    The test suite is unaffected: test fixtures create/drop tables directly
    via `Base.metadata.create_all`/`init_db()` before each test's
    `TestClient` (and therefore this lifespan) runs, so the schema already
    exists by the time this check executes.
    """
    if os.getenv("DEV_AUTO_CREATE_SCHEMA", "").strip().lower() in ("1", "true", "yes"):
        await init_db()
    else:
        try:
            async with engine.begin() as conn:
                await conn.execute(text("SELECT 1 FROM users LIMIT 1"))
        except Exception as exc:
            raise RuntimeError(
                "Database schema check failed on startup (expected table 'users' "
                "not found or not queryable). Run 'alembic upgrade head' before "
                "starting the app, or set DEV_AUTO_CREATE_SCHEMA=1 for local dev "
                "without Alembic."
            ) from exc

    # F6: periodically purge stale rate_limit_events rows so the append-only
    # event log doesn't grow forever on a long-lived deployment. Runs
    # in-process rather than as a separate cron job/Render Pre-Deploy step,
    # since that feature isn't available on Render's free tier.
    async def _rate_limit_cleanup_loop() -> None:
        while True:
            try:
                await asyncio.sleep(6 * 60 * 60)
                deleted = await purge_old_rate_limit_events()
                if deleted:
                    logger.info("Purged %d stale rate_limit_events rows", deleted)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("rate_limit_events cleanup task failed; will retry next cycle")

    cleanup_task = asyncio.create_task(_rate_limit_cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cleanup_task


app = FastAPI(title="Astronomy Multi-Catalog Identifier-Matcher", lifespan=lifespan)

# Session cookies back login state and anonymous rate limiting.
_session_secret_key = os.getenv("SESSION_SECRET_KEY")
if not _session_secret_key:
    _session_secret_key = secrets.token_hex(32)
    logger.warning(
        "SESSION_SECRET_KEY not set; using a randomly generated key for this "
        "process only. Sessions will not survive a restart. Set SESSION_SECRET_KEY "
        "explicitly before deploying anywhere beyond local single-process dev."
    )
_force_https_cookies = os.getenv("FORCE_HTTPS_COOKIES", "").strip().lower() in ("1", "true", "yes")
app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret_key,
    https_only=_force_https_cookies,
    same_site="lax",
)

_APP_DIR = Path(__file__).resolve().parent

app.mount("/static", StaticFiles(directory=_APP_DIR / "static"), name="static")

def _tojson(value) -> Markup:
    """Serialize a value for safe template embedding."""
    return Markup(json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


env = Environment(loader=FileSystemLoader(_APP_DIR / "templates"), autoescape=select_autoescape(["html"]))
env.filters["render_summary_markdown"] = render_summary_markdown
env.filters["tojson"] = _tojson
env.globals["csrf_token"] = get_csrf_token


async def require_csrf_form(request: Request, csrf_token: str = Form(...)) -> None:
    """Reject invalid form CSRF tokens."""
    if not verify_csrf_token(request, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token. Please reload the page and try again.")


async def require_csrf_header(request: Request) -> None:
    """Reject invalid JSON CSRF tokens."""
    if not verify_csrf_token(request, request.headers.get("x-csrf-token")):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token. Please reload the page and try again.")


@app.get("/", response_class=HTMLResponse)
async def home(request: Request, current_user: User | None = Depends(get_current_user)) -> HTMLResponse:
    """Render the landing page."""
    template = env.get_template("index.html")
    html = template.render(request=request, current_user=current_user)
    return HTMLResponse(content=html)


@app.get("/search", response_class=HTMLResponse)
async def search(
    request: Request,
    q: str | None = Query(None, max_length=200),
    current_user: User | None = Depends(get_current_user),
) -> HTMLResponse:
    """Resolve a search query or show the landing page when empty."""
    if not q:
        template = env.get_template("index.html")
        html = template.render(request=request, current_user=current_user)
        return HTMLResponse(content=html)

    subject_type = "resolve_user" if current_user is not None else "resolve_session"
    subject_id = str(current_user.id) if current_user is not None else get_session_id(request)
    try:
        await check_limit(subject_type, subject_id, limit=RESOLVE_RATE_LIMIT, window=RESOLVE_RATE_LIMIT_WINDOW)
    except RateLimitExceededError as exc:
        template = env.get_template("index.html")
        html = template.render(
            request=request,
            current_user=current_user,
            error=(
                "You've made too many searches recently. "
                f"Please try again in about {exc.retry_after_seconds} seconds."
            ),
        )
        return HTMLResponse(content=html, status_code=429)
    await record_usage(subject_type, subject_id)

    # Resolve the query and render the result page.
    result = await get_or_resolve(q, generate_ai_summary=False)
    favorited = False
    if current_user is not None and result.id is not None:
        favorited = await is_favorited(current_user.id, result.id)
    template = env.get_template("result.html")
    html = template.render(request=request, object=result, current_user=current_user, favorited=favorited)
    return HTMLResponse(content=html)


@app.get("/object/{simbad_main_id}", response_class=HTMLResponse)
async def object_profile(
    request: Request, simbad_main_id: str, current_user: User | None = Depends(get_current_user)
) -> HTMLResponse:
    """Display an object profile."""
    obj = await get_object_by_simbad_id(simbad_main_id)
    if obj is None:
        subject_type = "resolve_user" if current_user is not None else "resolve_session"
        subject_id = str(current_user.id) if current_user is not None else get_session_id(request)
        try:
            await check_limit(subject_type, subject_id, limit=RESOLVE_RATE_LIMIT, window=RESOLVE_RATE_LIMIT_WINDOW)
        except RateLimitExceededError as exc:
            template = env.get_template("index.html")
            html = template.render(
                request=request,
                current_user=current_user,
                error=(
                    "You've made too many searches recently. "
                    f"Please try again in about {exc.retry_after_seconds} seconds."
                ),
            )
            return HTMLResponse(content=html, status_code=429)
        await record_usage(subject_type, subject_id)
        obj = await get_or_resolve(simbad_main_id, generate_ai_summary=False)
    favorited = False
    if current_user is not None and obj.id is not None:
        favorited = await is_favorited(current_user.id, obj.id)
    template = env.get_template("result.html")
    html = template.render(request=request, object=obj, current_user=current_user, favorited=favorited)
    return HTMLResponse(content=html)


def _gemini_error_response(exc: GeminiGenerationError) -> JSONResponse:
    """Build a JSON error response for Gemini failures."""
    body: dict[str, object] = {"error": "ai_generation_failed", "message": exc.user_message}
    if isinstance(exc, GeminiRateLimitedError):
        body["error"] = "ai_rate_limited"
    if exc.retry_after_seconds:
        body["retry_after_seconds"] = exc.retry_after_seconds
    return JSONResponse(body, status_code=503)


@app.post("/object/{simbad_main_id}/summary", dependencies=[Depends(require_csrf_header)])
async def object_summary(
    request: Request, simbad_main_id: str, current_user: User | None = Depends(get_current_user)
) -> JSONResponse:
    """Return the AI narrative for an object."""
    cached_summary = await get_cached_ai_summary(simbad_main_id)
    if cached_summary is not None:
        # Save a personal snapshot for logged-in users on cache hits.
        if current_user is not None:
            object_id = await get_object_id_by_simbad_id(simbad_main_id)
            if object_id is not None:
                await save_user_summary_snapshot(current_user.id, object_id, cached_summary)
        return JSONResponse({"summary": cached_summary, "summary_html": render_summary_markdown(cached_summary)})

    subject_type = "user" if current_user is not None else "session"
    subject_id = str(current_user.id) if current_user is not None else get_session_id(request)
    try:
        await check_limit(subject_type, subject_id)
    except RateLimitExceededError as exc:
        response = JSONResponse(
            {"error": "Rate limit exceeded", "retry_after_seconds": exc.retry_after_seconds},
            status_code=429,
        )
        response.headers["Retry-After"] = str(exc.retry_after_seconds)
        return response

    try:
        personal_api_key = personal_model = None
        if current_user is not None:
            personal_api_key, personal_model = await get_personal_gemini_key(current_user.id)
        summary = await ensure_ai_summary(
            simbad_main_id, personal_api_key=personal_api_key, personal_model=personal_model
        )
    except LookupError:
        return JSONResponse({"error": "Object not found"}, status_code=404)
    except GeminiGenerationError as exc:
        logger.error("AI summary generation failed for %s: %s", simbad_main_id, exc)
        return _gemini_error_response(exc)

    # Record usage only after a successful generation.
    await record_usage(subject_type, subject_id)

    if current_user is not None:
        object_id = await get_object_id_by_simbad_id(simbad_main_id)
        if object_id is not None:
            await save_user_summary_snapshot(current_user.id, object_id, summary)

    return JSONResponse({"summary": summary, "summary_html": render_summary_markdown(summary)})


@app.post("/object/{simbad_main_id}/summary/regenerate", dependencies=[Depends(require_csrf_header)])
async def object_summary_regenerate(
    request: Request, simbad_main_id: str, current_user: User | None = Depends(get_current_user)
) -> JSONResponse:
    """Regenerate an object's AI narrative."""
    subject_type = "user" if current_user is not None else "session"
    subject_id = str(current_user.id) if current_user is not None else get_session_id(request)
    try:
        await check_limit(subject_type, subject_id)
    except RateLimitExceededError as exc:
        response = JSONResponse(
            {"error": "Rate limit exceeded", "retry_after_seconds": exc.retry_after_seconds},
            status_code=429,
        )
        response.headers["Retry-After"] = str(exc.retry_after_seconds)
        return response

    try:
        personal_api_key = personal_model = None
        if current_user is not None:
            personal_api_key, personal_model = await get_personal_gemini_key(current_user.id)
        summary = await regenerate_ai_summary(
            simbad_main_id, personal_api_key=personal_api_key, personal_model=personal_model
        )
    except LookupError:
        return JSONResponse({"error": "Object not found"}, status_code=404)
    except CooldownActiveError as exc:
        response = JSONResponse(
            {"error": "Cooldown active", "retry_after_seconds": exc.retry_after_seconds},
            status_code=429,
        )
        response.headers["Retry-After"] = str(exc.retry_after_seconds)
        return response
    except GeminiGenerationError as exc:
        logger.error("AI summary regeneration failed for %s: %s", simbad_main_id, exc)
        return _gemini_error_response(exc)

    await record_usage(subject_type, subject_id)

    if current_user is not None:
        object_id = await get_object_id_by_simbad_id(simbad_main_id)
        if object_id is not None:
            await save_user_summary_snapshot(current_user.id, object_id, summary)

    return JSONResponse(
        {
            "summary": summary,
            "summary_html": render_summary_markdown(summary),
            "cooldown_seconds": int(AI_SUMMARY_COOLDOWN.total_seconds()),
        }
    )


@app.get("/api/resolve")
async def api_resolve(
    request: Request,
    q: str | None = Query(None, max_length=200),
    current_user: User | None = Depends(get_current_user),
) -> JSONResponse:
    """Expose the resolver as a JSON API."""
    if not q:
        return JSONResponse({"error": "Missing query"}, status_code=400)

    subject_type = "resolve_user" if current_user is not None else "resolve_session"
    subject_id = str(current_user.id) if current_user is not None else get_session_id(request)
    try:
        await check_limit(subject_type, subject_id, limit=RESOLVE_RATE_LIMIT, window=RESOLVE_RATE_LIMIT_WINDOW)
    except RateLimitExceededError as exc:
        response = JSONResponse(
            {"error": "Rate limit exceeded", "retry_after_seconds": exc.retry_after_seconds},
            status_code=429,
        )
        response.headers["Retry-After"] = str(exc.retry_after_seconds)
        return response
    await record_usage(subject_type, subject_id)

    # Never generate an AI summary inline here: it's a second, independent
    # Gemini call site that doesn't go through the AI-summary rate limiter or
    # forward a personal API key, and (until store_result()'s belt-and-suspenders
    # try/except) a Gemini failure would crash this route and roll back an
    # otherwise-successful catalog resolution. Match /search's existing pattern:
    # clients that want a summary can fetch one via the rate-limited
    # /object/{id}/summary route.
    result = await get_or_resolve(q, generate_ai_summary=False)
    return JSONResponse(result.to_dict())


@app.get("/history", response_class=HTMLResponse)
async def history(request: Request, current_user: User | None = Depends(get_current_user)) -> HTMLResponse:
    """Show recently resolved objects."""
    objects = list_recent_objects(limit=10)
    if hasattr(objects, "__await__"):
        objects = await objects
    template = env.get_template("history.html")
    html = template.render(request=request, objects=objects, current_user=current_user)
    return HTMLResponse(content=html)


def _safe_next_path(next_path: str | None) -> str:
    """Validate a redirect target."""
    if not next_path:
        return "/"
    if not next_path.startswith("/"):
        return "/"
    if next_path.startswith("//") or next_path.startswith("/\\"):
        return "/"
    return next_path


@app.get("/register", response_class=HTMLResponse)
async def register_form(request: Request, next: str | None = None) -> HTMLResponse:
    """Render the registration form."""
    template = env.get_template("register.html")
    html = template.render(request=request, error=None, next=_safe_next_path(next) if next else None)
    return HTMLResponse(content=html)


@app.post("/register", response_class=HTMLResponse, response_model=None, dependencies=[Depends(require_csrf_form)])
async def register_submit(
    request: Request, email: str = Form(...), password: str = Form(...), next: str | None = Form(None)
) -> HTMLResponse | RedirectResponse:
    """Create a new user and log them in."""
    subject_id = get_session_id(request)
    try:
        await check_limit("auth", subject_id, limit=AUTH_RATE_LIMIT, window=AUTH_RATE_LIMIT_WINDOW)
    except RateLimitExceededError as exc:
        template = env.get_template("register.html")
        html = template.render(
            request=request,
            error=(
                "Too many attempts. "
                f"Please try again in about {exc.retry_after_seconds} seconds."
            ),
            next=next,
        )
        return HTMLResponse(content=html, status_code=429)

    normalized_email = email.strip().lower()
    try:
        user = await create_user(email=normalized_email, password=password)
    except DuplicateEmailError:
        await record_usage("auth", subject_id)
        template = env.get_template("register.html")
        html = template.render(request=request, error="That email is already registered.", next=next)
        return HTMLResponse(content=html, status_code=400)
    except ValueError as exc:
        await record_usage("auth", subject_id)
        template = env.get_template("register.html")
        html = template.render(request=request, error=str(exc), next=next)
        return HTMLResponse(content=html, status_code=400)

    await record_usage("auth", subject_id)
    log_in_session(request, user)
    return RedirectResponse(url=_safe_next_path(next), status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str | None = None) -> HTMLResponse:
    """Render the login form."""
    template = env.get_template("login.html")
    html = template.render(request=request, error=None, next=_safe_next_path(next) if next else None)
    return HTMLResponse(content=html)


@app.post("/login", response_class=HTMLResponse, response_model=None, dependencies=[Depends(require_csrf_form)])
async def login_submit(
    request: Request, email: str = Form(...), password: str = Form(...), next: str | None = Form(None)
) -> HTMLResponse | RedirectResponse:
    """Verify credentials and log the user in."""
    subject_id = get_session_id(request)
    try:
        await check_limit("auth", subject_id, limit=AUTH_RATE_LIMIT, window=AUTH_RATE_LIMIT_WINDOW)
    except RateLimitExceededError as exc:
        template = env.get_template("login.html")
        html = template.render(
            request=request,
            error=(
                "Too many attempts. "
                f"Please try again in about {exc.retry_after_seconds} seconds."
            ),
            next=next,
        )
        return HTMLResponse(content=html, status_code=429)

    user = await authenticate(email.strip().lower(), password)
    if user is None:
        # Only failed attempts count against the limit, so a legitimate user
        # who mistypes their password once isn't locked out.
        await record_usage("auth", subject_id)
        template = env.get_template("login.html")
        html = template.render(request=request, error="Incorrect email or password.", next=next)
        return HTMLResponse(content=html, status_code=400)

    log_in_session(request, user)
    return RedirectResponse(url=_safe_next_path(next), status_code=303)


@app.post("/logout", dependencies=[Depends(require_csrf_form)])
async def logout(request: Request) -> RedirectResponse:
    """Log the current user out."""
    log_out_session(request)
    return RedirectResponse(url="/", status_code=303)


@app.post("/object/{simbad_main_id}/favorite", response_model=None, dependencies=[Depends(require_csrf_form)])
async def favorite_object(
    request: Request, simbad_main_id: str, current_user: User | None = Depends(get_current_user)
) -> RedirectResponse | JSONResponse:
    """Favorite an object for the current user."""
    if current_user is None:
        return RedirectResponse(url=f"/login?next=/object/{quote(simbad_main_id, safe='')}", status_code=303)

    object_id = await get_object_id_by_simbad_id(simbad_main_id)
    if object_id is None:
        return JSONResponse({"error": "Object not found"}, status_code=404)

    await add_favorite(current_user.id, object_id)
    return RedirectResponse(url=f"/object/{quote(simbad_main_id, safe='')}", status_code=303)


@app.post("/object/{simbad_main_id}/unfavorite", response_model=None, dependencies=[Depends(require_csrf_form)])
async def unfavorite_object(
    request: Request, simbad_main_id: str, current_user: User | None = Depends(get_current_user)
) -> RedirectResponse | JSONResponse:
    """Remove an object from the current user's favorites."""
    if current_user is None:
        return RedirectResponse(url=f"/login?next=/object/{quote(simbad_main_id, safe='')}", status_code=303)

    object_id = await get_object_id_by_simbad_id(simbad_main_id)
    if object_id is None:
        return JSONResponse({"error": "Object not found"}, status_code=404)

    await remove_favorite(current_user.id, object_id)
    return RedirectResponse(url=f"/object/{quote(simbad_main_id, safe='')}", status_code=303)


@app.get("/account/saved", response_class=HTMLResponse, response_model=None)
async def account_saved(
    request: Request, current_user: User | None = Depends(get_current_user)
) -> HTMLResponse | RedirectResponse:
    """List the current user's favorite objects."""
    if current_user is None:
        return RedirectResponse(url="/login?next=/account/saved", status_code=303)

    favorites = await list_favorites(current_user.id)
    template = env.get_template("account_saved.html")
    html = template.render(request=request, current_user=current_user, favorites=favorites)
    return HTMLResponse(content=html)


def _render_account_settings(
    request: Request, current_user: User, *, message: str | None = None, error: str | None = None
) -> HTMLResponse:
    template = env.get_template("account_settings.html")
    html = template.render(
        request=request,
        current_user=current_user,
        has_personal_key=current_user.gemini_api_key_encrypted is not None,
        preferred_model=current_user.gemini_preferred_model or "",
        message=message,
        error=error,
    )
    return HTMLResponse(content=html, status_code=400 if error else 200)


@app.get("/account/settings", response_class=HTMLResponse, response_model=None)
async def account_settings_form(
    request: Request, current_user: User | None = Depends(get_current_user)
) -> HTMLResponse | RedirectResponse:
    """Show the personal Gemini API key settings form."""
    if current_user is None:
        return RedirectResponse(url="/login?next=/account/settings", status_code=303)
    return _render_account_settings(request, current_user)


@app.post(
    "/account/settings",
    response_class=HTMLResponse,
    response_model=None,
    dependencies=[Depends(require_csrf_form)],
)
async def account_settings_submit(
    request: Request,
    current_user: User | None = Depends(get_current_user),
    api_key: str = Form(""),
    preferred_model: str = Form(""),
    clear_key: str | None = Form(None),
) -> HTMLResponse | RedirectResponse:
    """Save, update, or clear the current user's personal Gemini API key."""
    if current_user is None:
        return RedirectResponse(url="/login?next=/account/settings", status_code=303)

    api_key = api_key.strip()
    preferred_model = preferred_model.strip()
    clearing = bool(clear_key)

    try:
        await update_personal_gemini_settings(
            current_user.id, api_key=api_key or None, preferred_model=preferred_model or None, clear=clearing
        )
    except ValueError as exc:
        return _render_account_settings(request, current_user, error=str(exc))

    if clearing:
        message = "Personal API key removed."
    elif api_key:
        message = "Personal API key saved."
    else:
        message = "Preferred model updated."

    refreshed_user = await get_user_by_id(current_user.id)
    return _render_account_settings(request, refreshed_user, message=message)
