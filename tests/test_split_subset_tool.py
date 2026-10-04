"""tools/split_subset_from_parallel: move subset names out of `parallel`."""
from app.models import Card, ImageUpload
from tools.split_subset_from_parallel import run, split_parallel


def test_subset_only_moves_entirely():
    assert split_parallel("League Leaders") == (None, "League Leaders")
    assert split_parallel("record breaker") == (None, "Record Breaker")


def test_finish_stays_in_parallel():
    assert split_parallel("Gold /99") == ("Gold /99", None)
    assert split_parallel("Refractor") == ("Refractor", None)
    assert split_parallel(None) == (None, None)


def test_mixed_value_is_split():
    assert split_parallel("All-Star Refractor") == ("Refractor", "All-Star")
    assert split_parallel("Magic Moments, Gold") == ("Gold", "Magic Moments")


def test_word_boundaries_are_respected():
    # "Highlights" must not match inside an unrelated word.
    assert split_parallel("Highlightsy Foil") == ("Highlightsy Foil", None)


def _card(db, **kw):
    up = ImageUpload(filename="x.jpg")
    db.add(up)
    db.flush()
    c = Card(upload_id=up.id, side="front", status="priced", **kw)
    db.add(c)
    db.flush()
    return c


def test_dry_run_changes_nothing_and_apply_writes(db_session):
    c = _card(db_session, player="A", parallel="League Leaders")
    keep = _card(db_session, player="B", parallel="Gold /99")
    has_subset = _card(db_session, player="C", parallel="Highlights", subset="Other")

    plan = run(db_session, apply=False)
    assert [p["card_id"] for p in plan] == [c.id]
    assert c.parallel == "League Leaders" and c.subset is None

    run(db_session, apply=True)
    assert c.parallel is None and c.subset == "League Leaders"
    assert keep.parallel == "Gold /99"
    # an existing subset is never overwritten
    assert has_subset.subset == "Other" and has_subset.parallel == "Highlights"
