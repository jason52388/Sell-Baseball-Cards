"""Archiving card photos (source + crops) and database backup on add."""
import sqlite3

from app.config import get_settings
from app.services import photo_archive


# --- archive_source_files (move originals out of inbox) ---


def test_archive_moves_and_renames(tmp_path, monkeypatch):
    src_dir = tmp_path / "processed"
    src_dir.mkdir()
    dest = tmp_path / "collection"
    (src_dir / "IMG_1.jpeg").write_bytes(b"front")
    monkeypatch.setattr(photo_archive, "INBOX_PROCESSED_DIR", src_dir)
    monkeypatch.setattr(get_settings(), "collection_photos_dir", str(dest))

    moved = photo_archive.archive_source_files([
        ("IMG_1.jpeg", "Mike Trout, Topps Chrome, 2023", "front"),
        ("manual entry", "x", "front"),
        (None, "x", "front"),
    ])

    assert moved == 1
    assert (dest / "Mike Trout, Topps Chrome, 2023 (front).jpeg").exists()
    assert not (src_dir / "IMG_1.jpeg").exists()


def test_archive_disabled_when_path_blank(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "collection_photos_dir", "")
    assert photo_archive.archive_source_files([("whatever.jpeg", "card", "front")]) == 0


def test_archive_skips_missing_and_dedupes(tmp_path, monkeypatch):
    src_dir = tmp_path / "processed"
    src_dir.mkdir()
    dest = tmp_path / "collection"
    (src_dir / "A.jpeg").write_bytes(b"a")
    monkeypatch.setattr(photo_archive, "INBOX_PROCESSED_DIR", src_dir)
    monkeypatch.setattr(get_settings(), "collection_photos_dir", str(dest))

    moved = photo_archive.archive_source_files([
        ("A.jpeg", "Mike Trout, Topps, 2023", "front"),
        ("A.jpeg", "Mike Trout, Topps, 2023", "front"),
        ("gone.jpeg", "Other Player, Topps, 2023", "front"),
    ])

    assert moved == 1
    assert (dest / "Mike Trout, Topps, 2023 (front).jpeg").exists()


def test_archive_dedupes_same_description(tmp_path, monkeypatch):
    """Two different source files with the same card description get unique names."""
    src_dir = tmp_path / "processed"
    src_dir.mkdir()
    dest = tmp_path / "collection"
    (src_dir / "IMG_1.jpeg").write_bytes(b"one")
    (src_dir / "IMG_2.jpeg").write_bytes(b"two")
    monkeypatch.setattr(photo_archive, "INBOX_PROCESSED_DIR", src_dir)
    monkeypatch.setattr(get_settings(), "collection_photos_dir", str(dest))

    moved = photo_archive.archive_source_files([
        ("IMG_1.jpeg", "Mike Trout, Topps, 2023", "front"),
        ("IMG_2.jpeg", "Mike Trout, Topps, 2023", "front"),
    ])

    assert moved == 2
    assert (dest / "Mike Trout, Topps, 2023 (front).jpeg").exists()
    assert (dest / "Mike Trout, Topps, 2023 (front) (2).jpeg").exists()


def test_archive_front_and_back(tmp_path, monkeypatch):
    src_dir = tmp_path / "processed"
    src_dir.mkdir()
    dest = tmp_path / "collection"
    (src_dir / "IMG_F.jpeg").write_bytes(b"front")
    (src_dir / "IMG_B.jpeg").write_bytes(b"back")
    monkeypatch.setattr(photo_archive, "INBOX_PROCESSED_DIR", src_dir)
    monkeypatch.setattr(get_settings(), "collection_photos_dir", str(dest))

    moved = photo_archive.archive_source_files([
        ("IMG_F.jpeg", "Mike Trout, Topps, 2023", "front"),
        ("IMG_B.jpeg", "Mike Trout, Topps, 2023", "back"),
    ])

    assert moved == 2
    assert (dest / "Mike Trout, Topps, 2023 (front).jpeg").exists()
    assert (dest / "Mike Trout, Topps, 2023 (back).jpeg").exists()


# --- archive_crop_files (copy crops into collection) ---


def test_crop_archive_copies_front_and_back(tmp_path, monkeypatch):
    crops_dir = tmp_path / "crops"
    crops_dir.mkdir()
    dest = tmp_path / "collection"
    front_crop = crops_dir / "1-abc123.jpg"
    back_crop = crops_dir / "1-def456.jpg"
    front_crop.write_bytes(b"front-crop")
    back_crop.write_bytes(b"back-crop")
    monkeypatch.setattr(get_settings(), "collection_photos_dir", str(dest))

    copied = photo_archive.archive_crop_files([
        (str(front_crop), "Mike Trout, Topps Chrome, 2023, Refractor", "front"),
        (str(back_crop), "Mike Trout, Topps Chrome, 2023, Refractor", "back"),
    ])

    assert copied == 2
    assert (dest / "Mike Trout, Topps Chrome, 2023, Refractor (front).jpg").exists()
    assert (dest / "Mike Trout, Topps Chrome, 2023, Refractor (back).jpg").exists()
    # Originals are still in place (copied, not moved).
    assert front_crop.exists()
    assert back_crop.exists()


def test_crop_archive_disabled_when_path_blank(monkeypatch):
    monkeypatch.setattr(get_settings(), "collection_photos_dir", "")
    assert photo_archive.archive_crop_files([("/tmp/x.jpg", "card", "front")]) == 0


def test_crop_archive_skips_missing(tmp_path, monkeypatch):
    dest = tmp_path / "collection"
    monkeypatch.setattr(get_settings(), "collection_photos_dir", str(dest))

    copied = photo_archive.archive_crop_files([
        ("/nonexistent/crop.jpg", "Player, Set, 2023", "front"),
        (None, "Player, Set, 2023", "back"),
    ])

    assert copied == 0


def test_crop_archive_dedupes_same_description(tmp_path, monkeypatch):
    crops_dir = tmp_path / "crops"
    crops_dir.mkdir()
    dest = tmp_path / "collection"
    c1 = crops_dir / "1.jpg"
    c2 = crops_dir / "2.jpg"
    c1.write_bytes(b"one")
    c2.write_bytes(b"two")
    monkeypatch.setattr(get_settings(), "collection_photos_dir", str(dest))

    copied = photo_archive.archive_crop_files([
        (str(c1), "Mike Trout, Topps, 2023", "front"),
        (str(c2), "Mike Trout, Topps, 2023", "front"),
    ])

    assert copied == 2
    assert (dest / "Mike Trout, Topps, 2023 (front).jpg").exists()
    assert (dest / "Mike Trout, Topps, 2023 (front) (2).jpg").exists()


# --- backup_database ---


def test_backup_database(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    dest = tmp_path / "collection"
    db_path = data_dir / "cards.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO t VALUES (42)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(photo_archive, "DATA_DIR", data_dir)
    monkeypatch.setattr(get_settings(), "collection_photos_dir", str(dest))

    assert photo_archive.backup_database() is True

    backup = dest / "cards-backup.db"
    assert backup.exists()
    restored = sqlite3.connect(str(backup))
    rows = restored.execute("SELECT id FROM t").fetchall()
    restored.close()
    assert rows == [(42,)]


def test_backup_disabled_when_path_blank(monkeypatch):
    monkeypatch.setattr(get_settings(), "collection_photos_dir", "")
    assert photo_archive.backup_database() is False


def test_backup_overwrites_previous(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    dest = tmp_path / "collection"
    db_path = data_dir / "cards.db"

    monkeypatch.setattr(photo_archive, "DATA_DIR", data_dir)
    monkeypatch.setattr(get_settings(), "collection_photos_dir", str(dest))

    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.execute("INSERT INTO t VALUES ('first')")
    conn.commit()
    conn.close()
    photo_archive.backup_database()

    conn = sqlite3.connect(str(db_path))
    conn.execute("DELETE FROM t")
    conn.execute("INSERT INTO t VALUES ('second')")
    conn.commit()
    conn.close()
    photo_archive.backup_database()

    backup = sqlite3.connect(str(dest / "cards-backup.db"))
    val = backup.execute("SELECT v FROM t").fetchone()[0]
    backup.close()
    assert val == "second"
