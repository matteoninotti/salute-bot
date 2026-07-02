# salute-bot — project TODO

Shared build tracker (Matteo + Claude). Ordered by build strategy **D** (parse-half tested first → deterministic core → live drive last), tagged by MoSCoW. Decision IDs (`D#`) point to `salute-bot-log.md`. **Deadline: 2026-07-04.**

Legend: `[ ]` todo · `[~]` in progress · `[x]` done · **(M)** Must · **(S)** Should · **(C)** Could

---

## Phase 0 — scaffolding & docs

- [x] Align docs (log §3 resolved, feasibility updated, D29 addendum: separate HMAC/AEAD env keys)
- [x] Rebuild venv on Python 3.14.6, add `requirements.txt` + `pyproject.toml`
- [x] Guardrails: build-mode, encapsulation (professor req), no `__future__` annotations
- [x] **(M)** Config/secrets bootstrap — read `SALUTEBOT_ENC_KEY` + `SALUTEBOT_HMAC_KEY` from env; fail loudly if missing
- [x] **(M)** CF/NRE validation helpers (format checks; never log values) — NRE format is loose/unverified (no captured form page); tighten once confirmed

## Phase 1 — parse half (trustworthy, offline)

- [x] `Slot` model + D16 natural key (`iso_date, time, struttura, cap` → sha256 `slot_key`)
- [x] Slots parser (JSF CDATA extract, structural card parse) + 8 ground-truth tests vs real recon capture
- [x] **(M)** Parse the `epPrestazioni` confirmation page (prestazione code + descrizione) for registration acknowledgment — fixture: `recon/epPrestazioni_redacted.xhtml`

## Phase 2 — deterministic core (offline-testable)

- [ ] **(M)** Store: SQLite schema — 4 tables `users`/`targets`/`prestazioni`/`slots` (D20) + `users.checknow_requested_at`/`last_checknow_at` (D26)
- [ ] **(M)** Crypto layer (D29): `cf_hash = HMAC-SHA256(cf, hmac_key)` blind index (PK/FK); `cf_enc`/`nre` AEAD; two separate env keys
- [ ] **(M)** Detector: per-prestazione dedup (D8/D20) — `new = current − known` in memory, `first_seen` written once, `last_seen` bumped
- [ ] **(M)** Alert fan-out: `slots(new) → targets → users` join; SES email adapter (D10/D15)
- [ ] **(M)** CLI client: registration (new/returning user, D14), `-u [CF]`, `--list`, `--disable [NRE]`, `--disable-all`, `--delete-user`
- [ ] **(M)** `--check-now`: CLI-owned cooldown + block-poll (D24/D26); daemon serving via two timestamps

## Phase 3 — daemon / service

- [ ] **(M)** Self-clocking serial loop, N=1 worker pool (D21/D22/D27); 2-min per-prestazione floor
- [ ] **(M)** `flock` single-instance guard (D27); systemd-friendly long-running process
- [ ] **(M)** Robustness: retry + backoff on JSF errors, N=3 consecutive fails → notify user; dead-man alert (D11)
- [ ] **(M)** Representative-NRE lifecycle (D28): first active target drives; rotate on permanent NRE-invalid; email owner; prestazione dormant if none valid

## Phase 4 — live drive (riskiest, needs valid NRE)

- [ ] **(M)** Scraper drive: Playwright headless Chromium through the stateful JSF flow (GET seed → form → Prosegui ×2 → Avanti → extend area → harvest); live token capture (ViewState/p_auth/ice.\*)
- [ ] **(M)** Live smoke run (Matteo, local, secrets via env/stdin) — confirm flow still matches June-23 recon
- [ ] Resolve residuals: NRE input box count (§3), exact "NRE invalid" wire signal (D28)
- [ ] **(C)** HAR-replay harness for offline drive testing (`recon/flow.har`)

## Phase 5 — demo / ship

- [ ] **(M)** Walking skeleton: fake scraper adapter → real detector/store/SES → demoable end-to-end (deadline insurance)
- [ ] **(M)** Demo/test fixture strategy — scrubbed saved markup, deterministic exam run
- [ ] **(M)** Run on Ubuntu amd64 (platform constraint, D12)
- [ ] **(M)** CI: GitHub ubuntu-amd64 runner + LocalStack; one real-AWS SES smoke to a verified address (D15)
- [ ] **(S)** systemd service unit (`Restart=always`, D21)
- [ ] **(S)** EC2 t3.small (amd64) 24/7 deploy (D13)
- [x] **(S)** Rebuild the PRD doc from log D1–D29 + feasibility (only doc not yet rebuilt)

## Backlog — Should / Could / Won't-this-MVP

- [ ] **(S)** SMS via Twilio plain number; email 6-digit verification; Web-UI-ready Client seam
- [ ] **(C)** Vertical worker-pool scaling (raise N) — gated on global rate cap; Telegram channel; change notification email/phone
- [ ] **Won't (MVP):** auto-booking · real Web UI · custom SMS sender-ID · slot field filters
