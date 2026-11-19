"""The public demo (DESK_DEMO=1): a private copy of the portfolio per visitor, and one saved AI review.

Each browser gets a cookie and its own database, filled from data/portfolio/ on its first
visit, so every new visitor starts from the same contracts with no signature adopted.
The portfolio's version files are read in place; only files the visitor creates (new
versions, signed PDFs) are written to the visitor's folder.

The demo has no model key. Run AI review replays one saved review (the Globex DPA) and
refuses every other contract with a message saying so.
"""

import json
import re
import secrets
import shutil
import time
from pathlib import Path

from legal_agent import config, store

ROOT = Path(__file__).resolve().parents[2]
CACHED_REVIEW = ROOT / "data" / "portfolio" / "cached_review.json"
VISITORS = config.DATA_DIR / "visitors"
COOKIE = "desk_visitor"
KEEP_DAYS = 7
STEP_SECONDS = 2  # pause between replayed progress steps, so the page shows each one


def visitor_db(cookie: str) -> tuple[Path, str]:
    """The database for this cookie, created from the portfolio if new. Returns it and the cookie to set."""
    from legal_agent.web import portfolio

    if not re.fullmatch(r"[0-9a-f]{32}", cookie):
        cookie = secrets.token_hex(16)
    db_file = VISITORS / cookie / "desk.db"
    if not db_file.exists():
        _delete_old()
        token = store.use_db(db_file)
        try:
            portfolio.load_if_empty(copy_files=False)
        finally:
            store.reset_db(token)
    return db_file, cookie


def _delete_old():
    # shortcut: age-based cleanup on each new visitor. Add a cap on folder count if bots fill the disk.
    cutoff = time.time() - KEEP_DAYS * 86400
    for folder in VISITORS.glob("*"):
        if folder.stat().st_mtime < cutoff:
            shutil.rmtree(folder, ignore_errors=True)


def replay_review(old_path, new_path, on_progress) -> dict:
    """The saved review, if these are the two files it was recorded on."""
    saved = json.loads(CACHED_REVIEW.read_text(encoding="utf-8"))
    if (store.sha256_of(old_path), store.sha256_of(new_path)) != (saved["old_sha256"], saved["new_sha256"]):
        raise RuntimeError("This public demo has no AI key. It replays one saved AI review: the Data Processing "
                           "Agreement from Globex. Run the app yourself with CLAUDE_KEY to review other contracts.")
    for stage in saved["stages"]:
        on_progress(stage)
        time.sleep(STEP_SECONDS)
    return saved["result"]
