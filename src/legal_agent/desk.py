"""The negotiation workflow, shared by the web pages and the JSON API.

Flow for each version the counterparty sends:
  1. the clause diff against the version we sent is computed at once (plain code, no AI);
  2. the AI review runs in a background thread;
  3. a person answers each change (accept, reject, counter) and closes the round;
  4. if everything was accepted the agreement is ready to sign, otherwise our next
     version is built from the answers and goes back unsigned.
Only a person signs (sign() is called from the web page, never from the API or the AI).
Every step lands in the audit trail, and every audit-trail entry is also a webhook event.
"""

import shutil
import tempfile
import zipfile
from contextvars import copy_context
from dataclasses import asdict
from pathlib import Path
from threading import Thread
from typing import BinaryIO

from legal_agent import config, demo, negotiation, store
from legal_agent.clause_diff import Change, diff_clauses
from legal_agent.docx_parser import parse_docx
from legal_agent.docx_writer import write_docx
from legal_agent.orchestrator import Usage, review_contract
from legal_agent.policy import load_policy, red_line_hits, rules_for
from legal_agent.signing import SIGNATURE_FONTS, SIGNER

SIGNATURE_STYLES = tuple(SIGNATURE_FONTS)


class DeskError(Exception):
    """A request the workflow refuses; `status` is the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


def require(condition: bool, message: str, status: int = 400):
    if not condition:
        raise DeskError(message, status)


# Reading

def load(agreement_id: int) -> dict:
    with store.connect() as db:
        agreement = store.get_agreement(db, agreement_id)
    require(agreement is not None, "No agreement with this id.", 404)
    return agreement


def round_of(round_id: int) -> tuple[dict, dict]:
    with store.connect() as db:
        row = db.execute("SELECT agreement_id FROM rounds WHERE id = ?", (round_id,)).fetchone()
    require(row is not None, "No round with this id.", 404)
    agreement = load(row[0])
    return agreement, agreement["rounds"][round_id]


def latest(agreement: dict) -> dict:
    return agreement["versions"][max(agreement["versions"])]


def version(agreement: dict, number: int) -> dict:
    require(number in agreement["versions"], "No such version.", 404)
    return agreement["versions"][number]


def summary(a: dict) -> dict:
    """Where an agreement stands and what to do next: one line of the contract list."""
    rnd = list(a["rounds"].values())[-1] if a["rounds"] else None
    tally = {}
    if rnd and rnd["result"]:
        for key in ("escalate", "push_back", "accept"):
            tally[key] = sum(1 for c in rnd["result"]["changes"] if c["decision"] == key)
    status = a["status"]
    if status == "signed":
        signed = next(e for e in reversed(a["events"]) if e["action"] == "sign")
        step = f"Signed v{signed['version']} on {signed['at'][:10]} by {signed['actor']}"
    elif status == "ready to sign":
        step = f"Sign v{latest(a)['number']}"
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
        step = f"Waiting for {a['counterparty']} to reply to v{latest(a)['number']}"
    return {"id": a["id"], "title": a["title"], "counterparty": a["counterparty"], "doc_type": a["doc_type"],
            "status": status, "versions": len(a["versions"]), "tally": tally, "next_step": step,
            "review_round": rnd["id"] if status == "not reviewed" else None,
            "updated_at": a["events"][-1]["at"] if a["events"] else a["created_at"]}


# Versions in

def save_upload(stream: BinaryIO, filename: str) -> Path:
    """Keep an uploaded .docx in a temporary file after checking it is one and is not too big.
    store.add_version copies it into the agreement's folder."""
    require(filename.lower().endswith(".docx"), f"{filename}: only .docx files are accepted.", 415)
    limit = config.max_upload_bytes()
    path = Path(tempfile.mkdtemp()) / "upload.docx"
    with path.open("wb") as out:
        size = 0
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                shutil.rmtree(path.parent)
                raise DeskError(f"{filename} is larger than {limit // (1024 * 1024)} MB.", 413)
            out.write(chunk)
    is_docx = zipfile.is_zipfile(path) and "word/document.xml" in zipfile.ZipFile(path).namelist()
    if not is_docx:
        shutil.rmtree(path.parent)
        raise DeskError(f"{filename} is not a Word .docx file.", 415)
    return path


def create(title: str, counterparty: str, ours: tuple[Path, str], theirs: tuple[Path, str] | None = None,
           related: Path | None = None, review: bool = True) -> int:
    """A new agreement: our version (v1) and, if they already replied, their version (v2).
    `related` is a contract this one refers to (an SOW's MSA), so the AI can read those clauses."""
    with store.connect() as db:
        agreement_id = store.create_agreement(db, title, counterparty, related)
        store.add_version(db, agreement_id, store.ACME, *ours)
        if theirs:
            receive(db, agreement_id, *theirs, review=review)
    return agreement_id


def receive_reply(agreement_id: int, path: Path, name: str, review: bool = True):
    """The counterparty's next version."""
    require(load(agreement_id)["status"] == "waiting for counterparty", "We are not waiting for a new version.")
    with store.connect() as db:
        receive(db, agreement_id, path, name, review)


def record_acceptance(agreement_id: int):
    """They accepted our last version as it is (say, by email): log it as their next version, unchanged."""
    agreement = load(agreement_id)
    require(agreement["status"] == "waiting for counterparty", "We are not waiting for a new version.")
    ours = latest(agreement)
    name = f"{agreement['counterparty'].split()[0]}_{slug(agreement['title'])}_v{ours['number'] + 1}.docx"
    with store.connect() as db:
        receive(db, agreement_id, ours["file"], name)


def receive(db, agreement_id: int, path: Path, name: str, review: bool = True):
    """Log their version and its diff against what we sent; start the AI review unless asked to wait."""
    agreement = store.get_agreement(db, agreement_id)
    ours = latest(agreement)
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


# The AI review

def start_review(round_id: int):
    """Run the AI review a person asked for, or try again after one stopped."""
    _, rnd = round_of(round_id)
    require(rnd["status"] in ("not reviewed", "failed"), "This round is already reviewed.")
    with store.connect() as db:
        _start_review(db, round_id)


def _start_review(db, round_id: int):
    agreement, rnd = round_of(round_id)
    versions = agreement["versions"]
    related = Path(agreement["related_file"]) if agreement["related_file"] else None
    store.start_review(db, round_id)
    # Run in a copy of this request's context, so the thread writes to the same visitor's database.
    Thread(target=copy_context().run, args=(run_review, round_id, versions[rnd["old_version"]]["file"],
                                            versions[rnd["new_version"]]["file"], related), daemon=True).start()


def run_review(round_id: int, old_path: str, new_path: str, related: Path | None):
    """Runs in a background thread. Any failure (including no `claude` CLI) is recorded, never hidden."""
    with store.connect() as db:
        progress = lambda step: store.set_stage(db, round_id, step)  # noqa: E731
        try:
            if config.demo_mode():
                result = demo.replay_review(old_path, new_path, progress)
            else:
                result = asdict(review_contract(old_path, new_path, {"msa": related} if related else None,
                                                on_progress=progress))
            store.finish_round(db, round_id, result)
        except Exception as error:  # the page must show why the review stopped
            store.fail_round(db, round_id, str(error))


# A person's answers

def answer(round_id: int, change_id: int, actor: str, outcome: str = "", counter_text: str = "",
           internal_comment: str = "", external_comment: str = ""):
    """A person's answer to one change. Saved as they go; the round closes separately."""
    _, rnd = round_of(round_id)
    require(rnd["status"] == "open", "This round is not open.")
    require(outcome in ("", *negotiation.OUTCOMES), "Unknown outcome.")
    require(any(c["id"] == change_id for c in rnd["changes"]), "No change with this id in the round.", 404)
    with store.connect() as db:
        store.save_item(db, round_id, change_id, {
            "outcome": outcome or None, "counter_text": counter_text.strip(), "internal_comment": internal_comment.strip(),
            "external_comment": external_comment.strip()}, actor.strip() or "Reviewer")


def close(round_id: int, actor: str) -> int | None:
    """All changes answered: ready to sign (returns None), or our next version goes back (returns its number)."""
    agreement, rnd = round_of(round_id)
    require(rnd["status"] == "open", "This round is not open.")
    reviewed = rnd["result"]["changes"]
    decisions = {r["change"]["id"]: r["decision"] for r in reviewed}
    open_items = [cid for cid, decision in decisions.items() if negotiation.missing(decision, rnd["answers"].get(cid))]
    require(not open_items, "Some changes still need an answer.")
    outcomes = {cid: (negotiation.outcome_of(decision, rnd["answers"].get(cid)),
                      (rnd["answers"].get(cid) or {}).get("counter_text") or "") for cid, decision in decisions.items()}
    counts = {o: sum(1 for oc, _ in outcomes.values() if oc == o) for o in negotiation.OUTCOMES}
    summary_line = ", ".join(f"{n} {o}ed" for o, n in counts.items() if n)

    with store.connect() as db:
        store.close_round(db, round_id, actor.strip() or "Reviewer", summary_line)
        if counts["accept"] == len(outcomes):
            store.set_status(db, agreement["id"], "ready to sign")
            return None
        old, new = agreement["versions"][rnd["old_version"]], agreement["versions"][rnd["new_version"]]
        changes = [Change(**r["change"]) for r in reviewed]
        clauses = negotiation.next_version(parse_docx(old["file"]), parse_docx(new["file"]), changes, outcomes)
        number = new["number"] + 1
        note = negotiation.cover_note(agreement["counterparty"], new["number"], number, changes,
                                      {cid: dict(i) for cid, i in rnd["answers"].items()}, decisions)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "next.docx"
            write_docx(clauses, path, agreement["title"])
            store.add_version(db, agreement["id"], store.ACME, path, f"Acme_{slug(agreement['title'])}_v{number}.docx", note)
        store.set_status(db, agreement["id"], "waiting for counterparty")
    return number


# Signing: a person only

def sign(agreement_id: int, name: str, job_title: str, style: str, consent: bool, remember: bool = False):
    """Only a person signs, and only the version both sides agreed. Neither the AI nor the API calls this."""
    agreement = load(agreement_id)
    require(agreement["status"] == "ready to sign", "This agreement is not ready to sign.")
    name, job_title = name.strip(), job_title.strip()
    require(bool(consent and name), "To sign, tick the consent box and type your full name.")
    require(style in SIGNATURE_STYLES, "Unknown signature style.")
    if remember:
        with store.connect() as db:
            store.save_profile(db, name, job_title, style)
    final = latest(agreement)
    require(store.sha256_of(final["file"]) == final["sha256"],
            "The document changed after it was agreed. It cannot be signed.", 409)
    rules = rules_for(load_policy(), agreement["doc_type"] or "")
    hits = red_line_hits(rules, "\n".join(c.text for c in parse_docx(final["file"])))
    final_check = f"red lines found: {', '.join(hits)}" if hits else "no red lines in the final text"
    certificate = {
        "document": f"{agreement['title']} between {store.ACME} and {agreement['counterparty']}",
        "signature": {"name": name, "job_title": job_title, "style": style},
        "fields": {"Agreement": agreement["title"], "Counterparty": agreement["counterparty"],
                   "Signed version": f"v{final['number']} ({final['name']})", "SHA-256 of signed file": final["sha256"],
                   "Signed by": f"{name}{', ' + job_title if job_title else ''} for {store.ACME},"
                                f" typed electronic signature ({style} style) with consent",
                   "Signed at": store.now(), "Final playbook check": final_check},
        "versions": [f"v{v['number']}  {v['created_at']}  from {v['author']}  {v['name']}  SHA-256 {v['sha256']}"
                     for v in agreement["versions"].values()]}
    pdf = SIGNER.sign(Path(final["file"]), store.files_dir(agreement_id) / "signed.pdf", certificate)
    with store.connect() as db:
        store.set_signed(db, agreement_id, pdf)
        store.add_event(db, agreement_id, "sign", name,
                        f"Signed v{final['number']} ({final['name']}), SHA-256 {final['sha256']}. Final check: {final_check}.",
                        version=final["number"])


def slug(title: str) -> str:
    return "-".join(title.split())
