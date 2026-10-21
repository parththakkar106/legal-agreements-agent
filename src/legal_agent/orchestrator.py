"""Run the whole review: parse -> diff -> classify -> retrieve -> review -> guardrails -> group.

Plain Python decides the order; the three AI agents only make judgments.
Changes are reviewed in parallel because each `claude -p` call takes seconds.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from legal_agent.agents.classifier import classify
from legal_agent.agents.grouper import group_changes
from legal_agent.agents.reviewer import Review, review_change
from legal_agent.clause_diff import Change, diff_clauses
from legal_agent.docx_parser import parse_docx
from legal_agent.gdpr import chapter_titles, load_chunks
from legal_agent.guardrails import check_review
from legal_agent.llm import LLMResult
from legal_agent.policy import load_policy, rules_for
from legal_agent.search import HybridSearch

SEARCH_RESULTS = 4  # GDPR paragraphs given to the Reviewer per change
PARALLEL_REVIEWS = 4


@dataclass
class ReviewedChange:
    change: Change
    gdpr_chapters: list[str]
    search_query: str
    excerpts: list[str]  # citations of the GDPR paragraphs the Reviewer saw
    meaning: str
    impact: str
    ai_decision: str  # what the Reviewer said
    decision: str  # after guardrails
    rule_ids: list[str]
    gdpr_citations: list[str]
    counter_proposal: str
    confidence: float
    guardrail_notes: list[str]
    context_used: list[str]


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    llm_seconds: float = 0.0

    def add(self, result: LLMResult):
        self.calls += 1
        self.input_tokens += result.input_tokens
        self.output_tokens += result.output_tokens
        self.cost_usd += result.cost_usd
        self.llm_seconds += result.seconds


@dataclass
class ContractReview:
    doc_type: str
    verdict: str  # "ready to sign" | "send back" | "escalate"
    changes: list[ReviewedChange]
    groups: list[dict]
    usage: Usage = field(default_factory=Usage)


_search: HybridSearch | None = None


def get_search() -> HybridSearch:
    """Build the GDPR index once per process (about 2 seconds)."""
    global _search
    if _search is None:
        _search = HybridSearch(load_chunks())
    return _search


def review_contract(old_path: str | Path, new_path: str | Path,
                    related: dict[str, str | Path] | None = None) -> ContractReview:
    """Review a customer's redline. `related` names other contracts the Reviewer may consult,
    e.g. {"msa": "msa.docx"} for a SOW that points at the MSA."""
    new_clauses = parse_docx(new_path)
    changes = diff_clauses(parse_docx(old_path), new_clauses)
    usage = Usage()
    if not changes:
        return ContractReview("unknown", "ready to sign", [], [], usage)

    classification, result = classify(new_clauses, changes, chapter_titles())
    usage.add(result)
    rules = rules_for(load_policy(), classification.doc_type)

    references = {c.id: c.text for c in new_clauses}
    for name, path in (related or {}).items():
        references.update({f"{name}:{c.id}": c.text for c in parse_docx(path)})

    def review_one(change: Change) -> tuple[ReviewedChange, list[LLMResult]]:
        chapters = classification.chapters[change.id]
        query = classification.queries[change.id]
        excerpts = [chunk for chunk, _ in get_search().search(query, k=SEARCH_RESULTS, chapters=set(chapters))] \
            if chapters and query else []
        review = _review_with_retry(change, classification.doc_type, rules, excerpts, references)
        if not review.data:  # the Reviewer failed twice: a human decides
            decision, notes = "escalate", ["The Reviewer did not return a valid answer."]
            data = {"meaning": "", "impact": "", "decision": "escalate", "rule_ids": [], "gdpr_citations": [],
                    "counter_proposal": "", "confidence": 0.0}
        else:
            data = review.data
            decision, notes = check_review(change, data, rules, {chunk.citation for chunk in excerpts})
        return ReviewedChange(
            change=change, gdpr_chapters=chapters, search_query=query,
            excerpts=[chunk.citation for chunk in excerpts],
            meaning=data["meaning"], impact=data["impact"], ai_decision=data["decision"], decision=decision,
            rule_ids=data["rule_ids"], gdpr_citations=data["gdpr_citations"],
            counter_proposal=data["counter_proposal"], confidence=data["confidence"],
            guardrail_notes=notes, context_used=review.context_used,
        ), review.calls

    get_search()  # build the index before the threads start, not in each of them
    with ThreadPoolExecutor(max_workers=PARALLEL_REVIEWS) as pool:
        outcomes = list(pool.map(review_one, changes))
    reviewed = [change for change, _ in outcomes]
    for _, calls in outcomes:  # counted here, not inside the threads, so no two threads add at once
        for call in calls:
            usage.add(call)

    groups = []
    if len(reviewed) >= 2:
        summaries = [{"id": r.change.id, "clause": r.change.clause_id, "decision": r.decision,
                      "meaning": r.meaning, "impact": r.impact} for r in reviewed]
        groups, result = group_changes(summaries)
        usage.add(result)

    decisions = {r.decision for r in reviewed}
    verdict = "escalate" if "escalate" in decisions else "send back" if "push_back" in decisions else "ready to sign"
    return ContractReview(classification.doc_type, verdict, reviewed, groups, usage)


def _review_with_retry(change, doc_type, rules, excerpts, references) -> Review:
    """One retry if the model's answer is unusable; an empty Review means it failed twice."""
    for _ in range(2):
        try:
            return review_change(change, doc_type, rules, excerpts, references)
        except ValueError:
            continue
    return Review(data={})
