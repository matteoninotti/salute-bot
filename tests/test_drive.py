"""Offline tests for the live Playwright drive (Phase 4).

The flow itself can only be verified by the live smoke run (`python -m
salutebot.scraper.drive`, needs a real ricetta + network) — these cover the parts
that need no browser: the selector helper, `from_env`, and the error contract (any
failure surfaces as a transient `ScrapeError`, D11). The form-page selectors are
verified separately against the live DOM, not here.
"""

import pytest

from salutebot.scraper import drive
from salutebot.scraper.base import ScrapeError
from salutebot.scraper.drive import LiveScraper

_CF = "RSSMRA85T10A562S"
_NRE = "1234567890123456"


def test_sel_builds_an_attribute_selector():
    # JSF ids contain colons, so `#id` is unusable — an [id="..."] selector is required.
    assert drive._sel("a:b:c") == '[id="a:b:c"]'


def test_from_env_builds_an_instance_headless_or_headful():
    assert isinstance(LiveScraper.from_env({}), LiveScraper)                       # default headless
    assert isinstance(LiveScraper.from_env({"SALUTEBOT_HEADFUL": "1"}), LiveScraper)


def test_invalid_ricetta_regex_matches_the_live_banner():
    # The D28 signal, captured live from a dead ricetta (a page-level banner).
    assert drive._INVALID_RICETTA_RE.search("Impossibile recuperare la ricetta dematerializzata")
    assert drive._INVALID_RICETTA_RE.search("impossibile recuperare la ricetta")  # case-insensitive
    assert not drive._INVALID_RICETTA_RE.search("VISITA UROLOGICA DI CONTROLLO - 8901.20")


def test_scrape_wraps_unexpected_failures_as_transient(monkeypatch):
    # No browser here: force the Playwright entry to fail and assert it becomes a
    # transient ScrapeError (so the daemon retries, never crashes on the fragile flow).
    def _boom(*_a, **_k):
        raise RuntimeError("no browser available")

    monkeypatch.setattr(drive, "sync_playwright", _boom)
    with pytest.raises(ScrapeError):
        LiveScraper().scrape(_CF, _NRE)
