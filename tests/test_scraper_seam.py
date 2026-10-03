"""Tests for the scraper seam contract (D5/D28).

The Protocol itself has no behavior; these lock the *contract* the daemon relies
on — the result carries prestazione + slots, and the two failure modes are
distinct, typed, and both non-fatal to catch.
"""

import pytest

from salutebot.models import Prestazione, Slot
from salutebot.scraper.base import NREInvalidError, ScrapeError, Scraper, ScrapeResult

_PREST = Prestazione(code="8901.20", descrizione="VISITA UROLOGICA DI CONTROLLO", quantita=1)


def _slot():
    return Slot(
        iso_date="2026-06-22", time="16:00", struttura="POLIAMBULATORIO MONGINEVRO",
        cap="10141", prestazione_code="8901.20", prestazione_desc="VISITA UROLOGICA",
        status="PRENOTABILE", doctor_unit="UROLOGIA", address="Via X 1, 10141",
    )


class _FakeScraper:
    """A minimal Scraper: returns a canned result, or raises a chosen error."""

    def __init__(self, result=None, raises=None):
        self.__result = result
        self.__raises = raises

    def scrape(self, cf: str, nre: str) -> ScrapeResult:
        if self.__raises is not None:
            raise self.__raises
        return self.__result


def test_scrape_result_carries_prestazione_and_slots():
    scraper: Scraper = _FakeScraper(result=ScrapeResult(prestazione=_PREST, slots=[_slot()]))
    out = scraper.scrape("RSSMRA85T10A562S", "1111111111111111")
    assert out.prestazione == _PREST
    assert len(out.slots) == 1


def test_no_slots_is_a_result_not_an_error():
    scraper: Scraper = _FakeScraper(result=ScrapeResult(prestazione=_PREST, slots=[]))
    assert scraper.scrape("cf", "nre").slots == []


def test_the_two_failure_modes_are_distinct_types():
    assert issubclass(NREInvalidError, RuntimeError)
    assert issubclass(ScrapeError, RuntimeError)
    assert not issubclass(NREInvalidError, ScrapeError)


def test_permanent_and_transient_errors_propagate():
    with pytest.raises(NREInvalidError):
        _FakeScraper(raises=NREInvalidError("expired")).scrape("cf", "nre")
    with pytest.raises(ScrapeError):
        _FakeScraper(raises=ScrapeError("timeout")).scrape("cf", "nre")
