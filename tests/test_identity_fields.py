"""Price-driving identity fields: subset, team, rookie.

`parallel` is only for finish or numbering variants (Gold, Refractor, /99);
an insert or subset name ("League Leaders", "Record Breaker") goes in `subset`.
"""
from sqlalchemy import create_engine

from app.models import Card
from app.prompts.card_detection import DETECTION_SYSTEM, VERIFICATION_SYSTEM
from app.routers.upload import _apply_detection
from app.schemas import CardOut, CardUpdateRequest, DetectedCard
from app.services import vision
from app.services.pairing import enrich_front_from_back


def test_detection_reads_subset_team_and_rookie():
    raw = ('{"cards": [{"player": "Ken Griffey Jr.", "team": "Seattle Mariners", '
           '"subset": "Star Rookie", "rookie": "RC", "parallel": null}]}')
    card = vision.parse_detection(raw)[0]
    assert card.subset == "Star Rookie"
    assert card.team == "Seattle Mariners"
    assert card.rookie is True


def test_rookie_defaults_to_false():
    assert vision.parse_detection('{"cards": [{"rookie": null}]}')[0].rookie is False


def test_apply_detection_stores_new_fields():
    card = Card()
    _apply_detection(card, DetectedCard(
        player="A", subset="League Leaders", team="Cubs", rookie=True, bbox=[0, 0, 1, 1],
    ))
    assert (card.subset, card.team, card.rookie) == ("League Leaders", "Cubs", True)


def test_card_out_exposes_new_fields():
    fields = CardOut.model_fields
    assert {"subset", "team", "rookie"} <= set(fields)
    assert {"subset", "team", "rookie"} <= set(CardUpdateRequest.model_fields)


def test_back_fills_missing_team_and_subset():
    front = Card(side="front", player="A", team=None, subset=None)
    back = Card(side="back", player="A", team="Cubs", subset="Record Breaker")
    assert enrich_front_from_back(front, back) is True
    assert front.team == "Cubs" and front.subset == "Record Breaker"


def test_prompt_covers_price_drivers():
    p = DETECTION_SYSTEM
    for phrase in ("subset", "team", "rookie", "Topps Chrome", "Bowman Chrome",
                   "Stadium Club", "Finest", "Refractor", "serial"):
        assert phrase.lower() in p.lower(), phrase
    # parallel is reserved for finish/numbering variants
    assert "League Leaders" in p


def test_verification_prompt_is_sport_neutral():
    assert "baseball card" not in VERIFICATION_SYSTEM.lower()
    # unknown is not disagreement
    assert "null" in VERIFICATION_SYSTEM


def test_migration_adds_new_card_columns(monkeypatch, tmp_path):
    import app.db as db

    url = f"sqlite:///{tmp_path / 'old.db'}"
    eng = create_engine(url, connect_args={"check_same_thread": False})
    with eng.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE cards (id INTEGER PRIMARY KEY)")
    monkeypatch.setattr(db, "engine", eng)
    monkeypatch.setattr(db._settings, "database_url", url)
    db._ensure_columns()
    with eng.begin() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(cards)")}
    assert {"subset", "team", "rookie"} <= cols
