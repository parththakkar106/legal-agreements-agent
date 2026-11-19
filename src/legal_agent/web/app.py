"""The agreement desk web app: the pages people use, plus the JSON API for other systems.

The pages are plain HTML from Jinja templates (no frontend framework). The workflow
itself lives in desk.py, so a page and an API call run exactly the same code.

  /                the contract list, by status
  /agreements/...  one agreement: versions, AI review, answers, signing
  /api/v1/...      the JSON API for other systems, with an API key (web/api.py; reference at /docs)
  /healthz         for load balancers and container health checks

The pages have no login of their own: put them behind your SSO or reverse proxy
(see the README). The API has its own keys.
"""

from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from starlette.concurrency import run_in_threadpool

from legal_agent import config, demo, desk, negotiation, store
from legal_agent.desk import SIGNATURE_STYLES, DeskError
from legal_agent.docx_parser import parse_docx
from legal_agent.signing import SIGNATURE_FONTS_DIR
from legal_agent.web import api
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

app = FastAPI(title="Acme Agreement Desk API", version="1.0",
              description="Contracts in, AI review results and events out. Every call needs an `X-API-Key` header. "
                          "Signing is not in the API on purpose: only a person signs, in the web app.")
app.include_router(api.router)
app.mount("/fonts", StaticFiles(directory=SIGNATURE_FONTS_DIR), name="fonts")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.globals["profile"] = lambda: _profile()  # the top bar shows who is signed in
page = {"include_in_schema": False}  # the pages stay out of the API reference


@app.middleware("http")
async def visitor_copy(request: Request, call_next):
    """In the public demo, each browser works on its own copy of the portfolio (see demo.py).
    Health checks, fonts and the API use the main database, so they never create a copy."""
    if not config.demo_mode() or request.url.path.startswith(("/healthz", "/fonts", "/api", "/docs", "/openapi")):
        return await call_next(request)
    db_file, cookie = await run_in_threadpool(demo.visitor_db, request.cookies.get(demo.COOKIE, ""))
    token = store.use_db(db_file)
    try:
        response = await call_next(request)
    finally:
        store.reset_db(token)
    if cookie != request.cookies.get(demo.COOKIE):
        response.set_cookie(demo.COOKIE, cookie, max_age=demo.KEEP_DAYS * 86400, httponly=True, samesite="lax",
                            secure=request.url.scheme == "https")
    return response


@app.exception_handler(DeskError)
def refused(request: Request, error: DeskError):
    return JSONResponse({"detail": error.message}, status_code=error.status)


@app.get("/healthz", tags=["health"])
def health() -> dict:
    """The database answers. No key needed, no data returned."""
    with store.connect() as db:
        db.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/", **page)
def index(request: Request, error: str = ""):
    with store.connect() as db:
        agreements = [store.get_agreement(db, r["id"]) for r in store.list_agreements(db)]
    rows = sorted((desk.summary(a) for a in agreements), key=lambda r: r["updated_at"] or "", reverse=True)
    sections = [(status, title, note, [r for r in rows if r["status"] == status])
                for status, (title, note) in SECTIONS.items()]
    return templates.TemplateResponse(request, "index.html", {
        "sections": [s for s in sections if s[3]], "total": len(rows), "error": error})


@app.post("/agreements", **page)
def create_agreement(title: str = Form(""), counterparty: str = Form(""), review: str = Form(""),
                     ours: UploadFile = File(...), theirs: UploadFile | None = File(None),
                     related: UploadFile | None = File(None)):
    if not ours.filename:
        return RedirectResponse("/?error=" + quote("Upload our version as a .docx."), status_code=303)
    try:
        agreement_id = desk.create(
            title.strip() or Path(ours.filename).stem, counterparty.strip() or "Counterparty",
            (desk.save_upload(ours.file, ours.filename), ours.filename),
            (desk.save_upload(theirs.file, theirs.filename), theirs.filename) if theirs and theirs.filename else None,
            desk.save_upload(related.file, related.filename) if related and related.filename else None,
            review=bool(review))
    except DeskError as error:
        return RedirectResponse("/?error=" + quote(error.message), status_code=303)
    return RedirectResponse(f"/agreements/{agreement_id}", status_code=303)


@app.post("/agreements/{agreement_id}/versions", **page)
def upload_reply(agreement_id: int, new: UploadFile = File(...)):
    desk.receive_reply(agreement_id, desk.save_upload(new.file, new.filename), new.filename)
    return RedirectResponse(f"/agreements/{agreement_id}", status_code=303)


@app.post("/agreements/{agreement_id}/accepted", **page)
def record_acceptance(agreement_id: int):
    desk.record_acceptance(agreement_id)
    return RedirectResponse(f"/agreements/{agreement_id}", status_code=303)


@app.post("/rounds/{round_id}/review", **page)
def review_round(round_id: int):
    desk.start_review(round_id)
    agreement, rnd = desk.round_of(round_id)
    return RedirectResponse(f"/agreements/{agreement['id']}#v{rnd['new_version']}", status_code=303)


@app.get("/agreements/{agreement_id}", **page)
def show_agreement(request: Request, agreement_id: int):
    agreement = desk.load(agreement_id)
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


@app.post("/rounds/{round_id}/items/{change_id}", **page)
def answer_change(round_id: int, change_id: int, actor: str = Form(...), outcome: str = Form(""),
                  counter_text: str = Form(""), internal_comment: str = Form(""), external_comment: str = Form("")):
    desk.answer(round_id, change_id, actor, outcome, counter_text, internal_comment, external_comment)
    agreement, _ = desk.round_of(round_id)
    response = RedirectResponse(f"/agreements/{agreement['id']}#change-{round_id}-{change_id}", status_code=303)
    response.set_cookie("reviewer", actor.strip())
    return response


@app.post("/rounds/{round_id}/close", **page)
def close_round(round_id: int, actor: str = Form(...)):
    agreement, _ = desk.round_of(round_id)
    number = desk.close(round_id, actor)
    return RedirectResponse(f"/agreements/{agreement['id']}#{f'v{number}' if number else 'sign'}", status_code=303)


@app.post("/agreements/{agreement_id}/sign", **page)
def sign(agreement_id: int, name: str = Form(""), job_title: str = Form(""), style: str = Form(SIGNATURE_STYLES[0]),
         consent: str = Form(""), remember: str = Form("")):
    """Only a person signs, here in the web app. The API has no signing call."""
    desk.sign(agreement_id, name, job_title, style, bool(consent), bool(remember))
    return RedirectResponse(f"/agreements/{agreement_id}#signed", status_code=303)


@app.get("/profile", **page)
def show_profile(request: Request, saved: bool = False):
    return templates.TemplateResponse(request, "profile.html", {"styles": SIGNATURE_STYLES, "saved": saved})


@app.post("/profile", **page)
def update_profile(name: str = Form(...), job_title: str = Form(""), style: str = Form(SIGNATURE_STYLES[0])):
    """Adopt a signature once; every sign form then starts from it."""
    desk.require(bool(name.strip()), "Type your full name.")
    desk.require(style in SIGNATURE_STYLES, "Unknown signature style.")
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


@app.get("/agreements/{agreement_id}/signed.pdf", **page)
def signed_pdf(agreement_id: int):
    agreement = desk.load(agreement_id)
    desk.require(bool(agreement["signed_pdf"]), "Not signed yet.", 404)
    return FileResponse(agreement["signed_pdf"], filename=f"{desk.slug(agreement['title'])}_signed.pdf")


@app.get("/agreements/{agreement_id}/versions/{number}", **page)
def show_version(request: Request, agreement_id: int, number: int):
    agreement = desk.load(agreement_id)
    version = desk.version(agreement, number)
    changed = {c["clause_id"] for r in agreement["rounds"].values() for c in r["changes"]
               if number in (r["old_version"], r["new_version"])}
    return templates.TemplateResponse(request, "document.html", {
        "doc": document(version["file"], version["name"]), "label": f"v{number}, from {version['author']}",
        "changed": changed, "back": (f"/agreements/{agreement_id}", agreement["title"]),
        "download": f"/agreements/{agreement_id}/versions/{number}/download"})


@app.get("/agreements/{agreement_id}/versions/{number}/download", **page)
def download_version(agreement_id: int, number: int):
    version = desk.version(desk.load(agreement_id), number)
    return FileResponse(version["file"], filename=version["name"])


@app.get("/agreements/{agreement_id}/rounds/{round_id}/diff", **page)
def show_diff(request: Request, agreement_id: int, round_id: int):
    """The whole document as a git-style diff: changed clauses in full, unchanged ones folded."""
    agreement, rnd = desk.round_of(round_id)
    desk.require(agreement["id"] == agreement_id, "Wrong agreement.")
    old, new = agreement["versions"][rnd["old_version"]], agreement["versions"][rnd["new_version"]]
    changed = {c["clause_id"]: c for c in rnd["changes"]}
    removed = [c for c in rnd["changes"] if c["kind"] == "removed"]
    rows = [{"clause": c, "change": changed.get(c.id)} for c in parse_docx(new["file"])]
    return templates.TemplateResponse(request, "diff.html", {
        "agreement": agreement, "old": old, "new": new, "rows": rows, "removed": removed})


@app.get("/agreements/{agreement_id}/progress", **page)
def review_progress(agreement_id: int) -> dict:
    """What the page polls while the AI works: reload when busy turns false or the step changes."""
    agreement = desk.load(agreement_id)
    stages = [r["stage"] for r in agreement["rounds"].values() if r["status"] == "reviewing"]
    return {"busy": agreement["status"] == "reviewing", "token": stages[-1] if stages else ""}
