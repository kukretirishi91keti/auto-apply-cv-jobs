"""SQLite database layer for tracking jobs, applications, and runs."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Generator

from src.config import PROJECT_ROOT

DB_PATH = PROJECT_ROOT / "auto_apply.db"

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
    path = db_path or DB_PATH
    with get_connection(path) as conn:
        conn.executescript(SCHEMA)


@contextmanager
def get_connection(db_path: Path | None = None) -> Generator[sqlite3.Connection, None, None]:
    path = db_path or DB_PATH
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
) -> int | None:
    """Insert a job, returns job ID or None if duplicate."""
    with get_connection() as conn:
        try:
            cursor = conn.execute(
                """INSERT INTO jobs (portal, external_id, title, company, location, url, description, salary)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (portal, external_id, title, company, location, url, description, salary),
            )
            return cursor.lastrowid
        except sqlite3.IntegrityError:
            return None


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
