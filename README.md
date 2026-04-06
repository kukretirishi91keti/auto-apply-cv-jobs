# Auto-Apply CV Jobs

Automated job application tool that scrapes 6 job portals daily, matches your CVs using AI, and auto-applies to the best fits.

## Features

- **3 CV versions** — AI picks the best CV for each job based on role requirements
- **6 portal scrapers** — Naukri, Indeed, Foundit, ZipRecruiter (full auto-apply); LinkedIn, Glassdoor (scrape-only)
- **Two-stage matching** — fast keyword filter, then Claude AI scoring for shortlisted jobs
- **AI cover letters** — generated per application, tailored to job description
- **SQLite tracking** — dedup across portals, application history, daily run logs
- **Daily scheduling** — APScheduler cron trigger with email/Slack notifications
- **Anti-detection** — stealth Playwright, human-like delays, session persistence

## Quick Start

```bash
# 0. Clone and enter the project
git clone https://github.com/kukretirishi91keti/auto-apply-cv-jobs.git
cd auto-apply-cv-jobs

# 1. Install
pip install -e ".[dev]"
playwright install chromium

# 2. Configure
cp .env.example .env
# Edit .env with your credentials
# Edit config/settings.yaml with your preferences
# Place your 3 CV files in data/cvs/

# 3. Dry run (no actual applications)
auto-apply --dry-run

# 4. Run for real
auto-apply

# 5. Run on schedule (daily at configured time)
auto-apply --schedule
```

> **Note:** After `pip install -e .`, the `auto-apply` command works from any directory.
> You can also use `python -m src.main` from within the project folder.

## CLI Options

```
auto-apply [OPTIONS]

  --dry-run         Scrape and match jobs, but don't apply
  --portal NAME     Run only a specific portal (naukri, indeed, etc.)
  --limit N         Max applications per run (overrides config)
  --schedule        Run on daily schedule instead of one-shot
  --scrape-only     Only scrape jobs, skip matching and applying
```

## Project Structure

```
auto-apply-cv-jobs/
├── config/
│   └── settings.yaml          # Main configuration
├── data/
│   ├── cvs/                   # Your CV files (PDF/DOCX)
│   └── screenshots/           # Application confirmation screenshots
├── src/
│   ├── config.py              # Pydantic config loader
│   ├── db.py                  # SQLite database layer
│   ├── cv_manager.py          # CV parsing and AI selection
│   ├── job_matcher.py         # Two-stage job matching
│   ├── cover_letter.py        # AI cover letter generation
│   ├── main.py                # CLI entry point and orchestrator
│   ├── scheduler.py           # APScheduler daily cron
│   ├── notifier.py            # Email + Slack notifications
│   ├── portals/
│   │   ├── base.py            # Abstract portal base class
│   │   ├── naukri.py
│   │   ├── indeed.py
│   │   ├── foundit.py
│   │   ├── ziprecruiter.py
│   │   ├── linkedin.py        # Scrape-only
│   │   └── glassdoor.py       # Scrape-only
│   └── utils/
│       ├── browser.py         # Playwright stealth browser
│       └── rate_limiter.py    # Human-like delays
└── tests/
```

## Safety & Limitations

- **LinkedIn & Glassdoor** are scrape-only (aggressive bot detection — jobs are discovered but opened in browser for manual apply)
- **Rate limiting** with human-like delays between actions
- **Daily caps** prevent over-applying
- **`--dry-run`** mode for safe testing
- **Portal ToS**: Automated applying may violate some portals' terms of service. Use at your own risk.

## Estimated Daily Cost

~$0.50–1.50 in Claude API credits for ~50 job scorings + ~20 cover letters.
