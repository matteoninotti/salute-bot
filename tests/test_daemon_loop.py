"""Tests for the daemon's self-clocking serial loop (D21/D22/D27).

Uses a real in-memory Store + fake Scraper/Mailer, so the scrape→detect→fan-out
wiring and the 2-min floor are verified against real persistence, deterministically
(explicit `now`, injected `clock`/`sleep`).
"""

import pytest
from cryptography.fernet import Fernet

from salutebot.crypto import Crypto
from salutebot.daemon import (
    FLOOR_SECONDS,
    process_prestazione,
    run,
    run_sweep,
    seconds_until_next_due,
)
from salutebot.models import Prestazione, Slot
from salutebot.scraper.base import NREInvalidError, ScrapeError, ScrapeResult
from salutebot.store import Store

_CF = "RSSMRA85T10A562S"
_CODE = "8901.20"
_PREST = Prestazione(code=_CODE, descrizione="VISITA UROLOGICA DI CONTROLLO", quantita=1)


def _slot(iso_date="2026-06-22", time_="16:00"):
    return Slot(
        iso_date=iso_date, time=time_, struttura="POLIAMBULATORIO MONGINEVRO", cap="10141",
        prestazione_code=_CODE, prestazione_desc="VISITA UROLOGICA DI CONTROLLO",
        status="PRENOTABILE", doctor_unit="UROLOGIA", address="Via Monginevro 130, 10141",
    )


class _FakeScraper:
    def __init__(self, result=None, raises=None):
        self.__result = result if result is not None else ScrapeResult(_PREST, [])
        self.__raises = raises
        self.calls: list[tuple[str, str]] = []

    def scrape(self, cf, nre):
        self.calls.append((cf, nre))
        if self.__raises is not None:
            raise self.__raises
        return self.__result


class _FakeMailer:
    def __init__(self):
        self.sent = []

    def send(self, to_addr, content):
        self.sent.append((to_addr, content))


@pytest.fixture
def store():
    crypto = Crypto(Fernet.generate_key().decode("ascii"), "hmac-secret")
    with Store(":memory:", crypto) as s:
        s.add_user(_CF, "a@b.it")
        s.add_target(_CF, _PREST, "1111111111111111")
        yield s


def _last_scrape_at(store, code):
    return store._Store__conn.execute(
        "SELECT last_scrape_at FROM prestazioni WHERE code = ?", (code,)
    ).fetchone()["last_scrape_at"]


# ----- process_prestazione -----

def test_scrape_drives_detection_and_fan_out(store):
    scraper = _FakeScraper(ScrapeResult(_PREST, [_slot()]))
    mailer = _FakeMailer()
    status = process_prestazione(store, scraper, mailer, _CODE, now=1000.0)
    assert status == "ok"
    assert scraper.calls == [(_CF, "1111111111111111")]  # decrypted credential (D28/D29)
    assert store.known_slot_keys(_CODE) == {_slot().slot_key}  # persisted post-send (D36)
    assert len(mailer.sent) == 1  # subscriber alerted (D20)
    assert _last_scrape_at(store, _CODE) == 1000.0


def test_no_new_slots_sends_nothing_but_still_marks_attempt(store):
    store.record_new_slots(_CODE, [_slot()], now=500.0)  # already known
    scraper = _FakeScraper(ScrapeResult(_PREST, [_slot()]))
    mailer = _FakeMailer()
    status = process_prestazione(store, scraper, mailer, _CODE, now=1000.0)
    assert status == "ok"
    assert mailer.sent == []
    assert _last_scrape_at(store, _CODE) == 1000.0


def test_dormant_prestazione_is_skipped_without_marking_attempt(store):
    store.deactivate_target(_CF, _CODE)  # no active credential -> dormant (D28)
    scraper = _FakeScraper()
    status = process_prestazione(store, scraper, _FakeMailer(), _CODE, now=1000.0)
    assert status == "dormant"
    assert scraper.calls == []
    assert _last_scrape_at(store, _CODE) is None


def test_attempt_is_marked_before_a_failing_scrape(store):
    # D22: the floor must throttle attempts, so last_scrape_at advances even on error.
    for raises, expected in [(ScrapeError("x"), "transient_error"),
                             (NREInvalidError("x"), "nre_invalid")]:
        store.set_last_scrape_at(_CODE, now=0.0)  # reset
        status = process_prestazione(store, _FakeScraper(raises=raises), _FakeMailer(),
                                     _CODE, now=2000.0)
        assert status == expected
        assert _last_scrape_at(store, _CODE) == 2000.0  # marked despite failure


# ----- run_sweep + the floor -----

def test_sweep_skips_a_prestazione_within_the_floor(store):
    store.set_last_scrape_at(_CODE, now=1000.0)
    scraper = _FakeScraper(ScrapeResult(_PREST, [_slot()]))
    run_sweep(store, scraper, _FakeMailer(), now=1000.0 + FLOOR_SECONDS - 1)  # too soon
    assert scraper.calls == []


def test_sweep_scrapes_once_the_floor_has_elapsed(store):
    store.set_last_scrape_at(_CODE, now=1000.0)
    scraper = _FakeScraper(ScrapeResult(_PREST, [_slot()]))
    run_sweep(store, scraper, _FakeMailer(), now=1000.0 + FLOOR_SECONDS)
    assert len(scraper.calls) == 1


# ----- seconds_until_next_due -----

def test_next_due_is_zero_for_a_never_scraped_prestazione(store):
    assert seconds_until_next_due(store, now=1000.0) == 0.0


def test_next_due_is_remaining_floor_after_a_scrape(store):
    store.set_last_scrape_at(_CODE, now=1000.0)
    assert seconds_until_next_due(store, now=1000.0 + 30) == FLOOR_SECONDS - 30


def test_next_due_is_none_when_nothing_is_watched(store):
    store.deactivate_target(_CF, _CODE)
    assert seconds_until_next_due(store, now=1000.0) is None


# ----- run (one iteration, broken out via sleep) -----

class _Stop(Exception):
    pass


def test_run_sweeps_then_sleeps_until_next_due(store, tmp_path):
    scraper = _FakeScraper(ScrapeResult(_PREST, [_slot()]))
    slept: list[float] = []

    def fake_sleep(seconds):
        slept.append(seconds)
        raise _Stop  # break out after the first sleep

    with pytest.raises(_Stop):
        run(store, scraper, _FakeMailer(), lock_path=str(tmp_path / "d.lock"),
            clock=lambda: 1000.0, sleep=fake_sleep)

    assert len(scraper.calls) == 1               # one sweep happened
    assert slept == [FLOOR_SECONDS]              # then slept exactly one floor (just scraped)
