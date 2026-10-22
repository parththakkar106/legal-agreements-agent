"""The human side of a negotiation round.

A round is one version from the counterparty, compared with the version we sent.
For each change a person picks an outcome: accept, reject (restore our wording)
or counter (propose new wording). How much is required depends on the AI's colour:
  escalate (red)    -> an outcome and an internal comment
  push_back (yellow) -> an outcome
  accept (green)     -> nothing; it counts as accepted unless a person says otherwise
When every change is accepted, the agreement is ready to sign. Otherwise our answers
become the next version we send back, unsigned. Nobody signs mid-negotiation.
"""

from copy import copy

from legal_agent.clause_diff import Change, word_diff
from legal_agent.docx_parser import Clause

OUTCOMES = ("accept", "reject", "counter")


def outcome_of(decision: str, item: dict | None) -> str | None:
    """The outcome a person chose, or "accept" for an untouched green change."""
    item = item or {}
    return item.get("outcome") or ("accept" if decision == "accept" else None)


def missing(decision: str, item: dict | None) -> list[str]:
    """What a person still has to do before this change is resolved."""
    item = item or {}
    needs = []
    if not outcome_of(decision, item):
        needs.append("choose accept, reject or counter")
    if decision == "escalate" and not item.get("internal_comment"):
        needs.append("add an internal comment")
    if item.get("outcome") == "counter" and not item.get("counter_text"):
        needs.append("write your counter-proposal")
    return needs


def next_version(old: list[Clause], new: list[Clause], changes: list[Change],
                 outcomes: dict[int, tuple[str, str]]) -> list[Clause]:
    """Their version with our answers applied. `outcomes` maps change id -> (outcome, counter text)."""
    result = [copy(c) for c in new]
    for change in changes:
        outcome, counter = outcomes.get(change.id, ("accept", ""))
        if outcome == "accept":
            continue
        wording = change.old_text if outcome == "reject" else counter
        if change.kind == "modified":
            next(c for c in result if c.id == change.clause_id).text = wording
        elif change.kind == "added":
            if outcome == "reject":
                result = [c for c in result if c.id != change.clause_id]
            else:
                next(c for c in result if c.id == change.clause_id).text = wording
        else:  # removed by them: put the clause back where it was in our version
            restored = copy(next(c for c in old if c.id == change.clause_id))
            restored.text = wording
            result.insert(_position_after_predecessor(old, result, change.clause_id), restored)
    return result


def _position_after_predecessor(old: list[Clause], result: list[Clause], clause_id: str) -> int:
    """Index just after the nearest clause that came before `clause_id` in our version."""
    ids = [c.id for c in result]
    before = [c.id for c in old][:[c.id for c in old].index(clause_id)]
    for previous in reversed(before):
        if previous in ids:
            return ids.index(previous) + 1
    return 0


def cover_note(counterparty: str, their_version: int, our_version: int, changes: list[Change],
               items: dict[int, dict], decisions: dict[int, str]) -> str:
    """The message that goes with our next version: our answer to each of their changes."""
    lines = [f"Our answer to your changes in v{their_version}. Version v{our_version} is attached, unsigned."]
    for change in changes:
        item = items.get(change.id, {})
        outcome = outcome_of(decisions[change.id], item)
        where = f"§ {change.clause_id}" if change.clause_id[:1].isdigit() else change.clause_id
        if change.title and change.title != change.clause_id:
            where += f" ({change.title})"
        answer = {"accept": "accepted", "reject": "not accepted; our original wording is restored",
                  "counter": "we propose new wording, see the attached version"}[outcome]
        if outcome == "reject" and change.kind == "added":
            answer = "not accepted; the new clause is taken out"
        line = f"- {where}: {answer}."
        if item.get("external_comment"):
            line += f" {item['external_comment']}"
        lines.append(line)
    return "\n".join(lines)


def clause_history(agreement: dict) -> list[dict]:
    """For every clause anyone changed: its wording in each version, and what we answered.

    Example: Payment Period: 30 days (v1, Acme) -> 90 days (v2, Globex, push back)
    -> 45 days (v3, Acme, countered by Ana).
    """
    versions, history = agreement["versions"], {}
    for rnd in agreement["rounds"].values():
        decisions = {r["change"]["id"]: r["decision"] for r in (rnd["result"] or {}).get("changes", [])}
        for change in rnd["changes"]:
            entry = history.setdefault(change["clause_id"], {"clause": change["clause_id"], "title": change["title"],
                                                             "steps": []})
            if not entry["steps"]:
                old = versions[rnd["old_version"]]
                entry["steps"].append({"version": old["number"], "who": old["author"],
                                       "text": change["old_text"] or "(not in this version)", "note": ""})
            new = versions[rnd["new_version"]]
            decision = decisions.get(change["id"])
            entry["steps"].append({"version": new["number"], "who": new["author"],
                                   "text": change["new_text"] or "(removed)", "note": decision or ""})
            if rnd["status"] != "closed" or decision is None:
                continue
            item = rnd["answers"].get(change["id"], {})
            outcome = outcome_of(decision, item)
            reply = versions.get(rnd["new_version"] + 1)
            if outcome == "accept" or reply is None:
                entry["steps"][-1]["note"] += f" · accepted{' by ' + item['actor'] if item.get('actor') else ''}"
            else:
                text = change["old_text"] if outcome == "reject" else item.get("counter_text", "")
                entry["steps"].append({"version": reply["number"], "who": reply["author"],
                                       "text": text or "(removed)", "note": f"{outcome}ed by {item.get('actor', '')}"})
    for entry in history.values():
        for before, step in zip(entry["steps"], entry["steps"][1:]):
            if not (before["text"].startswith("(") or step["text"].startswith("(")):
                step["diff"] = [(op, _shorten(words) if op == "equal" else words)
                                for op, words in word_diff(before["text"], step["text"])]
    return list(history.values())


def _shorten(words: str, keep: int = 6) -> str:
    """Long unchanged stretches are cut to their ends, so the history shows what moved."""
    parts = words.split()
    return words if len(parts) <= 2 * keep + 2 else " ".join(parts[:keep] + ["…"] + parts[-keep:])
