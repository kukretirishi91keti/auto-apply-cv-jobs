"""Streamlit web dashboard for Auto-Apply CV Jobs."""

from __future__ import annotations

import os
import re
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
    get_cloud_apply_queue,
    mark_manually_applied,
    save_generated_content,
    get_generated_content,
    get_daily_stats,
    get_portal_summary,
)
from src.auth import (
    is_multi_user_enabled,
    authenticate,
    load_users,
    save_users,
    add_user,
    remove_user,
    get_user_paths,
    get_default_paths,
    ensure_user_config,
    _hash_password,
)

PORTALS = ["All", "naukri", "indeed", "foundit", "ziprecruiter", "linkedin", "glassdoor", "remoteok", "weworkremotely", "jsearch", "adzuna"]
PORTAL_NAMES = ["naukri", "indeed", "foundit", "ziprecruiter", "linkedin", "glassdoor"]
STATUS_COLORS = {
    "applied": "🟢",
    "manually_applied": "🔵",
    "pending": "🟡",
    "scrape_only": "🟠",
    "failed": "🔴",
}

# ── Default paths (single-user fallback) ──
_DEFAULT_CV_DIR = PROJECT_ROOT / "data" / "cvs"
_DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"
_DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "settings.yaml"


def _get_user_id() -> str | None:
    """Get current logged-in user ID from session state."""
    return st.session_state.get("user_id")


def _get_paths() -> dict[str, Path]:
    """Get active paths — user-specific if logged in, else defaults."""
    user_id = _get_user_id()
    if user_id:
        return get_user_paths(user_id)
    return get_default_paths()


def _cv_dir() -> Path:
    return _get_paths()["cv_dir"]


def _env_path() -> Path:
    return _get_paths()["env_path"]


def _config_path() -> Path:
    return _get_paths()["config_path"]


def _db_path() -> Path:
    return _get_paths()["db_path"]


def rows_to_df(rows: list) -> pd.DataFrame:
    """Convert sqlite3.Row list to DataFrame."""
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([dict(row) for row in rows])


# ─── Page: Jobs Feed ───


def _setup_user_session(user_id: str) -> None:
    """Configure session for a specific user — set DB path, ensure dirs exist."""
    from src.db import set_db_path
    paths = get_user_paths(user_id)
    set_db_path(paths["db_path"])
    paths["cv_dir"].mkdir(parents=True, exist_ok=True)
    ensure_user_config(user_id)
    _restore_secrets_to_env()
    init_db(paths["db_path"])

    # Load user-specific .env into os.environ so credentials are available
    # both in-process AND inherited by subprocess
    env_file = paths["env_path"]
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip()
                if val:
                    os.environ[key] = val


def _restore_secrets_to_env() -> None:
    """Load Streamlit Cloud secrets into .env so credentials survive reboots.

    On Streamlit Cloud, the filesystem is ephemeral — .env files written at
    runtime are lost on every reboot. But st.secrets (set via the dashboard's
    Secrets UI) persists. This copies them into .env + os.environ on startup.
    """
    try:
        secrets = dict(st.secrets)
    except Exception:
        return

    if not secrets:
        return

    env_path = _env_path()
    existing_env: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                existing_env[key.strip()] = val.strip()

    merged = {**existing_env}
    for key, val in secrets.items():
        if isinstance(val, str) and val:
            merged[key] = val
            os.environ[key] = val

    if merged != existing_env:
        env_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"{k}={v}" for k, v in merged.items()]
        env_path.write_text("\n".join(lines) + "\n")
        root_env = PROJECT_ROOT / ".env"
        root_env.write_text("\n".join(lines) + "\n")


def _setup_default_session() -> None:
    """Configure session for single-user mode (default paths)."""
    from src.db import set_db_path
    set_db_path(None)  # use default DB_PATH
    _DEFAULT_CV_DIR.mkdir(parents=True, exist_ok=True)
    _restore_secrets_to_env()
    init_db()


def render_login() -> bool:
    """Render login page. Returns True if authenticated."""
    st.set_page_config(page_title="Auto-Apply - Login", page_icon="🔐", layout="centered")
    st.title("Auto-Apply CV Jobs")
    st.subheader("Login")

    with st.form("login_form"):
        email = st.text_input("Email", key="login_email")
        password = st.text_input("Password", type="password", key="login_password")
        submitted = st.form_submit_button("Login", type="primary")

    if submitted:
        if not email or not password:
            st.error("Please enter both email and password.")
            return False

        user = authenticate(email, password)
        if user:
            st.session_state.user_id = user["id"]
            st.session_state.user_name = user.get("name", email)
            st.session_state.user_email = user.get("email", email)
            st.session_state.user_is_admin = user.get("is_admin", False)
            st.session_state.authenticated = True
            _setup_user_session(user["id"])
            st.rerun()
        else:
            st.error("Invalid email or password.")

    st.divider()
    st.caption("Contact the admin to get access.")
    return False


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


# ─── Page: Cloud Apply Assistant ───


def _get_api_key() -> str:
    """Get Anthropic API key from env or .env file."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key and _env_path().exists():
        for line in _env_path().read_text().splitlines():
            if line.startswith("ANTHROPIC_API_KEY="):
                api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    return api_key


def _load_cv_and_config():
    """Load config, credentials, and CV texts. Cached per session."""
    from src.cv_manager import load_all_cvs
    from src.config import get_config, get_credentials
    cfg = get_config(_config_path())
    crd = get_credentials(_env_path())
    cv_texts = load_all_cvs(cfg, _cv_dir())
    return cfg, crd, cv_texts


def _generate_cover_letter_for_job(title, company, description, cfg, crd, cv_texts):
    """Generate a cover letter for a specific job."""
    from src.cover_letter import generate_cover_letter
    cv_text = next(iter(cv_texts.values()), "")
    if not cv_text:
        return ""
    user_name = st.session_state.get("user_name", "")
    clean_name = re.sub(r"\.(pdf|docx?|txt)$", "", user_name, flags=re.IGNORECASE).strip()
    return generate_cover_letter(title, company, description or "", cv_text, cfg, crd, candidate_name=clean_name)


def _build_education_block(cfg) -> str:
    """Build education + certifications text from config for the CV/CL prompt."""
    lines = []

    for e in (cfg.education or []):
        if not e.degree:
            continue
        parts = [e.degree]
        if e.institution:
            parts.append(e.institution)
        if e.year:
            parts.append(e.year)
        if e.cgpa:
            parts.append(e.cgpa)
        line = " | ".join(parts)
        if e.details:
            line += f" — {e.details}"
        lines.append(line)

    cert_lines = []
    for c in (cfg.certifications or []):
        if not c.name:
            continue
        parts = [c.name]
        if c.issuer:
            parts.append(c.issuer)
        if c.year:
            parts.append(c.year)
        cert_lines.append(" | ".join(parts))

    if cert_lines:
        lines.append("Certifications & Achievements: " + "; ".join(cert_lines))

    return "\n".join(lines)


def _generate_tailored_cv_for_job(title, company, description, cfg, crd, cv_texts):
    """Generate a tailored CV for a specific job."""
    import anthropic
    cv_text = next(iter(cv_texts.values()), "")
    if not cv_text:
        return ""

    edu_block = _build_education_block(cfg)
    if edu_block:
        edu_instruction = (
            f"EDUCATION & CERTIFICATIONS\n{edu_block}\n"
            "[Use EXACTLY the education and certification details above. "
            "Do NOT modify, omit, or fabricate any entry. "
            "List certifications as a sub-section under EDUCATION.]"
        )
    else:
        edu_instruction = (
            "EDUCATION\n"
            "[Degree | Institution | Year | CGPA — ONLY if education details appear in the CV.\n"
            "If the CV does not mention education, OMIT this entire section. Never write\n"
            'placeholder text like "Education details not provided".]'
        )

    client = anthropic.Anthropic(api_key=crd.anthropic_api_key)
    prompt = f"""You are a senior career coach creating an ATS-optimized tailored CV.

CRITICAL — ZERO TOLERANCE FOR FABRICATION:
- You must ONLY use companies, roles, dates, and achievements that appear VERBATIM in
  the candidate's actual CV text below.
- Do NOT invent ANY company name, job title, role, date, or achievement.
- If the CV lists 1 employer, output 1 employer. If it lists 2, output 2. Never add more.
- Before writing each company name and role, verify it appears in the CV text below.
- If you write a company name that does NOT appear in the CV text, the output is INVALID.

ATS OPTIMIZATION — most companies use Applicant Tracking Systems that scan CVs for:
1. EXACT keyword matches from the job description — mirror them verbatim
2. Standard section headers (PROFESSIONAL SUMMARY, PROFESSIONAL EXPERIENCE, EDUCATION, SKILLS)
3. Job-title and skill-term density — repeat key terms naturally 2-3 times across sections
4. Measurable achievements with numbers (%, Rs., Cr, revenue, growth, team size)

OUTPUT FORMAT — use this exact structure with ALL CAPS section headers:

PROFESSIONAL SUMMARY
[4-5 lines. Open with "Seasoned [domain] professional with specialised experience in..."
Do NOT mention specific number of years. Weave in 5-6 keywords directly from the job
description. Mention current company and role. Be specific.]

CORE COMPETENCIES
[12-15 skills separated by " | " on 2-3 lines. Pull terms DIRECTLY from the job
description first, then add the candidate's strongest skills. This section is the
primary ATS keyword match zone.]

PROFESSIONAL EXPERIENCE
[Company Name | Role Title | Duration — ONLY from the actual CV]
- [Achievement bullet with metrics — ONLY real achievements from the CV]
- [Achievement bullet with metrics]
- [Achievement bullet with metrics]
- [Achievement bullet with metrics]
[Include ALL roles from the actual CV, with 3-5 bullets each. Reframe bullets
to emphasize relevance to the target job, but never change the facts.
Naturally embed job description keywords into bullet text where truthful.
LABEL each bullet you use here as USED — they must NOT appear again in KEY ACHIEVEMENTS.]

KEY ACHIEVEMENTS
[Pick the 5-6 most impressive ADDITIONAL achievements from the CV that are NOT already
listed in PROFESSIONAL EXPERIENCE above. These must be DIFFERENT bullets — not repeats.
If the CV has achievements from multiple domains (e.g. brand, digital, product, P&L),
spread across domains to show breadth. Prioritise whichever domain is most relevant
to this specific job description. Every bullet must include a metric.]
- [Different achievement not in experience section]
- [Different achievement not in experience section]
- [Different achievement not in experience section]
- [Different achievement not in experience section]
- [Different achievement not in experience section]

{edu_instruction}

METRIC EXPRESSION RULE — CRITICAL:
Express all growth achievements as MULTIPLIERS or RATIOS, never as "from X to Y" ranges.
- "from 40% to 74%" → "1.7X improvement" or "increased 1.7X"
- "from Rs. 30 Lacs to Rs. 5 Crores" → "scaled 15X" or "grew media spends 15-fold"
- "from Rs. 200 Cr to Rs. 1,100 Cr" → "grew 5.5X"
- "from 10 to 35" → "improved 3.5X"
- "40,000+ leads in 30 days" stays as-is — it is a destination/output, not a range
- "Rs. 1,500 Crores revenue" stays as-is — it is a total, not a range
- RULE: if the CV says "from A to B", compute B/A and write "NX" — never write both A and B

RULES:
- Output ONLY the CV text — no commentary, no preamble, no markdown bold (**)
- Use ALL CAPS for section headers (ATS parsers rely on standard headers)
- Use " | " to separate items in skill lists and job title lines
- Each bullet must start with "- " and include a metric (multiplier, Rs., Cr, or count)
- Target 500-600 words (full 1 page when formatted as PDF)
- NEVER mention specific number of years of experience (e.g. "10 years", "8+ years")
  Instead use "Seasoned professional" or "Extensive experience" — let the CV speak for itself
- NEVER fabricate companies, roles, or achievements not in the candidate's CV
- Do NOT include candidate name, email, or phone — those are added separately
- Mirror exact phrases from the job description (e.g. if JD says "go-to-market
  strategy", use that exact phrase, not "GTM" alone)
- ALWAYS include the EDUCATION & CERTIFICATIONS section — ATS systems flag CVs missing education
- List certifications/achievements as a sub-section or inline under EDUCATION

Job Title: {title}
Company: {company}
Job Description:
{(description or 'No description available')[:3000]}

Candidate's ACTUAL CV (use ONLY facts from this):
{cv_text[:5000]}

Write the tailored CV content now:"""
    response = client.messages.create(
        model=cfg.matching.ai_model,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


def _generate_recruiter_message_for_job(title, company, description, cfg, crd, cv_texts):
    """Generate a LinkedIn recruiter outreach message for a job."""
    import anthropic
    cv_text = next(iter(cv_texts.values()), "")
    if not cv_text:
        return ""
    client = anthropic.Anthropic(api_key=crd.anthropic_api_key)
    prompt = f"""Write a short LinkedIn connection request / InMail message to a recruiter or \
hiring manager for this role. The candidate wants to express interest and stand out.

RULES:
- Max 280 characters for connection note, OR ~150 words for InMail
- Provide BOTH versions (label them "Connection Note:" and "InMail:")
- Be specific — mention the role, one key qualification, and a hook
- Sound human, not AI-generated — no buzzwords like "passionate" or "leveraging"
- End with a clear ask (chat, call, learn more)

Job Title: {title}
Company: {company}
Job Description:
{(description or '')[:2000]}

Candidate Background:
{cv_text[:2000]}

Write both messages now:"""
    response = client.messages.create(
        model=cfg.matching.ai_model,
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


def render_manual_queue() -> None:
    """Cloud Apply Assistant — streamlined for 50+ applications/day."""
    st.header("Cloud Apply Assistant")
    st.caption(
        "Generate cover letters, tailored CVs, and recruiter messages — then download as PDF and apply. "
        "Everything is auto-saved to the database."
    )

    # ── Quick CV download bar (use original PDF as-is on portal) ──
    _cv_dir_path = _cv_dir()
    if _cv_dir_path.exists():
        _orig_cvs = sorted(_cv_dir_path.glob("*.pdf")) + sorted(_cv_dir_path.glob("*.docx")) + sorted(_cv_dir_path.glob("*.doc"))
        if _orig_cvs:
            with st.container(border=True):
                st.caption("**Your CV files** — download and attach directly to portal applications:")
                dl_cols = st.columns(min(len(_orig_cvs), 4))
                for _ci, _cv_file in enumerate(_orig_cvs[:4]):
                    with dl_cols[_ci]:
                        st.download_button(
                            f"Download: {_cv_file.stem[:30]}",
                            data=_cv_file.read_bytes(),
                            file_name=_cv_file.name,
                            mime="application/pdf" if _cv_file.suffix.lower() == ".pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                            key=f"dl_quick_cv_{_cv_file.stem}",
                        )

    # ── Filters ──
    col1, col2, col3, col4 = st.columns([2, 2, 2, 1])
    with col1:
        portal_filter = st.selectbox("Portal", PORTALS, key="caq_portal")
    with col2:
        min_score = st.slider("Min AI Score", 0.0, 1.0, 0.0, 0.1, key="caq_score")
    with col3:
        view_mode = st.radio("View", ["Best Matches", "All Jobs"], key="caq_view", horizontal=True)
    with col4:
        page_size = st.selectbox("Per page", [10, 25, 50], key="caq_page_size")

    portal_val = portal_filter if portal_filter != "All" else None
    score_val = min_score if min_score > 0 else None

    if view_mode == "Best Matches":
        queue = get_cloud_apply_queue(min_ai_score=score_val, portal=portal_val, limit=200)
    else:
        queue = get_cloud_apply_queue(portal=portal_val, limit=200)

    # Also include legacy manual queue
    legacy_queue = get_manual_apply_queue()
    queue_ids = {dict(r).get("job_id") for r in queue}
    all_items = list(queue)
    for r in legacy_queue:
        if dict(r).get("job_id") not in queue_ids:
            all_items.append(r)

    if not all_items:
        st.info("No jobs in queue. Run a **Dry Run** first to discover and score jobs.")
        return

    # ── Stats bar ──
    scored = [dict(r) for r in all_items if dict(r).get("ai_score")]
    unscored = [dict(r) for r in all_items if not dict(r).get("ai_score")]
    cols = st.columns(4)
    cols[0].metric("Total Jobs", len(all_items))
    cols[1].metric("AI Scored", len(scored))
    cols[2].metric("Unscored", len(unscored))
    generated_count = sum(1 for r in all_items if dict(r).get("saved_cover_letter"))
    cols[3].metric("Generated", generated_count)

    if unscored and not scored:
        st.warning(
            "Jobs found but none have AI scores. To score jobs:\n"
            "1. **Upload your CV** in Settings > CV Management\n"
            "2. **Set your Anthropic API key** in Settings > Credentials\n"
            "3. Run a **Dry Run** -- jobs will be scored against your CV"
        )

    api_key = _get_api_key()
    has_cv = any(_cv_dir().glob("*.*")) if _cv_dir().exists() else False

    # ── Batch Actions ──
    if api_key and has_cv:
        st.divider()
        batch_col1, batch_col2, batch_col3 = st.columns(3)

        with batch_col1:
            batch_count = st.number_input("Jobs to process", 1, min(50, len(all_items)), min(10, len(all_items)), key="batch_n")
        with batch_col2:
            st.write("")
            st.write("")
            batch_generate = st.button("Batch Generate All (CL + CV + Recruiter Msg)", type="primary", key="batch_gen")
        with batch_col3:
            st.write("")
            st.write("")
            batch_what = st.multiselect(
                "Generate",
                ["Cover Letter", "Tailored CV", "Recruiter Message"],
                default=["Cover Letter", "Tailored CV"],
                key="batch_what",
            )

        if batch_generate:
            cfg, crd, cv_texts = _load_cv_and_config()
            if not cv_texts:
                st.error("No CVs found. Upload in Settings > CV Management.")
            else:
                progress = st.progress(0, text="Starting batch generation...")
                items_to_process = all_items[:batch_count]

                for idx, row in enumerate(items_to_process):
                    job = dict(row)
                    job_id = job.get("job_id")
                    title = job.get("title", "")
                    company = job.get("company", "")
                    description = job.get("description", "")

                    progress.progress(
                        (idx + 1) / len(items_to_process),
                        text=f"[{idx+1}/{len(items_to_process)}] {title} at {company}",
                    )

                    try:
                        cl = ""
                        tcv = ""
                        rm = ""

                        if "Cover Letter" in batch_what:
                            existing = st.session_state.get(f"cover_letter_{job_id}")
                            if not existing:
                                cl = _generate_cover_letter_for_job(title, company, description, cfg, crd, cv_texts)
                                st.session_state[f"cover_letter_{job_id}"] = cl
                            else:
                                cl = existing

                        if "Tailored CV" in batch_what:
                            existing = st.session_state.get(f"cv_tailor_{job_id}")
                            if not existing:
                                tcv = _generate_tailored_cv_for_job(title, company, description, cfg, crd, cv_texts)
                                st.session_state[f"cv_tailor_{job_id}"] = tcv
                            else:
                                tcv = existing

                        if "Recruiter Message" in batch_what:
                            existing = st.session_state.get(f"recruiter_msg_{job_id}")
                            if not existing:
                                rm = _generate_recruiter_message_for_job(title, company, description, cfg, crd, cv_texts)
                                st.session_state[f"recruiter_msg_{job_id}"] = rm
                            else:
                                rm = existing

                        save_generated_content(job_id, cover_letter=cl, tailored_cv_text=tcv, recruiter_message=rm)

                    except Exception as e:
                        st.warning(f"Failed for {title}: {e}")

                progress.progress(1.0, text="Batch generation complete!")
                st.success(f"Generated content for {len(items_to_process)} jobs. Scroll down to review and download PDFs.")
                time.sleep(1)
                st.rerun()

    st.divider()

    # ── Pagination ──
    total_pages = max(1, (len(all_items) + page_size - 1) // page_size)
    if "caq_page" not in st.session_state:
        st.session_state.caq_page = 0
    current_page = st.session_state.caq_page

    if total_pages > 1:
        pg_col1, pg_col2, pg_col3 = st.columns([1, 3, 1])
        with pg_col1:
            if st.button("Previous", disabled=current_page <= 0, key="pg_prev"):
                st.session_state.caq_page = max(0, current_page - 1)
                st.rerun()
        with pg_col2:
            st.markdown(f"**Page {current_page + 1} of {total_pages}** ({len(all_items)} jobs)")
        with pg_col3:
            if st.button("Next", disabled=current_page >= total_pages - 1, key="pg_next"):
                st.session_state.caq_page = min(total_pages - 1, current_page + 1)
                st.rerun()

    start_idx = current_page * page_size
    page_items = all_items[start_idx : start_idx + page_size]

    # ── Job Cards ──
    for i, row in enumerate(page_items):
        job = dict(row)
        job_id = job.get("job_id")
        title = job.get("title", "Unknown")
        company = job.get("company", "Unknown")
        url = job.get("url", "")
        location = job.get("location", "")
        portal = job.get("portal", "")
        salary = job.get("salary", "")
        ai_score = job.get("ai_score")
        kw_score = job.get("keyword_score")
        description = job.get("description", "")

        # Load saved content from DB or session
        saved_cl = job.get("saved_cover_letter") or st.session_state.get(f"cover_letter_{job_id}", "")
        saved_cv = job.get("saved_tailored_cv") or st.session_state.get(f"cv_tailor_{job_id}", "")
        saved_rm = job.get("saved_recruiter_msg") or st.session_state.get(f"recruiter_msg_{job_id}", "")

        has_generated = bool(saved_cl or saved_cv or saved_rm)
        card_idx = start_idx + i

        with st.container(border=True):
            # ── Header row ──
            header_col, score_col, action_col = st.columns([3, 1, 2])

            with header_col:
                if url:
                    st.markdown(f"### [{title}]({url})")
                else:
                    st.markdown(f"### {title}")
                portal_badge = portal.upper() if portal else "?"
                info_parts = [f"**{company}**", location, portal_badge]
                if salary:
                    info_parts.append(f"Salary: {salary}")
                st.write(" | ".join(filter(None, info_parts)))

            with score_col:
                if ai_score is not None:
                    score_pct = int(ai_score * 100)
                    color = "green" if score_pct >= 70 else "orange" if score_pct >= 50 else "red"
                    st.markdown(f"**AI: :{color}[{score_pct}%]**")
                if kw_score is not None:
                    st.caption(f"KW: {int(kw_score * 100)}%")

            with action_col:
                btn_c1, btn_c2 = st.columns(2)
                with btn_c1:
                    if url:
                        st.link_button("Apply Now", url, type="primary")
                with btn_c2:
                    if st.button("Mark Applied", key=f"mark_{job_id}_{card_idx}"):
                        mark_manually_applied(job_id)
                        st.success("Marked as applied!")
                        time.sleep(0.5)
                        st.rerun()

            # ── Quick action row: Generate + Download ──
            if api_key and has_cv:
                qa_col1, qa_col2, qa_col3, qa_col4 = st.columns(4)

                with qa_col1:
                    if not saved_cl:
                        if st.button("Gen Cover Letter", key=f"gen_cl_{job_id}_{card_idx}"):
                            with st.spinner("Generating..."):
                                try:
                                    cfg, crd, cv_texts = _load_cv_and_config()
                                    cl = _generate_cover_letter_for_job(title, company, description, cfg, crd, cv_texts)
                                    st.session_state[f"cover_letter_{job_id}"] = cl
                                    save_generated_content(job_id, cover_letter=cl)
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Failed: {e}")
                    else:
                        from src.pdf_generator import generate_cover_letter_pdf
                        user_name = st.session_state.get("user_name", "")
                        clean_name = re.sub(r"\.(pdf|docx?|txt)$", "", user_name, flags=re.IGNORECASE).strip()
                        pdf = generate_cover_letter_pdf(saved_cl, title, company, clean_name)
                        st.download_button(
                            "Download CL PDF",
                            pdf,
                            file_name=f"CoverLetter_{company.replace(' ', '_')}.pdf",
                            mime="application/pdf",
                            key=f"dl_cl_{job_id}_{card_idx}",
                        )

                with qa_col2:
                    if not saved_cv:
                        if st.button("Gen Tailored CV", key=f"gen_cv_{job_id}_{card_idx}"):
                            with st.spinner("Tailoring CV..."):
                                try:
                                    cfg, crd, cv_texts = _load_cv_and_config()
                                    tcv = _generate_tailored_cv_for_job(title, company, description, cfg, crd, cv_texts)
                                    st.session_state[f"cv_tailor_{job_id}"] = tcv
                                    save_generated_content(job_id, tailored_cv_text=tcv)
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Failed: {e}")
                    else:
                        from src.pdf_generator import generate_tailored_cv_pdf
                        user_name = st.session_state.get("user_name", "")
                        clean_name = re.sub(r"\.(pdf|docx?|txt)$", "", user_name, flags=re.IGNORECASE).strip()
                        pdf = generate_tailored_cv_pdf(saved_cv, clean_name)
                        st.download_button(
                            "Download CV PDF",
                            pdf,
                            file_name=f"CV_{company.replace(' ', '_')}.pdf",
                            mime="application/pdf",
                            key=f"dl_cv_{job_id}_{card_idx}",
                        )

                with qa_col3:
                    if not saved_rm:
                        if st.button("Gen Recruiter Msg", key=f"gen_rm_{job_id}_{card_idx}"):
                            with st.spinner("Generating..."):
                                try:
                                    cfg, crd, cv_texts = _load_cv_and_config()
                                    rm = _generate_recruiter_message_for_job(title, company, description, cfg, crd, cv_texts)
                                    st.session_state[f"recruiter_msg_{job_id}"] = rm
                                    save_generated_content(job_id, recruiter_message=rm)
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Failed: {e}")
                    else:
                        st.caption("Recruiter msg ready")

                with qa_col4:
                    if has_generated and not saved_cl and not saved_cv:
                        pass  # nothing to generate all-in-one for
                    elif not has_generated:
                        if st.button("Gen All", key=f"gen_all_{job_id}_{card_idx}", type="secondary"):
                            with st.spinner("Generating CL + CV + Recruiter Msg..."):
                                try:
                                    cfg, crd, cv_texts = _load_cv_and_config()
                                    cl = _generate_cover_letter_for_job(title, company, description, cfg, crd, cv_texts)
                                    tcv = _generate_tailored_cv_for_job(title, company, description, cfg, crd, cv_texts)
                                    rm = _generate_recruiter_message_for_job(title, company, description, cfg, crd, cv_texts)
                                    st.session_state[f"cover_letter_{job_id}"] = cl
                                    st.session_state[f"cv_tailor_{job_id}"] = tcv
                                    st.session_state[f"recruiter_msg_{job_id}"] = rm
                                    save_generated_content(job_id, cover_letter=cl, tailored_cv_text=tcv, recruiter_message=rm)
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Failed: {e}")

            # ── Expandable: view generated content ──
            if has_generated:
                with st.expander("View Generated Content"):
                    tab_cl, tab_cv, tab_rm, tab_desc = st.tabs(["Cover Letter", "Tailored CV", "Recruiter Message", "Job Description"])

                    with tab_cl:
                        if saved_cl:
                            st.text_area("Cover Letter", value=saved_cl, height=200, key=f"view_cl_{job_id}_{card_idx}")
                        else:
                            st.caption("Not generated yet.")

                    with tab_cv:
                        if saved_cv:
                            st.text_area("Tailored CV", value=saved_cv, height=300, key=f"view_cv_{job_id}_{card_idx}")
                        else:
                            st.caption("Not generated yet.")

                    with tab_rm:
                        if saved_rm:
                            st.text_area("Recruiter Message", value=saved_rm, height=150, key=f"view_rm_{job_id}_{card_idx}")
                        else:
                            st.caption("Not generated yet.")

                    with tab_desc:
                        if description:
                            st.text(description[:1000] + ("..." if len(description) > 1000 else ""))
                        else:
                            st.caption("No description available.")

            elif description:
                with st.expander("Job Description"):
                    st.text(description[:500] + ("..." if len(description) > 500 else ""))

            if not api_key or not has_cv:
                missing = []
                if not api_key:
                    missing.append("Anthropic API key")
                if not has_cv:
                    missing.append("CV upload")
                st.caption(f"Set up {' and '.join(missing)} in Settings to generate content.")


# ─── Page: Profile Booster ───


def render_profile_booster() -> None:
    """Profile Booster — analyze CV gaps vs matched jobs and suggest improvements."""
    st.header("Profile Booster")
    st.caption(
        "Analyzes your CV against recently matched jobs to find gaps and suggest "
        "skills, keywords, and experience bullets to add."
    )

    # Check prerequisites
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key and _env_path().exists():
        for line in _env_path().read_text().splitlines():
            if line.startswith("ANTHROPIC_API_KEY="):
                api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                break

    if not api_key:
        st.warning("Set your ANTHROPIC_API_KEY in Settings > Credentials to use this feature.")
        return

    has_cv = any(_cv_dir().glob("*.*")) if _cv_dir().exists() else False
    if not has_cv:
        st.warning("Upload your CV in Settings > CV Management first.")
        return

    # Get matched jobs from DB
    scored_jobs = get_cloud_apply_queue(min_ai_score=0.3, limit=50)
    if not scored_jobs:
        st.info("No scored jobs yet. Run a **Dry Run** first to discover and score jobs.")
        return

    st.metric("Scored Jobs to Analyze", len(scored_jobs))

    # Show current CVs
    from src.cv_manager import load_all_cvs
    from src.config import get_config
    cfg = get_config(_config_path())
    cv_texts = load_all_cvs(cfg, _cv_dir())

    if cv_texts:
        with st.expander("Your Current CVs"):
            for name, text in cv_texts.items():
                st.markdown(f"**{name}** ({len(text)} chars)")
                st.text(text[:500] + "...")

    # Analyze button
    if st.button("Analyze CV Gaps & Boost Profile", type="primary", key="boost_btn"):
        with st.spinner("Analyzing your CV against matched jobs..."):
            try:
                import anthropic

                # Build job summaries
                job_summaries = []
                for row in scored_jobs[:20]:  # top 20 jobs
                    job = dict(row)
                    ai_score = job.get("ai_score", 0) or 0
                    job_summaries.append(
                        f"- {job.get('title', '?')} at {job.get('company', '?')} "
                        f"(AI Score: {ai_score:.1f}, CV: {job.get('selected_cv', '?')})\n"
                        f"  Description: {(job.get('description', '') or '')[:200]}"
                    )

                # Build CV summary
                cv_summary = ""
                for name, text in cv_texts.items():
                    cv_summary += f"\n--- CV: {name} ---\n{text[:2000]}\n"

                prompt = f"""You are a senior career strategist. Analyze this candidate's CVs against \
the jobs they're targeting. Identify specific gaps and provide actionable improvements.

CANDIDATE'S CVs:
{cv_summary}

JOBS THEY'RE TARGETING (with AI match scores 0-1):
{chr(10).join(job_summaries)}

Provide a detailed analysis in this exact structure:

## MATCH PATTERN ANALYSIS
- What types of roles score highest (0.6+) vs lowest?
- What's the common thread in high-scoring matches?

## MISSING KEYWORDS (Top 15)
List specific keywords/phrases that appear in job descriptions but are MISSING from the CVs.
These are hurting keyword matching scores.

## EXPERIENCE GAPS
List 3-5 specific experience areas that would improve match rates.
For each, suggest a bullet point the candidate could truthfully add if they have that experience.

## SKILLS TO ADD
List 10 specific skills (technical + domain) to add to the CV, prioritized by impact.

## QUICK WINS (Do This Today)
5 specific, actionable changes to make RIGHT NOW to boost match rates:
1. [specific change with exact wording]
2. ...

## CV VERSION STRATEGY
Which CV version to use for which job type. Should they create a new CV variant?"""

                client = anthropic.Anthropic(api_key=api_key)
                response = client.messages.create(
                    model=cfg.matching.ai_model,
                    max_tokens=3000,
                    messages=[{"role": "user", "content": prompt}],
                )

                st.session_state.boost_result = response.content[0].text.strip()
            except Exception as e:
                st.error(f"Analysis failed: {e}")

    # Display results
    if "boost_result" in st.session_state and st.session_state.boost_result:
        st.divider()
        st.markdown(st.session_state.boost_result)

        # Download button
        st.download_button(
            "Download Analysis",
            st.session_state.boost_result,
            file_name="profile_boost_analysis.md",
            mime="text/markdown",
        )


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
    if _env_path().exists():
        for line in _env_path().read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                env[key.strip()] = val.strip()
    return env


def _save_env(env: dict[str, str]) -> None:
    """Save .env file to user-specific location AND project root for subprocess."""
    lines = []
    for key, val in env.items():
        lines.append(f"{key}={val}")
    content = "\n".join(lines) + "\n"

    # Save to user-specific path
    _env_path().write_text(content)

    # Also save to project root .env so subprocess always finds it
    root_env = PROJECT_ROOT / ".env"
    root_env.write_text(content)

    # Also set in os.environ for current process + subprocess inheritance
    for key, val in env.items():
        if val:
            os.environ[key] = val


def render_settings() -> None:
    """Settings page — CV upload, credentials, search config."""
    st.header("Settings")

    # ── Tab layout ──
    tab_cv, tab_edu, tab_creds, tab_search, tab_portals = st.tabs([
        "CV Management", "Education", "Credentials", "Search Preferences", "Portal Config",
    ])

    # ── CV Management ──
    with tab_cv:
        st.subheader("Upload CVs")
        st.caption(f"CVs are stored in `{_cv_dir()}`")
        st.info(
            "**Streamlit Cloud users:** Uploaded CVs are lost on app reboot. "
            "To make them permanent, commit your CV files to `data/cvs/` in your "
            "GitHub repo (remove the `data/cvs/*.pdf` line from `.gitignore` first). "
            "They'll be available automatically on every deploy."
        )

        _cv_dir().mkdir(parents=True, exist_ok=True)
        existing_cvs = sorted(_cv_dir().glob("*.*"))

        if existing_cvs:
            st.write("**Current CVs:**")
            for cv_file in existing_cvs:
                col1, col2, col3 = st.columns([4, 1, 1])
                col1.write(f"- {cv_file.name} ({cv_file.stat().st_size // 1024} KB)")
                col2.download_button(
                    "Download",
                    data=cv_file.read_bytes(),
                    file_name=cv_file.name,
                    mime="application/pdf" if cv_file.suffix.lower() == ".pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    key=f"dl_cv_mgmt_{cv_file.stem}",
                )
                if col3.button("Delete", key=f"del_{cv_file.name}"):
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
                dest = _cv_dir() / f.name
                dest.write_bytes(f.getvalue())
                st.success(f"Uploaded: {f.name}")

            st.success("CVs will be auto-detected on next run. No config needed!")

    # ── Education ──
    with tab_edu:
        st.subheader("Education Details")
        st.caption(
            "Add your education and certifications here. These details appear in every generated CV "
            "and cover letter — no more missing or placeholder education sections."
        )

        config = load_config(_config_path())
        existing_edu = config.education or []
        existing_certs = config.certifications or []

        with st.form("education_form"):
            # ── Degrees / Qualifications ──
            st.markdown("#### Degrees & Qualifications")
            edu_entries = []
            num_entries = max(len(existing_edu), 1)

            for i in range(num_entries):
                st.markdown(f"**Qualification {i + 1}**")
                prev = existing_edu[i] if i < len(existing_edu) else None
                c1, c2, c3, c4 = st.columns([3, 3, 1, 1])
                degree = c1.text_input(
                    "Degree / Qualification",
                    value=prev.degree if prev else "",
                    key=f"edu_deg_{i}",
                    placeholder="e.g. MBA (Marketing)",
                )
                institution = c2.text_input(
                    "College / Institution",
                    value=prev.institution if prev else "",
                    key=f"edu_inst_{i}",
                    placeholder="e.g. IIM Bangalore",
                )
                year = c3.text_input(
                    "Year",
                    value=prev.year if prev else "",
                    key=f"edu_year_{i}",
                    placeholder="2015",
                )
                cgpa = c4.text_input(
                    "CGPA / %",
                    value=prev.cgpa if prev else "",
                    key=f"edu_cgpa_{i}",
                    placeholder="8.5/10",
                )
                details = st.text_input(
                    "Achievements / Honours (optional)",
                    value=prev.details if prev else "",
                    key=f"edu_det_{i}",
                    placeholder="e.g. Gold Medalist, Specialization in Finance",
                )
                edu_entries.append({
                    "degree": degree, "institution": institution,
                    "year": year, "cgpa": cgpa, "details": details,
                })

            add_more_edu = st.checkbox("Add another degree / qualification", key="edu_add_more")
            if add_more_edu:
                st.markdown(f"**Qualification {num_entries + 1}**")
                c1, c2, c3, c4 = st.columns([3, 3, 1, 1])
                degree = c1.text_input("Degree / Qualification", key="edu_deg_new", placeholder="e.g. B.Com (Honours)")
                institution = c2.text_input("College / Institution", key="edu_inst_new", placeholder="e.g. St. Xavier's College")
                year = c3.text_input("Year", key="edu_year_new", placeholder="2012")
                cgpa = c4.text_input("CGPA / %", key="edu_cgpa_new", placeholder="7.8/10")
                details = st.text_input("Achievements / Honours (optional)", key="edu_det_new")
                edu_entries.append({
                    "degree": degree, "institution": institution,
                    "year": year, "cgpa": cgpa, "details": details,
                })

            st.divider()

            # ── Certifications & Achievements ──
            st.markdown("#### Certifications & Achievements")
            st.caption(
                "Include professional certifications, executive programmes, olympiad achievements, "
                "and any other credentials (e.g. IISc Executive Programme, AI National Olympiad)."
            )
            cert_entries = []
            num_certs = max(len(existing_certs), 1)

            for i in range(num_certs):
                prev_c = existing_certs[i] if i < len(existing_certs) else None
                c1, c2, c3 = st.columns([4, 3, 1])
                cert_name = c1.text_input(
                    "Certification / Achievement",
                    value=prev_c.name if prev_c else "",
                    key=f"cert_name_{i}",
                    placeholder="e.g. Executive Programme in Management",
                )
                cert_issuer = c2.text_input(
                    "Issuing Body",
                    value=prev_c.issuer if prev_c else "",
                    key=f"cert_issuer_{i}",
                    placeholder="e.g. IISc Bangalore",
                )
                cert_year = c3.text_input(
                    "Year",
                    value=prev_c.year if prev_c else "",
                    key=f"cert_year_{i}",
                    placeholder="2023",
                )
                cert_entries.append({"name": cert_name, "issuer": cert_issuer, "year": cert_year})

            add_more_cert = st.checkbox("Add another certification / achievement", key="cert_add_more")
            if add_more_cert:
                c1, c2, c3 = st.columns([4, 3, 1])
                cert_name = c1.text_input("Certification / Achievement", key="cert_name_new", placeholder="e.g. AI National Olympiad — Top 100")
                cert_issuer = c2.text_input("Issuing Body", key="cert_issuer_new", placeholder="e.g. NASSCOM / Govt of India")
                cert_year = c3.text_input("Year", key="cert_year_new", placeholder="2024")
                cert_entries.append({"name": cert_name, "issuer": cert_issuer, "year": cert_year})

            if st.form_submit_button("Save Education & Certifications", type="primary"):
                valid_edu = [e for e in edu_entries if e["degree"].strip()]
                valid_certs = [c for c in cert_entries if c["name"].strip()]

                if _config_path().exists():
                    with open(_config_path()) as f:
                        raw = yaml.safe_load(f) or {}
                else:
                    raw = {}

                raw["education"] = valid_edu
                raw["certifications"] = valid_certs

                with open(_config_path(), "w") as f:
                    yaml.dump(raw, f, default_flow_style=False, sort_keys=False)

                st.success(f"Saved {len(valid_edu)} education entries and {len(valid_certs)} certifications!")
                st.info("These will appear in all generated CVs and cover letters.")
                st.rerun()

        if existing_edu or existing_certs:
            st.divider()
            if existing_edu:
                st.markdown("**Education on file:**")
                for e in existing_edu:
                    parts = [e.degree]
                    if e.institution:
                        parts.append(e.institution)
                    if e.year:
                        parts.append(e.year)
                    line = " | ".join(parts)
                    if e.cgpa:
                        line += f" | {e.cgpa}"
                    if e.details:
                        line += f" — {e.details}"
                    st.markdown(f"- {line}")
            if existing_certs:
                st.markdown("**Certifications on file:**")
                for c in existing_certs:
                    parts = [c.name]
                    if c.issuer:
                        parts.append(c.issuer)
                    if c.year:
                        parts.append(c.year)
                    st.markdown(f"- {' | '.join(parts)}")

    # ── Credentials ──
    with tab_creds:
        st.subheader("API & Portal Credentials")
        st.caption("Saved to `.env` file (never committed to git)")
        st.info(
            "**Streamlit Cloud users:** Credentials entered here are lost on reboot. "
            "To make them permanent, go to your app's **Settings > Secrets** on Streamlit Cloud "
            "and add them there (e.g. `ANTHROPIC_API_KEY = \"sk-...\"`).\n\n"
            "Secrets are automatically loaded into the app on every startup."
        )

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
        st.caption(f"Saved to `{_config_path()}`")

        config = load_config(_config_path())

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
                if _config_path().exists():
                    with open(_config_path()) as f:
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

                with open(_config_path(), "w") as f:
                    yaml.dump(raw, f, default_flow_style=False, sort_keys=False)

                st.success("Search config saved to settings.yaml")

    # ── Portal Config ──
    with tab_portals:
        st.subheader("Portal Configuration")

        config = load_config(_config_path())

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
                if _config_path().exists():
                    with open(_config_path()) as f:
                        raw = yaml.safe_load(f) or {}
                else:
                    raw = {}

                raw["portals"] = portal_settings

                with open(_config_path(), "w") as f:
                    yaml.dump(raw, f, default_flow_style=False, sort_keys=False)

                st.success("Portal config saved to settings.yaml")

    # ── Multi-User Setup (only in single-user mode) ──
    if not is_multi_user_enabled():
        st.divider()
        st.subheader("Enable Multi-User Mode")
        st.caption(
            "Share this dashboard with up to 10 people. Each user gets isolated "
            "data (CVs, jobs, settings). You'll be the admin."
        )

        with st.form("enable_multiuser"):
            col1, col2 = st.columns(2)
            with col1:
                admin_name = st.text_input("Your Name", key="mu_name")
                admin_email = st.text_input("Your Email", key="mu_email")
            with col2:
                admin_pass = st.text_input("Set Password", type="password", key="mu_pass")
                admin_pass2 = st.text_input("Confirm Password", type="password", key="mu_pass2")

            if st.form_submit_button("Enable Multi-User & Create Admin Account"):
                if not admin_name or not admin_email or not admin_pass:
                    st.error("All fields are required.")
                elif admin_pass != admin_pass2:
                    st.error("Passwords don't match.")
                elif len(admin_pass) < 4:
                    st.error("Password must be at least 4 characters.")
                else:
                    success, msg = add_user(admin_name, admin_email, admin_pass, is_admin=True)
                    if success:
                        st.success(
                            f"Multi-user mode enabled! {msg}\n\n"
                            "The page will now require login. Use your email and password."
                        )
                        st.balloons()
                        # Clear session so login page appears
                        st.session_state.clear()
                        time.sleep(2)
                        st.rerun()
                    else:
                        st.error(msg)


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
            "**Dry Run** and **Scrape Only** work here (uses HTTP + APIs, no browser needed). "
            "**Real Apply** requires Playwright — install locally with: "
            "`pip install playwright && playwright install chromium`\n\n"
            "Use the **Cloud Apply Assistant** page to apply manually with AI-generated cover letters."
        )

    # Check for missing CV
    _has_cvs = any(_cv_dir().glob("*.*")) if _cv_dir().exists() else False
    if not _has_cvs:
        st.warning(
            "**No CVs uploaded!** Job matching needs your CV to score jobs.\n\n"
            "Go to **Settings > CV Management** to upload your CV (PDF or DOCX). "
            "Without a CV, jobs will be discovered but **0 will be matched**."
        )

    # Check for Anthropic key
    _env = _load_env()
    if not _env.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_API_KEY"):
        st.warning(
            "**No Anthropic API key set!** AI matching and cover letter generation won't work.\n\n"
            "Go to **Settings > Credentials** to add your key."
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
                "Portals (direct scrape)",
                PORTAL_NAMES,
                default=["linkedin"],  # only LinkedIn works via HTTP; others need browser
                key="run_portals",
                disabled=is_running,
                help="JSearch API already covers Indeed, Glassdoor, ZipRecruiter. "
                     "LinkedIn is the only portal that works via direct scraping on cloud. "
                     "Others (Naukri, Indeed, Foundit, etc.) require a browser.",
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

        # Pass user-specific paths as CLI args for multi-user support
        paths = _get_paths()
        cmd.extend(["--env-path", str(paths["env_path"])])
        cmd.extend(["--config-path", str(paths["config_path"])])
        cmd.extend(["--db-path", str(paths["db_path"])])
        cmd.extend(["--cv-dir", str(paths["cv_dir"])])

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
    if not api_key and _env_path().exists():
        for line in _env_path().read_text().splitlines():
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


# ─── Page: User Management (admin only) ───


def render_user_management() -> None:
    """User Management — admin can add/remove users (max 10)."""
    st.header("User Management")

    if not st.session_state.get("user_is_admin"):
        st.error("Admin access required.")
        return

    users = load_users()

    # ── Current Users ──
    st.subheader(f"Current Users ({len(users)}/10)")

    if users:
        for i, user in enumerate(users):
            with st.container(border=True):
                col1, col2, col3, col4 = st.columns([2, 3, 1, 1])
                col1.write(f"**{user.get('name', '?')}**")
                col2.write(user.get("email", "?"))
                col3.write("Admin" if user.get("is_admin") else "User")
                # Don't allow deleting yourself
                if user.get("email") != st.session_state.get("user_email"):
                    if col4.button("Remove", key=f"rm_user_{i}"):
                        success, msg = remove_user(user["email"])
                        if success:
                            st.success(msg)
                            st.rerun()
                        else:
                            st.error(msg)
                else:
                    col4.write("(you)")
    else:
        st.info("No users configured.")

    # ── Add New User ──
    st.divider()
    st.subheader("Add New User")

    if len(users) >= 10:
        st.warning("Maximum 10 users reached. Remove a user to add a new one.")
        return

    with st.form("add_user_form"):
        col1, col2 = st.columns(2)
        with col1:
            new_name = st.text_input("Full Name", key="new_user_name")
            new_email = st.text_input("Email", key="new_user_email")
        with col2:
            new_password = st.text_input("Password", type="password", key="new_user_pass")
            new_is_admin = st.checkbox("Admin privileges", key="new_user_admin")

        if st.form_submit_button("Add User", type="primary"):
            if not new_name or not new_email or not new_password:
                st.error("All fields are required.")
            elif len(new_password) < 4:
                st.error("Password must be at least 4 characters.")
            else:
                success, msg = add_user(new_name, new_email, new_password, new_is_admin)
                if success:
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)

    # ── Setup Instructions ──
    st.divider()
    st.subheader("How It Works")
    st.markdown("""
Each user gets **completely isolated data**:
- Their own CV uploads
- Their own job database (discovered jobs, matches, applications)
- Their own search settings and keywords
- Their own API credentials

**To enable multi-user mode for the first time:**
1. Add yourself as the first admin user above
2. Share the dashboard URL with your friends
3. Each person logs in with their email/password
4. Each person uploads their own CV and sets their own keywords in Settings

**Data is NOT shared between users** — each person's job search is independent.
""")


# ─── Main ───


def main() -> None:
    multi_user = is_multi_user_enabled()

    # ── Multi-user: require login ──
    if multi_user:
        if not st.session_state.get("authenticated"):
            render_login()
            return
        # Setup user-specific paths/DB on each rerun
        _setup_user_session(st.session_state.user_id)
    else:
        # Single-user: original behavior (no login)
        st.set_page_config(page_title="Auto-Apply Dashboard", page_icon="📋", layout="wide")
        _setup_default_session()

    # ── Authenticated (or single-user) — render dashboard ──
    if multi_user:
        st.set_page_config(page_title="Auto-Apply Dashboard", page_icon="📋", layout="wide")

    # Sidebar
    st.sidebar.title("Auto-Apply CV Jobs")

    if multi_user:
        user_name = st.session_state.get("user_name", "User")
        st.sidebar.markdown(f"**Logged in as:** {user_name}")
        if st.sidebar.button("Logout"):
            for key in ["authenticated", "user_id", "user_name", "user_email", "user_is_admin"]:
                st.session_state.pop(key, None)
            from src.db import set_db_path
            set_db_path(None)
            st.rerun()
        st.sidebar.divider()

    nav_items = [
        "Run",
        "Jobs Feed",
        "Cloud Apply Assistant",
        "Profile Booster",
        "Applications",
        "Daily Stats",
        "LinkedIn Optimizer",
        "Settings",
    ]

    # Admin-only: User Management
    if multi_user and st.session_state.get("user_is_admin"):
        nav_items.append("User Management")

    page = st.sidebar.radio("Navigation", nav_items)

    if page == "Run":
        render_run_page()
    elif page == "Jobs Feed":
        render_jobs_feed()
    elif page == "Applications":
        render_applications()
    elif page == "Cloud Apply Assistant":
        render_manual_queue()
    elif page == "Profile Booster":
        render_profile_booster()
    elif page == "Daily Stats":
        render_daily_stats()
    elif page == "LinkedIn Optimizer":
        render_linkedin_optimizer()
    elif page == "Settings":
        render_settings()
    elif page == "User Management":
        render_user_management()


if __name__ == "__main__":
    main()
