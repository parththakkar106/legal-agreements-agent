"""Load Acme's playbook (data/policy.yaml) and run its red-line checks."""

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

POLICY_FILE = Path(__file__).resolve().parents[2] / "data" / "policy.yaml"
DECISIONS = ("accept", "push_back", "escalate")


@dataclass
class Rule:
    id: str
    topic: str
    applies_to: list[str]  # document types: nda, msa, dpa, sow
    standard: str  # Acme's own position
    accept: str = ""  # what we accept, push back on, or escalate ("" = not used by this rule)
    push_back: str = ""
    escalate: str = ""
    red_line: list[str] = field(default_factory=list)  # regexes that force escalation


def load_policy(path: Path = POLICY_FILE) -> list[Rule]:
    rules = [Rule(**raw) for raw in yaml.safe_load(path.read_text(encoding="utf-8"))["rules"]]
    for rule in rules:
        for pattern in rule.red_line:
            re.compile(pattern)  # fail at load time, not mid-review, on a bad pattern
    return rules


def rules_for(rules: list[Rule], doc_type: str) -> list[Rule]:
    """The rules that apply to one document type."""
    return [rule for rule in rules if doc_type in rule.applies_to]


def red_line_hits(rules: list[Rule], added_text: str) -> list[str]:
    """Ids of rules whose red-line patterns match text the customer added."""
    return [
        rule.id
        for rule in rules
        if any(re.search(pattern, added_text, re.IGNORECASE) for pattern in rule.red_line)
    ]
