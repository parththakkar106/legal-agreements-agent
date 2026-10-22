from pathlib import Path

from fakes import result, review_answer

from legal_agent import evaluation
from legal_agent.agents import classifier, grouper, reviewer
from legal_agent.clause_diff import diff_clauses
from legal_agent.docx_parser import parse_docx


def test_every_case_builds_into_exactly_one_change(tmp_path):
    contracts = evaluation.load_cases()
    assert sum(len(c.cases) for c in contracts) >= 30
    for contract in contracts:
        old, new = evaluation.build(contract, tmp_path)
        changed = sorted(c.clause_id for c in diff_clauses(parse_docx(old), parse_docx(new)))
        assert changed == sorted(case.clause for case in contract.cases), contract.id


def test_scores_a_run(monkeypatch, tmp_path):
    """With a fake model that accepts everything, only the guardrails can catch anything."""
    monkeypatch.setattr(classifier, "ask_claude", lambda *a: result({"doc_type": "nda", "changes": []}))
    monkeypatch.setattr(reviewer, "ask_claude", lambda *a: result(review_answer(rule_ids=["nda-term"])))
    monkeypatch.setattr(grouper, "ask_claude", lambda *a: result({"groups": []}))
    contract = evaluation.load_cases()[0]
    runs = {contract.id: evaluation.run_pipeline(contract, *evaluation.build(contract, tmp_path))}

    metrics = evaluation.score([contract], runs)
    assert metrics["Changes missed"] == "0"
    # n3 (Russia) is a red line and n5 hides an injection; the fake AI accepts both,
    # the red-line guardrail still escalates n3.
    assert runs[contract.id].answers["n3"].decision == "escalate"
    assert runs[contract.id].answers["n3"].ai_decision == "accept"
    assert metrics["Contract type right"] == "1/1 (100%)"
    assert "| n1 | accept |" in evaluation.report([contract], runs, {})


def test_clause_references_from_the_baseline_are_normalised():
    assert evaluation._clause_ref("[§ 8.1]") == "8.1"
    assert evaluation._clause_ref("Section 12.8") == "12.8"
    assert evaluation._clause_ref("Governing Law") == "governing law"
    assert evaluation._article_paragraph("GDPR Art. 28(3)(g)") == (28, 3)
    assert evaluation._article_paragraph("Article 44") == (44, None)
