"""Tests for the per-prestazione detector (D8, D19/D20, D32).

Uses a real in-memory Store (not a mock of it) so the persistence side effects
-- first_seen written once, last_seen bumped, reappearance not re-alerted --
are verified against actual SQLite behavior, not an assumption about it.
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
    assert store.known_slot_keys(_CODE) == {s.slot_key for s in slots}


def test_second_identical_scrape_has_no_new_slots(store):
    slots = [_slot("2026-06-22", "16:00")]
    detect_new_slots(store, _CODE, slots, now=1000.0)
    result = detect_new_slots(store, _CODE, slots, now=2000.0)
    assert result.all_slots == slots  # full current set, per D32 -- not the diff
    assert result.new_slots == []
    assert result.has_new is False


def test_second_scrape_bumps_last_seen_not_first_seen(store):
    slot = _slot("2026-06-22", "16:00")
    detect_new_slots(store, _CODE, [slot], now=1000.0)
    detect_new_slots(store, _CODE, [slot], now=2000.0)
    row = store._Store__conn.execute(
        "SELECT first_seen, last_seen FROM slots WHERE slot_key = ?", (slot.slot_key,)
    ).fetchone()
    assert row["first_seen"] == 1000.0  # permanent, written once (D8)
    assert row["last_seen"] == 2000.0


def test_only_the_newly_appeared_slot_is_in_new_slots(store):
    old = _slot("2026-06-22", "16:00")
    new = _slot("2026-06-25", "08:00")
    detect_new_slots(store, _CODE, [old], now=1000.0)
    result = detect_new_slots(store, _CODE, [old, new], now=2000.0)
    assert result.all_slots == [old, new]
    assert result.new_slots == [new]


def test_disappeared_slot_is_never_touched_and_stays_in_store(store):
    slot = _slot("2026-06-22", "16:00")
    detect_new_slots(store, _CODE, [slot], now=1000.0)
    detect_new_slots(store, _CODE, [], now=2000.0)  # slot vanished this cycle
    assert store.known_slot_keys(_CODE) == {slot.slot_key}  # row persists (D8)
    row = store._Store__conn.execute(
        "SELECT last_seen FROM slots WHERE slot_key = ?", (slot.slot_key,)
    ).fetchone()
    assert row["last_seen"] == 1000.0  # untouched, since it wasn't in current_slots


def test_reappeared_slot_is_not_re_alerted(store):
    slot = _slot("2026-06-22", "16:00")
    detect_new_slots(store, _CODE, [slot], now=1000.0)  # first appearance -> alert
    detect_new_slots(store, _CODE, [], now=2000.0)  # disappears
    result = detect_new_slots(store, _CODE, [slot], now=3000.0)  # reappears
    assert result.new_slots == []  # D8: reappearance never re-alerts
    assert result.all_slots == [slot]


def test_duplicate_card_in_same_scrape_is_inserted_once(store):
    slot = _slot("2026-06-22", "16:00")
    duplicate = _slot("2026-06-22", "16:00")  # same natural key, distinct object
    result = detect_new_slots(store, _CODE, [slot, duplicate], now=1000.0)
    assert result.new_slots == [slot]  # second occurrence treated as already-known
    assert store.known_slot_keys(_CODE) == {slot.slot_key}
