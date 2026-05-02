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

# Characters that latin-1 core fonts cannot render — map to safe ASCII equivalents.
# Keys are the unicode characters; values are their replacements.
_UNICODE_REPLACE: dict[str, str] = {}
_UNICODE_REPLACE["•"] = "-"    # bullet •
_UNICODE_REPLACE["–"] = "-"    # en dash –
_UNICODE_REPLACE["—"] = "--"   # em dash —
_UNICODE_REPLACE["‘"] = "'"    # left single quote '
_UNICODE_REPLACE["’"] = "'"    # right single quote '
_UNICODE_REPLACE["“"] = '"'    # left double quote "
_UNICODE_REPLACE["”"] = '"'    # right double quote "
_UNICODE_REPLACE["…"] = "..."  # ellipsis …
_UNICODE_REPLACE["₹"] = "Rs."  # rupee sign ₹
_UNICODE_REPLACE["′"] = "'"    # prime ′
_UNICODE_REPLACE["″"] = '"'    # double prime ″
_UNICODE_REPLACE["®"] = ""     # registered trademark ® — strip
_UNICODE_REPLACE["™"] = ""     # trademark ™ — strip
_UNICODE_REPLACE["©"] = ""     # copyright © — strip


def _sanitize(text: str) -> str:
    """Replace unicode chars that latin-1 core fonts cannot render."""
    for char, replacement in _UNICODE_REPLACE.items():
        text = text.replace(char, replacement)
    return text


class _BasePDF(FPDF):
    """Base PDF with consistent styling."""

    def __init__(self) -> None:
        super().__init__()
        self.set_auto_page_break(auto=True, margin=15)
        self.set_margins(_MARGIN, 12, _MARGIN)

    def _section_fits(self, body: str, min_lines: int = 3) -> bool:
        """Check if section header + first few body lines fit on this page."""
        header_h = 8
        line_h = 4.5
        needed = header_h + (min_lines * line_h)
        return not self.will_page_break(needed)

    def _write_section(self, title: str, body: str) -> None:
        if not self._section_fits(body):
            self.add_page()

        self.set_font(_FONT, "B", 10.5)
        self.set_text_color(30, 60, 110)
        self.cell(0, 6, _sanitize(title.upper()), new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(30, 60, 110)
        self.line(self.x, self.y, self.x + _CONTENT_W, self.y)
        self.ln(1.5)

        self.set_font(_FONT, "", 9.5)
        self.set_text_color(40, 40, 40)
        lines = _sanitize(body).strip().split("\n")
        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                self.ln(1.5)
                continue

            is_subheader = "|" in line and not line.startswith(("- ", "* ")) and len(line) < 120
            is_bullet = line.startswith(("- ", "* "))

            if is_subheader:
                following_lines = min(3, len(lines) - i - 1)
                needed = 5 + (following_lines * 4.5)
                if self.will_page_break(needed):
                    self.add_page()
                self.set_font(_FONT, "B", 9.5)
                self.set_text_color(50, 50, 50)
                self.set_x(self.l_margin)
                self.multi_cell(_CONTENT_W, 4.5, line, new_x="LMARGIN", new_y="NEXT")
                self.set_font(_FONT, "", 9.5)
                self.set_text_color(40, 40, 40)
            elif is_bullet:
                self.set_x(_MARGIN + 4)
                bullet_text = line.lstrip("-* ").strip()
                self.multi_cell(_CONTENT_W - 4, 4.5, f"  -  {bullet_text}", new_x="LMARGIN", new_y="NEXT")
            else:
                self.set_x(self.l_margin)
                self.multi_cell(_CONTENT_W, 4.5, line, new_x="LMARGIN", new_y="NEXT")
        self.ln(2.5)


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

    # Subtitle — strip trademark symbols from company/title display
    subtitle = _sanitize(f"Cover Letter  |  {job_title} at {company}")
    pdf.set_font(_FONT, "", 9)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 5, subtitle, new_x="LMARGIN", new_y="NEXT", align="C")
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
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(_CONTENT_W, 5.5, paragraph, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

    return bytes(pdf.output())


def generate_tailored_cv_pdf(
    tailored_cv_text: str,
    candidate_name: str = "",
    contact_info: str = "",
) -> bytes:
    """Generate a professional tailored CV PDF.

    Parses section headers (lines ending with : or ALL CAPS lines) and
    bullet points to create structured formatting. Uses compact spacing
    to avoid orphan lines spilling onto the next page.

    Returns PDF as bytes.
    """
    sections = _parse_cv_sections(tailored_cv_text)

    # Try standard spacing first; if it overflows to 2+ pages and the
    # overflow is small (< 30% of page 2), retry with tighter spacing.
    for attempt, spacing in enumerate([(4.5, 2.5, 1.5), (4.0, 2.0, 1.0)]):
        line_h, section_gap, blank_gap = spacing
        pdf = _BasePDF()
        if attempt > 0:
            pdf.set_auto_page_break(auto=True, margin=12)
            pdf.set_margins(_MARGIN, 10, _MARGIN)
        pdf.add_page()

        if candidate_name:
            pdf.set_font(_FONT, "B", 16)
            pdf.set_text_color(30, 60, 110)
            pdf.cell(0, 8, candidate_name, new_x="LMARGIN", new_y="NEXT", align="C")

        if contact_info:
            pdf.set_font(_FONT, "", 8.5)
            pdf.set_text_color(80, 80, 80)
            pdf.cell(0, 4, _sanitize(contact_info), new_x="LMARGIN", new_y="NEXT", align="C")

        if candidate_name or contact_info:
            pdf.set_draw_color(30, 60, 110)
            pdf.line(_MARGIN, pdf.y + 1, _PAGE_W - _MARGIN, pdf.y + 1)
            pdf.ln(3)

        for title, body in sections:
            if title:
                pdf._write_section(title, body)
            else:
                pdf.set_font(_FONT, "", 9.5)
                pdf.set_text_color(40, 40, 40)
                pdf.set_x(pdf.l_margin)
                pdf.multi_cell(_CONTENT_W, line_h, _sanitize(body).strip(), new_x="LMARGIN", new_y="NEXT")
                pdf.ln(section_gap)

        # If it fits on 1 page, or if this is already the compact attempt, use it
        if pdf.page == 1 or attempt == 1:
            break
        # If page 2 has significant content (> 30% used), keep multi-page
        page2_usage = (pdf.get_y() - pdf.t_margin) / (pdf.h - pdf.t_margin - pdf.b_margin)
        if page2_usage > 0.30:
            break

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
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(_CONTENT_W, 5, _sanitize(msg.get("message", "")).strip(), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(6)

    return bytes(pdf.output())
