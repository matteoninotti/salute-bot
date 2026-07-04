"""Tests for the offline demo scraper (Phase 5 walking skeleton).

`FixtureScraper` is the fake behind the D5 seam. These pin its two jobs: parsing
the real redacted recon captures into genuine ground-truth data, and scripting a
sweep-to-sweep diff (plus the D28 dead-NRE signal) so the demo is deterministic.
"""

import pytest

from salutebot.models import Prestazione, Slot
from salutebot.scraper.base import NREInvalidError, ScrapeResult
from salutebot.scraper.fixture import FixtureScraper


def _slot(iso_date: str) -> Slot:
    return Slot(iso_date=iso_date, time="09:00", struttura="X", cap="10100",
                prestazione_code="8901.20", prestazione_desc="VISITA", status="PRENOTABILE",
                doctor_unit=None, address=None)


def _prest() -> Prestazione:
    return Prestazione(code="8901.20", descrizione="VISITA", quantita=1)


def test_from_recon_parses_real_capture():
    scraper = FixtureScraper.from_recon()
    result = scraper.scrape("CF", "NRE")
    assert isinstance(result, ScrapeResult)
    assert result.prestazione.code == "8901.20"
    assert result.prestazione.descrizione == "VISITA UROLOGICA DI CONTROLLO"
    assert result.slots  # ground-truth slots came through the real parser


def test_from_recon_scripts_a_new_slot_between_frames():
    scraper = FixtureScraper.from_recon(baseline=4, added=1)
    first = scraper.scrape("CF", "NRE").slots   # frame 0: baseline
    second = scraper.scrape("CF", "NRE").slots  # frame 1: unchanged
    third = scraper.scrape("CF", "NRE").slots   # frame 2: baseline + 1
    assert len(first) == 4
    assert len(second) == 4
    assert len(third) == 5
    # the extra slot is genuinely new (its key is absent from the baseline)
    base_keys = {s.slot_key for s in first}
    assert len([s for s in third if s.slot_key not in base_keys]) == 1


def test_frame_index_sticks_on_last():
    scraper = FixtureScraper(_prest(), [[_slot("2026-01-01")], [_slot("2026-02-02")]])
    scraper.scrape("CF", "NRE")            # frame 0
    last = scraper.scrape("CF", "NRE")     # frame 1
    again = scraper.scrape("CF", "NRE")    # past the end → sticks on frame 1
    assert [s.iso_date for s in again.slots] == [s.iso_date for s in last.slots]


def test_dead_nre_raises_and_does_not_consume_a_frame():
    scraper = FixtureScraper(_prest(), [[_slot("2026-01-01")], [_slot("2026-02-02")]],
                             dead_nres=["DEAD"])
    with pytest.raises(NREInvalidError):
        scraper.scrape("CF", "DEAD")
    # a live NRE still sees frame 0 — the dead attempt didn't advance the script
    assert scraper.scrape("CF", "LIVE").slots[0].iso_date == "2026-01-01"


def test_empty_frames_rejected():
    with pytest.raises(ValueError):
        FixtureScraper(_prest(), [])
