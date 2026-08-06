"""Tests for the personal-Gemini-key fallback in app/narrative.py::generate_summary
(README section III.A): the app's shared key is always tried first, when one is
configured; a caller-supplied personal_api_key is used as a retry if the shared
key specifically comes back rate-limited/quota-exhausted. If no shared key is
configured at all, a supplied personal_api_key is tried directly instead (see
F2, round-4 architectural audit).
"""

import pytest
from google.genai.errors import APIError

from app import narrative


def _rate_limit_api_error() -> APIError:
    return APIError(429, {"error": {"message": "RESOURCE_EXHAUSTED", "status": "RESOURCE_EXHAUSTED"}})


def _generic_api_error() -> APIError:
    return APIError(500, {"error": {"message": "internal error", "status": "INTERNAL"}})


class _FakeModels:
    """A fake `client.aio.models` that raises on the first call and, if a
    second call is made, returns a fixed response -- so tests can distinguish
    "shared key was tried" from "personal key was also tried"."""

    def __init__(self, first_call_error: Exception | None, second_call_text: str = "Fallback summary."):
        self.first_call_error = first_call_error
        self.second_call_text = second_call_text
        self.calls: list[str] = []

    async def generate_content(self, *, model, contents, config=None):
        self.calls.append(model)
        if len(self.calls) == 1 and self.first_call_error is not None:
            raise self.first_call_error
        return type("Response", (), {"text": self.second_call_text})()


class _FakeClient:
    def __init__(self, models: _FakeModels):
        self.aio = type("AIO", (), {"models": models})()


@pytest.mark.asyncio
async def test_no_personal_key_configured_raises_rate_limit_error_directly(monkeypatch):
    fake_models = _FakeModels(first_call_error=_rate_limit_api_error())
    monkeypatch.setattr(narrative, "client", _FakeClient(fake_models))

    with pytest.raises(narrative.GeminiRateLimitedError):
        await narrative.generate_summary({"main_id": "* alf Ori"})

    assert len(fake_models.calls) == 1  # no personal-key retry attempted


@pytest.mark.asyncio
async def test_personal_key_used_as_fallback_after_shared_key_rate_limited(monkeypatch):
    fake_models = _FakeModels(first_call_error=_rate_limit_api_error(), second_call_text="Personal-key summary.")
    monkeypatch.setattr(narrative, "client", _FakeClient(fake_models))

    captured_personal_client_kwargs: dict = {}

    def fake_genai_client(*, api_key):
        captured_personal_client_kwargs["api_key"] = api_key
        return _FakeClient(fake_models)

    monkeypatch.setattr(narrative.genai, "Client", fake_genai_client)

    result = await narrative.generate_summary(
        {"main_id": "* alf Ori"}, personal_api_key="my-personal-key", personal_model="gemini-2.5-pro"
    )

    assert result == "Personal-key summary."
    assert len(fake_models.calls) == 2
    assert fake_models.calls[1] == "gemini-2.5-pro"
    assert captured_personal_client_kwargs["api_key"] == "my-personal-key"


@pytest.mark.asyncio
async def test_personal_key_defaults_to_shared_model_name_when_no_preferred_model_set(monkeypatch):
    fake_models = _FakeModels(first_call_error=_rate_limit_api_error())
    monkeypatch.setattr(narrative, "client", _FakeClient(fake_models))
    monkeypatch.setattr(narrative.genai, "Client", lambda *, api_key: _FakeClient(fake_models))

    await narrative.generate_summary({"main_id": "* alf Ori"}, personal_api_key="my-personal-key")

    assert fake_models.calls[1] == narrative.DEFAULT_GEMINI_MODEL


@pytest.mark.asyncio
async def test_non_rate_limit_error_does_not_trigger_personal_key_fallback(monkeypatch):
    """A generic (non-rate-limit) Gemini error must not trigger the personal-key
    retry -- the fallback is specifically for rate-limit/quota exhaustion."""
    fake_models = _FakeModels(first_call_error=_generic_api_error())
    monkeypatch.setattr(narrative, "client", _FakeClient(fake_models))
    monkeypatch.setattr(
        narrative.genai, "Client", lambda *, api_key: pytest.fail("personal client should not be built")
    )

    with pytest.raises(narrative.GeminiGenerationError) as excinfo:
        await narrative.generate_summary({"main_id": "* alf Ori"}, personal_api_key="my-personal-key")

    assert not isinstance(excinfo.value, narrative.GeminiRateLimitedError)
    assert len(fake_models.calls) == 1


@pytest.mark.asyncio
async def test_personal_key_also_failing_surfaces_its_own_error(monkeypatch):
    """If the personal-key retry also fails, that failure (not a swallowed
    original error) must propagate to the caller."""
    shared_models = _FakeModels(first_call_error=_rate_limit_api_error())
    monkeypatch.setattr(narrative, "client", _FakeClient(shared_models))

    personal_models = _FakeModels(first_call_error=_generic_api_error())
    monkeypatch.setattr(narrative.genai, "Client", lambda *, api_key: _FakeClient(personal_models))

    with pytest.raises(narrative.GeminiGenerationError) as excinfo:
        await narrative.generate_summary({"main_id": "* alf Ori"}, personal_api_key="my-personal-key")

    assert not isinstance(excinfo.value, narrative.GeminiRateLimitedError)
    assert len(shared_models.calls) == 1
    assert len(personal_models.calls) == 1


@pytest.mark.asyncio
async def test_personal_key_used_directly_when_shared_client_unconfigured(monkeypatch):
    """F2 (round 4): when GEMINI_API_KEY isn't set at all (client is None), a
    supplied personal_api_key must be tried directly, rather than always
    falling through to 'No summary available.' -- see README section III.A
    and F2 in the round-4 architectural audit."""
    monkeypatch.setattr(narrative, "client", None)

    fake_models = _FakeModels(first_call_error=None, second_call_text="Personal-key-only summary.")
    captured_personal_client_kwargs: dict = {}

    def fake_genai_client(*, api_key):
        captured_personal_client_kwargs["api_key"] = api_key
        return _FakeClient(fake_models)

    monkeypatch.setattr(narrative.genai, "Client", fake_genai_client)

    result = await narrative.generate_summary(
        {"main_id": "* alf Ori"}, personal_api_key="my-personal-key", personal_model="gemini-2.5-pro"
    )

    assert result == "Personal-key-only summary."
    assert len(fake_models.calls) == 1
    assert fake_models.calls[0] == "gemini-2.5-pro"
    assert captured_personal_client_kwargs["api_key"] == "my-personal-key"


@pytest.mark.asyncio
async def test_no_summary_when_shared_client_unconfigured_and_no_personal_key(monkeypatch):
    """When the shared client is unconfigured and no personal key is supplied,
    the original safe fallback message is preserved unchanged."""
    monkeypatch.setattr(narrative, "client", None)

    result = await narrative.generate_summary({"main_id": "* alf Ori"})

    assert result == "No summary available."


@pytest.mark.asyncio
async def test_no_summary_when_shared_client_unconfigured_and_personal_client_fails(monkeypatch):
    """If the shared client is unconfigured and the personal key can't even
    build a client, fail closed to the same default message rather than
    raising."""
    monkeypatch.setattr(narrative, "client", None)
    monkeypatch.setattr(narrative.genai, "Client", lambda *, api_key: (_ for _ in ()).throw(RuntimeError("boom")))

    result = await narrative.generate_summary({"main_id": "* alf Ori"}, personal_api_key="my-personal-key")

    assert result == "No summary available."
