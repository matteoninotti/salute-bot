"""Tests for the alert fan-out (D10/D15/D32/D36).

`render_alert` and `fan_out` are exercised against a real in-memory Store and a
fake mailer (no AWS). `SesMailer` is checked against a fake boto3 client so the
content -> SES-call mapping is verified without touching AWS (LocalStack + a real
smoke are CI concerns, D15).
"""

import pytest
from botocore.exceptions import ClientError
from cryptography.fernet import Fernet

from salutebot.alerts import (
    EmailContent,
    MailerConfigError,
    MailerError,
    SesMailer,
    fan_out,
    render_alert,
)
from salutebot.crypto import Crypto
from salutebot.models import DetectionResult, Prestazione, Slot
from salutebot.store import Store

_CODE = "8901.20"
_PREST = Prestazione(code=_CODE, descrizione="VISITA UROLOGICA DI CONTROLLO", quantita=1)


def _slot(iso_date, time_, struttura="POLIAMBULATORIO MONGINEVRO"):
    return Slot(
        iso_date=iso_date, time=time_, struttura=struttura, cap="10141",
        prestazione_code=_CODE, prestazione_desc="VISITA UROLOGICA DI CONTROLLO",
        status="PRENOTABILE", doctor_unit="UROLOGIA", address="Via Monginevro 130, 10141",
    )


class _FakeMailer:
    """Records sends; can be told to fail for specific recipient addresses."""

    def __init__(self, fail_on: set[str] | None = None) -> None:
        self.sent: list[tuple[str, EmailContent]] = []
        self.__fail_on = fail_on or set()

    def send(self, to_addr: str, content: EmailContent) -> None:
        if to_addr in self.__fail_on:
            raise MailerError("boom")
        self.sent.append((to_addr, content))


@pytest.fixture
def store():
    crypto = Crypto(Fernet.generate_key().decode("ascii"), "hmac-secret")
    with Store(":memory:", crypto) as s:
        s.add_user("RSSMRA85T10A562S", "a@b.it")
        s.add_target("RSSMRA85T10A562S", _PREST, "1111111111111111")
        yield s


# ----- render_alert (D32) -----

def test_render_highlights_only_new_slots():
    old = _slot("2026-06-22", "16:00")
    new = _slot("2026-06-25", "08:00")
    result = DetectionResult(prestazione=_CODE, all_slots=[old, new], new_slots=[new])
    content = render_alert(result)
    # Both slots present (full current availability, D32) ...
    assert "2026-06-22" in content.text and "2026-06-25" in content.text
    # ... but only the new one is marked, in both text and HTML.
    assert ">>> 2026-06-25" in content.text
    assert ">>> 2026-06-22" not in content.text
    assert "NUOVO" in content.html
    assert content.html.count("NUOVO") == 1
    assert "1 nuovo posto" in content.subject


def test_render_subject_pluralizes():
    slots = [_slot("2026-06-25", "08:00"), _slot("2026-06-26", "09:00")]
    result = DetectionResult(prestazione=_CODE, all_slots=slots, new_slots=slots)
    assert "2 nuovi posti" in render_alert(result).subject


# ----- fan_out (D20/D36) -----

def test_fan_out_sends_to_all_active_subscribers_and_persists(store):
    store.add_user("BNCLGU80A01L219T", "c@d.it")
    store.add_target("BNCLGU80A01L219T", _PREST, "2222222222222222")
    new = _slot("2026-06-25", "08:00")
    result = DetectionResult(prestazione=_CODE, all_slots=[new], new_slots=[new])
    mailer = _FakeMailer()

    out = fan_out(store, mailer, result, now=1000.0)

    assert out.persisted is True
    assert out.sent == 2 and out.failed == ()
    assert {addr for addr, _ in mailer.sent} == {"a@b.it", "c@d.it"}
    assert store.known_slot_keys(_CODE) == {new.slot_key}  # recorded AFTER send (D36/D38)


def test_fan_out_persists_on_partial_delivery_and_abandons_the_failer(store):
    # D38: A delivers, B (c@d.it) permanently bounces -> persist anyway (A is never
    # re-alerted), B is abandoned into `failed`, not chased by re-alerting everyone.
    store.add_user("BNCLGU80A01L219T", "c@d.it")
    store.add_target("BNCLGU80A01L219T", _PREST, "2222222222222222")
    new = _slot("2026-06-25", "08:00")
    result = DetectionResult(prestazione=_CODE, all_slots=[new], new_slots=[new])
    mailer = _FakeMailer(fail_on={"c@d.it"})

    out = fan_out(store, mailer, result, now=1000.0, sleep=lambda _s: None)

    assert out.persisted is True                       # >=1 delivered -> recorded (D38)
    assert out.sent == 1 and out.failed == ("c@d.it",)  # A delivered, B abandoned
    assert store.known_slot_keys(_CODE) == {new.slot_key}  # so A is not re-alerted


def test_fan_out_total_failure_does_not_persist(store):
    # D38: every recipient fails (systemic outage) -> nothing persisted, so the
    # next sweep re-detects and retries the whole batch (D36 self-heal preserved).
    new = _slot("2026-06-25", "08:00")
    result = DetectionResult(prestazione=_CODE, all_slots=[new], new_slots=[new])
    mailer = _FakeMailer(fail_on={"a@b.it"})

    out = fan_out(store, mailer, result, now=1000.0, sleep=lambda _s: None)

    assert out.persisted is False
    assert out.sent == 0 and out.failed == ("a@b.it",)
    assert store.known_slot_keys(_CODE) == set()


def test_fan_out_retries_a_recipient_that_recovers(store):
    # D38: a recipient that fails once then succeeds is delivered within the same
    # fan-out (bounded retry + backoff), not abandoned.
    class _FlakyMailer:
        def __init__(self):
            self.attempts = 0
            self.delivered: list[str] = []

        def send(self, to_addr, content):
            self.attempts += 1
            if self.attempts == 1:
                raise MailerError("blip")
            self.delivered.append(to_addr)

    new = _slot("2026-06-25", "08:00")
    result = DetectionResult(prestazione=_CODE, all_slots=[new], new_slots=[new])
    mailer = _FlakyMailer()

    out = fan_out(store, mailer, result, now=1000.0, sleep=lambda _s: None)

    assert mailer.attempts == 2                 # failed once, retried, delivered
    assert out.persisted is True and out.failed == ()
    assert store.known_slot_keys(_CODE) == {new.slot_key}


def test_fan_out_no_new_slots_is_a_noop(store):
    slot = _slot("2026-06-22", "16:00")
    result = DetectionResult(prestazione=_CODE, all_slots=[slot], new_slots=[])
    mailer = _FakeMailer()

    out = fan_out(store, mailer, result, now=1000.0)

    assert out == out.__class__(recipients=0, sent=0, persisted=False)
    assert mailer.sent == []
    assert store.known_slot_keys(_CODE) == set()


def test_fan_out_only_emails_active_subscribers(store):
    store.add_user("BNCLGU80A01L219T", "c@d.it")
    store.add_target("BNCLGU80A01L219T", _PREST, "2222222222222222")
    store.deactivate_target("BNCLGU80A01L219T", _CODE)  # c opted out
    new = _slot("2026-06-25", "08:00")
    result = DetectionResult(prestazione=_CODE, all_slots=[new], new_slots=[new])
    mailer = _FakeMailer()

    fan_out(store, mailer, result, now=1000.0)

    assert {addr for addr, _ in mailer.sent} == {"a@b.it"}


# ----- SesMailer (D10/D15) -----

class _FakeSesClient:
    def __init__(self, raise_exc: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.__raise = raise_exc

    def send_email(self, **kwargs):
        if self.__raise is not None:
            raise self.__raise
        self.calls.append(kwargs)
        return {"MessageId": "fake-id"}


def test_ses_mailer_maps_content_to_send_email():
    client = _FakeSesClient()
    mailer = SesMailer("sender@verified.it", client)
    content = EmailContent(subject="S", text="T", html="<p>H</p>")

    mailer.send("to@x.it", content)

    (call,) = client.calls
    assert call["Source"] == "sender@verified.it"
    assert call["Destination"] == {"ToAddresses": ["to@x.it"]}
    assert call["Message"]["Subject"]["Data"] == "S"
    assert call["Message"]["Body"]["Text"]["Data"] == "T"
    assert call["Message"]["Body"]["Html"]["Data"] == "<p>H</p>"


def test_ses_mailer_wraps_client_error():
    err = ClientError({"Error": {"Code": "MessageRejected", "Message": "x"}}, "SendEmail")
    mailer = SesMailer("sender@verified.it", _FakeSesClient(raise_exc=err))
    with pytest.raises(MailerError):
        mailer.send("to@x.it", EmailContent(subject="S", text="T", html="H"))


def test_ses_mailer_from_env_requires_sender():
    with pytest.raises(MailerConfigError):
        SesMailer.from_env(env={})
