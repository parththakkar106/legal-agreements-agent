from docx import Document

from legal_agent.docx_parser import parse_docx


def make_docx(tmp_path, build):
    doc = Document()
    build(doc)
    path = tmp_path / "contract.docx"
    doc.save(path)
    return path


def numbered(doc, number, title, body):
    paragraph = doc.add_paragraph()
    paragraph.add_run(f"{number}. ")
    paragraph.add_run(f"{title}.").bold = True
    paragraph.add_run(f" {body}")


def test_numbered_clauses_with_bold_titles(tmp_path):
    def build(doc):
        doc.add_heading("Cloud Service Agreement", level=0)
        numbered(doc, "8.1", "Liability Cap", "Each party's liability is capped.")
        numbered(doc, "5.3.a", "Breach", "If the other party fails to cure.")

    clauses = parse_docx(make_docx(tmp_path, build))

    assert [c.id for c in clauses] == ["8.1", "5.3.a"]
    assert clauses[0].title == "Liability Cap"
    assert clauses[0].text == "Liability Cap. Each party's liability is capped."


def test_headings_become_clauses_and_empty_section_headings_are_dropped(tmp_path):
    def build(doc):
        doc.add_heading("Key Terms", level=1)
        doc.add_heading("Governing Law", level=2)
        doc.add_paragraph("State of Delaware.")

    clauses = parse_docx(make_docx(tmp_path, build))

    assert len(clauses) == 1
    assert (clauses[0].id, clauses[0].text) == ("Governing Law", "State of Delaware.")


def test_unnumbered_paragraph_joins_previous_clause(tmp_path):
    def build(doc):
        numbered(doc, "1", "Scope", "Provider will migrate data.")
        doc.add_paragraph("This includes up to 2 million records.")

    clauses = parse_docx(make_docx(tmp_path, build))

    assert len(clauses) == 1
    assert clauses[0].text.endswith("Provider will migrate data.\nThis includes up to 2 million records.")


def test_plain_numbers_in_text_do_not_start_a_clause(tmp_path):
    def build(doc):
        numbered(doc, "2", "Term", "This agreement lasts for")
        doc.add_paragraph("12 months from the Effective Date.")

    clauses = parse_docx(make_docx(tmp_path, build))

    assert [c.id for c in clauses] == ["2"]


def test_repeated_ids_are_made_unique(tmp_path):
    def build(doc):
        numbered(doc, "1", "First", "One.")
        numbered(doc, "1", "Again", "Two.")

    assert [c.id for c in parse_docx(make_docx(tmp_path, build))] == ["1", "1 (2)"]


def test_real_template_parses():
    from pathlib import Path

    msa = Path(__file__).resolve().parent.parent / "data" / "contracts" / "base" / "msa.docx"
    clauses = {c.id: c for c in parse_docx(msa)}

    assert clauses["8.1"].title == "Liability Cap"
    assert "12 months" in clauses["General Cap Amount"].text
