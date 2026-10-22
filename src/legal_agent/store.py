"""SQLite storage: one row per review, plus an audit trail of every human action."""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_FILE = Path(__file__).resolve().parents[2] / "data" / "app" / "desk.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY,
    title TEXT, doc_type TEXT, verdict TEXT, status TEXT,
    new_file TEXT, new_sha256 TEXT, created_at TEXT, result_json TEXT
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


def save_review(db, title: str, new_file: str | Path, result: dict) -> int:
    cursor = db.execute(
        "INSERT INTO reviews (title, doc_type, verdict, status, new_file, new_sha256, created_at, result_json)"
        " VALUES (?, ?, ?, 'awaiting decision', ?, ?, ?, ?)",
        (title, result["doc_type"], result["verdict"], str(new_file), sha256_of(new_file), now(), json.dumps(result)),
    )
    add_event(db, cursor.lastrowid, "reviewed", "agent", f"Verdict: {result['verdict']}")
    return cursor.lastrowid


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
    review["result"] = json.loads(review.pop("result_json"))
    review["events"] = [dict(e) for e in db.execute(
        "SELECT * FROM events WHERE review_id = ? ORDER BY id", (review_id,))]
    return review


def list_reviews(db) -> list[dict]:
    return [dict(r) for r in db.execute(
        "SELECT id, title, doc_type, verdict, status, created_at FROM reviews ORDER BY id DESC")]
