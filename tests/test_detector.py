"""Tests for the per-prestazione detector (D8, D19/D20, D32, D36).

Uses a real in-memory Store (not a mock of it) so the persistence side effects
-- last_seen bumped, reappearance not re-alerted -- are verified against actual
SQLite behavior, not an assumption about it.

Per D36 the detector **does not persist newly-seen slots** (the fan-out records
them after a successful send). So these tests seed "already-alerted" state with
`store.record_new_slots`, which is exactly what the post-send step does.
"""

import pytest
from cryptography.fernet import Fernet

from salutebot.crypto import Crypto
from salutebot.detector import detect_new_slots
from salutebot.models import Prestazione, Slot
from salutebot.store import Store

_CF = "RSSMRA85T10A562S"
_NRE = "1234567890123456"
_CODE = "8901.20"
_PREST = Prestazione(code=_CODE, descrizione="VISITA UROLOGICA DI CONTROLLO", quantita=1)


def _slot(iso_date, time_, struttura="POLIAMBULATORIO MONGINEVRO", cap="10141"):
    return Slot(
        iso_date=iso_date, time=time_, struttura=struttura, cap=cap,
        prestazione_code=_CODE, prestazione_desc="VISITA UROLOGICA DI CONTROLLO",
        status="PRENOTABILE", doctor_unit="UROLOGIA", address="Via X 1 - TORINO (TO)",
    )


@pytest.fixture
def store():
    crypto = Crypto(Fernet.generate_key().decode("ascii"), "hmac-secret")
    with Store(":memory:", crypto) as s:
        s.add_user(_CF, "a@b.it")
        s.add_target(_CF, _PREST, _NRE)
        yield s


def test_first_scrape_all_slots_are_new(store):
    slots = [_slot("2026-06-22", "16:00"), _slot("2026-06-24", "14:35")]
    result = detect_new_slots(store, _CODE, slots, now=1000.0)
    assert result.prestazione == _CODE
    assert result.all_slots == slots
    assert result.new_slots == slots
    assert result.has_new is True


def test_detect_does_not_persist_new_slots(store):
    # D36: detection is in-memory only; the fan-out records rows after a send.
    detect_new_slots(store, _CODE, [_slot("2026-06-22", "16:00")], now=1000.0)
    assert store.known_slot_keys(_CODE) == set()


def test_recorded_slots_are_not_new_next_scrape(store):
    slots = [_slot("2026-06-22", "16:00")]
    store.record_new_slots(_CODE, slots, now=1000.0)  # simulate a prior alerted cycle
    result = detect_new_slots(store, _CODE, slots, now=2000.0)
    assert result.all_slots == slots  # full current set, per D32 -- not the diff
    assert result.new_slots == []
    assert result.has_new is False


def test_detect_bumps_last_seen_not_first_seen(store):
    slot = _slot("2026-06-22", "16:00")
    store.record_new_slots(_CODE, [slot], now=1000.0)
    detect_new_slots(store, _CODE, [slot], now=2000.0)
    row = store._Store__conn.execute(
        "SELECT first_seen, last_seen FROM slots WHERE slot_key = ?", (slot.slot_key,)
    ).fetchone()
    assert row["first_seen"] == 1000.0  # permanent, written once (D8)
    assert row["last_seen"] == 2000.0


def test_only_the_newly_appeared_slot_is_in_new_slots(store):
    old = _slot("2026-06-22", "16:00")
    new = _slot("2026-06-25", "08:00")
    store.record_new_slots(_CODE, [old], now=1000.0)
    result = detect_new_slots(store, _CODE, [old, new], now=2000.0)
    assert result.all_slots == [old, new]
    assert result.new_slots == [new]


def test_disappeared_slot_is_never_touched_and_stays_in_store(store):
    slot = _slot("2026-06-22", "16:00")
    store.record_new_slots(_CODE, [slot], now=1000.0)
    detect_new_slots(store, _CODE, [], now=2000.0)  # slot vanished this cycle
    assert store.known_slot_keys(_CODE) == {slot.slot_key}  # row persists (D8)
    row = store._Store__conn.execute(
        "SELECT last_seen FROM slots WHERE slot_key = ?", (slot.slot_key,)
    ).fetchone()
    assert row["last_seen"] == 1000.0  # untouched, since it wasn't in current_slots


def test_reappeared_slot_is_not_re_alerted(store):
    slot = _slot("2026-06-22", "16:00")
    store.record_new_slots(_CODE, [slot], now=1000.0)  # first appearance already alerted
    detect_new_slots(store, _CODE, [], now=2000.0)  # disappears
    result = detect_new_slots(store, _CODE, [slot], now=3000.0)  # reappears
    assert result.new_slots == []  # D8: reappearance never re-alerts
    assert result.all_slots == [slot]


def test_duplicate_card_in_same_scrape_is_collapsed(store):
    slot = _slot("2026-06-22", "16:00")
    duplicate = _slot("2026-06-22", "16:00")  # same natural key, distinct object
    result = detect_new_slots(store, _CODE, [slot, duplicate], now=1000.0)
    assert result.new_slots == [slot]  # D16: one key -> one new slot
    assert result.all_slots == [slot]  # D32 current set is a set of keys, deduped
