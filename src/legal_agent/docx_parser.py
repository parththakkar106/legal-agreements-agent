"""Read a .docx contract and split it into clauses.

A new clause starts at either:
  - a paragraph that begins with a clause number such as "8.", "8.1." or "5.3.a.", or
  - a Word heading (used for cover-page fields such as "Governing Law").
Any other paragraph is added to the clause above it.

Limitation: numbers must be typed in the text. Word's automatic list numbering
is not read (our templates and their redlines always carry typed numbers).
"""

import re
from dataclasses import dataclass
from pathlib import Path

from docx import Document

# A number like "8." "8.1." "5.3.a." followed by a space. The trailing dot is
# required so that ordinary text such as "12 months ..." is not mistaken for one.
CLAUSE_NUMBER = re.compile(r"^(\d+(?:\.(?:\d+|[a-z]{1,4}))*)\.\s+")


@dataclass
class Clause:
    id: str  # clause number ("8.1") or heading text ("Governing Law")
    title: str  # short title, e.g. "Liability Cap"; may be empty
    text: str  # full clause text without the number


def parse_docx(path: str | Path) -> list[Clause]:
    """Return the clauses of a .docx file in document order."""
    clauses: list[Clause] = []
    for paragraph in Document(str(path)).paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        number = CLAUSE_NUMBER.match(text)
        if number:
            body = text[number.end():]
            clauses.append(Clause(id=number.group(1), title=_bold_title(paragraph), text=body))
        elif paragraph.style.name.startswith("Heading"):
            clauses.append(Clause(id=text, title=text, text=""))
        elif clauses:
            clauses[-1].text = (clauses[-1].text + "\n" + text).strip()
        # Text before the first clause (e.g. the document title) is skipped.

    # A heading with nothing under it (e.g. "Key Terms") is a section label, not a clause.
    clauses = [c for c in clauses if c.text]
    return _unique_ids(clauses)


def _bold_title(paragraph) -> str:
    """The bold words at the start of a clause, e.g. "Liability Cap"."""
    title = ""
    for run in paragraph.runs:
        if run.bold:
            title += run.text
        elif title:
            break  # the bold title has ended
        elif not CLAUSE_NUMBER.match(run.text):
            break  # the clause does not start with a bold title
    return title.strip().rstrip(".")


def _unique_ids(clauses: list[Clause]) -> list[Clause]:
    """Make ids unique by adding " (2)", " (3)" to repeats, so they can be used as keys."""
    seen: dict[str, int] = {}
    for clause in clauses:
        seen[clause.id] = seen.get(clause.id, 0) + 1
        if seen[clause.id] > 1:
            clause.id = f"{clause.id} ({seen[clause.id]})"
    return clauses
