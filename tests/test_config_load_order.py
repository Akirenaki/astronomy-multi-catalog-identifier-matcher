"""Tests that .env-only config values are honoured (F2 / TICKET-01).

These tests spawn a fresh subprocess for each case, since Python caches
imported modules -- re-importing `app.database` in-process would not
re-trigger its module-level code, so a subprocess boundary is required to
genuinely test import-order-sensitive behaviour.
"""

import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_CHECK_SCRIPT = textwrap.dedent(
    """
    import sys
    from app.database import engine
    sys.stdout.write(str(engine.url))
    """
)


def test_database_url_from_app_dotenv_is_honoured(tmp_path):
    """A DATABASE_URL set only in app/.env should be used, not the default."""
    work_dir = tmp_path / "workdir"
    shutil.copytree(ROOT / "app", work_dir / "app")
    (work_dir / "app" / ".env").write_text(
        "DATABASE_URL=sqlite+aiosqlite:///./from-dotenv.db\n"
    )

    env = {"PATH": subprocess.os.environ.get("PATH", "")}

    result = subprocess.run(
        [sys.executable, "-c", _CHECK_SCRIPT],
        cwd=work_dir,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "from-dotenv.db" in result.stdout
