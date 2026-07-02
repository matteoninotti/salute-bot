"""Tests for the secrets-at-rest primitives (D3, D29).

Keys are generated per-test (a real Fernet key + a distinct HMAC secret); no
real CF/NRE and no real env keys ever appear here.
"""

import pytest
from cryptography.fernet import Fernet, InvalidToken

from salutebot.config import EnvConfig
from salutebot.crypto import Crypto

# A syntactically valid Fernet key and a distinct HMAC secret, fixed so the
# blind-index assertions are reproducible.
_ENC_KEY = Fernet.generate_key().decode("ascii")
_HMAC_KEY = "hmac-secret-distinct-from-enc-key"

# A structurally plausible but fake CF (never a real one).
_FAKE_CF = "RSSMRA85T10A562S"


@pytest.fixture
def crypto():
    return Crypto(_ENC_KEY, _HMAC_KEY)


def test_encrypt_decrypt_roundtrip(crypto):
    assert crypto.decrypt(crypto.encrypt(_FAKE_CF)) == _FAKE_CF


def test_encrypt_is_non_deterministic(crypto):
    # Fresh IV each call -> same plaintext, different ciphertext (can't be a key).
    assert crypto.encrypt(_FAKE_CF) != crypto.encrypt(_FAKE_CF)


def test_hash_cf_is_deterministic(crypto):
    assert crypto.hash_cf(_FAKE_CF) == crypto.hash_cf(_FAKE_CF)


def test_hash_cf_is_sha256_hex(crypto):
    digest = crypto.hash_cf(_FAKE_CF)
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_hash_cf_differs_by_input(crypto):
    assert crypto.hash_cf(_FAKE_CF) != crypto.hash_cf("BNCLNZ90A01F205X")


def test_hash_cf_depends_on_key():
    # Same CF, different HMAC key -> different blind index (keyed, not a plain hash).
    a = Crypto(_ENC_KEY, "key-one")
    b = Crypto(_ENC_KEY, "key-two")
    assert a.hash_cf(_FAKE_CF) != b.hash_cf(_FAKE_CF)


def test_decrypt_with_wrong_key_raises(crypto):
    other = Crypto(Fernet.generate_key().decode("ascii"), _HMAC_KEY)
    token = crypto.encrypt(_FAKE_CF)
    with pytest.raises(InvalidToken):
        other.decrypt(token)


def test_malformed_enc_key_fails_fast():
    with pytest.raises(ValueError):
        Crypto("not-a-valid-fernet-key", _HMAC_KEY)


def test_from_env_uses_config_keys():
    config = EnvConfig({"SALUTEBOT_ENC_KEY": _ENC_KEY, "SALUTEBOT_HMAC_KEY": _HMAC_KEY})
    crypto = Crypto.from_env(config)
    assert crypto.decrypt(crypto.encrypt(_FAKE_CF)) == _FAKE_CF
    assert crypto.hash_cf(_FAKE_CF) == Crypto(_ENC_KEY, _HMAC_KEY).hash_cf(_FAKE_CF)
