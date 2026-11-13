"""Thin, read-only adapters over the Google APIs.

Each adapter exposes the few calls the mirrors need as plain methods, paced
and retried. The sync logic in ``gmail_sync``, ``calendar_sync`` and
``drive_sync`` talks to these and is tested against fakes with the same
methods. Nothing here writes to Google.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from jarvis.ratelimit import (
    RateLimiter,
    backoff_seconds,
    call_with_retry,
    error_reason,
    error_status,
    is_transient,
)

# Gmail allows this Cloud project 6,000 quota units per user per minute
# ("Total Query Cost"), and Jarvis uses 90% of it. Fetching one full message
# measured at about 50 units on 2026-11-12, ten times the documented figure.
GMAIL_UNITS_PER_SECOND = 90
GMAIL_LIST_COST = 5
GMAIL_GET_COST = 50
GMAIL_HISTORY_COST = 2
GMAIL_PROFILE_COST = 1
GMAIL_DRAFT_COST = 10
GMAIL_BATCH_SIZE = 15

DRIVE_FILE_FIELDS = (
    "id,name,mimeType,modifiedTime,viewedByMeTime,modifiedByMeTime,createdTime,"
    "owners(displayName,emailAddress),lastModifyingUser(displayName,emailAddress),"
    "webViewLink,driveId,parents,size,trashed"
)


class HistoryExpired(Exception):
    """Gmail no longer has history back to the stored history id."""


class GmailUnavailable(Exception):
    """Gmail kept refusing fetches after every retry; the sync can be resumed."""


class SyncTokenExpired(Exception):
    """Calendar rejected the stored sync token; a full sync is needed."""


class ChangesTokenExpired(Exception):
    """Drive rejected the stored changes page token."""


def _build(name: str, version: str, creds: Credentials):
    return build(name, version, credentials=creds, cache_discovery=False)


def describe_http_error(exc: BaseException) -> str:
    """Status and Google's reason for an error, without any response body."""
    status = error_status(exc)
    if status is None:
        return type(exc).__name__
    reason = error_reason(exc)
    return f"HTTP {status} {reason}".strip()


class GmailApi:
    MAX_RETRY_ROUNDS = 8

    def __init__(self, creds: Credentials, *, limiter: RateLimiter | None = None,
                 batch_size: int = GMAIL_BATCH_SIZE,
                 on_retry: Callable[[str], None] | None = None) -> None:
        self._svc = _build("gmail", "v1", creds)
        self._limiter = limiter or RateLimiter(GMAIL_UNITS_PER_SECOND)
        self._batch_size = batch_size
        self._on_retry = on_retry

    def profile(self) -> dict:
        self._limiter.acquire(GMAIL_PROFILE_COST)
        return call_with_retry(lambda: self._svc.users().getProfile(userId="me").execute())

    def list_ids(self, query: str, page_token: str | None = None,
                 page_size: int = 500) -> tuple[list[str], str | None]:
        self._limiter.acquire(GMAIL_LIST_COST)
        response = call_with_retry(
            lambda: self._svc.users().messages().list(
                userId="me", q=query, maxResults=page_size, pageToken=page_token,
                includeSpamTrash=False,
            ).execute()
        )
        return [m["id"] for m in response.get("messages", [])], response.get("nextPageToken")

    def get_many(self, ids: list[str]) -> dict[str, dict | None]:
        """Fetch full messages. A value of None means the message is gone.

        Rate-limit and server errors are retried with a growing pause and a
        smaller batch; if they outlast every retry, ``GmailUnavailable`` is
        raised and the caller can resume later.
        """
        results: dict[str, dict | None] = {}
        pending = list(ids)
        attempt = 0
        batch_size = self._batch_size
        while pending:
            failed: list[str] = []
            last_error = ""
            for start in range(0, len(pending), batch_size):
                chunk = pending[start : start + batch_size]
                outcome = call_with_retry(lambda chunk=chunk: self._run_batch(chunk))
                for message_id in chunk:
                    response, error = outcome.get(message_id, (None, TimeoutError("no response")))
                    if error is None:
                        results[message_id] = response
                    elif error_status(error) == 404:
                        results[message_id] = None
                    elif is_transient(error):
                        failed.append(message_id)
                        last_error = describe_http_error(error)
                    else:
                        raise error
            if failed:
                attempt += 1
                if attempt > self.MAX_RETRY_ROUNDS:
                    raise GmailUnavailable(
                        f"Gmail kept failing for {len(failed)} messages ({last_error})"
                    )
                pause = backoff_seconds(attempt, base=2.0, ceiling=90.0)
                # Fewer requests in flight at once is what Gmail asks for when it throttles.
                batch_size = max(3, batch_size // 2)
                if self._on_retry:
                    self._on_retry(
                        f"gmail: {len(failed)} fetches refused ({last_error}); "
                        f"waiting {pause:.0f}s, batches of {batch_size}"
                    )
                time.sleep(pause)
            pending = failed
        return results

    def _run_batch(self, chunk: list[str]) -> dict[str, tuple[dict | None, Exception | None]]:
        self._limiter.acquire(GMAIL_GET_COST * len(chunk))
        outcome: dict[str, tuple[dict | None, Exception | None]] = {}

        def collect(request_id, response, exception):
            outcome[request_id] = (response, exception)

        batch = self._svc.new_batch_http_request(callback=collect)
        for message_id in chunk:
            batch.add(
                self._svc.users().messages().get(userId="me", id=message_id, format="full"),
                request_id=message_id,
            )
        batch.execute()
        return outcome

    def create_draft(self, raw: str, thread_id: str | None = None) -> dict:
        """Save a draft: the one change Jarvis makes in Google. Nothing is delivered."""
        message = {"raw": raw}
        if thread_id:
            message["threadId"] = thread_id
        self._limiter.acquire(GMAIL_DRAFT_COST)
        return call_with_retry(
            lambda: self._svc.users().drafts().create(userId="me", body={"message": message}).execute()
        )

    def history(self, start_history_id: str, page_token: str | None = None) -> dict:
        self._limiter.acquire(GMAIL_HISTORY_COST)
        try:
            return call_with_retry(
                lambda: self._svc.users().history().list(
                    userId="me", startHistoryId=start_history_id, pageToken=page_token,
                    maxResults=500,
                    historyTypes=["messageAdded", "messageDeleted", "labelAdded", "labelRemoved"],
                ).execute()
            )
        except HttpError as exc:
            if error_status(exc) == 404:
                raise HistoryExpired() from None
            raise


class CalendarApi:
    def __init__(self, creds: Credentials, *, limiter: RateLimiter | None = None) -> None:
        self._svc = _build("calendar", "v3", creds)
        self._limiter = limiter or RateLimiter(8)

    def calendars(self) -> list[dict]:
        items: list[dict] = []
        token = None
        while True:
            self._limiter.acquire()
            response = call_with_retry(
                lambda: self._svc.calendarList().list(maxResults=250, pageToken=token).execute()
            )
            items.extend(response.get("items", []))
            token = response.get("nextPageToken")
            if not token:
                return items

    def events_page(self, calendar_id: str, *, time_min: str | None = None,
                    time_max: str | None = None, sync_token: str | None = None,
                    page_token: str | None = None) -> dict:
        kwargs: dict = {
            "calendarId": calendar_id, "singleEvents": True, "maxResults": 2500,
            "pageToken": page_token,
        }
        if sync_token:
            kwargs["syncToken"] = sync_token
        else:
            kwargs.update(timeMin=time_min, timeMax=time_max, showDeleted=False)
        self._limiter.acquire()
        try:
            return call_with_retry(lambda: self._svc.events().list(**kwargs).execute())
        except HttpError as exc:
            if error_status(exc) == 410:
                raise SyncTokenExpired() from None
            raise


class DriveApi:
    def __init__(self, creds: Credentials, *, limiter: RateLimiter | None = None,
                 sheets_limiter: RateLimiter | None = None) -> None:
        self._drive = _build("drive", "v3", creds)
        self._sheets = _build("sheets", "v4", creds)
        self._limiter = limiter or RateLimiter(8)
        # The Sheets API allows 60 read requests a minute per user.
        self._sheets_limiter = sheets_limiter or RateLimiter(50 / 60)

    def start_page_token(self) -> str:
        self._limiter.acquire()
        response = call_with_retry(
            lambda: self._drive.changes().getStartPageToken(supportsAllDrives=True).execute()
        )
        return response["startPageToken"]

    def list_files(self, query: str, page_token: str | None = None) -> dict:
        self._limiter.acquire()
        return call_with_retry(
            lambda: self._drive.files().list(
                q=query, corpora="allDrives", includeItemsFromAllDrives=True,
                supportsAllDrives=True, pageSize=1000, pageToken=page_token,
                fields=f"nextPageToken,incompleteSearch,files({DRIVE_FILE_FIELDS})",
            ).execute()
        )

    def changes(self, page_token: str) -> dict:
        self._limiter.acquire()
        try:
            return call_with_retry(
                lambda: self._drive.changes().list(
                    pageToken=page_token, includeItemsFromAllDrives=True, supportsAllDrives=True,
                    includeRemoved=True, pageSize=1000,
                    fields="nextPageToken,newStartPageToken,"
                    f"changes(fileId,removed,file({DRIVE_FILE_FIELDS}))",
                ).execute()
            )
        except HttpError as exc:
            if error_status(exc) in (400, 404, 410):
                raise ChangesTokenExpired() from None
            raise

    def export(self, file_id: str, mime_type: str) -> bytes:
        self._limiter.acquire()
        data = call_with_retry(
            lambda: self._drive.files().export(fileId=file_id, mimeType=mime_type).execute(),
            attempts=3,
        )
        return data if isinstance(data, bytes) else str(data).encode()

    def sheet_tabs(self, file_id: str) -> list[dict]:
        self._sheets_limiter.acquire()
        response = call_with_retry(
            lambda: self._sheets.spreadsheets().get(
                spreadsheetId=file_id,
                fields="sheets(properties(title,sheetType,hidden,gridProperties(rowCount,columnCount)))",
            ).execute(),
            attempts=4,
        )
        return [sheet.get("properties", {}) for sheet in response.get("sheets", [])]

    def sheet_values(self, file_id: str, ranges: list[str]) -> list[list[list]]:
        self._sheets_limiter.acquire()
        response = call_with_retry(
            lambda: self._sheets.spreadsheets().values().batchGet(
                spreadsheetId=file_id, ranges=ranges, valueRenderOption="FORMATTED_VALUE",
                majorDimension="ROWS",
            ).execute(),
            attempts=4,
        )
        return [value_range.get("values", []) for value_range in response.get("valueRanges", [])]
