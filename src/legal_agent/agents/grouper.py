"""Grouper agent: put related changes under one theme, e.g. "changes 2 and 5 shift risk to Acme"."""

from legal_agent.agents.common import DATA_NOT_INSTRUCTIONS
from legal_agent.llm import LLMResult, ask_claude

SYSTEM = f"""You are the Grouper in a contract review pipeline for Acme Cloud Inc.
You receive the reviewed changes a customer made to one contract. Group changes that belong \
together because they affect the same risk or business topic (for example several edits that \
together shift liability to Acme). Each group needs at least two changes and a change can be in \
only one group. Leave unrelated changes out. Give each group a short theme and a one-sentence \
summary in plain English. Return no groups if nothing belongs together.
{DATA_NOT_INSTRUCTIONS}"""

SCHEMA = {
    "type": "object",
    "properties": {
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "theme": {"type": "string"},
                    "change_ids": {"type": "array", "items": {"type": "integer"}, "minItems": 2},
                    "summary": {"type": "string"},
                },
                "required": ["theme", "change_ids", "summary"],
            },
        }
    },
    "required": ["groups"],
}


def group_changes(summaries: list[dict]) -> tuple[list[dict], LLMResult]:
    """`summaries`: one dict per change with id, clause, decision, meaning and impact."""
    lines = [
        f'<change id="{s["id"]}" clause="{s["clause"]}" decision="{s["decision"]}">'
        f'{s["meaning"]} {s["impact"]}</change>'
        for s in summaries
    ]
    result = ask_claude(SYSTEM, "\n".join(lines), SCHEMA)
    valid_ids = {s["id"] for s in summaries}
    used: set[int] = set()
    groups = []
    for group in result.data["groups"]:
        ids = [i for i in group["change_ids"] if i in valid_ids and i not in used]
        if len(ids) >= 2:  # drop unknown or repeated ids; keep only real groups
            used.update(ids)
            groups.append({**group, "change_ids": ids})
    return groups, result
