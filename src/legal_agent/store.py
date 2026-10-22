"""SQLite storage for negotiations.

  agreements  one per contract being negotiated with a counterparty
  versions    every version exchanged (v1, v2, ...), with who sent it and its SHA-256
  rounds      one per version received: the changes against the version we sent, and the AI review
  items       a person's answer to one change: outcome, counter text, comments
  events      the audit trail: everything that happened, in order
"""

import hashlib
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

APP_DATA = Path(__file__).resolve().parents[2] / "data" / "app"
DB_FILE = APP_DATA / "desk.db"
ACME = "Acme Cloud Inc."

SCHEMA = """
CREATE TABLE IF NOT EXISTS agreements (
    id INTEGER PRIMARY KEY, title TEXT, counterparty TEXT, doc_type TEXT,
    status TEXT,  -- reviewing | needs review | waiting for counterparty | ready to sign | signed | failed
    related_file TEXT, signed_pdf TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY, agreement_id INTEGER, number INTEGER, author TEXT,
    file TEXT, name TEXT, sha256 TEXT, note TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS rounds (
    id INTEGER PRIMARY KEY, agreement_id INTEGER, old_version INTEGER, new_version INTEGER,
    status TEXT,  -- reviewing | open | closed | failed
    stage TEXT, changes_json TEXT, result_json TEXT
);
CREATE TABLE IF NOT EXISTS items (
    round_id INTEGER, change_id INTEGER, outcome TEXT, counter_text TEXT,
    internal_comment TEXT, external_comment TEXT, actor TEXT, at TEXT,
    PRIMARY KEY (round_id, change_id)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, agreement_id INTEGER, at TEXT, action TEXT, actor TEXT, detail TEXT,
    version INTEGER, round_id INTEGER
);
"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def sha256_of(path: str | Path) -> str:
    """Fingerprint of an exact file: any later edit changes it."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def connect(db_file: Path | None = None) -> sqlite3.Connection:
    db_file = db_file or DB_FILE
    db_file.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_file)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def files_dir(agreement_id: int) -> Path:
    """Where an agreement's versions and signed PDF are kept (next to the database)."""
    return (DB_FILE.parent / "files" / str(agreement_id))


def create_agreement(db, title: str, counterparty: str, related_file: str | Path | None = None) -> int:
    cursor = db.execute("INSERT INTO agreements (title, counterparty, status, related_file, created_at)"
                        " VALUES (?, ?, 'needs review', ?, ?)",
                        (title, counterparty, str(related_file) if related_file else None, now()))
    db.commit()
    return cursor.lastrowid


def add_version(db, agreement_id: int, author: str, source: str | Path, name: str, note: str = "") -> dict:
    """Keep a copy of the file as the next version number and log who sent it."""
    number = db.execute("SELECT COALESCE(MAX(number), 0) + 1 FROM versions WHERE agreement_id = ?",
                        (agreement_id,)).fetchone()[0]
    path = files_dir(agreement_id) / f"v{number}.docx"
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, path)
    db.execute("INSERT INTO versions (agreement_id, number, author, file, name, sha256, note, created_at)"
               " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
               (agreement_id, number, author, str(path), name, sha256_of(path), note, now()))
    add_event(db, agreement_id, "sent" if author == ACME else "received", author, name, version=number)
    return get_version(db, agreement_id, number)


def get_version(db, agreement_id: int, number: int) -> dict | None:
    row = db.execute("SELECT * FROM versions WHERE agreement_id = ? AND number = ?", (agreement_id, number)).fetchone()
    return dict(row) if row else None


def start_round(db, agreement_id: int, old_version: int, new_version: int, changes: list[dict]) -> int:
    cursor = db.execute("INSERT INTO rounds (agreement_id, old_version, new_version, status, stage, changes_json)"
                        " VALUES (?, ?, ?, 'reviewing', 'Starting', ?)",
                        (agreement_id, old_version, new_version, json.dumps(changes)))
    set_status(db, agreement_id, "reviewing")
    return cursor.lastrowid


def set_stage(db, round_id: int, stage: str):
    db.execute("UPDATE rounds SET stage = ? WHERE id = ?", (stage, round_id))
    db.commit()


def finish_round(db, round_id: int, result: dict, by_ai: bool = True):
    """Store the review result; the round now waits for a person."""
    agreement_id = _agreement_of(db, round_id)
    db.execute("UPDATE rounds SET status = 'open', stage = NULL, result_json = ? WHERE id = ?",
               (json.dumps(result), round_id))
    db.execute("UPDATE agreements SET doc_type = COALESCE(doc_type, ?) WHERE id = ?", (result["doc_type"], agreement_id))
    set_status(db, agreement_id, "needs review")
    if by_ai:
        add_event(db, agreement_id, "reviewed", "AI agent", result["verdict"], round_id=round_id)


def fail_round(db, round_id: int, error: str):
    agreement_id = _agreement_of(db, round_id)
    db.execute("UPDATE rounds SET status = 'failed', stage = NULL WHERE id = ?", (round_id,))
    set_status(db, agreement_id, "failed")
    add_event(db, agreement_id, "review failed", "AI agent", error, round_id=round_id)


def save_item(db, round_id: int, change_id: int, fields: dict, actor: str):
    db.execute("INSERT OR REPLACE INTO items (round_id, change_id, outcome, counter_text, internal_comment,"
               " external_comment, actor, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
               (round_id, change_id, fields.get("outcome"), fields.get("counter_text"),
                fields.get("internal_comment"), fields.get("external_comment"), actor, now()))
    db.commit()


def close_round(db, round_id: int, actor: str, summary: str):
    db.execute("UPDATE rounds SET status = 'closed' WHERE id = ?", (round_id,))
    add_event(db, _agreement_of(db, round_id), "round closed", actor, summary, round_id=round_id)


def set_status(db, agreement_id: int, status: str):
    db.execute("UPDATE agreements SET status = ? WHERE id = ?", (status, agreement_id))
    db.commit()


def set_signed(db, agreement_id: int, pdf: Path):
    db.execute("UPDATE agreements SET status = 'signed', signed_pdf = ? WHERE id = ?", (str(pdf), agreement_id))
    db.commit()


def add_event(db, agreement_id: int, action: str, actor: str, detail: str = "",
              version: int | None = None, round_id: int | None = None):
    db.execute("INSERT INTO events (agreement_id, at, action, actor, detail, version, round_id)"
               " VALUES (?, ?, ?, ?, ?, ?, ?)", (agreement_id, now(), action, actor, detail, version, round_id))
    db.commit()


def get_agreement(db, agreement_id: int) -> dict | None:
    row = db.execute("SELECT * FROM agreements WHERE id = ?", (agreement_id,)).fetchone()
    if row is None:
        return None
    agreement = dict(row)
    agreement["versions"] = {v["number"]: dict(v) for v in db.execute(
        "SELECT * FROM versions WHERE agreement_id = ? ORDER BY number", (agreement_id,))}
    agreement["rounds"] = {}
    for r in db.execute("SELECT * FROM rounds WHERE agreement_id = ? ORDER BY id", (agreement_id,)):
        r = dict(r)
        r["changes"] = json.loads(r.pop("changes_json"))
        r["result"] = json.loads(r.pop("result_json") or "null")
        r["answers"] = {i["change_id"]: dict(i) for i in db.execute("SELECT * FROM items WHERE round_id = ?", (r["id"],))}
        agreement["rounds"][r["id"]] = r
    agreement["events"] = [dict(e) for e in db.execute(
        "SELECT * FROM events WHERE agreement_id = ? ORDER BY id", (agreement_id,))]
    return agreement


def list_agreements(db) -> list[dict]:
    return [dict(r) for r in db.execute(
        "SELECT a.id, title, counterparty, doc_type, status,"
        " (SELECT MAX(number) FROM versions WHERE agreement_id = a.id) AS latest_version,"
        " (SELECT at FROM events WHERE agreement_id = a.id ORDER BY id DESC LIMIT 1) AS updated_at"
        " FROM agreements a ORDER BY a.id DESC")]


def _agreement_of(db, round_id: int) -> int:
    return db.execute("SELECT agreement_id FROM rounds WHERE id = ?", (round_id,)).fetchone()[0]
