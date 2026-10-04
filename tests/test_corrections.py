"""Hand edits of identity fields are kept as a golden set of model mistakes."""
from app.db import get_db
from app.main import app
from app.models import IdentificationCorrection
from tests.test_api import _ingest_one, client  # noqa: F401  (fixture)
from tools.export_corrections import run as export


def _session():
    return next(app.dependency_overrides[get_db]())


def test_edit_records_model_read_and_final_value(client):  # noqa: F811
    card = _ingest_one(
        client, player="Pete Rose", year="1990", set_brand="Topps", card_number="505",
        field_reads={"year": {"value": "1990", "confidence": 0.6}},
    )["cards"][0]
    r = client.patch(f"/api/cards/{card['id']}", json={
        "year": "1989", "player": "Pete Rose", "subset": "Record Breaker", "rookie": True,
    })
    assert r.status_code == 200
    assert r.json()["subset"] == "Record Breaker" and r.json()["rookie"] is True

    rows = {c.field: c for c in _session().query(IdentificationCorrection).all()}
    assert set(rows) == {"year", "subset", "rookie"}  # unchanged player not recorded
    year = rows["year"]
    assert (year.card_id, year.model_value, year.previous_value, year.final_value) == (
        card["id"], "1990", "1990", "1989")
    assert year.crop_path == card["crop_path"]
    assert rows["rookie"].final_value == "True"


def test_export_writes_one_row_per_correction(client):  # noqa: F811
    card = _ingest_one(client, player="A", year="2001")["cards"][0]
    client.patch(f"/api/cards/{card['id']}", json={"card_number": "12"})
    out = export(_session())
    assert out[-1]["field"] == "card_number"
    assert out[-1]["final_value"] == "12" and out[-1]["previous_value"] is None
    assert out[-1]["created_at"]
