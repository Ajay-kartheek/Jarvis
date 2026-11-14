from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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
def no_cloud_voice(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never read the user's real ElevenLabs key, so no test speaks through the network by accident."""
    from jarvis import eleven

    monkeypatch.setattr(eleven, "_key", lambda: None)
    monkeypatch.setattr(eleven, "_client", None)
    monkeypatch.setattr(eleven, "_resting_until", 0.0)
    monkeypatch.setattr(eleven, "_credits", None)


@pytest.fixture(autouse=True)
def example_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests read the example settings, never the user's own config.toml."""
    example = Path(__file__).resolve().parent.parent / "config.example.toml"
    monkeypatch.setenv("JARVIS_CONFIG", str(example))


@pytest.fixture
def world(fake_home):
    """A home folder with a task folder in it, another project folder, and the places that must stay private."""
    home = fake_home.resolve()
    folder = home / "Documents" / "Jarvis Work" / "primes"
    folder.mkdir(parents=True)
    (folder / "notes.md").write_text("notes")
    (folder / "primes.py").write_text("print(2)")
    (folder / ".env").write_text("KEY=1")
    (folder / ".env.example").write_text("KEY=")
    (folder / "password_reset.py").write_text("def reset(): ...")
    (home / ".ssh").mkdir()
    (home / ".ssh" / "config").write_text("Host x")
    (home / "Documents" / "Atlas").mkdir()
    (home / "Documents" / "Atlas" / "plan.md").write_text("plan")
    (folder / "ssh-link").symlink_to(home / ".ssh")
    return SimpleNamespace(home=home, folder=folder, atlas=home / "Documents" / "Atlas")
