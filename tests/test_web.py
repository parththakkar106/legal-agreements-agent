import re
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


def start(http, kind: str, review: bool = True):
    """Upload our paper (v1) and Globex's redline (v2) of one contract type."""
    files = {"ours": (f"Acme_{kind}_v1.docx", (web.CONTRACTS / "base" / f"{kind}.docx").read_bytes()),
             "theirs": (f"Globex_{kind}_v2_redline.docx",
                        (web.CONTRACTS / "redlines" / f"{kind}_globex_v2.docx").read_bytes())}
    if kind == "sow":
        files["related"] = ("Acme_msa.docx", (web.CONTRACTS / "base" / "msa.docx").read_bytes())
    return http.post("/agreements", files=files,
                     data={"title": kind.upper(), "counterparty": "Globex Inc.", "review": "yes" if review else ""})


def test_full_negotiation_counter_then_accept_then_sign(monkeypatch, tmp_path):
    fake_ai(monkeypatch)
    http = client(monkeypatch, tmp_path)

    page = start(http, "msa")
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
    http.post(f"/agreements/{agreement_id}/accepted")
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
    page = start(http, "sow")
    agreement_id = page.url.path.rsplit("/", 1)[1]
    round_id = next(iter(http.get(f"/api/agreements/{agreement_id}").json()["rounds"]))

    assert "not subject to Section 8" in http.get(f"/agreements/{agreement_id}/versions/2").text
    download = http.get(f"/agreements/{agreement_id}/versions/2/download")
    assert download.content == (web.CONTRACTS / "redlines" / "sow_globex_v2.docx").read_bytes()
    assert "Globex_sow_v2_redline.docx" in download.headers["content-disposition"]
    diff = http.get(f"/agreements/{agreement_id}/rounds/{round_id}/diff").text
    assert "unchanged clause" in diff and "USD 40,000" in diff
    assert http.get(f"/agreements/{agreement_id}/versions/3").status_code == 404


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

    page = start(http, "nda")
    assert "AI review stopped" in page.text and "was not found" in page.text
    assert "Non-Solicitation" in page.text  # the plain diff is still shown
    assert http.get(page.url.path.replace("/agreements", "/api/agreements")).json()["status"] == "failed"


def test_review_waits_until_a_person_starts_it(monkeypatch, tmp_path):
    fake_ai(monkeypatch)
    http = client(monkeypatch, tmp_path)
    page = start(http, "msa", review=False)
    agreement_id = page.url.path.rsplit("/", 1)[1]
    data = http.get(f"/api/agreements/{agreement_id}").json()
    assert data["status"] == "not reviewed" and "Run AI review" in page.text
    assert "Run AI review" in http.get("/").text

    round_id = next(iter(data["rounds"]))
    page = http.post(f"/rounds/{round_id}/review")
    assert "We can offer 45 days." in page.text
    assert http.get(f"/api/agreements/{agreement_id}").json()["status"] == "needs review"
    assert http.post(f"/rounds/{round_id}/review").status_code == 400  # already reviewed


def test_portfolio_builds_saves_and_loads(monkeypatch, tmp_path):
    from legal_agent.web import portfolio
    fake_ai(monkeypatch)
    http = client(monkeypatch, tmp_path / "build")
    portfolio.build(http)
    portfolio.save(store.DB_FILE, tmp_path / "portfolio")

    monkeypatch.setattr(store, "DB_FILE", tmp_path / "app" / "desk.db")
    assert portfolio.load_if_empty(tmp_path / "portfolio")
    assert not portfolio.load_if_empty(tmp_path / "portfolio")  # only into an empty database
    agreements = [http.get(f"/api/agreements/{i}").json() for i in range(1, 10)]
    assert [(a["counterparty"], a["status"], len(a["versions"])) for a in agreements] == [
        ("Globex Inc.", "signed", 4), ("Initech Ltd.", "signed", 2), ("Umbrella Corp.", "signed", 4),
        ("Initech Ltd.", "waiting for counterparty", 3), ("Hooli Inc.", "needs review", 4),
        ("Vandelay Industries", "ready to sign", 2), ("Globex Inc.", "needs review", 2),
        ("Soylent Corp.", "waiting for counterparty", 1), ("Globex Inc.", "not reviewed", 2)]
    assert all(str(tmp_path / "app") in v["file"] for a in agreements for v in a["versions"].values())
    assert http.get("/agreements/1/signed.pdf").content[:4] == b"%PDF"
    home = http.get("/").text
    assert "Hooli Inc." in home and "Run AI review" in home and "Sign v2" in home
    sections = re.findall(r'class="group-head"[^>]*><h2>([^<]+)</h2>', home)
    assert sections == ["Not reviewed", "Needs review", "Ready to sign", "Waiting for counterparty", "Signed"]


def test_signature_adopted_once_is_reused(monkeypatch, tmp_path):
    http = client(monkeypatch, tmp_path)
    assert "Set up your signature" in http.get("/").text

    def ready_nda():
        with store.connect() as db:
            agreement_id = store.create_agreement(db, "NDA", "Globex")
            store.add_version(db, agreement_id, "Globex", web.CONTRACTS / "base" / "nda.docx", "nda.docx")
            store.set_status(db, agreement_id, "ready to sign")
        return agreement_id

    # First signature: name, designation and style, saved for next time.
    first = ready_nda()
    page = http.get(f"/agreements/{first}").text
    assert 'name="job_title"' in page and 'value="Caveat"' in page and 'name="remember"' in page
    http.post(f"/agreements/{first}/sign", data={"name": "Ana Ruiz", "job_title": "Head of Legal", "style": "Caveat",
                                                  "consent": "yes", "remember": "yes"})

    # Next one: the form starts from the saved signature; each document still needs its own consent.
    second = ready_nda()
    page = http.get(f"/agreements/{second}").text
    assert 'value="Ana Ruiz"' in page and 'value="Head of Legal"' in page and 'value="Caveat" checked' in page
    assert http.post(f"/agreements/{second}/sign", data={"name": "Ana Ruiz"}).status_code == 400  # no consent
    assert http.post(f"/agreements/{second}/sign", data={"name": "Ana Ruiz", "style": "Comic Sans",
                                                         "consent": "yes"}).status_code == 400
    http.post(f"/agreements/{second}/sign", data={"name": "Ana Ruiz", "job_title": "Head of Legal", "style": "Caveat",
                                                   "consent": "yes"})
    data = http.get(f"/api/agreements/{second}").json()
    assert data["status"] == "signed" and data["events"][-1]["actor"] == "Ana Ruiz"
    assert Path(data["signed_pdf"]).read_bytes()[:4] == b"%PDF"
    assert http.get("/fonts/Caveat.ttf").status_code == 200
    assert "Ana Ruiz" in http.get("/").text
    assert http.post("/profile", data={"name": "Ana", "style": "Comic Sans"}).status_code == 400
