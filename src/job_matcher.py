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
    used_ai: bool = False


_ABBREVIATIONS = {
    "avp": "assistant vice president",
    "vp": "vice president",
    "svp": "senior vice president",
    "evp": "executive vice president",
    "cmo": "chief marketing officer",
    "coo": "chief operating officer",
    "ceo": "chief executive officer",
    "cfo": "chief financial officer",
    "cto": "chief technology officer",
    "gm": "general manager",
    "agm": "assistant general manager",
    "dgm": "deputy general manager",
    "gtm": "go to market",
    "bfsi": "banking financial services insurance",
}


_ABBR_BY_LENGTH = sorted(_ABBREVIATIONS.items(), key=lambda x: -len(x[1]))


def _expand_text(text: str) -> set[str]:
    """Return all words from text plus abbreviation expansions (both directions)."""
    text_lower = text.lower()
    words = set(re.findall(r"[a-z0-9]+", text_lower))
    extra: set[str] = set()
    consumed: set[str] = set()
    for abbr, full in _ABBR_BY_LENGTH:
        if abbr in words:
            extra.update(full.split())
        if full in text_lower and abbr not in consumed:
            extra.add(abbr)
            for other_abbr, other_full in _ABBR_BY_LENGTH:
                if other_full != full and other_full in full:
                    consumed.add(other_abbr)
    return words | extra


def keyword_score(job_title: str, job_description: str, keywords: list[str]) -> float:
    """Stage 1: Fast keyword matching (free).

    Returns 0.0-1.0. Uses word-level matching with abbreviation expansion.
    Each keyword phrase is split into words, and ALL words must appear in
    the text (in any order). Handles: "VP of Growth" ↔ "VP Growth",
    "Assistant Vice President" ↔ "AVP", etc.
    """
    if not keywords:
        return 1.0

    # Split compound keywords on commas
    terms: list[str] = []
    for kw in keywords:
        for part in kw.split(","):
            term = part.strip().lower()
            if term and term not in terms:
                terms.append(term)

    if not terms:
        return 1.0

    raw_text = f"{job_title} {job_description}"
    text_words = _expand_text(raw_text)

    matches = 0
    for term in terms:
        term_words = _expand_text(term)
        if term_words and term_words.issubset(text_words):
            matches += 1

    if matches == 0:
        return 0.0

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

    exp_years = config.search.experience_years
    seniority = config.search.seniority_levels

    seniority_hint = ""
    if exp_years or seniority:
        parts = []
        if exp_years:
            parts.append(f"{exp_years}+ years of experience")
        if seniority:
            parts.append(f"targeting {'/'.join(seniority)}-level roles")
        seniority_hint = f"\nCandidate Profile: {', '.join(parts)}."

    prompt = f"""You are a senior recruiter. Rate how well this candidate matches this job.
{seniority_hint}
STRICT RULES:
- Score 0.3 or below if the role is junior/entry-level and the candidate has {exp_years}+ years
- Score 0.3 or below if the role is in a completely unrelated industry/domain
- GEOGRAPHY: The candidate is based in INDIA. Score 0.3 or below if the job is based in
  USA, Europe, or any country outside India — UNLESS the job description explicitly says
  "Remote - Worldwide", "Remote - India", or similar global remote policy. A US company
  listing as "Remote" without specifying international eligibility should score 0.3.
- Score 0.5-0.7 if there's partial overlap in domain or transferable skills
- Score 0.7+ ONLY if the role level, domain, AND skills are directly relevant

Consider:
- Role level alignment — a {exp_years}-year candidate should NOT match Associate/Junior roles
- Industry/domain overlap (BFSI, FinTech, Insurance, Marketing, Product, Growth)
- Skills and experience relevance
- Location: candidate is in India. US-only, Europe-only roles score 0.3 even if "Remote"

Job Title: {job_title}
Job Description:
{job_description[:3000]}

Candidate CVs:
{cv_summaries}

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


_JUNIOR_TITLE_PATTERNS = [
    "intern", "trainee", "apprentice", "fresher", "entry level",
    "junior", "associate analyst", "graduate trainee",
]

_NON_INDIA_LOCATIONS = [
    "united states", "usa", ", us", "new york", "san francisco",
    "los angeles", "chicago", "boston", "seattle", "austin", "denver",
    "atlanta", "miami", "dallas", "houston", "phoenix", "portland",
    "san diego", "philadelphia", "washington dc", "washington, dc",
    "california", "texas", "florida", "illinois", "colorado",
    "united kingdom", ", uk", "london", "manchester", "berlin",
    "paris", "amsterdam", "toronto", "vancouver", "sydney",
    "melbourne", "singapore", "hong kong", "tokyo", "dubai",
]


def _is_non_india_location(location: str) -> bool:
    """Quick check: reject jobs with clearly non-India locations."""
    if not location:
        return False
    loc_lower = location.lower()
    india_hints = ["india", "bangalore", "bengaluru", "mumbai", "delhi",
                   "hyderabad", "chennai", "pune", "gurugram", "gurgaon",
                   "noida", "kolkata", "ahmedabad", "jaipur"]
    if any(h in loc_lower for h in india_hints):
        return False
    return any(p in loc_lower for p in _NON_INDIA_LOCATIONS)


def _is_seniority_mismatch(job_title: str, experience_years: int) -> bool:
    """Quick check: reject obviously junior roles for senior candidates."""
    if experience_years < 7:
        return False
    title_lower = job_title.lower()
    return any(pat in title_lower for pat in _JUNIOR_TITLE_PATTERNS)


def match_job(
    job_title: str,
    job_description: str,
    cv_texts: dict[str, str],
    config: AppConfig,
    creds: Credentials,
    job_location: str = "",
) -> MatchResult:
    """Run two-stage matching pipeline."""
    # Stage 0a: Seniority filter (free, instant)
    if _is_seniority_mismatch(job_title, config.search.experience_years):
        logger.debug("Job '%s' rejected — junior title for %d-year candidate", job_title, config.search.experience_years)
        return MatchResult(keyword_score=0.0, should_apply=False)

    # Stage 0b: Location filter (free, instant) — skip obviously non-India jobs
    if _is_non_india_location(job_location):
        logger.debug("Job '%s' rejected — non-India location: %s", job_title, job_location)
        return MatchResult(keyword_score=0.0, should_apply=False)

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
        used_ai=True,
    )
