"""Archiving a card's original source photo to the collection folder on add."""
from app.config import get_settings
from app.services import photo_archive


def test_archive_moves_source_into_collection(tmp_path, monkeypatch):
    src_dir = tmp_path / "processed"
    src_dir.mkdir()
    dest = tmp_path / "collection"
    (src_dir / "IMG_1.jpeg").write_bytes(b"front")
    monkeypatch.setattr(photo_archive, "INBOX_PROCESSED_DIR", src_dir)
    monkeypatch.setattr(get_settings(), "collection_photos_dir", str(dest))

    moved = photo_archive.archive_source_files(["IMG_1.jpeg", "manual entry", None])

    assert moved == 1
    assert (dest / "IMG_1.jpeg").read_bytes() == b"front"
    assert not (src_dir / "IMG_1.jpeg").exists()  # moved, not copied


def test_archive_disabled_when_path_blank(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "collection_photos_dir", "")
    assert photo_archive.archive_source_files(["whatever.jpeg"]) == 0


def test_archive_skips_missing_and_dedupes(tmp_path, monkeypatch):
    src_dir = tmp_path / "processed"
    src_dir.mkdir()
    dest = tmp_path / "collection"
    (src_dir / "A.jpeg").write_bytes(b"a")
    monkeypatch.setattr(photo_archive, "INBOX_PROCESSED_DIR", src_dir)
    monkeypatch.setattr(get_settings(), "collection_photos_dir", str(dest))

    moved = photo_archive.archive_source_files(["A.jpeg", "A.jpeg", "gone.jpeg"])

    assert moved == 1  # A once, duplicate + missing skipped
    assert (dest / "A.jpeg").exists()
