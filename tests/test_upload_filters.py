"""Phantom-detection filtering, crop margins, and upload filename safety."""
from app.config import get_settings
from app.routers import upload
from app.routers.upload import _is_phantom_detection, _safe_source_name
from app.schemas import DetectedCard


def test_tiny_box_in_an_auto_detected_photo_is_a_phantom():
    """A sliver of table edge the vision model hallucinated as a card."""
    det = DetectedCard(player="Ghost", confidence=0.9, bbox=[0.0, 0.0, 0.05, 0.05])
    assert _is_phantom_detection(det) is True


def test_grid_cells_are_never_phantoms_however_dense_the_grid():
    """A 10x10 grid cell covers 1% of the photo — the area rule would delete
    every card in the batch after paying for 100 vision calls."""
    cell = DetectedCard(player="Real Card", confidence=0.9, bbox=[0.0, 0.0, 0.1, 0.1])
    assert _is_phantom_detection(cell, from_grid=True) is False


def test_unreadable_grid_cell_survives_as_a_low_confidence_preview():
    """The grid path promises every cell becomes a card the user can fix."""
    blank = DetectedCard(confidence=0.0, bbox=[0.0, 0.0, 0.33, 0.33])
    assert _is_phantom_detection(blank, from_grid=True) is False
    # ...but the same reading from auto-detection is still a phantom.
    assert _is_phantom_detection(blank) is True


def test_source_filenames_are_reduced_to_a_safe_basename():
    """The stored name is later joined to the inbox path and moved on promote."""
    assert "/" not in _safe_source_name("../../.env")
    assert "\\" not in _safe_source_name(r"..\..\secrets.env")
    assert _safe_source_name("/etc/passwd") == "passwd"
    assert _safe_source_name("photo.jpg") == "photo.jpg"
    assert _safe_source_name("") == "upload"
    assert _safe_source_name("..") == "upload"


# --- one card in the photo gets a looser crop ---

def _record_pads(monkeypatch) -> list:
    """Capture the pad each crop was taken with, without touching disk."""
    pads: list = []

    def fake_crop(image_bytes, bbox, card_id, *, pad=None):
        pads.append(pad)
        return None

    monkeypatch.setattr(upload.cropping, "crop_card", fake_crop)
    monkeypatch.setattr(upload, "preview_card", lambda card, db: None)
    return pads


def _run(dets, db_session, monkeypatch, **kw):
    pads = _record_pads(monkeypatch)
    upload._cards_from_detections("photo.jpg", b"", dets, db_session, verify=False, **kw)
    return pads


def test_single_card_photo_is_cropped_loosely(db_session, monkeypatch):
    """One card in frame: nothing else the margin could swallow, so a wide
    border is free while a tight box shaves the card's edge."""
    det = DetectedCard(player="Kerry Wood", confidence=0.9, bbox=[0.2, 0.2, 0.6, 0.6])
    pads = _run([det], db_session, monkeypatch)
    assert pads == [get_settings().single_card_pad_pct]


def test_multi_card_photo_keeps_the_tight_default(db_session, monkeypatch):
    """Loose margins would bleed a neighbouring card into the crop."""
    dets = [
        DetectedCard(player="A", confidence=0.9, bbox=[0.0, 0.0, 0.4, 0.9]),
        DetectedCard(player="B", confidence=0.9, bbox=[0.5, 0.0, 0.4, 0.9]),
    ]
    assert _run(dets, db_session, monkeypatch) == [None, None]


def test_phantoms_do_not_make_a_photo_look_multi_card(db_session, monkeypatch):
    """A hallucinated sliver alongside one real card is still a one-card photo."""
    dets = [
        DetectedCard(player="Kerry Wood", confidence=0.9, bbox=[0.2, 0.2, 0.6, 0.6]),
        DetectedCard(player="Ghost", confidence=0.9, bbox=[0.0, 0.0, 0.05, 0.05]),
    ]
    assert _run(dets, db_session, monkeypatch) == [get_settings().single_card_pad_pct]


def test_grid_cells_keep_the_tight_default(db_session, monkeypatch):
    """A 1x1 grid is a deliberate even split, not a loose-crop request."""
    det = DetectedCard(player="Kerry Wood", confidence=0.9, bbox=[0.0, 0.0, 1.0, 1.0])
    assert _run([det], db_session, monkeypatch, from_grid=True) == [None]
