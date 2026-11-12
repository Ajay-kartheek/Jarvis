from __future__ import annotations

import json
import os
import stat

import pytest
from google.oauth2.credentials import Credentials

from jarvis import google_auth
from jarvis.google_auth import SCOPES, AuthError


def _client_json(kind: str = "installed", **overrides) -> bytes:
    body = {
        "client_id": "123.apps.googleusercontent.com",
        "client_secret": "not-a-real-secret",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
    body.update(overrides)
    return json.dumps({kind: body}).encode()


def _creds(granted: list[str] | None = None, refresh_token: str | None = "r-token") -> Credentials:
    return Credentials(
        token="a-token",
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id="123.apps.googleusercontent.com",
        client_secret="not-a-real-secret",
        scopes=list(SCOPES),
        granted_scopes=list(SCOPES) if granted is None else granted,
    )


def test_scopes_are_exactly_the_four_agreed():
    assert list(SCOPES) == [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
    ]


# --- client file ---------------------------------------------------------


def test_install_client_file_copies_it_with_private_permissions(fake_home, tmp_path):
    source = tmp_path / "Downloads" / "client_secret_123.json"
    source.parent.mkdir()
    source.write_bytes(_client_json())

    target = google_auth.install_client_file(source)

    assert target == fake_home / ".config" / "jarvis" / "google_client.json"
    assert target.read_bytes() == source.read_bytes()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700


def test_install_client_file_tightens_an_existing_loose_folder(fake_home, tmp_path):
    loose = fake_home / ".config" / "jarvis"
    loose.mkdir(parents=True, mode=0o755)
    os.chmod(loose, 0o755)
    source = tmp_path / "client.json"
    source.write_bytes(_client_json())

    google_auth.install_client_file(source)

    assert stat.S_IMODE(loose.stat().st_mode) == 0o700


@pytest.mark.parametrize(
    "raw, message",
    [
        (_client_json("web"), "Desktop app"),
        (json.dumps({"type": "service_account", "private_key": "x"}).encode(), "service-account"),
        (b"not json", "not valid JSON"),
        (b"[]", "not an OAuth client"),
        (b"{}", "Desktop app"),
        (_client_json(client_secret=""), "missing: client_secret"),
    ],
)
def test_wrong_client_files_are_rejected(fake_home, tmp_path, raw, message):
    source = tmp_path / "client.json"
    source.write_bytes(raw)

    with pytest.raises(AuthError, match=message):
        google_auth.install_client_file(source)

    assert not (fake_home / ".config" / "jarvis" / "google_client.json").exists()


def test_unreadable_client_file(fake_home, tmp_path):
    with pytest.raises(AuthError, match="Cannot read"):
        google_auth.install_client_file(tmp_path / "missing.json")


# --- consent flow --------------------------------------------------------


class _FakeFlow:
    calls: dict = {}
    result: object = "creds"
    error: Exception | None = None

    @classmethod
    def from_client_secrets_file(cls, path, scopes):
        cls.calls = {"path": path, "scopes": scopes}
        return cls()

    def run_local_server(self, **kwargs):
        type(self).calls["kwargs"] = kwargs
        type(self).calls["relaxed"] = os.environ.get("OAUTHLIB_RELAX_TOKEN_SCOPE")
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def fake_flow(fake_home, monkeypatch):
    client = fake_home / ".config" / "jarvis" / "google_client.json"
    client.parent.mkdir(parents=True)
    client.write_bytes(_client_json())
    monkeypatch.setattr(google_auth, "InstalledAppFlow", _FakeFlow)
    monkeypatch.delenv("OAUTHLIB_RELAX_TOKEN_SCOPE", raising=False)
    _FakeFlow.calls, _FakeFlow.error = {}, None
    return _FakeFlow


def test_consent_flow_asks_for_offline_access_and_the_right_account(fake_flow, fake_home):
    result = google_auth.run_consent_flow("user@example.com", timeout_seconds=42)

    assert result == "creds"
    assert fake_flow.calls["scopes"] == list(SCOPES)
    assert fake_flow.calls["path"] == str(
        fake_home / ".config" / "jarvis" / "google_client.json"
    )
    kwargs = fake_flow.calls["kwargs"]
    assert kwargs["access_type"] == "offline"
    assert kwargs["prompt"] == "consent"
    assert kwargs["login_hint"] == "user@example.com"
    assert kwargs["timeout_seconds"] == 42


def test_consent_flow_relaxes_the_scope_check_only_while_running(fake_flow):
    google_auth.run_consent_flow("user@example.com")

    assert fake_flow.calls["relaxed"] == "1"
    assert "OAUTHLIB_RELAX_TOKEN_SCOPE" not in os.environ


def test_consent_timeout_points_at_an_admin_block(fake_flow):
    fake_flow.error = google_auth.WSGITimeoutError("timed out")

    with pytest.raises(AuthError, match="Workspace admin is blocking"):
        google_auth.run_consent_flow("user@example.com", timeout_seconds=1)


def test_consent_flow_needs_a_client_file(fake_home):
    with pytest.raises(AuthError, match="No Google client file"):
        google_auth.run_consent_flow("user@example.com")


# --- add_account ---------------------------------------------------------


def test_add_account_stores_credentials_when_the_address_matches(fake_keyring):
    actual = google_auth.add_account(
        "User@Example.com",
        consent=lambda email: _creds(),
        whoami=lambda creds: "user@example.com",
    )

    assert actual == "user@example.com"
    stored = json.loads(fake_keyring.items[("jarvis.google", "user@example.com")])
    assert stored["refresh_token"] == "r-token"
    assert stored["scopes"] == list(SCOPES)


def test_add_account_refuses_a_different_address(fake_keyring):
    with pytest.raises(AuthError, match="Signed in as other@example.com"):
        google_auth.add_account(
            "user@example.com",
            consent=lambda email: _creds(),
            whoami=lambda creds: "other@example.com",
        )

    assert fake_keyring.items == {}


def test_add_account_refuses_partial_permissions(fake_keyring):
    partial = [scope for scope in SCOPES if not scope.endswith("gmail.compose")]

    with pytest.raises(AuthError, match="Missing: gmail.compose"):
        google_auth.add_account(
            "user@example.com",
            consent=lambda email: _creds(granted=partial),
            whoami=lambda creds: "user@example.com",
        )

    assert fake_keyring.items == {}


def test_add_account_refuses_when_no_refresh_token(fake_keyring):
    with pytest.raises(AuthError, match="no long-lived token"):
        google_auth.add_account(
            "user@example.com",
            consent=lambda email: _creds(refresh_token=None),
            whoami=lambda creds: "user@example.com",
        )

    assert fake_keyring.items == {}


def test_add_account_installs_the_client_file_first(fake_keyring, fake_home, tmp_path):
    source = tmp_path / "client.json"
    source.write_bytes(_client_json())

    google_auth.add_account(
        "user@example.com",
        source,
        consent=lambda email: _creds(),
        whoami=lambda creds: "user@example.com",
    )

    assert (fake_home / ".config" / "jarvis" / "google_client.json").exists()


# --- stored credentials --------------------------------------------------


def test_credentials_round_trip_through_the_keychain(fake_keyring):
    google_auth.save_credentials("User@Example.com", _creds())

    assert google_auth.has_credentials("user@example.com")
    loaded = google_auth.load_credentials("USER@example.com", refresh=False)
    assert loaded.refresh_token == "r-token"
    assert loaded.client_id == "123.apps.googleusercontent.com"
    assert set(loaded.scopes) == set(SCOPES)


def test_no_stored_credentials(fake_keyring):
    assert google_auth.has_credentials("nobody@example.com") is False
    assert google_auth.load_credentials("nobody@example.com") is None


def test_missing_scopes():
    assert google_auth.missing_scopes(_creds()) == []
    assert google_auth.missing_scopes(_creds(granted=list(SCOPES[:1]))) == list(SCOPES[1:])
