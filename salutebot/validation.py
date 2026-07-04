"""CF / NRE format validation.

Neither function ever logs or echoes the value it rejects (CLAUDE.md: secrets
never in chat, code, or logs) -- error messages name the *rule* violated, not
the input.
"""

import re

# --- Codice Fiscale ---------------------------------------------------------
# Structural check only (character classes + length), NOT the check-digit
# algorithm -- out of scope for Phase 0 (see TODO.md); this catches the vast
# majority of malformed input without the full omocodia/checksum tables.
#
# Public Italian national standard (Agenzia delle Entrate), not specific to
# this project's target site: 16 chars = 6 surname/name letters + 2
# birth-year + 1 month letter + 2 day/sex + 1 birthplace letter + 3
# birthplace-code + 1 check letter. The digit positions may instead hold an
# "omocodia" substitute letter (LMNPQRSTU standing in for 0-9) when the
# numeric CF collides with an existing one.
_OMOCODIA_DIGIT = "0-9LMNPQRSTU"
_MONTH_LETTERS = "ABCDEHLMPRST"
_CF_RE = re.compile(
    rf"^[A-Z]{{6}}"
    rf"[{_OMOCODIA_DIGIT}]{{2}}"
    rf"[{_MONTH_LETTERS}]"
    rf"[{_OMOCODIA_DIGIT}]{{2}}"
    rf"[A-Z]"
    rf"[{_OMOCODIA_DIGIT}]{{3}}"
    rf"[A-Z]$"
)


def validate_cf(cf: str) -> str:
    """Normalize (strip + uppercase) and structurally validate a Codice Fiscale.

    Raises ValueError if the 16-character shape doesn't match the national
    standard's character classes. Does not verify the check-digit.
    """
    normalized = cf.strip().upper()
    if not _CF_RE.match(normalized):
        raise ValueError("Formato CF non valido (atteso un Codice Fiscale italiano di 16 caratteri).")
    return normalized


# --- NRE ---------------------------------------------------------------------
# CONFIRMED 2026-07-04 (live smoke run, D42): the CUP form's single NRE box wants
# the WHOLE 15-character code -- a 5-character alphanumeric prefix + a 10-digit
# number (e.g. "010A3" + ten digits). Entering only the 10-digit part is rejected
# by the site ("Numero ricetta elettronica non valido"). Whitespace is dropped (the
# ricetta prints the two parts spaced) and the value is upper-cased (the form applies
# text-transform: uppercase). Length/charset kept slightly lenient over the exact
# 5+10 split until confirmed general across ricette.
_NRE_RE = re.compile(r"^[0-9A-Z]{15}$")


def validate_nre(nre: str) -> str:
    """Normalize (strip whitespace, upper-case) and validate a 15-char NRE.

    Raises ValueError if it isn't 15 alphanumeric characters. The value is never
    logged or echoed (only the rule is named).
    """
    normalized = "".join(nre.split()).upper()
    if not _NRE_RE.match(normalized):
        raise ValueError(
            "Formato NRE non valido (atteso il codice ricetta completo di 15 caratteri "
            "— un prefisso di 5 caratteri seguito da 10 cifre)."
        )
    return normalized
