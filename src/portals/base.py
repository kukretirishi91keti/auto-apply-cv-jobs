"""Abstract base class for all portal scrapers."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from src.config import AppConfig, Credentials

logger = logging.getLogger(__name__)


@dataclass
class JobListing:
    """Represents a discovered job."""
    portal: str
    external_id: str
    title: str
    company: str
    location: str = ""
    url: str = ""
    description: str = ""
    salary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class BasePortal(ABC):
    """Abstract portal scraper.

    Subclasses must implement:
    - login()
    - search_jobs()
    - apply_to_job()
    - health_check()
    """

    name: str = "base"
    auto_apply_supported: bool = True

    def __init__(self, config: AppConfig, creds: Credentials):
        self.config = config
        self.creds = creds
        self.logger = logging.getLogger(f"portal.{self.name}")

    @abstractmethod
    async def login(self) -> bool:
        """Log into the portal. Returns True if successful."""
        ...

    @abstractmethod
    async def search_jobs(self) -> list[JobListing]:
        """Search for jobs matching configured criteria. Returns list of JobListings."""
        ...

    @abstractmethod
    async def apply_to_job(self, job: JobListing, cv_path: str, cover_letter: str = "") -> bool:
        """Apply to a specific job. Returns True if successful."""
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """Validate key selectors to detect DOM changes. Returns True if healthy."""
        ...

    async def close(self) -> None:
        """Clean up resources."""
        pass

    def get_credential(self, key: str) -> str:
        """Get a credential by portal-specific key."""
        return getattr(self.creds, f"{self.name}_{key}", "")
