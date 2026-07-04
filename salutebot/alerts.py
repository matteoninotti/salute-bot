"""Alert fan-out: turn a detector result into per-subscriber emails (D10/D15/D32),
delivered at-least-once (D36).

Three parts, kept separate for testability:
  - `render_alert` -- pure: a `DetectionResult` -> an `EmailContent` (subject +
    text + HTML). Per D32 the email lists the **whole current availability**,
    with the newly-appeared slots **highlighted** -- never the new ones alone,
    because the user's decision depends on seeing a new opening against
    everything else on offer.
  - `SesMailer` -- the only AWS-touching part: sends one `EmailContent` to one
    address via SES (D10/D15). The boto3 client is injected, so tests drive a
    fake and CI drives LocalStack; a real-AWS smoke to a verified address stays a
    separate check (D15).
  - `fan_out` -- orchestration: resolve the prestazione's active subscribers
    (`targets -> users`, D20), send to each with a bounded per-recipient retry,
    and persist the new slots **once at least one recipient was delivered**
    (D38, refining D36). A recipient still failing after its retries is
    abandoned and surfaced in `FanOutResult.failed`; only a **total** failure
    (zero delivered) leaves the slots unpersisted, so the next sweep re-detects
    and retries the whole batch -- at-least-once for outages, without spamming
    the good recipients to chase one dead mailbox.

Recipient addresses are ordinary contact data, not CF/NRE secrets, so they may
appear in a message; no CF/NRE ever passes through here.
"""

import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from html import escape
from typing import Any, Protocol, cast

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from salutebot.models import DetectionResult, Slot
from salutebot.store import Store

# Per-recipient send retry (D38, D11-style). A transient SES/transport error on
# one recipient is retried up to this many times with exponential backoff, inside
# the same fan-out; a recipient still failing after them is abandoned (a dead
# mailbox is unreachable — chasing it must not re-alert the others).
SEND_RETRY_ATTEMPTS = 3
SEND_RETRY_BACKOFF_BASE = 1.0



class MailerError(RuntimeError):
    """A single email send failed at the transport level (wraps the SES error)."""



class MailerConfigError(RuntimeError):
    """Required mailer configuration (e.g. the verified sender) is missing."""



@dataclass(frozen=True)
class EmailContent:
    """A rendered alert email. DTO carve-out (same as `Slot`): frozen data-carrier
    whose public fields are its read-only interface."""

    subject: str
    text: str
    html: str



@dataclass(frozen=True)
class FanOutResult:
    """Outcome of one prestazione's fan-out. DTO carve-out (frozen data-carrier).

    `persisted` is the load-bearing field (D38): True means at least one recipient
    was delivered and the new slots were recorded — good recipients are therefore
    never re-alerted. False means either there was nothing to send or the batch
    was a *total* failure (zero delivered), so the new slots stay unpersisted for
    the next sweep to re-detect and retry.

    `sent` counts delivered recipients; `failed` names the recipients that were
    abandoned after their per-recipient retries (a dead mailbox) — surfaced but
    not yet escalated (D38 residual).
    """

    recipients: int
    sent: int
    persisted: bool
    failed: tuple[str, ...] = field(default_factory=tuple)



class Mailer(Protocol):
    """The seam the fan-out depends on -- `SesMailer` in prod, a fake in tests."""

    def send(self, to_addr: str, content: EmailContent) -> None:
        """Deliver one email; raise `MailerError` on a transport failure."""
        ...



class SesClient(Protocol):
    """The slice of boto3's SES client `SesMailer` actually calls -- boto3 ships no
    stubs, so `object` would hide this contract from callers/tests entirely."""

    def send_email(self, *, Source: str, Destination: dict[str, Any],
                   Message: dict[str, Any]) -> dict[str, Any]:
        ...



class SesMailer:
    """Sends alert emails via AWS SES (D10/D15). Attributes private (encapsulation)."""

    def __init__(self, sender: str, client: SesClient) -> None:
        # `sender` must be an SES-verified address in sandbox (D15); `client` is a
        # boto3 SES client, injected so tests need no AWS.
        self.__sender = sender
        self.__client = client

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "SesMailer":
        """Build from env: `SALUTEBOT_SENDER_EMAIL` (required, the verified sender);
        optional `SALUTEBOT_AWS_REGION`/`AWS_REGION` and `SALUTEBOT_SES_ENDPOINT`
        (the last points boto3 at LocalStack in CI, D12/D15). Typed as `Mapping`,
        not `dict`: `os.environ` is an `os._Environ`, which does not subclass `dict`."""
        source = os.environ if env is None else env
        sender = source.get("SALUTEBOT_SENDER_EMAIL")
        if not sender:
            raise MailerConfigError(
                "SALUTEBOT_SENDER_EMAIL is not set -- SES needs a verified sender "
                "address (sandbox, D15). Set it before sending alerts.")
        kwargs: dict[str, str] = {}
        region = source.get("SALUTEBOT_AWS_REGION") or source.get("AWS_REGION")
        if region:
            kwargs["region_name"] = region
        endpoint = source.get("SALUTEBOT_SES_ENDPOINT")
        if endpoint:
            kwargs["endpoint_url"] = endpoint
        # botocore generates `send_email` dynamically, so the client doesn't
        # statically satisfy SesClient though it does at runtime -- cast at this one
        # boto3 boundary rather than weaken the Protocol that keeps `send` type-safe.
        client = cast(SesClient, boto3.client("ses", **kwargs))
        return cls(sender, client)

    def send(self, to_addr: str, content: EmailContent) -> None:
        try:
            self.__client.send_email(
                Source=self.__sender,
                Destination={"ToAddresses": [to_addr]},
                Message={
                    "Subject": {
                        "Data": content.subject,
                        "Charset": "UTF-8"
                    },
                    "Body": {
                        "Text": {
                            "Data": content.text,
                            "Charset": "UTF-8"
                        },
                        "Html": {
                            "Data": content.html,
                            "Charset": "UTF-8"
                        },
                    },
                },
            )
        except (BotoCoreError, ClientError) as exc:
            # No recipient in the message: keep addresses out of error text/logs.
            raise MailerError(
                f"SES send failed: {type(exc).__name__}") from exc



def fan_out(store: Store,
            mailer: Mailer,
            result: DetectionResult,
            now: float | None = None,
            *,
            sleep=time.sleep) -> FanOutResult:
    """Email every active subscriber of the prestazione, then persist (D20/D38).

    Send each recipient with a bounded per-recipient retry (`_send_with_retry`),
    then persist the new slots **once at least one recipient was delivered** (D38):
    good recipients are recorded as alerted and never re-emailed, while a recipient
    still failing after its retries is abandoned and returned in `failed`. Only a
    *total* failure (zero delivered — a systemic SES outage, not one dead mailbox)
    leaves the slots unpersisted, so the next sweep re-detects and retries the
    whole batch (D36's outage self-heal, preserved). `sleep` is injected so the
    retry backoff is testable.
    """
    if not result.has_new:
        return FanOutResult(recipients=0, sent=0, persisted=False)

    recipients = store.subscriber_emails(result.prestazione)
    if not recipients:
        # A scraped prestazione always has >=1 active target driving it (D28), so
        # this is a can't-happen guard; nothing to send, nothing recorded.
        return FanOutResult(recipients=0, sent=0, persisted=False)

    content = render_alert(result)
    delivered = 0
    failed: list[str] = []
    for addr in recipients:
        if _send_with_retry(mailer, addr, content, sleep):
            delivered += 1
        else:
            failed.append(addr)

    persisted = delivered > 0
    if persisted:
        store.record_new_slots(result.prestazione, result.new_slots, now)  # D38
    return FanOutResult(recipients=len(recipients), sent=delivered,
                        persisted=persisted, failed=tuple(failed))


def _send_with_retry(mailer: Mailer, addr: str, content: EmailContent, sleep) -> bool:
    """Send one recipient with bounded retry + exponential backoff (D38/D11).

    Returns True on delivery, False if every attempt raised `MailerError` (the
    recipient is then abandoned by the caller). Mirrors the scraper's
    `_scrape_with_retry` shape so the two robustness layers read the same."""
    delay = SEND_RETRY_BACKOFF_BASE
    for attempt in range(SEND_RETRY_ATTEMPTS):
        try:
            mailer.send(addr, content)
            return True
        except MailerError:
            if attempt == SEND_RETRY_ATTEMPTS - 1:
                return False
            sleep(delay)
            delay *= 2
    return False



def render_alert(result: DetectionResult) -> EmailContent:
    """Render the D32 alert: the full current availability, new slots highlighted.

    Assumes `result.has_new` (the fan-out only renders when there is something to
    alert); `all_slots` is non-empty because `new_slots` is a subset of it.
    """
    new_keys = {slot.slot_key for slot in result.new_slots}
    desc = result.all_slots[0].prestazione_desc or result.prestazione
    n = len(result.new_slots)
    posti = "1 nuovo posto" if n == 1 else f"{n} nuovi posti"
    subject = f"salute-bot: {posti} — {desc}"

    text_lines = [
        f"{posti.capitalize()} per {desc} ({result.prestazione}).",
        "",
        "Disponibilità attuale (>>> = nuovo):",
        "",
    ]
    html_items = []
    for slot in result.all_slots:
        is_new = slot.slot_key in new_keys
        text_lines.append(("  >>> " if is_new else "      ") +
                          _slot_line(slot))
        if is_new:
            html_items.append(
                f'<li style="background:#fff3cd;font-weight:bold">'
                f"{escape(_slot_line(slot))} <span>&#127381; NUOVO</span></li>"
            )
        else:
            html_items.append(f"<li>{escape(_slot_line(slot))}</li>")

    html = (
        f"<p>{escape(posti.capitalize())} per <strong>{escape(desc)}</strong> "
        f"({escape(result.prestazione)}).</p>"
        f'<p>Disponibilità attuale (i nuovi sono evidenziati):</p>'
        f"<ul>{''.join(html_items)}</ul>")
    return EmailContent(subject=subject, text="\n".join(text_lines), html=html)



def render_nre_invalid_notice(code: str, descrizione: str | None = None) -> EmailContent:
    """The D28 owner notice: the user's ricetta/NRE is permanently invalid, so their
    scrape can no longer run — they must re-add a fresh one. Sent to that one owner,
    not fanned out; Italian, matching the message wording D28 specifies."""
    what = f"{descrizione} ({code})" if descrizione else code
    subject = f"salute-bot: la tua ricetta per {what} non è più valida"
    body = (
        f"La ricetta (NRE) che avevi inserito per {what} non è più valida "
        "— è scaduta, già utilizzata, o non più riconosciuta dal CUP.\n\n"
        "Per continuare a ricevere notifiche su questa prestazione, reinserisci "
        "una nuova ricetta valida."
    )
    html = (
        f"<p>La ricetta (NRE) che avevi inserito per <strong>{escape(what)}</strong> "
        "non è più valida — è scaduta, già utilizzata, o non più riconosciuta dal CUP.</p>"
        "<p>Per continuare a ricevere notifiche su questa prestazione, "
        "reinserisci una nuova ricetta valida.</p>"
    )
    return EmailContent(subject=subject, text=body, html=html)


def render_watch_failing_notice(code: str, descrizione: str | None = None) -> EmailContent:
    """The D11 notice: watching this prestazione has failed N=3 consecutive cycles
    (~6 min) with transient portal errors, so the user learns the silent watch is
    currently broken (not that their ricetta is invalid — that is D28's notice)."""
    what = f"{descrizione} ({code})" if descrizione else code
    subject = f"salute-bot: problemi temporanei nel monitoraggio di {what}"
    body = (
        f"Il monitoraggio della prestazione {what} sta riscontrando ripetuti errori "
        "tecnici nell'accesso al portale CUP.\n\n"
        "Continuiamo a riprovare automaticamente. La tua ricetta è ancora valida — "
        "non devi fare nulla; ti avviseremo appena trovi nuovi posti."
    )
    html = (
        f"<p>Il monitoraggio della prestazione <strong>{escape(what)}</strong> sta "
        "riscontrando ripetuti errori tecnici nell'accesso al portale CUP.</p>"
        "<p>Continuiamo a riprovare automaticamente. La tua ricetta è ancora valida "
        "— non devi fare nulla; ti avviseremo appena trovi nuovi posti.</p>"
    )
    return EmailContent(subject=subject, text=body, html=html)


def render_dead_man_notice() -> EmailContent:
    """The D11 dead-man notice: the watcher itself has stopped, so *no* prestazione
    is being checked. Broadcast to all users by an external checker (a live daemon
    can't send this about itself) when the heartbeat goes stale."""
    subject = "salute-bot: il servizio di monitoraggio è temporaneamente sospeso"
    body = (
        "Il servizio di monitoraggio salute-bot si è interrotto e al momento non sta "
        "controllando la disponibilità dei posti.\n\n"
        "Stiamo lavorando per ripristinarlo e riprenderemo ad avvisarti appena il "
        "servizio torna attivo. Le tue ricette restano registrate."
    )
    html = (
        "<p>Il servizio di monitoraggio salute-bot si è interrotto e al momento non "
        "sta controllando la disponibilità dei posti.</p>"
        "<p>Stiamo lavorando per ripristinarlo e riprenderemo ad avvisarti appena il "
        "servizio torna attivo. Le tue ricette restano registrate.</p>"
    )
    return EmailContent(subject=subject, text=body, html=html)


def _slot_line(slot: Slot) -> str:
    """One-line human rendering of a slot for the alert body."""
    where = slot.struttura or "?"
    if slot.address:
        where = f"{where}, {slot.address}"
    return f"{slot.iso_date} {slot.time} — {where}"
