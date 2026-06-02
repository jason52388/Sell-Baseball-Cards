"""Vision provider selection (auto / anthropic / gemini) — no network."""
from types import SimpleNamespace

import pytest

from app.services import vision


def _settings(**kw):
    base = dict(vision_provider="auto", anthropic_api_key="", gemini_api_key="")
    base.update(kw)
    return SimpleNamespace(**base)


def test_auto_prefers_anthropic(monkeypatch):
    monkeypatch.setattr(vision, "get_settings",
                        lambda: _settings(anthropic_api_key="a", gemini_api_key="g"))
    assert vision._provider() == "anthropic"


def test_auto_uses_gemini_when_only_gemini(monkeypatch):
    monkeypatch.setattr(vision, "get_settings",
                        lambda: _settings(gemini_api_key="g"))
    assert vision._provider() == "gemini"


def test_explicit_gemini(monkeypatch):
    monkeypatch.setattr(vision, "get_settings",
                        lambda: _settings(vision_provider="gemini", anthropic_api_key="a"))
    assert vision._provider() == "gemini"


def test_no_keys_raises(monkeypatch):
    monkeypatch.setattr(vision, "get_settings", lambda: _settings())
    with pytest.raises(vision.MissingVisionKeyError):
        vision._provider()
