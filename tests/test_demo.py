"""Tests for the walking-skeleton demo (Phase 5 deadline insurance).

The demo wires the real detector/store/fan-out to the fixture scraper. These
assert the observable end-to-end behaviour — the alert path fires the right
emails and the D28 rotation path fires — so the demo can't silently rot.
"""

from salutebot.demo import ConsoleMailer, run_demo


class _CountingMailer:
    """A `Mailer` that records every (recipient, subject) it is handed."""

    def __init__(self) -> None:
        self.sends: list[tuple[str, str]] = []

    def send(self, to_addr, content) -> None:
        self.sends.append((to_addr, content.subject))


def test_console_mailer_prints_body_once_per_subject():
    lines: list[str] = []
    mailer = ConsoleMailer(write=lines.append)

    class _C:
        subject = "s"
        text = "line-a\nline-b"
        html = ""

    mailer.send("a@x.com", _C())
    mailer.send("b@x.com", _C())  # same subject → body not repeated
    assert mailer.sent == 2
    assert sum("line-a" in line for line in lines) == 1  # body printed exactly once


def test_run_demo_is_quiet_and_completes():
    # Runs the whole skeleton with a no-op writer: it must not raise and must send.
    mailer = _CountingMailer()
    run_demo(mailer=mailer, write=lambda *_: None)
    # Scene 1: baseline (2 recipients) + new-slot (2 recipients) = 4 alert emails.
    subjects = [s for _, s in mailer.sends]
    assert sum("nuovi posti" in s or "nuovo posto" in s for s in subjects) == 4


def test_run_demo_default_console_mailer_writes_output():
    lines: list[str] = []
    run_demo(write=lines.append)
    text = "\n".join(lines)
    assert "Scene 1" in text
    assert "Scene 2" in text
    assert "non è più valida" in text  # D28 rotation notice reached the output
