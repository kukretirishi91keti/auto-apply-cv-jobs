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
) -> str:
    """Generate a tailored cover letter using Claude."""
    name_line = ""
    if candidate_name:
        name_line = f"\nCandidate Name: {candidate_name}"
    else:
        name_line = "\nSign off with the candidate's name from the CV (never use '[Candidate Name]')."

    prompt = f"""Write a concise, professional cover letter for this job application.
Keep it under 250 words. Be specific about how the candidate's experience matches the role.
Do NOT use generic filler — reference actual skills from the CV that match the job.
Do NOT use placeholders like [Candidate Name] or [Your Name] — use the actual name.
Do NOT mention specific number of years of experience (e.g. "8 years", "10+ years").
Instead use phrases like "extensive experience" or "proven track record".
{name_line}

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
