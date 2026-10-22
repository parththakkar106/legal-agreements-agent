"""Build data/portfolio/: the agreements the web app starts with.

Plays each negotiation through the app with live AI reviews (`claude -p`, about
10 reviews, a few minutes, around a dollar), in a scratch database, then saves it.

    python scripts/build_portfolio.py

Afterwards delete data/app/ so the next `serve` loads the new portfolio.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from legal_agent import store  # noqa: E402
from legal_agent.web import portfolio  # noqa: E402
from legal_agent.web.app import app  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    store.DB_FILE = Path(tmp) / "desk.db"
    portfolio.build(TestClient(app))
    portfolio.save(store.DB_FILE)
print(f"Saved to {portfolio.PORTFOLIO}")
