"""Thin wrapper over the macOS Keychain, reached through ``keyring``."""

from __future__ import annotations

import keyring
from keyring.errors import PasswordDeleteError

GOOGLE_SERVICE = "jarvis.google"


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
