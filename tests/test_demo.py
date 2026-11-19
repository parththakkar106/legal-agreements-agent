"""The public demo: one copy of the portfolio per visitor, and the saved review replayed only for the Globex DPA."""

import time

from fastapi.testclient import TestClient

from legal_agent import demo, store
from legal_agent.web.app import app


def _status(http, title, counterparty):
    page = http.get("/").text
    return page, title in page and counterparty in page


def _round_of_globex_dpa(db_file):
    with store.connect(db_file) as db:
        return db.execute("SELECT r.id FROM rounds r JOIN agreements a ON a.id = r.agreement_id "
                          "WHERE a.counterparty = 'Globex Inc.' AND r.status = 'not reviewed'").fetchone()[0]


def test_each_visitor_gets_a_private_copy_and_the_saved_review(monkeypatch, tmp_path):
    monkeypatch.setenv("DESK_DEMO", "1")
    monkeypatch.setattr(store, "DB_FILE", tmp_path / "main" / "desk.db")
    monkeypatch.setattr(demo, "VISITORS", tmp_path / "visitors")
    monkeypatch.setattr(demo, "STEP_SECONDS", 0)
    alice, bob = TestClient(app), TestClient(app)
    alice.get("/")
    bob.get("/")
    alice_db = demo.VISITORS / alice.cookies[demo.COOKIE] / "desk.db"
    bob_db = demo.VISITORS / bob.cookies[demo.COOKIE] / "desk.db"
    assert alice_db != bob_db

    round_id = _round_of_globex_dpa(alice_db)
    alice.post(f"/rounds/{round_id}/review")
    for _ in range(50):
        with store.connect(alice_db) as db:
            status = db.execute("SELECT status FROM rounds WHERE id = ?", (round_id,)).fetchone()[0]
        if status != "reviewing":
            break
        time.sleep(0.1)
    assert status == "open"  # replayed, and waiting for a person's answers
    with store.connect(bob_db) as db:  # Bob's copy is untouched
        assert db.execute("SELECT status FROM rounds WHERE id = ?", (round_id,)).fetchone()[0] == "not reviewed"
    assert not store.DB_FILE.exists() or not store.connect().execute("SELECT 1 FROM agreements").fetchone()


def test_replay_refuses_other_contracts(tmp_path):
    other = demo.ROOT / "data" / "contracts" / "base" / "nda.docx"
    redline = demo.ROOT / "data" / "contracts" / "redlines" / "nda_globex_v2.docx"
    try:
        demo.replay_review(other, redline, lambda step: None)
    except RuntimeError as error:
        assert "Globex" in str(error)
    else:
        raise AssertionError("replayed a review for the wrong contract")
