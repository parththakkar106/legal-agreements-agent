"""The JSON API for other systems: a CRM, an email intake, Slack, a document store.

Every call needs an `X-API-Key` header with a key from DESK_API_KEYS; with no keys set
the API is off (503). Keys are compared in constant time, and the key's name is recorded
in the audit trail next to the person the call acts for ("Ana Ruiz via crm").

What a system can do: add agreements and versions, start the AI review, read the results,
record a person's answers, close a round, and download files. What it cannot do: sign.
Signing stays with a person in the web app. To hear what happens, subscribe to webhooks
(webhooks.py) instead of polling.

The reference, with every field, is at /docs.
"""

import hmac

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from legal_agent import config, desk, store


def caller(x_api_key: str = Header("", description="One of the keys in DESK_API_KEYS")) -> str:
    """The name of the system calling, from its API key."""
    keys = config.api_keys()
    if not keys:
        raise HTTPException(503, "The API is off: set DESK_API_KEYS.")
    for key, name in keys.items():
        if hmac.compare_digest(x_api_key.encode(), key.encode()):
            return name
    raise HTTPException(401, "Missing or wrong X-API-Key.", headers={"WWW-Authenticate": "ApiKey"})


router = APIRouter(prefix="/api/v1", tags=["agreements"], dependencies=[Depends(caller)])


class Answer(BaseModel):
    change_id: int
    actor: str  # the person this answer is from
    outcome: str = ""  # accept | reject | counter; "" leaves a green change accepted
    counter_text: str = ""
    internal_comment: str = ""  # stays in our audit trail
    external_comment: str = ""  # goes in the cover note to the counterparty


class Close(BaseModel):
    actor: str


@router.get("/agreements")
def list_agreements() -> list[dict]:
    """Every agreement with its status and next step, newest activity first."""
    with store.connect() as db:
        agreements = [store.get_agreement(db, r["id"]) for r in store.list_agreements(db)]
    rows = [desk.summary(a) for a in agreements]
    return sorted(({k: v for k, v in r.items() if k != "review_round"} for r in rows),
                  key=lambda r: r["updated_at"] or "", reverse=True)


@router.post("/agreements", status_code=201)
def create_agreement(title: str = Form(...), counterparty: str = Form(...), ours: UploadFile = File(...),
                     theirs: UploadFile | None = File(None), related: UploadFile | None = File(None),
                     review: bool = Form(True)) -> dict:
    """Our version (v1) and, if they replied, theirs (v2). With theirs, the AI review starts unless review=false."""
    agreement_id = desk.create(
        title.strip(), counterparty.strip(), (desk.save_upload(ours.file, ours.filename), ours.filename),
        (desk.save_upload(theirs.file, theirs.filename), theirs.filename) if theirs and theirs.filename else None,
        desk.save_upload(related.file, related.filename) if related and related.filename else None, review)
    return public(desk.load(agreement_id))


@router.get("/agreements/{agreement_id}")
def get_agreement(agreement_id: int) -> dict:
    """Versions, every round with each change, the AI's finding and a person's answer, and the audit trail."""
    return public(desk.load(agreement_id))


@router.post("/agreements/{agreement_id}/versions", status_code=201)
def add_version(agreement_id: int, file: UploadFile = File(...), review: bool = Form(True)) -> dict:
    """The counterparty's next version. The AI review starts unless review=false."""
    desk.receive_reply(agreement_id, desk.save_upload(file.file, file.filename), file.filename, review)
    return public(desk.load(agreement_id))


@router.post("/agreements/{agreement_id}/accepted")
def record_acceptance(agreement_id: int) -> dict:
    """The counterparty accepted our last version as it is: the agreement becomes ready to sign."""
    desk.record_acceptance(agreement_id)
    return public(desk.load(agreement_id))


@router.post("/rounds/{round_id}/review", status_code=202)
def start_review(round_id: int) -> dict:
    """Start (or retry) the AI review. It runs in the background: wait for the review.completed webhook."""
    desk.start_review(round_id)
    return {"round_id": round_id, "status": "reviewing"}


@router.post("/rounds/{round_id}/answers")
def answer(round_id: int, body: Answer, system: str = Depends(caller)) -> dict:
    desk.answer(round_id, body.change_id, f"{body.actor.strip()} via {system}", body.outcome, body.counter_text,
                body.internal_comment, body.external_comment)
    agreement, _ = desk.round_of(round_id)
    return public(agreement)


@router.post("/rounds/{round_id}/close")
def close_round(round_id: int, body: Close, system: str = Depends(caller)) -> dict:
    """Ready to sign if every change was accepted; otherwise our next version is written and goes back."""
    agreement, _ = desk.round_of(round_id)
    desk.close(round_id, f"{body.actor.strip()} via {system}")
    return public(desk.load(agreement["id"]))


@router.get("/agreements/{agreement_id}/versions/{number}/download")
def download_version(agreement_id: int, number: int):
    version = desk.version(desk.load(agreement_id), number)
    return FileResponse(version["file"], filename=version["name"])


@router.get("/agreements/{agreement_id}/signed.pdf")
def signed_pdf(agreement_id: int):
    agreement = desk.load(agreement_id)
    desk.require(bool(agreement["signed_pdf"]), "Not signed yet.", 404)
    return FileResponse(agreement["signed_pdf"], filename=f"{desk.slug(agreement['title'])}_signed.pdf")


def public(a: dict) -> dict:
    """An agreement as other systems see it: links instead of server file paths."""
    base = f"/api/v1/agreements/{a['id']}"
    rounds = []
    for r in a["rounds"].values():
        reviewed = {c["change"]["id"]: c for c in (r["result"] or {}).get("changes", [])}
        changes = []
        for c in r["changes"]:
            finding = reviewed.get(c["id"], {})
            changes.append({
                "id": c["id"], "clause": c["clause_id"], "title": c["title"], "kind": c["kind"],
                "old_text": c["old_text"], "new_text": c["new_text"],
                "decision": finding.get("decision"), "ai_decision": finding.get("ai_decision"),
                "meaning": finding.get("meaning"), "impact": finding.get("impact"),
                "suggested_reply": finding.get("counter_proposal"), "rule_ids": finding.get("rule_ids", []),
                "gdpr_citations": finding.get("gdpr_citations", []), "confidence": finding.get("confidence"),
                "guardrail_notes": finding.get("guardrail_notes", []),
                "answer": {k: v for k, v in (r["answers"].get(c["id"]) or {}).items()
                           if k in ("outcome", "counter_text", "internal_comment", "external_comment", "actor", "at")}
                          or None})
        rounds.append({"id": r["id"], "old_version": r["old_version"], "new_version": r["new_version"],
                       "status": r["status"], "stage": r["stage"],
                       "verdict": (r["result"] or {}).get("verdict"), "groups": (r["result"] or {}).get("groups", []),
                       "changes": changes})
    return {
        "id": a["id"], "title": a["title"], "counterparty": a["counterparty"], "doc_type": a["doc_type"],
        "status": a["status"], "next_step": desk.summary(a)["next_step"], "created_at": a["created_at"],
        "versions": [{"number": v["number"], "author": v["author"], "name": v["name"], "sha256": v["sha256"],
                      "created_at": v["created_at"], "cover_note": v["note"] or None,
                      "download": f"{base}/versions/{v['number']}/download"} for v in a["versions"].values()],
        "rounds": rounds,
        "signed_pdf": f"{base}/signed.pdf" if a["signed_pdf"] else None,
        "audit_trail": [{"at": e["at"], "action": e["action"], "actor": e["actor"], "detail": e["detail"]}
                        for e in a["events"]],
    }
