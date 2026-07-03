"""SQLite persistence — the 4-table store (D20) with secrets encrypted at rest.

`Store` owns the connection and is the only place SQL lives. Secrets never enter
a query in plaintext: the CF is written as `cf_enc` (AEAD) and looked up by its
`cf_hash` blind index; the NRE is written as `nre_enc` and only ever decrypted
in memory when a scrape needs it (D28/D29). All timestamps are unix-epoch floats;
the caller supplies `now` (default `time.time()`) so tests stay deterministic.
"""

import sqlite3
import time
from pathlib import Path

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

    def __enter__(self) -> "Store":
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

    def representative_nre(self, code: str) -> str | None:
        """The driver NRE for a scrape: first active subscriber's NRE, decrypted
        (D28). None when the prestazione has no active target (dormant)."""
        row = self.__row(
            "SELECT nre_enc FROM targets WHERE prestazione = ? AND active = 1 "
            "ORDER BY rowid LIMIT 1",
            (code,),
        )
        return self.__crypto.decrypt(row["nre_enc"]) if row else None

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

    def insert_slot(self, code: str, slot: Slot, now: float | None = None) -> None:
        """Record one newly-seen slot with `first_seen = last_seen = now` (D8)."""
        self.__insert_slot(code, slot, time.time() if now is None else now)
        self.__conn.commit()

    def record_new_slots(self, code: str, slots: list[Slot], now: float | None = None) -> None:
        """Persist a batch of newly-alerted slots in one transaction (D8).

        Called by the alert fan-out **only after a successful send** (at-least-once,
        D36): if the send fails these rows stay unwritten, so the next sweep
        re-detects the same keys as new and re-attempts the alert. One commit keeps
        the batch atomic — a crash mid-batch leaves the un-inserted keys to re-alert
        next cycle (a bounded duplicate, accepted over a lost alert, D36)."""
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

    # ----- internal query helpers -----

    def __row(self, sql: str, params: tuple) -> sqlite3.Row | None:
        return self.__conn.execute(sql, params).fetchone()

    def __rows(self, sql: str, params: tuple) -> list[sqlite3.Row]:
        return self.__conn.execute(sql, params).fetchall()
