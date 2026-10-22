"""Signing: turn the agreed version into a signed PDF with a certificate page.

The signer sits behind a tiny interface so a real e-signature provider (DocuSign,
Dropbox Sign) can replace the built-in one without touching the rest of the app.
The built-in signer records a typed-name electronic signature: who signed, when,
and the SHA-256 of the exact file signed, listed on a certificate page like
DocuSign's "certificate of completion".
"""

from pathlib import Path
from typing import Protocol

from fpdf import FPDF

from legal_agent.docx_parser import parse_docx

# The PDF core fonts only cover Latin-1, so typographic characters are mapped to plain ones.
PLAIN = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", "…": "...", "‎": "", "→": "->"})


class Signer(Protocol):
    def sign(self, docx_path: Path, out_path: Path, certificate: dict) -> Path: ...


class BuiltinSigner:
    def sign(self, docx_path: Path, out_path: Path, certificate: dict) -> Path:
        pdf = FPDF()
        pdf.set_auto_page_break(True, margin=18)
        pdf.add_page()
        _text(pdf, certificate["document"], 14, bold=True)
        pdf.ln(2)
        for clause in parse_docx(docx_path):
            label = "" if clause.id == clause.title else f"{clause.id}. "
            heading = f"{clause.title}. " if clause.id == clause.title else ""
            _text(pdf, label + heading + clause.text, 10)
            pdf.ln(1)
        pdf.ln(6)  # the signature block: "/s/ Name" is the usual form of a typed e-signature
        _text(pdf, f"Signed for Acme Cloud Inc.:  /s/ {certificate['signature']}", 11, style="I")
        _text(pdf, certificate["fields"]["Signed at"], 9)

        pdf.add_page()
        _text(pdf, "Certificate of completion", 14, bold=True)
        pdf.ln(2)
        for key, value in certificate["fields"].items():
            _text(pdf, f"{key}: {value}", 10)
        pdf.ln(3)
        _text(pdf, "Version history", 11, bold=True)
        for line in certificate["versions"]:
            _text(pdf, line, 9)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        pdf.output(str(out_path))
        return out_path


def _text(pdf: FPDF, text: str, size: int, bold: bool = False, style: str = ""):
    pdf.set_font("Helvetica", "B" if bold else style, size)
    plain = text.translate(PLAIN).encode("latin-1", "replace").decode("latin-1")
    pdf.multi_cell(0, size * 0.5, plain, new_x="LMARGIN", new_y="NEXT")


SIGNER: Signer = BuiltinSigner()
