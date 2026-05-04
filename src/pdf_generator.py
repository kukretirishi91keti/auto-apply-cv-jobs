"""Generate professional PDF documents for cover letters and tailored CVs."""

from __future__ import annotations

import logging
import re
from io import BytesIO

from fpdf import FPDF

logger = logging.getLogger(__name__)

_FONT = "Helvetica"
_PAGE_W = 210  # A4 mm
_MARGIN = 18
_CONTENT_W = _PAGE_W - 2 * _MARGIN

# Brand colours
_NAVY = (25, 52, 103)       # deep navy for name, headers, accents
_DARK = (30, 30, 30)        # near-black for body text
_GREY = (110, 110, 110)     # grey for dates, subtitles
_LIGHT_LINE = (180, 195, 220)  # light blue-grey for dividers

# Characters that latin-1 core fonts cannot render — map to safe ASCII equivalents.
_UNICODE_REPLACE: dict[str, str] = {
    "•": "-",
    "–": "-",
    "—": "--",
    "‘": "'",  # left single quote
    "’": "'",  # right single quote
    "“": '"',  # left double quote
    "”": '"',  # right double quote
    "…": "...",
    "₹": "Rs.",
    "′": "'",
    "″": '"',
    "®": "",
    "™": "",
    "©": "",
    "•": "-",  # bullet
    "▸": ">",  # right-pointing triangle
    "–": "-",  # en dash (unicode)
    "—": "--", # em dash (unicode)
}


def _sanitize(text: str) -> str:
    """Replace unicode chars that latin-1 core fonts cannot render."""
    for char, replacement in _UNICODE_REPLACE.items():
        text = text.replace(char, replacement)
    return text


def _parse_job_entry(line: str) -> tuple[str, str, str] | None:
    """Try to parse 'Company | Role | Date' or 'Company | Role' job entry.

    Returns (company, role, date) or None if line is not a job entry.
    Job entries have 2-3 pipe-separated parts, each relatively short.
    Skill lists have 4+ parts, so are excluded.
    """
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 2 or len(parts) > 3:
        return None
    # Each part must be reasonably short (not a long skill list)
    if any(len(p) > 60 for p in parts):
        return None
    # Must not be a bullet
    if line.strip().startswith(("-", "*")):
        return None
    company = parts[0]
    role = parts[1]
    date = parts[2] if len(parts) == 3 else ""
    return company, role, date


class _BasePDF(FPDF):
    """Base PDF with consistent colour helpers."""

    def __init__(self) -> None:
        super().__init__()
        self.set_auto_page_break(auto=True, margin=15)
        self.set_margins(_MARGIN, 12, _MARGIN)

    def _set_navy(self) -> None:
        self.set_text_color(*_NAVY)

    def _set_dark(self) -> None:
        self.set_text_color(*_DARK)

    def _set_grey(self) -> None:
        self.set_text_color(*_GREY)

    def _section_fits(self, min_lines: int = 3) -> bool:
        header_h = 9
        line_h = 4.5
        needed = header_h + (min_lines * line_h)
        return not self.will_page_break(needed)

    def _draw_section_header(self, title: str) -> None:
        """Draw a styled section header with left accent bar and underline."""
        if not self._section_fits():
            self.add_page()

        # Left accent bar
        self.set_fill_color(*_NAVY)
        self.rect(self.l_margin, self.y + 0.5, 3, 5.5, "F")

        # Section title — bold, navy, 4 mm right of the bar
        self.set_font(_FONT, "B", 10)
        self._set_navy()
        self.set_x(self.l_margin + 5)
        self.cell(_CONTENT_W - 5, 5.5, _sanitize(title.upper()), new_x="LMARGIN", new_y="NEXT")

        # Thin underline
        self.set_draw_color(*_LIGHT_LINE)
        self.set_line_width(0.3)
        self.line(self.l_margin, self.y + 0.5, self.l_margin + _CONTENT_W, self.y + 0.5)
        self.set_line_width(0.2)
        self.ln(2.5)


class _ProfessionalCV(_BasePDF):
    """Professional CV renderer — formats job entries, bullets, skills neatly."""

    def name_header(self, name: str, contact: str = "") -> None:
        """Render candidate name and optional contact line."""
        self.set_font(_FONT, "B", 20)
        self._set_navy()
        self.cell(0, 9, _sanitize(name), new_x="LMARGIN", new_y="NEXT", align="C")

        if contact:
            self.set_font(_FONT, "", 8.5)
            self._set_grey()
            self.cell(0, 4.5, _sanitize(contact), new_x="LMARGIN", new_y="NEXT", align="C")

        # Full-width divider
        self.set_draw_color(*_NAVY)
        self.set_line_width(0.5)
        self.line(_MARGIN, self.y + 1.5, _PAGE_W - _MARGIN, self.y + 1.5)
        self.set_line_width(0.2)
        self.ln(4)

    def write_cv_section(self, title: str, body: str) -> None:
        """Render a named CV section with proper sub-formatting."""
        self._draw_section_header(title)
        self._render_body(body.strip().split("\n"), is_competencies="COMPETENC" in title.upper())
        self.ln(1.5)

    def _render_body(self, lines: list[str], is_competencies: bool = False) -> None:
        """Render body lines with smart detection of job entries, bullets, plain text."""
        i = 0
        while i < len(lines):
            raw = lines[i]
            line = raw.strip()

            if not line:
                self.ln(1.5)
                i += 1
                continue

            # Core Competencies: pipe-separated skills — render as wrapped text rows
            if is_competencies:
                self.set_font(_FONT, "", 9)
                self._set_dark()
                self.set_x(self.l_margin)
                self.multi_cell(_CONTENT_W, 4.5, _sanitize(line), new_x="LMARGIN", new_y="NEXT")
                self.ln(0.5)
                i += 1
                continue

            # Try to parse as a job entry header
            entry = _parse_job_entry(line)
            if entry:
                company, role, date = entry
                if self.will_page_break(12):
                    self.add_page()

                # Company name bold left, date grey right — on the same line
                self.set_font(_FONT, "B", 9.5)
                self._set_dark()
                self.set_x(self.l_margin)
                if date:
                    date_w = 42
                    self.cell(_CONTENT_W - date_w, 5, _sanitize(company))
                    self.set_font(_FONT, "", 8.5)
                    self._set_grey()
                    self.cell(date_w, 5, _sanitize(date), align="R", new_x="LMARGIN", new_y="NEXT")
                else:
                    self.cell(_CONTENT_W, 5, _sanitize(company), new_x="LMARGIN", new_y="NEXT")

                # Role in italic on the next line
                if role:
                    self.set_font(_FONT, "I", 9)
                    self._set_grey()
                    self.set_x(self.l_margin)
                    self.cell(_CONTENT_W, 4.5, _sanitize(role), new_x="LMARGIN", new_y="NEXT")

                self.ln(1)
                i += 1
                continue

            # Bullet point
            if line.startswith(("-", "*")):
                bullet_text = line.lstrip("-* ").strip()
                self.set_font(_FONT, "", 9)
                self._set_dark()
                # Draw bullet dash manually then multi_cell for wrapping
                self.set_x(self.l_margin + 3)
                self.multi_cell(
                    _CONTENT_W - 3,
                    4.5,
                    "- " + _sanitize(bullet_text),
                    new_x="LMARGIN",
                    new_y="NEXT",
                )
                i += 1
                continue

            # Plain paragraph
            self.set_font(_FONT, "", 9.5)
            self._set_dark()
            self.set_x(self.l_margin)
            self.multi_cell(_CONTENT_W, 4.8, _sanitize(line), new_x="LMARGIN", new_y="NEXT")
            i += 1


def generate_tailored_cv_pdf(
    tailored_cv_text: str,
    candidate_name: str = "",
    contact_info: str = "",
) -> bytes:
    """Generate a professional 2-page tailored CV PDF.

    Parses ALL CAPS section headers, job entry lines (Company | Role | Date),
    bullet points, and skills lists into a polished layout.

    Returns PDF as bytes.
    """
    sections = _parse_cv_sections(tailored_cv_text)

    pdf = _ProfessionalCV()
    pdf.add_page()

    if candidate_name:
        pdf.name_header(candidate_name, contact_info)

    for title, body in sections:
        if title:
            pdf.write_cv_section(title, body)
        else:
            # Preamble text before any section header
            pdf.set_font(_FONT, "", 9.5)
            pdf._set_dark()
            pdf.set_x(pdf.l_margin)
            pdf.multi_cell(_CONTENT_W, 4.8, _sanitize(body.strip()), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2)

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
            (len(clean) > 2 and clean == clean.upper() and not clean.startswith(("- ", "* ", "•")))
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


# ---------------------------------------------------------------------------
# Cover letter PDF
# ---------------------------------------------------------------------------

class _CoverLetterPDF(_BasePDF):
    """Clean cover letter PDF renderer."""

    def cover_header(self, candidate_name: str, subtitle: str) -> None:
        if candidate_name:
            self.set_font(_FONT, "B", 18)
            self._set_navy()
            self.cell(0, 9, _sanitize(candidate_name), new_x="LMARGIN", new_y="NEXT", align="C")

        self.set_font(_FONT, "", 9)
        self._set_grey()
        self.cell(0, 4.5, _sanitize(subtitle), new_x="LMARGIN", new_y="NEXT", align="C")

        self.set_draw_color(*_NAVY)
        self.set_line_width(0.4)
        self.line(_MARGIN, self.y + 1.5, _PAGE_W - _MARGIN, self.y + 1.5)
        self.set_line_width(0.2)
        self.ln(7)


def generate_cover_letter_pdf(
    cover_letter: str,
    job_title: str,
    company: str,
    candidate_name: str = "",
) -> bytes:
    """Generate a professional cover letter PDF.

    Returns PDF as bytes (ready for download).
    """
    pdf = _CoverLetterPDF()
    pdf.add_page()

    subtitle = _sanitize(f"Cover Letter  |  {job_title} at {company}")
    pdf.cover_header(candidate_name, subtitle)

    # Body
    pdf.set_font(_FONT, "", 10.5)
    pdf._set_dark()

    for paragraph in _sanitize(cover_letter).strip().split("\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            pdf.ln(4)
            continue
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(_CONTENT_W, 5.5, paragraph, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

    return bytes(pdf.output())


# ---------------------------------------------------------------------------
# Recruiter message PDF
# ---------------------------------------------------------------------------

def generate_recruiter_message_pdf(
    messages: list[dict[str, str]],
    candidate_name: str = "",
) -> bytes:
    """Generate a PDF with recruiter outreach messages for multiple jobs.

    Each dict in messages should have: job_title, company, message.
    """
    pdf = _BasePDF()
    pdf.add_page()

    # Title
    pdf.set_font(_FONT, "B", 16)
    pdf.set_text_color(*_NAVY)
    title = f"Recruiter Outreach -- {candidate_name}" if candidate_name else "Recruiter Outreach Messages"
    pdf.cell(0, 10, _sanitize(title), new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.set_draw_color(*_NAVY)
    pdf.set_line_width(0.4)
    pdf.line(_MARGIN, pdf.y + 2, _PAGE_W - _MARGIN, pdf.y + 2)
    pdf.set_line_width(0.2)
    pdf.ln(8)

    for i, msg in enumerate(messages, 1):
        pdf.set_font(_FONT, "B", 11)
        pdf.set_text_color(*_NAVY)
        pdf.cell(
            0, 7,
            _sanitize(f"{i}. {msg.get('job_title', '')} -- {msg.get('company', '')}"),
            new_x="LMARGIN", new_y="NEXT",
        )
        pdf.ln(2)

        pdf.set_font(_FONT, "", 10)
        pdf.set_text_color(*_DARK)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(_CONTENT_W, 5, _sanitize(msg.get("message", "")).strip(), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(6)

    return bytes(pdf.output())
