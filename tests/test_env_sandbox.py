"""The suite must never touch the developer's real data directories.

Without this, running pytest archives fake cards into the real collection folder
(COLLECTION_PHOTOS_DIR, often an iCloud directory), writes crops into the real
data/crops, and runs schema migrations against the real cards.db.
"""
from app.config import ROOT_DIR, get_settings
from app.routers import upload
from app.services import cropping, photo_archive, ref_image


def _under_repo(path) -> bool:
    try:
        path.resolve().relative_to((ROOT_DIR / "data").resolve())
    except ValueError:
        return False
    return True


def test_photo_archiving_is_disabled_during_tests():
    assert not get_settings().collection_photos_dir


def test_image_directories_point_outside_the_real_data_folder():
    for path in (
        cropping.CROPS_DIR,
        ref_image.REF_IMAGES_DIR,
        upload.INBOX_DIR,
        photo_archive.INBOX_PROCESSED_DIR,
    ):
        assert not _under_repo(path), f"{path} is the real data directory"
