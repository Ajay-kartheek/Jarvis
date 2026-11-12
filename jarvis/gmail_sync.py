"""Mirror Gmail into the local store (read-only).

First run: a resumable backfill of the ingest window, newest first. Later
runs: incremental sync through the history API, with a full reconcile when
the stored history id is too old. Promotions, Social, Spam, Trash, drafts
and chats are left out.
"""

from __future__ import annotations

import base64
import html
import re
from collections.abc import Callable
from datetime import datetime

from jarvis.google_api import HistoryExpired
from jarvis.ratelimit import error_status
from jarvis.store import MailRecord, Store
from jarvis.textutil import cap, html_to_text, tidy
from jarvis.timeutil import day, shift_months, utc_now

SOURCE = "gmail"
BODY_CAP = 30_000
FETCH_CHUNK = 60  # messages fetched and committed together

SKIP_LABELS = frozenset(
    {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "SPAM", "TRASH", "DRAFT", "CHAT"}
)
KEPT_HEADERS = frozenset(
    {
        "from", "to", "cc", "bcc", "reply-to", "sender", "subject", "date", "message-id",
        "in-reply-to", "references", "list-id", "list-unsubscribe", "auto-submitted",
        "precedence", "delivered-to",
    }
)
_CHARSET = re.compile(r'charset\s*=\s*"?([^";\s]+)', re.IGNORECASE)

Progress = Callable[[str], None]


def window_start(months: int, now: datetime | None = None) -> int:
    """Unix time of the start of the ingest window."""
    return int(shift_months(now or utc_now(), -months).timestamp())


def build_query(after_s: int) -> str:
    # Spam and Trash are excluded by the list call itself.
    return f"after:{after_s} -category:promotions -category:social -in:drafts -in:chats"


def should_skip(labels: list[str]) -> bool:
    return not SKIP_LABELS.isdisjoint(labels)


def parse_message(message: dict) -> MailRecord:
    """Turn a Gmail API message (format=full) into a ``MailRecord``."""
    payload = message.get("payload") or {}
    headers: dict[str, str] = {}
    for header in payload.get("headers", []):
        name = (header.get("name") or "").lower()
        if name in KEPT_HEADERS and name not in headers:
            headers[name] = header.get("value") or ""

    plain: list[str] = []
    rich: list[str] = []
    attachments: list[str] = []
    _walk(payload, plain, rich, attachments)
    text = tidy("\n\n".join(plain))
    if not text and rich:
        text = html_to_text("\n".join(rich))
    body, truncated = cap(text, BODY_CAP)

    return MailRecord(
        id=message["id"],
        thread_id=message.get("threadId") or message["id"],
        internal_date=int(message.get("internalDate") or 0),
        history_id=message.get("historyId"),
        subject=headers.get("subject", ""),
        sender=headers.get("from", ""),
        recipients=headers.get("to", ""),
        cc=headers.get("cc", ""),
        bcc=headers.get("bcc", ""),
        headers=headers,
        labels=list(message.get("labelIds") or []),
        snippet=html.unescape(message.get("snippet") or ""),
        body=body,
        body_truncated=truncated,
        attachments=attachments,
        size_estimate=message.get("sizeEstimate"),
    )


def _walk(part: dict, plain: list[str], rich: list[str], attachments: list[str]) -> None:
    filename = part.get("filename") or ""
    if filename:
        attachments.append(filename)  # the name only; content is never fetched
        return
    children = part.get("parts") or []
    if children:
        for child in children:
            _walk(child, plain, rich, attachments)
        return
    mime = (part.get("mimeType") or "").lower()
    data = (part.get("body") or {}).get("data")
    if not data or mime not in ("text/plain", "text/html"):
        return
    text = _decode(data, _charset(part))
    (plain if mime == "text/plain" else rich).append(text)


def _charset(part: dict) -> str | None:
    for header in part.get("headers") or []:
        if (header.get("name") or "").lower() == "content-type":
            match = _CHARSET.search(header.get("value") or "")
            return match.group(1) if match else None
    return None


def _decode(data: str, charset: str | None) -> str:
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    try:
        return raw.decode(charset or "utf-8")
    except (LookupError, UnicodeDecodeError):
        return raw.decode("utf-8", errors="replace")


def sync(
    api,
    store: Store,
    account: str,
    months: int,
    *,
    progress: Progress | None = None,
    max_messages: int | None = None,
    now: datetime | None = None,
) -> dict:
    """Run one Gmail sync for ``account`` and return its counters."""
    say = progress or (lambda message: None)
    summary = {
        "mode": "", "listed": 0, "fetched": 0, "stored": 0, "skipped": 0, "gone": 0,
        "deleted": 0, "complete": False,
    }
    current_after = window_start(months, now)

    # The history baseline is taken before any listing, so nothing that
    # changes while the backfill runs is missed by the next incremental sync.
    if store.get_state(account, SOURCE, "history_id") is None:
        store.set_state(account, SOURCE, "history_id", api.profile()["historyId"])

    if store.get_state(account, SOURCE, "backfill_done") != "1":
        summary["mode"] = "backfill"
        after_s = int(store.get_state(account, SOURCE, "backfill_after") or current_after)
        store.set_state(account, SOURCE, "backfill_after", after_s)
        if not _backfill(api, store, account, after_s, summary, say, max_messages):
            return summary  # sample run: stopped early, resumable
        store.set_state(account, SOURCE, "backfill_done", "1")
        store.delete_state(account, SOURCE, "backfill_page_token")
        say(f"gmail: backfill finished, {summary['stored']} messages stored in this run")
    else:
        summary["mode"] = "incremental"

    backfill_after = int(store.get_state(account, SOURCE, "backfill_after") or current_after)
    if current_after < backfill_after - 86_400:
        # The ingest window was widened in config: pick up the older mail.
        summary["mode"] += "+widen"
        _reconcile(api, store, account, current_after, summary, say)
        store.set_state(account, SOURCE, "backfill_after", current_after)
    else:
        try:
            _incremental(api, store, account, current_after, summary, say)
        except HistoryExpired:
            summary["mode"] += "+reconcile"
            say("gmail: history is too old, reconciling against a full listing")
            _reconcile(api, store, account, current_after, summary, say)
    summary["complete"] = True
    return summary


def _backfill(api, store: Store, account: str, after_s: int, summary: dict,
              say: Progress, max_messages: int | None) -> bool:
    """Walk the window newest first. Returns False if stopped by ``max_messages``."""
    query = build_query(after_s)
    token = store.get_state(account, SOURCE, "backfill_page_token") or None
    resumed = bool(token)
    if resumed:
        say("gmail: resuming the backfill where it stopped")
    while True:
        try:
            ids, next_token = api.list_ids(query, token)
        except Exception as exc:
            if resumed and error_status(exc) == 400:
                # The saved page token went stale. Start from the top, once;
                # messages already stored are skipped without a fetch.
                resumed = False
                token = None
                store.delete_state(account, SOURCE, "backfill_page_token")
                continue
            raise
        resumed = False
        summary["listed"] += len(ids)
        missing = store.gmail_missing_ids(account, ids)
        for start in range(0, len(missing), FETCH_CHUNK):
            chunk = missing[start : start + FETCH_CHUNK]
            if max_messages is not None:
                chunk = chunk[: max(0, max_messages - summary["fetched"])]
                if not chunk:
                    return False
            oldest = _fetch_and_store(api, store, account, chunk, summary, after_s)
            reached = f", reached {day(oldest // 1000)}" if oldest else ""
            say(f"gmail: {summary['stored']} stored this run{reached}")
            if max_messages is not None and summary["fetched"] >= max_messages:
                return False
        if not next_token:
            return True
        token = next_token
        store.set_state(account, SOURCE, "backfill_page_token", token)


def _incremental(api, store: Store, account: str, after_s: int, summary: dict, say: Progress) -> None:
    start = store.get_state(account, SOURCE, "history_id")
    changed: dict[str, None] = {}
    deleted: set[str] = set()
    latest = start
    token = None
    while True:
        response = api.history(start, token)
        for record in response.get("history", []):
            for key in ("messagesAdded", "labelsAdded", "labelsRemoved"):
                for item in record.get(key, []):
                    changed[item["message"]["id"]] = None
            for item in record.get("messagesDeleted", []):
                deleted.add(item["message"]["id"])
        latest = response.get("historyId", latest)
        token = response.get("nextPageToken")
        if not token:
            break

    if deleted:
        summary["deleted"] += store.gmail_delete(account, sorted(deleted))
    # Changed messages are fetched again, so labels and content are current.
    to_fetch = [message_id for message_id in changed if message_id not in deleted]
    for start_index in range(0, len(to_fetch), FETCH_CHUNK):
        chunk = to_fetch[start_index : start_index + FETCH_CHUNK]
        _fetch_and_store(api, store, account, chunk, summary, after_s, drop_skipped=True)
    store.set_state(account, SOURCE, "history_id", latest)
    if changed or deleted:
        say(f"gmail: {len(to_fetch)} changed, {summary['deleted']} removed")


def _reconcile(api, store: Store, account: str, after_s: int, summary: dict, say: Progress) -> None:
    """Make the mirror match a full listing of the window."""
    baseline = api.profile()["historyId"]
    query = build_query(after_s)
    live: list[str] = []
    token = None
    while True:
        ids, token = api.list_ids(query, token)
        live.extend(ids)
        if not token:
            break
    summary["listed"] += len(live)
    stored = store.gmail_ids_since(account, after_s * 1000)
    stale = sorted(stored - set(live))  # deleted, trashed or moved to a skipped category
    if stale:
        summary["deleted"] += store.gmail_delete(account, stale)
    missing = [message_id for message_id in live if message_id not in stored]
    for start in range(0, len(missing), FETCH_CHUNK):
        _fetch_and_store(api, store, account, missing[start : start + FETCH_CHUNK], summary, after_s)
        say(f"gmail: reconcile, {summary['stored']} stored this run")
    store.set_state(account, SOURCE, "history_id", baseline)


def _fetch_and_store(api, store: Store, account: str, ids: list[str], summary: dict,
                     after_s: int, drop_skipped: bool = False) -> int | None:
    """Fetch ``ids`` and store the ones that belong in the mirror.

    Returns the oldest internal date stored (milliseconds), if any.
    """
    results = api.get_many(ids)
    summary["fetched"] += len(ids)
    records: list[MailRecord] = []
    drop: list[str] = []
    for message_id in ids:
        message = results.get(message_id)
        if message is None:
            summary["gone"] += 1
            drop.append(message_id)
            continue
        record = parse_message(message)
        if should_skip(record.labels) or record.internal_date < after_s * 1000:
            summary["skipped"] += 1
            drop.append(message_id)
            continue
        records.append(record)
    store.gmail_upsert(account, records)
    summary["stored"] += len(records)
    if drop_skipped and drop:
        # A mirrored message that moved to Trash, Spam or a skipped category.
        summary["deleted"] += store.gmail_delete(account, drop)
    return min((record.internal_date for record in records), default=None)
