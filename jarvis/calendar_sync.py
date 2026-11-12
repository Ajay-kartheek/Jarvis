"""Mirror Google Calendar into the local store (read-only).

Covers the calendars the account owns or can write to, from the ingest
window back to three months ahead, with recurring events expanded into
single events. Incremental runs use Calendar's sync tokens; a full listing
runs once a day so the window keeps rolling forward.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime

from jarvis.google_api import SyncTokenExpired
from jarvis.store import EventRecord, Store
from jarvis.textutil import cap, html_to_text, tidy
from jarvis.timeutil import rfc3339, shift_months, utc_now

SOURCE = "calendar"
AHEAD_MONTHS = 3
FULL_RESYNC_SECONDS = 24 * 3600
WRITABLE_ROLES = frozenset({"owner", "writer"})
# Stored, but kept out of the search index: one per working day adds only noise.
UNINDEXED_EVENT_TYPES = frozenset({"workingLocation"})
DESCRIPTION_CAP = 20_000

_MEETING_LINK = re.compile(
    r"https://[\w.-]*(?:zoom\.us|teams\.microsoft\.com|teams\.live\.com|meet\.google\.com|"
    r"webex\.com|gotomeeting\.com|chime\.aws|bluejeans\.com)/[^\s<>\"')\]]+"
)
_HAS_TAG = re.compile(r"<[a-zA-Z/][^>]*>")

Progress = Callable[[str], None]


def parse_event(item: dict) -> EventRecord:
    start, end = item.get("start") or {}, item.get("end") or {}
    start_raw = start.get("dateTime") or start.get("date")
    end_raw = end.get("dateTime") or end.get("date")
    description = item.get("description") or ""
    description = html_to_text(description) if _HAS_TAG.search(description) else tidy(description)
    description, _ = cap(description, DESCRIPTION_CAP)
    location = item.get("location") or ""

    attendees = []
    my_response = None
    for person in item.get("attendees") or []:
        attendees.append(
            {
                "email": person.get("email", ""),
                "name": person.get("displayName", ""),
                "response": person.get("responseStatus", ""),
                "optional": bool(person.get("optional")),
                "organizer": bool(person.get("organizer")),
                "resource": bool(person.get("resource")),
                "self": bool(person.get("self")),
            }
        )
        if person.get("self"):
            my_response = person.get("responseStatus")

    organizer = item.get("organizer") or {}
    event_type = item.get("eventType") or "default"
    return EventRecord(
        id=item["id"],
        status=item.get("status"),
        event_type=event_type,
        summary=item.get("summary") or "",
        description=description,
        location=location,
        start_ts=_to_ts(start_raw),
        end_ts=_to_ts(end_raw),
        start_raw=start_raw,
        end_raw=end_raw,
        all_day="date" in start and "dateTime" not in start,
        organizer=_person(organizer.get("displayName"), organizer.get("email")),
        attendees=attendees,
        my_response=my_response,
        meeting_link=_meeting_link(item, location, description),
        html_link=item.get("htmlLink"),
        recurring_event_id=item.get("recurringEventId"),
        updated=item.get("updated"),
        indexed=event_type not in UNINDEXED_EVENT_TYPES,
    )


def _meeting_link(item: dict, location: str, description: str) -> str | None:
    if item.get("hangoutLink"):
        return item["hangoutLink"]
    for entry in (item.get("conferenceData") or {}).get("entryPoints") or []:
        if entry.get("entryPointType") == "video" and entry.get("uri"):
            return entry["uri"]
    match = _MEETING_LINK.search(f"{location}\n{description}")
    return match.group(0) if match else None


def _to_ts(value: str | None) -> int | None:
    if not value:
        return None
    try:
        # An all-day date has no offset and is read as local midnight.
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _person(name: str | None, email: str | None) -> str:
    if name and email:
        return f"{name} <{email}>"
    return name or email or ""


def sync(
    api,
    store: Store,
    account: str,
    months: int,
    *,
    progress: Progress | None = None,
    now: datetime | None = None,
    ahead_months: int = AHEAD_MONTHS,
) -> dict:
    """Run one Calendar sync for ``account`` and return its counters."""
    say = progress or (lambda message: None)
    now = now or utc_now()
    now_s = int(now.timestamp())
    window = (shift_months(now, -months), shift_months(now, ahead_months))
    summary = {"calendars": 0, "full": 0, "incremental": 0, "upserted": 0, "deleted": 0}

    calendars = [
        c for c in api.calendars() if c.get("accessRole") in WRITABLE_ROLES and not c.get("deleted")
    ]
    store.calendar_set_calendars(account, calendars)
    summary["calendars"] = len(calendars)
    mirrored = {c["id"] for c in calendars}
    known = {
        row[0]
        for row in store.db.execute(
            "SELECT DISTINCT calendar_id FROM calendar_events WHERE account=?", (account,)
        )
    }
    for gone in sorted(known - mirrored):
        summary["deleted"] += store.calendar_drop_calendar(account, gone)
        store.delete_state(account, SOURCE, f"sync_token:{gone}")
        store.delete_state(account, SOURCE, f"full_sync_at:{gone}")

    for calendar in calendars:
        calendar_id = calendar["id"]
        token = store.get_state(account, SOURCE, f"sync_token:{calendar_id}")
        last_full = int(store.get_state(account, SOURCE, f"full_sync_at:{calendar_id}") or 0)
        if token and now_s - last_full < FULL_RESYNC_SECONDS:
            try:
                _incremental(api, store, account, calendar_id, token, window, summary)
                summary["incremental"] += 1
                continue
            except SyncTokenExpired:
                store.delete_state(account, SOURCE, f"sync_token:{calendar_id}")
        _full(api, store, account, calendar_id, window, now_s, summary)
        summary["full"] += 1
        say(f"calendar: {calendar.get('summary', calendar_id)}: {summary['upserted']} events so far")
    return summary


def _full(api, store: Store, account: str, calendar_id: str, window, now_s: int, summary: dict) -> None:
    seen: set[str] = set()
    page_token = None
    while True:
        page = api.events_page(
            calendar_id, time_min=rfc3339(window[0]), time_max=rfc3339(window[1]),
            page_token=page_token,
        )
        records = [parse_event(i) for i in page.get("items", []) if i.get("status") != "cancelled"]
        store.calendar_upsert_events(account, calendar_id, records)
        seen.update(record.id for record in records)
        summary["upserted"] += len(records)
        page_token = page.get("nextPageToken")
        if not page_token:
            break
    stale = store.calendar_event_ids(account, calendar_id) - seen
    summary["deleted"] += store.calendar_delete_events(account, calendar_id, sorted(stale))
    sync_token = page.get("nextSyncToken")
    if sync_token:
        store.set_state(account, SOURCE, f"sync_token:{calendar_id}", sync_token)
    else:
        store.delete_state(account, SOURCE, f"sync_token:{calendar_id}")
    store.set_state(account, SOURCE, f"full_sync_at:{calendar_id}", now_s)


def _incremental(api, store: Store, account: str, calendar_id: str, sync_token: str,
                 window, summary: dict) -> None:
    low, high = int(window[0].timestamp()), int(window[1].timestamp())
    upserts: list[EventRecord] = []
    deletes: list[str] = []
    page_token = None
    while True:
        page = api.events_page(calendar_id, sync_token=sync_token, page_token=page_token)
        for item in page.get("items", []):
            if item.get("status") == "cancelled":
                deletes.append(item["id"])
                continue
            record = parse_event(item)
            if record.start_ts is not None and low <= record.start_ts < high:
                upserts.append(record)
            else:
                deletes.append(record.id)  # moved outside the mirrored window
        page_token = page.get("nextPageToken")
        if not page_token:
            break
    store.calendar_upsert_events(account, calendar_id, upserts)
    summary["upserted"] += len(upserts)
    summary["deleted"] += store.calendar_delete_events(account, calendar_id, deletes)
    if page.get("nextSyncToken"):
        store.set_state(account, SOURCE, f"sync_token:{calendar_id}", page["nextSyncToken"])
