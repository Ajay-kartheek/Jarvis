from __future__ import annotations

from jarvis import keychain


def test_round_trip(fake_keyring):
    keychain.save_secret("jarvis.test", "user@example.com", "s3cret")

    assert keychain.load_secret("jarvis.test", "user@example.com") == "s3cret"
    assert fake_keyring.items == {("jarvis.test", "user@example.com"): "s3cret"}


def test_missing_secret_is_none(fake_keyring):
    assert keychain.load_secret("jarvis.test", "nobody@example.com") is None


def test_save_overwrites(fake_keyring):
    keychain.save_secret("jarvis.test", "u", "one")
    keychain.save_secret("jarvis.test", "u", "two")

    assert keychain.load_secret("jarvis.test", "u") == "two"


def test_delete(fake_keyring):
    keychain.save_secret("jarvis.test", "u", "one")

    assert keychain.delete_secret("jarvis.test", "u") is True
    assert keychain.load_secret("jarvis.test", "u") is None
    assert keychain.delete_secret("jarvis.test", "u") is False


def test_google_service_name():
    assert keychain.GOOGLE_SERVICE == "jarvis.google"
