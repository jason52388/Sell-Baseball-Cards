"""Vision provider selection (auto / anthropic / gemini) — no network."""
from pathlib import Path
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


def test_reanalysis_falls_back_when_strong_gemini_is_unavailable(monkeypatch):
    """Google retires Pro models and the free plan allows them zero requests;
    re-analysis must then use the regular Gemini model instead of failing."""
    from google.genai import errors

    from app.services import vision

    settings = vision.get_settings()
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "gemini_api_key", "k")
    calls = []

    def fake_gemini(system, image_bytes, text, max_tokens, model=None):
        calls.append(model)
        if model == settings.gemini_model_hq:
            raise errors.ClientError(429, {"error": {"message": "quota", "status": "RESOURCE_EXHAUSTED"}})
        return '{"cards": [{"player": "Mike Trout", "confidence": 0.9}]}'

    monkeypatch.setattr(vision, "_gemini_generate", fake_gemini)
    det, label = vision.reidentify_strongest(b"not-an-image")
    assert det.player == "Mike Trout"
    assert calls == [settings.gemini_model_hq, settings.gemini_model]
    assert label == "Gemini"


def test_claude_cli_reads_an_upright_copy_with_opus(monkeypatch, tmp_path):
    """The claude_cli provider runs headless Claude Code on the user's
    subscription: it is handed a file path to Read, the chosen model, and its
    stdout is the answer. The temp image is cleaned up afterwards."""
    import io

    from PIL import Image

    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        path = cmd[cmd.index("-p") + 1].split("'")[1]
        img = Image.open(path)
        seen["size"], seen["path"] = img.size, path
        return SimpleNamespace(returncode=0, stdout='{"cards": []}', stderr="")

    monkeypatch.setattr(vision, "get_settings", lambda: _settings(
        vision_provider="claude_cli", claude_cli_model="claude-opus-5-5",
        claude_cli_timeout=60))
    monkeypatch.setattr(vision, "_CLI_TMP_DIR", tmp_path)
    monkeypatch.setattr(vision.subprocess, "run", fake_run)

    buf = io.BytesIO()
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (400, 300), "white").save(buf, format="JPEG", exif=exif)
    out = vision._generate("SYSTEM", buf.getvalue(), "find cards")

    assert out == '{"cards": []}'
    cmd = seen["cmd"]
    assert cmd[0] == "claude"
    assert cmd[cmd.index("--model") + 1] == "claude-opus-5-5"
    assert cmd[cmd.index("--allowedTools") + 1] == "Read"
    assert cmd[cmd.index("--system-prompt") + 1] == "SYSTEM"
    assert seen["size"] == (300, 400)
    assert not Path(seen["path"]).exists()


def test_claude_cli_failure_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(vision, "get_settings", lambda: _settings(
        vision_provider="claude_cli", claude_cli_model="", claude_cli_timeout=60))
    monkeypatch.setattr(vision, "_CLI_TMP_DIR", tmp_path)
    monkeypatch.setattr(vision.subprocess, "run", lambda cmd, **kw: SimpleNamespace(
        returncode=1, stdout="", stderr="not logged in"))
    with pytest.raises(RuntimeError, match="not logged in"):
        vision._generate("S", b"\x89PNG\r\n\x1a\nxx", "t")


def test_strong_backend_uses_claude_cli_when_chosen(monkeypatch):
    monkeypatch.setattr(vision, "get_settings", lambda: _settings(
        vision_provider="claude_cli", claude_cli_model="claude-opus-5-5"))
    assert vision.strong_backend() == ("claude_cli", "claude-opus-5-5", "Claude")
