"""Tests for configuration loading."""

from pathlib import Path

from src.config import load_config, AppConfig, CVVersion


def test_load_config_from_yaml():
    config = load_config()
    assert isinstance(config, AppConfig)
    assert len(config.search.keywords) > 0
    assert config.matching.ai_min_score > 0


def test_load_config_defaults():
    config = load_config(Path("/nonexistent/path.yaml"))
    assert isinstance(config, AppConfig)
    assert config.apply.max_applications_per_day == 30
    assert config.browser.headless is True


def test_cv_versions():
    config = load_config()
    assert len(config.cvs.versions) == 3
    for v in config.cvs.versions:
        assert isinstance(v, CVVersion)
        assert v.name
        assert v.file


def test_portal_config():
    config = load_config()
    assert "naukri" in config.portals
    assert config.portals["naukri"].enabled is True
    assert config.portals["linkedin"].auto_apply is False
