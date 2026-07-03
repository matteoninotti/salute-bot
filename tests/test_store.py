"""Tests for the SQLite store (D20/D26/D29).

Uses an in-memory DB and a real Crypto with generated keys. Verifies both the
persistence behavior and the security invariant: secrets are never stored in
plaintext, and the CF is looked up via its blind index.
"""

import sqlite3

import pytest
from cryptography.fernet import Fernet

from salutebot.crypto import Crypto
from salutebot.models import Prestazione, Slot

from salutebot.store import Store

_CF = "RSSMRA85T10A562S"
_CF2 = "BNCLNZ90A01F205X"
_NRE = "1234567890123456"


def _slot(iso_date="2026-06-22", time_="16:00", struttura="POLIAMBULATORIO MONGINEVRO", cap="10141"):
    return Slot(
        iso_date=iso_date, time=time_, struttura=struttura, cap=cap,
        prestazione_code="8901.20", prestazione_desc="VISITA UROLOGICA DI CONTROLLO",
        status="PRENOTABILE", doctor_unit="UROLOGIA", address="Via X 1 - TORINO (TO)",
    )


_PREST = Prestazione(code="8901.20", descrizione="VISITA UROLOGICA DI CONTROLLO", quantita=1)


@pytest.fixture
def crypto():
    return Crypto(Fernet.generate_key().decode("ascii"), "hmac-secret")


@pytest.fixture
def store(crypto):
    with Store(":memory:", crypto) as s:
        yield s


# ----- users -----

def test_add_and_find_user_by_cf(store):
    store.add_user(_CF, "a@b.it")
    assert store.user_exists(_CF) is True
    assert store.user_exists(_CF2) is False
    assert store.get_email(_CF) == "a@b.it"


def test_cf_and_email_lookup_never_needs_plaintext_cf(store, crypto):
    # The stored cf_enc must NOT equal the plaintext CF, and must decrypt back to it.
    store.add_user(_CF, "a@b.it")
    raw = store._Store__conn.execute("SELECT cf_hash, cf_enc FROM users").fetchone()
    assert raw["cf_enc"] != _CF
    assert crypto.decrypt(raw["cf_enc"]) == _CF
    assert raw["cf_hash"] == crypto.hash_cf(_CF)


def test_duplicate_user_raises(store):
    store.add_user(_CF, "a@b.it")
    with pytest.raises(sqlite3.IntegrityError):
        store.add_user(_CF, "other@b.it")


def test_delete_user_cascades_targets_but_keeps_slots(store):
    store.add_user(_CF, "a@b.it")
    store.add_target(_CF, _PREST, _NRE)
    store.insert_slot(_PREST.code, _slot(), now=1000.0)
    store.delete_user(_CF)
    assert store.user_exists(_CF) is False
    assert store.get_user_targets(_CF) == []
    # Shared slots belong to the prestazione, not the user — they survive (D20).
    assert store.known_slot_keys(_PREST.code) == {_slot().slot_key}


# ----- targets -----

def test_add_target_stores_nre_encrypted(store, crypto):
    store.add_user(_CF, "a@b.it")
    store.add_target(_CF, _PREST, _NRE)
    raw = store._Store__conn.execute("SELECT nre_enc FROM targets").fetchone()
    assert raw["nre_enc"] != _NRE
    assert crypto.decrypt(raw["nre_enc"]) == _NRE


def test_get_user_targets(store):
    store.add_user(_CF, "a@b.it")
    store.add_target(_CF, _PREST, _NRE)
    targets = store.get_user_targets(_CF)
    assert targets == [{"code": "8901.20", "descrizione": "VISITA UROLOGICA DI CONTROLLO", "active": 1}]


def test_add_target_without_user_violates_fk(store):
    # FK enforcement is on: a target needs an existing user row.
    with pytest.raises(sqlite3.IntegrityError):
        store.add_target(_CF, _PREST, _NRE)


def test_representative_nre_is_first_active_decrypted(store):
    store.add_user(_CF, "a@b.it")
    store.add_target(_CF, _PREST, _NRE)
    assert store.representative_nre(_PREST.code) == _NRE


def test_representative_nre_none_when_all_inactive(store):
    store.add_user(_CF, "a@b.it")
    store.add_target(_CF, _PREST, _NRE)
    store.deactivate_target(_CF, _PREST.code)
    assert store.representative_nre(_PREST.code) is None
    assert store.get_user_targets(_CF)[0]["active"] == 0


def test_subscriber_emails_are_active_watchers(store):
    store.add_user(_CF, "a@b.it")
    store.add_user(_CF2, "c@d.it")
    store.add_target(_CF, _PREST, _NRE)
    store.add_target(_CF2, _PREST, "9999888877776666")
    assert set(store.subscriber_emails(_PREST.code)) == {"a@b.it", "c@d.it"}
    store.deactivate_target(_CF2, _PREST.code)
    assert store.subscriber_emails(_PREST.code) == ["a@b.it"]


# ----- slots -----

def test_known_slot_keys_and_insert(store):
    store.add_user(_CF, "a@b.it")
    store.add_target(_CF, _PREST, _NRE)
    assert store.known_slot_keys(_PREST.code) == set()
    s = _slot()
    store.insert_slot(_PREST.code, s, now=1000.0)
    assert store.known_slot_keys(_PREST.code) == {s.slot_key}


def test_record_new_slots_inserts_batch_with_first_seen(store):
    store.add_user(_CF, "a@b.it")
    store.add_target(_CF, _PREST, _NRE)
    a = _slot("2026-06-22", "16:00")
    b = _slot("2026-06-24", "14:35")
    store.record_new_slots(_PREST.code, [a, b], now=1000.0)
    assert store.known_slot_keys(_PREST.code) == {a.slot_key, b.slot_key}
    row = store._Store__conn.execute(
        "SELECT first_seen, last_seen FROM slots WHERE slot_key = ?", (a.slot_key,)
    ).fetchone()
    assert row["first_seen"] == row["last_seen"] == 1000.0


def test_touch_slot_updates_last_seen_only(store):
    store.add_user(_CF, "a@b.it")
    store.add_target(_CF, _PREST, _NRE)
    s = _slot()
    store.insert_slot(_PREST.code, s, now=1000.0)
    store.touch_slot(_PREST.code, s.slot_key, now=2000.0)
    row = store._Store__conn.execute(
        "SELECT first_seen, last_seen FROM slots WHERE slot_key = ?", (s.slot_key,)
    ).fetchone()
    assert row["first_seen"] == 1000.0  # permanent, unchanged
    assert row["last_seen"] == 2000.0
