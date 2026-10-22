"""Example negotiations, played through the real app so every page has something to show.

Each example goes through the same routes a person uses: upload, live AI review,
answers, close the round, the counterparty's reply, signing. Only the human answers
and the counterparty's replies are scripted. The AI reviews are real, so loading
the examples needs `claude -p`, takes a few minutes and costs a few cents.

  1. Cloud Service Agreement with Globex: four versions, two AI-reviewed rounds, signed
  2. Mutual NDA with Initech: every change accepted in the first round, signed
  3. Data Processing Agreement with Umbrella: our counter-proposal accepted as is, signed
"""

import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient

from legal_agent.docx_parser import parse_docx
from legal_agent.docx_writer import write_docx

CONTRACTS = Path(__file__).resolve().parents[3] / "data" / "contracts"
SIGNER = "Ana Ruiz"
running = False  # the home page shows a progress note while this is True


def load_examples():
    """Runs in a background thread started from the home page."""
    global running
    running = True
    try:
        from legal_agent.web.app import app  # here, not at the top: app imports this module
        http = TestClient(app)
        _msa_with_globex(http)
        _nda_with_initech(http)
        _dpa_with_umbrella(http)
    finally:
        running = False


def _msa_with_globex(http):
    agreement = _start(http, "Cloud Service Agreement", "Globex Inc.", "msa", "Globex")
    _answer(http, agreement, {
        "Payment Period": ("counter", "45 days from the invoice date.", "Finance agreed 45 days as our limit.",
                           "We can move from 30 to 45 days, which is the longest we offer."),
        "General Cap Amount": ("reject", "", "A 24-month cap doubles our exposure.",
                               "We keep the cap at 12 months of fees, our standard for all customers."),
        "5.5.b": ("reject", "", "The deletion duty protects us under GDPR Art. 28(3)(g).",
                  "We need to keep the deletion clause; it is required for GDPR compliance."),
    })
    _close(http, agreement)
    # Globex accepts 45 days and meets us halfway on the cap: 18 months. That is their v4.
    v3 = _api(http, agreement)["versions"]["3"]
    clauses = parse_docx(v3["file"])
    for clause in clauses:
        if clause.id == "General Cap Amount":
            clause.text = clause.text.replace("12 months", "18 months")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "v4.docx"
        write_docx(clauses, path, "Cloud Service Agreement")
        with path.open("rb") as f:
            http.post(f"/agreements/{agreement}/versions",
                      files={"new": ("Globex_Cloud-Service-Agreement_v4.docx", f)})
    _wait(http, agreement)
    _answer(http, agreement, {"General Cap Amount": (
        "accept", "", "Deal owner approved 18 months for this deal size; legal agreed.", "We accept 18 months.")})
    _close(http, agreement)
    _sign(http, agreement)


def _nda_with_initech(http):
    agreement = _start(http, "Mutual NDA", "Initech Ltd.", "nda", "Initech")
    _answer(http, agreement, {}, default=("accept", "", "Within our playbook: mutual, limited to the deal.", ""))
    _close(http, agreement)
    _sign(http, agreement)


def _dpa_with_umbrella(http):
    agreement = _start(http, "Data Processing Agreement", "Umbrella Corp.", "dpa", "Umbrella")
    _answer(http, agreement, {
        "Subprocessor Notice Period": ("reject", "", "60 days would block vendor changes.",
                                       "We keep 30 days' notice with your right to object."),
        "3.1.a": ("reject", "", "Our hosting is in the US and the EU; EU-only needs a product change.",
                  "We cannot commit to EU-only processing; transfers are covered by the SCCs."),
        "4.1": ("counter", None, "48 hours is the shortest we can operate.",
                "We can commit to 48 hours, with details provided in phases."),
    })
    _close(http, agreement)
    http.post(f"/agreements/{agreement}/simulate-reply")
    _sign(http, agreement)


def _start(http, title: str, counterparty: str, demo: str, prefix: str) -> int:
    from legal_agent.web.app import DEMOS
    spec, short = DEMOS[demo], DEMOS[demo]["v1"][1].split("_")[1]
    with (CONTRACTS / spec["v1"][0]).open("rb") as v1, (CONTRACTS / spec["v2"][0]).open("rb") as v2:
        page = http.post("/agreements", data={"title": title, "counterparty": counterparty}, files={
            "old": (spec["v1"][1], v1), "new": (f"{prefix}_{short}_v2_redline.docx", v2)})
    agreement = int(page.url.path.rsplit("/", 1)[1])
    _wait(http, agreement)
    return agreement


def _answer(http, agreement: int, answers: dict, default=("accept", "", "Fine as is.", "")):
    """Answer every change in the open round: by clause id, or with `default`."""
    data = _api(http, agreement)
    rnd = [r for r in data["rounds"].values() if r["status"] == "open"][-1]
    for change in rnd["changes"]:
        outcome, counter, internal, external = answers.get(change["clause_id"], default)
        if outcome == "counter" and counter is None:  # counter on the timing only
            counter = change["new_text"].replace("24 hours", "48 hours")
        http.post(f"/rounds/{rnd['id']}/items/{change['id']}", data={
            "actor": SIGNER, "outcome": outcome, "counter_text": counter,
            "internal_comment": internal, "external_comment": external})


def _close(http, agreement: int):
    rnd = [r for r in _api(http, agreement)["rounds"].values() if r["status"] == "open"][-1]
    http.post(f"/rounds/{rnd['id']}/close", data={"actor": SIGNER})


def _sign(http, agreement: int):
    http.post(f"/agreements/{agreement}/sign", data={"name": SIGNER, "consent": "yes"})


def _wait(http, agreement: int):
    """The AI review runs in the app's background thread; wait for it to finish."""
    while _api(http, agreement)["status"] == "reviewing":
        time.sleep(2)
    if _api(http, agreement)["status"] == "failed":
        raise RuntimeError("The AI review failed; see the agreement's audit trail.")


def _api(http, agreement: int) -> dict:
    return http.get(f"/api/agreements/{agreement}").json()
