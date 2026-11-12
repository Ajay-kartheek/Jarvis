from __future__ import annotations

from pathlib import Path

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError


class FakeKeyring(KeyringBackend):
    """In-memory stand-in for the macOS Keychain."""

    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self.items: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, username: str, password: str) -> None:
        self.items[(service, username)] = password

    def get_password(self, service: str, username: str) -> str | None:
        return self.items.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        try:
            del self.items[(service, username)]
        except KeyError:
            raise PasswordDeleteError("not found") from None


@pytest.fixture
def fake_keyring():
    previous = keyring.get_keyring()
    fake = FakeKeyring()
    keyring.set_keyring(fake)
    try:
        yield fake
    finally:
        keyring.set_keyring(previous)


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``Path.home()`` at a temp folder so tests never touch real paths."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def example_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests read the example settings, never the user's own config.toml."""
    example = Path(__file__).resolve().parent.parent / "config.example.toml"
    monkeypatch.setenv("JARVIS_CONFIG", str(example))
