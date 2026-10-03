"""SQLite persistence — the core tables plus registration staging (D20/D40).

`Store` owns the connection and is the only place SQL lives. Secrets never enter
a query in plaintext: the CF is written as `cf_enc` (AEAD) and looked up by its
`cf_hash` blind index; the NRE is written as `nre_enc` and only ever decrypted
in memory when a scrape needs it (D28/D29). All timestamps are unix-epoch floats;
the caller supplies `now` (default `time.time()`) so tests stay deterministic.
"""

import sqlite3
import time
from pathlib import Path
from typing import Self

from salutebot.crypto import Crypto
from salutebot.models import Prestazione, Slot

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


class Store:
    """Persistence over SQLite. Attributes private (encapsulation guardrail)."""

    def __init__(self, db_path: str, crypto: Crypto) -> None:
        self.__crypto = crypto
        self.__conn = sqlite3.connect(db_path)
        self.__conn.row_factory = sqlite3.Row
        # Per-connection: FK enforcement is off by default in SQLite, and must be
        # set outside a transaction (hence here, not in schema.sql).
        self.__conn.execute("PRAGMA foreign_keys = ON")
        self.__conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        self.__conn.commit()

    def close(self) -> None:
        self.__conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ----- users -----

    def add_user(self, cf: str, email: str) -> str:
        """Insert a new user; return its `cf_hash`. Raises on a duplicate CF —
        callers branch on `user_exists` first (D14 registration)."""
        cf_hash = self.__crypto.hash_cf(cf)
        self.__conn.execute(
            "INSERT INTO users (cf_hash, cf_enc, email) VALUES (?, ?, ?)",
            (cf_hash, self.__crypto.encrypt(cf), email),
        )
        self.__conn.commit()
        return cf_hash

    def user_exists(self, cf: str) -> bool:
        return self.__row("SELECT 1 FROM users WHERE cf_hash = ?", (self.__crypto.hash_cf(cf),)) is not None

    def all_user_emails(self) -> list[str]:
        """Every registered user's email — the dead-man broadcast set (D11)."""
        return [r["email"] for r in self.__rows("SELECT email FROM users", ())]

    def get_email(self, cf: str) -> str | None:
        row = self.__row("SELECT email FROM users WHERE cf_hash = ?", (self.__crypto.hash_cf(cf),))
        return row["email"] if row else None

    def delete_user(self, cf: str) -> None:
        """Delete the user and (via ON DELETE CASCADE) their targets. Shared `slots`
        rows are left intact — they belong to the prestazione, not the user (D20)."""
        self.__conn.execute("DELETE FROM users WHERE cf_hash = ?", (self.__crypto.hash_cf(cf),))
        self.__conn.commit()

    # ----- prestazioni + targets -----

    def add_target(self, cf: str, prestazione: Prestazione, nre: str) -> None:
        """Subscribe a user to a prestazione (D20). Upserts the prestazione row and
        the target; the NRE is stored encrypted. Re-adding reactivates the target."""
        self.__conn.execute(
            "INSERT INTO prestazioni (code, descrizione) VALUES (?, ?) "
            "ON CONFLICT(code) DO UPDATE SET descrizione = excluded.descrizione",
            (prestazione.code, prestazione.descrizione),
        )
        self.__conn.execute(
            "INSERT INTO targets (user, prestazione, nre_enc, active) VALUES (?, ?, ?, 1) "
            "ON CONFLICT(user, prestazione) DO UPDATE SET nre_enc = excluded.nre_enc, active = 1",
            (self.__crypto.hash_cf(cf), prestazione.code, self.__crypto.encrypt(nre)),
        )
        self.__conn.commit()

    def get_user_targets(self, cf: str) -> list[dict]:
        """The prestazioni a user watches: code, descrizione, active (0/1). No NRE."""
        rows = self.__rows(
            "SELECT p.code, p.descrizione, t.active "
            "FROM targets t JOIN prestazioni p ON p.code = t.prestazione "
            "WHERE t.user = ? ORDER BY p.code",
            (self.__crypto.hash_cf(cf),),
        )
        return [dict(r) for r in rows]

    def deactivate_target(self, cf: str, code: str) -> None:
        """Turn off notifications for one (user, prestazione) — `--disable` / D28 rotation."""
        self.__conn.execute(
            "UPDATE targets SET active = 0 WHERE user = ? AND prestazione = ?",
            (self.__crypto.hash_cf(cf), code),
        )
        self.__conn.commit()

    def deactivate_all_targets(self, cf: str) -> int:
        """Disable every one of a user's targets (`--disable-all`); return the count."""
        cur = self.__conn.execute(
            "UPDATE targets SET active = 0 WHERE user = ?", (self.__crypto.hash_cf(cf),)
        )
        self.__conn.commit()
        return cur.rowcount

    def list_user_slots(self, cf: str) -> list[dict]:
        """All slots for the prestazioni a user watches, for `--list` (D20).

        Joins the user's targets to the shared per-prestazione `slots`; ordered by
        code then date/time. No NRE is read. A watched prestazione with no slots
        yet simply contributes no rows."""
        rows = self.__rows(
            "SELECT p.code, p.descrizione, s.iso_date, s.time, s.struttura, "
            "s.cap, s.address, s.status, s.first_seen, s.last_seen "
            "FROM targets t "
            "JOIN slots s ON s.prestazione = t.prestazione "
            "JOIN prestazioni p ON p.code = t.prestazione "
            "WHERE t.user = ? "
            "ORDER BY p.code, s.iso_date, s.time",
            (self.__crypto.hash_cf(cf),),
        )
        return [dict(r) for r in rows]

    def representative_credential(self, code: str) -> tuple[str, str] | None:
        """The `(cf, nre)` credential that drives a scrape of this prestazione (D28):
        the **first active target** (by insertion order), with both the owner's CF
        and the target's NRE decrypted in memory (D29 — the scrape needs the literal
        CF+NRE). None when the prestazione has no active target (dormant, D28).

        Returns plaintext secrets: the caller uses them only to drive the scrape and
        must never log them (D3)."""
        row = self.__row(
            "SELECT u.cf_enc, t.nre_enc FROM targets t JOIN users u ON u.cf_hash = t.user "
            "WHERE t.prestazione = ? AND t.active = 1 ORDER BY t.rowid LIMIT 1",
            (code,),
        )
        if row is None:
            return None
        return self.__crypto.decrypt(row["cf_enc"]), self.__crypto.decrypt(row["nre_enc"])

    def prestazione_descrizione(self, code: str) -> str | None:
        """The human-readable name of a prestazione (for the D28 owner notice)."""
        row = self.__row("SELECT descrizione FROM prestazioni WHERE code = ?", (code,))
        return row["descrizione"] if row else None

    def non_dormant_prestazioni(self) -> list[dict]:
        """Prestazioni the loop should consider this sweep: those with >=1 active
        target (dormant ones — zero active NREs — are excluded, D28). Each row is
        `{code, last_scrape_at}`, ordered never-scraped-first then oldest-scraped, so
        the loop is fair and picks the most overdue first."""
        rows = self.__rows(
            "SELECT p.code, p.last_scrape_at FROM prestazioni p "
            "WHERE EXISTS (SELECT 1 FROM targets t "
            "             WHERE t.prestazione = p.code AND t.active = 1) "
            "ORDER BY p.last_scrape_at IS NOT NULL, p.last_scrape_at",
            (),
        )
        return [dict(r) for r in rows]

    def claim_prestazione(self, code: str, now: float, floor: float) -> bool:
        """Atomically claim this prestazione for a scrape, returning True iff won (D39).

        One indivisible `UPDATE … WHERE last_scrape_at is NULL or aged past `floor``
        both advances `last_scrape_at` (the D22 attempt mark) **and** reports, via
        `rowcount`, whether *this* caller won the 2-min-floor window. Because SQLite
        serializes writes, exactly one caller wins per prestazione per window — so a
        worker pool (N>1, D27) can never double-scrape the same code (the losers get
        `rowcount 0` and reuse the stored slots). Replaces the old read-then-mark,
        whose race-freeness relied on the serial loop being single-flight."""
        cur = self.__conn.execute(
            "UPDATE prestazioni SET last_scrape_at = ? "
            "WHERE code = ? AND (last_scrape_at IS NULL OR last_scrape_at <= ?)",
            (now, code, now - floor),
        )
        self.__conn.commit()
        return cur.rowcount == 1

    def subscriber_emails(self, code: str) -> list[str]:
        """Emails of the active watchers of a prestazione — the alert fan-out set (D20)."""
        rows = self.__rows(
            "SELECT u.email FROM targets t JOIN users u ON u.cf_hash = t.user "
            "WHERE t.prestazione = ? AND t.active = 1",
            (code,),
        )
        return [r["email"] for r in rows]

    # ----- slots (per-prestazione de-dup memory, D8/D20) -----

    def known_slot_keys(self, code: str) -> set[str]:
        rows = self.__rows("SELECT slot_key FROM slots WHERE prestazione = ?", (code,))
        return {r["slot_key"] for r in rows}

    def record_new_slots(self, code: str, slots: list[Slot], now: float | None = None) -> None:
        """Persist a batch of newly-alerted slots in one transaction (D8).

        Called by the alert fan-out **once at least one recipient was delivered**
        (D38, refining D36): a partially-delivered batch is recorded so the good
        recipients are never re-alerted; only a total-failure batch stays unwritten,
        so the next sweep re-detects the same keys and retries (D38). One commit keeps
        the batch atomic — a crash mid-batch leaves the un-inserted keys to re-alert
        next cycle (a bounded duplicate, accepted over a lost alert)."""
        ts = time.time() if now is None else now
        for slot in slots:
            self.__insert_slot(code, slot, ts)
        self.__conn.commit()

    def __insert_slot(self, code: str, slot: Slot, ts: float) -> None:
        """Insert one slot row — no commit; the caller owns the transaction boundary."""
        self.__conn.execute(
            "INSERT INTO slots (prestazione, slot_key, first_seen, last_seen, "
            "iso_date, time, struttura, cap, prestazione_desc, status, doctor_unit, address) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                code, slot.slot_key, ts, ts,
                slot.iso_date, slot.time, slot.struttura, slot.cap,
                slot.prestazione_desc, slot.status, slot.doctor_unit, slot.address,
            ),
        )

    def touch_slot(self, code: str, slot_key: str, now: float | None = None) -> None:
        """Bump `last_seen` for a still-present slot (never re-alerts, D8)."""
        ts = time.time() if now is None else now
        self.__conn.execute(
            "UPDATE slots SET last_seen = ? WHERE prestazione = ? AND slot_key = ?",
            (ts, code, slot_key),
        )
        self.__conn.commit()

    # ----- check-now (D24/D26/D39) -----

    def checknow_cooldown_remaining(self, cf: str, now: float, cooldown: float) -> float:
        """Seconds still to wait before this user may fire `--check-now` again (D23/D26).

        0.0 when free (never fired, or the last accepted fire is older than
        `cooldown`). Read-only — the CLI (which owns the cooldown, D26) calls this
        before deciding to accept; the anchor is the *last accepted* fire, so a
        rejected call never moves it (else the cooldown would never elapse)."""
        row = self.__row(
            "SELECT checknow_requested_at FROM users WHERE cf_hash = ?",
            (self.__crypto.hash_cf(cf),),
        )
        if row is None or row["checknow_requested_at"] is None:
            return 0.0
        return max(0.0, cooldown - (now - row["checknow_requested_at"]))

    def accept_checknow(self, cf: str, now: float) -> None:
        """Record an **accepted** check-now fire (D26): set `checknow_requested_at`.
        Only ever called on accept (never on a throttled reject)."""
        self.__conn.execute(
            "UPDATE users SET checknow_requested_at = ? WHERE cf_hash = ?",
            (now, self.__crypto.hash_cf(cf)),
        )
        self.__conn.commit()

    def checknow_served_since(self, cf: str, request_ts: float) -> bool:
        """True once the daemon has completed *this* check-now (D26): the block-poll
        condition the CLI waits on.

        Uses `last_checknow_at >= request_ts` (not strict `>`): a completion is always
        at-or-after the request it serves, and any *prior* completion is strictly
        earlier than `request_ts` (which was set to `now` at accept, after that prior
        completion) — so `>=` can only ever be satisfied by this request's own
        completion. This closes the exact-equal boundary where a same-timestamp
        completion would leave a strict-`>` poll hanging forever while the daemon (whose
        `outstanding` test is strict `>`) already considered the request served."""
        row = self.__row(
            "SELECT last_checknow_at FROM users WHERE cf_hash = ?",
            (self.__crypto.hash_cf(cf),),
        )
        if row is None or row["last_checknow_at"] is None:
            return False
        return row["last_checknow_at"] >= request_ts

    def outstanding_checknow(self) -> list[tuple[str, list[str]]]:
        """Users with a check-now awaiting service, each with their active codes (D26/D39).

        Outstanding = a fire newer than the last completion (`checknow_requested_at >
        last_checknow_at`, or never completed). Returns `(cf_hash, [active codes])`;
        the daemon works in hash space (it never needs the plaintext CF to serve —
        each code's scrape picks its own representative credential, D28). A user with
        no active target yields an empty code list (served as an immediate no-op)."""
        rows = self.__rows(
            "SELECT cf_hash FROM users "
            "WHERE checknow_requested_at IS NOT NULL "
            "AND (last_checknow_at IS NULL OR checknow_requested_at > last_checknow_at)",
            (),
        )
        out: list[tuple[str, list[str]]] = []
        for row in rows:
            cf_hash = row["cf_hash"]
            codes = self.__rows(
                "SELECT prestazione FROM targets WHERE user = ? AND active = 1 "
                "ORDER BY prestazione",
                (cf_hash,),
            )
            out.append((cf_hash, [c["prestazione"] for c in codes]))
        return out

    def mark_checknow_done(self, cf_hash: str, now: float) -> None:
        """Record a check-now batch complete (D26): set `last_checknow_at = now`.

        Set **regardless of scrape success/failure** so the blocking CLI always
        unblocks (failure is coarse — the CLI then prints whatever slots exist, D26).
        Takes the `cf_hash` from `outstanding_checknow` directly (daemon hash-space)."""
        self.__conn.execute(
            "UPDATE users SET last_checknow_at = ? WHERE cf_hash = ?", (now, cf_hash)
        )
        self.__conn.commit()

    # ----- registration staging (D14/D40) -----

    def submit_registration(self, cf: str, email: str, nre: str, now: float) -> None:
        """Stage an unresolved `(CF, NRE)` registration for the daemon (D40).

        Upserts on `cf_hash` (one pending op per user; a re-submit replaces and clears
        any prior result), storing CF + NRE encrypted (D3). The daemon resolves the
        prestazione code by scraping — the CLI never scrapes (D27)."""
        self.__conn.execute(
            "INSERT INTO pending_registrations "
            "(cf_hash, cf_enc, email, nre_enc, requested_at, resolved_at, "
            " result_status, result_code, result_desc) "
            "VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL) "
            "ON CONFLICT(cf_hash) DO UPDATE SET cf_enc = excluded.cf_enc, "
            "email = excluded.email, nre_enc = excluded.nre_enc, "
            "requested_at = excluded.requested_at, resolved_at = NULL, "
            "result_status = NULL, result_code = NULL, result_desc = NULL",
            (self.__crypto.hash_cf(cf), self.__crypto.encrypt(cf), email,
             self.__crypto.encrypt(nre), now),
        )
        self.__conn.commit()

    def registration_result(self, cf: str, request_ts: float) -> dict | None:
        """The daemon's ack-scrape outcome for this request, or None if not yet done.

        Resolved iff `resolved_at >= request_ts` (a completion at-or-after this submit;
        the `>=` boundary reasoning mirrors `checknow_served_since`). Returns
        `{status, code, desc}` — `status` is 'ok' | 'invalid' | 'error' (D40)."""
        row = self.__row(
            "SELECT resolved_at, result_status, result_code, result_desc "
            "FROM pending_registrations WHERE cf_hash = ?",
            (self.__crypto.hash_cf(cf),),
        )
        if row is None or row["resolved_at"] is None or row["resolved_at"] < request_ts:
            return None
        return {"status": row["result_status"], "code": row["result_code"],
                "desc": row["result_desc"]}

    def clear_registration(self, cf: str) -> None:
        """Drop the pending row once the CLI has consumed the result (confirm or reject)."""
        self.__conn.execute(
            "DELETE FROM pending_registrations WHERE cf_hash = ?", (self.__crypto.hash_cf(cf),)
        )
        self.__conn.commit()

    def outstanding_registrations(self) -> list[tuple[str, str, str]]:
        """Pending registrations awaiting the daemon's ack scrape (D40).

        Outstanding = `resolved_at IS NULL`. Returns `(cf_hash, cf, nre)` with the CF
        and NRE **decrypted** — the daemon needs the literal credential to scrape (D28/
        D29); the caller uses them only to drive the scrape and must never log them."""
        rows = self.__rows(
            "SELECT cf_hash, cf_enc, nre_enc FROM pending_registrations "
            "WHERE resolved_at IS NULL",
            (),
        )
        return [
            (r["cf_hash"], self.__crypto.decrypt(r["cf_enc"]), self.__crypto.decrypt(r["nre_enc"]))
            for r in rows
        ]

    def resolve_registration(self, cf_hash: str, now: float, status: str,
                             code: str | None = None, desc: str | None = None) -> None:
        """Record the ack-scrape outcome so the blocking CLI unblocks (D40). Takes the
        `cf_hash` from `outstanding_registrations` directly (daemon hash-space)."""
        self.__conn.execute(
            "UPDATE pending_registrations SET resolved_at = ?, result_status = ?, "
            "result_code = ?, result_desc = ? WHERE cf_hash = ?",
            (now, status, code, desc, cf_hash),
        )
        self.__conn.commit()

    def has_active_target(self, code: str) -> bool:
        """True if some user already actively watches this prestazione (D40 baseline gate)."""
        return self.__row(
            "SELECT 1 FROM targets WHERE prestazione = ? AND active = 1 LIMIT 1", (code,)
        ) is not None

    def upsert_prestazione(self, prestazione: Prestazione) -> None:
        """Ensure the prestazione row exists (D40) — the ack scrape discovers it before
        any target references it. Refreshes the descrizione on conflict."""
        self.__conn.execute(
            "INSERT INTO prestazioni (code, descrizione) VALUES (?, ?) "
            "ON CONFLICT(code) DO UPDATE SET descrizione = excluded.descrizione",
            (prestazione.code, prestazione.descrizione),
        )
        self.__conn.commit()

    def slots_for_code(self, code: str) -> list[dict]:
        """All slots for one prestazione, by code (D40 registration display).

        Unlike `list_user_slots` this needs no target join — registration shows the
        prestazione's slots *before* the user is subscribed (D20 consequence a)."""
        rows = self.__rows(
            "SELECT iso_date, time, struttura, cap, address, status, first_seen, last_seen "
            "FROM slots WHERE prestazione = ? ORDER BY iso_date, time",
            (code,),
        )
        return [dict(r) for r in rows]

    # ----- internal query helpers -----

    def __row(self, sql: str, params: tuple) -> sqlite3.Row | None:
        return self.__conn.execute(sql, params).fetchone()

    def __rows(self, sql: str, params: tuple) -> list[sqlite3.Row]:
        return self.__conn.execute(sql, params).fetchall()
