"""Generate professional PDF documents for cover letters and tailored CVs."""

from __future__ import annotations

import logging
import re
from io import BytesIO

from fpdf import FPDF

logger = logging.getLogger(__name__)

_FONT = "Helvetica"
_PAGE_W = 210  # A4 mm
_MARGIN = 20
_CONTENT_W = _PAGE_W - 2 * _MARGIN

_UNICODE_REPLACE = {
    "•": "-",  # bullet
    "–": "-",  # en dash
    "—": "--",  # em dash
    "‘": "'",  # left single quote
    "’": "'",  # right single quote
    "“": '"',  # left double quote
    "”": '"',  # right double quote
    "…": "...",  # ellipsis
    "₹": "Rs.",  # rupee sign
    "′": "'",  # prime
    "″": '"',  # double prime
}


def _sanitize(text: str) -> str:
    """Replace unicode chars that latin-1 core fonts can't render."""
    for char, replacement in _UNICODE_REPLACE.items():
        text = text.replace(char, replacement)
    return text


class _BasePDF(FPDF):
    """Base PDF with consistent styling."""

    def __init__(self) -> None:
        super().__init__()
        self.set_auto_page_break(auto=True, margin=25)
        self.set_margins(_MARGIN, _MARGIN, _MARGIN)

    def _write_section(self, title: str, body: str) -> None:
        self.set_font(_FONT, "B", 11)
        self.set_text_color(30, 60, 110)
        self.cell(0, 7, _sanitize(title.upper()), new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(30, 60, 110)
        self.line(self.x, self.y, self.x + _CONTENT_W, self.y)
        self.ln(3)

        self.set_font(_FONT, "", 10)
        self.set_text_color(40, 40, 40)
        for line in _sanitize(body).strip().split("\n"):
            line = line.strip()
            if not line:
                self.ln(3)
                continue
            if line.startswith(("- ", "* ")):
                self.set_x(_MARGIN + 5)
                bullet_text = line.lstrip("-* ").strip()
                self.multi_cell(_CONTENT_W - 5, 5, f"  -  {bullet_text}")
            else:
                self.multi_cell(_CONTENT_W, 5, line)
        self.ln(4)


def generate_cover_letter_pdf(
    cover_letter: str,
    job_title: str,
    company: str,
    candidate_name: str = "",
) -> bytes:
    """Generate a professional cover letter PDF.

    Returns PDF as bytes (ready for download).
    """
    pdf = _BasePDF()
    pdf.add_page()

    # Header
    if candidate_name:
        pdf.set_font(_FONT, "B", 16)
        pdf.set_text_color(30, 60, 110)
        pdf.cell(0, 10, candidate_name, new_x="LMARGIN", new_y="NEXT", align="C")
        pdf.ln(2)

    # Subtitle
    pdf.set_font(_FONT, "", 9)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 5, _sanitize(f"Cover Letter  |  {job_title} at {company}"), new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.set_draw_color(30, 60, 110)
    pdf.line(_MARGIN, pdf.y + 2, _PAGE_W - _MARGIN, pdf.y + 2)
    pdf.ln(8)

    # Body
    pdf.set_font(_FONT, "", 10.5)
    pdf.set_text_color(40, 40, 40)

    for paragraph in _sanitize(cover_letter).strip().split("\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            pdf.ln(4)
            continue
        pdf.multi_cell(_CONTENT_W, 5.5, paragraph)
        pdf.ln(2)

    return bytes(pdf.output())


def generate_tailored_cv_pdf(
    tailored_cv_text: str,
    candidate_name: str = "",
    contact_info: str = "",
) -> bytes:
    """Generate a professional tailored CV PDF.

    Parses section headers (lines ending with : or ALL CAPS lines) and
    bullet points to create structured formatting.

    Returns PDF as bytes.
    """
    pdf = _BasePDF()
    pdf.add_page()

    # Header
    if candidate_name:
        pdf.set_font(_FONT, "B", 18)
        pdf.set_text_color(30, 60, 110)
        pdf.cell(0, 10, candidate_name, new_x="LMARGIN", new_y="NEXT", align="C")

    if contact_info:
        pdf.set_font(_FONT, "", 9)
        pdf.set_text_color(80, 80, 80)
        pdf.cell(0, 5, _sanitize(contact_info), new_x="LMARGIN", new_y="NEXT", align="C")

    if candidate_name or contact_info:
        pdf.set_draw_color(30, 60, 110)
        pdf.line(_MARGIN, pdf.y + 2, _PAGE_W - _MARGIN, pdf.y + 2)
        pdf.ln(6)

    # Parse and render sections
    sections = _parse_cv_sections(tailored_cv_text)

    for title, body in sections:
        if title:
            pdf._write_section(title, body)
        else:
            # Free-form text (e.g., professional summary before first heading)
            pdf.set_font(_FONT, "", 10)
            pdf.set_text_color(40, 40, 40)
            pdf.multi_cell(_CONTENT_W, 5, _sanitize(body).strip())
            pdf.ln(4)

    return bytes(pdf.output())


def _parse_cv_sections(text: str) -> list[tuple[str, str]]:
    """Parse CV text into (section_title, section_body) pairs.

    Detects section headers by patterns:
    - ALL CAPS lines (e.g., PROFESSIONAL EXPERIENCE)
    - Lines ending with colon (e.g., Core Competencies:)
    - Lines starting with ## (markdown headers)
    """
    lines = text.strip().split("\n")
    sections: list[tuple[str, str]] = []
    current_title = ""
    current_body: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            current_body.append("")
            continue

        # Remove markdown header markers
        clean = re.sub(r"^#{1,3}\s*", "", stripped)

        is_header = (
            (len(clean) > 2 and clean == clean.upper() and not clean.startswith(("- ", "• ", "* ")))
            or (clean.endswith(":") and len(clean) < 60 and not clean.startswith(("- ", "• ")))
            or stripped.startswith(("## ", "### "))
        )

        if is_header:
            if current_title or current_body:
                sections.append((current_title, "\n".join(current_body)))
            current_title = clean.rstrip(":")
            current_body = []
        else:
            current_body.append(stripped)

    if current_title or current_body:
        sections.append((current_title, "\n".join(current_body)))

    return sections


def generate_recruiter_message_pdf(
    messages: list[dict[str, str]],
    candidate_name: str = "",
) -> bytes:
    """Generate a PDF with recruiter outreach messages for multiple jobs.

    Each dict in messages should have: job_title, company, message
    """
    pdf = _BasePDF()
    pdf.add_page()

    # Title page header
    pdf.set_font(_FONT, "B", 16)
    pdf.set_text_color(30, 60, 110)
    title = f"Recruiter Outreach Messages -- {candidate_name}" if candidate_name else "Recruiter Outreach Messages"
    pdf.cell(0, 10, _sanitize(title), new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.set_draw_color(30, 60, 110)
    pdf.line(_MARGIN, pdf.y + 2, _PAGE_W - _MARGIN, pdf.y + 2)
    pdf.ln(8)

    for i, msg in enumerate(messages, 1):
        pdf.set_font(_FONT, "B", 11)
        pdf.set_text_color(30, 60, 110)
        pdf.cell(0, 7, _sanitize(f"{i}. {msg.get('job_title', '')} -- {msg.get('company', '')}"), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

        pdf.set_font(_FONT, "", 10)
        pdf.set_text_color(40, 40, 40)
        pdf.multi_cell(_CONTENT_W, 5, _sanitize(msg.get("message", "")).strip())
        pdf.ln(6)

    return bytes(pdf.output())
