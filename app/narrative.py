"""Gemini-backed natural-language summary generation."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from markupsafe import Markup

from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from google import genai
    from google.genai import types
    from google.genai.errors import APIError
else:
    try:
        from google import genai
        from google.genai import types
        from google.genai.errors import APIError
    except ImportError:  # pragma: no cover
        genai = None
        types = cast(Any, None)
        APIError = cast(Any, Exception)

try:
    from markdown_it import MarkdownIt
except ImportError:  # pragma: no cover
    MarkdownIt = None

logger = logging.getLogger(__name__)

SUMMARY_PROMPT = (
    "You are an astronomy professor writing for a general audience. "
    "Write a precise, analytical, and professional summary in Markdown. "
    "Prioritise accuracy over persuasion. Avoid excessive enthusiasm, "
    "motivational language, or unnecessary reassurance. Explain reasoning "
    "explicitly, distinguish facts from assumptions, and acknowledge "
    "uncertainty where appropriate. Use concise but complete paragraphs "
    "rather than overly short responses. Avoid rhetorical flourishes and "
    "exaggerated praise. Maintain a cordial but objective tone. "
    "Do not mention internal pipeline states, resolution labels, or database "
    "implementation details. Do not use headings or code fences. "
    "Use only the facts listed under 'Object data' below. Do not state "
    "additional facts, figures, or planets not listed there, even if you "
    "believe you know them independently."
)

_markdown_renderer = MarkdownIt("commonmark", {"html": False}) if MarkdownIt is not None else None


def _format_planet(planet: dict[str, Any]) -> str:
    parts: list[str] = []
    if planet.get("pl_name"):
        parts.append(f"name: {planet['pl_name']}")
    if planet.get("pl_letter"):
        parts.append(f"letter: {planet['pl_letter']}")
    if planet.get("orbital_period_days") is not None:
        parts.append(f"orbital period (days): {planet['orbital_period_days']}")
    if planet.get("planet_radius_earth") is not None:
        parts.append(f"radius (Earths): {planet['planet_radius_earth']}")
    if planet.get("discovery_year") is not None:
        parts.append(f"discovery year: {planet['discovery_year']}")
    if planet.get("discovery_method"):
        parts.append(f"discovery method: {planet['discovery_method']}")
    return "; ".join(parts) if parts else json.dumps(planet, sort_keys=True)


def _build_summary_prompt(payload: dict[str, Any]) -> str:
    lines = [SUMMARY_PROMPT, "", "Object data:"]

    if payload.get("main_id"):
        lines.append(f"- Main ID: {payload['main_id']}")
    if payload.get("spectral_type"):
        lines.append(f"- Spectral type: {payload['spectral_type']}")
    if payload.get("planet_count") is not None:
        lines.append(f"- Known exoplanets: {payload['planet_count']}")

    planets = payload.get("planets") or []
    if planets:
        lines.append("- Planet details:")
        for planet in planets:
            lines.append(f"  - {_format_planet(planet)}")

    return "\n".join(lines)


def render_summary_markdown(text: str | None) -> Markup:
    """Render AI output as HTML for the result page."""
    summary_text = text or "No summary available."
    if _markdown_renderer is None:
        return Markup.escape(summary_text).replace("\n", Markup("<br>\n"))
    return Markup(_markdown_renderer.render(summary_text))


def _init_client() -> Any | None:
    if genai is None:
        return None
    if not os.getenv("GEMINI_API_KEY"):
        return None
    try:
        return genai.Client()
    except Exception as e:
        logger.error("Failed to initialize Gemini client: %s", e)
        return None


client: Any | None = None


class GeminiGenerationError(Exception):
    """Raised when Gemini summary generation fails."""

    def __init__(self, user_message: str, *, retry_after_seconds: int | None = None) -> None:
        self.user_message = user_message
        self.retry_after_seconds = retry_after_seconds
        super().__init__(user_message)


class GeminiRateLimitedError(GeminiGenerationError):
    """Raised when Gemini reports a rate-limit or quota error."""


def _is_rate_limit_or_quota_error(error: Exception) -> bool:
    """Detect Gemini rate-limit and quota errors."""
    code = getattr(error, "code", None)
    status_code = getattr(error, "status_code", None)
    status = getattr(error, "status", None)
    
    # Extract the most descriptive string possible.
    err_detail = getattr(error, "message", str(error))
    message = str(err_detail).upper()

    if code == 429 or status_code == 429 or status == 429 or status == "RESOURCE_EXHAUSTED":
        return True

    # Check for common quota/rate-limit keywords in the error message.
    quota_markers = (
        "RESOURCE_EXHAUSTED",
        "RATE LIMIT",
        "RATE_LIMIT",
        "QUOTA",
        "RPM", #Request per Minute
        "TPM", #Tokens per Minute
        "TOKENS PER MINUTE",
        "REQUESTS PER MINUTE",
        "REQUESTS PER DAY",
        "RPD", #Requests per Day
        "TOO MANY REQUESTS",
    )
    return any(marker in message for marker in quota_markers)


def load_environment() -> None:
    """Load environment variables from the local .env files."""
    module_path = Path(__file__).resolve()
    app_dir = module_path.parent
    root_dir = module_path.parents[1]

    load_dotenv(dotenv_path=root_dir / ".env", override=False)
    load_dotenv(dotenv_path=app_dir / ".env", override=True)

    global client
    client = _init_client()


load_environment()


DEFAULT_GEMINI_MODEL = "gemini-3.6-flash"


def _build_personal_client(api_key: str) -> Any | None:
    """Build a one-off Gemini client from a user's personal API key.

    Used only as a fallback when the shared app key is rate-limited/quota-
    exhausted (see generate_summary() and README section III.A). Never
    cached/reused across requests -- a fresh client is built per fallback
    attempt from the (decrypted, in-memory only) key.
    """
    if genai is None:
        return None
    try:
        return genai.Client(api_key=api_key)
    except Exception as e:
        logger.error("Failed to initialize a personal Gemini client: %s", e)
        return None


async def _call_gemini(client_obj: Any, prompt: str, *, model: str) -> str:
    """Issue one generate_content call and translate failures into the typed
    GeminiGenerationError/GeminiRateLimitedError exceptions."""
    try:
        config = types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(
                thinking_level=types.ThinkingLevel.LOW,
            )
        )
        response = await client_obj.aio.models.generate_content(
            model=model,
            contents=prompt,
            config=config,
        )
        return response.text or "No summary available."

    except APIError as e:
        if _is_rate_limit_or_quota_error(e):
            logger.error(
                "Gemini rate limit/quota triggered (likely RPM/TPM/token budget exceeded). "
                "Technical details: %s",
                e,
            )
            raise GeminiRateLimitedError(
                "The AI summary service is receiving too many requests right now. "
                "Please wait a moment and try again.",
            ) from e

        # Catch remaining Gemini API errors (4xx/5xx and other SDK failures)
        logger.error("Gemini API error occurred: %s", e)
        raise GeminiGenerationError(
            "The AI summary service returned an error. Please try again in a moment.",
        ) from e
    except Exception as e:
        logger.exception("Unexpected failure while generating summary: %s", e)
        raise GeminiGenerationError(
            "The AI summary could not be generated due to an unexpected error. "
            "Please try again in a moment.",
        ) from e


async def generate_summary(
    payload: dict[str, Any],
    *,
    personal_api_key: str | None = None,
    personal_model: str | None = None,
) -> str:
    """Generate a plain-English summary of an astronomical object.

    Always tries the app's own shared Gemini key first, *if one is
    configured*. If the shared key specifically comes back rate-limited/
    quota-exhausted (GeminiRateLimitedError) and the caller has a
    personal_api_key configured, retries once against that key -- see README
    section III.A for the fallback semantics (shared key first, personal key
    only as a last resort; the resulting summary is still written to the one
    shared ai_summary column, benefiting every future visitor, not just the
    requesting user).

    If the shared key isn't configured at all (client is None -- e.g. a
    self-hoster deliberately running BYOK-only, per README §II), a supplied
    personal_api_key is tried directly instead of always falling through to
    "No summary available." (see F2, 2026 architectural audit round 4). This
    only changes behaviour for that specific "no shared key" configuration;
    the shared-key-first / rate-limit-triggered fallback above is unchanged.
    """
    if types is None:
        logger.warning(
            "The Gemini SDK is unavailable; skipping narrative generation. "
            "Returning default 'No summary available.' message."
        )
        return "No summary available."

    prompt = _build_summary_prompt(payload)

    if not client:
        if not personal_api_key:
            logger.warning(
                "GEMINI_API_KEY is not set and no personal key was supplied; skipping "
                "narrative generation. Returning default 'No summary available.' message."
            )
            return "No summary available."

        personal_client = _build_personal_client(personal_api_key)
        if personal_client is None:
            logger.warning(
                "GEMINI_API_KEY is not set and the personal key could not be used to "
                "build a client; skipping narrative generation. Returning default "
                "'No summary available.' message."
            )
            return "No summary available."

        logger.info("Shared Gemini key is not configured; using the requesting user's personal key.")
        return await _call_gemini(personal_client, prompt, model=personal_model or DEFAULT_GEMINI_MODEL)

    try:
        return await _call_gemini(client, prompt, model=DEFAULT_GEMINI_MODEL)
    except GeminiRateLimitedError:
        if not personal_api_key:
            raise

        personal_client = _build_personal_client(personal_api_key)
        if personal_client is None:
            # Couldn't even construct a client from the stored key -- surface
            # the original shared-key rate-limit error rather than a new,
            # more confusing one.
            raise

        logger.info("Shared Gemini key rate-limited; retrying with the requesting user's personal key.")
        return await _call_gemini(personal_client, prompt, model=personal_model or DEFAULT_GEMINI_MODEL)
