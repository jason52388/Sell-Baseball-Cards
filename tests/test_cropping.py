"""Crops must come out right-side-up regardless of a photo's EXIF orientation."""
import io

from PIL import Image

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
