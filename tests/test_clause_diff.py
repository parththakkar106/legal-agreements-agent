from legal_agent.clause_diff import diff_clauses, word_diff
from legal_agent.docx_parser import Clause


def c(id, text, title=""):
    return Clause(id=id, title=title, text=text)


def test_identical_documents_have_no_changes():
    doc = [c("1", "Fees are due in 30 days."), c("2", "Delaware law applies.")]
    assert diff_clauses(doc, doc) == []


def test_modified_clause_with_word_diff():
    old = [c("4.3", "Payment. Customer will pay within 30 days of the invoice.", "Payment")]
    new = [c("4.3", "Payment. Customer will pay within 90 days of the invoice.", "Payment")]

    [change] = diff_clauses(old, new)

    assert change.kind == "modified"
    assert ("delete", "30") in change.word_diff and ("insert", "90") in change.word_diff


def test_short_field_fully_rewritten_is_modified_not_removed_and_added():
    old = [c("Governing Law", "State of Delaware.", "Governing Law")]
    new = [c("Governing Law", "England and Wales.", "Governing Law")]

    [change] = diff_clauses(old, new)

    assert change.kind == "modified"


def test_added_and_removed_clauses():
    old = [c("1", "Scope of the services."), c("2", "Customer may terminate for convenience.")]
    new = [c("1", "Scope of the services."), c("3", "Neither party will solicit the other's employees.")]

    changes = diff_clauses(old, new)

    assert [(ch.kind, ch.clause_id) for ch in changes] == [("removed", "2"), ("added", "3")]


def test_renumbered_but_unchanged_clause_is_not_a_change():
    old = [c("1", "Scope of the services."), c("2", "Fees are due in 30 days.")]
    new = [c("1", "Scope of the services."), c("2", "A brand new clause about insurance."), c("3", "Fees are due in 30 days.")]

    changes = diff_clauses(old, new)

    assert [(ch.kind, ch.clause_id) for ch in changes] == [("added", "2")]


def test_renumbered_and_edited_clause_is_matched_by_similarity():
    old = [c("5", "Each party's total liability will not exceed the fees paid in the 12 months before the claim.")]
    new = [c("6", "Each party's total liability will not exceed the fees paid in the 6 months before the claim.")]

    [change] = diff_clauses(old, new)

    assert (change.kind, change.clause_id) == ("modified", "6")


def test_change_ids_follow_document_order():
    old = [c("1", "Alpha clause text here."), c("2", "Beta clause text here."), c("3", "Gamma clause text here.")]
    new = [c("1", "Alpha clause text changed."), c("3", "Gamma clause text here."), c("4", "Delta is new.")]

    changes = diff_clauses(old, new)

    assert [ch.id for ch in changes] == [1, 2, 3]
    assert [ch.kind for ch in changes] == ["modified", "removed", "added"]


def test_word_diff_ignores_nothing_when_equal():
    assert word_diff("a b c", "a b c") == [("equal", "a b c")]
