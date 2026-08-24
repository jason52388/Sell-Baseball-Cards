"""Crop geometry: upright regardless of EXIF, padded, and never zoomed into."""
import io

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.config import get_settings
from app.services import cropping


def _camera_jpeg(orientation: int) -> bytes:
    """A portrait 100x200 image stored the way a phone does: pixels rotated to
    landscape with an EXIF Orientation tag telling viewers to rotate it back."""
    upright = Image.new("RGB", (100, 200), "white")
    landscape = upright.rotate(-90, expand=True)  # 200x100 pixels on disk
    exif = landscape.getexif()
    exif[274] = orientation  # 274 = EXIF Orientation
    buf = io.BytesIO()
    landscape.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


def test_crop_honors_exif_orientation(tmp_path, monkeypatch):
    monkeypatch.setattr(cropping, "CROPS_DIR", tmp_path)
    path = cropping.crop_card(_camera_jpeg(6), [0, 0, 1, 1], 1)
    assert path is not None
    # Upright portrait (100x200), not the sideways stored buffer (200x100).
    assert Image.open(path).size == (100, 200)


def test_crop_normal_orientation_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(cropping, "CROPS_DIR", tmp_path)
    buf = io.BytesIO()
    Image.new("RGB", (120, 240), "white").save(buf, format="JPEG")
    path = cropping.crop_card(buf.getvalue(), [0, 0, 1, 1], 2)
    assert Image.open(path).size == (120, 240)


def test_crop_pads_the_box(tmp_path, monkeypatch):
    monkeypatch.setattr(cropping, "CROPS_DIR", tmp_path)
    buf = io.BytesIO()
    Image.new("RGB", (100, 100), "white").save(buf, format="JPEG")
    # 50x50 centre box, padded 10% of its size per side -> grows to 60x60.
    path = cropping.crop_card(buf.getvalue(), [0.25, 0.25, 0.5, 0.5], 3, pad=0.1)
    assert Image.open(path).size == (60, 60)


def test_crop_padding_clamped_to_image(tmp_path, monkeypatch):
    monkeypatch.setattr(cropping, "CROPS_DIR", tmp_path)
    buf = io.BytesIO()
    Image.new("RGB", (100, 100), "white").save(buf, format="JPEG")
    # A full-frame box stays full even with padding (margin clamps at the edges).
    path = cropping.crop_card(buf.getvalue(), [0, 0, 1, 1], 4, pad=0.2)
    assert Image.open(path).size == (100, 100)


def test_grid_cells_splits_evenly():
    buf = io.BytesIO()
    Image.new("RGB", (300, 600), "white").save(buf, format="JPEG")
    cells = cropping.grid_cells(buf.getvalue(), rows=3, cols=3)
    # 3x3 grid, row-major order, each cell evenly sized.
    assert len(cells) == 9
    bboxes = [bbox for bbox, _ in cells]
    assert bboxes[0] == [0.0, 0.0, 1 / 3, 1 / 3]  # top-left
    assert bboxes[8] == [2 / 3, 2 / 3, 1 / 3, 1 / 3]  # bottom-right
    # Each cell decodes to a 100x200 image (300/3 x 600/3).
    for _, cell_bytes in cells:
        assert Image.open(io.BytesIO(cell_bytes)).size == (100, 200)


def test_delete_crop_is_best_effort(tmp_path):
    f = tmp_path / "1.jpg"
    f.write_bytes(b"x")
    cropping.delete_crop(str(f))
    assert not f.exists()
    # No-ops on missing path / None instead of raising.
    cropping.delete_crop(str(f))
    cropping.delete_crop(None)


# --- straightening may enlarge the view, never shrink past the card ---

def _card_on_white(rect: tuple[int, int, int, int]) -> bytes:
    """A 500x700 white photo with one solid dark rectangle drawn on it."""
    img = Image.new("RGB", (500, 700), "white")
    ImageDraw.Draw(img).rectangle(rect, fill=(20, 20, 20))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def test_covers_card_rejects_a_shape_inside_the_card():
    """The art window on a card front: card-shaped, but short of the edges."""
    card = (0, 0, 400, 560)
    whole = np.array([[0, 0], [400, 0], [400, 560], [0, 560]], dtype="float32")
    art = np.array([[30, 45], [370, 45], [370, 515], [30, 515]], dtype="float32")
    assert cropping._covers_card(whole, card) is True
    assert cropping._covers_card(art, card) is False
    # No rectangle to protect (e.g. a replacement photo) -> nothing to reject.
    assert cropping._covers_card(art, None) is True


@pytest.mark.skipif(not cropping._CV_OK, reason="OpenCV not installed")
def test_straightening_ignores_a_rectangle_inside_the_card(tmp_path, monkeypatch):
    """This is the bug the guard exists for: the only strong edge in the photo
    is a card-shaped block INSIDE the card, and warping to it used to crop away
    the border, name plate, and card number."""
    monkeypatch.setattr(cropping, "CROPS_DIR", tmp_path)
    monkeypatch.setattr(get_settings(), "crop_autostraighten", True)
    # Detected card spans x 50..450, y 70..630; the dark block is well inside it.
    photo = _card_on_white((80, 115, 420, 585))
    path = cropping.crop_card(photo, [0.1, 0.1, 0.8, 0.8], 10, pad=0.08)
    # Crop stays the padded bbox, not the 340x470 inner block.
    assert Image.open(path).size == (464, 649)


@pytest.mark.skipif(not cropping._CV_OK, reason="OpenCV not installed")
def test_straightening_still_trims_to_the_real_card(tmp_path, monkeypatch):
    """The guard must not disable deskew: a shape that does reach the card's
    own edges is still straightened and the background trimmed off."""
    monkeypatch.setattr(cropping, "CROPS_DIR", tmp_path)
    monkeypatch.setattr(get_settings(), "crop_autostraighten", True)
    photo = _card_on_white((50, 70, 450, 630))  # exactly the detected card
    path = cropping.crop_card(photo, [0.1, 0.1, 0.8, 0.8], 11, pad=0.08)
    w, h = Image.open(path).size
    assert abs(w - 400) <= 8 and abs(h - 560) <= 8
