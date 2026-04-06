"""Tests for job matching."""

from src.job_matcher import keyword_score


def test_keyword_score_all_match():
    score = keyword_score(
        "Senior Python Developer",
        "We need a python developer with backend experience",
        ["python", "developer", "backend"],
    )
    assert score == 1.0


def test_keyword_score_partial_match():
    score = keyword_score(
        "Frontend Developer",
        "React and JavaScript role",
        ["python", "developer", "backend", "react"],
    )
    assert 0.0 < score < 1.0


def test_keyword_score_no_match():
    score = keyword_score(
        "Marketing Manager",
        "Lead our marketing team",
        ["python", "developer", "backend"],
    )
    assert score == 0.0


def test_keyword_score_empty_keywords():
    score = keyword_score("Any Job", "Any description", [])
    assert score == 1.0


def test_keyword_score_case_insensitive():
    score = keyword_score(
        "PYTHON DEVELOPER",
        "BACKEND SERVICES",
        ["python", "developer", "backend"],
    )
    assert score == 1.0
