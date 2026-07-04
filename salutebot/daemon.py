"""The watcher daemon — the long-running Service (D21) that scrapes and alerts.

This module currently provides the **single-instance guard** (D27); the
self-clocking serial loop, representative-NRE rotation, robustness, and
`--check-now`/registration serving land on top of it in later Phase 3 modules.

Single-instance guard (D27): the daemon takes an **exclusive, non-blocking
`flock`** on a lockfile at startup and refuses to start if the lock is already
held. This is a *kernel-owned* lock tied to the open file description, so it
**auto-releases on any exit — crash included** — with no stale-PID-file problem.
It composes with systemd (`Restart=always`, one instance, D21): even a stray
manual launch alongside the managed service cannot spawn a second competing
scraper, which is what keeps single-flight structural (D27).
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
from salutebot.scraper.base import NREInvalidError, Scraper, ScrapeError, ScrapeResult
from salutebot.store import Store

_DEFAULT_LOCK_PATH = "/tmp/salute-bot.lock"
_DEFAULT_HEARTBEAT_PATH = "/tmp/salute-bot.heartbeat"

# Dead-man (D11). The daemon rewrites the heartbeat at the top of every loop pass;
# an EXTERNAL checker (systemd/cron, Phase 5) reads it and, if older than this,
# broadcasts that the watcher is down. Must exceed the longest loop gap — the idle
# sleep is FLOOR_SECONDS (120s) — so a healthy-but-idle daemon is never mistaken
# for dead; 300s leaves generous margin.
HEARTBEAT_MAX_AGE = 300.0

# The politeness floor (D22): a single prestazione is scraped at most once per this
# many seconds, by the loop or by --check-now. Also the idle re-check interval when
# nothing is being watched.
FLOOR_SECONDS = 120.0

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

    Politeness (D22): the attempt is marked (`last_scrape_at = now`) once, before the
    first scrape, so a failure/crash still counts against the 2-min floor and the
    loop can never busy-scrape a broken prestazione. A prestazione with no active
    credential at all is dormant and marks nothing. On success, detection (D8) +
    fan-out (D32/D36/D38) run. The `transient_error` status feeds the across-sweep
    consecutive-failure counter in `run_sweep` (N=3 → notify, D11).
    """
    marked = False
    while True:
        credential = store.representative_credential(code)
        if credential is None:
            return "dormant"  # no active NRE left (or ever) — skip until one is added
        if not marked:
            store.set_last_scrape_at(code, now)
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
) -> None:
    """One pass over the non-dormant prestazioni (D19/D21): scrape each one that is
    **due** under the 2-min floor (D22), one at a time (D27). Prestazioni scraped
    within the floor are left as-is (their stored slots stand).

    `failure_counts` (owned by `run`, passed in so it persists across sweeps) tracks
    consecutive transient failures per prestazione: at `FAILURE_NOTIFY_THRESHOLD`
    (~6 min) the subscribers are told watching is currently broken; a success resets
    the count (D11)."""
    counts = failure_counts if failure_counts is not None else {}
    for row in store.non_dormant_prestazioni():
        last = row["last_scrape_at"]
        if last is None or (now - last) >= FLOOR_SECONDS:
            code = row["code"]
            status = process_prestazione(store, scraper, mailer, code, now, sleep=sleep)
            _record_cycle_outcome(store, mailer, counts, code, status)


def _record_cycle_outcome(
    store: Store, mailer: Mailer, counts: dict[str, int], code: str, status: str
) -> None:
    """Update the consecutive-failure counter and notify at the threshold (D11)."""
    if status == "transient_error":
        counts[code] = counts.get(code, 0) + 1
        if counts[code] == FAILURE_NOTIFY_THRESHOLD:
            _notify_watch_failing(store, mailer, code)
    else:  # ok / dormant — the streak of transient failures is broken
        counts.pop(code, None)


def _notify_watch_failing(store: Store, mailer: Mailer, code: str) -> None:
    """Tell a prestazione's active subscribers that watching is currently failing (D11).
    Best-effort per recipient; a failed notice must not break the sweep."""
    notice = render_watch_failing_notice(code, store.prestazione_descrizione(code))
    for email in store.subscriber_emails(code):
        try:
            mailer.send(email, notice)
        except MailerError:
            pass


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
    heartbeat_path: str = _DEFAULT_HEARTBEAT_PATH,
    clock=time.time,
    sleep=time.sleep,
) -> None:
    """The daemon's self-clocking serial loop (D21/D22/D27). Holds the single-
    instance flock for its whole life (D27), emits a heartbeat (D11), sweeps, then
    sleeps exactly until the next prestazione is due — no fixed timer (D21). Runs
    until interrupted; `clock`/`sleep` are injected so the cadence is testable."""
    # Consecutive-failure counts persist across sweeps for the whole daemon life
    # (D11). In-memory, like D26's in-flight set: a crash resets them, which is
    # harmless (systemd restarts, and the streak simply restarts too).
    failure_counts: dict[str, int] = {}
    with single_instance_lock(lock_path):
        while True:
            write_heartbeat(heartbeat_path, clock())  # liveness for the dead-man (D11)
            run_sweep(store, scraper, mailer, clock(), failure_counts=failure_counts, sleep=sleep)
            wait = seconds_until_next_due(store, clock())
            sleep(FLOOR_SECONDS if wait is None else wait)
