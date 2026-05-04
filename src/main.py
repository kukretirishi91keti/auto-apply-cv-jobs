"""Main orchestrator and CLI entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from src.config import get_config, get_credentials, AppConfig, Credentials, PROJECT_ROOT
from src.cv_manager import load_all_cvs, select_best_cv
from src.cover_letter import generate_cover_letter
from src.db import (
    init_db,
    insert_job,
    is_job_scored,
    update_job_scores,
    insert_application,
    is_already_applied,
    is_company_blocked,
    get_today_application_count,
    start_daily_run,
    finish_daily_run,
)
from src.job_matcher import match_job, MatchResult
from src.notifier import send_daily_summary
from src.portals import ALL_PORTALS, AUTO_APPLY_PORTALS
from src.portals.base import JobListing
from src.utils.rate_limiter import between_applications

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(PROJECT_ROOT / "auto_apply.log"),
    ],
)
logger = logging.getLogger(__name__)


def _is_cheap_filtered(job: JobListing, config: AppConfig) -> bool:
    """Return True if job should be skipped before AI scoring (free checks only)."""
    if is_company_blocked(job.company):
        logger.debug("Skipping blocked company: %s", job.company)
        return True
    for exc in config.search.excluded_companies:
        if exc.lower() in job.company.lower():
            logger.debug("Skipping excluded company: %s", job.company)
            return True
    title_lower = job.title.lower()
    for pat in (config.search.excluded_title_patterns or []):
        if pat.lower() in title_lower:
            logger.debug("Skipping excluded title pattern '%s': %s", pat, job.title)
            return True
    return False


async def process_portal(
    portal_name: str,
    config: AppConfig,
    creds: Credentials,
    cv_texts: dict[str, str],
    dry_run: bool = False,
    scrape_only: bool = False,
    limit: int | None = None,
    ai_scoring_state: list[int] | None = None,
) -> tuple[dict[str, int], list[tuple[JobListing, MatchResult]]]:
    """Run the full pipeline for a single portal.

    Returns (stats, matched_jobs) where matched_jobs is the list of jobs that
    passed AI scoring. The application limit is enforced AFTER scoring so that
    all discovered jobs are evaluated and the best matches are acted on.
    """
    stats = {"discovered": 0, "matched": 0, "applied": 0, "failed": 0}
    matched_jobs: list[tuple[JobListing, MatchResult]] = []

    portal_config = config.portals.get(portal_name)
    if not portal_config or not portal_config.enabled:
        logger.info("Portal %s is disabled, skipping", portal_name)
        return stats, matched_jobs

    try:
        portal_cls = ALL_PORTALS.get(portal_name)
    except Exception as e:
        logger.error("Cannot load portal %s: %s", portal_name, e)
        return stats, matched_jobs
    if not portal_cls:
        logger.error("Unknown portal: %s", portal_name)
        return stats, matched_jobs

    portal = portal_cls(config, creds)
    run_id = start_daily_run(portal_name)
    max_apply = limit or config.apply.max_per_portal

    try:
        if dry_run or scrape_only:
            logger.info("[%s] Skipping login (%s mode)", portal_name,
                        "dry-run" if dry_run else "scrape-only")
        else:
            logged_in = await portal.login()
            if not logged_in:
                logger.warning(
                    "Login failed for %s — continuing to search (some results "
                    "may still be available without authentication)", portal_name)

        jobs = await portal.search_jobs()
        stats["discovered"] = len(jobs)
        logger.info("[%s] Discovered %d jobs", portal_name, len(jobs))

        if scrape_only:
            for job in jobs:
                insert_job(
                    portal=job.portal, external_id=job.external_id,
                    title=job.title, company=job.company,
                    location=job.location, url=job.url,
                    description=job.description, salary=job.salary,
                )
            logger.info("[%s] Scrape-only mode — saved %d jobs", portal_name, len(jobs))
            finish_daily_run(run_id, stats["discovered"], 0, 0, 0)
            return stats, matched_jobs

        can_auto_apply = portal_name in AUTO_APPLY_PORTALS and portal_config.auto_apply

        # Pre-sort by keyword score (free) so AI budget goes to best candidates first
        from src.job_matcher import keyword_score as _kw_score
        jobs.sort(
            key=lambda j: _kw_score(j.title, j.description, config.search.keywords),
            reverse=True,
        )

        # ── Phase 1: Score ALL jobs — no application limit here ──────────────
        ai_cap = config.matching.max_ai_scorings_per_day
        for job in jobs:
            if _is_cheap_filtered(job, config):
                continue

            if is_already_applied(job.company, job.title):
                logger.debug("Already applied: %s at %s", job.title, job.company)
                continue

            job_id, is_new = insert_job(
                portal=job.portal, external_id=job.external_id,
                title=job.title, company=job.company,
                location=job.location, url=job.url,
                description=job.description, salary=job.salary,
            )
            if job_id is None:
                continue

            if not is_new and is_job_scored(job_id):
                # Already scored in a previous run — load result from DB to
                # include in match list without burning API quota
                logger.debug("Already scored (from prior run): %s at %s", job.title, job.company)
                continue

            if not is_new:
                logger.info("Re-scoring previously unscored job: %s at %s", job.title, job.company)

            if ai_scoring_state is not None and ai_scoring_state[0] >= ai_cap:
                logger.info(
                    "AI scoring cap reached (%d) — skipping remaining jobs in %s",
                    ai_cap, portal_name,
                )
                break

            result = match_job(
                job.title, job.description, cv_texts, config, creds,
                job_location=job.location,
            )
            if ai_scoring_state is not None and result.used_ai:
                ai_scoring_state[0] += 1

            update_job_scores(
                job_id,
                keyword_score=result.keyword_score,
                ai_score=result.ai_score,
                selected_cv=result.recommended_cv,
            )

            if result.should_apply:
                stats["matched"] += 1
                matched_jobs.append((job, result))
                logger.info(
                    "[%s] MATCH: %s at %s (kw=%.2f ai=%.2f cv=%s) %s",
                    portal_name, job.title, job.company,
                    result.keyword_score, result.ai_score or 0,
                    result.recommended_cv, job.url,
                )
            else:
                logger.debug(
                    "No match: %s (kw=%.2f ai=%s reason=%s)",
                    job.title, result.keyword_score,
                    f"{result.ai_score:.2f}" if result.ai_score is not None else "n/a",
                    result.reasoning,
                )

        # ── Phase 2: Apply — limit enforced here, best scores first ──────────
        matched_jobs.sort(key=lambda x: x[1].ai_score or 0, reverse=True)
        applied_count = 0

        for job, result in matched_jobs:
            total_today = get_today_application_count()
            if total_today >= config.apply.max_applications_per_day:
                logger.info("Daily application limit reached (%d)", total_today)
                break
            if applied_count >= max_apply:
                logger.info(
                    "Portal limit reached for %s (%d/%d applied, %d matches remain)",
                    portal_name, applied_count, max_apply,
                    len(matched_jobs) - applied_count,
                )
                break

            if dry_run:
                logger.info(
                    "[DRY RUN] Would apply: %s at %s (ai=%.2f) → %s",
                    job.title, job.company, result.ai_score or 0, job.url,
                )
                continue

            if not can_auto_apply:
                job_id_row = insert_job(
                    portal=job.portal, external_id=job.external_id,
                    title=job.title, company=job.company,
                    location=job.location, url=job.url,
                    description=job.description, salary=job.salary,
                )
                job_id = job_id_row[0]
                if job_id:
                    insert_application(job_id, portal_name, status="scrape_only")
                logger.info("[%s] Scrape-only portal — open manually: %s", portal_name, job.url)
                continue

            cover_letter = ""
            if config.apply.generate_cover_letter and cv_texts:
                cv_name = result.recommended_cv or next(iter(cv_texts))
                try:
                    cover_letter = generate_cover_letter(
                        job.title, job.company, job.description,
                        cv_texts.get(cv_name, ""), config, creds,
                    )
                except Exception as e:
                    logger.warning("Cover letter generation failed: %s", e)

            cv_name = result.recommended_cv or next(iter(cv_texts))
            cv_version = next((v for v in config.cvs.versions if v.name == cv_name), None)
            cv_path = str(PROJECT_ROOT / config.cvs.directory / (cv_version.file if cv_version else "cv.pdf"))

            job_id_row = insert_job(
                portal=job.portal, external_id=job.external_id,
                title=job.title, company=job.company,
                location=job.location, url=job.url,
                description=job.description, salary=job.salary,
            )
            job_id = job_id_row[0]
            if job_id is None:
                continue

            try:
                success = await portal.apply_to_job(job, cv_path, cover_letter)
                if success:
                    stats["applied"] += 1
                    applied_count += 1
                    insert_application(job_id, portal_name, status="applied", cover_letter=cover_letter)
                    logger.info("[%s] Applied: %s at %s", portal_name, job.title, job.company)
                    await between_applications()
                else:
                    stats["failed"] += 1
                    insert_application(job_id, portal_name, status="failed")
            except Exception as e:
                stats["failed"] += 1
                insert_application(job_id, portal_name, status="failed", error_message=str(e))
                logger.error("Apply failed for %s: %s", job.title, e)

    except Exception as e:
        logger.error("Portal %s error: %s", portal_name, e)
    finally:
        await portal.close()
        finish_daily_run(run_id, stats["discovered"], stats["matched"], stats["applied"], stats["failed"])

    return stats, matched_jobs


async def run_pipeline(
    config: AppConfig,
    creds: Credentials,
    portals: list[str] | None = None,
    dry_run: bool = False,
    scrape_only: bool = False,
    limit: int | None = None,
) -> None:
    """Run the full auto-apply pipeline."""
    init_db()

    cv_dir_override = getattr(config, "_cv_dir_override", None)
    cv_texts = load_all_cvs(config, cv_dir_override=cv_dir_override)
    if not cv_texts:
        logger.warning("No CVs loaded — check config/settings.yaml and data/cvs/")

    # Shared AI scoring counter (enforces max_ai_scorings_per_day across all portals)
    ai_scoring_count = 0
    ai_scoring_cap = config.matching.max_ai_scorings_per_day

    portal_list = portals or [name for name, pc in config.portals.items() if pc.enabled]
    portal_results: dict[str, dict[str, int]] = {}

    # All matched jobs across every source — used for the dry-run summary
    all_matched: list[tuple[JobListing, MatchResult, str]] = []  # (job, result, source)

    # ── Step 1: Aggregator API search (JSearch + Adzuna) ─────────────────────
    has_aggregator = bool(creds.rapidapi_key or (creds.adzuna_app_id and creds.adzuna_app_key))
    if has_aggregator:
        from src.job_apis import aggregator_search

        terms: list[str] = []
        for kw in config.search.keywords:
            for part in kw.split(","):
                term = part.strip()
                if term and term not in terms:
                    terms.append(term)
        terms = terms[:10]

        locations = config.search.locations or [""]
        api_locations = locations[:3]
        logger.info(
            "=== Aggregator API search: %d terms, %d locations (of %d configured) ===",
            len(terms), len(api_locations), len(locations),
        )

        all_agg_jobs: list = []
        seen_agg: set[str] = set()
        for loc_idx, location in enumerate(api_locations):
            if loc_idx > 0:
                await asyncio.sleep(2)
            loc_jobs = await aggregator_search(
                terms=terms,
                location=location,
                rapidapi_key=creds.rapidapi_key,
                adzuna_app_id=creds.adzuna_app_id,
                adzuna_app_key=creds.adzuna_app_key,
            )
            for j in loc_jobs:
                key = f"{j.portal}:{j.external_id}"
                if key not in seen_agg:
                    seen_agg.add(key)
                    all_agg_jobs.append(j)
        agg_jobs = all_agg_jobs

        agg_stats = {"discovered": len(agg_jobs), "matched": 0, "applied": 0, "failed": 0}
        logger.info("[aggregator] Discovered %d jobs via APIs", len(agg_jobs))

        # ── Phase 1: Score ALL aggregator jobs — no limit ────────────────────
        for job in agg_jobs:
            job_id, is_new = insert_job(
                portal=job.portal, external_id=job.external_id,
                title=job.title, company=job.company,
                location=job.location, url=job.url,
                description=job.description, salary=job.salary,
            )
            if job_id is None:
                continue

            if not is_new and is_job_scored(job_id):
                continue

            if scrape_only:
                continue

            if _is_cheap_filtered(job, config):
                continue

            if is_already_applied(job.company, job.title):
                continue

            if not cv_texts:
                continue

            if ai_scoring_count >= ai_scoring_cap:
                logger.info("AI scoring cap reached (%d) — skipping remaining aggregator jobs", ai_scoring_cap)
                break

            result = match_job(
                job.title, job.description, cv_texts, config, creds,
                job_location=job.location,
            )
            if result.used_ai:
                ai_scoring_count += 1

            update_job_scores(
                job_id,
                keyword_score=result.keyword_score,
                ai_score=result.ai_score,
                selected_cv=result.recommended_cv,
            )

            if result.should_apply:
                agg_stats["matched"] += 1
                all_matched.append((job, result, job.portal))
                logger.info(
                    "[aggregator/%s] MATCH: %s at %s (kw=%.2f ai=%.2f) %s",
                    job.portal, job.title, job.company,
                    result.keyword_score, result.ai_score or 0, job.url,
                )
                if dry_run:
                    logger.info(
                        "[DRY RUN] Would apply via %s: %s at %s (ai=%.2f) → %s",
                        job.portal, job.title, job.company,
                        result.ai_score or 0, job.url,
                    )

        portal_results["aggregator"] = agg_stats
    else:
        logger.info("No aggregator API keys configured — set RAPIDAPI_KEY or ADZUNA_APP_ID+ADZUNA_APP_KEY in .env")

    # ── Step 2: Per-portal direct search ─────────────────────────────────────
    ai_state = [ai_scoring_count]
    for portal_name in portal_list:
        logger.info("=== Processing portal: %s ===", portal_name)
        stats, portal_matched = await process_portal(
            portal_name, config, creds, cv_texts,
            dry_run=dry_run, scrape_only=scrape_only, limit=limit,
            ai_scoring_state=ai_state,
        )
        portal_results[portal_name] = stats
        for job, result in portal_matched:
            all_matched.append((job, result, portal_name))

    # ── Dry-run: print ranked match table for manual review ──────────────────
    if dry_run and all_matched:
        all_matched.sort(key=lambda x: x[1].ai_score or 0, reverse=True)
        separator = "─" * 100
        logger.info("")
        logger.info("=" * 100)
        logger.info("  DRY-RUN RESULTS: %d MATCHED JOBS (ranked by AI score, best first)", len(all_matched))
        logger.info("  These jobs passed all filters. Apply manually or re-run without --dry-run.")
        logger.info("=" * 100)
        for rank, (job, result, source) in enumerate(all_matched, 1):
            logger.info("%s", separator)
            logger.info(
                "  #%-3d  AI=%.2f  KW=%.2f  [%s]",
                rank, result.ai_score or 0, result.keyword_score, source.upper(),
            )
            logger.info("  TITLE:   %s", job.title)
            logger.info("  COMPANY: %s", job.company)
            logger.info("  LOCATION:%s", job.location or "—")
            logger.info("  CV:      %s", result.recommended_cv or "default")
            logger.info("  REASON:  %s", result.reasoning or "—")
            logger.info("  URL:     %s", job.url or "—")
        logger.info("%s", separator)
        logger.info("  TOTAL MATCHES: %d", len(all_matched))
        logger.info("=" * 100)
        logger.info("")
    elif dry_run:
        logger.info("DRY-RUN: No jobs passed all scoring thresholds this run.")

    send_daily_summary(portal_results, config, creds)


def run_once(
    portals: list[str] | None = None,
    dry_run: bool = False,
    scrape_only: bool = False,
    limit: int | None = None,
    env_path: str | None = None,
    config_path: str | None = None,
    db_path: str | None = None,
    cv_dir: str | None = None,
) -> None:
    """One-shot run."""
    env_path = env_path or os.environ.get("AUTO_APPLY_ENV_PATH")
    config_path = config_path or os.environ.get("AUTO_APPLY_CONFIG_PATH")
    db_path = db_path or os.environ.get("AUTO_APPLY_DB_PATH")
    cv_dir = cv_dir or os.environ.get("AUTO_APPLY_CV_DIR")

    if env_path:
        logger.info("Using user-specific env: %s", env_path)
    if config_path:
        logger.info("Using user-specific config: %s", config_path)
    if cv_dir:
        logger.info("Using user-specific CV dir: %s", cv_dir)
    if db_path:
        logger.info("Using user-specific DB: %s", db_path)

    config = get_config(Path(config_path) if config_path else None)
    creds = get_credentials(Path(env_path) if env_path else None)

    if db_path:
        from src.db import set_db_path
        set_db_path(Path(db_path))

    if cv_dir:
        config._cv_dir_override = Path(cv_dir)

    asyncio.run(run_pipeline(config, creds, portals, dry_run, scrape_only, limit))


def run_scheduled() -> None:
    """Run on a daily schedule."""
    from src.scheduler import create_scheduler

    config = get_config()
    scheduler = create_scheduler(config, run_once)
    logger.info("Starting scheduler... Press Ctrl+C to stop.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped.")


def cli() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Auto-Apply CV Jobs")
    parser.add_argument("--dry-run", action="store_true",
                        help="Score all jobs and show matches, but don't apply")
    parser.add_argument("--portal", type=str, action="append",
                        help="Run only specific portal(s); repeat for multiple")
    parser.add_argument("--limit", type=int,
                        help="Max applications to SEND per portal (scoring is unlimited)")
    parser.add_argument("--schedule", action="store_true", help="Run on daily schedule")
    parser.add_argument("--scrape-only", action="store_true", help="Only scrape, skip matching/applying")
    parser.add_argument("--dashboard", action="store_true", help="Launch web dashboard")
    parser.add_argument("--env-path", type=str, help="Path to user-specific .env file")
    parser.add_argument("--config-path", type=str, help="Path to user-specific settings.yaml")
    parser.add_argument("--db-path", type=str, help="Path to user-specific database")
    parser.add_argument("--cv-dir", type=str, help="Path to user-specific CV directory")
    args = parser.parse_args()

    if args.dashboard:
        import subprocess
        dashboard_path = str(Path(__file__).parent / "dashboard.py")
        logger.info("Launching dashboard at http://localhost:8501")
        subprocess.run([sys.executable, "-m", "streamlit", "run", dashboard_path, "--server.headless", "true"])
        return

    if args.schedule:
        run_scheduled()
    else:
        portals = args.portal if args.portal else None
        run_once(
            portals=portals, dry_run=args.dry_run, scrape_only=args.scrape_only,
            limit=args.limit, env_path=args.env_path, config_path=args.config_path,
            db_path=args.db_path, cv_dir=args.cv_dir,
        )


if __name__ == "__main__":
    cli()
