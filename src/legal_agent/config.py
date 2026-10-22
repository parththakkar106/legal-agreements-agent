"""Settings, all from environment variables, so the same code runs on a laptop and in a container.

  DESK_DATA_DIR        where the database, versions and signed PDFs live (default: data/app)
  DESK_API_KEYS        keys for the JSON API, comma-separated, each "name:key" or just "key";
                       the name shows in the audit trail. Unset = the API is off.
  DESK_WEBHOOK_URLS    where events are POSTed, comma-separated. Unset = no webhooks.
  DESK_WEBHOOK_SECRET  signs every webhook (HMAC-SHA256). Required when URLs are set.
  DESK_PUBLIC_URL      this app's address, used for links inside webhook events (optional)
  DESK_MAX_UPLOAD_MB   largest .docx accepted (default 10)
  CLAUDE_KEY           token for `claude -p` (see llm.py)

Secrets are read when used and never written to disk or logs.
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("DESK_DATA_DIR") or ROOT / "data" / "app")


def _list(name: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


def api_keys() -> dict[str, str]:
    """{key: name}; a key without a name is called "api"."""
    keys = {}
    for item in _list("DESK_API_KEYS"):
        name, _, key = item.rpartition(":")
        keys[key] = name or "api"
    return keys


def webhook_urls() -> list[str]:
    return _list("DESK_WEBHOOK_URLS")


def webhook_secret() -> str:
    return os.environ.get("DESK_WEBHOOK_SECRET", "")


def public_url() -> str:
    return os.environ.get("DESK_PUBLIC_URL", "").rstrip("/")


def max_upload_bytes() -> int:
    return int(float(os.environ.get("DESK_MAX_UPLOAD_MB", "10")) * 1024 * 1024)
