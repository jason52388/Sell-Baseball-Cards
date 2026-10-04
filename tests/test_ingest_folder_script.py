"""tools/ingest_folder.sh with stand-ins for `claude` and `curl`: what happens
to each photo (ingested, already uploaded, no cards), unique names on move,
and non-photo files reported instead of silently skipped."""
import io
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "ingest_folder.sh"

FAKE_CLAUDE = """#!/usr/bin/env bash
echo '{"cards": [{"player": "X", "bbox": [0, 0, 1, 1]}]}'
"""

# The server's answer depends on the photo name: dup* -> 409, empty* -> 422.
FAKE_CURL = """#!/usr/bin/env bash
out=""; img=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o) out="$2"; shift ;;
    -F) [[ "$2" == image=@* ]] && img="${2#image=@}"; shift ;;
  esac
  shift
done
name="$(basename "$img")"
case "$name" in
  dup*) echo '{"detail":"already uploaded"}' >"$out"; printf 409 ;;
  empty*) echo '{"detail":"No cards found in detections JSON."}' >"$out"; printf 422 ;;
  *) echo '{}' >"$out"; printf 200 ;;
esac
"""


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (60, 40), (9, 9, 9)).save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture
def stubs(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("claude", FAKE_CLAUDE), ("curl", FAKE_CURL)):
        p = bin_dir / name
        p.write_text(body)
        p.chmod(0o755)
    return bin_dir


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_each_photo_lands_in_the_right_place(tmp_path, stubs):
    folder = tmp_path / "photos"
    folder.mkdir()
    for name in ("good.jpg", "dup.jpg", "empty.jpg", "notes.txt"):
        (folder / name).write_bytes(_jpeg() if name.endswith(".jpg") else b"hi")
    # A same-named file already processed must not be overwritten.
    (folder / "processed").mkdir()
    (folder / "processed" / "good.jpg").write_bytes(b"older")

    env = dict(os.environ, PATH=f"{stubs}:{os.environ['PATH']}", PYTHON=sys.executable)
    proc = subprocess.run(
        ["bash", str(SCRIPT), str(folder), "http://test"],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout
    assert "notes.txt" in out and "not photos" in out

    assert (folder / "processed" / "good.jpg").read_bytes() == b"older"
    assert (folder / "processed" / "good-2.jpg").exists()
    assert (folder / "duplicates" / "dup.jpg").exists()
    assert (folder / "failed" / "empty.jpg").exists()
    note = (folder / "failed" / "empty.jpg.txt").read_text()
    assert "No cards found" in note
    assert not (folder / "good.jpg").exists()
    assert (folder / "notes.txt").exists()
    assert "1 ingested, 1 already uploaded, 1 with no cards, 0 failed" in out
