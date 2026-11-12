"""Mirror Google Drive into the local store (read-only).

Keeps the name, owner, dates and link of every file the account opened
inside the ingest window, and the text of Google Docs, Sheets and Slides.
Files that only other people touched are left out: on shared drives they
outnumber the user's own by far. The first run lists the window; later runs follow Drive's change
feed. A file that fails to export is recorded and the run carries on.
"""

from __future__ import annotations

from collections.abc import Callable

from jarvis.google_api import ChangesTokenExpired, describe_http_error
from jarvis.store import FileRecord, Store
from jarvis.textutil import cap, strip_data_uris, tidy
from jarvis.timeutil import rfc3339, shift_months, utc_now

SOURCE = "drive"
FOLDER = "application/vnd.google-apps.folder"
DOC = "application/vnd.google-apps.document"
SHEET = "application/vnd.google-apps.spreadsheet"
SLIDES = "application/vnd.google-apps.presentation"
EXPORTABLE = (DOC, SHEET, SLIDES)

TEXT_CAP = 200_000
SHEET_MAX_TABS = 20
SHEET_MAX_ROWS = 300
SHEET_MAX_COLUMNS = 26
PROGRESS_EVERY = 25

Progress = Callable[[str], None]


def parse_file(item: dict) -> FileRecord:
    editor = item.get("lastModifyingUser") or {}
    editor_text = " ".join(
        part
        for part in (
            editor.get("displayName", ""),
            f"<{editor['emailAddress']}>" if editor.get("emailAddress") else "",
        )
        if part
    )
    size = item.get("size")
    return FileRecord(
        id=item["id"],
        name=item.get("name", ""),
        mime_type=item.get("mimeType", ""),
        modified_time=item.get("modifiedTime"),
        viewed_by_me_time=item.get("viewedByMeTime"),
        modified_by_me_time=item.get("modifiedByMeTime"),
        created_time=item.get("createdTime"),
        owners=list(item.get("owners") or []),
        last_modifying_user=editor_text,
        web_view_link=item.get("webViewLink"),
        drive_id=item.get("driveId"),
        parents=list(item.get("parents") or []),
        size=int(size) if size is not None else None,
    )


def in_window(item: dict, cutoff: str) -> bool:
    """True if the account opened the file since ``cutoff``.

    Drive's timestamps are RFC 3339 in UTC, so comparing the strings works.
    """
    return (item.get("viewedByMeTime") or "") > cutoff


def sync(api, store: Store, account: str, ingest_months: int, *,
         progress: Progress = print, max_exports: int | None = None) -> dict:
    cutoff = rfc3339(shift_months(utc_now(), -ingest_months))
    summary = {"listed": 0, "upserted": 0, "deleted": 0, "exported": 0, "export_failed": 0}

    token = store.get_state(account, SOURCE, "changes_token")
    if token:
        try:
            token = _follow_changes(api, store, account, token, cutoff, summary)
        except ChangesTokenExpired:
            progress("drive: change feed expired, listing everything again")
            token = None
    if not token:
        # Take the token first, so nothing changed during the listing is missed.
        token = api.start_page_token()
        _list_window(api, store, account, cutoff, summary, progress)
    store.set_state(account, SOURCE, "changes_token", token)

    _export_pending(api, store, account, summary, progress, max_exports)
    summary["pending_exports"] = len(store.drive_pending_exports(account, EXPORTABLE))
    return summary


def _list_window(api, store: Store, account: str, cutoff: str, summary: dict,
                 progress: Progress) -> None:
    query = (
        f"trashed = false and mimeType != '{FOLDER}' and viewedByMeTime > '{cutoff}'"
    )
    seen: set[str] = set()
    page_token = None
    while True:
        page = api.list_files(query, page_token)
        records = [parse_file(item) for item in page.get("files", [])]
        store.drive_upsert_files(account, records)
        seen.update(record.id for record in records)
        summary["listed"] += len(records)
        summary["upserted"] += len(records)
        progress(f"drive: {account}: {summary['listed']} files listed")
        page_token = page.get("nextPageToken")
        if not page_token:
            break
    # Anything stored earlier that is no longer in the window has been
    # trashed, unshared or has aged out.
    stale = sorted(store.drive_ids(account) - seen)
    summary["deleted"] += store.drive_delete(account, stale)


def _follow_changes(api, store: Store, account: str, token: str, cutoff: str,
                    summary: dict) -> str:
    known = store.drive_ids(account)
    while True:
        page = api.changes(token)
        upserts, removals = [], []
        for change in page.get("changes", []):
            file_id = change.get("fileId")
            item = change.get("file") or {}
            if change.get("removed") or item.get("trashed"):
                if file_id in known:
                    removals.append(file_id)
            elif item.get("mimeType") != FOLDER and (file_id in known or in_window(item, cutoff)):
                upserts.append(parse_file(item))
        store.drive_upsert_files(account, upserts)
        known.update(record.id for record in upserts)
        summary["upserted"] += len(upserts)
        summary["deleted"] += store.drive_delete(account, removals)
        known.difference_update(removals)
        if page.get("nextPageToken"):
            token = page["nextPageToken"]
            continue
        return page.get("newStartPageToken") or token


def _export_pending(api, store: Store, account: str, summary: dict, progress: Progress,
                    max_exports: int | None) -> None:
    pending = store.drive_pending_exports(account, EXPORTABLE)
    if max_exports is not None:
        pending = pending[:max_exports]
    for count, row in enumerate(pending, start=1):
        try:
            text, note = _export_text(api, row["id"], row["mime_type"])
        except Exception as exc:  # one bad file must not stop the rest
            store.drive_set_export_error(account, row["id"], describe_http_error(exc))
            summary["export_failed"] += 1
        else:
            text, truncated = cap(text, TEXT_CAP)
            store.drive_set_text(account, row["id"], text, truncated, row["modified_time"], note)
            summary["exported"] += 1
        if count % PROGRESS_EVERY == 0:
            progress(f"drive: {account}: {count} of {len(pending)} files read")


def _export_text(api, file_id: str, mime_type: str) -> tuple[str, str | None]:
    """Returns the file's text and, for a partial read, a note saying what was left out."""
    if mime_type == DOC:
        markdown = api.export(file_id, "text/markdown").decode("utf-8", errors="replace")
        return tidy(strip_data_uris(markdown)), None
    if mime_type == SLIDES:
        return tidy(api.export(file_id, "text/plain").decode("utf-8", errors="replace")), None
    return _sheet_text(api, file_id)


def _sheet_text(api, file_id: str) -> tuple[str, str | None]:
    tabs = [
        tab
        for tab in api.sheet_tabs(file_id)
        if tab.get("sheetType", "GRID") == "GRID" and tab.get("title")
    ]
    notes = []
    if len(tabs) > SHEET_MAX_TABS:
        notes.append(f"partial: first {SHEET_MAX_TABS} of {len(tabs)} tabs")
        tabs = tabs[:SHEET_MAX_TABS]
    if not tabs:
        return "", None
    last_column = _column_letter(SHEET_MAX_COLUMNS)
    ranges = [f"{_quote_tab(tab['title'])}!A1:{last_column}{SHEET_MAX_ROWS}" for tab in tabs]
    parts = []
    for tab, rows in zip(tabs, api.sheet_values(file_id, ranges)):
        grid = tab.get("gridProperties") or {}
        if grid.get("rowCount", 0) > SHEET_MAX_ROWS or grid.get("columnCount", 0) > SHEET_MAX_COLUMNS:
            notes.append(f"partial: tab '{tab['title']}' cut to {SHEET_MAX_ROWS} rows, {SHEET_MAX_COLUMNS} columns")
        lines = [" | ".join(str(cell) for cell in row).rstrip(" |") for row in rows]
        body = "\n".join(line for line in lines if line.strip())
        if body:
            parts.append(f"## Tab: {tab['title']}\n{body}")
    return tidy("\n\n".join(parts)), "; ".join(notes) or None


def _quote_tab(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


def _column_letter(number: int) -> str:
    letters = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters
