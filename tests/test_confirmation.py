"""Ground-truth tests for the ricetta-confirmation (epPrestazioni) parser.

Anchored to the real recon capture (`recon/epPrestazioni_redacted.xhtml`): a
live confirmation page for an actual ricetta. Real captured markup, not a mock,
so the assertions stay trustworthy as the app is built around the parser.
"""

from pathlib import Path

from salutebot.models import Prestazione
from salutebot.scraper.confirmation import parse_prestazione_confirmation

FIXTURE = (
    Path(__file__).resolve().parent.parent / "recon" / "epPrestazioni_redacted.xhtml"
)


def _parse():
    return parse_prestazione_confirmation(FIXTURE.read_text(encoding="utf-8"))


def test_returns_a_prestazione():
    assert isinstance(_parse(), Prestazione)


def test_parses_code():
    assert _parse().code == "8901.20"


def test_parses_descrizione():
    assert _parse().descrizione == "VISITA UROLOGICA DI CONTROLLO"


def test_parses_quantita_as_int():
    # Label is "Quantità" (entity &#224;) — exercises entity decoding + int parse.
    assert _parse().quantita == 1


def test_missing_prestazione_row_returns_none():
    assert parse_prestazione_confirmation("<html><body>no rows here</body></html>") is None


def test_empty_markup_returns_none():
    assert parse_prestazione_confirmation("") is None
