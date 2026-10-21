"""Build Acme's contract .docx files (base versions and customer redlines).

Base contracts = a Key Terms cover page + Common Paper's standard terms,
written as numbered clauses like "8.1. Liability Cap. <text>".
Redlines = a base contract with a few edits, the way a customer would send it back.

Run:  python scripts/build_contracts.py
"""

import re
from pathlib import Path

from docx import Document

DATA = Path(__file__).resolve().parent.parent / "data"
SOURCES = DATA / "sources" / "commonpaper"

CONTRACTS = {
    "nda": {
        "title": "Mutual Non-Disclosure Agreement",
        "source": "nda.md",
        "key_terms": {
            "Parties": "Acme Cloud Inc. (Party 1) and the counterparty named on the signature page (Party 2).",
            "Purpose": "Evaluating whether to enter into a business relationship with the other party.",
            "MNDA Term": "Expires 1 year from the Effective Date.",
            "Term of Confidentiality": "2 years from the Effective Date, but for trade secrets until the information "
            "is no longer a trade secret under applicable laws.",
            "Governing Law": "State of Delaware.",
            "Jurisdiction": "Courts located in New Castle, Delaware.",
        },
    },
    "msa": {
        "title": "Cloud Service Agreement",
        "source": "msa.md",
        "key_terms": {
            "Provider": "Acme Cloud Inc.",
            "Subscription Period": "1 year from the Order Date, renewing for further 1 year periods unless either "
            "party gives notice of non-renewal at least 30 days before the end of the current period.",
            "Payment Period": "30 days from the invoice date.",
            "General Cap Amount": "The Fees paid or payable by Customer in the 12 months immediately before the claim.",
            "Increased Claims": "None.",
            "Governing Law": "State of Delaware.",
            "Chosen Courts": "Courts located in New Castle, Delaware.",
        },
    },
    "dpa": {
        "title": "Data Processing Agreement",
        "source": "dpa.md",
        "key_terms": {
            "Provider": "Acme Cloud Inc.",
            "Categories of Personal Data": "Name, business email address, job title and product usage data of Users.",
            "Special Category Data": "None. Customer will not submit special category data.",
            "Approved Subprocessors": "Amazon Web Services (hosting, United States and Ireland) and "
            "Zendesk (customer support, United States).",
            "Subprocessor Notice Period": "30 days before a new Subprocessor is engaged.",
            "Governing Member State": "Ireland.",
        },
    },
}

# The SOW is short, so we write it ourselves. It points at MSA sections on
# purpose: the Reviewer must ask for those clauses to judge some changes.
SOW = {
    "title": "Statement of Work: Onboarding and Data Migration",
    "key_terms": {
        "Provider": "Acme Cloud Inc.",
        "Agreement": "This SOW is governed by the Cloud Service Agreement between the parties (the \"Agreement\").",
        "Fees": "USD 48,000, invoiced in two equal parts at kickoff and at completion.",
    },
    "clauses": [
        ("1", "Scope", "Provider will configure the Cloud Service for Customer, migrate up to 2 million records "
         "from Customer's existing system and train up to 20 Users."),
        ("2", "Timeline", "Work starts within 10 business days of signature and is expected to finish within 8 weeks."),
        ("3", "Customer Responsibilities", "Customer will provide a named project lead, access to the source "
         "system and a clean data export within 5 business days of kickoff."),
        ("4", "Acceptance", "Each deliverable is accepted when Customer confirms in writing or 10 business days "
         "after delivery, whichever comes first, unless Customer reports a material defect in that period."),
        ("5", "Payment", "Invoices are payable within the Payment Period and as described in Section 4 "
         "(Payment & Taxes) of the Agreement."),
        ("6", "Liability", "Each party's liability under this SOW is subject to Section 8 (Limitation of Liability) "
         "of the Agreement."),
        ("7", "Change Requests", "Any change to scope, timeline or Fees requires a written change order signed by both parties."),
    ],
}

# Each redline is a list of edits applied to the base contract:
#   ("replace", old_text, new_text)   change wording inside a clause or key term
#   ("insert_after", number, (number, title, body))   add a new clause
#   ("delete", number)   remove a clause
REDLINES = {
    "nda_globex_v2": ("nda", [
        ("replace", "Expires 1 year from the Effective Date.", "Expires 3 years from the Effective Date."),
        ("replace", "State of Delaware.", "England and Wales."),
        ("replace", "Courts located in New Castle, Delaware.", "Courts located in London, England."),
        ("insert_after", "11", ("12", "Non-Solicitation", "During the MNDA Term, neither party will solicit "
                                "for employment any employee of the other party it met through the Purpose.")),
    ]),
    "msa_globex_v2": ("msa", [
        ("replace", "The Fees paid or payable by Customer in the 12 months immediately before the claim.",
         "The Fees paid or payable by Customer in the 6 months immediately before the claim."),
        ("replace", "30 days from the invoice date.", "90 days from the invoice date."),
        ("delete", "5.5.b"),
    ]),
}

LIST_ITEM = re.compile(r"^( *)(\d+|[a-z]+)\.\s+(.*)$")
TITLE_SPAN = re.compile(r'^<span class="header_\d"[^>]*>(.*?)</span>\s*(.*)$')
TITLE_BOLD = re.compile(r"^\*\*(.+?)\*\*\.?\s*(.*)$")


def clean(text):
    """Strip the HTML tags, bold markers and links Common Paper uses in its Markdown."""
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return text.replace("**", "").strip()


def parse_markdown(md):
    """Turn Common Paper Markdown into clause dicts with dotted numbers like '5.3.a'."""
    clauses, path = [], []
    for line in md.splitlines():
        match = LIST_ITEM.match(line)
        if not match:
            # Headings, footers and blank lines are not clauses.
            continue
        indent, marker, text = match.groups()
        level = len(indent) // 4
        path = path[:level] + [marker]
        title, body = "", text
        title_match = TITLE_SPAN.match(text) or TITLE_BOLD.match(text)
        if title_match:
            title, body = title_match.groups()
        clauses.append({"number": ".".join(path), "title": clean(title).rstrip("."), "body": clean(body)})
    return clauses


def base_contract(name):
    if name == "sow":
        clauses = [{"number": n, "title": t, "body": b} for n, t, b in SOW["clauses"]]
        return {"title": SOW["title"], "key_terms": dict(SOW["key_terms"]), "clauses": clauses}
    spec = CONTRACTS[name]
    clauses = parse_markdown((SOURCES / spec["source"]).read_text(encoding="utf-8"))
    return {"title": spec["title"], "key_terms": dict(spec["key_terms"]), "clauses": clauses}


def apply_edit(contract, edit):
    kind = edit[0]
    clauses = contract["clauses"]
    if kind == "replace":
        _, old, new = edit
        for term, value in contract["key_terms"].items():
            if old in value:
                contract["key_terms"][term] = value.replace(old, new)
                return
        for clause in clauses:
            if old in clause["body"]:
                clause["body"] = clause["body"].replace(old, new)
                return
        raise ValueError(f"Text not found: {old!r}")
    numbers = [c["number"] for c in clauses]
    if kind == "insert_after":
        _, after, (number, title, body) = edit
        clauses.insert(numbers.index(after) + 1, {"number": number, "title": title, "body": body})
    elif kind == "delete":
        del clauses[numbers.index(edit[1])]
    else:
        raise ValueError(f"Unknown edit: {kind}")


def write_docx(contract, path):
    doc = Document()
    doc.add_heading(contract["title"], level=0)
    doc.add_heading("Key Terms", level=1)
    for term, value in contract["key_terms"].items():
        doc.add_heading(term, level=2)
        doc.add_paragraph(value)
    doc.add_heading("Standard Terms", level=1)
    for clause in contract["clauses"]:
        paragraph = doc.add_paragraph()
        paragraph.add_run(f"{clause['number']}. ")
        if clause["title"]:
            paragraph.add_run(clause["title"] + ("." if clause["body"] else "")).bold = True
        if clause["body"]:
            paragraph.add_run((" " if clause["title"] else "") + clause["body"])
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)


def main():
    for name in ["nda", "msa", "dpa", "sow"]:
        write_docx(base_contract(name), DATA / "contracts" / "base" / f"{name}.docx")
    for redline, (base, edits) in REDLINES.items():
        contract = base_contract(base)
        for edit in edits:
            apply_edit(contract, edit)
        write_docx(contract, DATA / "contracts" / "redlines" / f"{redline}.docx")
    print("Contracts written to", DATA / "contracts")


if __name__ == "__main__":
    main()
