"""Tests for database layer."""

import tempfile
from pathlib import Path

import src.db as db


def setup_test_db():
    """Create a temporary test database."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    path = Path(tmp.name)
    tmp.close()
    # Monkey-patch DB_PATH for tests
    original = db.DB_PATH
    db.DB_PATH = path
    db.init_db(path)
    return path, original


def teardown_test_db(path, original):
    db.DB_PATH = original
    path.unlink(missing_ok=True)


def test_init_db():
    path, original = setup_test_db()
    try:
        with db.get_connection(path) as conn:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            table_names = {t[0] for t in tables}
            assert "jobs" in table_names
            assert "applications" in table_names
            assert "daily_runs" in table_names
            assert "blocked_companies" in table_names
    finally:
        teardown_test_db(path, original)


def test_insert_job():
    path, original = setup_test_db()
    try:
        job_id, is_new = db.insert_job(
            portal="naukri",
            external_id="123",
            title="Software Engineer",
            company="TestCo",
            location="Bangalore",
        )
        assert job_id is not None
        assert job_id > 0
        assert is_new is True
    finally:
        teardown_test_db(path, original)


def test_duplicate_job_returns_existing():
    path, original = setup_test_db()
    try:
        job_id, is_new = db.insert_job(portal="naukri", external_id="456", title="Dev", company="Co")
        assert is_new is True
        dup_id, dup_new = db.insert_job(portal="naukri", external_id="456", title="Dev", company="Co")
        assert dup_new is False
        assert dup_id == job_id
    finally:
        teardown_test_db(path, original)


def test_is_already_applied():
    path, original = setup_test_db()
    try:
        assert db.is_already_applied("TestCo", "Dev") is False

        job_id, _ = db.insert_job(portal="indeed", external_id="789", title="Dev", company="TestCo")
        db.insert_application(job_id, "indeed", status="applied")

        assert db.is_already_applied("TestCo", "Dev") is True
        assert db.is_already_applied("testco", "dev") is True  # case insensitive
        assert db.is_already_applied("OtherCo", "Dev") is False
    finally:
        teardown_test_db(path, original)


def test_blocked_companies():
    path, original = setup_test_db()
    try:
        assert db.is_company_blocked("SpamCo") is False

        with db.get_connection() as conn:
            conn.execute("INSERT INTO blocked_companies (company_name) VALUES (?)", ("SpamCo",))

        assert db.is_company_blocked("SpamCo") is True
        assert db.is_company_blocked("spamco") is True
    finally:
        teardown_test_db(path, original)


def test_daily_run_lifecycle():
    path, original = setup_test_db()
    try:
        run_id = db.start_daily_run("naukri")
        assert run_id > 0

        db.finish_daily_run(run_id, discovered=10, matched=5, applied=3, failed=1)

        with db.get_connection() as conn:
            row = conn.execute("SELECT * FROM daily_runs WHERE id = ?", (run_id,)).fetchone()
            assert row["jobs_discovered"] == 10
            assert row["jobs_applied"] == 3
    finally:
        teardown_test_db(path, original)


def test_update_job_scores():
    path, original = setup_test_db()
    try:
        job_id, _ = db.insert_job(portal="naukri", external_id="score1", title="Dev", company="Co")
        db.update_job_scores(job_id, keyword_score=0.8, ai_score=0.75, selected_cv="backend_focused")

        with db.get_connection() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            assert row["keyword_score"] == 0.8
            assert row["ai_score"] == 0.75
            assert row["selected_cv"] == "backend_focused"
    finally:
        teardown_test_db(path, original)


def test_save_and_get_generated_content():
    path, original = setup_test_db()
    try:
        job_id, _ = db.insert_job(portal="naukri", external_id="gen1", title="PM", company="Acme")

        # Initially empty
        content = db.get_generated_content(job_id)
        assert content["cover_letter"] == ""
        assert content["tailored_cv_text"] == ""
        assert content["recruiter_message"] == ""

        # Save content (creates application row)
        db.save_generated_content(
            job_id,
            cover_letter="Dear Hiring...",
            tailored_cv_text="SUMMARY\nLeader with 8+ years...",
            recruiter_message="Hi, I saw your posting...",
        )

        content = db.get_generated_content(job_id)
        assert content["cover_letter"] == "Dear Hiring..."
        assert "Leader with 8+ years" in content["tailored_cv_text"]
        assert content["recruiter_message"] == "Hi, I saw your posting..."

        # Update content (existing row)
        db.save_generated_content(job_id, cover_letter="Updated letter")
        content = db.get_generated_content(job_id)
        assert content["cover_letter"] == "Updated letter"
        assert "Leader with 8+ years" in content["tailored_cv_text"]
    finally:
        teardown_test_db(path, original)


def test_is_job_scored():
    path, original = setup_test_db()
    try:
        job_id, _ = db.insert_job(portal="indeed", external_id="sc1", title="Dev", company="Co")
        assert db.is_job_scored(job_id) is False

        db.update_job_scores(job_id, ai_score=0.85)
        assert db.is_job_scored(job_id) is True
    finally:
        teardown_test_db(path, original)
