"""Record one live AI review for the public demo to replay: Acme's DPA against Globex's redline.

The demo (DESK_DEMO=1) has no model key. It replays this saved review, with the
same progress steps, when a visitor clicks Run AI review on the Globex DPA.
Runs `claude -p` about 5 times.

    python scripts/build_cached_review.py
"""

import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from legal_agent import store  # noqa: E402
from legal_agent.demo import CACHED_REVIEW  # noqa: E402
from legal_agent.orchestrator import review_contract  # noqa: E402

old = ROOT / "data" / "contracts" / "base" / "dpa.docx"
new = ROOT / "data" / "contracts" / "redlines" / "dpa_globex_v2.docx"
stages = []
result = review_contract(old, new, on_progress=stages.append)
CACHED_REVIEW.write_text(json.dumps({
    "old_sha256": store.sha256_of(old), "new_sha256": store.sha256_of(new),
    "stages": stages, "result": asdict(result)}, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"Saved {len(result.changes)} reviewed changes, verdict {result.verdict!r}, to {CACHED_REVIEW}")
