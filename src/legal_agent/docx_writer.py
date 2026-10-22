"""Write clauses back to a .docx, in the layout docx_parser reads.

Used to build the next version we send to the counterparty: their version with
our answers applied. Key Terms become a heading plus a paragraph; numbered
clauses become "8.1. **Title.** text".
"""

from pathlib import Path

from docx import Document

from legal_agent.docx_parser import Clause


def write_docx(clauses: list[Clause], path: str | Path, title: str):
    doc = Document()
    doc.add_heading(title, level=0)
    for clause in clauses:
        if clause.id == clause.title:  # a Key Term such as "Governing Law"
            doc.add_heading(clause.id, level=2)
            for line in clause.text.split("\n"):
                doc.add_paragraph(line)
            continue
        first, *rest = clause.text.split("\n")
        paragraph = doc.add_paragraph()
        paragraph.add_run(f"{clause.id}. ")
        if clause.title and first.startswith(clause.title + "."):
            paragraph.add_run(clause.title + ".").bold = True
            first = first[len(clause.title) + 1:]
        paragraph.add_run(first)
        for line in rest:
            doc.add_paragraph(line)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
