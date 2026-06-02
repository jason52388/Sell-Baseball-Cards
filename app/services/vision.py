"""Claude vision: detect cards in an image and (optionally) verify each one.

Network calls are isolated in small helpers so tests can monkeypatch
`detect_cards` / `_call_claude` without hitting the API.
"""
from __future__ import annotations

import base64
import json
import re

from app.config import get_settings
from app.prompts.card_detection import (
    DETECTION_SYSTEM,
    DETECTION_USER,
    VERIFICATION_SYSTEM,
)
from app.schemas import DetectedCard, VerificationResult

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", text.strip())


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

    Falls back to salvaging complete card objects if the JSON is truncated.
    """
    cleaned = _strip_fences(text)
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            raw_cards = data.get("cards", [])
        elif isinstance(data, list):
            raw_cards = data
        else:
            raw_cards = []
    except json.JSONDecodeError:
        raw_cards = _salvage_card_objects(cleaned)
        if not raw_cards:
            raise  # genuinely unparseable -> surface the error

    cards: list[DetectedCard] = []
    for item in raw_cards:
        if isinstance(item, dict):
            cards.append(DetectedCard.model_validate(item))
    return cards


def parse_verification(text: str) -> VerificationResult:
    cleaned = _strip_fences(text)
    data = json.loads(cleaned)
    return VerificationResult.model_validate(data)


def _media_type(image_bytes: bytes) -> str:
    if image_bytes[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


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
    if _provider(provider) == "gemini":
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


def strong_backend() -> tuple[str, str, str]:
    """Pick the strongest available identification backend for a re-analysis.

    Prefers Claude (Anthropic) when an Anthropic key is configured, else falls
    back to the high-quality Gemini model. Returns (provider, model, label).
    """
    settings = get_settings()
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
