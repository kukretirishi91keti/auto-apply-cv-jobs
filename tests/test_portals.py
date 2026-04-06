"""Tests for portal infrastructure."""

from src.portals import ALL_PORTALS, AUTO_APPLY_PORTALS, SCRAPE_ONLY_PORTALS
from src.portals.base import BasePortal, JobListing


def test_all_portals_registered():
    expected = {"naukri", "indeed", "foundit", "ziprecruiter", "linkedin", "glassdoor"}
    assert set(ALL_PORTALS.keys()) == expected


def test_auto_apply_vs_scrape_only():
    assert AUTO_APPLY_PORTALS == {"naukri", "indeed", "foundit", "ziprecruiter"}
    assert SCRAPE_ONLY_PORTALS == {"linkedin", "glassdoor"}
    assert AUTO_APPLY_PORTALS & SCRAPE_ONLY_PORTALS == set()


def test_job_listing_creation():
    job = JobListing(
        portal="naukri",
        external_id="123",
        title="Software Engineer",
        company="TestCo",
        location="Bangalore",
        url="https://example.com/job/123",
    )
    assert job.portal == "naukri"
    assert job.title == "Software Engineer"
    assert job.metadata == {}


def test_portal_classes_are_base_subclasses():
    for name, cls in ALL_PORTALS.items():
        assert issubclass(cls, BasePortal), f"{name} must subclass BasePortal"


def test_scrape_only_portals_flag():
    from src.config import load_config, Credentials
    config = load_config()
    creds = Credentials()
    for name in SCRAPE_ONLY_PORTALS:
        portal = ALL_PORTALS[name](config, creds)
        assert portal.auto_apply_supported is False, f"{name} should have auto_apply_supported=False"
