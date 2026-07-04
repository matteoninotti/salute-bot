"""Walking-skeleton demo (Phase 5): the whole pipeline end-to-end, no live scrape.

Deadline insurance (build strategy D): wire the **real** detector (D8), store
(D20), and alert fan-out (D32/D36/D38) to the offline `FixtureScraper` (D5 seam)
and a console mailer, so the tool's reason to exist — *"a new slot opened, here is
the mail"* — is demonstrable deterministically, without a valid NRE, an AWS
account, or the fragile browser drive.

Nothing here is production wiring: it builds an **in-memory** store with
**ephemeral** crypto keys (no env, no persistence, no real secret) and drives the
sweep with an injected clock so the 2-min floor (D22) is crossed instantly. The
mailer defaults to a console printer; pass `SesMailer.from_env()` (env
`SALUTEBOT_DEMO_SES=1`) to send through real SES/LocalStack instead (D15).

Run: `python -m salutebot.demo`.
"""

import base64
import os
import secrets

from salutebot.alerts import EmailContent, Mailer, SesMailer
from salutebot.crypto import Crypto
from salutebot.daemon import process_prestazione, run_sweep
from salutebot.models import Prestazione
from salutebot.scraper.fixture import FixtureScraper
from salutebot.store import Store

# Demo users. CFs are structurally valid but fictitious; emails are placeholders —
# no real secret is involved (the whole point of the fixture skeleton).
_ALICE = ("RSSMRA85T10A562S", "alice@example.com")
_BOB = ("VRDLGI90A01F205X", "bob@example.com")
# A 2-min floor (D22) means each sweep must advance the clock past it; +130 s clears it.
_SWEEP_STEP = 130.0


class ConsoleMailer:
    """A `Mailer` (alerts.Mailer seam) that prints instead of sending (D10 fake).

    Shows each recipient + subject, and the Italian body once per distinct subject
    (D32/D43) so the demo output stays readable when a batch fans out to several
    subscribers. Attributes private (encapsulation guardrail)."""

    def __init__(self, write=print) -> None:
        self.__write = write
        self.__seen_bodies: set[str] = set()
        self.__sent = 0

    @property
    def sent(self) -> int:
        return self.__sent

    def send(self, to_addr: str, content: EmailContent) -> None:
        self.__sent += 1
        self.__write(f"    ✉  email → {to_addr}  |  {content.subject}")
        if content.subject not in self.__seen_bodies:
            self.__seen_bodies.add(content.subject)
            for line in content.text.splitlines():
                self.__write(f"       | {line}")


def _ephemeral_crypto() -> Crypto:
    """A `Crypto` with fresh in-process keys — never persisted, never from env.

    The demo store is in-memory and thrown away, so the keys need only be valid
    (a real Fernet key + a distinct HMAC secret, D29 addendum), not stable."""
    enc_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
    hmac_key = secrets.token_hex(32)
    return Crypto(enc_key, hmac_key)


def _seed(store: Store) -> Prestazione:
    """Register the two demo users and subscribe both to the fixture prestazione."""
    prest = Prestazione(code="8901.20", descrizione="VISITA UROLOGICA DI CONTROLLO",
                        quantita=1)
    for cf, email in (_ALICE, _BOB):
        store.add_user(cf, email)
        store.add_target(cf, prest, nre=f"DEMO{cf[:11]}")  # placeholder NRE, never scraped for real
    return prest


def _scene_alerts(store: Store, mailer: Mailer, write) -> None:
    """Scene 1 — the alert path: baseline → no-change → a new slot appears (D8/D32)."""
    scraper = FixtureScraper.from_recon(baseline=4, added=1)
    write("\n=== Scene 1 — un posto si libera (rilevamento + notifica) ===")
    labels = [
        "Sweep 1 — primo controllo: tutti i posti sono nuovi → notifica iniziale",
        "Sweep 2 — nessun cambiamento → nessuna email (dedup permanente, D8)",
        "Sweep 3 — un nuovo posto compare → email col nuovo evidenziato (D32)",
    ]
    base = 1_000_000.0
    for i, label in enumerate(labels):
        write(f"\n{label}")
        before = mailer.sent if isinstance(mailer, ConsoleMailer) else 0
        run_sweep(store, scraper, mailer, now=base + i * _SWEEP_STEP)
        if isinstance(mailer, ConsoleMailer) and mailer.sent == before:
            write("    (nessuna email inviata)")


def _scene_rotation(write) -> None:
    """Scene 2 — D28: the representative NRE is dead → deactivate + notify + rotate."""
    write("\n=== Scene 2 — la ricetta del primo iscritto è morta (rotazione D28) ===")
    store = Store(":memory:", _ephemeral_crypto())
    mailer = ConsoleMailer(write)
    prest = _seed(store)
    dead_nre = f"DEMO{_ALICE[0][:11]}"  # Alice is the first active target → the representative
    scraper = FixtureScraper.from_recon(baseline=4, added=0, dead_nres=[dead_nre])
    write("\nAlice guida lo scrape; la sua ricetta risulta non valida:")
    process_prestazione(store, scraper, mailer, prest.code, now=2_000_000.0)
    alice_targets = store.get_user_targets(_ALICE[0])
    write(f"    → target di Alice ora: "
          f"{'attivo' if alice_targets[0]['active'] else 'DISATTIVATO'} "
          f"(rotazione a Bob, lo scrape prosegue)")
    store.close()


def _scene_list(store: Store, write) -> None:
    """Scene 3 — the `--list` view over the shared per-prestazione slots (D20)."""
    write("\n=== Scene 3 — cosa vede Alice con `--list` ===")
    rows = store.list_user_slots(_ALICE[0])
    for row in rows:
        where = row["struttura"] or "?"
        write(f"    {row['iso_date']} {row['time']} — {where}")


def run_demo(*, mailer: Mailer | None = None, write=print) -> None:
    """Drive the full skeleton against the fixture scraper (deterministic)."""
    write("salute-bot — demo end-to-end (scraper finto, pipeline reale)")
    store = Store(":memory:", _ephemeral_crypto())
    the_mailer = mailer if mailer is not None else ConsoleMailer(write)
    _seed(store)
    _scene_alerts(store, the_mailer, write)
    _scene_list(store, write)
    store.close()
    _scene_rotation(write)
    write("\nFatto. Pipeline reale (detector/store/fan-out) su dati di recon reali, "
          "senza NRE né browser.")


def main() -> None:
    """Console entrypoint. `SALUTEBOT_DEMO_SES=1` swaps the console mailer for real
    SES (needs `SALUTEBOT_SENDER_EMAIL` + verified recipients, D15)."""
    use_ses = os.environ.get("SALUTEBOT_DEMO_SES", "").strip().lower() in ("1", "true", "yes")
    run_demo(mailer=SesMailer.from_env() if use_ses else None)


if __name__ == "__main__":
    main()
