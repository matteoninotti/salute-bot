"""Parse the CUP ricetta-confirmation page (epPrestazioni) into a `Prestazione`.

Shown during registration / adding a prestazione (D14): the user confirms the
service their NRE unlocks before it is watched. Pure and deterministic — tested
offline against the real recon capture (`recon/epPrestazioni_redacted.xhtml`).

Unlike the slots page, this is a full HTML document (not a JSF partial-response),
so there is no CDATA to unwrap. Fields are read by their **visible label text**,
never by element id: the `…:epPrestazioniForm:_t167` ids are positional and
renumber per render, so keying on them would be fragile.

The page shows the prestazione as label/value pairs inside one `.prestazioneRow`:
    "Prestazione Da Erogare" -> 8901.20        (the code)
    "Descrizione Regionale"  -> VISITA ...     (the descrizione)
    "Quantità"               -> 1
A ricetta in every sample we hold carries a single prestazione (D19), so this
returns one `Prestazione`; a multi-prestazione ricetta would be a model change,
not just a parser change.
"""

import re

from bs4 import BeautifulSoup

from salutebot.models import Prestazione

_LABEL_CODE = "prestazione da erogare"
_LABEL_DESC = "descrizione regionale"
_LABEL_QTY = "quantità"


def parse_prestazione_confirmation(markup: str) -> Prestazione | None:
    """Parse the confirmation page into a `Prestazione`, or None if absent.

    Returns None when the `.prestazioneRow` block is missing or carries neither
    a code nor a descrizione (e.g. an error/redirect page reached the parser).
    """
    soup = BeautifulSoup(markup, "html.parser")
    row = soup.select_one(".prestazioneRow")
    if row is None:
        return None

    fields = _label_value_map(row)
    code = fields.get(_LABEL_CODE, "")
    descrizione = fields.get(_LABEL_DESC, "")
    if not code and not descrizione:
        return None

    return Prestazione(
        code=code,
        descrizione=descrizione,
        quantita=_parse_quantita(fields.get(_LABEL_QTY)),
    )


def _label_value_map(row) -> dict[str, str]:
    """Pair each `.label-small` with the `.infoValue` that follows it.

    Both appear once per field in document order, so zipping the two ordered
    lists aligns label -> value. Labels are normalized (whitespace-collapsed,
    lower-cased, trailing colon dropped) for stable matching.
    """
    labels = [_norm_label(s) for s in row.select(".label-small")]
    values = [_text(s) for s in row.select(".infoValue")]
    return dict(zip(labels, values))


def _text(el) -> str:
    return el.get_text(" ", strip=True)


def _norm_label(el) -> str:
    return " ".join(_text(el).split()).lower().rstrip(":").strip()


def _parse_quantita(raw: str | None) -> int | None:
    """First integer in the quantità value ("1" -> 1); None if absent/parseless."""
    if not raw:
        return None
    match = re.search(r"\d+", raw)
    return int(match.group()) if match else None
