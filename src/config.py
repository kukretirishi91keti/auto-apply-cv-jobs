"""Configuration loader using Pydantic settings with YAML support."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "settings.yaml"


class CVVersion(BaseModel):
    name: str
    file: str
    description: str


class SearchConfig(BaseModel):
    keywords: list[str] = []
    locations: list[str] = []
    experience_years: int = 0
    min_salary: int = 0
    excluded_companies: list[str] = []


class CVConfig(BaseModel):
    directory: str = "data/cvs"
    versions: list[CVVersion] = []


class MatchingConfig(BaseModel):
    keyword_min_score: float = 0.3
    ai_min_score: float = 0.7
    ai_model: str = "claude-sonnet-4-20250514"
    max_ai_scorings_per_day: int = 100
    api_budget_usd: float = 2.0


class ApplyConfig(BaseModel):
    max_applications_per_day: int = 30
    max_per_portal: int = 10
    generate_cover_letter: bool = True
    save_screenshots: bool = True


class PortalConfig(BaseModel):
    enabled: bool = True
    auto_apply: bool = True


class ScheduleConfig(BaseModel):
    enabled: bool = False
    cron_hour: int = 9
    cron_minute: int = 0
    timezone: str = "Asia/Kolkata"


class BrowserConfig(BaseModel):
    headless: bool = True
    slow_mo: int = 100
    timeout: int = 30000
    state_dir: str = "browser_state"


class EmailNotifConfig(BaseModel):
    enabled: bool = False


class SlackNotifConfig(BaseModel):
    enabled: bool = False


class NotificationsConfig(BaseModel):
    email: EmailNotifConfig = Field(default_factory=EmailNotifConfig)
    slack: SlackNotifConfig = Field(default_factory=SlackNotifConfig)


class AppConfig(BaseModel):
    search: SearchConfig = Field(default_factory=SearchConfig)
    cvs: CVConfig = Field(default_factory=CVConfig)
    matching: MatchingConfig = Field(default_factory=MatchingConfig)
    apply: ApplyConfig = Field(default_factory=ApplyConfig)
    portals: dict[str, PortalConfig] = Field(default_factory=dict)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)


class Credentials(BaseSettings):
    """Loaded from environment variables / .env file."""

    anthropic_api_key: str = ""

    naukri_email: str = ""
    naukri_password: str = ""

    indeed_email: str = ""
    indeed_password: str = ""

    foundit_email: str = ""
    foundit_password: str = ""

    ziprecruiter_email: str = ""
    ziprecruiter_password: str = ""

    linkedin_email: str = ""
    linkedin_password: str = ""

    glassdoor_email: str = ""
    glassdoor_password: str = ""

    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    notification_email: str = ""

    slack_webhook_url: str = ""

    # Job aggregator API keys
    rapidapi_key: str = ""          # JSearch on RapidAPI (free tier)
    adzuna_app_id: str = ""         # Adzuna API
    adzuna_app_key: str = ""        # Adzuna API

    model_config = {"env_file": str(PROJECT_ROOT / ".env"), "extra": "ignore"}


def load_config(path: Path | None = None) -> AppConfig:
    """Load settings from YAML file."""
    config_path = path or CONFIG_PATH
    if config_path.exists():
        with open(config_path) as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}
        return AppConfig(**data)
    return AppConfig()


def load_credentials(env_path: Path | None = None) -> Credentials:
    """Load credentials from environment / .env file."""
    if env_path and env_path.exists():
        # Load user-specific .env into os.environ temporarily
        from dotenv import load_dotenv
        load_dotenv(env_path, override=True)
    return Credentials()


# Singletons (used by CLI; dashboard overrides per-user)
_config: AppConfig | None = None
_creds: Credentials | None = None


def get_config(config_path: Path | None = None) -> AppConfig:
    global _config
    if config_path:
        return load_config(config_path)
    if _config is None:
        _config = load_config()
    return _config


def get_credentials(env_path: Path | None = None) -> Credentials:
    global _creds
    if env_path:
        return load_credentials(env_path)
    if _creds is None:
        _creds = load_credentials()
    return _creds
