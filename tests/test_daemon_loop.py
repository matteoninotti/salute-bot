"""Tests for the daemon's self-clocking serial loop (D21/D22/D27).

Uses a real in-memory Store + fake Scraper/Mailer, so the scrape→detect→fan-out
wiring and the 2-min floor are verified against real persistence, deterministically
(explicit `now`, injected `clock`/`sleep`).
"""

import pytest
from cryptography.fernet import Fernet

from salutebot.crypto import Crypto
from salutebot.daemon import (
    FAILURE_NOTIFY_THRESHOLD,
    FLOOR_SECONDS,
    RETRY_ATTEMPTS,
    process_prestazione,
    run,
    run_sweep,
    seconds_until_next_due,
)
from salutebot.models import Prestazione, Slot
from salutebot.scraper.base import NREInvalidError, ScrapeError, ScrapeResult
from salutebot.store import Store

_CF = "RSSMRA85T10A562S"
_NRE_A = "1111111111111111"
_CF_B = "BNCLGU80A01L219T"
_NRE_B = "2222222222222222"
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


class _RotatingScraper:
    """Raises NREInvalidError for NREs in `dead`, returns `result` otherwise —
    lets tests drive D28 rotation across subscribers."""

    def __init__(self, dead, result=None):
        self.__dead = set(dead)
        self.__result = result if result is not None else ScrapeResult(_PREST, [_slot()])
        self.calls: list[tuple[str, str]] = []

    def scrape(self, cf, nre):
        self.calls.append((cf, nre))
        if nre in self.__dead:
            raise NREInvalidError("expired")
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
        s.add_target(_CF, _PREST, _NRE_A)
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
    assert scraper.calls == [(_CF, _NRE_A)]  # decrypted credential (D28/D29)
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


def test_transient_error_retries_with_backoff_then_marks_and_stops(store):
    # D11 in-attempt retry; D22 floor still advances despite the failure.
    slept: list[float] = []
    scraper = _FakeScraper(raises=ScrapeError("x"))
    status = process_prestazione(store, scraper, _FakeMailer(), _CODE, now=2000.0,
                                 sleep=slept.append)
    assert status == "transient_error"
    assert len(scraper.calls) == RETRY_ATTEMPTS          # retried, not given up at once
    assert len(slept) == RETRY_ATTEMPTS - 1              # backoff between attempts
    assert slept == sorted(slept) and slept[0] < slept[-1]  # exponential (increasing)
    assert store.get_user_targets(_CF)[0]["active"] == 1    # transient != deactivation
    assert _last_scrape_at(store, _CODE) == 2000.0


def test_retry_recovers_if_a_later_attempt_succeeds(store):
    # A scraper that fails once then succeeds -> no transient_error surfaces.
    class _FlakyScraper:
        def __init__(self):
            self.calls = 0

        def scrape(self, cf, nre):
            self.calls += 1
            if self.calls == 1:
                raise ScrapeError("blip")
            return ScrapeResult(_PREST, [_slot()])

    scraper = _FlakyScraper()
    status = process_prestazione(store, scraper, _FakeMailer(), _CODE, now=1000.0,
                                 sleep=lambda _s: None)
    assert status == "ok"
    assert scraper.calls == 2


# ----- N=3 consecutive-failure notice (D11) -----

def _fail_sweeps(store, scraper, mailer, counts, times):
    for now in times:
        run_sweep(store, scraper, mailer, now, failure_counts=counts, sleep=lambda _s: None)


def test_three_consecutive_failures_notify_subscribers_once(store):
    counts: dict[str, int] = {}
    scraper = _FakeScraper(raises=ScrapeError("x"))
    mailer = _FakeMailer()
    # Three due sweeps spaced by the floor (~6 min), each fails after retries.
    _fail_sweeps(store, scraper, mailer, counts,
                 [1000.0, 1000.0 + FLOOR_SECONDS, 1000.0 + 2 * FLOOR_SECONDS])
    assert counts[_CODE] == FAILURE_NOTIFY_THRESHOLD
    assert len(mailer.sent) == 1                      # notified once, at the threshold
    addr, notice = mailer.sent[0]
    assert addr == "a@b.it" and "problemi temporanei" in notice.subject


def test_failures_below_threshold_do_not_notify(store):
    counts: dict[str, int] = {}
    mailer = _FakeMailer()
    _fail_sweeps(store, _FakeScraper(raises=ScrapeError("x")), mailer, counts,
                 [1000.0, 1000.0 + FLOOR_SECONDS])       # only 2 failures
    assert counts[_CODE] == 2
    assert mailer.sent == []


def test_a_success_resets_the_failure_streak(store):
    counts = {_CODE: 2}
    run_sweep(store, _FakeScraper(ScrapeResult(_PREST, [_slot()])), _FakeMailer(),
              now=1000.0, failure_counts=counts, sleep=lambda _s: None)
    assert _CODE not in counts


# ----- representative-NRE rotation (D28) -----

def test_dead_nre_rotates_to_next_subscriber(store):
    store.add_user(_CF_B, "b@b.it")
    store.add_target(_CF_B, _PREST, _NRE_B)  # second subscriber, valid NRE
    scraper = _RotatingScraper(dead={_NRE_A})  # A's ricetta is dead
    mailer = _FakeMailer()

    status = process_prestazione(store, scraper, mailer, _CODE, now=1000.0)

    assert status == "ok"
    assert scraper.calls == [(_CF, _NRE_A), (_CF_B, _NRE_B)]  # tried A, rotated to B
    assert store.get_user_targets(_CF)[0]["active"] == 0   # A deactivated (D28)
    assert store.get_user_targets(_CF_B)[0]["active"] == 1  # B still active
    # A got the ricetta-invalid notice; B got the slot alert (active subscriber only).
    by_addr = {addr: content for addr, content in mailer.sent}
    assert "non è più valida" in by_addr["a@b.it"].subject
    assert "nuovo posto" in by_addr["b@b.it"].subject
    assert store.known_slot_keys(_CODE) == {_slot().slot_key}


def test_all_dead_nres_make_prestazione_dormant(store):
    store.add_user(_CF_B, "b@b.it")
    store.add_target(_CF_B, _PREST, _NRE_B)
    scraper = _RotatingScraper(dead={_NRE_A, _NRE_B})  # every subscriber's NRE dead
    mailer = _FakeMailer()

    status = process_prestazione(store, scraper, mailer, _CODE, now=1000.0)

    assert status == "dormant"
    assert store.get_user_targets(_CF)[0]["active"] == 0
    assert store.get_user_targets(_CF_B)[0]["active"] == 0
    assert {addr for addr, _ in mailer.sent} == {"a@b.it", "b@b.it"}  # both owners told
    assert store.known_slot_keys(_CODE) == set()  # never succeeded, nothing recorded
    assert _last_scrape_at(store, _CODE) == 1000.0  # attempt still marked (D22)


def test_single_dead_nre_goes_dormant_and_deactivates(store):
    status = process_prestazione(store, _RotatingScraper(dead={_NRE_A}),
                                 _FakeMailer(), _CODE, now=2000.0)
    assert status == "dormant"
    assert store.get_user_targets(_CF)[0]["active"] == 0


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
