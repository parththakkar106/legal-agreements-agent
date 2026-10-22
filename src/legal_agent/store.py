"""SQLite storage: one row per agreement under review, plus its audit trail.

The audit trail (events) is the record of everything that happened to an
agreement: which versions were exchanged, what the AI found, and what a person decided.
"""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_FILE = Path(__file__).resolve().parents[2] / "data" / "app" / "desk.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY,
    title TEXT, counterparty TEXT, doc_type TEXT, verdict TEXT,
    status TEXT,  -- reviewing | awaiting decision | signed | sent back | escalated | failed
    stage TEXT,  -- what the AI is doing right now, while status is "reviewing"
    old_file TEXT, old_name TEXT, new_file TEXT, new_name TEXT, related_file TEXT,
    new_sha256 TEXT, created_at TEXT, result_json TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    review_id INTEGER, at TEXT, action TEXT, actor TEXT, detail TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def sha256_of(path: str | Path) -> str:
    """Fingerprint of the exact file reviewed and signed: any later edit changes it."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def connect(db_file: Path | None = None) -> sqlite3.Connection:
    db_file = db_file or DB_FILE
    db_file.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_file)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def create_review(db, title: str, counterparty: str, old_file: str | Path, old_name: str,
                  new_file: str | Path, new_name: str, related_file: str | Path | None = None) -> int:
    """Record both versions and mark the agreement as being reviewed."""
    cursor = db.execute(
        "INSERT INTO reviews (title, counterparty, status, stage, old_file, old_name, new_file, new_name,"
        " related_file, new_sha256, created_at) VALUES (?, ?, 'reviewing', 'Starting', ?, ?, ?, ?, ?, ?, ?)",
        (title, counterparty, str(old_file), old_name, str(new_file), new_name,
         str(related_file) if related_file else None, sha256_of(new_file), now()),
    )
    review_id = cursor.lastrowid
    add_event(db, review_id, "sent", "Acme Cloud Inc.", old_name)
    add_event(db, review_id, "received", counterparty, new_name)
    return review_id


def set_stage(db, review_id: int, stage: str):
    db.execute("UPDATE reviews SET stage = ? WHERE id = ?", (stage, review_id))
    db.commit()


def finish_review(db, review_id: int, result: dict):
    db.execute("UPDATE reviews SET status = 'awaiting decision', stage = NULL, doc_type = ?, verdict = ?,"
               " result_json = ? WHERE id = ?", (result["doc_type"], result["verdict"], json.dumps(result), review_id))
    add_event(db, review_id, "reviewed", "AI agent", result["verdict"])


def fail_review(db, review_id: int, error: str):
    db.execute("UPDATE reviews SET status = 'failed', stage = NULL WHERE id = ?", (review_id,))
    add_event(db, review_id, "review failed", "AI agent", error)


def add_event(db, review_id: int, action: str, actor: str, detail: str = ""):
    db.execute("INSERT INTO events (review_id, at, action, actor, detail) VALUES (?, ?, ?, ?, ?)",
               (review_id, now(), action, actor, detail))
    db.commit()


def set_status(db, review_id: int, status: str):
    db.execute("UPDATE reviews SET status = ? WHERE id = ?", (status, review_id))
    db.commit()


def get_review(db, review_id: int) -> dict | None:
    row = db.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
    if row is None:
        return None
    review = dict(row)
    review["result"] = json.loads(review.pop("result_json") or "null")
    review["events"] = [dict(e) for e in db.execute(
        "SELECT * FROM events WHERE review_id = ? ORDER BY id", (review_id,))]
    return review


def list_reviews(db) -> list[dict]:
    return [dict(r) for r in db.execute(
        "SELECT r.id, title, counterparty, doc_type, verdict, status, created_at,"
        " (SELECT at FROM events WHERE review_id = r.id ORDER BY id DESC LIMIT 1) AS updated_at"
        " FROM reviews r ORDER BY r.id DESC")]
