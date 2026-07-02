"""Tests for CF/NRE format validation.

CF test values are the standard synthetic placeholder used throughout Italian
software documentation/examples (not a real person's data), plus deliberately
malformed variants.

NRE test values are arbitrary numeric placeholders -- the format itself is
unverified (see salutebot/validation.py docstring), so these tests only pin
the *loose* rules actually implemented, not a confirmed spec.
"""

import pytest

from salutebot.validation import validate_cf, validate_nre

# Canonical placeholder CF (Mario Rossi), ubiquitous in Italian dev examples --
# not a real, identifiable individual's data.
_PLACEHOLDER_CF = "RSSMRA85M01H501U"


def test_validate_cf_accepts_well_formed():
    assert validate_cf(_PLACEHOLDER_CF) == _PLACEHOLDER_CF


def test_validate_cf_normalizes_case_and_whitespace():
    assert validate_cf(f"  {_PLACEHOLDER_CF.lower()}  ") == _PLACEHOLDER_CF


@pytest.mark.parametrize(
    "bad_cf",
    [
        "",
        "RSSMRA85M01H501",  # 15 chars, one short
        "RSSMRA85M01H501UU",  # 17 chars, one long
        "123456789012345U",  # digits where surname/name letters must be
        "RSSMRA85Z01H501U",  # 'Z' is not a valid month letter
        "RSSMRA85M01H5011",  # last char must be a letter, not a digit
    ],
)
def test_validate_cf_rejects_malformed(bad_cf):
    with pytest.raises(ValueError):
        validate_cf(bad_cf)


def test_validate_nre_accepts_plausible_numeric_code():
    assert validate_nre("123456789012345") == "123456789012345"


def test_validate_nre_strips_whitespace():
    assert validate_nre("  123456  ") == "123456"


@pytest.mark.parametrize(
    "bad_nre",
    [
        "",
        "12345",  # below the 6-char floor
        "123456789012345678901",  # above the 20-char ceiling
        "12345A",  # non-numeric
        "12 345",  # embedded whitespace
    ],
)
def test_validate_nre_rejects_malformed(bad_nre):
    with pytest.raises(ValueError):
        validate_nre(bad_nre)
