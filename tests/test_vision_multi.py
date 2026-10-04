"""Several images in one vision request (front + back), on every provider."""
import base64
import io
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from app.schemas import DetectedCard
from app.services import vision


def _jpeg(color="white", size=(300, 400)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


def test_anthropic_gets_one_block_per_image(monkeypatch):
    sent = {}

    def fake_claude(system, content, max_tokens=2048, model=None):
        sent["content"] = content
        return "{}"

    monkeypatch.setattr(vision, "_call_claude", fake_claude)
    vision._generate("sys", [_jpeg("red"), _jpeg("blue")], "go", provider="anthropic")
    kinds = [b["type"] for b in sent["content"]]
    assert kinds == ["image", "image", "text"]
    first = Image.open(io.BytesIO(base64.b64decode(sent["content"][0]["source"]["data"])))
    assert first.getpixel((5, 5))[0] > 200  # red first: order is kept


def test_gemini_gets_every_image(monkeypatch):
    sent = {}

    def fake_gemini(system, image_bytes, text, max_tokens, model=None):
        sent["images"] = image_bytes
        return "{}"

    monkeypatch.setattr(vision, "_gemini_generate", fake_gemini)
    vision._generate("sys", [_jpeg(), _jpeg()], "go", provider="gemini")
    assert isinstance(sent["images"], list) and len(sent["images"]) == 2


def test_claude_cli_reads_every_image_and_cleans_up(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, **kw):
        prompt = cmd[cmd.index("-p") + 1]
        paths = [p for p in prompt.split("'") if p.startswith(str(tmp_path))]
        seen["paths"] = paths
        seen["exist"] = [Path(p).exists() for p in paths]
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(vision, "get_settings", lambda: SimpleNamespace(
        vision_provider="claude_cli", claude_cli_model="claude-opus-5-5",
        claude_cli_timeout=60))
    monkeypatch.setattr(vision, "_CLI_TMP_DIR", tmp_path)
    monkeypatch.setattr(vision.subprocess, "run", fake_run)
    vision._generate("S", [_jpeg(), _jpeg()], "both sides")
    assert len(seen["paths"]) == 2 and all(seen["exist"])
    assert not any(Path(p).exists() for p in seen["paths"])


def test_verify_card_sends_back_when_given(monkeypatch):
    sent = {}

    def fake_generate(system, images, text, max_tokens=2048, **kw):
        sent["images"], sent["text"] = images, text
        return '{"agree": true}'

    monkeypatch.setattr(vision, "_generate", fake_generate)
    vision.verify_card(b"front", DetectedCard(player="A", subset="League Leaders"),
                       back_bytes=b"back")
    assert sent["images"] == [b"front", b"back"]
    assert "back" in sent["text"].lower()
    assert "League Leaders" in sent["text"]


def test_verify_card_front_only(monkeypatch):
    sent = {}
    monkeypatch.setattr(vision, "_generate", lambda s, images, t, **kw: (
        sent.update(images=images) or '{"agree": null}'))
    v = vision.verify_card(b"front", DetectedCard(player="A"))
    assert sent["images"] == [b"front"]
    assert v.agree is None
