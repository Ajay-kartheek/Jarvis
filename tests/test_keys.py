"""API keys of other services: pasted into a macOS dialog, kept in the Keychain, never shown."""

import subprocess
from types import SimpleNamespace

from typer.testing import CliRunner

from jarvis import keychain
from jarvis.cli import app


def test_a_key_pasted_into_the_dialog_is_kept_in_the_keychain_and_not_shown(fake_keyring, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="sk_test_123456\n", stderr=""))
    result = CliRunner().invoke(app, ["key", "elevenlabs"])
    assert result.exit_code == 0 and "sk_test" not in result.output
    assert keychain.load_key("elevenlabs") == "sk_test_123456"


def test_a_cancelled_dialog_keeps_nothing(fake_keyring, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="User canceled."))
    result = CliRunner().invoke(app, ["key", "elevenlabs"])
    assert result.exit_code == 1 and keychain.load_key("elevenlabs") is None


def test_only_known_services_have_keys(fake_keyring):
    assert CliRunner().invoke(app, ["key", "someone"]).exit_code == 2
