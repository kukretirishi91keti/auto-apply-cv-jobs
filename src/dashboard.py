"""Streamlit web dashboard for Auto-Apply CV Jobs."""

from __future__ import annotations

import pandas as pd
import streamlit as st

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
STATUS_COLORS = {
    "applied": "🟢",
    "manually_applied": "🔵",
    "pending": "🟡",
    "scrape_only": "🟠",
    "failed": "🔴",
}


def rows_to_df(rows: list) -> pd.DataFrame:
    """Convert sqlite3.Row list to DataFrame."""
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([dict(row) for row in rows])


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

    # Show URLs for easy access
    if "url" in df.columns:
        with st.expander("Job URLs"):
            for _, row in df.iterrows():
                if row.get("url"):
                    st.markdown(f"- [{row['title']} @ {row['company']}]({row['url']})")


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

    # Status metrics
    if "status" in df.columns:
        cols = st.columns(5)
        for i, s in enumerate(["applied", "manually_applied", "pending", "scrape_only", "failed"]):
            count = len(df[df["status"] == s])
            cols[i].metric(f"{STATUS_COLORS.get(s, '')} {s}", count)

    display_cols = ["title", "company", "portal", "status", "selected_cv", "applied_at", "error_message"]
    available = [c for c in display_cols if c in df.columns]
    st.dataframe(df[available], use_container_width=True, hide_index=True)


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


def render_daily_stats() -> None:
    """Daily Stats page — run summaries and portal breakdown."""
    st.header("Daily Stats")

    days = st.selectbox("Period", [7, 14, 30], key="stats_days")
    stats = get_daily_stats(days=days)
    df = rows_to_df(stats)

    if df.empty:
        st.info("No run data yet. Run `auto-apply --dry-run` to generate stats.")
        return

    # Summary metrics
    cols = st.columns(4)
    cols[0].metric("Discovered", int(df["discovered"].sum()))
    cols[1].metric("Matched", int(df["matched"].sum()))
    cols[2].metric("Applied", int(df["applied"].sum()))
    cols[3].metric("Failed", int(df["failed"].sum()))

    # Daily chart
    st.subheader("Daily Activity")
    chart_df = df.set_index("run_date")[["discovered", "matched", "applied", "failed"]]
    st.bar_chart(chart_df)

    # Portal summary
    st.subheader("Portal Summary")
    summary = get_portal_summary()
    summary_df = rows_to_df(summary)
    if not summary_df.empty:
        st.dataframe(summary_df, use_container_width=True, hide_index=True)


def main() -> None:
    st.set_page_config(page_title="Auto-Apply Dashboard", page_icon="📋", layout="wide")
    init_db()

    st.sidebar.title("Auto-Apply CV Jobs")
    page = st.sidebar.radio("Navigation", [
        "Jobs Feed",
        "Applications",
        "Manual Apply Queue",
        "Daily Stats",
    ])

    if page == "Jobs Feed":
        render_jobs_feed()
    elif page == "Applications":
        render_applications()
    elif page == "Manual Apply Queue":
        render_manual_queue()
    elif page == "Daily Stats":
        render_daily_stats()


if __name__ == "__main__":
    main()
