"""SQLite database layer for tracking jobs, applications, and runs."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Generator

from src.config import PROJECT_ROOT

DB_PATH = PROJECT_ROOT / "auto_apply.db"
_active_db_path: Path | None = None  # overridden by dashboard for multi-user


def set_db_path(path: Path | None) -> None:
    """Set the active database path (for multi-user support)."""
    global _active_db_path
    _active_db_path = path


def get_db_path() -> Path:
    """Get the active database path."""
    return _active_db_path or DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portal TEXT NOT NULL,
    external_id TEXT,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    location TEXT,
    url TEXT,
    description TEXT,
    salary TEXT,
    keyword_score REAL,
    ai_score REAL,
    selected_cv TEXT,
    discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(portal, external_id)
);

CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    portal TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    cover_letter TEXT,
    screenshot_path TEXT,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS daily_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date DATE NOT NULL,
    portal TEXT NOT NULL,
    jobs_discovered INTEGER DEFAULT 0,
    jobs_matched INTEGER DEFAULT 0,
    jobs_applied INTEGER DEFAULT 0,
    jobs_failed INTEGER DEFAULT 0,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS blocked_companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name TEXT NOT NULL UNIQUE,
    reason TEXT,
    blocked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_jobs_portal ON jobs(portal);
CREATE INDEX IF NOT EXISTS idx_jobs_company_title ON jobs(company, title);
CREATE INDEX IF NOT EXISTS idx_applications_job ON applications(job_id);
CREATE INDEX IF NOT EXISTS idx_daily_runs_date ON daily_runs(run_date);
"""


def init_db(db_path: Path | None = None) -> None:
    """Initialize database schema."""
    path = db_path or get_db_path()
    with get_connection(path) as conn:
        conn.executescript(SCHEMA)
        _run_migrations(conn)


MIGRATIONS = [
    "ALTER TABLE applications ADD COLUMN notes TEXT DEFAULT ''",
    "ALTER TABLE applications ADD COLUMN tailored_cv_text TEXT DEFAULT ''",
    "ALTER TABLE applications ADD COLUMN recruiter_message TEXT DEFAULT ''",
]


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Apply schema migrations (idempotent)."""
    for sql in MIGRATIONS:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass  # column already exists


@contextmanager
def get_connection(db_path: Path | None = None) -> Generator[sqlite3.Connection, None, None]:
    path = db_path or get_db_path()
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def insert_job(
    portal: str,
    external_id: str,
    title: str,
    company: str,
    location: str = "",
    url: str = "",
    description: str = "",
    salary: str = "",
) -> tuple[int | None, bool]:
    """Insert a job, returns (job_id, is_new).

    If the job already exists, returns (existing_id, False) so callers
    can re-score unscored jobs from previous runs.
    """
    with get_connection() as conn:
        try:
            cursor = conn.execute(
                """INSERT INTO jobs (portal, external_id, title, company, location, url, description, salary)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (portal, external_id, title, company, location, url, description, salary),
            )
            return cursor.lastrowid, True
        except sqlite3.IntegrityError:
            # Return existing job ID so caller can re-score if needed
            row = conn.execute(
                "SELECT id FROM jobs WHERE portal = ? AND external_id = ?",
                (portal, external_id),
            ).fetchone()
            return (row[0] if row else None), False


def is_job_scored(job_id: int) -> bool:
    """Check if a job has already been AI-scored."""
    with get_connection() as conn:
        row = conn.execute("SELECT ai_score FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return row is not None and row[0] is not None


def update_job_scores(job_id: int, keyword_score: float | None = None, ai_score: float | None = None, selected_cv: str | None = None) -> None:
    with get_connection() as conn:
        updates = []
        params: list[Any] = []
        if keyword_score is not None:
            updates.append("keyword_score = ?")
            params.append(keyword_score)
        if ai_score is not None:
            updates.append("ai_score = ?")
            params.append(ai_score)
        if selected_cv is not None:
            updates.append("selected_cv = ?")
            params.append(selected_cv)
        if updates:
            params.append(job_id)
            conn.execute(f"UPDATE jobs SET {', '.join(updates)} WHERE id = ?", params)


def insert_application(job_id: int, portal: str, status: str = "pending", cover_letter: str = "", screenshot_path: str = "", error_message: str = "") -> int:
    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO applications (job_id, portal, status, cover_letter, screenshot_path, error_message)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (job_id, portal, status, cover_letter, screenshot_path, error_message),
        )
        return cursor.lastrowid  # type: ignore[return-value]


def is_already_applied(company: str, title: str) -> bool:
    """Cross-portal dedup: check if we already applied to similar job."""
    with get_connection() as conn:
        row = conn.execute(
            """SELECT 1 FROM applications a
               JOIN jobs j ON a.job_id = j.id
               WHERE a.status = 'applied'
               AND LOWER(j.company) = LOWER(?)
               AND LOWER(j.title) = LOWER(?)
               LIMIT 1""",
            (company, title),
        ).fetchone()
        return row is not None


def is_company_blocked(company: str) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM blocked_companies WHERE LOWER(company_name) = LOWER(?) LIMIT 1",
            (company,),
        ).fetchone()
        return row is not None


def get_today_application_count(portal: str | None = None) -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    with get_connection() as conn:
        if portal:
            row = conn.execute(
                "SELECT COUNT(*) FROM applications WHERE status='applied' AND DATE(applied_at) = ? AND portal = ?",
                (today, portal),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM applications WHERE status='applied' AND DATE(applied_at) = ?",
                (today,),
            ).fetchone()
        return row[0] if row else 0


def start_daily_run(portal: str) -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO daily_runs (run_date, portal) VALUES (?, ?)",
            (today, portal),
        )
        return cursor.lastrowid  # type: ignore[return-value]


def finish_daily_run(run_id: int, discovered: int, matched: int, applied: int, failed: int) -> None:
    with get_connection() as conn:
        conn.execute(
            """UPDATE daily_runs SET jobs_discovered=?, jobs_matched=?, jobs_applied=?, jobs_failed=?, finished_at=CURRENT_TIMESTAMP
               WHERE id = ?""",
            (discovered, matched, applied, failed, run_id),
        )


# --- Dashboard query functions ---


def get_jobs_feed(
    portal: str | None = None,
    min_score: float | None = None,
    days: int = 7,
    limit: int = 200,
) -> list[sqlite3.Row]:
    """Get recent jobs with optional filters, joined with application status."""
    with get_connection() as conn:
        conditions = ["j.discovered_at >= datetime('now', ?)"]
        params: list[Any] = [f"-{days} days"]

        if portal:
            conditions.append("j.portal = ?")
            params.append(portal)
        if min_score is not None:
            conditions.append("(j.keyword_score >= ? OR j.keyword_score IS NULL)")
            params.append(min_score)

        where = " AND ".join(conditions)
        params.append(limit)

        return conn.execute(
            f"""SELECT j.*, a.status AS app_status, a.applied_at
                FROM jobs j
                LEFT JOIN applications a ON j.id = a.job_id
                WHERE {where}
                ORDER BY j.discovered_at DESC
                LIMIT ?""",
            params,
        ).fetchall()


def get_applications(
    portal: str | None = None,
    status: str | None = None,
    days: int = 30,
    limit: int = 200,
) -> list[sqlite3.Row]:
    """Get application history joined with job details."""
    with get_connection() as conn:
        conditions = ["a.applied_at >= datetime('now', ?)"]
        params: list[Any] = [f"-{days} days"]

        if portal:
            conditions.append("a.portal = ?")
            params.append(portal)
        if status:
            conditions.append("a.status = ?")
            params.append(status)

        where = " AND ".join(conditions)
        params.append(limit)

        return conn.execute(
            f"""SELECT a.id AS app_id, a.status, a.applied_at, a.error_message, a.notes,
                       j.title, j.company, j.location, j.portal, j.url, j.selected_cv,
                       j.keyword_score, j.ai_score
                FROM applications a
                JOIN jobs j ON a.job_id = j.id
                WHERE {where}
                ORDER BY a.applied_at DESC
                LIMIT ?""",
            params,
        ).fetchall()


def get_manual_apply_queue() -> list[sqlite3.Row]:
    """Get scrape-only jobs (LinkedIn/Glassdoor) awaiting manual application."""
    with get_connection() as conn:
        return conn.execute(
            """SELECT j.id AS job_id, j.title, j.company, j.location, j.portal,
                      j.url, j.salary, j.description, j.keyword_score, j.ai_score,
                      j.discovered_at, a.status AS app_status
               FROM jobs j
               LEFT JOIN applications a ON j.id = a.job_id
               WHERE j.portal IN ('linkedin', 'glassdoor')
                 AND (a.status IS NULL OR a.status = 'scrape_only')
               ORDER BY j.discovered_at DESC""",
        ).fetchall()


def get_cloud_apply_queue(
    min_ai_score: float | None = None,
    portal: str | None = None,
    limit: int = 100,
    include_applied: bool = False,
) -> list[sqlite3.Row]:
    """Get all jobs awaiting application (for Cloud Apply Assistant).

    Includes ALL portals — not just scrape-only. Jobs that haven't been
    applied to yet, ordered by AI score (best matches first).
    Set include_applied=True to also return manually_applied jobs (for undo).
    """
    with get_connection() as conn:
        if include_applied:
            conditions = ["(a.status IS NULL OR a.status IN ('scrape_only', 'pending', 'manually_applied'))"]
        else:
            conditions = ["(a.status IS NULL OR a.status IN ('scrape_only', 'pending'))"]
        params: list[Any] = []

        if min_ai_score is not None:
            conditions.append("j.ai_score >= ?")
            params.append(min_ai_score)
        if portal:
            conditions.append("j.portal = ?")
            params.append(portal)

        # Must have a URL to apply
        conditions.append("j.url IS NOT NULL AND j.url != ''")

        where = " AND ".join(conditions)
        params.append(limit)

        return conn.execute(
            f"""SELECT j.id AS job_id, j.title, j.company, j.location, j.portal,
                       j.url, j.salary, j.description, j.keyword_score, j.ai_score,
                       j.selected_cv, j.discovered_at,
                       a.status AS app_status,
                       a.cover_letter AS saved_cover_letter,
                       a.tailored_cv_text AS saved_tailored_cv,
                       a.recruiter_message AS saved_recruiter_msg
                FROM jobs j
                LEFT JOIN applications a ON j.id = a.job_id
                WHERE {where}
                ORDER BY COALESCE(j.ai_score, 0) DESC, j.keyword_score DESC
                LIMIT ?""",
            params,
        ).fetchall()


def mark_manually_applied(job_id: int, notes: str = "") -> None:
    """Mark a scrape-only job as manually applied."""
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT id FROM applications WHERE job_id = ?", (job_id,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE applications SET status = 'manually_applied', notes = ? WHERE job_id = ?",
                (notes, job_id),
            )
        else:
            portal = conn.execute(
                "SELECT portal FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            conn.execute(
                "INSERT INTO applications (job_id, portal, status, notes) VALUES (?, ?, 'manually_applied', ?)",
                (job_id, portal[0] if portal else "unknown", notes),
            )


def unmark_applied(job_id: int) -> None:
    """Reset a manually-applied job back to pending so it reappears in the queue."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE applications SET status = 'scrape_only' WHERE job_id = ? AND status = 'manually_applied'",
            (job_id,),
        )


def save_generated_content(
    job_id: int,
    cover_letter: str = "",
    tailored_cv_text: str = "",
    recruiter_message: str = "",
) -> None:
    """Save AI-generated content (cover letter, tailored CV, recruiter message) for a job."""
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT id FROM applications WHERE job_id = ?", (job_id,)
        ).fetchone()
        if existing:
            updates = []
            params: list[Any] = []
            if cover_letter:
                updates.append("cover_letter = ?")
                params.append(cover_letter)
            if tailored_cv_text:
                updates.append("tailored_cv_text = ?")
                params.append(tailored_cv_text)
            if recruiter_message:
                updates.append("recruiter_message = ?")
                params.append(recruiter_message)
            if updates:
                params.append(job_id)
                conn.execute(
                    f"UPDATE applications SET {', '.join(updates)} WHERE job_id = ?",
                    params,
                )
        else:
            portal = conn.execute(
                "SELECT portal FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            conn.execute(
                """INSERT INTO applications
                   (job_id, portal, status, cover_letter, tailored_cv_text, recruiter_message)
                   VALUES (?, ?, 'pending', ?, ?, ?)""",
                (job_id, portal[0] if portal else "unknown", cover_letter, tailored_cv_text, recruiter_message),
            )


def get_generated_content(job_id: int) -> dict[str, str]:
    """Retrieve previously generated content for a job."""
    with get_connection() as conn:
        row = conn.execute(
            """SELECT cover_letter, tailored_cv_text, recruiter_message
               FROM applications WHERE job_id = ? LIMIT 1""",
            (job_id,),
        ).fetchone()
        if row:
            return {
                "cover_letter": row[0] or "",
                "tailored_cv_text": row[1] or "",
                "recruiter_message": row[2] or "",
            }
        return {"cover_letter": "", "tailored_cv_text": "", "recruiter_message": ""}


def get_daily_stats(days: int = 14) -> list[sqlite3.Row]:
    """Get daily aggregated run stats."""
    with get_connection() as conn:
        return conn.execute(
            """SELECT run_date,
                      SUM(jobs_discovered) AS discovered,
                      SUM(jobs_matched) AS matched,
                      SUM(jobs_applied) AS applied,
                      SUM(jobs_failed) AS failed
               FROM daily_runs
               WHERE run_date >= date('now', ?)
               GROUP BY run_date
               ORDER BY run_date DESC""",
            (f"-{days} days",),
        ).fetchall()


def get_portal_summary() -> list[sqlite3.Row]:
    """Get per-portal summary totals."""
    with get_connection() as conn:
        return conn.execute(
            """SELECT j.portal,
                      COUNT(DISTINCT j.id) AS total_jobs,
                      COUNT(DISTINCT CASE WHEN a.status = 'applied' THEN a.id END) AS total_applied,
                      COUNT(DISTINCT CASE WHEN a.status = 'failed' THEN a.id END) AS total_failed,
                      MAX(j.discovered_at) AS last_discovered
               FROM jobs j
               LEFT JOIN applications a ON j.id = a.job_id
               GROUP BY j.portal
               ORDER BY total_jobs DESC""",
        ).fetchall()
