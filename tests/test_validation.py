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


def test_validate_nre_accepts_a_15char_alphanumeric_code():
    assert validate_nre("010A31234567890") == "010A31234567890"


def test_validate_nre_uppercases_and_drops_whitespace():
    # The ricetta prints the 5-char prefix and 10-digit body spaced + lower-case.
    assert validate_nre("  010a3 1234567890  ") == "010A31234567890"


@pytest.mark.parametrize(
    "bad_nre",
    [
        "",
        "1234567890",         # only the 10-digit body (missing the 5-char prefix)
        "010A3123456789",     # 14 chars (too short)
        "010A312345678901",   # 16 chars (too long)
        "010@31234567890",    # illegal character
    ],
)
def test_validate_nre_rejects_malformed(bad_nre):
    with pytest.raises(ValueError):
        validate_nre(bad_nre)
