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
    (`targets -> users`, D20), send to each, and **only on a fully successful
    batch** persist the new slots (D36). A send failure leaves them unpersisted,
    so the next sweep re-detects and re-attempts -- at-least-once, trading a rare
    duplicate for never silently dropping the one alert the tool exists to send.

Recipient addresses are ordinary contact data, not CF/NRE secrets, so they may
appear in a message; no CF/NRE ever passes through here.
"""

import os
from dataclasses import dataclass
from html import escape
from typing import Protocol

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from salutebot.models import DetectionResult, Slot
from salutebot.store import Store


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

    `persisted` is the load-bearing field: True means every recipient's send
    succeeded and the new slots were recorded (D36); False means either there was
    nothing to send or a send failed, so the new slots stay unpersisted for the
    next sweep to retry.
    """

    recipients: int
    sent: int
    persisted: bool


class Mailer(Protocol):
    """The seam the fan-out depends on -- `SesMailer` in prod, a fake in tests."""

    def send(self, to_addr: str, content: EmailContent) -> None:
        """Deliver one email; raise `MailerError` on a transport failure."""
        ...


class SesMailer:
    """Sends alert emails via AWS SES (D10/D15). Attributes private (encapsulation)."""

    def __init__(self, sender: str, client: object) -> None:
        # `sender` must be an SES-verified address in sandbox (D15); `client` is a
        # boto3 SES client, injected so tests need no AWS.
        self.__sender = sender
        self.__client = client

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "SesMailer":
        """Build from env: `SALUTEBOT_SENDER_EMAIL` (required, the verified sender);
        optional `SALUTEBOT_AWS_REGION`/`AWS_REGION` and `SALUTEBOT_SES_ENDPOINT`
        (the last points boto3 at LocalStack in CI, D12/D15)."""
        source = os.environ if env is None else env
        sender = source.get("SALUTEBOT_SENDER_EMAIL")
        if not sender:
            raise MailerConfigError(
                "SALUTEBOT_SENDER_EMAIL is not set -- SES needs a verified sender "
                "address (sandbox, D15). Set it before sending alerts."
            )
        kwargs: dict[str, str] = {}
        region = source.get("SALUTEBOT_AWS_REGION") or source.get("AWS_REGION")
        if region:
            kwargs["region_name"] = region
        endpoint = source.get("SALUTEBOT_SES_ENDPOINT")
        if endpoint:
            kwargs["endpoint_url"] = endpoint
        return cls(sender, boto3.client("ses", **kwargs))

    def send(self, to_addr: str, content: EmailContent) -> None:
        try:
            self.__client.send_email(
                Source=self.__sender,
                Destination={"ToAddresses": [to_addr]},
                Message={
                    "Subject": {"Data": content.subject, "Charset": "UTF-8"},
                    "Body": {
                        "Text": {"Data": content.text, "Charset": "UTF-8"},
                        "Html": {"Data": content.html, "Charset": "UTF-8"},
                    },
                },
            )
        except (BotoCoreError, ClientError) as exc:
            # No recipient in the message: keep addresses out of error text/logs.
            raise MailerError(f"SES send failed: {type(exc).__name__}") from exc


def fan_out(
    store: Store, mailer: Mailer, result: DetectionResult, now: float | None = None
) -> FanOutResult:
    """Email every active subscriber of the prestazione, then persist (D20/D36).

    Ordering is D36's at-least-once: send first, record the new slots only if the
    whole batch succeeded. On any send failure nothing is persisted, so the next
    sweep re-detects the same keys and retries (recipients who already got the
    mail receive a bounded duplicate -- accepted over a lost alert). Per-recipient
    retry/backoff and the failure-notification are the daemon's job (D11), not
    this function's.
    """
    if not result.has_new:
        return FanOutResult(recipients=0, sent=0, persisted=False)

    recipients = store.subscriber_emails(result.prestazione)
    if not recipients:
        # A scraped prestazione always has >=1 active target driving it (D28), so
        # this is a can't-happen guard; nothing to send, nothing recorded.
        return FanOutResult(recipients=0, sent=0, persisted=False)

    content = render_alert(result)
    sent = 0
    failed = False
    for addr in recipients:
        try:
            mailer.send(addr, content)
            sent += 1
        except MailerError:
            failed = True

    if failed:
        return FanOutResult(recipients=len(recipients), sent=sent, persisted=False)

    store.record_new_slots(result.prestazione, result.new_slots, now)  # post-send (D36)
    return FanOutResult(recipients=len(recipients), sent=sent, persisted=True)


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
        text_lines.append(("  >>> " if is_new else "      ") + _slot_line(slot))
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
        f"<ul>{''.join(html_items)}</ul>"
    )
    return EmailContent(subject=subject, text="\n".join(text_lines), html=html)


def _slot_line(slot: Slot) -> str:
    """One-line human rendering of a slot for the alert body."""
    where = slot.struttura or "?"
    if slot.address:
        where = f"{where}, {slot.address}"
    return f"{slot.iso_date} {slot.time} — {where}"
