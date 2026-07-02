-- salute-bot SQLite schema — 4 tables (D20, refined D26/D29).
--
-- Secrets at rest: `cf_enc` / `nre_enc` are AEAD (Fernet) ciphertext; `cf_hash`
-- is the HMAC-SHA256 blind index (D29) that serves as the users PK and the value
-- targets.user references, so a CF is findable without decrypting every row.
--
-- Timestamps are REAL unix-epoch seconds — arithmetic backs the 2-min per-
-- prestazione floor (D22) and the check-now cooldown (D26). NULL = never.
--
-- `PRAGMA foreign_keys = ON` is set per-connection in code (it is a no-op inside
-- the implicit transaction executescript runs), so it is not repeated here.

CREATE TABLE IF NOT EXISTS users (
    cf_hash                TEXT PRIMARY KEY,          -- HMAC-SHA256(cf) blind index (D29)
    cf_enc                 TEXT NOT NULL,             -- AEAD ciphertext of the CF (D3/D29)
    email                  TEXT NOT NULL,
    checknow_requested_at  REAL,                      -- last accepted --check-now fire (D26)
    last_checknow_at       REAL                       -- last completion, set by daemon (D26)
);

CREATE TABLE IF NOT EXISTS prestazioni (
    code            TEXT PRIMARY KEY,                 -- de-dup grouping key (D19)
    descrizione     TEXT NOT NULL,
    last_scrape_at  REAL                              -- backs the 2-min floor (D22)
);

CREATE TABLE IF NOT EXISTS targets (
    user         TEXT NOT NULL REFERENCES users(cf_hash) ON DELETE CASCADE,
    prestazione  TEXT NOT NULL REFERENCES prestazioni(code) ON DELETE CASCADE,
    nre_enc      TEXT NOT NULL,                       -- AEAD ciphertext of the NRE (D3)
    active       INTEGER NOT NULL DEFAULT 1,          -- 1/0; rotation deactivates (D28)
    PRIMARY KEY (user, prestazione)                   -- one target per (user, prestazione) (D33)
);

-- Fan-out join + representative-NRE selection both scan targets by prestazione.
CREATE INDEX IF NOT EXISTS idx_targets_prestazione ON targets(prestazione);

CREATE TABLE IF NOT EXISTS slots (
    prestazione       TEXT NOT NULL REFERENCES prestazioni(code) ON DELETE CASCADE,
    slot_key          TEXT NOT NULL,                  -- natural-key hash (D16)
    first_seen        REAL NOT NULL,                  -- permanent, written once (D8)
    last_seen         REAL NOT NULL,
    -- descriptive fields (NOT part of the key; for --list + the alert email, D16)
    iso_date          TEXT,
    time              TEXT,
    struttura         TEXT,
    cap               TEXT,
    prestazione_desc  TEXT,
    status            TEXT,
    doctor_unit       TEXT,
    address           TEXT,
    PRIMARY KEY (prestazione, slot_key)               -- slots shared per prestazione (D20)
);
