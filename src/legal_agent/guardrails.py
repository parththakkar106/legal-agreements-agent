"""Code checks on every Reviewer answer. They can only make a decision stricter.

The AI judges; these checks make sure it cannot be talked into, or slip into,
approving something a human should see.
"""

import re

from legal_agent.clause_diff import Change
from legal_agent.policy import Rule, red_line_hits

MIN_CONFIDENCE = 0.6

# Simple phrases that suggest the counterparty is trying to instruct the AI.
INJECTION_PATTERNS = [
    r"ignore (all |any )?(previous|prior|above|your) (instructions|rules)",
    r"\b(system|developer) prompt\b",
    r"\byou are (an? )?(ai|assistant|language model|reviewer)\b",
    r"\b(approve|accept) (this|these) (change|clause)s?\b",
    r"\bdo not (escalate|flag)\b",
]


def added_text(change: Change) -> str:
    """The words the customer added: new clauses whole, inserted words for edits."""
    if change.kind == "added":
        return change.new_text
    return " ".join(words for op, words in change.word_diff if op == "insert")


def looks_like_injection(text: str) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in INJECTION_PATTERNS)


def check_review(change: Change, data: dict, rules: list[Rule], retrieved: set[str]) -> tuple[str, list[str]]:
    """Return the final decision and the reasons any check changed or flagged it."""
    decision, notes = data["decision"], []
    rule_ids = {rule.id for rule in rules}

    def escalate(reason: str):
        nonlocal decision
        decision = "escalate"
        notes.append(reason)

    hits = red_line_hits(rules, added_text(change))
    if hits:
        escalate(f"Red line in added text ({', '.join(hits)}).")
    if data["injection_suspected"] or looks_like_injection(change.new_text):
        escalate("The clause text looks like it is trying to instruct the AI.")
    if not data["rule_ids"]:
        escalate("No playbook rule covers this change.")
    unknown_rules = [r for r in data["rule_ids"] if r not in rule_ids]
    unknown_gdpr = [c for c in data["gdpr_citations"] if c not in retrieved]
    if unknown_rules or unknown_gdpr:
        escalate(f"Cited sources that were not provided: {', '.join(unknown_rules + unknown_gdpr)}.")
    if data["confidence"] < MIN_CONFIDENCE:
        escalate(f"Low confidence ({data['confidence']:.2f}).")
    return decision, notes
