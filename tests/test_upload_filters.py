"""Phantom-detection filtering and upload filename safety."""
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
