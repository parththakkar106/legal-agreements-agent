from fakes import result, review_answer

from legal_agent.agents import classifier, grouper, reviewer
from legal_agent.clause_diff import Change
from legal_agent.docx_parser import Clause
from legal_agent.policy import load_policy

CHANGE = Change(1, "modified", "6", "Liability", "subject to Section 8.", "subject to Section 8 and 9.", [])


def test_reviewer_fetches_requested_clause_then_decides(monkeypatch):
    prompts = []
    answers = iter([review_answer(need_context=["msa:8"]), review_answer(decision="push_back")])

    def fake(system, prompt, schema):
        prompts.append(prompt)
        return result(next(answers))
    monkeypatch.setattr(reviewer, "ask_claude", fake)

    review = reviewer.review_change(CHANGE, "sow", load_policy(), [], {"msa:8": "Liability is capped."})

    assert review.data["decision"] == "push_back"
    assert review.context_used == ["msa:8"]
    assert '<clause ref="msa:8">Liability is capped.</clause>' in prompts[1]
    assert "Liability is capped." not in prompts[0]  # other clauses' text only when asked for


def test_reviewer_stops_asking_for_context_after_the_limit(monkeypatch):
    calls = []

    def fake(system, prompt, schema):
        calls.append(prompt)
        return result(review_answer(need_context=[f"x{len(calls)}"]))
    monkeypatch.setattr(reviewer, "ask_claude", fake)

    review = reviewer.review_change(CHANGE, "sow", load_policy(), [], {})

    assert len(calls) == reviewer.MAX_CONTEXT_ROUNDS + 1
    assert "(no clause with this reference)" in calls[-1]


def test_classifier_maps_answers_to_change_ids(monkeypatch):
    answer = {"doc_type": "dpa", "changes": [{"change_id": 1, "gdpr_chapters": ["IV"], "search_query": "processor"}]}
    monkeypatch.setattr(classifier, "ask_claude", lambda *a: result(answer))

    classification, _ = classifier.classify([Clause("1", "", "x")], [CHANGE], {"IV": "Controller and processor"})

    assert classification.doc_type == "dpa"
    assert classification.chapters == {1: ["IV"]} and classification.queries == {1: "processor"}


def test_grouper_drops_unknown_and_repeated_ids(monkeypatch):
    answer = {"groups": [{"theme": "a", "change_ids": [1, 2, 99], "summary": "s"},
                         {"theme": "b", "change_ids": [2, 3], "summary": "s"}]}
    monkeypatch.setattr(grouper, "ask_claude", lambda *a: result(answer))
    summaries = [{"id": i, "clause": str(i), "decision": "accept", "meaning": "", "impact": ""} for i in (1, 2, 3)]

    groups, _ = grouper.group_changes(summaries)

    assert [g["change_ids"] for g in groups] == [[1, 2]]  # group b is left with one change, so it is dropped
