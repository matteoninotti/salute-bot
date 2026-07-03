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

from salutebot.alerts import Mailer, fan_out
from salutebot.detector import detect_new_slots
from salutebot.scraper.base import NREInvalidError, Scraper, ScrapeError
from salutebot.store import Store

_DEFAULT_LOCK_PATH = "/tmp/salute-bot.lock"

# The politeness floor (D22): a single prestazione is scraped at most once per this
# many seconds, by the loop or by --check-now. Also the idle re-check interval when
# nothing is being watched.
FLOOR_SECONDS = 120.0

# N=1 (D27): one prestazione scraped at a time. Kept as a named constant because
# D27 wants raising N later to be a one-line change once a global rate cap exists —
# it is NOT an invitation to add workers now (extra workers only raise concurrent
# load on the CUP server, §3).
WORKER_POOL_SIZE = 1


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
    store: Store, scraper: Scraper, mailer: Mailer, code: str, now: float
) -> str:
    """Scrape one prestazione and process its result (N=1, D27). Returns a status.

    Order matters for politeness (D22): the attempt is marked (`last_scrape_at = now`)
    **before** scraping, so a failure or crash still counts against the 2-min floor
    and the loop can never busy-scrape a broken prestazione. A dormant prestazione
    (no active credential, D28) is skipped without marking an attempt.

    Errors are only *classified* here, not yet acted on: rotation on a permanent
    `NREInvalidError` is D28 (next module) and retry/backoff on a transient
    `ScrapeError` is D11 — for now both just end the attempt, so the floor governs
    the retry cadence. On success, detection (D8) + fan-out (D32/D36) run.
    """
    credential = store.representative_credential(code)
    if credential is None:
        return "dormant"
    store.set_last_scrape_at(code, now)
    cf, nre = credential
    try:
        result = scraper.scrape(cf, nre)
    except NREInvalidError:
        return "nre_invalid"
    except ScrapeError:
        return "transient_error"
    detection = detect_new_slots(store, code, result.slots, now)
    if detection.has_new:
        fan_out(store, mailer, detection, now)
    return "ok"


def run_sweep(store: Store, scraper: Scraper, mailer: Mailer, now: float) -> None:
    """One pass over the non-dormant prestazioni (D19/D21): scrape each one that is
    **due** under the 2-min floor (D22), one at a time (D27). Prestazioni scraped
    within the floor are left as-is (their stored slots stand)."""
    for row in store.non_dormant_prestazioni():
        last = row["last_scrape_at"]
        if last is None or (now - last) >= FLOOR_SECONDS:
            process_prestazione(store, scraper, mailer, row["code"], now)


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
    clock=time.time,
    sleep=time.sleep,
) -> None:
    """The daemon's self-clocking serial loop (D21/D22/D27). Holds the single-
    instance flock for its whole life (D27), sweeps, then sleeps exactly until the
    next prestazione is due — no fixed timer (D21). Runs until interrupted; `clock`
    and `sleep` are injected so the cadence is testable without real time."""
    with single_instance_lock(lock_path):
        while True:
            run_sweep(store, scraper, mailer, clock())
            wait = seconds_until_next_due(store, clock())
            sleep(FLOOR_SECONDS if wait is None else wait)
