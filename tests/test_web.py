from fastapi.testclient import TestClient
from fakes import result, review_answer

from legal_agent import store
from legal_agent.agents import classifier, grouper, reviewer
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
                                        gdpr_citations=[], counter_proposal="We can offer 45 days."))
        return result(review_answer(rule_ids=["liability-cap"]))

    monkeypatch.setattr(classifier, "ask_claude", lambda *a: result({"doc_type": "msa", "changes": []}))
    monkeypatch.setattr(reviewer, "ask_claude", fake_review)
    monkeypatch.setattr(grouper, "ask_claude", lambda *a: result({"groups": []}))


def client(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DB_FILE", tmp_path / "desk.db")
    monkeypatch.setattr(web, "Thread", RunNow)
    return TestClient(web.app)


def test_demo_review_then_sign(monkeypatch, tmp_path):
    fake_ai(monkeypatch)
    http = client(monkeypatch, tmp_path)

    page = http.post("/reviews", data={"demo": "msa"})
    assert page.status_code == 200 and "Send back to Globex Inc." in page.text and "We can offer 45 days." in page.text
    assert 'data-source="rule:payment-terms:push_back"' in page.text  # the source opens in the side panel
    review_id = page.url.path.rsplit("/", 1)[1]

    refused = http.post(f"/reviews/{review_id}/decision", data={"action": "sign", "name": "Ana Ruiz"})
    assert refused.status_code == 400  # no consent box

    signed = http.post(f"/reviews/{review_id}/decision", data={"action": "sign", "name": "Ana Ruiz", "consent": "yes"})
    assert signed.status_code == 200 and "Signed by Ana Ruiz" in signed.text
    data = http.get(f"/api/reviews/{review_id}").json()
    assert data["status"] == "signed"
    assert [e["action"] for e in data["events"]] == ["sent", "received", "reviewed", "sign"]
    assert data["new_sha256"] in data["events"][-1]["detail"]

    again = http.post(f"/reviews/{review_id}/decision", data={"action": "escalate"})
    assert again.status_code == 400  # already decided


def test_versions_can_be_read_and_downloaded(monkeypatch, tmp_path):
    http = client(monkeypatch, tmp_path)

    page = http.get("/demos/sow/v2")
    assert page.status_code == 200 and "Globex_Onboarding-SOW_v2_redline.docx" in page.text
    assert "not subject to Section 8" in page.text
    download = http.get("/demos/sow/v2/download")
    assert download.content == (web.CONTRACTS / "redlines" / "sow_globex_v2.docx").read_bytes()
    assert "Globex_Onboarding-SOW_v2_redline.docx" in download.headers["content-disposition"]
    assert http.get("/demos/sow/v3").status_code == 404


def test_sign_refused_if_document_changed(monkeypatch, tmp_path):
    http = client(monkeypatch, tmp_path)
    old, new = tmp_path / "v1.docx", tmp_path / "v2.docx"
    old.write_bytes((web.CONTRACTS / "base" / "msa.docx").read_bytes())
    new.write_bytes((web.CONTRACTS / "redlines" / "msa_globex_v2.docx").read_bytes())
    with store.connect() as db:
        review_id = store.create_review(db, "t", "Globex", old, "v1.docx", new, "v2.docx")
        store.finish_review(db, review_id, {"doc_type": "msa", "verdict": "send back", "changes": []})
    new.write_bytes(new.read_bytes() + b"tampered")

    response = http.post(f"/reviews/{review_id}/decision", data={"action": "sign", "name": "A", "consent": "yes"})
    assert response.status_code == 409


def test_missing_claude_is_shown_in_the_trail(monkeypatch, tmp_path):
    def unavailable(*args):
        raise LLMUnavailable("The `claude` CLI was not found.")
    monkeypatch.setattr(classifier, "ask_claude", unavailable)
    http = client(monkeypatch, tmp_path)

    page = http.post("/reviews", data={"demo": "nda"})
    assert "AI review stopped" in page.text and "was not found" in page.text
    assert http.get(page.url.path.replace("/reviews", "/api/reviews")).json()["status"] == "failed"
