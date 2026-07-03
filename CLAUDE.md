# salute-bot — operating notes for Claude

Alert-first slot-watcher for the Piemonte SSN **CUP** no-login flow (Codice Fiscale + NRE). Watches the public booking flow headlessly and **notifies the moment a slot opens**. No auto-booking in the MVP. Multi-user from day one. **Deadline: 2026-07-04.** School project (ITS ICT, Python class).

> This file is a thin pointer + the essentials needed to work in-repo. The old PRD and decision log were **discarded** (drifted out of alignment); both have since been **rebuilt**: the decision log (`salute-bot-log.md` — the live decision record) and the PRD (`salute-bot-prd.md`, rebuilt 2026-07-02 from the log + the feasibility). The log holds the full history + rationale.

## Canonical docs (vault — not in this repo)

- Feasibility (IT, for submission): `/Users/matteo/Library/CloudStorage/OneDrive-Personal/Documenti/my_vault/ITS/python/salute-bot-project/salute-bot-feasibility_v2.md`
- PRD / build spec: `/Users/matteo/Library/CloudStorage/OneDrive-Personal/Documenti/my_vault/ITS/python/salute-bot-project/salute-bot-prd.md` — **rebuilt 2026-07-02** (D31) from the log's decisions + the feasibility. A synthesis, not a new source of truth — every requirement in it cites the `D#` that authorizes it. It is a **bureaucratic/submission artifact**, not a build reference: it is allowed to go stale between periodic realignment passes, and **must never be read as authority for code or architecture** — only `salute-bot-log.md` is.
- Recon + decision log (live history): `/Users/matteo/Library/CloudStorage/OneDrive-Personal/Documenti/my_vault/ITS/python/salute-bot-project/salute-bot-log.md` — **rebuilt 2026-06-15**: §1 recon · §2 decisions · §3 open questions. The single source of truth for specs, architecture, and rationale — CLAUDE.md and the PRD both just point here.

## How to work here (hard guardrails)

- **Build mode (active 2026-07-01, deadline crunch).** Work autonomously in module-sized batches, commit at each boundary, tests green before moving on.
- **keep the responses brief** 1 sentence min, 6 sentences max
- **Python at the core is a hard requirement** (it's a Python class).
- **Encapsulation: attributes are private by default.** Every attribute must be private (name-mangled `__attr`) except where it _explicitly_ needs to be protected (`_attr`) or public — and any such exception is justified case by case, never granted by category. _This is a **professor requirement** for the Python project, not a stylistic preference._
- **Internal functions get a leading underscore.** Only a module's public API (what callers are meant to import) stays unprefixed; every internal helper is named `_helper`. The function-level counterpart of the private-by-default attribute rule.
- **No `from __future__ import annotations`.** We target Python 3.14, which defers annotation evaluation by default (PEP 649), so the import is redundant — don't add it.
- **Secrets (CF/NRE) never appear in chat, code, or logs.** They're persisted **encrypted at rest in SQLite**; the encryption key comes from **env only** (never committed, never stored in the DB).
- **Don't hard-wrap prose mid-sentence in docs.** No line breaks (`\n`, `<br>`, etc.) within a sentence — break only where structurally needed (after a period, a list item, a new paragraph). Let lines run long; the editor soft-wraps.
- **Record locked decisions in the log.** Every time a **locked** decision is added or changed, append it to `salute-bot-log.md` §2 under a `### YYYY-MM-DD` heading reflecting the **current date** (a new heading per day; `D#` numbering runs **continuously** across all dates — the next decision after `D13` is `D14`) — never rewrite past entries.
- **Before starting any task, phase, or module — and when citing a `D#` — grep/read the relevant decisions in the log itself.** Never rely on memory of what a decision says; re-derive it from `salute-bot-log.md` every time it's load-bearing for the work at hand. The log is the one place drift isn't allowed to happen twice. If a decision leaves something deferred/residual, also check whether a **later** `D#` has since resolved it before treating it as still open.
- **Keep `TODO.md` in sync at commit time.** Whenever a commit completes or advances a tracked task, check it off (`[x]`) / update its status in `TODO.md` **in that same commit** — the tracker must never lag the code.
- **Versioning = GitHub flow, one branch per phase.** Do each phase's code on a branch named for it (`phase-N`, matching `TODO.md`); commit at every completed/advanced task (tests green + `TODO.md` synced in that same commit). When the phase is done, merge into `main` with `git merge --no-ff` (keep the phase boundary visible in history). `main` stays releasable. Project-meta changes (CLAUDE.md, guardrails, docs) go straight to `main`, not a phase branch. Merges, pushes, and branch deletion happen on Matteo's say-so — don't push or delete branches unprompted.

---

### History — design-phase guardrails (INACTIVE)

> Superseded by "Build mode" on 2026-07-01. **Not in force.** To switch back to hands-on/learning mode, move these four back up as active bullets in the list above and remove the "Build mode" line.

- ~~**Matteo leads the build.**~~ Explain _every_ structural choice "for-dummies" before/while making it — the learning is the point, not just a working app.
- ~~**No long autonomous coding.**~~ Write code only when Matteo explicitly authorizes it, piece by piece, so he stays hands-on.
- ~~**Never nudge toward coding.**~~ Don't end turns with "shall we build/implement X now?" or similar — Matteo decides when to start writing code and will say so explicitly. Until then, keep the work on design and decisions.
- ~~**Never suggest next steps.**~~ Matteo will lead the decisions as he finds suggestions to derail him towards confusion and potentially unwanted outcomes.

## Glossary (stable terms)

The shared vocabulary; these meanings are mostly immutable. Mechanics/values live in the log decisions referenced.

- **slot** — one individual available appointment the CUP offers for a prestazione (its `Cosa / Quando / Dove` card). Identified by a **natural key** from its business fields (date, time, struttura, CAP) — see D16.
- **prestazione** — a bookable medical service/exam. It is the **de-dup unit**: watched and scraped once, shared across all users who want it (D19/D20).
- **prestazione code** — the national/regional code identifying that service type (e.g. `8901.20`); the **grouping key** by which prestazioni are de-duplicated (D19).
- **NRE** — _Numero Ricetta Elettronica_: the code of one patient's specific dematerialized prescription. With the CF it is the **credential that unlocks** a prestazione's slot search; it is a secret (encrypted at rest, D3), carries a prestazione code, and has a lifecycle (expires; consumed on booking).
- **target** — a user's **subscription to watch one prestazione**: the (user, prestazione) link carrying _that user's_ NRE (the `targets` table, D20). Not the prestazione itself.
- **scrape** — one full execution of the CUP flow for **one prestazione** (driven by a representative NRE), producing that prestazione's current slot set.
- **sweep** — one **pass over all distinct watched prestazioni** (the whole queue scraped once).
- **worker** — one member of the **bounded concurrency pool**: an isolated headless-Chromium context that performs **one scrape at a time**; workers are identical and interchangeable, with at most N running at once.
