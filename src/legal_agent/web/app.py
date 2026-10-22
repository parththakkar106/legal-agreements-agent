"""The agreement desk web app: upload a redline, read the AI review, then a human decides.

Pages are plain HTML from Jinja templates (no frontend framework). The same
data is available as JSON under /api for other systems.
"""

import shutil
from urllib.parse import quote
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from legal_agent import store
from legal_agent.llm import LLMUnavailable
from legal_agent.orchestrator import review_contract

ROOT = Path(__file__).resolve().parents[3]
UPLOADS = ROOT / "data" / "app" / "uploads"
CONTRACTS = ROOT / "data" / "contracts"

# Ready-made redlines so the demo works without preparing any files.
DEMOS = {
    "nda": ("Globex: Mutual NDA", "base/nda.docx", "redlines/nda_globex_v2.docx", None),
    "msa": ("Globex: Cloud Service Agreement", "base/msa.docx", "redlines/msa_globex_v2.docx", None),
    "dpa": ("Globex: Data Processing Agreement", "base/dpa.docx", "redlines/dpa_globex_v2.docx", None),
    "sow": ("Globex: Onboarding SOW", "base/sow.docx", "redlines/sow_globex_v2.docx", "base/msa.docx"),
}

STATUS_FOR_ACTION = {"sign": "signed", "send_back": "sent back", "escalate": "escalated"}

app = FastAPI(title="Acme Agreement Desk")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


@app.get("/")
def index(request: Request, error: str = ""):
    with store.connect() as db:
        reviews = store.list_reviews(db)
    return templates.TemplateResponse(request, "index.html", {"reviews": reviews, "demos": DEMOS, "error": error})


@app.post("/reviews")
def create_review(demo: str = Form(""), title: str = Form(""),
                  old: UploadFile | None = File(None), new: UploadFile | None = File(None)):
    """Run the AI review on a demo redline or on two uploaded .docx files."""
    if demo in DEMOS:
        title, old_rel, new_rel, related_rel = DEMOS[demo]
        old_path, new_path = CONTRACTS / old_rel, CONTRACTS / new_rel
        related = {"msa": CONTRACTS / related_rel} if related_rel else None
    elif old and new and old.filename and new.filename:
        folder = UPLOADS / store.now().replace(" ", "_").replace(":", "-")
        folder.mkdir(parents=True, exist_ok=True)
        old_path, new_path = folder / "old.docx", folder / "new.docx"
        for upload, path in ((old, old_path), (new, new_path)):
            with path.open("wb") as f:
                shutil.copyfileobj(upload.file, f)
        title = title or new.filename
        related = None
    else:
        return RedirectResponse("/?error=Pick a demo or upload both versions as .docx.", status_code=303)

    try:
        result = asdict(review_contract(old_path, new_path, related))
    except LLMUnavailable as error:
        return RedirectResponse(f"/?error={quote(str(error))}", status_code=303)
    with store.connect() as db:
        review_id = store.save_review(db, title, new_path, result)
    return RedirectResponse(f"/reviews/{review_id}", status_code=303)


@app.get("/reviews/{review_id}")
def show_review(request: Request, review_id: int):
    return templates.TemplateResponse(request, "review.html", {"review": _load(review_id)})


@app.post("/reviews/{review_id}/decision")
def decide(review_id: int, action: str = Form(...), name: str = Form(""), consent: str = Form(""),
           note: str = Form("")):
    """The human's decision. Only a person can sign: the AI never calls this."""
    review = _load(review_id)
    if action not in STATUS_FOR_ACTION or review["status"] != "awaiting decision":
        raise HTTPException(400, "This review is not waiting for a decision.")
    if action == "sign":
        if not (consent and name.strip()):
            raise HTTPException(400, "To sign, tick the consent box and type your full name.")
        if store.sha256_of(review["new_file"]) != review["new_sha256"]:
            raise HTTPException(409, "The document changed after it was reviewed. Review it again before signing.")
        note = f"Signed as \"{name.strip()}\". Document SHA-256 {review['new_sha256']}."
    with store.connect() as db:
        store.set_status(db, review_id, STATUS_FOR_ACTION[action])
        store.add_event(db, review_id, action, name.strip() or "reviewer", note)
    return RedirectResponse(f"/reviews/{review_id}", status_code=303)


@app.get("/api/reviews/{review_id}")
def review_json(review_id: int) -> dict:
    return _load(review_id)


def _load(review_id: int) -> dict:
    with store.connect() as db:
        review = store.get_review(db, review_id)
    if review is None:
        raise HTTPException(404, "No review with this id.")
    return review
