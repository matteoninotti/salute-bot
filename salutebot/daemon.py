"""The watcher daemon — the long-running Service (D21) that scrapes and alerts.

Single-instance guard (D27): the daemon takes an **exclusive, non-blocking
`flock`** on a lockfile at startup and refuses to start if the lock is already
held. This is a *kernel-owned* lock tied to the open file description, so it
**auto-releases on any exit — crash included** — with no stale-PID-file problem.
It composes with systemd (`Restart=always`, one instance, D21): even a stray
manual launch alongside the managed service cannot spawn a second competing
scraper, which is what keeps single-flight structural (D27).

The loop itself is self-clocking (D21/D22), serves check-now requests first
(D39), resolves pending registrations (D40), then sweeps due prestazioni.
"""

import fcntl
import os
import time
from contextlib import contextmanager

from salutebot.alerts import (
    Mailer,
    MailerError,
    fan_out,
    render_dead_man_notice,
    render_nre_invalid_notice,
    render_watch_failing_notice,
)
from salutebot.detector import detect_new_slots
from salutebot.scraper.base import NREInvalidError, ScrapeError, Scraper, ScrapeResult
from salutebot.store import Store

_DEFAULT_LOCK_PATH = "/tmp/salute-bot.lock"
_DEFAULT_HEARTBEAT_PATH = "/tmp/salute-bot.heartbeat"
_HEARTBEAT_PATH_VAR = "SALUTEBOT_HEARTBEAT"


def resolve_heartbeat_path(env: os._Environ | dict[str, str] | None = None) -> str:
    """The heartbeat file location, `SALUTEBOT_HEARTBEAT`-overridable (D11).

    Shared by the daemon (which writes it) and the CLI (which reads it to tell
    whether the watcher is alive before block-polling, D24) — they MUST agree on
    the path, so both resolve it here rather than hard-coding the default twice."""
    src = os.environ if env is None else env
    return src.get(_HEARTBEAT_PATH_VAR, _DEFAULT_HEARTBEAT_PATH)

# Dead-man (D11). The daemon rewrites the heartbeat at the top of every loop pass
# AND after each prestazione it processes (per-prestazione, not once per sweep), so
# the max gap between beats is one scrape — a few seconds at most — not a whole
# sweep, whose duration is unbounded (many prestazioni × live scrape + retry
# backoff). An EXTERNAL checker (systemd/cron, Phase 5) reads the heartbeat and, if
# older than this, broadcasts that the watcher is down. 300s dwarfs both the beat
# gap (one scrape) and the capped idle poll (CHECKNOW_POLL_INTERVAL), so a healthy
# daemon — busy or idle — is never mistaken for dead.
HEARTBEAT_MAX_AGE = 300.0

# The politeness floor (D22): a single prestazione is scraped at most once per this
# many seconds, by the loop or by --check-now. Also the idle re-check interval when
# nothing is being watched.
FLOOR_SECONDS = 120.0

# The idle sleep is capped at this (D39/Finding 3): the loop wakes at least this often
# to serve the check-now lane, so a block-polling CLI (D24) is answered within ~this
# many seconds instead of waiting up to a whole floor. Cheap — an idle tick is a couple
# of SQLite reads, no scrape.
CHECKNOW_POLL_INTERVAL = 2.0

# N=1 (D27): one prestazione scraped at a time. Kept as a named constant because
# D27 wants raising N later to be a one-line change once a global rate cap exists —
# it is NOT an invitation to add workers now (extra workers only raise concurrent
# load on the CUP server, §3).
WORKER_POOL_SIZE = 1

# Robustness (D11). In-attempt retry with exponential backoff rides out momentary
# JSF glitches (ViewState/p_auth races) inside one scrape; across sweeps, a
# prestazione that fails this many CONSECUTIVE cycles (~6 min at the 2-min floor)
# gets its subscribers a "watching is currently broken" notice.
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_BASE = 1.0
FAILURE_NOTIFY_THRESHOLD = 3


class DaemonAlreadyRunningError(RuntimeError):
    """Raised when the single-instance `flock` is already held by another daemon."""


@contextmanager
def single_instance_lock(lock_path: str = _DEFAULT_LOCK_PATH):
    """Hold an exclusive `flock` for the duration of the `with` block (D27).

    Raises `DaemonAlreadyRunningError` immediately (non-blocking) if another holder
    exists. The lock is released when the block exits — the fd is closed in
    `finally`, and the kernel also drops it on process death, so no cleanup of the
    lockfile itself is needed (its mere existence is not the lock; the `flock` is).
    """
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as err:
        os.close(fd)
        raise DaemonAlreadyRunningError(
            f"another salute-bot daemon already holds {lock_path} — refusing to "
            "start a second scraper (D27)."
        ) from err
    try:
        yield
    finally:
        os.close(fd)  # releases the flock (kernel would too, on any exit)


# ----- the self-clocking serial loop (D21/D22/D27) -----


def process_prestazione(
    store: Store, scraper: Scraper, mailer: Mailer, code: str, now: float,
    *, sleep=time.sleep,
) -> str:
    """Scrape one prestazione and process its result (N=1, D27). Returns a status.

    Drives from the **first active target** (D28), rotating on a permanent
    `NREInvalidError`: the dead target is deactivated, its owner emailed, and the
    **next** active subscriber's NRE is tried — until one works, a transient error
    stops the attempt, or none remain and the prestazione is **dormant** (D28). Each
    scrape rides out momentary JSF glitches via in-attempt retry + backoff (D11).

    Politeness (D22) + concurrency (D39): the first scrape is gated by an **atomic
    claim** (`claim_prestazione`) that advances `last_scrape_at` once and confirms no
    other worker holds the 2-min floor window — so a failure/crash still counts
    against the floor (no busy-scraping a broken prestazione) and, at N>1, two workers
    can never scrape the same code. A lost claim returns `"skipped_floor"` (scraped
    recently — stored slots stand); a prestazione with no active credential is
    `"dormant"` and marks nothing. On success, detection (D8) + fan-out (D32/D36/D38)
    run. The `transient_error` status feeds the across-sweep consecutive-failure
    counter in `run_sweep` (N=3 → notify, D11); `"skipped_floor"` is neutral to it.
    """
    marked = False
    while True:
        credential = store.representative_credential(code)
        if credential is None:
            return "dormant"  # no active NRE left (or ever) — skip until one is added
        if not marked:
            # Atomically claim the 2-min floor (D22) window: winning both advances
            # `last_scrape_at` and guarantees no other worker scrapes this code in
            # the same window (N>1-safe coalescing, D39). A lost claim = someone
            # already scraped it recently, so the stored slots stand.
            if not store.claim_prestazione(code, now, FLOOR_SECONDS):
                return "skipped_floor"
            marked = True
        cf, nre = credential
        try:
            result = _scrape_with_retry(scraper, cf, nre, sleep)
        except NREInvalidError:
            # Permanent: this ricetta is dead. Deactivate it, tell its owner, and
            # rotate to the next active subscriber (D28). The loop terminates because
            # each pass deactivates one target, so the active set strictly shrinks.
            store.deactivate_target(cf, code)
            _notify_nre_invalid(store, mailer, cf, code)
            continue
        except ScrapeError:
            return "transient_error"  # retries exhausted this cycle (D11)
        detection = detect_new_slots(store, code, result.slots, now)
        if detection.has_new:
            fan_out(store, mailer, detection, now, sleep=sleep)  # per-recipient retry (D38)
        return "ok"


def _scrape_with_retry(scraper: Scraper, cf: str, nre: str, sleep) -> ScrapeResult:
    """One scrape with in-attempt retry + exponential backoff on transient errors (D11).

    A permanent `NREInvalidError` is NOT retried — it propagates immediately so the
    caller can rotate (D28). A `ScrapeError` is retried up to `RETRY_ATTEMPTS` times
    with backoff, then re-raised for the caller to count as a failed cycle."""
    delay = RETRY_BACKOFF_BASE
    for attempt in range(RETRY_ATTEMPTS):
        try:
            return scraper.scrape(cf, nre)
        except ScrapeError:
            if attempt == RETRY_ATTEMPTS - 1:
                raise
            sleep(delay)
            delay *= 2
    raise AssertionError("unreachable: RETRY_ATTEMPTS must be >= 1")


def _notify_nre_invalid(store: Store, mailer: Mailer, cf: str, code: str) -> None:
    """Email the owner of a just-deactivated target that their ricetta is dead (D28).

    Best-effort: a failed notice must not stop rotation (keeping the scrape alive for
    the other subscribers is the priority); broader failure signalling is D11."""
    email = store.get_email(cf)
    if email is None:
        return
    notice = render_nre_invalid_notice(code, store.prestazione_descrizione(code))
    try:
        mailer.send(email, notice)
    except MailerError:
        pass


def run_sweep(
    store: Store, scraper: Scraper, mailer: Mailer, now: float,
    *, failure_counts: dict[str, int] | None = None, sleep=time.sleep,
    heartbeat=lambda: None,
) -> None:
    """One pass over the non-dormant prestazioni (D19/D21): scrape each one that is
    **due** under the 2-min floor (D22), one at a time (D27). Prestazioni scraped
    within the floor are left as-is (their stored slots stand).

    `failure_counts` (owned by `run`, passed in so it persists across sweeps) tracks
    consecutive transient failures per prestazione: at `FAILURE_NOTIFY_THRESHOLD`
    (~6 min) the subscribers are told watching is currently broken; a success resets
    the count (D11). `heartbeat` is called after each prestazione so a long sweep
    keeps the dead-man liveness fresh (D11, Finding 2)."""
    counts = failure_counts if failure_counts is not None else {}
    for row in store.non_dormant_prestazioni():
        last = row["last_scrape_at"]
        if last is None or (now - last) >= FLOOR_SECONDS:
            code = row["code"]
            status = process_prestazione(store, scraper, mailer, code, now, sleep=sleep)
            _record_cycle_outcome(store, mailer, counts, code, status)
            heartbeat()  # per-prestazione liveness — a long sweep can't look dead (D11)


def _record_cycle_outcome(
    store: Store, mailer: Mailer, counts: dict[str, int], code: str, status: str
) -> None:
    """Update the consecutive-failure counter and notify at the threshold (D11)."""
    if status == "transient_error":
        counts[code] = counts.get(code, 0) + 1
        if counts[code] == FAILURE_NOTIFY_THRESHOLD:
            _notify_watch_failing(store, mailer, code)
    elif status in ("ok", "dormant"):  # a real outcome — the transient streak is broken
        counts.pop(code, None)
    # "skipped_floor": not scraped this cycle (within floor / lost claim) — neutral,
    # neither a failure nor a success, so the streak is left untouched (D39).


def _notify_watch_failing(store: Store, mailer: Mailer, code: str) -> None:
    """Tell a prestazione's active subscribers that watching is currently failing (D11).
    Best-effort per recipient; a failed notice must not break the sweep."""
    notice = render_watch_failing_notice(code, store.prestazione_descrizione(code))
    for email in store.subscriber_emails(code):
        try:
            mailer.send(email, notice)
        except MailerError:
            pass


# ----- check-now lane (D24/D26/D39) -----


def serve_checknow(
    store: Store, scraper: Scraper, mailer: Mailer, now: float,
    *, in_flight: set[str], failure_counts: dict[str, int], sleep=time.sleep,
    heartbeat=lambda: None,
) -> None:
    """Serve outstanding `--check-now` requests, ahead of the sweep (D25/D26).

    For each user with a fire newer than their last completion, scrape each of their
    active prestazioni (`process_prestazione`, so a code the sweep just hit is reused
    via the claim, not re-scraped — D23/D39), then set `last_checknow_at = now` so the
    blocking CLI unblocks — **even on scrape failure** (failure is coarse; the CLI
    prints whatever slots exist, D26). `in_flight` (owned by `run`, in memory per D26)
    guards against re-picking a request already being served; a crash just re-runs it.
    `heartbeat` is called after each scrape so a long batch keeps liveness fresh (D11)."""
    for cf_hash, codes in store.outstanding_checknow():
        if cf_hash in in_flight:
            continue
        in_flight.add(cf_hash)
        try:
            for code in codes:
                status = process_prestazione(store, scraper, mailer, code, now, sleep=sleep)
                _record_cycle_outcome(store, mailer, failure_counts, code, status)
                heartbeat()  # per-prestazione liveness during a long batch (D11)
            store.mark_checknow_done(cf_hash, now)
        finally:
            in_flight.discard(cf_hash)


# ----- registration ack-scrape lane (D14/D40) -----


def serve_registrations(
    store: Store, scraper: Scraper, now: float,
    *, in_flight: set[str], sleep=time.sleep, heartbeat=lambda: None,
) -> None:
    """Resolve pending registration ack-scrapes (D14/D40).

    For each staged `(CF, NRE)`, scrape to discover its prestazione + current slots
    (the CLI can't scrape, D27), write the outcome back for the blocking CLI, and — for
    a **brand-new** prestazione (no active subscriber yet) — baseline its current slots
    as already-seen so the first subscriber starts from "now" and isn't alerted for the
    slots shown at registration (D8). It never persists a target (that waits on the
    user's confirm, D14) and sends no alert. `in_flight` guards re-picking (D26-style);
    `heartbeat` keeps liveness fresh across a slow scrape (D11)."""
    for cf_hash, cf, nre in store.outstanding_registrations():
        if cf_hash in in_flight:
            continue
        in_flight.add(cf_hash)
        try:
            _resolve_registration(store, scraper, cf_hash, cf, nre, now, sleep)
        finally:
            in_flight.discard(cf_hash)
        heartbeat()


def _resolve_registration(
    store: Store, scraper: Scraper, cf_hash: str, cf: str, nre: str, now: float, sleep
) -> None:
    """One ack scrape: resolve the prestazione, baseline a brand-new one, write back (D40)."""
    try:
        result = _scrape_with_retry(scraper, cf, nre, sleep)
    except NREInvalidError:
        store.resolve_registration(cf_hash, now, "invalid")  # dead ricetta — CLI tells the user
        return
    except ScrapeError:
        store.resolve_registration(cf_hash, now, "error")    # transient — CLI says try again
        return
    prest = result.prestazione
    store.upsert_prestazione(prest)
    if not store.has_active_target(prest.code):
        # First subscriber: baseline the current slots as already-seen (no alert). An
        # already-watched code is left to the sweep (its slots are shared, D20), so a
        # new slot found here still alerts existing subscribers next sweep (not suppressed).
        known = store.known_slot_keys(prest.code)
        fresh = [s for s in result.slots if s.slot_key not in known]
        if fresh:
            store.record_new_slots(prest.code, fresh, now)
    store.resolve_registration(cf_hash, now, "ok", prest.code, prest.descrizione)


# ----- dead-man heartbeat (D11) -----


def write_heartbeat(path: str, now: float) -> None:
    """Record the daemon is alive at `now` (D11). Written atomically (temp + rename)
    so an external reader never sees a torn value."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(str(now))
    os.replace(tmp, path)


def heartbeat_is_stale(path: str, now: float, max_age: float = HEARTBEAT_MAX_AGE) -> bool:
    """True if the watcher looks dead: the heartbeat is older than `max_age`, or
    missing/unreadable entirely (D11). A pure primitive for the external checker."""
    try:
        with open(path, encoding="utf-8") as handle:
            last = float(handle.read().strip())
    except (OSError, ValueError):
        return True
    return (now - last) > max_age


def notify_watcher_down(store: Store, mailer: Mailer) -> None:
    """Broadcast the dead-man notice to every user (D11). Called by the external
    checker (not the daemon — it's the one that died); best-effort per recipient.
    The 'once per outage' de-dup is the checker's concern (Phase 5), not here."""
    notice = render_dead_man_notice()
    for email in store.all_user_emails():
        try:
            mailer.send(email, notice)
        except MailerError:
            pass


def seconds_until_next_due(store: Store, now: float) -> float | None:
    """How long to sleep before any prestazione is next due (D21/D22).

    `0.0` if something is due now (e.g. a never-scraped prestazione), the smallest
    remaining floor otherwise, and `None` when there is nothing to watch at all
    (the loop then idles and re-checks, since a user may register meanwhile)."""
    rows = store.non_dormant_prestazioni()
    if not rows:
        return None
    waits = []
    for row in rows:
        last = row["last_scrape_at"]
        if last is None:
            return 0.0
        waits.append(max(0.0, FLOOR_SECONDS - (now - last)))
    return min(waits)


def run(
    store: Store,
    scraper: Scraper,
    mailer: Mailer,
    *,
    lock_path: str = _DEFAULT_LOCK_PATH,
    heartbeat_path: str | None = None,
    clock=time.time,
    sleep=time.sleep,
) -> None:
    """The daemon's self-clocking serial loop (D21/D22/D27). Holds the single-
    instance flock for its whole life (D27), emits a heartbeat (D11), serves the
    check-now lane then sweeps (D25/D39), and sleeps until the next prestazione is
    due — capped at `CHECKNOW_POLL_INTERVAL` so an interactive check-now is answered
    promptly (D39). Runs until interrupted; `clock`/`sleep` are injected for tests."""
    # Consecutive-failure counts + the check-now in-flight set persist across ticks
    # for the whole daemon life. In-memory (D11/D26): a crash resets them, which is
    # harmless (systemd restarts, and the streak / in-flight simply restart too).
    failure_counts: dict[str, int] = {}
    in_flight: set[str] = set()
    reg_in_flight: set[str] = set()
    resolved_heartbeat_path = resolve_heartbeat_path() if heartbeat_path is None else heartbeat_path

    def beat() -> None:  # rewrite the dead-man heartbeat with a fresh timestamp (D11)
        write_heartbeat(resolved_heartbeat_path, clock())

    with single_instance_lock(lock_path):
        while True:
            beat()  # liveness at the top of every tick (D11)
            serve_checknow(store, scraper, mailer, clock(), in_flight=in_flight,
                           failure_counts=failure_counts, sleep=sleep, heartbeat=beat)  # lane first (D25)
            serve_registrations(store, scraper, clock(), in_flight=reg_in_flight,
                                sleep=sleep, heartbeat=beat)  # ack-scrape lane (D14/D40)
            run_sweep(store, scraper, mailer, clock(), failure_counts=failure_counts,
                      sleep=sleep, heartbeat=beat)
            wait = seconds_until_next_due(store, clock())
            # Cap the idle sleep so the next check-now is served within ~POLL_INTERVAL,
            # not up to a full floor (D39). None (nothing watched) → just the poll tick.
            capped = CHECKNOW_POLL_INTERVAL if wait is None else min(wait, CHECKNOW_POLL_INTERVAL)
            sleep(capped)
