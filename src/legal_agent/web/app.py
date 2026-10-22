"""The agreement desk web app: negotiate a contract round by round, then sign it.

Pages are plain HTML from Jinja templates (no frontend framework). The same
data is available as JSON under /api for other systems.

Flow for each version the counterparty sends:
  1. the clause diff against the version we sent is shown at once (plain code, no AI);
  2. the AI review runs in a background thread and the page refreshes until it is done;
  3. a person answers each change (accept, reject, counter) and closes the round;
  4. if everything was accepted the agreement is ready to sign, otherwise our next
     version is built from the answers and goes back unsigned.
Every step lands in the audit trail.
"""

import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path
from threading import Thread
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from legal_agent import negotiation, store
from legal_agent.clause_diff import Change, diff_clauses
from legal_agent.docx_parser import parse_docx
from legal_agent.docx_writer import write_docx
from legal_agent.orchestrator import Usage, review_contract
from legal_agent.policy import load_policy, red_line_hits, rules_for
from legal_agent.signing import SIGNER
from legal_agent.web import examples
from legal_agent.web.sources import document, round_sources

ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "data" / "contracts"

# Ready-made negotiations so the demo works without preparing any files.
# Each has our version (v1), the customer's redline (v2) and, for the SOW, the MSA it points at.
DEMOS = {
    "nda": {"title": "Mutual NDA", "counterparty": "Globex Inc.",
            "v1": ("base/nda.docx", "Acme_Mutual-NDA_v1.docx"),
            "v2": ("redlines/nda_globex_v2.docx", "Globex_Mutual-NDA_v2_redline.docx")},
    "msa": {"title": "Cloud Service Agreement", "counterparty": "Globex Inc.",
            "v1": ("base/msa.docx", "Acme_Cloud-Service-Agreement_v1.docx"),
            "v2": ("redlines/msa_globex_v2.docx", "Globex_Cloud-Service-Agreement_v2_redline.docx")},
    "dpa": {"title": "Data Processing Agreement", "counterparty": "Globex Inc.",
            "v1": ("base/dpa.docx", "Acme_DPA_v1.docx"),
            "v2": ("redlines/dpa_globex_v2.docx", "Globex_DPA_v2_redline.docx")},
    "sow": {"title": "Onboarding SOW", "counterparty": "Globex Inc.",
            "v1": ("base/sow.docx", "Acme_Onboarding-SOW_v1.docx"),
            "v2": ("redlines/sow_globex_v2.docx", "Globex_Onboarding-SOW_v2_redline.docx"),
            "related": "base/msa.docx"},
}

app = FastAPI(title="Acme Agreement Desk")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


@app.get("/")
def index(request: Request, error: str = ""):
    with store.connect() as db:
        agreements = store.list_agreements(db)
    return templates.TemplateResponse(request, "index.html", {
        "agreements": agreements, "demos": DEMOS, "error": error, "examples_running": examples.running})


@app.post("/agreements")
def create_agreement(demo: str = Form(""), title: str = Form(""), counterparty: str = Form(""),
                     old: UploadFile | None = File(None), new: UploadFile | None = File(None)):
    """Start a negotiation: our v1 and their v2, from a demo or two uploaded .docx files."""
    if demo in DEMOS:
        spec = DEMOS[demo]
        title, counterparty = spec["title"], spec["counterparty"]
        v1, v1_name = CONTRACTS / spec["v1"][0], spec["v1"][1]
        v2, v2_name = CONTRACTS / spec["v2"][0], spec["v2"][1]
        related = CONTRACTS / spec["related"] if "related" in spec else None
    elif old and new and old.filename and new.filename:
        v1, v1_name, v2, v2_name = _save_upload(old), old.filename, _save_upload(new), new.filename
        title, counterparty, related = title or Path(new.filename).stem, counterparty or "Counterparty", None
    else:
        return RedirectResponse("/?error=" + quote("Pick a demo, or upload both versions as .docx."), status_code=303)

    with store.connect() as db:
        agreement_id = store.create_agreement(db, title, counterparty, related)
        store.add_version(db, agreement_id, store.ACME, v1, v1_name)
        _receive(db, agreement_id, v2, v2_name)
    return RedirectResponse(f"/agreements/{agreement_id}", status_code=303)


@app.post("/agreements/{agreement_id}/versions")
def upload_reply(agreement_id: int, new: UploadFile = File(...)):
    """The counterparty's next version."""
    agreement = _load(agreement_id)
    _require(agreement["status"] == "waiting for counterparty", "We are not waiting for a new version.")
    with store.connect() as db:
        _receive(db, agreement_id, _save_upload(new), new.filename)
    return RedirectResponse(f"/agreements/{agreement_id}", status_code=303)


@app.post("/agreements/{agreement_id}/simulate-reply")
def simulate_reply(agreement_id: int):
    """Demo only: the counterparty sends our last version back unchanged, i.e. they accept it."""
    agreement = _load(agreement_id)
    _require(agreement["status"] == "waiting for counterparty", "We are not waiting for a new version.")
    ours = _latest(agreement)
    name = f"{agreement['counterparty'].split()[0]}_{_slug(agreement['title'])}_v{ours['number'] + 1}.docx"
    with store.connect() as db:
        _receive(db, agreement_id, ours["file"], name)
    return RedirectResponse(f"/agreements/{agreement_id}", status_code=303)


def _receive(db, agreement_id: int, path: Path, name: str):
    """Log their version, show its diff against what we sent at once, and start the AI review."""
    agreement = store.get_agreement(db, agreement_id)
    ours = _latest(agreement)
    theirs = store.add_version(db, agreement_id, agreement["counterparty"], path, name)
    changes = diff_clauses(parse_docx(ours["file"]), parse_docx(theirs["file"]))
    round_id = store.start_round(db, agreement_id, ours["number"], theirs["number"], [asdict(c) for c in changes])
    if not changes:  # they accepted our version as it was: nothing to review
        store.finish_round(db, round_id, {"doc_type": agreement["doc_type"] or "unknown", "verdict": "ready to sign",
                                          "changes": [], "groups": [], "usage": asdict(Usage())}, by_ai=False)
        store.close_round(db, round_id, "the desk", f"No changes: {agreement['counterparty']} accepted v{ours['number']}.")
        store.set_status(db, agreement_id, "ready to sign")
        return
    related = Path(agreement["related_file"]) if agreement["related_file"] else None
    Thread(target=run_review, args=(round_id, ours["file"], theirs["file"], related), daemon=True).start()


def run_review(round_id: int, old_path: str, new_path: str, related: Path | None):
    """Runs in a background thread. Any failure (including no `claude` CLI) is recorded, never hidden."""
    with store.connect() as db:
        try:
            result = review_contract(old_path, new_path, {"msa": related} if related else None,
                                     on_progress=lambda step: store.set_stage(db, round_id, step))
            store.finish_round(db, round_id, asdict(result))
        except Exception as error:  # the page must show why the review stopped
            store.fail_round(db, round_id, str(error))


@app.get("/agreements/{agreement_id}")
def show_agreement(request: Request, agreement_id: int):
    agreement = _load(agreement_id)
    sources = {rid: round_sources(agreement["versions"][r["old_version"]], agreement["versions"][r["new_version"]],
                                  agreement["related_file"], r["result"])
               for rid, r in agreement["rounds"].items() if r["result"] and r["result"]["changes"]}
    missing = {rid: {c["change"]["id"]: negotiation.missing(c["decision"], r["answers"].get(c["change"]["id"]))
                     for c in r["result"]["changes"]}
               for rid, r in agreement["rounds"].items() if r["result"]}
    return templates.TemplateResponse(request, "agreement.html", {
        "agreement": agreement, "sources": sources, "missing": missing,
        "history": negotiation.clause_history(agreement), "reviewer": request.cookies.get("reviewer", "")})


@app.post("/rounds/{round_id}/items/{change_id}")
def answer_change(round_id: int, change_id: int, actor: str = Form(...), outcome: str = Form(""),
                  counter_text: str = Form(""), internal_comment: str = Form(""), external_comment: str = Form("")):
    """A person's answer to one change. Saved as they go; the round closes separately."""
    agreement, rnd = _round(round_id)
    _require(rnd["status"] == "open", "This round is not open.")
    _require(outcome in ("", *negotiation.OUTCOMES), "Unknown outcome.")
    with store.connect() as db:
        store.save_item(db, round_id, change_id, {
            "outcome": outcome or None, "counter_text": counter_text.strip(), "internal_comment": internal_comment.strip(),
            "external_comment": external_comment.strip()}, actor.strip() or "Reviewer")
    response = RedirectResponse(f"/agreements/{agreement['id']}#change-{round_id}-{change_id}", status_code=303)
    response.set_cookie("reviewer", actor.strip())
    return response


@app.post("/rounds/{round_id}/close")
def close_round(round_id: int, actor: str = Form(...)):
    """All changes answered: either ready to sign, or build and send our next version."""
    agreement, rnd = _round(round_id)
    _require(rnd["status"] == "open", "This round is not open.")
    reviewed = rnd["result"]["changes"]
    decisions = {r["change"]["id"]: r["decision"] for r in reviewed}
    open_items = [cid for cid, decision in decisions.items() if negotiation.missing(decision, rnd["answers"].get(cid))]
    _require(not open_items, "Some changes still need an answer.")
    outcomes = {cid: (negotiation.outcome_of(decision, rnd["answers"].get(cid)),
                      (rnd["answers"].get(cid) or {}).get("counter_text") or "") for cid, decision in decisions.items()}
    counts = {o: sum(1 for oc, _ in outcomes.values() if oc == o) for o in negotiation.OUTCOMES}
    summary = ", ".join(f"{n} {o}ed" for o, n in counts.items() if n)
    actor = actor.strip() or "Reviewer"

    with store.connect() as db:
        store.close_round(db, round_id, actor, summary)
        if counts["accept"] == len(outcomes):
            store.set_status(db, agreement["id"], "ready to sign")
            return RedirectResponse(f"/agreements/{agreement['id']}#sign", status_code=303)
        old, new = agreement["versions"][rnd["old_version"]], agreement["versions"][rnd["new_version"]]
        changes = [Change(**r["change"]) for r in reviewed]
        clauses = negotiation.next_version(parse_docx(old["file"]), parse_docx(new["file"]), changes, outcomes)
        number = new["number"] + 1
        note = negotiation.cover_note(agreement["counterparty"], new["number"], number, changes,
                                      {cid: dict(i) for cid, i in rnd["answers"].items()}, decisions)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "next.docx"
            write_docx(clauses, path, agreement["title"])
            store.add_version(db, agreement["id"], store.ACME, path, f"Acme_{_slug(agreement['title'])}_v{number}.docx", note)
        store.set_status(db, agreement["id"], "waiting for counterparty")
    return RedirectResponse(f"/agreements/{agreement['id']}#v{number}", status_code=303)


@app.post("/agreements/{agreement_id}/sign")
def sign(agreement_id: int, name: str = Form(""), consent: str = Form("")):
    """Only a person signs, and only the version both sides agreed. The AI never calls this."""
    agreement = _load(agreement_id)
    _require(agreement["status"] == "ready to sign", "This agreement is not ready to sign.")
    _require(bool(consent and name.strip()), "To sign, tick the consent box and type your full name.")
    final = _latest(agreement)
    if store.sha256_of(final["file"]) != final["sha256"]:
        raise HTTPException(409, "The document changed after it was agreed. It cannot be signed.")
    rules = rules_for(load_policy(), agreement["doc_type"] or "")
    hits = red_line_hits(rules, "\n".join(c.text for c in parse_docx(final["file"])))
    final_check = f"red lines found: {', '.join(hits)}" if hits else "no red lines in the final text"
    signed_at = store.now()
    certificate = {
        "document": f"{agreement['title']} between {store.ACME} and {agreement['counterparty']}",
        "fields": {"Agreement": agreement["title"], "Counterparty": agreement["counterparty"],
                   "Signed version": f"v{final['number']} ({final['name']})", "SHA-256 of signed file": final["sha256"],
                   "Signed by": f"{name.strip()} for {store.ACME}, typed electronic signature with consent",
                   "Signed at": signed_at, "Final playbook check": final_check},
        "versions": [f"v{v['number']}  {v['created_at']}  from {v['author']}  {v['name']}  SHA-256 {v['sha256']}"
                     for v in agreement["versions"].values()]}
    pdf = SIGNER.sign(Path(final["file"]), store.files_dir(agreement_id) / "signed.pdf", certificate)
    with store.connect() as db:
        store.set_signed(db, agreement_id, pdf)
        store.add_event(db, agreement_id, "sign", name.strip(),
                        f"Signed v{final['number']} ({final['name']}), SHA-256 {final['sha256']}. Final check: {final_check}.",
                        version=final["number"])
    return RedirectResponse(f"/agreements/{agreement_id}#signed", status_code=303)


@app.get("/agreements/{agreement_id}/signed.pdf")
def signed_pdf(agreement_id: int):
    agreement = _load(agreement_id)
    _require(bool(agreement["signed_pdf"]), "Not signed yet.")
    return FileResponse(agreement["signed_pdf"], filename=f"{_slug(agreement['title'])}_signed.pdf")


@app.get("/agreements/{agreement_id}/versions/{number}")
def show_version(request: Request, agreement_id: int, number: int):
    agreement = _load(agreement_id)
    version = _version(agreement, number)
    changed = {c["clause_id"] for r in agreement["rounds"].values() for c in r["changes"]
               if number in (r["old_version"], r["new_version"])}
    return templates.TemplateResponse(request, "document.html", {
        "doc": document(version["file"], version["name"]), "label": f"v{number}, from {version['author']}",
        "changed": changed, "back": (f"/agreements/{agreement_id}", agreement["title"]),
        "download": f"/agreements/{agreement_id}/versions/{number}/download"})


@app.get("/agreements/{agreement_id}/versions/{number}/download")
def download_version(agreement_id: int, number: int):
    version = _version(_load(agreement_id), number)
    return FileResponse(version["file"], filename=version["name"])


@app.get("/agreements/{agreement_id}/rounds/{round_id}/diff")
def show_diff(request: Request, agreement_id: int, round_id: int):
    """The whole document as a git-style diff: changed clauses in full, unchanged ones folded."""
    agreement, rnd = _round(round_id)
    _require(agreement["id"] == agreement_id, "Wrong agreement.")
    old, new = agreement["versions"][rnd["old_version"]], agreement["versions"][rnd["new_version"]]
    changed = {c["clause_id"]: c for c in rnd["changes"]}
    removed = [c for c in rnd["changes"] if c["kind"] == "removed"]
    rows = [{"clause": c, "change": changed.get(c.id)} for c in parse_docx(new["file"])]
    return templates.TemplateResponse(request, "diff.html", {
        "agreement": agreement, "old": old, "new": new, "rows": rows, "removed": removed})


@app.get("/demos/{demo}/{version}")
def show_demo_document(request: Request, demo: str, version: str):
    path, name = _demo_version(demo, version)
    return templates.TemplateResponse(request, "document.html", {
        "doc": document(path, name), "label": "v1, ours" if version == "v1" else "v2, their redline",
        "changed": set(), "back": ("/", "Agreements"), "download": f"/demos/{demo}/{version}/download"})


@app.get("/demos/{demo}/{version}/download")
def download_demo_document(demo: str, version: str):
    path, name = _demo_version(demo, version)
    return FileResponse(path, filename=name)


@app.post("/examples")
def start_examples():
    """Play the example negotiations through the app in the background (needs `claude -p`)."""
    if not examples.running:
        Thread(target=examples.load_examples, daemon=True).start()
    return RedirectResponse("/", status_code=303)


@app.get("/api/examples")
def examples_progress() -> dict:
    with store.connect() as db:
        count = len(store.list_agreements(db))
    return {"busy": examples.running, "token": str(count)}


@app.get("/api/agreements/{agreement_id}/progress")
def review_progress(agreement_id: int) -> dict:
    """What the page polls while the AI works: reload when busy turns false or the step changes."""
    agreement = _load(agreement_id)
    stages = [r["stage"] for r in agreement["rounds"].values() if r["status"] == "reviewing"]
    return {"busy": agreement["status"] == "reviewing", "token": stages[-1] if stages else ""}


@app.get("/api/agreements/{agreement_id}")
def agreement_json(agreement_id: int) -> dict:
    return _load(agreement_id)


def _load(agreement_id: int) -> dict:
    with store.connect() as db:
        agreement = store.get_agreement(db, agreement_id)
    if agreement is None:
        raise HTTPException(404, "No agreement with this id.")
    return agreement


def _round(round_id: int) -> tuple[dict, dict]:
    with store.connect() as db:
        row = db.execute("SELECT agreement_id FROM rounds WHERE id = ?", (round_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "No round with this id.")
    agreement = _load(row[0])
    return agreement, agreement["rounds"][round_id]


def _latest(agreement: dict) -> dict:
    return agreement["versions"][max(agreement["versions"])]


def _version(agreement: dict, number: int) -> dict:
    if number not in agreement["versions"]:
        raise HTTPException(404, "No such version.")
    return agreement["versions"][number]


def _demo_version(demo: str, version: str) -> tuple[Path, str]:
    if demo not in DEMOS or version not in ("v1", "v2"):
        raise HTTPException(404, "No such demo document.")
    rel, name = DEMOS[demo][version]
    return CONTRACTS / rel, name


def _save_upload(upload: UploadFile) -> Path:
    """Keep an upload in a temporary file; store.add_version copies it into the agreement's folder."""
    path = Path(tempfile.mkdtemp()) / "upload.docx"
    with path.open("wb") as f:
        shutil.copyfileobj(upload.file, f)
    return path


def _slug(title: str) -> str:
    return "-".join(title.split())


def _require(condition: bool, message: str):
    if not condition:
        raise HTTPException(400, message)
