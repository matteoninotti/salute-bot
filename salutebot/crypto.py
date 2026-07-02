"""Secrets-at-rest primitives: AEAD encryption + HMAC blind index (D3, D29).

Two independent operations on a secret, backed by the two SEPARATE env keys
(D29 addendum) that `EnvConfig` loads:

  - **encrypt / decrypt** (AEAD via Fernet) — confidentiality for `cf_enc` and
    `nre_enc`. Non-deterministic: every call embeds a fresh IV + timestamp, so
    the same plaintext yields different ciphertext — which is exactly why it
    cannot double as a lookup key.
  - **hash_cf** (HMAC-SHA256) — a deterministic, irreversible **blind index**.
    This is the `users` PK and the value `targets.user` references (D29), so a
    CF can be found by equality/join without decrypting every row. The keyed
    HMAC (not a plain SHA-256) blocks offline brute-forcing of the enumerable
    CF space.

Nothing here reads config or the environment directly — keys are injected — and
nothing logs a plaintext secret.
"""

import hashlib
import hmac

from cryptography.fernet import Fernet

from salutebot.config import EnvConfig


class Crypto:
    """Encrypt/decrypt secrets and compute the CF blind index.

    Construct with the two key strings (or `Crypto.from_env(config)`). Keys are
    held privately (encapsulation guardrail); there is no accessor that returns
    them.
    """

    def __init__(self, enc_key: str, hmac_key: str) -> None:
        # Fernet validates the key format here — a malformed ENC key fails fast
        # at construction (raises ValueError) rather than at first encrypt.
        self.__fernet = Fernet(enc_key.encode("utf-8"))
        self.__hmac_key = hmac_key.encode("utf-8")

    @classmethod
    def from_env(cls, config: EnvConfig) -> "Crypto":
        return cls(config.enc_key, config.hmac_key)

    def hash_cf(self, cf: str) -> str:
        """Deterministic HMAC-SHA256 blind index (hex) — the `users` PK / `targets` FK."""
        return hmac.new(self.__hmac_key, cf.encode("utf-8"), hashlib.sha256).hexdigest()

    def encrypt(self, plaintext: str) -> str:
        """AEAD-encrypt a secret (CF/NRE) for storage. Non-deterministic; returns an
        ASCII Fernet token safe to store as TEXT."""
        return self.__fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        """Reverse `encrypt`. Raises `cryptography.fernet.InvalidToken` on a tampered
        token or wrong key."""
        return self.__fernet.decrypt(token.encode("ascii")).decode("utf-8")
