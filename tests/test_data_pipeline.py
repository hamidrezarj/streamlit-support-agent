"""Offline unit tests for the data pipeline (no network). Run: PYTHONPATH=src python -m pytest -q"""
from ssa.data.clean_issues import (DUP_OF, clean_text, detect_context, error_signatures,
                                   extract_env, split_template, _regression)
from ssa.data.crawl_docs import slugify, split_sections
from ssa.data.crawl_issues import select

BUG = """### Summary
App stuck on 'Please wait...' behind nginx.

### Debug info

- Streamlit version: 1.45.1
- Python version: 3.12
- Operating System:
- Browser: N/A

### Is this a regression?

- [x] Yes, this used to work in a previous version.
"""


def test_env_missing_stays_none():
    env = extract_env(BUG)
    assert env["streamlit_version_parsed"] == "1.45.1"
    assert env["os"] is None and env["browser"] is None   # never guessed


def test_regression_checkbox():
    assert _regression(split_template(BUG)) is True


def test_clean_text_scrubs_pii_and_images():
    t = clean_text("hi @alice see ![x](http://a/b.png) mail bob@x.com in /Users/bob/app", 1000)
    assert "@alice" not in t and "bob@x.com" not in t and "/Users/bob" not in t and "[image]" in t


def test_duplicate_regex():
    assert DUP_OF.findall("Duplicate of #1234") == ["1234"]
    assert DUP_OF.findall("duplicate of https://github.com/streamlit/streamlit/issues/99") == ["99"]


def test_context_ignores_urls():
    assert "https_ssl" not in detect_context("see https://github.com/streamlit/streamlit/issues/1")
    assert "nginx" in detect_context("I use an nginx reverse proxy")


def test_error_signature_normalised():
    sigs = error_signatures("Traceback...\nValueError: port 8501 is in use")
    assert sigs == ["ValueError: port N is in use"]


def test_select_drops_prs_and_old():
    cands = {"1": {"sources": ["label:a"], "is_pr": True, "created_at": "2024-01-01", "comments": 0},
             "2": {"sources": ["kw:x"], "is_pr": False, "created_at": "2020-01-01", "comments": 0},
             "3": {"sources": ["kw:x"], "is_pr": False, "created_at": "2024-01-01", "comments": 5},
             "4": {"sources": ["label:a"], "is_pr": False, "created_at": "2024-01-01", "comments": 0}}
    chosen, stats = select(cands, "2021-01-01", max_issues=1)
    assert chosen == [4] and stats["dropped_pull_requests"] == 1 and stats["dropped_before_since"] == 1


def test_sections_ignore_headings_in_code():
    secs = split_sections("## A\ntext text text text text text text text\n```\n# not a heading\n```\n")
    assert [s[0] for s in secs] == [["A"]]
    assert slugify("Version 1.52.0") == "version-1520"
