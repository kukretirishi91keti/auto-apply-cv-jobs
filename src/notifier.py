"""Email and Slack notification support for daily summaries."""

from __future__ import annotations

import json
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx

from src.config import AppConfig, Credentials

logger = logging.getLogger(__name__)


def send_email_notification(
    subject: str,
    body: str,
    config: AppConfig,
    creds: Credentials,
) -> bool:
    """Send an email notification."""
    if not config.notifications.email.enabled:
        return False

    if not creds.smtp_user or not creds.notification_email:
        logger.warning("Email notification credentials not configured")
        return False

    try:
        msg = MIMEMultipart()
        msg["From"] = creds.smtp_user
        msg["To"] = creds.notification_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(creds.smtp_host, creds.smtp_port) as server:
            server.starttls()
            server.login(creds.smtp_user, creds.smtp_password)
            server.send_message(msg)

        logger.info("Email notification sent: %s", subject)
        return True
    except Exception as e:
        logger.error("Failed to send email: %s", e)
        return False


def send_slack_notification(
    message: str,
    config: AppConfig,
    creds: Credentials,
) -> bool:
    """Send a Slack webhook notification."""
    if not config.notifications.slack.enabled:
        return False

    if not creds.slack_webhook_url:
        logger.warning("Slack webhook URL not configured")
        return False

    try:
        response = httpx.post(
            creds.slack_webhook_url,
            json={"text": message},
            timeout=10,
        )
        response.raise_for_status()
        logger.info("Slack notification sent")
        return True
    except Exception as e:
        logger.error("Failed to send Slack notification: %s", e)
        return False


def send_daily_summary(
    portal_results: dict[str, dict[str, int]],
    config: AppConfig,
    creds: Credentials,
) -> None:
    """Send daily summary via all configured channels."""
    total_discovered = sum(r.get("discovered", 0) for r in portal_results.values())
    total_matched = sum(r.get("matched", 0) for r in portal_results.values())
    total_applied = sum(r.get("applied", 0) for r in portal_results.values())
    total_failed = sum(r.get("failed", 0) for r in portal_results.values())

    lines = [
        "=== Auto-Apply Daily Summary ===",
        f"Total: {total_discovered} discovered | {total_matched} matched | {total_applied} applied | {total_failed} failed",
        "",
    ]
    for portal, stats in portal_results.items():
        lines.append(
            f"  {portal}: {stats.get('discovered', 0)} found, "
            f"{stats.get('matched', 0)} matched, "
            f"{stats.get('applied', 0)} applied"
        )

    summary = "\n".join(lines)
    logger.info("\n%s", summary)

    send_email_notification(
        subject=f"Auto-Apply: {total_applied} applications sent",
        body=summary,
        config=config,
        creds=creds,
    )

    send_slack_notification(summary, config=config, creds=creds)
