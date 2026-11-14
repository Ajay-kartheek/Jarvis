"""Google sign-in for Jarvis: read-only access plus Gmail drafts.

Jarvis may read mail, calendar and Drive, and may create drafts. It delivers
mail only when the user has said yes to that very draft. Google has no
drafts-only permission (the compose scope that allows drafts also allows
delivery), so the limit is enforced in this code base: the one function that
delivers mail, ``GmailApi.send_draft``, refuses without that yes, and
``tests/test_no_send.py`` fails the build if any other way to deliver appears.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow, WSGITimeoutError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from oauthlib.oauth2 import OAuth2Error

from jarvis import keychain, paths

SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",  # answering invitations and placing holds, after the user's yes
    "https://www.googleapis.com/auth/drive.readonly",
)

DEFAULT_CONSENT_TIMEOUT_SECONDS = 300


class AuthError(RuntimeError):
    """Sign-in could not be completed. The message is safe to show the user."""


@dataclass(frozen=True)
class ApiCheck:
    name: str
    ok: bool
    detail: str


def normalise_email(email: str) -> str:
    return email.strip().lower()


def install_client_file(source: Path) -> Path:
    """Copy the downloaded OAuth client JSON into the secrets folder."""
    source = Path(source).expanduser()
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise AuthError(f"Cannot read the client file {source}: {exc}") from None
    validate_client_json(raw)
    target = paths.google_client_file()
    paths.write_secret_file(target, raw)
    return target


def validate_client_json(raw: bytes) -> None:
    try:
        data = json.loads(raw)
    except ValueError:
        raise AuthError("The client file is not valid JSON.") from None
    if not isinstance(data, dict):
        raise AuthError("The client file is not an OAuth client download.")
    if "installed" not in data:
        if "web" in data:
            raise AuthError(
                "This is a 'Web application' client. Create an OAuth client of "
                "type 'Desktop app' and download that JSON instead."
            )
        if data.get("type") == "service_account":
            raise AuthError(
                "This is a service-account key. Jarvis needs an OAuth client of "
                "type 'Desktop app' (APIs & Services > Credentials)."
            )
        raise AuthError(
            "The client file is not an OAuth 'Desktop app' client download."
        )
    missing = [
        key
        for key in ("client_id", "client_secret", "auth_uri", "token_uri")
        if not data["installed"].get(key)
    ]
    if missing:
        raise AuthError(f"The client file is missing: {', '.join(missing)}.")


def run_consent_flow(
    email: str, timeout_seconds: int = DEFAULT_CONSENT_TIMEOUT_SECONDS
) -> Credentials:
    """Open the browser for Google consent and return the new credentials."""
    client_file = paths.google_client_file()
    if not client_file.exists():
        raise AuthError(
            "No Google client file yet. Pass --client <path to the downloaded JSON>."
        )
    flow = InstalledAppFlow.from_client_secrets_file(
        str(client_file), scopes=list(SCOPES)
    )
    try:
        # Without this, oauthlib raises a bare Warning when the user unticks a
        # permission; we check what was granted ourselves and explain it.
        with _relaxed_scope_check():
            return flow.run_local_server(
                host="localhost",
                port=0,
                open_browser=True,
                timeout_seconds=timeout_seconds,
                access_type="offline",
                prompt="consent",
                login_hint=email,
            )
    except WSGITimeoutError:
        raise AuthError(
            f"No sign-in was completed within {timeout_seconds} seconds. If the "
            "browser showed 'Access blocked' or 'admin_policy_enforced', the "
            "Google Workspace admin is blocking this app and has to allow it."
        ) from None
    except OAuth2Error as exc:
        raise AuthError(f"Google refused the sign-in: {exc}") from None


def missing_scopes(creds: Credentials) -> list[str]:
    granted = set(creds.granted_scopes or creds.scopes or [])
    return [scope for scope in SCOPES if scope not in granted]


def fetch_signed_in_address(creds: Credentials) -> str:
    """Ask Gmail which mailbox these credentials belong to."""
    profile = _service("gmail", "v1", creds).users().getProfile(userId="me").execute()
    return profile["emailAddress"]


def add_account(
    email: str,
    client_source: Path | None = None,
    *,
    consent: Callable[[str], Credentials] = run_consent_flow,
    whoami: Callable[[Credentials], str] = fetch_signed_in_address,
) -> str:
    """Sign ``email`` in and store its credentials in the Keychain.

    Returns the address Google reports. Nothing is stored unless every
    permission was granted and the signed-in address matches ``email``.
    """
    if client_source is not None:
        install_client_file(client_source)
    creds = consent(email)
    if not creds.refresh_token:
        raise AuthError(
            "Google returned no long-lived token, so nothing was saved. "
            "Run the command again and approve the consent screen."
        )
    missing = missing_scopes(creds)
    if missing:
        raise AuthError(
            "Not every permission was granted, so nothing was saved. Run the "
            "command again and leave every box ticked. Missing: "
            + ", ".join(scope.rsplit("/", 1)[-1] for scope in missing)
        )
    try:
        actual = whoami(creds)
    except (HttpError, GoogleAuthError) as exc:
        raise AuthError(
            "Signed in, but the Gmail check failed, so nothing was saved: "
            + describe_error(exc)
        ) from None
    if normalise_email(actual) != normalise_email(email):
        raise AuthError(
            f"Signed in as {actual}, but {email} was expected. Nothing was saved. "
            "Run the command again and pick the right account."
        )
    save_credentials(email, creds)
    return actual


def save_credentials(email: str, creds: Credentials) -> None:
    keychain.save_secret(
        keychain.GOOGLE_SERVICE, normalise_email(email), creds.to_json()
    )


def has_credentials(email: str) -> bool:
    return (
        keychain.load_secret(keychain.GOOGLE_SERVICE, normalise_email(email))
        is not None
    )


def load_credentials(email: str, *, refresh: bool = True) -> Credentials | None:
    """Return stored credentials for ``email``, refreshed if they have expired."""
    raw = keychain.load_secret(keychain.GOOGLE_SERVICE, normalise_email(email))
    if raw is None:
        return None
    creds = Credentials.from_authorized_user_info(json.loads(raw), list(SCOPES))
    if refresh and not creds.valid:
        creds.refresh(Request())
        save_credentials(email, creds)
    return creds


def check_apis(creds: Credentials) -> list[ApiCheck]:
    """Make one tiny read call per API so a block or a disabled API shows up."""
    probes: list[tuple[str, Callable[[], str]]] = [
        ("Gmail", lambda: fetch_signed_in_address(creds)),
        ("Calendar", lambda: _probe_calendar(creds)),
        ("Drive", lambda: _probe_drive(creds)),
    ]
    results = []
    for name, probe in probes:
        try:
            results.append(ApiCheck(name, True, probe()))
        except Exception as exc:  # report whatever went wrong, per API
            results.append(ApiCheck(name, False, describe_error(exc)))
    return results


def describe_error(exc: BaseException) -> str:
    if isinstance(exc, HttpError):
        reason = getattr(exc, "reason", None) or str(exc)
        return f"HTTP {exc.resp.status}: {reason}"
    return f"{type(exc).__name__}: {exc}"


def _probe_calendar(creds: Credentials) -> str:
    listing = _service("calendar", "v3", creds).calendarList().list(maxResults=1).execute()
    return f"{len(listing.get('items', []))} calendar listed"


def _probe_drive(creds: Credentials) -> str:
    about = _service("drive", "v3", creds).about().get(fields="user(emailAddress)").execute()
    return about.get("user", {}).get("emailAddress", "ok")


def _service(name: str, version: str, creds: Credentials):
    return build(name, version, credentials=creds, cache_discovery=False)


@contextmanager
def _relaxed_scope_check():
    key = "OAUTHLIB_RELAX_TOKEN_SCOPE"
    previous = os.environ.get(key)
    os.environ[key] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous
