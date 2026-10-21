"""Compare two versions of a contract clause by clause.

Matching happens in three passes, so a renumbered clause is not reported as
"removed + added":
  1. Clauses with identical text are paired (wherever they sit in the document).
  2. Remaining clauses with the same id and the same title are paired. This
     catches short fields whose whole value changed, e.g. Governing Law
     "State of Delaware." -> "England and Wales.".
  3. The rest are paired by text similarity, best match first, if they are
     at least MIN_SIMILARITY alike.
Paired clauses whose text differs are "modified". Whatever is left over is
"removed" (only in the old version) or "added" (only in the new version).
"""

from dataclasses import dataclass, field
from difflib import SequenceMatcher

from legal_agent.docx_parser import Clause

MIN_SIMILARITY = 0.5


@dataclass
class Change:
    id: int  # 1, 2, 3 ... in document order
    kind: str  # "added" | "removed" | "modified"
    clause_id: str  # clause number in the new version (old version for "removed")
    title: str
    old_text: str  # "" when added
    new_text: str  # "" when removed
    word_diff: list[tuple[str, str]] = field(default_factory=list)  # [("equal"|"delete"|"insert", words)]


def diff_clauses(old: list[Clause], new: list[Clause]) -> list[Change]:
    """Return the list of changes needed to turn `old` into `new`."""
    pairs: dict[int, int] = {}  # index in new -> index in old

    # Pass 1: identical text.
    old_by_text: dict[str, list[int]] = {}
    for i, clause in enumerate(old):
        old_by_text.setdefault(_normalise(clause.text), []).append(i)
    for j, clause in enumerate(new):
        same = old_by_text.get(_normalise(clause.text))
        if same:
            pairs[j] = same.pop(0)

    # Pass 2: same id and same title.
    free_old = set(range(len(old))) - set(pairs.values())
    old_by_key = {(old[i].id, old[i].title): i for i in free_old if old[i].title}
    for j, clause in enumerate(new):
        i = old_by_key.get((clause.id, clause.title))
        if j not in pairs and i is not None:
            pairs[j] = i
            free_old.remove(i)

    # Pass 3: most similar remaining clauses, best pairs first.
    free_new = [j for j in range(len(new)) if j not in pairs]
    candidates = []
    for j in free_new:
        for i in free_old:
            score = _similarity(old[i].text, new[j].text)
            if score >= MIN_SIMILARITY:
                candidates.append((score, j, i))
    for score, j, i in sorted(candidates, reverse=True):
        if j not in pairs and i in free_old:
            pairs[j] = i
            free_old.remove(i)

    # Build the change list in the order of the new document; removed
    # clauses are placed right after the clause that preceded them in the old one.
    changes: list[Change] = []
    old_position = {i: j for j, i in pairs.items()}
    removed_after: dict[int, list[int]] = {}  # new index (-1 = start) -> removed old indexes
    for i in sorted(free_old):
        previous_kept = max((k for k in old_position if k < i), default=None)
        anchor = -1 if previous_kept is None else old_position[previous_kept]
        removed_after.setdefault(anchor, []).append(i)

    for i in removed_after.get(-1, []):
        changes.append(_removed(old[i]))
    for j, clause in enumerate(new):
        if j not in pairs:
            changes.append(Change(0, "added", clause.id, clause.title, "", clause.text))
        else:
            before = old[pairs[j]]
            if _normalise(before.text) != _normalise(clause.text):
                changes.append(Change(0, "modified", clause.id, clause.title or before.title,
                                      before.text, clause.text, word_diff(before.text, clause.text)))
        for i in removed_after.get(j, []):
            changes.append(_removed(old[i]))

    for number, change in enumerate(changes, start=1):
        change.id = number
    return changes


def word_diff(old_text: str, new_text: str) -> list[tuple[str, str]]:
    """Word-level differences, e.g. [("equal", "Fees paid in the"), ("delete", "12"), ("insert", "6"), ...]."""
    old_words, new_words = old_text.split(), new_text.split()
    result = []
    for op, i1, i2, j1, j2 in SequenceMatcher(None, old_words, new_words, autojunk=False).get_opcodes():
        if op in ("delete", "replace"):
            result.append(("delete", " ".join(old_words[i1:i2])))
        if op in ("insert", "replace"):
            result.append(("insert", " ".join(new_words[j1:j2])))
        if op == "equal":
            result.append(("equal", " ".join(old_words[i1:i2])))
    return result


def _removed(clause: Clause) -> Change:
    return Change(0, "removed", clause.id, clause.title, clause.text, "")


def _normalise(text: str) -> str:
    """Ignore differences in spacing and letter case."""
    return " ".join(text.lower().split())


def _similarity(a: str, b: str) -> float:
    """0.0 (nothing in common) to 1.0 (same words), compared word by word."""
    matcher = SequenceMatcher(None, _normalise(a).split(), _normalise(b).split(), autojunk=False)
    if matcher.real_quick_ratio() < MIN_SIMILARITY or matcher.quick_ratio() < MIN_SIMILARITY:
        return 0.0  # cheap upper bounds: skip the slow exact ratio when it cannot pass
    return matcher.ratio()
