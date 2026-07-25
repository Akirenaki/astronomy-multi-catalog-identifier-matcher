"""FastAPI app entry point."""

import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from markupsafe import Markup
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.middleware.sessions import SessionMiddleware

from app.auth import (
    DuplicateEmailError,
    authenticate,
    create_user,
    get_csrf_token,
    get_current_user,
    get_session_id,
    log_in_session,
    log_out_session,
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
from app.database import init_db
from app.models import User
from app.narrative import GeminiGenerationError, GeminiRateLimitedError, render_summary_markdown
from app.ratelimit import RateLimitExceededError, check_limit, record_usage

# Configure logging.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize the database on startup."""
    await init_db()
    yield


app = FastAPI(title="Astronomy Multi-Catalog Cross-Matcher", lifespan=lifespan)

# Session cookies back login state and anonymous rate limiting.
_session_secret_key = os.getenv("SESSION_SECRET_KEY")
if not _session_secret_key:
    _session_secret_key = secrets.token_hex(32)
    logger.warning(
        "SESSION_SECRET_KEY not set; using a randomly generated key for this "
        "process only. Sessions will not survive a restart. Set SESSION_SECRET_KEY "
        "explicitly before deploying anywhere beyond local single-process dev."
    )
app.add_middleware(SessionMiddleware, secret_key=_session_secret_key)

# Serve static assets.
app.mount("/static", StaticFiles(directory="app/static"), name="static")

def _tojson(value) -> Markup:
    """Render a Python value as a JSON literal for templates."""
    return Markup(json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


# Load templates with HTML escaping enabled.
env = Environment(loader=FileSystemLoader("app/templates"), autoescape=select_autoescape(["html"]))
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
    request: Request, q: str | None = None, current_user: User | None = Depends(get_current_user)
) -> HTMLResponse:
    """Resolve a search query or show the landing page when empty."""
    if not q:
        template = env.get_template("index.html")
        html = template.render(request=request, current_user=current_user)
        return HTMLResponse(content=html)

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


@app.get("/object/{simbad_main_id}/summary")
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
        summary = await ensure_ai_summary(simbad_main_id)
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
        summary = await regenerate_ai_summary(simbad_main_id)
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

    return JSONResponse({"summary": summary, "cooldown_seconds": int(AI_SUMMARY_COOLDOWN.total_seconds())})


@app.get("/api/resolve")
async def api_resolve(q: str | None = None) -> JSONResponse:
    """Expose the resolver as a JSON API."""
    if not q:
        return JSONResponse({"error": "Missing query"}, status_code=400)

    result = await get_or_resolve(q)
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
    normalized_email = email.strip().lower()
    try:
        user = await create_user(email=normalized_email, password=password)
    except DuplicateEmailError:
        template = env.get_template("register.html")
        html = template.render(request=request, error="That email is already registered.", next=next)
        return HTMLResponse(content=html, status_code=400)
    except ValueError as exc:
        template = env.get_template("register.html")
        html = template.render(request=request, error=str(exc), next=next)
        return HTMLResponse(content=html, status_code=400)

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
    user = await authenticate(email.strip().lower(), password)
    if user is None:
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
