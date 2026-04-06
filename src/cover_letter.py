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
) -> str:
    """Generate a tailored cover letter using Claude."""
    prompt = f"""Write a concise, professional cover letter for this job application.
Keep it under 250 words. Be specific about how the candidate's experience matches the role.
Do NOT use generic filler — reference actual skills from the CV that match the job.

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
