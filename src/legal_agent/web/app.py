"""The agreement desk web app: receive a redline, let the AI review it, then a person decides.

Pages are plain HTML from Jinja templates (no frontend framework). The same
data is available as JSON under /api for other systems.

Flow: POST /reviews stores both versions and starts the AI review in a background
thread. The agreement page refreshes itself while the review runs, showing each step,
then shows the result and the decision form. Every step lands in the audit trail.
"""

import shutil
from dataclasses import asdict
from pathlib import Path
from threading import Thread
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from legal_agent import store
from legal_agent.orchestrator import review_contract
from legal_agent.web.sources import document, review_sources

ROOT = Path(__file__).resolve().parents[3]
UPLOADS = ROOT / "data" / "app" / "uploads"
CONTRACTS = ROOT / "data" / "contracts"

# Ready-made redlines so the demo works without preparing any files.
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
VERSIONS = ("v1", "v2")
STATUS_FOR_ACTION = {"sign": "signed", "send_back": "sent back", "escalate": "escalated"}

app = FastAPI(title="Acme Agreement Desk")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


@app.get("/")
def index(request: Request, error: str = ""):
    with store.connect() as db:
        reviews = store.list_reviews(db)
    return templates.TemplateResponse(request, "index.html", {"reviews": reviews, "demos": DEMOS, "error": error})


@app.post("/reviews")
def create_review(demo: str = Form(""), title: str = Form(""), counterparty: str = Form(""),
                  old: UploadFile | None = File(None), new: UploadFile | None = File(None)):
    """Store both versions, start the AI review in the background, and open the agreement page."""
    if demo in DEMOS:
        spec = DEMOS[demo]
        (old_rel, old_name), (new_rel, new_name) = spec["v1"], spec["v2"]
        old_path, new_path = CONTRACTS / old_rel, CONTRACTS / new_rel
        related = CONTRACTS / spec["related"] if "related" in spec else None
        title, counterparty = spec["title"], spec["counterparty"]
    elif old and new and old.filename and new.filename:
        folder = UPLOADS / store.now().replace(" ", "_").replace(":", "-")
        folder.mkdir(parents=True, exist_ok=True)
        old_path, new_path = folder / "v1.docx", folder / "v2.docx"
        for upload, path in ((old, old_path), (new, new_path)):
            with path.open("wb") as f:
                shutil.copyfileobj(upload.file, f)
        old_name, new_name, related = old.filename, new.filename, None
        title, counterparty = title or Path(new.filename).stem, counterparty or "Counterparty"
    else:
        return RedirectResponse("/?error=" + quote("Pick a demo, or upload both versions as .docx."), status_code=303)

    with store.connect() as db:
        review_id = store.create_review(db, title, counterparty, old_path, old_name, new_path, new_name, related)
    Thread(target=run_review, args=(review_id, old_path, new_path, related), daemon=True).start()
    return RedirectResponse(f"/reviews/{review_id}", status_code=303)


def run_review(review_id: int, old_path: Path, new_path: Path, related: Path | None):
    """Runs in a background thread. Any failure (including no `claude` CLI) is recorded, never hidden."""
    with store.connect() as db:
        try:
            result = review_contract(old_path, new_path, {"msa": related} if related else None,
                                     on_progress=lambda step: store.set_stage(db, review_id, step))
            store.finish_review(db, review_id, asdict(result))
        except Exception as error:  # the page must show why the review stopped
            store.fail_review(db, review_id, str(error))


@app.get("/reviews/{review_id}")
def show_review(request: Request, review_id: int):
    review = _load(review_id)
    sources = review_sources(review) if review["result"] else {"docs": {}, "sources": {}}
    return templates.TemplateResponse(request, "review.html", {"review": review, "sources": sources})


@app.post("/reviews/{review_id}/decision")
def decide(review_id: int, action: str = Form(...), name: str = Form(""), consent: str = Form(""),
           note: str = Form("")):
    """The human's decision. Only a person can sign: the AI never calls this."""
    review = _load(review_id)
    if action not in STATUS_FOR_ACTION or review["status"] != "awaiting decision":
        raise HTTPException(400, "This agreement is not waiting for a decision.")
    if action == "sign":
        if not (consent and name.strip()):
            raise HTTPException(400, "To sign, tick the consent box and type your full name.")
        if store.sha256_of(review["new_file"]) != review["new_sha256"]:
            raise HTTPException(409, "The document changed after it was reviewed. Review it again before signing.")
        note = f"Signed {review['new_name']} as \"{name.strip()}\". Document SHA-256 {review['new_sha256']}."
    with store.connect() as db:
        store.set_status(db, review_id, STATUS_FOR_ACTION[action])
        store.add_event(db, review_id, action, name.strip() or "Reviewer", note)
    return RedirectResponse(f"/reviews/{review_id}", status_code=303)


@app.get("/reviews/{review_id}/documents/{version}")
def show_document(request: Request, review_id: int, version: str):
    review = _load(review_id)
    path, name = _version(review, version)
    changed = {r["change"]["clause_id"] for r in (review["result"] or {}).get("changes", [])}
    return templates.TemplateResponse(request, "document.html", {
        "doc": document(path, name), "version": version, "changed": changed,
        "back": (f"/reviews/{review_id}", review["title"]),
        "download": f"/reviews/{review_id}/documents/{version}/download"})


@app.get("/reviews/{review_id}/documents/{version}/download")
def download_document(review_id: int, version: str):
    path, name = _version(_load(review_id), version)
    return FileResponse(path, filename=name)


@app.get("/demos/{demo}/{version}")
def show_demo_document(request: Request, demo: str, version: str):
    path, name = _demo_version(demo, version)
    return templates.TemplateResponse(request, "document.html", {
        "doc": document(path, name), "version": version, "changed": set(), "back": ("/", "Agreements"),
        "download": f"/demos/{demo}/{version}/download"})


@app.get("/demos/{demo}/{version}/download")
def download_demo_document(demo: str, version: str):
    path, name = _demo_version(demo, version)
    return FileResponse(path, filename=name)


@app.get("/api/reviews/{review_id}")
def review_json(review_id: int) -> dict:
    return _load(review_id)


def _load(review_id: int) -> dict:
    with store.connect() as db:
        review = store.get_review(db, review_id)
    if review is None:
        raise HTTPException(404, "No agreement with this id.")
    return review


def _version(review: dict, version: str) -> tuple[str, str]:
    if version not in VERSIONS:
        raise HTTPException(404, "Versions are v1 and v2.")
    return (review["old_file"], review["old_name"]) if version == "v1" else (review["new_file"], review["new_name"])


def _demo_version(demo: str, version: str) -> tuple[Path, str]:
    if demo not in DEMOS or version not in VERSIONS:
        raise HTTPException(404, "No such demo document.")
    rel, name = DEMOS[demo][version]
    return CONTRACTS / rel, name
