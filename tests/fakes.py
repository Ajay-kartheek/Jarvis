"""Fake Google API adapters with the same methods as ``jarvis.google_api``."""

from __future__ import annotations

import base64
import re
import time
from collections import Counter
from types import SimpleNamespace

from jarvis.google_api import ChangesTokenExpired, HistoryExpired, SyncTokenExpired

NOW_S = 1_800_000_000  # a fixed "now" for tests (2027-01-15)
DAY = 86_400


class FakeHttpError(Exception):
    def __init__(self, status: int, reason: str = "") -> None:
        super().__init__(f"HTTP {status} {reason}")
        self.resp = SimpleNamespace(status=status)
        self.reason = reason
        body = '{"error": {"errors": [{"reason": "%s"}]}}' % reason
        self.content = body.encode()


def b64(text: str, encoding: str = "utf-8") -> str:
    return base64.urlsafe_b64encode(text.encode(encoding)).decode().rstrip("=")


def mail(
    message_id: str,
    days_ago: float = 1,
    *,
    subject: str = "Subject",
    sender: str = "Ann <ann@example.com>",
    to: str = "me@example.com",
    labels: tuple[str, ...] = ("INBOX",),
    body: str | None = "Hello there",
    html: str | None = None,
    attachments: tuple[str, ...] = (),
    thread_id: str | None = None,
) -> dict:
    parts = []
    if body is not None:
        parts.append({"mimeType": "text/plain", "filename": "", "body": {"data": b64(body)}})
    if html is not None:
        parts.append({"mimeType": "text/html", "filename": "", "body": {"data": b64(html)}})
    for name in attachments:
        parts.append({"mimeType": "application/pdf", "filename": name, "body": {"attachmentId": "A1"}})
    return {
        "id": message_id,
        "threadId": thread_id or f"t-{message_id}",
        "labelIds": list(labels),
        "internalDate": str(int((NOW_S - days_ago * DAY) * 1000)),
        "historyId": "1",
        "snippet": (body or "")[:50],
        "sizeEstimate": 1234,
        "payload": {
            "mimeType": "multipart/mixed",
            "filename": "",
            "headers": [
                {"name": "From", "value": sender},
                {"name": "To", "value": to},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": "Thu, 1 Jan 2026 10:00:00 +0000"},
                {"name": "Received", "value": "by mx.example.com"},
            ],
            "parts": parts,
        },
    }


class FakeGmail:
    """An in-memory mailbox. ``list_ids`` honours ``after:`` and newest-first."""

    HIDDEN = {"SPAM", "TRASH", "CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "DRAFT", "CHAT"}

    def __init__(self, messages: list[dict], *, history_id: int = 100, page_size: int = 3) -> None:
        self.messages = {m["id"]: m for m in messages}
        self.history_id = history_id
        self.page_size = page_size
        self.history_records: list[tuple[int, dict]] = []
        self.history_expired = False
        self.stale_tokens: set[str] = set()
        self.list_everything = False  # when True, list ignores labels (to test the label check)
        self.fail_get_after: int | None = None
        self.calls: Counter = Counter()
        self.list_tokens: list[str | None] = []
        self.queries: list[str] = []
        self.fetched: list[str] = []

    def profile(self) -> dict:
        self.calls["profile"] += 1
        return {"historyId": str(self.history_id), "emailAddress": "me@example.com"}

    def list_ids(self, query: str, page_token: str | None = None, page_size: int = 500):
        self.calls["list"] += 1
        self.queries.append(query)
        self.list_tokens.append(page_token)
        if page_token in self.stale_tokens:
            self.stale_tokens.discard(page_token)  # a fresh listing hands out good tokens
            raise FakeHttpError(400, "invalidArgument")
        after_ms = int(re.search(r"after:(\d+)", query).group(1)) * 1000
        live = [
            m
            for m in sorted(self.messages.values(), key=lambda m: -int(m["internalDate"]))
            if int(m["internalDate"]) >= after_ms
            and (self.list_everything or not self.HIDDEN & set(m["labelIds"]))
        ]
        start = int(page_token or 0)
        page = live[start : start + self.page_size]
        more = start + self.page_size < len(live)
        return [m["id"] for m in page], (str(start + self.page_size) if more else None)

    def get_many(self, ids: list[str]) -> dict:
        if self.fail_get_after is not None and self.calls["get"] >= self.fail_get_after:
            raise FakeHttpError(500, "backendError")
        self.calls["get"] += 1
        self.fetched.extend(ids)
        return {message_id: self.messages.get(message_id) for message_id in ids}

    def history(self, start_history_id: str, page_token: str | None = None) -> dict:
        self.calls["history"] += 1
        if self.history_expired:
            raise HistoryExpired()
        records = [r for at, r in self.history_records if at > int(start_history_id)]
        return {"history": records, "historyId": str(self.history_id)}

    # helpers for tests -----------------------------------------------------

    def deliver(self, message: dict) -> None:
        self.history_id += 1
        self.messages[message["id"]] = message
        self.history_records.append(
            (self.history_id, {"messagesAdded": [{"message": {"id": message["id"]}}]})
        )

    def delete(self, message_id: str) -> None:
        self.history_id += 1
        del self.messages[message_id]
        self.history_records.append(
            (self.history_id, {"messagesDeleted": [{"message": {"id": message_id}}]})
        )

    def relabel(self, message_id: str, labels: tuple[str, ...]) -> None:
        self.history_id += 1
        self.messages[message_id]["labelIds"] = list(labels)
        self.history_records.append(
            (self.history_id, {"labelsAdded": [{"message": {"id": message_id}, "labelIds": list(labels)}]})
        )


def event(
    event_id: str,
    days_from_now: float = 1,
    *,
    summary: str = "Meeting",
    status: str = "confirmed",
    attendees: list[dict] | None = None,
    **extra,
) -> dict:
    start = NOW_S + int(days_from_now * DAY)
    item = {
        "id": event_id,
        "status": status,
        "summary": summary,
        "start": {"dateTime": _iso(start)},
        "end": {"dateTime": _iso(start + 3600)},
        "htmlLink": f"https://calendar.example/{event_id}",
        "organizer": {"email": "boss@example.com", "displayName": "Boss"},
        "attendees": attendees or [],
        "updated": _iso(NOW_S),
    }
    item.update(extra)
    return item


def _iso(ts: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class FakeCalendar:
    def __init__(self, calendars: list[dict], events: dict[str, list[dict]], *, page_size: int = 2) -> None:
        self._calendars = calendars
        self.events = {cid: {e["id"]: e for e in items} for cid, items in events.items()}
        self.page_size = page_size
        self.changes: dict[str, list[dict]] = {cid: [] for cid in events}
        self.token_expired = False
        self.give_sync_tokens = True
        self.calls: Counter = Counter()
        self.requests: list[dict] = []

    def calendars(self) -> list[dict]:
        self.calls["calendars"] += 1
        return list(self._calendars)

    def events_page(self, calendar_id: str, *, time_min=None, time_max=None, sync_token=None, page_token=None) -> dict:
        self.calls["events"] += 1
        self.requests.append(
            {"calendar": calendar_id, "time_min": time_min, "time_max": time_max,
             "sync_token": sync_token, "page_token": page_token}
        )
        if sync_token:
            if self.token_expired:
                raise SyncTokenExpired()
            items = self.changes[calendar_id]
            self.changes[calendar_id] = []
            return {"items": items, "nextSyncToken": f"sync-{self.calls['events']}"}
        items = sorted(self.events[calendar_id].values(), key=lambda e: e["id"])
        start = int(page_token or 0)
        page = items[start : start + self.page_size]
        if start + self.page_size < len(items):
            return {"items": page, "nextPageToken": str(start + self.page_size)}
        response = {"items": page}
        if self.give_sync_tokens:
            response["nextSyncToken"] = f"sync-{self.calls['events']}"
        return response

    def change(self, calendar_id: str, item: dict) -> None:
        if item.get("status") == "cancelled":
            self.events[calendar_id].pop(item["id"], None)
        else:
            self.events[calendar_id][item["id"]] = item
        self.changes[calendar_id].append(item)


GDOC = "application/vnd.google-apps.document"
GSHEET = "application/vnd.google-apps.spreadsheet"
GSLIDES = "application/vnd.google-apps.presentation"


def drive_file(
    file_id: str,
    name: str = "File",
    mime: str = GDOC,
    *,
    modified_days_ago: float = 5,
    viewed_days_ago: float | None = 3,
    **extra,
) -> dict:
    item = {
        "id": file_id,
        "name": name,
        "mimeType": mime,
        "modifiedTime": _iso(NOW_S - int(modified_days_ago * DAY)),
        "createdTime": _iso(NOW_S - 400 * DAY),
        "owners": [{"displayName": "Owner", "emailAddress": "owner@example.com"}],
        "lastModifyingUser": {"displayName": "Editor", "emailAddress": "editor@example.com"},
        "webViewLink": f"https://docs.example/{file_id}",
        "parents": ["folder1"],
    }
    if viewed_days_ago is not None:
        item["viewedByMeTime"] = _iso(NOW_S - int(viewed_days_ago * DAY))
    item.update(extra)
    return item


class FakeDrive:
    def __init__(self, files: list[dict], *, page_size: int = 2) -> None:
        self.files = {f["id"]: f for f in files}
        self.page_size = page_size
        self.exports: dict[tuple[str, str], bytes | Exception] = {}
        self.tabs: dict[str, list[dict] | Exception] = {}
        self.values: dict[str, list[list[list]]] = {}
        self.pending_changes: list[dict] = []
        self.page_token = 10
        self.token_expired = False
        self.calls: Counter = Counter()
        self.exported: list[tuple[str, str]] = []
        self.queries: list[str] = []
        self.value_ranges: list[list[str]] = []

    def start_page_token(self) -> str:
        self.calls["start_token"] += 1
        return str(self.page_token)

    def list_files(self, query: str, page_token: str | None = None) -> dict:
        self.calls["list"] += 1
        self.queries.append(query)
        items = sorted(self.files.values(), key=lambda f: f["id"])
        start = int(page_token or 0)
        response: dict = {"files": items[start : start + self.page_size]}
        if start + self.page_size < len(items):
            response["nextPageToken"] = str(start + self.page_size)
        return response

    def changes(self, page_token: str) -> dict:
        self.calls["changes"] += 1
        if self.token_expired:
            raise ChangesTokenExpired()
        items, self.pending_changes = self.pending_changes, []
        self.page_token += 1
        return {"changes": items, "newStartPageToken": str(self.page_token)}

    def export(self, file_id: str, mime_type: str) -> bytes:
        self.calls["export"] += 1
        self.exported.append((file_id, mime_type))
        result = self.exports.get((file_id, mime_type), b"exported text")
        if isinstance(result, Exception):
            raise result
        return result

    def sheet_tabs(self, file_id: str) -> list[dict]:
        self.calls["tabs"] += 1
        result = self.tabs.get(file_id, [{"title": "Sheet1", "gridProperties": {"rowCount": 10, "columnCount": 3}}])
        if isinstance(result, Exception):
            raise result
        return result

    def sheet_values(self, file_id: str, ranges: list[str]) -> list[list[list]]:
        self.calls["values"] += 1
        self.value_ranges.append(ranges)
        return self.values.get(file_id, [[["a", "b"], ["1", "2"]] for _ in ranges])

    def touch(self, item: dict) -> None:
        self.files[item["id"]] = item
        self.pending_changes.append({"fileId": item["id"], "removed": False, "file": item})

    def remove(self, file_id: str) -> None:
        self.files.pop(file_id, None)
        self.pending_changes.append({"fileId": file_id, "removed": True})
