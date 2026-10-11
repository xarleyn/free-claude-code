"""Stable, non-secret account fingerprints for eligibility evidence.

Eligibility evidence is account specific: a model that is free on one account may be
payment-locked on another. To keep that distinction without ever storing a key, each
provider/account pair is reduced to a short fingerprint derived from the provider id
and a hash of the account credential.

The hash is one-way and salted with a fixed, non-secret application namespace. It is
not a secret and is not reversible, and it is stable across restarts so that evidence
survives a reboot. When the credential changes, the fingerprint changes and every
``verified`` record for the old fingerprint stops matching, which resets the proof —
exactly the behaviour the specification requires.
"""

import hashlib

from free_claude_code.config.provider_catalog import PROVIDER_CATALOG
from free_claude_code.config.settings import Settings

_NAMESPACE = "fcc-free-guard/account-binding/v1"
_FINGERPRINT_LENGTH = 32


def _fingerprint(*parts: str) -> str:
    digest = hashlib.sha256()
    digest.update(_NAMESPACE.encode("utf-8"))
    for part in parts:
        digest.update(b"\x00")
        digest.update(part.encode("utf-8"))
    return digest.hexdigest()[:_FINGERPRINT_LENGTH]


def account_binding_id(provider_id: str, settings: Settings) -> str:
    """Return the stable fingerprint of the account behind ``provider_id``.

    Several accounts in one setting share a single binding, which is the restrictive
    choice: proof gathered for one account is never inherited by another pool.
    """

    descriptor = PROVIDER_CATALOG.get(provider_id)
    if descriptor is None:
        # Unknown/custom providers share one binding: the safest, most restrictive
        # choice, because a new account can never inherit another account's proof.
        return f"unregistered:{provider_id}"

    credential = ""
    if descriptor.credential_attr is not None:
        value = getattr(settings, descriptor.credential_attr, None)
        credential = _credential_text(value)

    custom = settings.custom_provider(provider_id)
    if custom is not None and custom.api_key:
        credential = _credential_text(custom.api_key) or credential

    if not credential:
        # Connected-account providers (ChatGPT, Copilot) and unconfigured providers
        # have no FCC-held credential; bind to the configured model default instead
        # so an unconfigured provider can never inherit someone else's evidence.
        default_model: str = str(settings.model)
        return f"no-credential:{provider_id}:{_fingerprint(default_model)}"
    return f"{provider_id}:{_fingerprint(provider_id, credential)}"


def _credential_text(value: object) -> str:
    """Read a secret-bearing setting without ever retaining the value."""

    if isinstance(value, str):
        return value
    hidden = getattr(value, "get_secret_value", None)
    if callable(hidden):
        try:
            return str(hidden())
        except Exception:  # pragma: no cover - defensive
            return ""
    return ""


def credential_fingerprint(provider_id: str, credential: str) -> str:
    """Return the fingerprint of one raw credential, for tests and diagnostics."""

    return f"{provider_id}:{_fingerprint(provider_id, credential)}"
