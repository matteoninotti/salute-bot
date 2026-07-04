"""Terminal CLI client — the management surface over the store (D5/D14/D35).

Per D27 the CLI **never scrapes**; it only reads/writes SQLite rows. Login (`-u`),
`--list`, `--disable` (menu), `--disable-all`, `--delete-user`, and the returning-
user display are pure row operations. `--check-now` also never scrapes here (D27):
it writes a request timestamp and **blocks** until the daemon serves it (D24/D26) —
the scrape happens in the daemon. New-user registration and adding a prestazione
(D14) work the same way: the CLI stages an unresolved `(CF, NRE)` (D40), blocks while
the daemon runs the acknowledgment scrape (NRE → prestazione + initial slots), shows
the result for confirmation, then writes the user/target rows — still no scrape here.

Secret hygiene (D35): no secret is ever a command-line argument. `-u`'s CF value
is **optional** (prompt when omitted, so it need not enter shell history / the
process table); `--disable` uses a **numbered menu** (no NRE typed); nothing here
echoes `argv` or a secret. The CF is treated as identifying-but-not-terminal-
secret, so it is prompted in the clear; no NRE is ever handled by this surface.

I/O is injected (`read`/`write` default to `input`/`print`) so the whole flow is
driftless to test without a TTY.
"""

import argparse
import math
import os
import time

from salutebot.config import EnvConfig
from salutebot.crypto import Crypto
from salutebot.models import Prestazione
from salutebot.store import Store
from salutebot.validation import validate_cf, validate_nre

_DB_PATH_VAR = "SALUTEBOT_DB"
_DEFAULT_DB = "salute-bot.db"

# --check-now anti-abuse throttle: a 2nd fire within this many seconds, per user,
# is rejected (D23). Enforced CLI-side, the cooldown owner (D26).
COOLDOWN_SECONDS = 300.0
# How often the blocking CLI re-checks whether the daemon has served its request
# (D24 strictly-blocking). Small; injected in tests.
_CHECKNOW_POLL = 1.0


def main(argv: list[str] | None = None, *, store: Store | None = None,
         read=input, write=print, clock=time.time, sleep=time.sleep) -> None:
    """Entry point. Builds a real `Store` from env unless one is injected (tests).
    `clock`/`sleep` back `--check-now`'s cooldown + block-poll and are injectable."""
    args = _build_parser().parse_args(argv)
    if store is not None:
        _dispatch(args, store, read, write, clock, sleep)  # injected (tests)
        return
    owned = Store(os.environ.get(_DB_PATH_VAR, _DEFAULT_DB), Crypto.from_env(EnvConfig()))
    try:
        _dispatch(args, owned, read, write, clock, sleep)
    finally:
        owned.close()  # only close the store we created


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="salute-bot",
        description="Watcher di disponibilità per il CUP SSN Piemonte (accesso senza login): "
                    "ti avvisa appena si libera un posto.",
    )
    # -u takes an OPTIONAL CF (D35): `-u CF` uses it, bare `-u` prompts. Absent =>
    # None. A management flag without -u also prompts, via _resolve_cf.
    parser.add_argument("-u", "--user", nargs="?", const="", default=None, metavar="CF",
                        help="accedi con questo CF (valore opzionale; se omesso viene richiesto)")
    parser.add_argument("--check-now", action="store_true",
                        help="chiedi al watcher di aggiornare subito le tue prestazioni e mostra i risultati")
    parser.add_argument("--list", action="store_true",
                        help="elenca tutti i posti trovati finora per le prestazioni che segui")
    parser.add_argument("--disable", action="store_true",
                        help="disattiva le notifiche per una prestazione (menu numerato)")
    parser.add_argument("--disable-all", action="store_true",
                        help="disattiva tutte le tue notifiche")
    parser.add_argument("--delete-user", action="store_true",
                        help="cancella definitivamente tutti i tuoi dati (riscrivi il CF per confermare)")
    return parser


def _dispatch(args, store: Store, read, write, clock, sleep) -> None:
    if args.check_now:
        _with_user(args.user, store, read, write,
                   lambda s, cf, w: _cmd_check_now(s, cf, w, clock=clock, sleep=sleep))
    elif args.list:
        _with_user(args.user, store, read, write, _cmd_list)
    elif args.disable:
        _with_user(args.user, store, read, write,
                   lambda s, cf, w: _cmd_disable(s, cf, read, w))
    elif args.disable_all:
        _with_user(args.user, store, read, write, _cmd_disable_all)
    elif args.delete_user:
        _with_user(args.user, store, read, write,
                   lambda s, cf, w: _cmd_delete_user(s, cf, read, w))
    elif args.user is not None:  # -u given, no other flag => log in + show status
        _with_user(args.user, store, read, write, _returning_user_menu)
    else:  # bare invocation => registration / interactive manage menu
        _registration(store, read, write, clock, sleep)


# ----- CF resolution + guard -----

def _resolve_cf(user_arg, read, write) -> str | None:
    """A validated CF from the `-u` value, or a prompt when it was omitted."""
    raw = user_arg if user_arg else read("Codice Fiscale: ")
    try:
        return validate_cf(raw)
    except ValueError as err:
        write(str(err))
        return None


def _with_user(user_arg, store: Store, read, write, action) -> None:
    """Resolve + require an existing user, then run `action(store, cf, write)`."""
    cf = _resolve_cf(user_arg, read, write)
    if cf is None:
        return
    if not store.user_exists(cf):
        write("Nessun utente trovato per questo CF. Avvia salute-bot senza argomenti per registrarti.")
        return
    action(store, cf, write)


# ----- commands (all read-only or row-level; no scrape, D27) -----

def _cmd_list(store: Store, cf: str, write) -> None:
    rows = store.list_user_slots(cf)
    if not rows:
        write("Nessun posto trovato finora per le prestazioni che segui.")
        return
    current_code = None
    for row in rows:
        if row["code"] != current_code:
            current_code = row["code"]
            write(f"\n{row['code']} — {row['descrizione']}")
        where = row["struttura"] or "?"
        if row["address"]:
            where = f"{where}, {row['address']}"
        write(f"  {row['iso_date']} {row['time']} — {where}")


def _cmd_check_now(store: Store, cf: str, write, *, clock, sleep) -> None:
    """Ask the daemon to refresh this user's prestazioni now, then print them (D24/D26).

    The CLI owns the cooldown (D26): a 2nd fire within 5 min (D23) prints only the
    remaining cooldown and exits — no request, no wait. Otherwise it records the fire
    and **blocks** (D24, no email fallback) until the daemon signals completion
    (`last_checknow_at > this request`), then shows the user's current slots. Requires
    the watcher daemon to be running; with none, it waits (D24 is strictly blocking)."""
    remaining = store.checknow_cooldown_remaining(cf, clock(), COOLDOWN_SECONDS)
    if remaining > 0:
        write(f"Controllo immediato in pausa — riprova tra {math.ceil(remaining)}s.")
        return
    request_ts = clock()
    store.accept_checknow(cf, request_ts)
    write("Controllo immediato in coda — potrebbe richiedere qualche istante...")
    while not store.checknow_served_since(cf, request_ts):
        sleep(_CHECKNOW_POLL)
    _cmd_list(store, cf, write)


def _cmd_disable(store: Store, cf: str, read, write) -> None:
    targets = store.get_user_targets(cf)
    if not targets:
        write("Non segui nessuna prestazione.")
        return
    write("Per quale prestazione vuoi disattivare le notifiche?")
    for i, target in enumerate(targets, 1):
        off = "" if target["active"] else "  (già disattivata)"
        write(f"  {i}) {target['code']} — {target['descrizione']}{off}")
    choice = read("Numero (vuoto per annullare): ").strip()
    if not choice:
        write("Annullato.")
        return
    if not choice.isdigit() or not (1 <= int(choice) <= len(targets)):
        write("Scelta non valida — nulla è stato modificato.")
        return
    picked = targets[int(choice) - 1]
    store.deactivate_target(cf, picked["code"])
    write(f"Notifiche disattivate per {picked['code']}.")


def _cmd_disable_all(store: Store, cf: str, write) -> None:
    n = store.deactivate_all_targets(cf)
    write(f"Notifiche disattivate per {n} prestazione/i.")


def _cmd_delete_user(store: Store, cf: str, read, write) -> None:
    write("Questo CANCELLA tutti i tuoi dati (CF, email, NRE, posti trovati). L'operazione è irreversibile.")
    confirm = read("Riscrivi il tuo CF per confermare: ")
    try:
        confirmed = validate_cf(confirm)
    except ValueError:
        confirmed = None
    if confirmed != cf:
        write("Il CF non corrisponde — nulla è stato cancellato.")
        return
    store.delete_user(cf)
    write("I tuoi dati sono stati cancellati definitivamente.")


# ----- registration / add-prestazione (D14, daemon-driven ack scrape) -----

def _registration(store: Store, read, write, clock, sleep) -> None:
    """Bare invocation: register a new user, or show an existing user's manage menu.

    New user → collect email + NRE, then the shared ack-scrape flow (D14). Existing
    user → status + an interactive offer to add another prestazione."""
    cf = _resolve_cf(None, read, write)
    if cf is None:
        return
    if store.user_exists(cf):
        _returning_user_menu(store, cf, write)
        email = store.get_email(cf)
        if email is not None:  # always set for an existing user; guard satisfies the type
            _offer_add_prestazione(store, cf, email, read, write, clock, sleep)
        return
    email = read("Email per le notifiche: ").strip()
    if not _valid_email(email):
        write("Non sembra un indirizzo email valido. Nulla è stato salvato.")
        return
    nre = _prompt_nre(read, write)
    if nre is None:
        return
    _run_ack(store, cf, email, nre, read, write, clock, sleep)


def _returning_user_menu(store: Store, cf: str, write) -> None:
    """Read-only status (the `-u` login view — no prompts, so it never blocks)."""
    write(f"Bentornato. Le notifiche arrivano a {store.get_email(cf)}.")
    targets = store.get_user_targets(cf)
    if not targets:
        write("Non segui ancora nessuna prestazione.")
    else:
        write("Stai seguendo:")
        for target in targets:
            state = "attiva" if target["active"] else "disattivata"
            write(f"  {target['code']} — {target['descrizione']}  [{state}]")
    write("Gestisci con: --check-now, --list, --disable, --disable-all, --delete-user.")


def _offer_add_prestazione(store: Store, cf: str, email: str, read, write, clock, sleep) -> None:
    """Interactive add-prestazione for an existing user (D14/D37 — a menu action, not a
    flag). Enter an NRE to watch another prestazione; blank to skip."""
    raw = read("Aggiungere una prestazione? Inserisci il suo NRE (vuoto per saltare): ").strip()
    if not raw:
        return
    try:
        nre = validate_nre(raw)
    except ValueError as err:
        write(str(err))
        return
    _run_ack(store, cf, email, nre, read, write, clock, sleep)


def _run_ack(store: Store, cf: str, email: str, nre: str, read, write, clock, sleep) -> None:
    """Stage an ack-scrape (D40), block until the daemon resolves it, show the
    prestazione + initial slots, confirm, and persist the target (D14/D27).

    The CLI never scrapes — it writes the request and waits for the daemon (needs the
    watcher running). On 'invalid'/'error' nothing is saved; on confirm it writes the
    user (if new) and the target."""
    request_ts = clock()
    store.submit_registration(cf, email, nre, request_ts)
    write("Verifica della ricetta sul sistema di prenotazione — potrebbe richiedere qualche istante...")
    result = store.registration_result(cf, request_ts)
    while result is None:
        sleep(_CHECKNOW_POLL)
        result = store.registration_result(cf, request_ts)

    if result["status"] != "ok":
        store.clear_registration(cf)
        if result["status"] == "invalid":
            write("Questa ricetta (NRE) non è valida — scaduta, già utilizzata o non "
                  "riconosciuta. Nulla è stato salvato.")
        else:
            write("Impossibile contattare il sistema di prenotazione al momento — "
                  "riprova tra poco. Nulla è stato salvato.")
        return

    code, desc = result["code"], result["desc"]
    write(f"\nLa tua ricetta sblocca: {desc} ({code}).")
    _print_code_slots(store, code, write)
    answer = read("Vuoi seguire questa prestazione? [s/N]: ").strip().lower()
    store.clear_registration(cf)
    if answer not in ("s", "si", "sì", "y", "yes"):
        write("Ok — non verrà seguita. Nulla è stato salvato.")
        return
    if not store.user_exists(cf):
        store.add_user(cf, email)
    store.add_target(cf, Prestazione(code=code, descrizione=desc, quantita=None), nre)
    write(f"Fatto — ora segui {desc} ({code}). Le notifiche arrivano a {email}.")


def _print_code_slots(store: Store, code: str, write) -> None:
    rows = store.slots_for_code(code)
    if not rows:
        write("Al momento non ci sono posti disponibili — ti avviseremo appena se ne libera uno.")
        return
    write(f"Al momento {len(rows)} posto/i disponibile/i:")
    for row in rows:
        where = row["struttura"] or "?"
        if row["address"]:
            where = f"{where}, {row['address']}"
        write(f"  {row['iso_date']} {row['time']} — {where}")


def _prompt_nre(read, write) -> str | None:
    """Prompt for and validate an NRE (never echoed — D35); None on invalid format."""
    try:
        return validate_nre(read("NRE (numero ricetta): "))
    except ValueError as err:
        write(str(err))
        return None


def _valid_email(email: str) -> bool:
    """Minimal structural check — an `@` with a dotted domain. Not RFC-complete."""
    at = email.count("@")
    return at == 1 and "." in email.split("@")[1] and not email.endswith(".")


if __name__ == "__main__":
    main()
