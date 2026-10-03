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
_PREST2 = Prestazione(code="7001.10", descrizione="ECOGRAFIA", quantita=1)
_PREST3 = Prestazione(code="5001.30", descrizione="VISITA CARDIOLOGICA", quantita=1)
_FLOOR = 120.0


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
    store.record_new_slots(_PREST.code, [_slot()], now=1000.0)
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


def test_representative_credential_is_first_active_decrypted(store):
    store.add_user(_CF, "a@b.it")
    store.add_target(_CF, _PREST, _NRE)
    assert store.representative_credential(_PREST.code) == (_CF, _NRE)


def test_representative_credential_none_when_all_inactive(store):
    store.add_user(_CF, "a@b.it")
    store.add_target(_CF, _PREST, _NRE)
    store.deactivate_target(_CF, _PREST.code)
    assert store.representative_credential(_PREST.code) is None
    assert store.get_user_targets(_CF)[0]["active"] == 0


def test_non_dormant_prestazioni_excludes_zero_active_and_orders_overdue_first(store):
    store.add_user(_CF, "a@b.it")
    store.add_target(_CF, _PREST, _NRE)             # 8901.20 — never scraped
    store.add_target(_CF, _PREST2, "9999888877776666")  # 7001.10 — scraped recently
    store.claim_prestazione(_PREST2.code, now=5000.0, floor=_FLOOR)
    store.add_user(_CF2, "c@d.it")
    store.add_target(_CF2, _PREST3, "1231231231231231")
    store.deactivate_target(_CF2, _PREST3.code)     # dormant — excluded
    rows = store.non_dormant_prestazioni()
    codes = [r["code"] for r in rows]
    assert codes == [_PREST.code, _PREST2.code]     # never-scraped first, then oldest
    assert _PREST3.code not in codes


def test_claim_prestazione_advances_the_floor_marker(store):
    store.add_user(_CF, "a@b.it")
    store.add_target(_CF, _PREST, _NRE)
    assert store.claim_prestazione(_PREST.code, now=1234.0, floor=_FLOOR) is True
    row = store._Store__conn.execute(
        "SELECT last_scrape_at FROM prestazioni WHERE code = ?", (_PREST.code,)
    ).fetchone()
    assert row["last_scrape_at"] == 1234.0


def test_subscriber_emails_are_active_watchers(store):
    store.add_user(_CF, "a@b.it")
    store.add_user(_CF2, "c@d.it")
    store.add_target(_CF, _PREST, _NRE)
    store.add_target(_CF2, _PREST, "9999888877776666")
    assert set(store.subscriber_emails(_PREST.code)) == {"a@b.it", "c@d.it"}
    store.deactivate_target(_CF2, _PREST.code)
    assert store.subscriber_emails(_PREST.code) == ["a@b.it"]


# ----- slots -----

def test_known_slot_keys_and_record_new_slots(store):
    store.add_user(_CF, "a@b.it")
    store.add_target(_CF, _PREST, _NRE)
    assert store.known_slot_keys(_PREST.code) == set()
    s = _slot()
    store.record_new_slots(_PREST.code, [s], now=1000.0)
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
    store.record_new_slots(_PREST.code, [s], now=1000.0)
    store.touch_slot(_PREST.code, s.slot_key, now=2000.0)
    row = store._Store__conn.execute(
        "SELECT first_seen, last_seen FROM slots WHERE slot_key = ?", (s.slot_key,)
    ).fetchone()
    assert row["first_seen"] == 1000.0  # permanent, unchanged
    assert row["last_seen"] == 2000.0


# ----- atomic scrape claim (D39) -----


def test_claim_wins_when_never_scraped_then_loses_within_floor(store):
    store.add_user(_CF, "a@b.it")
    store.add_target(_CF, _PREST, _NRE)
    # First claim wins (last_scrape_at NULL) and advances the mark to `now`.
    assert store.claim_prestazione(_PREST.code, now=1000.0, floor=_FLOOR) is True
    # A second claimer within the floor loses (no double-scrape, N>1-safe).
    assert store.claim_prestazione(_PREST.code, now=1000.0 + _FLOOR - 1, floor=_FLOOR) is False
    # last_scrape_at was set by the winner and not moved by the loser.
    row = store._Store__conn.execute(
        "SELECT last_scrape_at FROM prestazioni WHERE code = ?", (_PREST.code,)
    ).fetchone()
    assert row["last_scrape_at"] == 1000.0


def test_claim_wins_again_once_the_floor_elapses(store):
    store.add_user(_CF, "a@b.it")
    store.add_target(_CF, _PREST, _NRE)
    assert store.claim_prestazione(_PREST.code, now=1000.0, floor=_FLOOR) is True
    assert store.claim_prestazione(_PREST.code, now=1000.0 + _FLOOR, floor=_FLOOR) is True


# ----- check-now (D24/D26) -----

def test_checknow_cooldown_and_accept(store):
    store.add_user(_CF, "a@b.it")
    # Never fired -> free.
    assert store.checknow_cooldown_remaining(_CF, now=1000.0, cooldown=300.0) == 0.0
    store.accept_checknow(_CF, now=1000.0)
    # A 2nd fire 100s later is still on cooldown, with 200s remaining.
    assert store.checknow_cooldown_remaining(_CF, now=1100.0, cooldown=300.0) == 200.0
    # After the cooldown elapses, free again.
    assert store.checknow_cooldown_remaining(_CF, now=1300.0, cooldown=300.0) == 0.0


def test_checknow_served_since_tracks_completion(store):
    store.add_user(_CF, "a@b.it")
    store.accept_checknow(_CF, now=1000.0)
    assert store.checknow_served_since(_CF, request_ts=1000.0) is False  # not served yet
    cf_hash = store._Store__crypto.hash_cf(_CF)
    store.mark_checknow_done(cf_hash, now=1005.0)
    assert store.checknow_served_since(_CF, request_ts=1000.0) is True   # completion is newer


def test_outstanding_checknow_lists_users_with_their_active_codes(store):
    store.add_user(_CF, "a@b.it")
    store.add_target(_CF, _PREST, _NRE)
    store.add_target(_CF, _PREST2, _NRE)
    store.deactivate_target(_CF, _PREST2.code)  # inactive -> excluded
    store.add_user(_CF2, "b@b.it")              # no outstanding fire -> excluded
    store.accept_checknow(_CF, now=1000.0)

    outstanding = store.outstanding_checknow()

    assert len(outstanding) == 1
    cf_hash, codes = outstanding[0]
    assert cf_hash == store._Store__crypto.hash_cf(_CF)
    assert codes == [_PREST.code]               # only the active target
    # Once served, it drops out of the outstanding set.
    store.mark_checknow_done(cf_hash, now=1005.0)
    assert store.outstanding_checknow() == []


# ----- registration staging (D14/D40) -----

def test_submit_registration_stages_encrypted_secrets(store):
    store.submit_registration(_CF, "a@b.it", _NRE, now=1000.0)
    row = store._Store__conn.execute(
        "SELECT cf_enc, nre_enc, resolved_at FROM pending_registrations").fetchone()
    assert row["cf_enc"] != _CF and row["nre_enc"] != _NRE   # encrypted at rest (D3)
    assert row["resolved_at"] is None                        # outstanding until the daemon resolves
    # The daemon reads back the decrypted credential to drive the scrape.
    outstanding = store.outstanding_registrations()
    assert outstanding == [(store._Store__crypto.hash_cf(_CF), _CF, _NRE)]


def test_registration_result_none_until_resolved_then_returned(store):
    store.submit_registration(_CF, "a@b.it", _NRE, now=1000.0)
    assert store.registration_result(_CF, request_ts=1000.0) is None
    cf_hash = store._Store__crypto.hash_cf(_CF)
    store.resolve_registration(cf_hash, now=1002.0, status="ok",
                               code=_PREST.code, desc=_PREST.descrizione)
    result = store.registration_result(_CF, request_ts=1000.0)
    assert result == {"status": "ok", "code": _PREST.code, "desc": _PREST.descrizione}
    assert store.outstanding_registrations() == []           # resolved -> no longer outstanding


def test_resubmit_replaces_and_clears_a_prior_result(store):
    store.submit_registration(_CF, "a@b.it", _NRE, now=1000.0)
    store.resolve_registration(store._Store__crypto.hash_cf(_CF), now=1001.0, status="invalid")
    # A fresh fire (e.g. a new ricetta) resets the row and re-opens it for the daemon.
    store.submit_registration(_CF, "a@b.it", "9999999999", now=2000.0)
    assert store.registration_result(_CF, request_ts=2000.0) is None
    assert len(store.outstanding_registrations()) == 1
    store.clear_registration(_CF)
    assert store.outstanding_registrations() == []


def test_has_active_target_and_upsert_prestazione(store):
    assert store.has_active_target(_PREST.code) is False
    store.upsert_prestazione(_PREST)  # exists but nobody watches it yet
    assert store.has_active_target(_PREST.code) is False
    store.add_user(_CF, "a@b.it")
    store.add_target(_CF, _PREST, _NRE)
    assert store.has_active_target(_PREST.code) is True


def test_slots_for_code_reads_without_a_target_join(store):
    store.upsert_prestazione(_PREST)
    store.record_new_slots(_PREST.code, [_slot("2026-06-22", "16:00")], now=1000.0)
    rows = store.slots_for_code(_PREST.code)         # no user/target needed (D20 consequence a)
    assert len(rows) == 1 and rows[0]["iso_date"] == "2026-06-22"
