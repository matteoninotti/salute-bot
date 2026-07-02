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
        raise ValueError("CF format invalid (expected a 16-character Italian Codice Fiscale).")
    return normalized


# --- NRE ---------------------------------------------------------------------
# UNVERIFIED FORMAT: the search-form page (with the nreInput0 field's real
# maxlength/pattern) was never captured in recon -- only the confirmation and
# slots pages were (see salute-bot-log.md SS3, "NRE box count"). This check is
# deliberately loose -- numeric, plausible length -- and only rejects obvious
# non-candidates. Tighten once the exact format is confirmed against a real
# ricetta (do not paste the value; confirm digit count/charset only).
_NRE_RE = re.compile(r"^[0-9]{6,20}$")


def validate_nre(nre: str) -> str:
    """Normalize (strip) and loosely validate an NRE.

    Raises ValueError if clearly malformed (empty, non-numeric, implausible
    length). This is NOT the confirmed spec -- see the module docstring.
    """
    normalized = nre.strip()
    if not _NRE_RE.match(normalized):
        raise ValueError("NRE format invalid (expected a numeric code).")
    return normalized
