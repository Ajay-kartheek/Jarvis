"""Thin wrapper over the macOS Keychain, reached through ``keyring``."""

from __future__ import annotations

import keyring
from keyring.errors import PasswordDeleteError

GOOGLE_SERVICE = "jarvis.google"
KEY_SERVICES = {"elevenlabs": "jarvis.elevenlabs"}  # other services' API keys, kept under the user name "api-key"
KEY_USER = "api-key"


def load_key(service: str) -> str | None:
    """The API key stored for ``service`` (a name in KEY_SERVICES), or None."""
    return load_secret(KEY_SERVICES[service], KEY_USER)


def save_secret(service: str, username: str, secret: str) -> None:
    keyring.set_password(service, username, secret)


def load_secret(service: str, username: str) -> str | None:
    return keyring.get_password(service, username)


def delete_secret(service: str, username: str) -> bool:
    """Remove a secret. Returns False when there was nothing to remove."""
    try:
        keyring.delete_password(service, username)
    except PasswordDeleteError:
        return False
    return True
