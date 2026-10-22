import json

from fastapi.testclient import TestClient
from fakes import result, review_answer

from legal_agent import desk, store, webhooks
from legal_agent.agents import classifier, grouper, reviewer
from legal_agent.web import app as web

KEY = {"X-API-Key": "s3cret"}


class RunNow:
    """Stands in for Thread so background work finishes before the call returns."""

    def __init__(self, target, args, daemon):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


def setup(monkeypatch, tmp_path, keys="crm:s3cret"):
    monkeypatch.setattr(store, "DB_FILE", tmp_path / "desk.db")
    monkeypatch.setattr(desk, "Thread", RunNow)
    monkeypatch.setattr(webhooks, "Thread", RunNow)
    monkeypatch.setenv("DESK_API_KEYS", keys)
    monkeypatch.setattr(classifier, "ask_claude", lambda *a: result({"doc_type": "msa", "changes": []}))
    monkeypatch.setattr(reviewer, "ask_claude", lambda s, prompt, schema: result(
        review_answer(decision="push_back", rule_ids=["payment-terms"], counter_proposal="45 days.")
        if 'clause="Payment Period"' in prompt else review_answer(rule_ids=["liability-cap"])))
    monkeypatch.setattr(grouper, "ask_claude", lambda *a: result({"groups": []}))
    return TestClient(web.app)


def files(kind="msa"):
    return {"ours": ("Acme_v1.docx", (web.CONTRACTS / "base" / f"{kind}.docx").read_bytes()),
            "theirs": ("Globex_v2.docx", (web.CONTRACTS / "redlines" / f"{kind}_globex_v2.docx").read_bytes())}


def test_the_api_needs_a_key(monkeypatch, tmp_path):
    http = setup(monkeypatch, tmp_path)
    assert http.get("/api/v1/agreements").status_code == 401
    assert http.get("/api/v1/agreements", headers={"X-API-Key": "wrong"}).status_code == 401
    assert http.get("/api/v1/agreements", headers=KEY).json() == []
    monkeypatch.delenv("DESK_API_KEYS")
    assert http.get("/api/v1/agreements", headers=KEY).status_code == 503  # no keys set: the API is off
    assert http.get("/healthz").json() == {"status": "ok"}


def test_a_negotiation_through_the_api(monkeypatch, tmp_path):
    http = setup(monkeypatch, tmp_path)
    created = http.post("/api/v1/agreements", headers=KEY, files=files(),
                        data={"title": "Cloud Service Agreement", "counterparty": "Globex Inc."})
    assert created.status_code == 201
    agreement = created.json()
    assert agreement["status"] == "needs review" and agreement["next_step"].startswith("Answer 1 change")
    assert "file" not in json.dumps(agreement["versions"])  # links, never server paths
    rnd = agreement["rounds"][0]
    payment = next(c for c in rnd["changes"] if c["clause"] == "Payment Period")
    assert payment["decision"] == "push_back" and payment["suggested_reply"] == "45 days."

    assert http.post(f"/api/v1/rounds/{rnd['id']}/close", headers=KEY, json={"actor": "Ana"}).status_code == 400
    http.post(f"/api/v1/rounds/{rnd['id']}/answers", headers=KEY, json={
        "change_id": payment["id"], "actor": "Ana Ruiz", "outcome": "counter", "counter_text": "45 days from the invoice date."})
    closed = http.post(f"/api/v1/rounds/{rnd['id']}/close", headers=KEY, json={"actor": "Ana Ruiz"}).json()
    assert closed["status"] == "waiting for counterparty" and len(closed["versions"]) == 3
    assert closed["audit_trail"][-2]["actor"] == "Ana Ruiz via crm"  # the person and the system that called

    download = http.get(closed["versions"][2]["download"], headers=KEY)
    assert download.status_code == 200 and download.content[:2] == b"PK"
    assert http.get(closed["versions"][2]["download"]).status_code == 401

    accepted = http.post(f"/api/v1/agreements/{agreement['id']}/accepted", headers=KEY).json()
    assert accepted["status"] == "ready to sign"
    # Signing is only in the web app, never in the API.
    assert http.post(f"/api/v1/agreements/{agreement['id']}/sign", headers=KEY).status_code in (404, 405)


def test_review_can_wait_for_a_call(monkeypatch, tmp_path):
    http = setup(monkeypatch, tmp_path)
    agreement = http.post("/api/v1/agreements", headers=KEY, files=files(),
                          data={"title": "MSA", "counterparty": "Globex", "review": "false"}).json()
    assert agreement["status"] == "not reviewed"
    rnd = agreement["rounds"][0]["id"]
    assert http.post(f"/api/v1/rounds/{rnd}/review", headers=KEY).status_code == 202
    assert http.get(f"/api/v1/agreements/{agreement['id']}", headers=KEY).json()["status"] == "needs review"


def test_uploads_must_be_small_word_files(monkeypatch, tmp_path):
    http = setup(monkeypatch, tmp_path)
    data = {"title": "X", "counterparty": "Y"}
    fake = {"ours": ("contract.docx", b"not a zip at all")}
    assert http.post("/api/v1/agreements", headers=KEY, files=fake, data=data).status_code == 415
    pdf = {"ours": ("contract.pdf", b"%PDF-1.4")}
    assert http.post("/api/v1/agreements", headers=KEY, files=pdf, data=data).status_code == 415
    monkeypatch.setenv("DESK_MAX_UPLOAD_MB", "0.001")
    assert http.post("/api/v1/agreements", headers=KEY, files=files(), data=data).status_code == 413


def test_webhooks_are_signed_and_cover_the_workflow(monkeypatch, tmp_path):
    http = setup(monkeypatch, tmp_path)
    monkeypatch.setenv("DESK_WEBHOOK_URLS", "https://crm.example.com/hooks")
    monkeypatch.setenv("DESK_WEBHOOK_SECRET", "whsec")
    sent = []
    monkeypatch.setattr(webhooks, "post", lambda url, body, headers: sent.append((url, body, headers)) or 200)

    http.post("/api/v1/agreements", headers=KEY, files=files(), data={"title": "MSA", "counterparty": "Globex"})

    events = [h["X-Desk-Event"] for _, _, h in sent]
    assert events[:2] == ["version.sent", "version.received"]
    assert {"review.completed", "agreement.status_changed"} <= set(events)
    url, body, headers = sent[-1]
    assert url == "https://crm.example.com/hooks"
    assert webhooks.verify("whsec", headers["X-Desk-Timestamp"], body, headers["X-Desk-Signature"])
    assert not webhooks.verify("other", headers["X-Desk-Timestamp"], body, headers["X-Desk-Signature"])
    assert not webhooks.verify("whsec", "1000", body, headers["X-Desk-Signature"])  # too old: a replay
    event = json.loads(body)
    assert event["agreement"]["url"].endswith("/api/v1/agreements/1") and "old_text" not in body


def test_webhooks_retry_then_give_up_and_never_go_unsigned(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(webhooks, "post", lambda *a: calls.append(a) or 500)
    monkeypatch.setattr(webhooks.time, "sleep", lambda s: None)
    assert webhooks.deliver("https://x.example.com", "round.closed", "{}", "whsec") is False
    assert len(calls) == webhooks.ATTEMPTS

    monkeypatch.setenv("DESK_WEBHOOK_URLS", "https://x.example.com")
    monkeypatch.delenv("DESK_WEBHOOK_SECRET", raising=False)
    calls.clear()
    webhooks.publish("round.closed", {"id": 1})
    assert calls == []  # no secret, no delivery
