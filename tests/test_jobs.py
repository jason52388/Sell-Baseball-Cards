"""Uploads and price refreshes run as background jobs: the request saves the
photos and returns a job id; one worker processes photos one at a time and
commits after every step; progress, repeats, restarts and retries."""
import io
import time

from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db
from app.main import app
from app.models import ITEM_FAILED, ITEM_WORKING, Card, ImageUpload, JobItem
from app.schemas import DetectedCard
from app.services import images, jobs, photo_archive, vision
from tests.test_api import _png_bytes, client  # noqa: F401  (fixture)


def _session():
    return next(app.dependency_overrides[get_db]())


def _jpeg(color=(10, 120, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (400, 300), color).save(buf, format="JPEG")
    return buf.getvalue()


def _upload(client, *files, **data):  # noqa: F811
    payload = [("files", f) for f in files]
    r = client.post("/api/upload", files=payload, data=data)
    assert r.status_code == 200, r.text
    return r.json()


def test_upload_returns_a_job_at_once_and_saves_each_original(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(jobs, "kick", lambda: None)  # the worker has not run yet
    data = _jpeg()
    job = _upload(client, ("IMG_1.jpg", data, "image/jpeg"))
    assert job["status"] == "queued" and job["total"] == 1 and job["done"] == 0
    photo = job["photos"][0]
    assert photo["state"] == "waiting" and photo["filename"] == "IMG_1.jpg"

    db = _session()
    up = db.get(ImageUpload, photo["upload_id"])
    assert up.sha256 == images.sha256(data)
    saved = photo_archive.INBOX_PROCESSED_DIR / up.stored_name
    assert saved.read_bytes() == data and up.stored_name != "IMG_1.jpg"

    # The page can poll it, and find it again after a refresh.
    assert client.get(f"/api/jobs/{job['job_id']}").json()["status"] == "queued"
    active = client.get("/api/jobs/active").json()["jobs"]
    assert [j["job_id"] for j in active] == [job["job_id"]]

    # The worker then processes it.
    assert jobs.run_pending() == 1
    done = client.get(f"/api/jobs/{job['job_id']}").json()
    assert done["status"] == "done" and done["photos"][0]["state"] == "done"
    assert len(done["photos"][0]["card_ids"]) == 2
    assert client.get("/api/jobs/active").json()["jobs"] == []


def test_progress_steps_are_reported(client, monkeypatch):  # noqa: F811
    from app.routers import upload as upload_router

    steps: list[str] = []
    real = upload_router._note

    def note(progress, step):
        steps.append(step)
        real(progress, step)

    monkeypatch.setattr(upload_router, "_note", note)
    _upload(client, ("p.png", _png_bytes(), "image/png"))
    assert steps[0] == "Finding cards"
    assert "Cropping 2 cards" in steps
    assert "Verifying card 1 of 2" in steps and "Pricing card 2 of 2" in steps


def test_slow_steps_run_with_no_write_pending(client, monkeypatch):  # noqa: F811
    """Verification is a ~20 s vision call: nothing may be waiting to be
    written (and so holding SQLite's write lock) while it runs."""
    from app.schemas import VerificationResult

    states = []

    def verify(crop, proposed, **kw):
        db = jobs_session["db"]
        states.append(bool(db.new or db.dirty or db.deleted))
        return VerificationResult(agree=True)

    jobs_session = {}
    real_factory = jobs.session_factory

    def factory():
        s = real_factory()
        jobs_session["db"] = s
        return s

    monkeypatch.setattr(jobs, "session_factory", factory)
    monkeypatch.setattr(vision, "verify_card", verify)
    _upload(client, ("p.png", _png_bytes(), "image/png"))
    assert states == [False, False]


def test_repeat_upload_is_skipped_unless_forced(client):  # noqa: F811
    first = _upload(client, ("a.png", _png_bytes(), "image/png"))
    ids = first["photos"][0]["card_ids"]

    again = _upload(client, ("a-copy.png", _png_bytes(), "image/png"))
    photo = again["photos"][0]
    assert photo["state"] == "done" and photo["duplicate"] is True
    assert photo["message"].startswith("already uploaded (cards #")
    assert sorted(photo["card_ids"]) == sorted(ids)
    assert photo["can_retry"] is True

    # Retry with force processes it after all.
    r = client.post(f"/api/jobs/{again['job_id']}/retry/0?force=true")
    assert r.status_code == 200
    forced = r.json()["photos"][0]
    assert forced["state"] == "done" and not forced["duplicate"]
    assert len(forced["card_ids"]) == 2 and set(forced["card_ids"]).isdisjoint(ids)

    # force=true at upload time skips the check too.
    direct = _upload(client, ("a.png", _png_bytes(), "image/png"), force="true")
    assert direct["photos"][0]["duplicate"] is False


def test_retrying_a_done_photo_without_force_is_refused(client):  # noqa: F811
    job = _upload(client, ("a.png", _png_bytes(), "image/png"))
    r = client.post(f"/api/jobs/{job['job_id']}/retry/0")
    assert r.status_code == 409
    assert client.post(f"/api/jobs/{job['job_id']}/retry/7").status_code == 404
    assert client.post("/api/jobs/nope/retry/0").status_code == 404


def test_a_failed_photo_is_retryable_and_stays_visible_until_dismissed(client, monkeypatch):  # noqa: F811
    def boom(image_bytes):
        raise RuntimeError("vision is down")

    monkeypatch.setattr(vision, "detect_cards", boom)
    job = _upload(client, ("a.png", _png_bytes(), "image/png"))
    photo = job["photos"][0]
    assert photo["state"] == "failed" and "vision is down" in photo["message"]
    assert job["failed"] == 1 and job["status"] == "done"
    # Still listed so a refresh shows it needs a retry.
    assert [j["job_id"] for j in client.get("/api/jobs/active").json()["jobs"]] == [job["job_id"]]

    monkeypatch.setattr(vision, "detect_cards", lambda b: [
        DetectedCard(player="Ken Griffey Jr.", year="1989", set_brand="Upper Deck",
                     card_number="1", confidence=0.95, bbox=[0.0, 0.0, 0.5, 0.5]),
    ])
    retried = client.post(f"/api/jobs/{job['job_id']}/retry/0").json()
    assert retried["photos"][0]["state"] == "done"
    assert len(retried["photos"][0]["card_ids"]) == 1
    # The detection failure no longer blocks a re-upload of these bytes either.
    assert _session().get(ImageUpload, photo["upload_id"]).error is None

    client.post(f"/api/jobs/{job['job_id']}/dismiss")
    assert client.get("/api/jobs/active").json()["jobs"] == []


def test_restart_fails_the_photo_in_flight_and_resumes_the_rest(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(jobs, "kick", lambda: None)
    job = _upload(client, ("a.png", _png_bytes(), "image/png"),
                  ("b.jpg", _jpeg(), "image/jpeg"))
    db = _session()
    items = db.query(JobItem).order_by(JobItem.idx).all()
    # Simulate a crash mid-way through photo 0, after it made a preview card.
    items[0].state = ITEM_WORKING
    db.add(Card(upload_id=items[0].upload_id, player="half done", status="preview"))
    db.commit()

    assert jobs.recover() == 1
    after = client.get(f"/api/jobs/{job['job_id']}").json()
    assert after["photos"][0]["state"] == "failed"
    assert after["photos"][0]["message"] == jobs.INTERRUPTED
    assert after["photos"][1]["state"] == "waiting"
    jobs.run_pending()
    assert client.get(f"/api/jobs/{job['job_id']}").json()["photos"][1]["state"] == "done"

    # Retry drops the half-finished preview before processing again.
    client.post(f"/api/jobs/{job['job_id']}/retry/0")
    jobs.run_pending()
    db = _session()
    assert db.query(Card).filter(Card.player == "half done").count() == 0
    assert client.get(f"/api/jobs/{job['job_id']}").json()["photos"][0]["state"] == "done"


def test_heic_without_a_converter_fails_with_a_clear_message(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(images, "_with_pillow_heif", lambda data: None)
    monkeypatch.setattr(images, "_with_sips", lambda data: None)
    job = _upload(client, ("IMG_9.HEIC", b"\x00\x00\x00\x18ftypheic0000", "image/heic"))
    photo = job["photos"][0]
    assert photo["state"] == "failed" and "HEIC not supported" in photo["message"]


def test_heic_is_converted_to_jpeg(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(images, "heic_to_jpeg", lambda data: _jpeg())
    job = _upload(client, ("IMG_9.heic", b"\x00\x00\x00\x18ftypheic0000", "image/heic"))
    photo = job["photos"][0]
    assert photo["filename"] == "IMG_9.jpg" and photo["state"] == "done"
    up = _session().get(ImageUpload, photo["upload_id"])
    assert up.stored_name.endswith(".jpg")


def test_reprice_job_skips_cards_on_ebay(client):  # noqa: F811
    from app.models import Listing

    a = client.post("/api/cards/manual", json={
        "player": "Ken Griffey Jr.", "year": "1989", "set_brand": "Upper Deck",
        "card_number": "1"}).json()
    b = client.post("/api/cards/manual", json={
        "player": "Ken Griffey Jr.", "year": "1989", "set_brand": "Upper Deck",
        "card_number": "2"}).json()
    db = _session()
    db.add(Listing(card_id=b["id"], ebay_mode="live", status="published", list_price=60.0))
    db.commit()
    job = client.post("/api/cards/reprice").json()
    msgs = {p["card_id"]: p["message"] for p in job["photos"]}
    assert msgs[a["id"]] == "$50.00 (sold)"
    assert msgs[b["id"]].startswith("skipped: on eBay")


def test_the_worker_thread_processes_jobs_in_the_background(tmp_path, monkeypatch):
    """The real thread, on a file database (no inline shortcut)."""
    engine = create_engine(f"sqlite:///{tmp_path / 'jobs.db'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(jobs, "INLINE", False)
    monkeypatch.setattr(jobs, "session_factory", Session)
    ran = []
    jobs.register("test-kind", lambda db, job, item, progress: ran.append(item.filename))
    with Session() as db:
        job = jobs.new_job(db, "test-kind")
        jobs.add_item(db, job, "one")
        jobs.add_item(db, job, "two")
        db.commit()
        job_id = job.id
    try:
        jobs.kick()
        deadline = time.time() + 5
        while time.time() < deadline and len(ran) < 2:
            time.sleep(0.05)
        assert ran == ["one", "two"]
        with Session() as db:
            from app.models import Job
            deadline = time.time() + 5
            while time.time() < deadline:
                db.expire_all()
                if db.get(Job, job_id).finished_at is not None:
                    break
                time.sleep(0.05)
            assert jobs.serialize(db.get(Job, job_id))["status"] == "done"
    finally:
        jobs.stop_worker()


def test_ingest_keeps_a_copy_in_the_inbox_and_refuses_repeats(client):  # noqa: F811
    import json as _json

    det = {"cards": [{"player": "Ken Griffey Jr.", "year": "1989", "confidence": 0.95,
                      "bbox": [0.0, 0.0, 0.5, 0.5]}]}
    data = _jpeg((1, 2, 3))
    r = client.post("/api/ingest", files={"image": ("IMG_5.jpg", data, "image/jpeg")},
                    data={"detections": _json.dumps(det)})
    assert r.status_code == 200
    up = _session().get(ImageUpload, r.json()["upload_id"])
    assert up.filename == "IMG_5.jpg" and up.stored_name.startswith("IMG_5-")
    assert (photo_archive.INBOX_PROCESSED_DIR / up.stored_name).read_bytes() == data

    again = client.post("/api/ingest", files={"image": ("IMG_5.jpg", data, "image/jpeg")},
                        data={"detections": _json.dumps(det)})
    assert again.status_code == 409 and "already uploaded" in again.json()["detail"]
    forced = client.post("/api/ingest", files={"image": ("IMG_5.jpg", data, "image/jpeg")},
                         data={"detections": _json.dumps(det), "force": "true"})
    assert forced.status_code == 200


def test_an_item_that_raises_fails_alone(monkeypatch):
    def handler(db, job, item, progress):
        if item.filename == "bad":
            raise ValueError("broken photo")
        item.message = "fine"

    jobs.register("test-mixed", handler)
    with jobs.session() as db:
        job = jobs.new_job(db, "test-mixed")
        jobs.add_item(db, job, "bad")
        jobs.add_item(db, job, "good")
        db.commit()
        job_id = job.id
    jobs.run_pending()
    with jobs.session() as db:
        from app.models import Job
        out = jobs.serialize(db.get(Job, job_id))
    assert [p["state"] for p in out["photos"]] == [ITEM_FAILED, "done"]
    assert "broken photo" in out["photos"][0]["message"]
    assert out["photos"][1]["message"] == "fine"
