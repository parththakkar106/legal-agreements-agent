from pathlib import Path

from legal_agent.clause_diff import diff_clauses
from legal_agent.docx_parser import Clause, parse_docx
from legal_agent.docx_writer import write_docx
from legal_agent.negotiation import cover_note, missing, next_version, outcome_of

DATA = Path(__file__).resolve().parent.parent / "data" / "contracts"
OLD = [Clause("Payment Period", "Payment Period", "30 days from the invoice date."),
       Clause("1", "Scope", "Scope. We build it."),
       Clause("2", "Audit", "Audit. Once a year.")]
NEW = [Clause("Payment Period", "Payment Period", "90 days from the invoice date."),
       Clause("1", "Scope", "Scope. We build it."),
       Clause("1.a", "", "Customer may terminate for convenience.")]


def test_gates_depend_on_the_ai_colour():
    assert missing("accept", None) == []  # green: accepted unless someone says otherwise
    assert missing("push_back", None) == ["choose accept, reject or counter"]
    assert missing("push_back", {"outcome": "reject"}) == []
    assert missing("escalate", {"outcome": "accept"}) == ["add an internal comment"]
    assert missing("escalate", {"outcome": "counter", "internal_comment": "ok"}) == ["write your counter-proposal"]
    assert outcome_of("accept", None) == "accept" and outcome_of("push_back", None) is None


def test_next_version_applies_our_answers():
    changes = diff_clauses(OLD, NEW)
    by_clause = {c.clause_id: c.id for c in changes}
    outcomes = {by_clause["Payment Period"]: ("counter", "45 days from the invoice date."),
                by_clause["1.a"]: ("reject", ""),
                by_clause["2"]: ("reject", "")}

    result = next_version(OLD, NEW, changes, outcomes)

    assert [(c.id, c.text) for c in result] == [
        ("Payment Period", "45 days from the invoice date."),  # our counter
        ("1", "Scope. We build it."),
        ("2", "Audit. Once a year."),  # their deletion undone, back in its place
    ]  # and their new clause 1.a is gone


def test_cover_note_lists_each_answer():
    changes = diff_clauses(OLD, NEW)
    decisions = {c.id: "push_back" for c in changes}
    items = {changes[0].id: {"outcome": "counter", "external_comment": "45 days is our limit."}}
    note = cover_note("Globex", 2, 3, changes[:1], items, decisions)
    assert "v3 is attached, unsigned" in note and "Payment Period: we propose new wording" in note
    assert "45 days is our limit." in note


def test_written_docx_reads_back_the_same(tmp_path):
    clauses = parse_docx(DATA / "base" / "sow.docx")
    write_docx(clauses, tmp_path / "copy.docx", "SOW")
    assert parse_docx(tmp_path / "copy.docx") == clauses
