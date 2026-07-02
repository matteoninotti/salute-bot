"""Environment-sourced configuration for the secrets-at-rest keys.

Two separate keys, never one reused across primitives (D29 addendum):
    - SALUTEBOT_ENC_KEY  -- AEAD key for `cf_enc` / `nre_enc` (D3, D29).
    - SALUTEBOT_HMAC_KEY -- HMAC key for the `cf_hash` blind index (D29).

Both are read from the environment only -- never committed, never in the DB,
never logged (CLAUDE.md). This module only checks *presence* (and that the two
keys differ); the exact key format/length is a Phase 2 concern, pinned once
the crypto library call (AEAD primitive, HMAC construction) is chosen.
"""

import os


class MissingEnvKeyError(RuntimeError):
    """Raised when a required secret-bearing env var is absent or empty."""


class DuplicateEnvKeyError(RuntimeError):
    """Raised when the AEAD and HMAC keys are identical (violates D29 addendum)."""


class EnvConfig:
    """Loads and holds the two encryption keys.

    Attributes are private (encapsulation guardrail): the keys are only reachable
    through read-only properties, never as bare public attributes.
    """

    __ENC_KEY_VAR = "SALUTEBOT_ENC_KEY"
    __HMAC_KEY_VAR = "SALUTEBOT_HMAC_KEY"

    def __init__(self, env: dict[str, str] | None = None) -> None:
        """Load both keys from `env` (defaults to `os.environ`).

        The `env` parameter exists so tests can pass a hermetic dict instead of
        mutating the real process environment.
        """
        source = env if env is not None else os.environ
        self.__enc_key = self.__require(source, self.__ENC_KEY_VAR)
        self.__hmac_key = self.__require(source, self.__HMAC_KEY_VAR)
        if self.__enc_key == self.__hmac_key:
            raise DuplicateEnvKeyError(
                f"{self.__ENC_KEY_VAR} and {self.__HMAC_KEY_VAR} must be distinct "
                "secrets (D29 addendum) -- reusing one key across AEAD and HMAC "
                "is not permitted."
            )

    @staticmethod
    def __require(source: dict[str, str], var_name: str) -> str:
        value = source.get(var_name)
        if not value:
            raise MissingEnvKeyError(
                f"{var_name} is not set. Secrets are encrypted at rest with a key "
                "from the environment only (never committed, never in the DB) -- "
                f"set {var_name} before running salute-bot."
            )
        return value

    @property
    def enc_key(self) -> str:
        return self.__enc_key

    @property
    def hmac_key(self) -> str:
        return self.__hmac_key
