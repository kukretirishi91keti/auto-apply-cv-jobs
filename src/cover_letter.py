"""AI-powered cover letter generation."""

from __future__ import annotations

import logging

import anthropic

from src.config import AppConfig, Credentials

logger = logging.getLogger(__name__)


def generate_cover_letter(
    job_title: str,
    company: str,
    job_description: str,
    cv_text: str,
    config: AppConfig,
    creds: Credentials,
    candidate_name: str = "",
    domain_emphasis: list[str] | None = None,
    extra_context: str = "",
) -> str:
    """Generate a tailored cover letter using Claude."""
    name_line = ""
    if candidate_name:
        name_line = f"\nCandidate Name: {candidate_name}"
    else:
        name_line = "\nSign off with the candidate's name from the CV (never use '[Candidate Name]')."

    edu_line = ""
    if config.education:
        edu_parts = []
        for e in config.education:
            if e.degree:
                parts = [e.degree]
                if e.institution:
                    parts.append(e.institution)
                if e.cgpa:
                    parts.append(e.cgpa)
                edu_parts.append(" from ".join(parts[:2]) + (f" ({e.cgpa})" if e.cgpa else ""))
        if edu_parts:
            edu_line = f"\nCandidate Education: {'; '.join(edu_parts)}"

    domain_line = ""
    if domain_emphasis:
        _DOMAIN_CL = {
            "Brand": "brand marketing, campaign management, consumer insights",
            "Digital / AI": "digital marketing, AI/automation tools, performance marketing",
            "Content": "content marketing, organic growth, content production at scale",
            "Trade Marketing": "trade marketing, BTL activations, channel programs",
            "P&L / Revenue": "P&L management, revenue growth, business efficiency",
        }
        parts = [_DOMAIN_CL[d] for d in domain_emphasis if d in _DOMAIN_CL]
        if parts:
            domain_line = f"\nFOCUS AREAS: Emphasise the candidate's experience in: {'; '.join(parts)}."

    extra_line = f"\nAdditional context: {extra_context}" if extra_context else ""

    cert_line = ""
    if config.certifications:
        cert_parts = []
        for c in config.certifications:
            if c.name:
                entry = c.name
                if c.issuer:
                    entry += f" ({c.issuer})"
                cert_parts.append(entry)
        if cert_parts:
            cert_line = f"\nCertifications & Achievements: {'; '.join(cert_parts)}"

    prompt = f"""Write a concise, professional cover letter for this job application.
Keep it under 250 words. Be specific about how the candidate's experience matches the role.
Do NOT use generic filler — reference actual skills from the CV that match the job.
Do NOT use placeholders like [Candidate Name] or [Your Name] — use the actual name.
Do NOT mention specific number of years of experience (e.g. "8 years", "10+ years").
Instead use phrases like "extensive experience" or "proven track record".

SALUTATION RULE: Address as "Dear [Company Name] Hiring Team," — use the actual company
name, never write "Dear Hiring Manager," or "To Whom It May Concern,".

METRIC RULE — CRITICAL:
- If CV says "from A to B": compute B/A and write "NX". Example: "from 40% to 74%" → "1.7X improvement". NEVER extract just the start number (do NOT write "40% improvement").
- If CV already says "1.7X" or "15X": copy it VERBATIM — do NOT convert back to a percentage.
- Totals and destinations are fine as-is (e.g. "Rs. 500 Cr AUM", "40,000 leads").
- Do NOT add fiscal years or time qualifiers (e.g. "FY25") not in the CV.
{name_line}{edu_line}{cert_line}{domain_line}{extra_line}

Job Title: {job_title}
Company: {company}
Job Description:
{job_description[:3000]}

Candidate CV:
{cv_text[:3000]}

Write the cover letter now (no preamble, just the letter):"""

    client = anthropic.Anthropic(api_key=creds.anthropic_api_key)
    response = client.messages.create(
        model=config.matching.ai_model,
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )

    letter = response.content[0].text.strip()  # type: ignore[union-attr]
    logger.info("Generated cover letter for %s at %s (%d chars)", job_title, company, len(letter))
    return letter
