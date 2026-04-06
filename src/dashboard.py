"""Streamlit web dashboard for Auto-Apply CV Jobs."""

from __future__ import annotations

import os
import subprocess
import sys
import shutil
import time
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml

from src.config import PROJECT_ROOT, load_config
from src.db import (
    init_db,
    get_jobs_feed,
    get_applications,
    get_manual_apply_queue,
    mark_manually_applied,
    get_daily_stats,
    get_portal_summary,
)

PORTALS = ["All", "naukri", "indeed", "foundit", "ziprecruiter", "linkedin", "glassdoor"]
PORTAL_NAMES = ["naukri", "indeed", "foundit", "ziprecruiter", "linkedin", "glassdoor"]
STATUS_COLORS = {
    "applied": "🟢",
    "manually_applied": "🔵",
    "pending": "🟡",
    "scrape_only": "🟠",
    "failed": "🔴",
}

CV_DIR = PROJECT_ROOT / "data" / "cvs"
ENV_PATH = PROJECT_ROOT / ".env"
CONFIG_PATH = PROJECT_ROOT / "config" / "settings.yaml"


def rows_to_df(rows: list) -> pd.DataFrame:
    """Convert sqlite3.Row list to DataFrame."""
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([dict(row) for row in rows])


# ─── Page: Jobs Feed ───


def render_jobs_feed() -> None:
    """Jobs Feed page — all discovered jobs with filters."""
    st.header("Jobs Feed")

    col1, col2, col3 = st.columns(3)
    with col1:
        portal = st.selectbox("Portal", PORTALS, key="jf_portal")
    with col2:
        min_score = st.slider("Min Keyword Score", 0.0, 1.0, 0.0, 0.1, key="jf_score")
    with col3:
        days = st.selectbox("Last N days", [7, 14, 30, 90], key="jf_days")

    portal_filter = portal if portal != "All" else None
    score_filter = min_score if min_score > 0 else None
    jobs = get_jobs_feed(portal=portal_filter, min_score=score_filter, days=days)
    df = rows_to_df(jobs)

    st.metric("Jobs Found", len(df))

    if df.empty:
        st.info("No jobs found. Run `auto-apply --dry-run` to discover jobs.")
        return

    display_cols = ["title", "company", "location", "portal", "keyword_score", "ai_score", "selected_cv", "app_status", "discovered_at"]
    available = [c for c in display_cols if c in df.columns]
    st.dataframe(
        df[available],
        column_config={
            "title": st.column_config.TextColumn("Title", width="large"),
            "keyword_score": st.column_config.NumberColumn("KW Score", format="%.2f"),
            "ai_score": st.column_config.NumberColumn("AI Score", format="%.2f"),
            "app_status": st.column_config.TextColumn("Status"),
        },
        use_container_width=True,
        hide_index=True,
    )

    if "url" in df.columns:
        with st.expander("Job URLs"):
            for _, row in df.iterrows():
                if row.get("url"):
                    st.markdown(f"- [{row['title']} @ {row['company']}]({row['url']})")


# ─── Page: Applications ───


def render_applications() -> None:
    """Applications page — history with status, CV used, portal."""
    st.header("Application History")

    col1, col2, col3 = st.columns(3)
    with col1:
        portal = st.selectbox("Portal", PORTALS, key="app_portal")
    with col2:
        status = st.selectbox("Status", ["All", "applied", "manually_applied", "pending", "scrape_only", "failed"], key="app_status")
    with col3:
        days = st.selectbox("Last N days", [7, 14, 30, 90], key="app_days")

    portal_filter = portal if portal != "All" else None
    status_filter = status if status != "All" else None
    apps = get_applications(portal=portal_filter, status=status_filter, days=days)
    df = rows_to_df(apps)

    if df.empty:
        st.info("No applications recorded yet.")
        return

    if "status" in df.columns:
        cols = st.columns(5)
        for i, s in enumerate(["applied", "manually_applied", "pending", "scrape_only", "failed"]):
            count = len(df[df["status"] == s])
            cols[i].metric(f"{STATUS_COLORS.get(s, '')} {s}", count)

    display_cols = ["title", "company", "portal", "status", "selected_cv", "applied_at", "error_message"]
    available = [c for c in display_cols if c in df.columns]
    st.dataframe(df[available], use_container_width=True, hide_index=True)


# ─── Page: Manual Apply Queue ───


def render_manual_queue() -> None:
    """Manual Apply Queue — LinkedIn/Glassdoor scrape-only jobs."""
    st.header("Manual Apply Queue")
    st.caption("Jobs from LinkedIn & Glassdoor that need manual application")

    queue = get_manual_apply_queue()

    if not queue:
        st.info("No scrape-only jobs in queue. Run `auto-apply --scrape-only --portal linkedin` to discover jobs.")
        return

    st.metric("Jobs to Review", len(queue))

    for row in queue:
        job = dict(row)
        with st.container(border=True):
            left, right = st.columns([4, 1])
            with left:
                title = job.get("title", "Unknown")
                company = job.get("company", "Unknown")
                url = job.get("url", "")
                location = job.get("location", "")
                portal = job.get("portal", "")
                salary = job.get("salary", "")

                if url:
                    st.markdown(f"### [{title}]({url})")
                else:
                    st.markdown(f"### {title}")
                st.write(f"**{company}** | {location} | {portal.upper()}")
                if salary:
                    st.write(f"Salary: {salary}")

            with right:
                job_id = job.get("job_id")
                if st.button("Mark Applied", key=f"mark_{job_id}", type="primary"):
                    mark_manually_applied(job_id)
                    st.rerun()


# ─── Page: Daily Stats ───


def render_daily_stats() -> None:
    """Daily Stats page — run summaries and portal breakdown."""
    st.header("Daily Stats")

    days = st.selectbox("Period", [7, 14, 30], key="stats_days")
    stats = get_daily_stats(days=days)
    df = rows_to_df(stats)

    if df.empty:
        st.info("No run data yet. Run `auto-apply --dry-run` to generate stats.")
        return

    cols = st.columns(4)
    cols[0].metric("Discovered", int(df["discovered"].sum()))
    cols[1].metric("Matched", int(df["matched"].sum()))
    cols[2].metric("Applied", int(df["applied"].sum()))
    cols[3].metric("Failed", int(df["failed"].sum()))

    st.subheader("Daily Activity")
    chart_df = df.set_index("run_date")[["discovered", "matched", "applied", "failed"]]
    st.bar_chart(chart_df)

    st.subheader("Portal Summary")
    summary = get_portal_summary()
    summary_df = rows_to_df(summary)
    if not summary_df.empty:
        st.dataframe(summary_df, use_container_width=True, hide_index=True)


# ─── Page: Settings ───


def _load_env() -> dict[str, str]:
    """Load current .env values."""
    env: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                env[key.strip()] = val.strip()
    return env


def _save_env(env: dict[str, str]) -> None:
    """Save .env file."""
    lines = []
    for key, val in env.items():
        lines.append(f"{key}={val}")
    ENV_PATH.write_text("\n".join(lines) + "\n")


def render_settings() -> None:
    """Settings page — CV upload, credentials, search config."""
    st.header("Settings")

    # ── Tab layout ──
    tab_cv, tab_creds, tab_search, tab_portals = st.tabs([
        "CV Management", "Credentials", "Search Preferences", "Portal Config",
    ])

    # ── CV Management ──
    with tab_cv:
        st.subheader("Upload CVs")
        st.caption(f"CVs are stored in `{CV_DIR}`")

        CV_DIR.mkdir(parents=True, exist_ok=True)
        existing_cvs = sorted(CV_DIR.glob("*.*"))

        if existing_cvs:
            st.write("**Current CVs:**")
            for cv_file in existing_cvs:
                col1, col2 = st.columns([4, 1])
                col1.write(f"- {cv_file.name} ({cv_file.stat().st_size // 1024} KB)")
                if col2.button("Delete", key=f"del_{cv_file.name}"):
                    cv_file.unlink()
                    st.rerun()
        else:
            st.warning("No CVs uploaded yet.")

        uploaded_files = st.file_uploader(
            "Upload CV files (PDF or DOCX)",
            type=["pdf", "docx", "doc"],
            accept_multiple_files=True,
            key="cv_upload",
        )
        if uploaded_files:
            for f in uploaded_files:
                dest = CV_DIR / f.name
                dest.write_bytes(f.getvalue())
                st.success(f"Uploaded: {f.name}")

            # Update settings.yaml with new CV filenames
            st.info("Update the CV names in **Search Preferences** or `config/settings.yaml`.")

    # ── Credentials ──
    with tab_creds:
        st.subheader("API & Portal Credentials")
        st.caption("Saved to `.env` file (never committed to git)")

        env = _load_env()

        with st.form("credentials_form"):
            st.markdown("**Anthropic API**")
            anthropic_key = st.text_input(
                "ANTHROPIC_API_KEY",
                value=env.get("ANTHROPIC_API_KEY", ""),
                type="password",
            )

            st.markdown("---")
            st.markdown("**Portal Logins**")

            portal_creds: dict[str, tuple[str, str]] = {}
            for portal in PORTAL_NAMES:
                col1, col2 = st.columns(2)
                email_key = f"{portal.upper()}_EMAIL"
                pass_key = f"{portal.upper()}_PASSWORD"
                with col1:
                    email = st.text_input(
                        f"{portal.title()} Email",
                        value=env.get(email_key, ""),
                        key=f"cred_{email_key}",
                    )
                with col2:
                    password = st.text_input(
                        f"{portal.title()} Password",
                        value=env.get(pass_key, ""),
                        type="password",
                        key=f"cred_{pass_key}",
                    )
                portal_creds[portal] = (email, password)

            st.markdown("---")
            st.markdown("**Job Aggregator APIs (recommended for Streamlit Cloud)**")
            st.caption("These APIs find jobs across all portals without needing a browser.")
            col1, col2 = st.columns(2)
            with col1:
                rapidapi_key = st.text_input(
                    "RAPIDAPI_KEY (JSearch — free at rapidapi.com)",
                    value=env.get("RAPIDAPI_KEY", ""),
                    type="password",
                    key="cred_rapidapi",
                )
                adzuna_app_id = st.text_input(
                    "ADZUNA_APP_ID (free at developer.adzuna.com)",
                    value=env.get("ADZUNA_APP_ID", ""),
                    key="cred_adzuna_id",
                )
            with col2:
                st.write("")  # spacer
                st.write("")
                st.caption("JSearch covers: LinkedIn, Indeed, Glassdoor, ZipRecruiter")
                adzuna_app_key = st.text_input(
                    "ADZUNA_APP_KEY",
                    value=env.get("ADZUNA_APP_KEY", ""),
                    type="password",
                    key="cred_adzuna_key",
                )

            st.markdown("---")
            st.markdown("**Notifications (optional)**")
            col1, col2 = st.columns(2)
            with col1:
                smtp_user = st.text_input("SMTP Email", value=env.get("SMTP_USER", ""), key="smtp_user")
                smtp_pass = st.text_input("SMTP Password", value=env.get("SMTP_PASSWORD", ""), type="password", key="smtp_pass")
            with col2:
                notif_email = st.text_input("Notification Email", value=env.get("NOTIFICATION_EMAIL", ""), key="notif_email")
                slack_url = st.text_input("Slack Webhook URL", value=env.get("SLACK_WEBHOOK_URL", ""), type="password", key="slack_url")

            if st.form_submit_button("Save Credentials", type="primary"):
                new_env: dict[str, str] = {}
                if anthropic_key:
                    new_env["ANTHROPIC_API_KEY"] = anthropic_key
                for portal in PORTAL_NAMES:
                    email, password = portal_creds[portal]
                    if email:
                        new_env[f"{portal.upper()}_EMAIL"] = email
                    if password:
                        new_env[f"{portal.upper()}_PASSWORD"] = password
                if smtp_user:
                    new_env["SMTP_HOST"] = env.get("SMTP_HOST", "smtp.gmail.com")
                    new_env["SMTP_PORT"] = env.get("SMTP_PORT", "587")
                    new_env["SMTP_USER"] = smtp_user
                if smtp_pass:
                    new_env["SMTP_PASSWORD"] = smtp_pass
                if notif_email:
                    new_env["NOTIFICATION_EMAIL"] = notif_email
                if slack_url:
                    new_env["SLACK_WEBHOOK_URL"] = slack_url
                if rapidapi_key:
                    new_env["RAPIDAPI_KEY"] = rapidapi_key
                if adzuna_app_id:
                    new_env["ADZUNA_APP_ID"] = adzuna_app_id
                if adzuna_app_key:
                    new_env["ADZUNA_APP_KEY"] = adzuna_app_key

                _save_env(new_env)
                st.success("Credentials saved to .env")

    # ── Search Preferences ──
    with tab_search:
        st.subheader("Search Configuration")
        st.caption(f"Saved to `{CONFIG_PATH}`")

        config = load_config()

        with st.form("search_form"):
            keywords = st.text_area(
                "Search Keywords (one per line)",
                value="\n".join(config.search.keywords),
                height=100,
            )
            locations = st.text_area(
                "Locations (one per line)",
                value="\n".join(config.search.locations),
                height=80,
            )
            experience = st.number_input(
                "Years of Experience",
                min_value=0, max_value=30,
                value=config.search.experience_years,
            )
            excluded = st.text_area(
                "Excluded Companies (one per line)",
                value="\n".join(config.search.excluded_companies),
                height=80,
            )

            st.markdown("---")
            st.markdown("**Matching**")
            col1, col2 = st.columns(2)
            with col1:
                kw_min = st.slider("Keyword Min Score", 0.0, 1.0, config.matching.keyword_min_score, 0.05)
                ai_min = st.slider("AI Min Score", 0.0, 1.0, config.matching.ai_min_score, 0.05)
            with col2:
                max_per_day = st.number_input("Max Applications/Day", 1, 100, config.apply.max_applications_per_day)
                max_per_portal = st.number_input("Max per Portal", 1, 50, config.apply.max_per_portal)

            st.markdown("---")
            st.markdown("**CV Versions**")
            st.caption("Map each CV name to a file in data/cvs/")
            cv_configs = []
            for i, v in enumerate(config.cvs.versions):
                c1, c2, c3 = st.columns(3)
                name = c1.text_input("Name", value=v.name, key=f"cvn_{i}")
                file = c2.text_input("File", value=v.file, key=f"cvf_{i}")
                desc = c3.text_input("Description", value=v.description, key=f"cvd_{i}")
                cv_configs.append({"name": name, "file": file, "description": desc})

            if st.form_submit_button("Save Search Config", type="primary"):
                # Read existing YAML and update
                if CONFIG_PATH.exists():
                    with open(CONFIG_PATH) as f:
                        raw = yaml.safe_load(f) or {}
                else:
                    raw = {}

                raw.setdefault("search", {})
                raw["search"]["keywords"] = [k.strip() for k in keywords.strip().split("\n") if k.strip()]
                raw["search"]["locations"] = [l.strip() for l in locations.strip().split("\n") if l.strip()]
                raw["search"]["experience_years"] = experience
                raw["search"]["excluded_companies"] = [c.strip() for c in excluded.strip().split("\n") if c.strip()]

                raw.setdefault("matching", {})
                raw["matching"]["keyword_min_score"] = kw_min
                raw["matching"]["ai_min_score"] = ai_min

                raw.setdefault("apply", {})
                raw["apply"]["max_applications_per_day"] = max_per_day
                raw["apply"]["max_per_portal"] = max_per_portal

                raw.setdefault("cvs", {})
                raw["cvs"]["directory"] = "data/cvs"
                raw["cvs"]["versions"] = [c for c in cv_configs if c["name"]]

                with open(CONFIG_PATH, "w") as f:
                    yaml.dump(raw, f, default_flow_style=False, sort_keys=False)

                st.success("Search config saved to settings.yaml")

    # ── Portal Config ──
    with tab_portals:
        st.subheader("Portal Configuration")

        config = load_config()

        with st.form("portal_form"):
            portal_settings = {}
            for portal in PORTAL_NAMES:
                pc = config.portals.get(portal)
                col1, col2, col3 = st.columns([2, 1, 1])
                col1.write(f"**{portal.title()}**")
                enabled = col2.checkbox("Enabled", value=pc.enabled if pc else True, key=f"pe_{portal}")
                auto = col3.checkbox(
                    "Auto-Apply",
                    value=pc.auto_apply if pc else (portal not in ("linkedin", "glassdoor")),
                    key=f"pa_{portal}",
                    disabled=portal in ("linkedin", "glassdoor"),
                )
                portal_settings[portal] = {"enabled": enabled, "auto_apply": auto}

            if st.form_submit_button("Save Portal Config", type="primary"):
                if CONFIG_PATH.exists():
                    with open(CONFIG_PATH) as f:
                        raw = yaml.safe_load(f) or {}
                else:
                    raw = {}

                raw["portals"] = portal_settings

                with open(CONFIG_PATH, "w") as f:
                    yaml.dump(raw, f, default_flow_style=False, sort_keys=False)

                st.success("Portal config saved to settings.yaml")


# ─── Page: Run ───


def render_run_page() -> None:
    """Run page — trigger dry-runs and real applications from the dashboard."""
    st.header("Run Job Search & Apply")

    # Detect if Playwright is available (only needed for Real Apply)
    try:
        import playwright  # noqa: F401
        _has_playwright = True
    except ImportError:
        _has_playwright = False

    if not _has_playwright:
        st.info(
            "**Dry Run** and **Scrape Only** work here (uses HTTP, no browser needed). "
            "**Real Apply** requires Playwright — install locally with: "
            "`pip install playwright && playwright install chromium`"
        )

    # Initialize session state
    if "run_process" not in st.session_state:
        st.session_state.run_process = None
    if "run_log" not in st.session_state:
        st.session_state.run_log = ""
    if "run_status" not in st.session_state:
        st.session_state.run_status = "idle"  # idle, running, done, failed

    is_running = st.session_state.run_status == "running"

    # ── Controls ──
    with st.container(border=True):
        st.subheader("Configuration")

        col1, col2 = st.columns(2)
        with col1:
            mode = st.radio(
                "Mode",
                ["Dry Run (search only, no applications)", "Real Apply", "Scrape Only (discover jobs)"],
                key="run_mode",
                disabled=is_running,
            )
            portals = st.multiselect(
                "Portals",
                PORTAL_NAMES,
                default=PORTAL_NAMES,
                key="run_portals",
                disabled=is_running,
            )
        with col2:
            limit = st.number_input(
                "Max applications per portal",
                min_value=1, max_value=50, value=5,
                key="run_limit",
                disabled=is_running,
            )
            headless = st.checkbox("Headless browser (no visible window)", value=True, key="run_headless", disabled=is_running)

        # ── Start / Stop buttons ──
        btn_col1, btn_col2, _ = st.columns([1, 1, 3])
        with btn_col1:
            start_clicked = st.button(
                "Start Run", type="primary", disabled=is_running or not portals, key="start_btn",
            )
        with btn_col2:
            stop_clicked = st.button(
                "Stop Run", disabled=not is_running, key="stop_btn",
            )

    # ── Handle Stop ──
    if stop_clicked and st.session_state.run_process:
        try:
            st.session_state.run_process.terminate()
            st.session_state.run_process.wait(timeout=5)
        except Exception:
            try:
                st.session_state.run_process.kill()
            except Exception:
                pass
        st.session_state.run_status = "idle"
        st.session_state.run_log += "\n--- Run stopped by user ---\n"
        st.session_state.run_process = None
        st.rerun()

    # ── Handle Start ──
    if start_clicked and portals:
        # Build CLI command
        cmd = [sys.executable, "-m", "src"]

        if "Dry Run" in mode:
            cmd.append("--dry-run")
        elif "Scrape Only" in mode:
            cmd.append("--scrape-only")

        for portal in portals:
            cmd.extend(["--portal", portal])

        cmd.extend(["--limit", str(limit)])

        st.session_state.run_log = f"$ {' '.join(cmd)}\n\n"
        st.session_state.run_status = "running"

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=str(PROJECT_ROOT),
            )
            st.session_state.run_process = process
        except Exception as e:
            st.session_state.run_status = "failed"
            st.session_state.run_log += f"Failed to start: {e}\n"
            st.session_state.run_process = None

    # ── Live log output ──
    if st.session_state.run_status == "running" and st.session_state.run_process:
        process = st.session_state.run_process

        status_container = st.empty()
        log_container = st.empty()

        status_container.info("Running... please wait. Logs will appear below.")

        # Read all available output
        lines_read = 0
        while True:
            line = process.stdout.readline()
            if line:
                st.session_state.run_log += line
                lines_read += 1
            else:
                if process.poll() is not None:
                    break
                # Small sleep to avoid busy-waiting, then check again
                time.sleep(0.5)
                continue

            # Update display every few lines
            if lines_read % 3 == 0:
                log_container.code(st.session_state.run_log, language="log")

        # Read any remaining output
        remaining = process.stdout.read()
        if remaining:
            st.session_state.run_log += remaining

        # Final status
        return_code = process.returncode
        st.session_state.run_process = None

        if return_code == 0:
            st.session_state.run_status = "done"
            st.session_state.run_log += "\n--- Run completed successfully ---\n"
        else:
            st.session_state.run_status = "failed"
            st.session_state.run_log += f"\n--- Run failed (exit code {return_code}) ---\n"

        status_container.empty()

    # ── Show status and log ──
    if st.session_state.run_status == "done":
        st.success("Run completed successfully! Check 'Jobs Feed' and 'Applications' pages for results.")
    elif st.session_state.run_status == "failed":
        st.error("Run failed. Check the log below for details.")

    if st.session_state.run_log:
        st.subheader("Run Log")
        st.code(st.session_state.run_log, language="log")

        if st.button("Clear Log"):
            st.session_state.run_log = ""
            st.session_state.run_status = "idle"
            st.rerun()


# ─── Page: LinkedIn Optimizer ───

LINKEDIN_OPTIMIZER_PROMPT = """You are a senior LinkedIn strategist and recruiter with 10+ years \
optimizing profiles for top-tier hiring and creator growth.

Here is the scraped LinkedIn profile:
<profile>
{profile_content}
</profile>

The user's goal is: {user_goal}
(If blank, assume: "attract senior-level opportunities in their domain")

Ignore any LinkedIn UI boilerplate, ads, or "People Also Viewed" content.

Analyze the profile and provide specific rewrites — not suggestions — across all 7 dimensions:

1. **Headline** — Is it keyword-rich, role-specific, value-forward? Rewrite it.
2. **About/Summary** — Hook in line 1? Compelling arc? Rewrite it.
3. **Experience Bullets** — Find the 3 weakest (no metric/passive/task-only). \
Rewrite each as: [Verb] + [Action] + [Measurable Result].
4. **Featured Section** — What 2-3 items should be pinned for the user's goal?
5. **Skills & Keywords** — What's missing for SEO + recruiter discoverability?
6. **Creator/Posting Signals** — Gaps in niche authority or content presence?
7. **CTA** — Does the profile have a clear next step? Rewrite if missing.

Return output as JSON only — no markdown, no preamble:
{{
  "headline": {{ "score": 0, "issues": [], "rewrite": "" }},
  "about": {{ "score": 0, "issues": [], "rewrite": "" }},
  "experience_bullets": {{ "weak_bullets": [], "rewrites": [] }},
  "featured": {{ "recommendations": [] }},
  "skills": {{ "missing": [], "suggested_additions": [] }},
  "creator_signals": {{ "gaps": [], "quick_wins": [] }},
  "cta": {{ "present": false, "rewrite": "" }},
  "top_3_priority_fixes": []
}}"""


def render_linkedin_optimizer() -> None:
    """LinkedIn Profile Optimizer — paste your profile and get AI rewrites."""
    st.header("LinkedIn Profile Optimizer")
    st.caption("Paste your LinkedIn profile content and get AI-powered rewrites to boost visibility.")

    # Check for API key
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key and ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            if line.startswith("ANTHROPIC_API_KEY="):
                api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                break

    if not api_key:
        st.warning("Set your ANTHROPIC_API_KEY in Settings > Credentials to use this feature.")
        return

    col1, col2 = st.columns([3, 1])
    with col1:
        user_goal = st.text_input(
            "Your goal",
            placeholder="e.g., Land a senior backend engineer role at a FAANG company",
            key="li_goal",
        )
    with col2:
        st.write("")  # spacing
        st.write("")
        analyze_btn = st.button("Analyze Profile", type="primary", key="li_analyze")

    profile_content = st.text_area(
        "Paste your LinkedIn profile content",
        height=300,
        placeholder=(
            "Copy everything from your LinkedIn profile page:\n"
            "- Headline\n"
            "- About section\n"
            "- Experience (all roles + bullets)\n"
            "- Skills\n"
            "- Featured items\n\n"
            "Tip: Go to your LinkedIn profile, select all (Ctrl+A), copy (Ctrl+C), paste here."
        ),
        key="li_profile",
    )

    if analyze_btn:
        if not profile_content.strip():
            st.error("Please paste your LinkedIn profile content first.")
            return

        with st.spinner("Analyzing your profile with Claude AI..."):
            try:
                import anthropic

                client = anthropic.Anthropic(api_key=api_key)
                prompt = LINKEDIN_OPTIMIZER_PROMPT.format(
                    profile_content=profile_content,
                    user_goal=user_goal or "attract senior-level opportunities in their domain",
                )

                response = client.messages.create(
                    model="claude-sonnet-4-20250514",
                    max_tokens=4096,
                    messages=[{"role": "user", "content": prompt}],
                )

                result_text = response.content[0].text
                st.session_state.li_result = result_text

            except Exception as e:
                st.error(f"Analysis failed: {e}")
                return

    # Display results
    if "li_result" in st.session_state and st.session_state.li_result:
        result_text = st.session_state.li_result

        # Try to parse as JSON for structured display
        import json
        try:
            data = json.loads(result_text)
            _render_optimizer_results(data)
        except json.JSONDecodeError:
            # If not valid JSON, show raw output
            st.subheader("Analysis Results")
            st.markdown(result_text)


def _render_optimizer_results(data: dict) -> None:
    """Render structured LinkedIn optimizer results."""

    # Top priorities
    if data.get("top_3_priority_fixes"):
        st.subheader("Top 3 Priority Fixes")
        for i, fix in enumerate(data["top_3_priority_fixes"], 1):
            st.markdown(f"**{i}.** {fix}")
        st.divider()

    # Headline
    if "headline" in data:
        h = data["headline"]
        col1, col2 = st.columns([1, 5])
        col1.metric("Headline", f"{h.get('score', '?')}/10")
        with col2:
            st.subheader("Headline")
            if h.get("issues"):
                for issue in h["issues"]:
                    st.markdown(f"- {issue}")
            if h.get("rewrite"):
                st.success(f"**Rewrite:** {h['rewrite']}")

    # About
    if "about" in data:
        a = data["about"]
        col1, col2 = st.columns([1, 5])
        col1.metric("About", f"{a.get('score', '?')}/10")
        with col2:
            st.subheader("About / Summary")
            if a.get("issues"):
                for issue in a["issues"]:
                    st.markdown(f"- {issue}")
            if a.get("rewrite"):
                st.info(f"**Rewrite:**\n\n{a['rewrite']}")

    # Experience
    if "experience_bullets" in data:
        exp = data["experience_bullets"]
        st.subheader("Experience Bullets")
        weak = exp.get("weak_bullets", [])
        rewrites = exp.get("rewrites", [])
        for i, (old, new) in enumerate(zip(weak, rewrites)):
            st.markdown(f"**Weak:** {old}")
            st.success(f"**Rewrite:** {new}")
            if i < len(weak) - 1:
                st.write("")

    # Featured
    if "featured" in data and data["featured"].get("recommendations"):
        st.subheader("Featured Section")
        for rec in data["featured"]["recommendations"]:
            st.markdown(f"- {rec}")

    # Skills
    if "skills" in data:
        sk = data["skills"]
        st.subheader("Skills & Keywords")
        col1, col2 = st.columns(2)
        with col1:
            if sk.get("missing"):
                st.markdown("**Missing keywords:**")
                for m in sk["missing"]:
                    st.markdown(f"- {m}")
        with col2:
            if sk.get("suggested_additions"):
                st.markdown("**Add these:**")
                for s in sk["suggested_additions"]:
                    st.markdown(f"- {s}")

    # Creator signals
    if "creator_signals" in data:
        cs = data["creator_signals"]
        st.subheader("Creator / Posting Signals")
        col1, col2 = st.columns(2)
        with col1:
            if cs.get("gaps"):
                st.markdown("**Gaps:**")
                for g in cs["gaps"]:
                    st.markdown(f"- {g}")
        with col2:
            if cs.get("quick_wins"):
                st.markdown("**Quick wins:**")
                for q in cs["quick_wins"]:
                    st.markdown(f"- {q}")

    # CTA
    if "cta" in data:
        cta = data["cta"]
        st.subheader("Call to Action")
        present = cta.get("present", False)
        st.markdown(f"**CTA present:** {'Yes' if present else 'No'}")
        if cta.get("rewrite"):
            st.success(f"**Rewrite:** {cta['rewrite']}")


# ─── Main ───


def main() -> None:
    st.set_page_config(page_title="Auto-Apply Dashboard", page_icon="📋", layout="wide")
    init_db()

    st.sidebar.title("Auto-Apply CV Jobs")
    page = st.sidebar.radio("Navigation", [
        "Run",
        "Jobs Feed",
        "Applications",
        "Manual Apply Queue",
        "Daily Stats",
        "LinkedIn Optimizer",
        "Settings",
    ])

    if page == "Run":
        render_run_page()
    elif page == "Jobs Feed":
        render_jobs_feed()
    elif page == "Applications":
        render_applications()
    elif page == "Manual Apply Queue":
        render_manual_queue()
    elif page == "Daily Stats":
        render_daily_stats()
    elif page == "LinkedIn Optimizer":
        render_linkedin_optimizer()
    elif page == "Settings":
        render_settings()


if __name__ == "__main__":
    main()
