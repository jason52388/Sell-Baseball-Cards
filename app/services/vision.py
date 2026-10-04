"""Claude vision: detect cards in an image and (optionally) verify each one.

Network calls are isolated in small helpers so tests can monkeypatch
`detect_cards` / `_call_claude` without hitting the API.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import re
import subprocess
import uuid

from PIL import Image, ImageOps
from pydantic import ValidationError

from app.config import DATA_DIR, get_settings
from app.prompts.card_detection import (
    DETECTION_SYSTEM,
    DETECTION_USER,
    VERIFICATION_SYSTEM,
)
from app.schemas import DetectedCard, VerificationResult

logger = logging.getLogger("vision")

# Markdown code fences anywhere in the reply, not only at its very start/end:
# models often write a sentence, then the fenced JSON, then another sentence.
_FENCE_RE = re.compile(r"```(?:json)?", re.IGNORECASE)


def _strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", text).strip()


def _first_json_object(text: str) -> dict | None:
    """The first complete, parseable `{...}` object in `text`, or None.

    Recovers a JSON answer wrapped in prose ("Here is the result: {...} Hope
    that helps."). Braces inside strings are ignored while scanning."""
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(obj, dict):
                        return obj
                    break
        start = text.find("{", start + 1)
    return None


def _salvage_card_objects(text: str) -> list[dict]:
    """Recover complete card objects from truncated/invalid JSON.

    Scans the `cards` array brace-by-brace and json.loads each complete `{...}`
    object, skipping any trailing incomplete one. Lets a response cut off at the
    token limit still yield every fully-returned card.
    """
    anchor = text.find('"cards"')
    start = text.find("[", anchor if anchor != -1 else 0)
    if start == -1:
        return []
    objs: list[dict] = []
    buf = ""
    depth = 0
    in_obj = False
    in_str = False
    esc = False
    for ch in text[start + 1 :]:
        if in_str:
            buf += ch
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            buf += ch
            continue
        if ch == "{":
            depth += 1
            in_obj = True
            buf += ch
            continue
        if ch == "}":
            depth -= 1
            buf += ch
            if depth == 0 and in_obj:
                try:
                    objs.append(json.loads(buf))
                except json.JSONDecodeError:
                    pass
                buf = ""
                in_obj = False
            continue
        if in_obj:
            buf += ch
    return objs


def parse_detection(text: str) -> list[DetectedCard]:
    """Parse the model's JSON response into DetectedCard objects, defensively.

    Falls back to the first JSON object in surrounding prose, then to salvaging
    complete card objects if the JSON is truncated. Each card is validated on
    its own: one card the schema still rejects is logged and skipped, never
    allowed to fail the other cards in the photo.
    """
    cleaned = _strip_fences(text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        data = _first_json_object(cleaned)
        if data is None or "cards" not in data:
            data = _salvage_card_objects(cleaned)
            if not data:
                raise  # genuinely unparseable -> surface the error
    if isinstance(data, dict):
        raw_cards = data.get("cards") or []
    elif isinstance(data, list):
        raw_cards = data
    else:
        raw_cards = []

    cards: list[DetectedCard] = []
    for n, item in enumerate(raw_cards):
        if not isinstance(item, dict):
            continue
        try:
            cards.append(DetectedCard.model_validate(item))
        except ValidationError as exc:
            logger.warning("skipping card %d the model returned malformed: %s", n, exc)
    return cards


def parse_verification(text: str) -> VerificationResult:
    """Parse the verifier's reply, with the same prose/fence salvage as
    detection."""
    cleaned = _strip_fences(text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        data = _first_json_object(cleaned)
        if data is None:
            raise
    return VerificationResult.model_validate(data)


def _media_type(image_bytes: bytes) -> str:
    if image_bytes[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _upright(image_bytes: bytes) -> bytes:
    """Return the photo with its EXIF rotation applied to the pixels.

    Phone photos store pixels sideways plus an EXIF flag saying how to turn them.
    The vision models read the raw pixels and ignore the flag, while cropping
    applies it first, so a box read off the sideways pixels lands on the wrong
    part of the upright photo and the crop slices through the card. Sending the
    model the upright pixels keeps both on the same picture. Photos already
    upright (and anything Pillow can't read) pass through untouched.
    """
    try:
        img = Image.open(io.BytesIO(image_bytes))
        if img.getexif().get(0x0112, 1) == 1:
            return image_bytes
        img = ImageOps.exif_transpose(img).convert("RGB")
    except Exception:
        return image_bytes
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def _image_block(image_bytes: bytes) -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": _media_type(image_bytes),
            "data": base64.standard_b64encode(image_bytes).decode("ascii"),
        },
    }


class MissingVisionKeyError(RuntimeError):
    pass


def _client():
    from anthropic import Anthropic

    api_key = get_settings().anthropic_api_key
    if not api_key:
        raise MissingVisionKeyError(
            "No ANTHROPIC_API_KEY set. Add one to .env to identify cards from "
            "photos, or use 'Add a card manually' (no key needed)."
        )
    return Anthropic(api_key=api_key)


def _text_from_response(resp) -> str:
    parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    return "".join(parts)


def _call_claude(
    system: str, content: list[dict], max_tokens: int = 2048, model: str | None = None
) -> str:
    """Single Messages call with the system prompt cached."""
    settings = get_settings()
    resp = _client().messages.create(
        model=model or settings.anthropic_model,
        max_tokens=max_tokens,
        system=[
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": content}],
    )
    return _text_from_response(resp)


# --- Gemini backend ------------------------------------------------------


def _gemini_generate(
    system: str, image_bytes: bytes, text: str, max_tokens: int, model: str | None = None
) -> str:
    from google import genai
    from google.genai import types

    settings = get_settings()
    if not settings.gemini_api_key:
        raise MissingVisionKeyError(
            "No GEMINI_API_KEY set. Add one to .env to identify cards from photos, "
            "or use 'Add a card manually' (no key needed)."
        )
    gemini_model = model or settings.gemini_model
    client = genai.Client(api_key=settings.gemini_api_key)
    cfg_kwargs = dict(
        system_instruction=system,
        response_mime_type="application/json",
        max_output_tokens=max_tokens,
    )
    # Gemini 2.5 models "think" by default, which can consume the whole output
    # budget and return empty text. Disable it for fast, reliable JSON.
    if "2.5" in gemini_model:
        cfg_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
    resp = client.models.generate_content(
        model=gemini_model,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type=_media_type(image_bytes)),
            text,
        ],
        config=types.GenerateContentConfig(**cfg_kwargs),
    )
    return resp.text


# --- Claude Code CLI backend (Claude subscription, no API key) -----------

# Inside the project, so headless Claude may Read it.
_CLI_TMP_DIR = DATA_DIR / ".vision_tmp"


def _claude_cli_generate(system: str, image_bytes: bytes, text: str) -> str:
    """Ask headless Claude Code to Read the photo from disk and answer.

    The image arrives already upright (see `_generate`). It is written to a
    temp file because the CLI reads images through its Read tool, then removed.
    """
    settings = get_settings()
    _CLI_TMP_DIR.mkdir(parents=True, exist_ok=True)
    ext = {"image/png": ".png", "image/webp": ".webp"}.get(_media_type(image_bytes), ".jpg")
    path = _CLI_TMP_DIR / f"{uuid.uuid4().hex}{ext}"
    path.write_bytes(image_bytes)
    cmd = [
        "claude", "-p", f"{text}\n\nRead the image at the path '{path}' and answer "
        "with ONLY the JSON object, no other text.",
        "--system-prompt", system,
        "--allowedTools", "Read",
        "--add-dir", str(_CLI_TMP_DIR),
        "--output-format", "text",
    ]
    if settings.claude_cli_model:
        cmd += ["--model", settings.claude_cli_model]
    try:
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=settings.claude_cli_timeout,
            )
        except FileNotFoundError as exc:
            raise MissingVisionKeyError(
                "VISION_PROVIDER=claude_cli but `claude` (Claude Code) is not on PATH."
            ) from exc
    finally:
        path.unlink(missing_ok=True)
    if result.returncode != 0 or not result.stdout.strip():
        detail = (result.stderr or result.stdout or "no output").strip()[:500]
        raise RuntimeError(f"claude CLI failed: {detail}")
    return result.stdout


# --- Provider dispatch ---------------------------------------------------


def _provider(override: str | None = None) -> str:
    settings = get_settings()
    choice = (override or settings.vision_provider or "auto").lower()
    if choice == "auto":
        if settings.anthropic_api_key:
            return "anthropic"
        if settings.gemini_api_key:
            return "gemini"
        raise MissingVisionKeyError(
            "No vision API key set. Add ANTHROPIC_API_KEY or GEMINI_API_KEY to .env "
            "to identify cards from photos, or use 'Add a card manually'."
        )
    return choice


def _generate(
    system: str,
    image_bytes: bytes,
    text: str,
    max_tokens: int = 2048,
    provider: str | None = None,
    model: str | None = None,
) -> str:
    image_bytes = _upright(image_bytes)
    chosen = _provider(provider)
    if chosen == "claude_cli":
        return _claude_cli_generate(system, image_bytes, text)
    if chosen == "gemini":
        return _gemini_generate(system, image_bytes, text, max_tokens, model=model)
    return _call_claude(
        system,
        [_image_block(image_bytes), {"type": "text", "text": text}],
        max_tokens=max_tokens,
        model=model,
    )


def detect_cards(
    image_bytes: bytes, provider: str | None = None, model: str | None = None
) -> list[DetectedCard]:
    """Detect up to MAX_CARDS cards in one image.

    `provider`/`model` override the configured vision backend (used by the
    on-demand re-analysis path to escalate to a stronger model).
    """
    # Up to 9 cards with per-field detail is a large response — give it room so
    # the JSON isn't truncated mid-object.
    raw = _generate(
        DETECTION_SYSTEM, image_bytes, DETECTION_USER, max_tokens=8192,
        provider=provider, model=model,
    )
    cards = parse_detection(raw)
    return cards[: get_settings().max_cards]


def reidentify(
    crop_bytes: bytes, provider: str | None = None, model: str | None = None
) -> DetectedCard | None:
    """Re-run identification on a single-card crop, optionally with a stronger
    model. Returns the first detected card, or None if nothing was read."""
    cards = detect_cards(crop_bytes, provider=provider, model=model)
    return cards[0] if cards else None


def _model_unavailable(exc: Exception) -> bool:
    """A Gemini model that is retired (404) or has no allowance on this plan
    (429): the free plan allows the Pro models zero requests."""
    try:
        from google.genai import errors
    except ImportError:  # pragma: no cover
        return False
    return isinstance(exc, errors.ClientError) and getattr(exc, "code", None) in (404, 429)


def reidentify_strongest(crop_bytes: bytes) -> tuple[DetectedCard | None, str]:
    """Re-identify a crop with the strongest backend, falling back to the
    regular Gemini model when the strong one can't be used. Returns
    (detection, label of the model that answered)."""
    provider, model, label = strong_backend()
    try:
        return reidentify(crop_bytes, provider=provider, model=model), label
    except Exception as exc:  # noqa: BLE001
        settings = get_settings()
        if provider != "gemini" or model == settings.gemini_model or not _model_unavailable(exc):
            raise
    return reidentify(crop_bytes, provider="gemini", model=settings.gemini_model), "Gemini"


def strong_backend() -> tuple[str, str, str]:
    """Pick the strongest available identification backend for a re-analysis.

    Prefers Claude (Anthropic) when an Anthropic key is configured, else falls
    back to the high-quality Gemini model. Returns (provider, model, label).
    """
    settings = get_settings()
    if (settings.vision_provider or "").lower() == "claude_cli":
        return "claude_cli", settings.claude_cli_model, "Claude"
    if settings.anthropic_api_key:
        return "anthropic", settings.anthropic_model, "Claude"
    if settings.gemini_api_key:
        return "gemini", settings.gemini_model_hq, "Gemini Pro"
    raise MissingVisionKeyError(
        "No vision API key set. Add ANTHROPIC_API_KEY (Claude) or GEMINI_API_KEY "
        "to .env to re-analyze, or edit the card manually."
    )


def verify_card(crop_bytes: bytes, card: DetectedCard) -> VerificationResult:
    """Second-pass check of one card crop against its proposed identity."""
    proposed = card.model_dump(
        include={"player", "year", "set_brand", "card_number", "parallel", "serial_number"}
    )
    instruction = "Proposed identification:\n" + json.dumps(proposed, indent=2)
    raw = _generate(VERIFICATION_SYSTEM, crop_bytes, instruction, max_tokens=512)
    return parse_verification(raw)
