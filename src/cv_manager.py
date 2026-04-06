"""CV management — parsing, text extraction, and AI-assisted CV selection."""

from __future__ import annotations

import logging
from pathlib import Path

import anthropic

from src.config import AppConfig, Credentials, PROJECT_ROOT

logger = logging.getLogger(__name__)


def extract_text_from_pdf(path: Path) -> str:
    """Extract text from a PDF file."""
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    text_parts = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            text_parts.append(text)
    return "\n".join(text_parts)


def extract_text_from_docx(path: Path) -> str:
    """Extract text from a DOCX file."""
    import docx

    doc = docx.Document(str(path))
    return "\n".join(para.text for para in doc.paragraphs if para.text.strip())


def extract_cv_text(path: Path) -> str:
    """Extract text from CV file (PDF or DOCX)."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return extract_text_from_pdf(path)
    elif suffix in (".docx", ".doc"):
        return extract_text_from_docx(path)
    else:
        return path.read_text(encoding="utf-8")


def load_all_cvs(config: AppConfig) -> dict[str, str]:
    """Load and extract text from all configured CV versions.

    Returns dict mapping cv_name -> extracted_text.
    """
    cv_dir = PROJECT_ROOT / config.cvs.directory
    cvs: dict[str, str] = {}

    for version in config.cvs.versions:
        cv_path = cv_dir / version.file
        if cv_path.exists():
            try:
                cvs[version.name] = extract_cv_text(cv_path)
                logger.info("Loaded CV: %s (%s)", version.name, version.file)
            except Exception as e:
                logger.warning("Failed to load CV %s: %s", version.name, e)
        else:
            logger.warning("CV file not found: %s", cv_path)

    return cvs


def select_best_cv(
    job_title: str,
    job_description: str,
    cv_texts: dict[str, str],
    config: AppConfig,
    creds: Credentials,
) -> tuple[str, str]:
    """Use Claude to select the best CV for a job.

    Returns (cv_name, reasoning).
    """
    if not cv_texts:
        raise ValueError("No CVs loaded")

    if len(cv_texts) == 1:
        name = next(iter(cv_texts))
        return name, "Only one CV available"

    cv_summaries = "\n\n".join(
        f"--- CV: {name} ---\n{text[:2000]}" for name, text in cv_texts.items()
    )

    prompt = f"""Given this job posting, select the best CV to use for the application.

Job Title: {job_title}
Job Description:
{job_description[:3000]}

Available CVs:
{cv_summaries}

Respond in exactly this format:
SELECTED: <cv_name>
REASON: <one sentence explanation>"""

    client = anthropic.Anthropic(api_key=creds.anthropic_api_key)
    response = client.messages.create(
        model=config.matching.ai_model,
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text  # type: ignore[union-attr]
    selected = ""
    reason = ""

    for line in text.strip().split("\n"):
        if line.startswith("SELECTED:"):
            selected = line.split(":", 1)[1].strip()
        elif line.startswith("REASON:"):
            reason = line.split(":", 1)[1].strip()

    # Validate selection
    if selected not in cv_texts:
        selected = next(iter(cv_texts))
        reason = f"AI selected unknown CV, defaulting to {selected}"

    return selected, reason
