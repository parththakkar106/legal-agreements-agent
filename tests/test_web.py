from pathlib import Path

from fastapi.testclient import TestClient
from fakes import result, review_answer

from legal_agent import store
from legal_agent.agents import classifier, grouper, reviewer
from legal_agent.docx_parser import parse_docx
from legal_agent.llm import LLMUnavailable
from legal_agent.web import app as web


class RunNow:
    """Stands in for Thread so the background review finishes before the redirect."""

    def __init__(self, target, args, daemon):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


def fake_ai(monkeypatch):
    def fake_review(system, prompt, schema):
        if 'clause="Payment Period"' in prompt:
            return result(review_answer(decision="push_back", rule_ids=["payment-terms"],
                                        counter_proposal="We can offer 45 days."))
        return result(review_answer(rule_ids=["liability-cap"]))

    monkeypatch.setattr(classifier, "ask_claude", lambda *a: result({"doc_type": "msa", "changes": []}))
    monkeypatch.setattr(reviewer, "ask_claude", fake_review)
    monkeypatch.setattr(grouper, "ask_claude", lambda *a: result({"groups": []}))


def client(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DB_FILE", tmp_path / "desk.db")
    monkeypatch.setattr(web, "Thread", RunNow)
    return TestClient(web.app)


def test_full_negotiation_counter_then_accept_then_sign(monkeypatch, tmp_path):
    fake_ai(monkeypatch)
    http = client(monkeypatch, tmp_path)

    page = http.post("/agreements", data={"demo": "msa"})
    assert "We can offer 45 days." in page.text and "Close round" in page.text
    agreement_id = int(page.url.path.rsplit("/", 1)[1])
    data = http.get(f"/api/agreements/{agreement_id}").json()
    round_id = int(next(iter(data["rounds"])))
    payment = next(c["id"] for c in data["rounds"][str(round_id)]["changes"] if c["clause_id"] == "Payment Period")

    # The yellow change has no answer yet, so the round cannot close.
    assert http.post(f"/rounds/{round_id}/close", data={"actor": "Ana"}).status_code == 400

    http.post(f"/rounds/{round_id}/items/{payment}", data={
        "actor": "Ana", "outcome": "counter", "counter_text": "45 days from the invoice date.",
        "external_comment": "45 days is the most we can do."})
    http.post(f"/rounds/{round_id}/close", data={"actor": "Ana"})
    data = http.get(f"/api/agreements/{agreement_id}").json()
    assert data["status"] == "waiting for counterparty"
    v3 = data["versions"]["3"]
    assert v3["author"] == store.ACME and "45 days is the most we can do." in v3["note"]
    terms = {c.id: c.text for c in parse_docx(v3["file"])}
    assert terms["Payment Period"] == "45 days from the invoice date."
    assert "24 months" in terms["General Cap Amount"]  # their accepted change stays

    # Globex sends v3 back unchanged: nothing to review, ready to sign.
    http.post(f"/agreements/{agreement_id}/simulate-reply")
    assert http.get(f"/api/agreements/{agreement_id}").json()["status"] == "ready to sign"

    assert http.post(f"/agreements/{agreement_id}/sign", data={"name": "Ana Ruiz"}).status_code == 400  # no consent
    signed = http.post(f"/agreements/{agreement_id}/sign", data={"name": "Ana Ruiz", "consent": "yes"})
    assert "Signed by Ana Ruiz" in signed.text
    data = http.get(f"/api/agreements/{agreement_id}").json()
    assert data["status"] == "signed" and Path(data["signed_pdf"]).read_bytes()[:4] == b"%PDF"
    assert [e["action"] for e in data["events"]] == [
        "sent", "received", "reviewed", "round closed", "sent", "received", "round closed", "sign"]
    assert "Clause history" in signed.text


def test_versions_diff_and_downloads(monkeypatch, tmp_path):
    fake_ai(monkeypatch)
    http = client(monkeypatch, tmp_path)
    page = http.post("/agreements", data={"demo": "sow"})
    agreement_id = page.url.path.rsplit("/", 1)[1]
    round_id = next(iter(http.get(f"/api/agreements/{agreement_id}").json()["rounds"]))

    assert "not subject to Section 8" in http.get(f"/agreements/{agreement_id}/versions/2").text
    download = http.get(f"/agreements/{agreement_id}/versions/2/download")
    assert download.content == (web.CONTRACTS / "redlines" / "sow_globex_v2.docx").read_bytes()
    assert "Globex_Onboarding-SOW_v2_redline.docx" in download.headers["content-disposition"]
    diff = http.get(f"/agreements/{agreement_id}/rounds/{round_id}/diff").text
    assert "unchanged clause" in diff and "USD 40,000" in diff
    assert http.get("/demos/sow/v2").status_code == 200 and http.get("/demos/sow/v3").status_code == 404


def test_sign_refused_if_document_changed(monkeypatch, tmp_path):
    http = client(monkeypatch, tmp_path)
    with store.connect() as db:
        agreement_id = store.create_agreement(db, "NDA", "Globex")
        version = store.add_version(db, agreement_id, "Globex", web.CONTRACTS / "base" / "nda.docx", "nda.docx")
        store.set_status(db, agreement_id, "ready to sign")
    Path(version["file"]).write_bytes(Path(version["file"]).read_bytes() + b"tampered")

    response = http.post(f"/agreements/{agreement_id}/sign", data={"name": "A", "consent": "yes"})
    assert response.status_code == 409


def test_missing_claude_is_shown_in_the_trail(monkeypatch, tmp_path):
    def unavailable(*args):
        raise LLMUnavailable("The `claude` CLI was not found.")
    monkeypatch.setattr(classifier, "ask_claude", unavailable)
    http = client(monkeypatch, tmp_path)

    page = http.post("/agreements", data={"demo": "nda"})
    assert "AI review stopped" in page.text and "was not found" in page.text
    assert "Non-Solicitation" in page.text  # the plain diff is still shown
    assert http.get(page.url.path.replace("/agreements", "/api/agreements")).json()["status"] == "failed"
