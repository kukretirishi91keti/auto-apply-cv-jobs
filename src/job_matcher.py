"""Two-stage job matching: keyword filter + Claude AI scoring."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import anthropic

from src.config import AppConfig, Credentials

logger = logging.getLogger(__name__)


@dataclass
class MatchResult:
    keyword_score: float
    ai_score: float | None = None
    recommended_cv: str | None = None
    reasoning: str = ""
    should_apply: bool = False


def keyword_score(job_title: str, job_description: str, keywords: list[str]) -> float:
    """Stage 1: Fast keyword matching (free).

    Returns 0.0–1.0. Uses ANY-match logic: if ANY keyword matches, the job
    passes. Score = proportion of keywords found, but a single match is enough
    to pass the threshold (returns at least 0.3 for any match).

    Keywords are split on commas to handle compound entries like
    "BFSI, FinTech, Life Insurance" as separate terms.
    """
    if not keywords:
        return 1.0  # no keywords configured = pass everything

    # Split compound keywords on commas
    terms: list[str] = []
    for kw in keywords:
        for part in kw.split(","):
            term = part.strip().lower()
            if term and term not in terms:
                terms.append(term)

    if not terms:
        return 1.0

    text = f"{job_title} {job_description}".lower()
    matches = sum(1 for term in terms if term in text)

    if matches == 0:
        return 0.0

    # Any match = at least 0.3 (passes default threshold)
    raw_score = matches / len(terms)
    return max(raw_score, 0.3)


def ai_score_job(
    job_title: str,
    job_description: str,
    cv_texts: dict[str, str],
    config: AppConfig,
    creds: Credentials,
) -> tuple[float, str, str]:
    """Stage 2: Claude AI scoring (paid).

    Returns (score, recommended_cv, reasoning).
    """
    cv_summaries = "\n".join(
        f"- {name}: {text[:1500]}..." for name, text in cv_texts.items()
    )

    prompt = f"""You are a senior recruiter. Rate how well this candidate matches this job.

Consider:
- Role level alignment (VP/Director/Manager/Associate)
- Industry/domain overlap (BFSI, FinTech, Insurance, Product, etc.)
- Skills and experience relevance
- Seniority match

Job Title: {job_title}
Job Description:
{job_description[:3000]}

Candidate CVs:
{cv_summaries}

Score 0.7+ if the candidate's experience is directly relevant.
Score 0.5-0.7 if there's partial overlap in domain or skills.
Score below 0.5 if the role is clearly misaligned.

Respond in exactly this format:
SCORE: <0.0-1.0>
CV: <cv_name>
REASON: <one sentence>"""

    client = anthropic.Anthropic(api_key=creds.anthropic_api_key)
    response = client.messages.create(
        model=config.matching.ai_model,
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text  # type: ignore[union-attr]
    score = 0.0
    cv_name = next(iter(cv_texts)) if cv_texts else ""
    reason = ""

    for line in text.strip().split("\n"):
        if line.startswith("SCORE:"):
            try:
                match = re.search(r"[\d.]+", line.split(":", 1)[1])
                score = float(match.group()) if match else 0.0
            except (ValueError, AttributeError):
                score = 0.0
        elif line.startswith("CV:"):
            cv_name = line.split(":", 1)[1].strip()
        elif line.startswith("REASON:"):
            reason = line.split(":", 1)[1].strip()

    # Validate CV name
    if cv_name not in cv_texts and cv_texts:
        cv_name = next(iter(cv_texts))

    return min(max(score, 0.0), 1.0), cv_name, reason


def match_job(
    job_title: str,
    job_description: str,
    cv_texts: dict[str, str],
    config: AppConfig,
    creds: Credentials,
) -> MatchResult:
    """Run two-stage matching pipeline."""
    # Stage 1: Keyword filter
    kw_score = keyword_score(job_title, job_description, config.search.keywords)

    if kw_score < config.matching.keyword_min_score:
        logger.debug("Job '%s' failed keyword filter (%.2f < %.2f)", job_title, kw_score, config.matching.keyword_min_score)
        return MatchResult(keyword_score=kw_score, should_apply=False)

    # Stage 2: AI scoring
    try:
        score, cv_name, reason = ai_score_job(job_title, job_description, cv_texts, config, creds)
    except Exception as e:
        logger.warning("AI scoring failed for '%s': %s", job_title, e)
        return MatchResult(keyword_score=kw_score, should_apply=False, reasoning=str(e))

    should_apply = score >= config.matching.ai_min_score

    return MatchResult(
        keyword_score=kw_score,
        ai_score=score,
        recommended_cv=cv_name,
        reasoning=reason,
        should_apply=should_apply,
    )
