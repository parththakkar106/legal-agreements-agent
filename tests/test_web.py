from fastapi.testclient import TestClient
from fakes import result, review_answer

from legal_agent import store
from legal_agent.agents import classifier, grouper, reviewer
from legal_agent.llm import LLMUnavailable
from legal_agent.web import app as web


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
    return TestClient(web.app)


def test_demo_review_then_sign(monkeypatch, tmp_path):
    fake_ai(monkeypatch)
    http = client(monkeypatch, tmp_path)

    page = http.post("/reviews", data={"demo": "msa"})
    assert page.status_code == 200 and "send back" in page.text and "We can offer 45 days." in page.text
    review_id = page.url.path.rsplit("/", 1)[1]

    refused = http.post(f"/reviews/{review_id}/decision", data={"action": "sign", "name": "Ana Ruiz"})
    assert refused.status_code == 400  # no consent box

    signed = http.post(f"/reviews/{review_id}/decision", data={"action": "sign", "name": "Ana Ruiz", "consent": "yes"})
    assert signed.status_code == 200
    data = http.get(f"/api/reviews/{review_id}").json()
    assert data["status"] == "signed"
    assert [e["action"] for e in data["events"]] == ["reviewed", "sign"]
    assert data["new_sha256"] in data["events"][1]["detail"]

    again = http.post(f"/reviews/{review_id}/decision", data={"action": "escalate"})
    assert again.status_code == 400  # already decided


def test_sign_refused_if_document_changed(monkeypatch, tmp_path):
    fake_ai(monkeypatch)
    http = client(monkeypatch, tmp_path)
    copy = tmp_path / "new.docx"
    copy.write_bytes((web.CONTRACTS / "redlines" / "msa_globex_v2.docx").read_bytes())
    with store.connect() as db:
        review_id = store.save_review(db, "t", copy, {"doc_type": "msa", "verdict": "send back"})
    copy.write_bytes(copy.read_bytes() + b"tampered")

    response = http.post(f"/reviews/{review_id}/decision", data={"action": "sign", "name": "A", "consent": "yes"})
    assert response.status_code == 409


def test_missing_claude_shows_error(monkeypatch, tmp_path):
    def unavailable(*args):
        raise LLMUnavailable("The `claude` CLI was not found.")
    monkeypatch.setattr(classifier, "ask_claude", unavailable)
    http = client(monkeypatch, tmp_path)

    page = http.post("/reviews", data={"demo": "nda"})
    assert page.url.path == "/" and "was not found" in page.text
