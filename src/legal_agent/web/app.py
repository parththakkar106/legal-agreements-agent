"""The agreement desk web app: negotiate a contract round by round, then sign it.

Pages are plain HTML from Jinja templates (no frontend framework). The same
data is available as JSON under /api for other systems.

Flow for each version the counterparty sends:
  1. the clause diff against the version we sent is shown at once (plain code, no AI);
  2. the AI review runs in a background thread and the page refreshes until it is done;
  3. a person answers each change (accept, reject, counter) and closes the round;
  4. if everything was accepted the agreement is ready to sign, otherwise our next
     version is built from the answers and goes back unsigned.
Every step lands in the audit trail. A new agreement can also hold the review until
a person asks for it, or start with only our version, waiting for their reply.
"""

import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path
from threading import Thread
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from legal_agent import negotiation, store
from legal_agent.clause_diff import Change, diff_clauses
from legal_agent.docx_parser import parse_docx
from legal_agent.docx_writer import write_docx
from legal_agent.orchestrator import Usage, review_contract
from legal_agent.policy import load_policy, red_line_hits, rules_for
from legal_agent.signing import SIGNATURE_FONTS, SIGNATURE_FONTS_DIR, SIGNER
from legal_agent.web.sources import document, round_sources

ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "data" / "contracts"
SECTIONS = {  # the home page, one section per status, in the order work flows
    "not reviewed": ("Not reviewed", "Their redline is in; run the AI review when you are ready."),
    "reviewing": ("AI reviewing", "The AI is reviewing their changes."),
    "failed": ("AI review stopped", "Open the agreement to see why and try again."),
    "needs review": ("Needs review", "The AI has reviewed their changes; a person answers each one."),
    "ready to sign": ("Ready to sign", "Every change is agreed; only a person can sign."),
    "waiting for counterparty": ("Waiting for counterparty", "Our latest version is with them."),
    "signed": ("Signed", "Signed PDF with a certificate page."),
}

app = FastAPI(title="Acme Agreement Desk")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.globals["profile"] = lambda: _profile()  # the top bar shows who is signed in
SIGNATURE_STYLES = tuple(SIGNATURE_FONTS)
app.mount("/fonts", StaticFiles(directory=SIGNATURE_FONTS_DIR), name="fonts")


@app.get("/")
def index(request: Request, error: str = ""):
    with store.connect() as db:
        agreements = [store.get_agreement(db, r["id"]) for r in store.list_agreements(db)]
    rows = sorted((_summary(a) for a in agreements), key=lambda r: r["updated_at"] or "", reverse=True)
    sections = [(status, title, note, [r for r in rows if r["status"] == status])
                for status, (title, note) in SECTIONS.items()]
    return templates.TemplateResponse(request, "index.html", {
        "sections": [s for s in sections if s[3]], "total": len(rows), "error": error})


def _summary(a: dict) -> dict:
    """One line of the contract list: where the agreement stands and what to do next."""
    latest = _latest(a)
    rnd = list(a["rounds"].values())[-1] if a["rounds"] else None
    tally = {}
    if rnd and rnd["result"]:
        for key in ("escalate", "push_back", "accept"):
            tally[key] = sum(1 for c in rnd["result"]["changes"] if c["decision"] == key)
    them = a["counterparty"]
    status = a["status"]
    if status == "signed":
        signed = next(e for e in reversed(a["events"]) if e["action"] == "sign")
        step = f"Signed v{signed['version']} on {signed['at'][:10]} by {signed['actor']}"
    elif status == "ready to sign":
        step = f"Sign v{latest['number']}"
    elif status == "needs review":
        open_items = sum(1 for c in rnd["result"]["changes"]
                         if negotiation.missing(c["decision"], rnd["answers"].get(c["change"]["id"])))
        step = f"Answer {open_items} change{'' if open_items == 1 else 's'} in v{rnd['new_version']}" if open_items \
            else f"Close the round on v{rnd['new_version']}"
    elif status == "not reviewed":
        step = f"Run the AI review of v{rnd['new_version']} ({len(rnd['changes'])} changes)"
    elif status == "reviewing":
        step = f"AI is reviewing v{rnd['new_version']}"
    elif status == "failed":
        step = "AI review stopped: open to retry"
    else:
        step = f"Waiting for {them} to reply to v{latest['number']}"
    return {"id": a["id"], "title": a["title"], "counterparty": them, "doc_type": a["doc_type"],
            "status": status, "versions": len(a["versions"]), "tally": tally, "step": step,
            "review_round": rnd["id"] if status == "not reviewed" else None,
            "updated_at": a["events"][-1]["at"] if a["events"] else a["created_at"]}


@app.post("/agreements")
def create_agreement(title: str = Form(""), counterparty: str = Form(""), review: str = Form(""),
                     ours: UploadFile = File(...), theirs: UploadFile | None = File(None),
                     related: UploadFile | None = File(None)):
    """A new agreement: our version (v1) and, if they already replied, their version (v2).

    `related` is a contract this one refers to (an SOW's MSA) so the AI can read those clauses.
    """
    if not ours.filename:
        return RedirectResponse("/?error=" + quote("Upload our version as a .docx."), status_code=303)
    title = title.strip() or Path(ours.filename).stem
    counterparty = counterparty.strip() or "Counterparty"
    with store.connect() as db:
        agreement_id = store.create_agreement(db, title, counterparty,
                                              _save_upload(related) if related and related.filename else None)
        store.add_version(db, agreement_id, store.ACME, _save_upload(ours), ours.filename)
        if theirs and theirs.filename:
            _receive(db, agreement_id, _save_upload(theirs), theirs.filename, review=bool(review))
    return RedirectResponse(f"/agreements/{agreement_id}", status_code=303)


@app.post("/agreements/{agreement_id}/versions")
def upload_reply(agreement_id: int, new: UploadFile = File(...)):
    """The counterparty's next version."""
    agreement = _load(agreement_id)
    _require(agreement["status"] == "waiting for counterparty", "We are not waiting for a new version.")
    with store.connect() as db:
        _receive(db, agreement_id, _save_upload(new), new.filename)
    return RedirectResponse(f"/agreements/{agreement_id}", status_code=303)


@app.post("/agreements/{agreement_id}/accepted")
def record_acceptance(agreement_id: int):
    """The counterparty accepted our last version as it is (say, by email): log it as their next version, unchanged."""
    agreement = _load(agreement_id)
    _require(agreement["status"] == "waiting for counterparty", "We are not waiting for a new version.")
    ours = _latest(agreement)
    name = f"{agreement['counterparty'].split()[0]}_{_slug(agreement['title'])}_v{ours['number'] + 1}.docx"
    with store.connect() as db:
        _receive(db, agreement_id, ours["file"], name)
    return RedirectResponse(f"/agreements/{agreement_id}", status_code=303)


def _receive(db, agreement_id: int, path: Path, name: str, review: bool = True):
    """Log their version and its diff against what we sent; start the AI review unless asked to wait."""
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
    elif review:
        _start_review(db, round_id)


@app.post("/rounds/{round_id}/review")
def review_round(round_id: int):
    """Run the AI review a person asked for, or try again after one stopped."""
    agreement, rnd = _round(round_id)
    _require(rnd["status"] in ("not reviewed", "failed"), "This round is already reviewed.")
    with store.connect() as db:
        _start_review(db, round_id)
    return RedirectResponse(f"/agreements/{agreement['id']}#v{rnd['new_version']}", status_code=303)


def _start_review(db, round_id: int):
    """The AI review runs in a background thread; the page shows its progress."""
    agreement, rnd = _round(round_id)
    versions = agreement["versions"]
    related = Path(agreement["related_file"]) if agreement["related_file"] else None
    store.start_review(db, round_id)
    Thread(target=run_review, args=(round_id, versions[rnd["old_version"]]["file"],
                                    versions[rnd["new_version"]]["file"], related), daemon=True).start()


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
        "history": negotiation.clause_history(agreement), "reviewer": _reviewer(request),
        "styles": SIGNATURE_STYLES})


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
def sign(agreement_id: int, name: str = Form(""), job_title: str = Form(""), style: str = Form(SIGNATURE_STYLES[0]),
         consent: str = Form(""), remember: str = Form("")):
    """Only a person signs, and only the version both sides agreed. The AI never calls this.

    The form starts from the profile's signature; `remember` saves what was used back to it.
    Each document needs its own consent tick.
    """
    agreement = _load(agreement_id)
    _require(agreement["status"] == "ready to sign", "This agreement is not ready to sign.")
    name, job_title = name.strip(), job_title.strip()
    _require(bool(consent and name), "To sign, tick the consent box and type your full name.")
    _require(style in SIGNATURE_STYLES, "Unknown signature style.")
    if remember:
        with store.connect() as db:
            store.save_profile(db, name, job_title, style)
    final = _latest(agreement)
    if store.sha256_of(final["file"]) != final["sha256"]:
        raise HTTPException(409, "The document changed after it was agreed. It cannot be signed.")
    rules = rules_for(load_policy(), agreement["doc_type"] or "")
    hits = red_line_hits(rules, "\n".join(c.text for c in parse_docx(final["file"])))
    final_check = f"red lines found: {', '.join(hits)}" if hits else "no red lines in the final text"
    signed_at = store.now()
    certificate = {
        "document": f"{agreement['title']} between {store.ACME} and {agreement['counterparty']}",
        "signature": {"name": name, "job_title": job_title, "style": style},
        "fields": {"Agreement": agreement["title"], "Counterparty": agreement["counterparty"],
                   "Signed version": f"v{final['number']} ({final['name']})", "SHA-256 of signed file": final["sha256"],
                   "Signed by": f"{name}{', ' + job_title if job_title else ''} for {store.ACME},"
                                f" typed electronic signature ({style} style) with consent",
                   "Signed at": signed_at, "Final playbook check": final_check},
        "versions": [f"v{v['number']}  {v['created_at']}  from {v['author']}  {v['name']}  SHA-256 {v['sha256']}"
                     for v in agreement["versions"].values()]}
    pdf = SIGNER.sign(Path(final["file"]), store.files_dir(agreement_id) / "signed.pdf", certificate)
    with store.connect() as db:
        store.set_signed(db, agreement_id, pdf)
        store.add_event(db, agreement_id, "sign", name,
                        f"Signed v{final['number']} ({final['name']}), SHA-256 {final['sha256']}. Final check: {final_check}.",
                        version=final["number"])
    return RedirectResponse(f"/agreements/{agreement_id}#signed", status_code=303)


@app.get("/profile")
def show_profile(request: Request, saved: bool = False):
    return templates.TemplateResponse(request, "profile.html", {"styles": SIGNATURE_STYLES, "saved": saved})


@app.post("/profile")
def update_profile(name: str = Form(...), job_title: str = Form(""), style: str = Form(SIGNATURE_STYLES[0])):
    """Adopt a signature once; every sign form then uses it."""
    _require(bool(name.strip()), "Type your full name.")
    _require(style in SIGNATURE_STYLES, "Unknown signature style.")
    with store.connect() as db:
        store.save_profile(db, name.strip(), job_title.strip(), style)
    return RedirectResponse("/profile?saved=1", status_code=303)


def _profile() -> dict | None:
    with store.connect() as db:
        return store.get_profile(db)


def _reviewer(request: Request) -> str:
    """The name to prefill in answer and sign forms: the profile, else the last name typed."""
    me = _profile()
    return me["name"] if me else request.cookies.get("reviewer", "")


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
