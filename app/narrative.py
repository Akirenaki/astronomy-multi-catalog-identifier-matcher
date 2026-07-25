"""Natural-language summary generation."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from markupsafe import Markup

try:
    from google import genai
    from google.genai.errors import APIError
except ImportError:  # pragma: no cover
    genai = None

    class APIError(Exception):
        """Fallback API error."""

        pass

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
    "implementation details. Do not use headings or code fences."
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


async def generate_summary(payload: dict[str, Any]) -> str:
    """Generate a plain-English summary of an astronomical object."""
    if not client:
        logger.warning(
            "GEMINI_API_KEY is not set -- skipping narrative generation. "
            "Returning default 'No summary available.' message."
        )
        return "No summary available."

    try:
        response = await client.aio.models.generate_content(
            model="gemini-3.5-flash",
            contents=_build_summary_prompt(payload),
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
