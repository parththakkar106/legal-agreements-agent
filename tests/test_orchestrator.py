from pathlib import Path

from fakes import result, review_answer

from legal_agent import orchestrator
from legal_agent.agents import classifier, grouper, reviewer

DATA = Path(__file__).resolve().parent.parent / "data" / "contracts"


def test_full_review_with_fake_ai(monkeypatch):
    def fake_classify(system, prompt, schema):
        return result({"doc_type": "msa", "changes": []})

    def fake_review(system, prompt, schema):
        if 'clause="Payment Period"' in prompt:
            return result(review_answer(decision="push_back", rule_ids=["payment-terms"]))
        return result(review_answer(decision="accept", rule_ids=["liability-cap"]))

    monkeypatch.setattr(classifier, "ask_claude", fake_classify)
    monkeypatch.setattr(reviewer, "ask_claude", fake_review)
    monkeypatch.setattr(grouper, "ask_claude", lambda *a: result({"groups": []}))

    review = orchestrator.review_contract(DATA / "base" / "msa.docx", DATA / "redlines" / "msa_globex_v2.docx")

    decisions = {r.change.clause_id: r.decision for r in review.changes}
    assert decisions["Payment Period"] == "push_back"
    assert decisions["General Cap Amount"] == "accept"
    assert review.verdict == "send back"
    assert review.usage.calls == 1 + len(review.changes) + 1  # classify + one review each + group


def test_identical_contracts_need_no_ai(monkeypatch):
    def fail(*args):
        raise AssertionError("the AI should not be called")
    monkeypatch.setattr(classifier, "ask_claude", fail)

    review = orchestrator.review_contract(DATA / "base" / "nda.docx", DATA / "base" / "nda.docx")

    assert review.verdict == "ready to sign" and review.usage.calls == 0
