"""Environment loading for import-time configuration."""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv


def load_environment() -> None:
    """Load environment variables from the local .env files.

    Root `.env` is loaded first (without overriding real process
    environment variables), then `app/.env` is loaded with override=True
    so it takes precedence over the root `.env`.
    """
    module_path = Path(__file__).resolve()
    app_dir = module_path.parent
    root_dir = module_path.parents[1]

    load_dotenv(dotenv_path=root_dir / ".env", override=False)
    load_dotenv(dotenv_path=app_dir / ".env", override=True)
