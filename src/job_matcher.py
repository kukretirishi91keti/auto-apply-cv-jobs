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
    fast_passed: bool = False   # NEW: True if title fast-pass bypassed keyword filter


# ---------------------------------------------------------------------------
# Abbreviation expansion
# ---------------------------------------------------------------------------

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
    "d2c": "direct to consumer",
    "b2c": "business to consumer",
    "b2b": "business to business",
    "plg": "product led growth",
    "crm": "customer relationship management",
    "roi": "return on investment",
    "kpi": "key performance indicator",
    "p&l": "profit and loss",
    "seo": "search engine optimisation",
    "sem": "search engine marketing",
    "atl": "above the line",
    "btl": "below the line",
    "imc": "integrated marketing communications",
}

_ABBR_BY_LENGTH = sorted(_ABBREVIATIONS.items(), key=lambda x: -len(x[1]))


def _expand_text(text: str) -> set[str]:
    """Return all words from text plus abbreviation expansions (both directions)."""
    text_lower = text.lower()
    words = set(re.findall(r"[a-z0-9&]+", text_lower))
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


# ---------------------------------------------------------------------------
# Title fast-pass
# ---------------------------------------------------------------------------

# If job title contains a seniority signal + domain signal → send to AI directly,
# bypassing keyword scoring. One word from each set is enough.
_SENIORITY_SIGNALS = {
    "vp", "vice", "president",
    "svp", "evp", "avp",
    "head",
    "director",
    "chief",
    "cmo", "coo", "ceo", "cto",
    "lead",          # "Marketing Lead" at senior level
    "principal",
}

_DOMAIN_SIGNALS = {
    # Marketing & Brand
    "marketing", "brand", "branding",
    # Digital & Growth
    "digital", "growth", "acquisition",
    "performance", "demand", "funnel",
    # Product
    "product", "platform",
    # BFSI / Sector
    "insurance", "bfsi", "fintech", "insurtech",
    "banking", "financial", "wealth",
    # Consulting / Strategy
    "consulting", "strategy", "transformation",
    "advisory", "business",
    # Content & Comms
    "content", "communications", "pr",
    "media",
}


def _title_fast_pass(job_title: str) -> bool:
    """Return True if job title alone warrants sending to AI.

    Rule: must have at least one seniority word AND one domain word.
    This catches roles like:
      - "VP – Brand & Growth"  (has "vp" + "brand")
      - "Head of Digital"      (has "head" + "digital")
      - "Director – FinTech Marketing" (has "director" + "fintech"/"marketing")
      - "Chief Marketing Officer" (has "chief" + "marketing")
    """
    title_words = _expand_text(job_title)
    has_seniority = bool(title_words & _SENIORITY_SIGNALS)
    has_domain = bool(title_words & _DOMAIN_SIGNALS)
    return has_seniority and has_domain


# ---------------------------------------------------------------------------
# Keyword scoring  (Stage 1 — only runs if fast-pass fails)
# ---------------------------------------------------------------------------

def keyword_score(job_title: str, job_description: str, keywords: list[str]) -> float:
    """Stage 1: Fast keyword matching (free).

    CHANGED v2: Uses OR logic within multi-word terms.
    A term like "VP Marketing" now matches if ANY of its words ("vp" OR "marketing")
    appear in the text — not requiring all words.  This stops "VP – Brand & Growth"
    being rejected because "marketing" wasn't literally present.

    Returns 0.0–1.0.
    """
    if not keywords:
        return 1.0

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
        # OR logic: match if ANY word from the term appears in text
        if term_words and term_words & text_words:
            matches += 1

    if matches == 0:
        return 0.0

    raw_score = matches / len(terms)
    return max(raw_score, 0.3)


# ---------------------------------------------------------------------------
# AI scoring  (Stage 2)
# ---------------------------------------------------------------------------

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

    if cv_name not in cv_texts and cv_texts:
        cv_name = next(iter(cv_texts))

    return min(max(score, 0.0), 1.0), cv_name, reason


# ---------------------------------------------------------------------------
# Pre-filters  (Stage 0 — free, instant)
# ---------------------------------------------------------------------------

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
    "melbourne", "tokyo",
    # NOTE: singapore, hong kong, dubai removed — Rishi is open to global relocation
    # and these are plausible targets. AI scorer handles them with geography rules.
]


def _is_non_india_location(location: str) -> bool:
    """Quick check: reject jobs with clearly non-India / non-target locations."""
    if not location:
        return False
    loc_lower = location.lower()
    india_hints = [
        "india", "bangalore", "bengaluru", "mumbai", "delhi",
        "hyderabad", "chennai", "pune", "gurugram", "gurgaon",
        "noida", "kolkata", "ahmedabad", "jaipur", "remote",
        "singapore", "dubai", "hong kong",   # kept — open to global relocation
    ]
    if any(h in loc_lower for h in india_hints):
        return False
    return any(p in loc_lower for p in _NON_INDIA_LOCATIONS)


def _is_seniority_mismatch(job_title: str, experience_years: int) -> bool:
    """Quick check: reject obviously junior roles for senior candidates."""
    if experience_years < 7:
        return False
    title_lower = job_title.lower()
    return any(pat in title_lower for pat in _JUNIOR_TITLE_PATTERNS)


def _is_excluded_title(job_title: str, excluded_patterns: list[str]) -> bool:
    """Check config excluded_title_patterns."""
    title_lower = job_title.lower()
    return any(pat.lower() in title_lower for pat in excluded_patterns)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def match_job(
    job_title: str,
    job_description: str,
    cv_texts: dict[str, str],
    config: AppConfig,
    creds: Credentials,
    job_location: str = "",
) -> MatchResult:
    """Run two-stage matching pipeline.

    Stage 0a — seniority mismatch filter   (free, instant)
    Stage 0b — location filter             (free, instant)
    Stage 0c — excluded title filter       (free, instant)
    Stage 0d — title fast-pass check       (free, instant)
      → if title has seniority + domain signal: skip to Stage 2
      → else: run Stage 1
    Stage 1  — keyword filter              (free, fast)
    Stage 2  — Claude AI scoring           (paid, smart)
    """

    # --- Stage 0a: Junior seniority filter ---
    if _is_seniority_mismatch(job_title, config.search.experience_years):
        logger.debug(
            "Job '%s' rejected — junior title for %d-year candidate",
            job_title, config.search.experience_years,
        )
        return MatchResult(keyword_score=0.0, should_apply=False)

    # --- Stage 0b: Location filter ---
    if _is_non_india_location(job_location):
        logger.debug("Job '%s' rejected — non-target location: %s", job_title, job_location)
        return MatchResult(keyword_score=0.0, should_apply=False)

    # --- Stage 0c: Config excluded title patterns ---
    excluded_patterns = getattr(config.search, "excluded_title_patterns", [])
    if excluded_patterns and _is_excluded_title(job_title, excluded_patterns):
        logger.debug("Job '%s' rejected — excluded title pattern", job_title)
        return MatchResult(keyword_score=0.0, should_apply=False)

    # --- Stage 0d: Title fast-pass ---
    # If the title already signals seniority + domain, skip keyword scoring
    # and go straight to AI.  This prevents good jobs being dropped because
    # their description doesn't happen to contain our keyword phrases.
    fast_passed = _title_fast_pass(job_title)

    if fast_passed:
        logger.debug("Job '%s' fast-passed to AI (title signal matched)", job_title)
        kw_score = 0.5   # neutral score — wasn't keyword-filtered
    else:
        # --- Stage 1: Keyword filter ---
        kw_score = keyword_score(job_title, job_description, config.search.keywords)

        if kw_score < config.matching.keyword_min_score:
            logger.debug(
                "Job '%s' failed keyword filter (%.2f < %.2f)",
                job_title, kw_score, config.matching.keyword_min_score,
            )
            return MatchResult(keyword_score=kw_score, should_apply=False)

    # --- Stage 2: AI scoring ---
    try:
        score, cv_name, reason = ai_score_job(
            job_title, job_description, cv_texts, config, creds
        )
    except Exception as e:
        logger.warning("AI scoring failed for '%s': %s", job_title, e)
        return MatchResult(
            keyword_score=kw_score,
            should_apply=False,
            reasoning=str(e),
            fast_passed=fast_passed,
        )

    should_apply = score >= config.matching.ai_min_score

    return MatchResult(
        keyword_score=kw_score,
        ai_score=score,
        recommended_cv=cv_name,
        reasoning=reason,
        should_apply=should_apply,
        used_ai=True,
        fast_passed=fast_passed,
    )
