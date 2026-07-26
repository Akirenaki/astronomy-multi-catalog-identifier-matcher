"""Tests for environment loading and narrative helpers."""

import os

import pytest

from app import narrative


def test_load_environment_reads_app_dotenv(tmp_path, monkeypatch):
    """The app should load API keys from the app-local .env file when present."""
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / ".env").write_text("GEMINI_API_KEY=test-key\n")

    monkeypatch.setattr(narrative, "__file__", str(app_dir / "narrative.py"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    narrative.load_environment()

    assert os.getenv("GEMINI_API_KEY") == "test-key"


def test_render_summary_markdown_converts_markup():
    """Markdown summaries should render to safe HTML for the UI."""
    rendered = narrative.render_summary_markdown("Betelgeuse is **bright**.")

    assert "<strong>bright</strong>" in str(rendered)


@pytest.mark.asyncio
async def test_generate_summary_omits_state_and_uses_prompt_instructions(monkeypatch):
    """The Gemini prompt should exclude internal state labels and state the desired style."""
    captured: dict[str, str] = {}

    class FakeModels:
        async def generate_content(self, *, model, contents, config=None):
            captured["model"] = model
            captured["contents"] = contents
            captured["config"] = config
            return type("Response", (), {"text": "A concise summary."})()

    class FakeClient:
        def __init__(self):
            self.aio = type("AIO", (), {"models": FakeModels()})()

    monkeypatch.setattr(narrative, "client", FakeClient())

    payload = {
        "state": "RESOLVED",
        "main_id": "* alf Ori",
        "spectral_type": "M1-M2Ia-Iab",
        "planet_count": 1,
        "planets": [{"pl_name": "Betelgeuse b"}],
    }

    output = await narrative.generate_summary(payload)

    assert output == "A concise summary."
    assert captured["model"] == "gemini-3.6-flash"
    assert "RESOLVED" not in captured["contents"]
    assert "You are an astronomy professor" in captured["contents"]
    assert "Prioritise accuracy over persuasion" in captured["contents"]
