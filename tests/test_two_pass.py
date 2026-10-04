"""Size guard for vision requests and two-pass detection for multi-card photos."""
import io
import os
from types import SimpleNamespace

import pytest
from PIL import Image

from app.prompts.card_detection import CROP_USER, DETECTION_USER
from app.services import cropping, vision


def _jpeg(size, noisy=False):
    if noisy:
        img = Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3))
    else:
        img = Image.new("RGB", size, (200, 120, 60))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def _size(data):
    return Image.open(io.BytesIO(data)).size


def test_fit_downscales_long_edge():
    out = vision._fit_for_provider(_jpeg((5000, 3000)), max_edge=3000, max_bytes=10**8)
    assert max(_size(out)) == 3000
    assert _size(out)[1] == 1800  # aspect kept


def test_fit_leaves_small_images_alone():
    raw = _jpeg((800, 600))
    assert vision._fit_for_provider(raw, max_edge=3000, max_bytes=10**8) is raw


def test_fit_never_upscales():
    out = vision._fit_for_provider(_jpeg((400, 300)), max_edge=3000, max_bytes=10**8)
    assert _size(out) == (400, 300)


def test_fit_brings_bytes_under_the_limit():
    raw = _jpeg((1500, 1500), noisy=True)
    assert len(raw) > 400_000
    out = vision._fit_for_provider(raw, max_edge=3000, max_bytes=400_000)
    assert len(out) <= 400_000


def test_fit_passes_unreadable_bytes_through():
    assert vision._fit_for_provider(b"nope", max_edge=10, max_bytes=1) == b"nope"


def test_generate_caps_what_the_provider_sees(monkeypatch):
    sent = {}

    def fake_gemini(system, image_bytes, text, max_tokens, model=None):
        sent["img"] = image_bytes
        return "{}"

    monkeypatch.setattr(vision, "_gemini_generate", fake_gemini)
    vision._generate("s", _jpeg((4000, 3000)), "t", provider="gemini")
    assert max(_size(sent["img"])) == 3000
    vision._generate("s", _jpeg((4000, 3000)), "t", provider="gemini", max_edge=2000)
    assert max(_size(sent["img"])) == 2000


def test_padded_crop_only_grows_outward():
    photo = _jpeg((4000, 3000))
    crop = cropping.padded_crop_bytes(photo, [0.25, 0.25, 0.25, 0.25], pad=0.08)
    w, h = _size(crop)
    assert w >= 1000 and h >= 750  # never smaller than the box at full resolution
    neg = cropping.padded_crop_bytes(photo, [0.25, 0.25, 0.25, 0.25], pad=-0.2)
    assert _size(neg) == (1000, 750)  # a negative pad never cuts into the card


def _settings(**kw):
    base = dict(two_pass_detection=True, detection_pass1_max_edge=2000,
                two_pass_concurrency=1, max_cards=9, crop_padding_pct=0.08,
                vision_max_edge=3000, vision_max_bytes=10**8)
    base.update(kw)
    return SimpleNamespace(**base)


PASS1 = ('{"cards": ['
         '{"player": "Blurry A", "confidence": 0.5, "bbox": [0.0, 0.0, 0.5, 1.0], "side": "front"},'
         '{"player": "Blurry B", "confidence": 0.5, "bbox": [0.5, 0.0, 0.5, 1.0]}'
         ']}')


@pytest.fixture
def calls(monkeypatch):
    log = _Log()

    def fake_generate(system, images, text, max_tokens=2048, provider=None, model=None,
                      max_edge=None):
        log.append({"text": text, "max_edge": max_edge, "size": _size(images)})
        if text == DETECTION_USER:
            return log_pass1[0]
        n = sum(1 for c in log if c["text"] == CROP_USER)
        if fail_on[0] == n:
            raise RuntimeError("crop read failed")
        return ('{"cards": [{"player": "Sharp %d", "year": 1989, "card_number": "%d", '
                '"confidence": 0.9, "bbox": [0.05, 0.05, 0.9, 0.9]}]}' % (n, n))

    log_pass1 = [PASS1]
    fail_on = [None]
    monkeypatch.setattr(vision, "_generate", fake_generate)
    monkeypatch.setattr(vision, "get_settings", lambda: _settings())
    log.pass1, log.fail_on = log_pass1, fail_on
    return log


class _Log(list):
    pass


def test_two_pass_rereads_each_card_at_full_resolution(calls):
    photo = _jpeg((4000, 3000))
    cards = vision.detect_cards(photo)
    assert calls[0]["text"] == DETECTION_USER and calls[0]["max_edge"] == 2000
    crops = [c for c in calls if c["text"] == CROP_USER]
    assert len(crops) == 2
    assert all(c["size"][0] >= 2000 for c in crops)  # full-resolution crops
    assert [c.player for c in cards] == ["Sharp 1", "Sharp 2"]
    # boxes stay those of the whole photo, so crops are cut in the right place
    assert cards[0].bbox == [0.0, 0.0, 0.5, 1.0] and cards[1].bbox == [0.5, 0.0, 0.5, 1.0]
    assert cards[0].side == "front"


def test_single_card_photo_is_not_reread(calls):
    calls.pass1[0] = '{"cards": [{"player": "Solo", "bbox": [0.1, 0.1, 0.8, 0.8]}]}'
    cards = vision.detect_cards(_jpeg((4000, 3000)))
    assert [c.player for c in cards] == ["Solo"]
    assert len(calls) == 1


def test_failed_crop_read_keeps_the_pass1_card(calls):
    calls.fail_on[0] = 1
    cards = vision.detect_cards(_jpeg((4000, 3000)))
    assert [c.player for c in cards] == ["Blurry A", "Sharp 2"]


def test_two_pass_can_be_turned_off(calls, monkeypatch):
    monkeypatch.setattr(vision, "get_settings", lambda: _settings(two_pass_detection=False))
    cards = vision.detect_cards(_jpeg((4000, 3000)))
    assert [c.player for c in cards] == ["Blurry A", "Blurry B"]
    assert len(calls) == 1 and calls[0]["max_edge"] is None
