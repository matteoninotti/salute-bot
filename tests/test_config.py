"""Tests for the env-sourced secrets config (Phase 0 bootstrap).

Uses a plain dict as the env source (never the real process environment) so
these tests stay hermetic and never touch actual secrets.
"""

import pytest

from salutebot.config import (
    DuplicateEnvKeyError,
    EnvConfig,
    MissingEnvKeyError,
)

VALID_ENV = {
    "SALUTEBOT_ENC_KEY": "test-enc-key-placeholder",
    "SALUTEBOT_HMAC_KEY": "test-hmac-key-placeholder",
}


def test_loads_both_keys():
    config = EnvConfig(env=VALID_ENV)
    assert config.enc_key == "test-enc-key-placeholder"
    assert config.hmac_key == "test-hmac-key-placeholder"


def test_missing_enc_key_raises():
    env = {"SALUTEBOT_HMAC_KEY": "test-hmac-key-placeholder"}
    with pytest.raises(MissingEnvKeyError, match="SALUTEBOT_ENC_KEY"):
        EnvConfig(env=env)


def test_missing_hmac_key_raises():
    env = {"SALUTEBOT_ENC_KEY": "test-enc-key-placeholder"}
    with pytest.raises(MissingEnvKeyError, match="SALUTEBOT_HMAC_KEY"):
        EnvConfig(env=env)


def test_empty_string_treated_as_missing():
    env = {"SALUTEBOT_ENC_KEY": "", "SALUTEBOT_HMAC_KEY": "test-hmac-key-placeholder"}
    with pytest.raises(MissingEnvKeyError, match="SALUTEBOT_ENC_KEY"):
        EnvConfig(env=env)


def test_identical_keys_rejected():
    env = {"SALUTEBOT_ENC_KEY": "same-value", "SALUTEBOT_HMAC_KEY": "same-value"}
    with pytest.raises(DuplicateEnvKeyError):
        EnvConfig(env=env)


def test_keys_are_not_public_attributes():
    config = EnvConfig(env=VALID_ENV)
    assert not hasattr(config, "enc_key_")  # sanity: no stray attribute
    assert "_EnvConfig__enc_key" in vars(config)  # name-mangled, private storage
