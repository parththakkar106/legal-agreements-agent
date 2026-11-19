"""Acme's contract portfolio: the agreements the app starts with.

The portfolio is built once by `scripts/build_portfolio.py`, which plays each
negotiation through the real app: uploads, live AI reviews (`claude -p`), a
person's answers, the counterparty's replies, signing. Only the human answers and
the counterparty's replies are scripted. The result is saved in `data/portfolio/`
and loaded the first time the web app starts, so the AI reviews in it are the
record of past reviews, like any other entry in the audit trail. Every new review
in the app runs live.

  signed            Cloud Service Agreement, Globex (v1-v4), Mutual NDA, Initech (v1-v2),
                    Data Processing Agreement, Umbrella (v1-v4)
  ready to sign     Mutual NDA, Vandelay (v1-v2)
  needs review      Cloud Service Agreement, Hooli (v1-v4), Onboarding SOW, Globex (v1-v2)
  with counterparty Data Processing Agreement, Initech (v1-v3), Mutual NDA, Soylent (v1 only)
  not reviewed      Data Processing Agreement, Globex (v1-v2): a person starts the AI review
"""

import json
import shutil
import tempfile
import time
from pathlib import Path

from legal_agent import store
from legal_agent.docx_parser import parse_docx
from legal_agent.docx_writer import write_docx

ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "data" / "contracts"
PORTFOLIO = ROOT / "data" / "portfolio"
PEOPLE = {"legal": "Ana Ruiz", "sales": "Ben Okafor"}

# Our paper and a customer redline of it, per contract type.
PAPER = {
    "nda": ("Mutual NDA", "base/nda.docx", "redlines/nda_globex_v2.docx"),
    "msa": ("Cloud Service Agreement", "base/msa.docx", "redlines/msa_globex_v2.docx"),
    "dpa": ("Data Processing Agreement", "base/dpa.docx", "redlines/dpa_globex_v2.docx"),
    "sow": ("Onboarding SOW", "base/sow.docx", "redlines/sow_globex_v2.docx"),
}
TABLES = ("agreements", "versions", "rounds", "items", "events")
PATH_COLUMNS = {"agreements": ("related_file", "signed_pdf"), "versions": ("file",)}

# How a person answered the customer's changes in the first round, by clause id.
MSA_ROUND_1 = {
    "Payment Period": ("counter", "45 days from the invoice date.", "Finance agreed 45 days as our limit.",
                       "We can move from 30 to 45 days, which is the longest we offer."),
    "General Cap Amount": ("reject", "", "A 24-month cap doubles our exposure.",
                           "We keep the cap at 12 months of fees, our standard for all customers."),
    "5.5.b": ("reject", "", "The deletion duty protects us under GDPR Art. 28(3)(g).",
              "We need to keep the deletion clause; it is required for GDPR compliance."),
}
DPA_ROUND_1 = {
    "Subprocessor Notice Period": ("reject", "", "60 days would block vendor changes.",
                                   "We keep 30 days' notice with your right to object."),
    "3.1.a": ("reject", "", "Our hosting is in the US and the EU; EU-only needs a product change.",
              "We cannot commit to EU-only processing; transfers are covered by the SCCs."),
    "4.1": ("counter", None, "48 hours is the shortest we can operate.",
            "We can commit to 48 hours, with details provided in phases."),
}
ACCEPT_ALL = ("accept", "", "Within our playbook: mutual, limited to the deal.", "")


def build(http):
    """Play every negotiation through the app (`http` is a TestClient). Oldest activity first."""
    # Signed
    msa = _start(http, "msa", "Globex Inc.")
    _answer(http, msa, MSA_ROUND_1)
    _close(http, msa)
    _reply_with_cap(http, msa, "18 months")
    _answer(http, msa, {"General Cap Amount": (
        "accept", "", "Deal owner approved 18 months for this deal size; legal agreed.", "We accept 18 months.")})
    _close(http, msa)
    _sign(http, msa)

    nda = _start(http, "nda", "Initech Ltd.")
    _answer(http, nda, {}, default=ACCEPT_ALL)
    _close(http, nda)
    _sign(http, nda)

    dpa = _start(http, "dpa", "Umbrella Corp.")
    _answer(http, dpa, DPA_ROUND_1)
    _close(http, dpa)
    http.post(f"/agreements/{dpa}/accepted")
    _sign(http, dpa)

    # In negotiation
    dpa = _start(http, "dpa", "Initech Ltd.")
    _answer(http, dpa, DPA_ROUND_1)
    _close(http, dpa)

    msa = _start(http, "msa", "Hooli Inc.")
    _answer(http, msa, MSA_ROUND_1)
    _close(http, msa)
    _reply_with_cap(http, msa, "18 months")

    nda = _start(http, "nda", "Vandelay Industries")
    _answer(http, nda, {}, default=ACCEPT_ALL)
    _close(http, nda)

    _start(http, "sow", "Globex Inc.", related="base/msa.docx")

    # New
    _start(http, "nda", "Soylent Corp.", reply=False)
    _start(http, "dpa", "Globex Inc.", review=False)


def save(db_file: Path, out: Path = PORTFOLIO):
    """Write the database at `db_file` and its files to `out`, with paths relative to their folder."""
    app_dir = db_file.parent
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(app_dir / "files", out / "files")
    with store.connect(db_file) as db:
        tables = {t: [dict(r) for r in db.execute(f"SELECT * FROM {t}")] for t in TABLES}
    for table, columns in PATH_COLUMNS.items():
        for row in tables[table]:
            for column in columns:
                if row[column]:
                    row[column] = Path(row[column]).relative_to(app_dir).as_posix()
    (out / "portfolio.json").write_text(json.dumps(tables, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def load_if_empty(source: Path = PORTFOLIO, copy_files: bool = True) -> bool:
    """Fill an empty database with the portfolio. Returns whether it did.
    With `copy_files` off, the rows point at the portfolio's own files, which are only ever read."""
    with store.connect() as db:
        if db.execute("SELECT COUNT(*) FROM agreements").fetchone()[0] or not (source / "portfolio.json").exists():
            return False
        app_dir = store.current_db().parent if copy_files else source
        if copy_files:
            shutil.copytree(source / "files", app_dir / "files", dirs_exist_ok=True)
        tables = json.loads((source / "portfolio.json").read_text(encoding="utf-8"))
        for table in TABLES:
            for row in tables[table]:
                for column in PATH_COLUMNS.get(table, ()):
                    if row[column]:
                        row[column] = str(app_dir / row[column])
                db.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                           list(row.values()))
        db.commit()
    return True


def _start(http, kind: str, counterparty: str, related: str | None = None,
           reply: bool = True, review: bool = True) -> int:
    """Our v1 and, unless `reply` is False, their redline as v2."""
    title, ours, theirs = PAPER[kind]
    short = _slug(title)
    files = {"ours": (f"Acme_{short}_v1.docx", (CONTRACTS / ours).read_bytes())}
    if reply:
        files["theirs"] = (f"{counterparty.split()[0]}_{short}_v2_redline.docx", (CONTRACTS / theirs).read_bytes())
    if related:
        files["related"] = ("Acme_Cloud-Service-Agreement.docx", (CONTRACTS / related).read_bytes())
    page = http.post("/agreements", files=files, data={
        "title": title, "counterparty": counterparty, "review": "yes" if review else ""})
    agreement = int(page.url.path.rsplit("/", 1)[1])
    _wait(http, agreement)
    return agreement


def _reply_with_cap(http, agreement: int, cap: str):
    """They take our last version and meet us halfway on the liability cap."""
    data = _api(http, agreement)
    last = data["versions"][str(len(data["versions"]))]
    clauses = parse_docx(last["file"])
    for clause in clauses:
        if clause.id == "General Cap Amount":
            clause.text = clause.text.replace("12 months", cap)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "reply.docx"
        write_docx(clauses, path, data["title"])
        name = f"{data['counterparty'].split()[0]}_{_slug(data['title'])}_v{last['number'] + 1}.docx"
        http.post(f"/agreements/{agreement}/versions", files={"new": (name, path.read_bytes())})
    _wait(http, agreement)


def _answer(http, agreement: int, answers: dict, default=("accept", "", "Fine as is.", "")):
    """Answer every change in the open round: by clause id, or with `default`."""
    rnd = [r for r in _api(http, agreement)["rounds"].values() if r["status"] == "open"][-1]
    for change in rnd["changes"]:
        outcome, counter, internal, external = answers.get(change["clause_id"], default)
        if outcome == "counter" and counter is None:  # counter on the timing only
            counter = change["new_text"].replace("24 hours", "48 hours")
        http.post(f"/rounds/{rnd['id']}/items/{change['id']}", data={
            "actor": PEOPLE["legal"], "outcome": outcome, "counter_text": counter,
            "internal_comment": internal, "external_comment": external})


def _close(http, agreement: int):
    rnd = [r for r in _api(http, agreement)["rounds"].values() if r["status"] == "open"][-1]
    http.post(f"/rounds/{rnd['id']}/close", data={"actor": PEOPLE["legal"]})


def _sign(http, agreement: int):
    http.post(f"/agreements/{agreement}/sign", data={"name": PEOPLE["sales"], "job_title": "VP of Sales",
                                                     "style": "Dancing Script", "consent": "yes"})


def _wait(http, agreement: int):
    """The AI review runs in the app's background thread; wait for it to finish."""
    while _api(http, agreement)["status"] == "reviewing":
        time.sleep(2)
    if _api(http, agreement)["status"] == "failed":
        raise RuntimeError(f"The AI review of agreement {agreement} failed; see its audit trail.")


def _api(http, agreement: int) -> dict:
    """The agreement as stored, with string keys like the JSON the pages see."""
    with store.connect() as db:
        return json.loads(json.dumps(store.get_agreement(db, agreement)))


def _slug(title: str) -> str:
    return "-".join(title.split())
