"""tools/upright_copy: the folder ingest hands Claude an upright copy of each
photo, so the boxes it reads line up with the upright photo the cropper cuts."""
import io

from PIL import Image

from tools.upright_copy import write_upright_copy


def _photo(path, orientation=None, fmt="JPEG"):
    img = Image.new("RGB", (400, 300), "white")
    kwargs = {}
    if orientation:
        exif = Image.Exif()
        exif[274] = orientation
        kwargs["exif"] = exif
    img.save(path, format=fmt, **kwargs)


def test_sideways_photo_becomes_upright_jpeg(tmp_path):
    src = tmp_path / "IMG_1.jpeg"
    _photo(src, orientation=6)
    out = write_upright_copy(src, tmp_path / "work")
    img = Image.open(out)
    assert img.size == (300, 400)
    assert img.getexif().get(274, 1) == 1
    assert out.suffix == ".jpg"


def test_upright_photo_is_copied_unchanged(tmp_path):
    src = tmp_path / "card.png"
    _photo(src, fmt="PNG")
    out = write_upright_copy(src, tmp_path / "work")
    assert out.read_bytes() == src.read_bytes()
    assert out.suffix == ".png"
