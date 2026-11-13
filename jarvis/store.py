"""The local store: one SQLite file holding the mirror and a search index.

Per-source tables (``gmail_messages``, ``calendar_events``, ``drive_files``)
keep what was copied from Google. ``docs`` is the unified, searchable view of
all three (kind = mail | event | file) with an FTS5 index over title, body
and people. Every row is keyed by account.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from jarvis import paths

SCHEMA_VERSION = 1
KINDS = ("mail", "event", "file")
# Files indexed from folders on this Mac are stored under this pseudo-account.
LOCAL_ACCOUNT = "local"
NOTE_PREFIX = "note:"  # source_id of a meeting note Jarvis wrote
PREP_PREFIX = "prep:"  # source_id of a preparation sheet Jarvis wrote
ARTIFACT_KINDS = ("prep", "notes", "brief", "draft", "file", "folder")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gmail_messages (
    account        TEXT NOT NULL,
    id             TEXT NOT NULL,
    thread_id      TEXT NOT NULL,
    history_id     TEXT,
    internal_date  INTEGER NOT NULL,          -- milliseconds since the epoch
    subject        TEXT NOT NULL DEFAULT '',
    sender         TEXT NOT NULL DEFAULT '',
    recipients     TEXT NOT NULL DEFAULT '',  -- To
    cc             TEXT NOT NULL DEFAULT '',
    bcc            TEXT NOT NULL DEFAULT '',
    headers        TEXT NOT NULL DEFAULT '{}',-- JSON: the headers worth keeping
    labels         TEXT NOT NULL DEFAULT '[]',-- JSON array of label ids
    snippet        TEXT NOT NULL DEFAULT '',
    body           TEXT NOT NULL DEFAULT '',
    body_truncated INTEGER NOT NULL DEFAULT 0,
    attachments    TEXT NOT NULL DEFAULT '[]',-- JSON array of file names only
    size_estimate  INTEGER,
    fetched_at     INTEGER NOT NULL,
    PRIMARY KEY (account, id)
);
CREATE INDEX IF NOT EXISTS gmail_messages_thread ON gmail_messages(account, thread_id);
CREATE INDEX IF NOT EXISTS gmail_messages_date ON gmail_messages(account, internal_date);

CREATE TABLE IF NOT EXISTS calendars (
    account     TEXT NOT NULL,
    id          TEXT NOT NULL,
    summary     TEXT NOT NULL DEFAULT '',
    access_role TEXT NOT NULL DEFAULT '',
    is_primary  INTEGER NOT NULL DEFAULT 0,
    time_zone   TEXT,
    PRIMARY KEY (account, id)
);

CREATE TABLE IF NOT EXISTS calendar_events (
    account            TEXT NOT NULL,
    calendar_id        TEXT NOT NULL,
    id                 TEXT NOT NULL,
    status             TEXT,
    event_type         TEXT,
    summary            TEXT NOT NULL DEFAULT '',
    description        TEXT NOT NULL DEFAULT '',
    location           TEXT NOT NULL DEFAULT '',
    start_ts           INTEGER,                -- seconds since the epoch
    end_ts             INTEGER,
    start_raw          TEXT,
    end_raw            TEXT,
    all_day            INTEGER NOT NULL DEFAULT 0,
    organizer          TEXT NOT NULL DEFAULT '',
    attendees          TEXT NOT NULL DEFAULT '[]', -- JSON: email, name, response, ...
    my_response        TEXT,
    meeting_link       TEXT,
    html_link          TEXT,
    recurring_event_id TEXT,
    updated            TEXT,
    fetched_at         INTEGER NOT NULL,
    PRIMARY KEY (account, calendar_id, id)
);
CREATE INDEX IF NOT EXISTS calendar_events_start ON calendar_events(account, start_ts);

CREATE TABLE IF NOT EXISTS drive_files (
    account            TEXT NOT NULL,
    id                 TEXT NOT NULL,
    name               TEXT NOT NULL DEFAULT '',
    mime_type          TEXT NOT NULL DEFAULT '',
    modified_time      TEXT,
    viewed_by_me_time  TEXT,
    modified_by_me_time TEXT,
    created_time       TEXT,
    owners             TEXT NOT NULL DEFAULT '[]', -- JSON
    last_modifying_user TEXT NOT NULL DEFAULT '',
    web_view_link      TEXT,
    drive_id           TEXT,
    parents            TEXT NOT NULL DEFAULT '[]', -- JSON
    size               INTEGER,
    text               TEXT,                       -- exported text, NULL until exported
    text_truncated     INTEGER NOT NULL DEFAULT 0,
    text_modified_time TEXT,                       -- modified_time the text was exported at
    export_error       TEXT,
    export_attempts    INTEGER NOT NULL DEFAULT 0,
    fetched_at         INTEGER NOT NULL,
    PRIMARY KEY (account, id)
);

CREATE TABLE IF NOT EXISTS sync_state (
    account    TEXT NOT NULL,
    source     TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (account, source, key)
);

CREATE TABLE IF NOT EXISTS sync_runs (
    run_id      INTEGER PRIMARY KEY,
    account     TEXT NOT NULL,
    source      TEXT NOT NULL,
    started_at  INTEGER NOT NULL,
    finished_at INTEGER,
    status      TEXT NOT NULL DEFAULT 'running',  -- running | ok | error
    summary     TEXT NOT NULL DEFAULT '{}',       -- JSON counters
    error       TEXT
);
CREATE INDEX IF NOT EXISTS sync_runs_latest ON sync_runs(account, source, started_at);

CREATE TABLE IF NOT EXISTS projects (
    name       TEXT PRIMARY KEY,
    people     TEXT NOT NULL DEFAULT '[]',   -- JSON list of names
    folders    TEXT NOT NULL DEFAULT '[]',   -- JSON list of folders on this Mac
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS local_files (
    project TEXT NOT NULL,
    path    TEXT NOT NULL,                   -- absolute path
    mtime   REAL NOT NULL,
    size    INTEGER NOT NULL,
    PRIMARY KEY (project, path)
);

CREATE TABLE IF NOT EXISTS facts (
    id      INTEGER PRIMARY KEY,
    project TEXT,                    -- nullable: about the user's work in general
    text    TEXT NOT NULL,
    source  TEXT NOT NULL DEFAULT 'user',
    ts      INTEGER NOT NULL
);

-- What Jarvis made: preparation sheets, notes, briefs, drafts, files and folders.
CREATE TABLE IF NOT EXISTS artifacts (
    id      INTEGER PRIMARY KEY,
    kind    TEXT NOT NULL,
    title   TEXT NOT NULL,
    project TEXT,
    path    TEXT,                    -- a file or folder on this Mac, when there is one
    link    TEXT,
    body    TEXT NOT NULL DEFAULT '',
    meta    TEXT NOT NULL DEFAULT '{}',
    ts      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS artifacts_kind_ts ON artifacts(kind, ts);

-- What Jarvis did, for the dashboard.
CREATE TABLE IF NOT EXISTS activity (
    id   INTEGER PRIMARY KEY,
    ts   INTEGER NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL
);

-- Things the user asked to be reminded of. Jarvis speaks and shows them when due.
CREATE TABLE IF NOT EXISTS reminders (
    id      INTEGER PRIMARY KEY,
    due_ts  INTEGER NOT NULL,
    text    TEXT NOT NULL,
    done    INTEGER NOT NULL DEFAULT 0,
    ts      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS docs (
    doc_id    INTEGER PRIMARY KEY,
    account   TEXT NOT NULL,
    kind      TEXT NOT NULL CHECK (kind IN ('mail', 'event', 'file')),
    source_id TEXT NOT NULL,
    title     TEXT NOT NULL DEFAULT '',
    body      TEXT NOT NULL DEFAULT '',
    people    TEXT NOT NULL DEFAULT '',
    ts        INTEGER,                 -- seconds since the epoch
    link      TEXT,
    project   TEXT,                    -- nullable project tag
    UNIQUE (account, kind, source_id)
);
CREATE INDEX IF NOT EXISTS docs_kind_ts ON docs(kind, ts);
CREATE INDEX IF NOT EXISTS docs_project ON docs(project);

CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
    title, body, people,
    content='docs', content_rowid='doc_id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS docs_ai AFTER INSERT ON docs BEGIN
    INSERT INTO docs_fts(rowid, title, body, people)
    VALUES (new.doc_id, new.title, new.body, new.people);
END;
CREATE TRIGGER IF NOT EXISTS docs_ad AFTER DELETE ON docs BEGIN
    INSERT INTO docs_fts(docs_fts, rowid, title, body, people)
    VALUES ('delete', old.doc_id, old.title, old.body, old.people);
END;
CREATE TRIGGER IF NOT EXISTS docs_au AFTER UPDATE OF title, body, people ON docs BEGIN
    INSERT INTO docs_fts(docs_fts, rowid, title, body, people)
    VALUES ('delete', old.doc_id, old.title, old.body, old.people);
    INSERT INTO docs_fts(rowid, title, body, people)
    VALUES (new.doc_id, new.title, new.body, new.people);
END;
"""


@dataclass
class MailRecord:
    id: str
    thread_id: str
    internal_date: int  # milliseconds
    history_id: str | None = None
    subject: str = ""
    sender: str = ""
    recipients: str = ""
    cc: str = ""
    bcc: str = ""
    headers: dict = field(default_factory=dict)
    labels: list[str] = field(default_factory=list)
    snippet: str = ""
    body: str = ""
    body_truncated: bool = False
    attachments: list[str] = field(default_factory=list)
    size_estimate: int | None = None


@dataclass
class EventRecord:
    id: str
    status: str | None = None
    event_type: str | None = None
    summary: str = ""
    description: str = ""
    location: str = ""
    start_ts: int | None = None
    end_ts: int | None = None
    start_raw: str | None = None
    end_raw: str | None = None
    all_day: bool = False
    organizer: str = ""
    attendees: list[dict] = field(default_factory=list)
    my_response: str | None = None
    meeting_link: str | None = None
    html_link: str | None = None
    recurring_event_id: str | None = None
    updated: str | None = None
    indexed: bool = True  # False keeps the event out of the search index


@dataclass
class FileRecord:
    id: str
    name: str = ""
    mime_type: str = ""
    modified_time: str | None = None
    viewed_by_me_time: str | None = None
    modified_by_me_time: str | None = None
    created_time: str | None = None
    owners: list[dict] = field(default_factory=list)
    last_modifying_user: str = ""
    web_view_link: str | None = None
    drive_id: str | None = None
    parents: list[str] = field(default_factory=list)
    size: int | None = None


@dataclass(frozen=True)
class SearchHit:
    doc_id: int
    account: str
    kind: str
    source_id: str
    title: str
    people: str
    ts: int | None
    link: str | None
    project: str | None
    snippet: str


@dataclass(frozen=True)
class Doc:
    doc_id: int
    account: str
    kind: str
    source_id: str
    title: str
    body: str
    people: str
    ts: int | None
    link: str | None
    project: str | None


def default_db_path() -> Path:
    return paths.data_dir() / "jarvis.db"


def open_store(path: Path | str | None = None) -> "Store":
    """Open the store at the default data path, creating the folder if needed."""
    if path is None:
        paths.ensure_data_dir()
        path = default_db_path()
    return Store(path)


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        # Autocommit mode; writes go through ``_tx`` so they are short and explicit.
        self.db = sqlite3.connect(str(self.path), isolation_level=None, timeout=60)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA busy_timeout=60000")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._create_schema()

    def _create_schema(self) -> None:
        # executescript commits implicitly, so it runs outside ``_tx``.
        self.db.executescript(_SCHEMA)
        self.db.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def _tx(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    # --- sync state --------------------------------------------------------

    def get_state(self, account: str, source: str, key: str, default: str | None = None) -> str | None:
        row = self.db.execute(
            "SELECT value FROM sync_state WHERE account=? AND source=? AND key=?",
            (account, source, key),
        ).fetchone()
        return row["value"] if row else default

    def set_state(self, account: str, source: str, key: str, value: str | int) -> None:
        self.db.execute(
            "INSERT INTO sync_state(account, source, key, value, updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(account, source, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (account, source, key, str(value), int(time.time())),
        )

    def delete_state(self, account: str, source: str, key: str) -> None:
        self.db.execute(
            "DELETE FROM sync_state WHERE account=? AND source=? AND key=?",
            (account, source, key),
        )

    def start_run(self, account: str, source: str) -> int:
        cursor = self.db.execute(
            "INSERT INTO sync_runs(account, source, started_at) VALUES (?,?,?)",
            (account, source, int(time.time())),
        )
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, status: str, summary: dict, error: str | None = None) -> None:
        self.db.execute(
            "UPDATE sync_runs SET finished_at=?, status=?, summary=?, error=? WHERE run_id=?",
            (int(time.time()), status, json.dumps(summary), error, run_id),
        )

    def last_run(self, account: str, source: str, status: str | None = None) -> sqlite3.Row | None:
        sql = "SELECT * FROM sync_runs WHERE account=? AND source=?"
        args: list = [account, source]
        if status:
            sql += " AND status=?"
            args.append(status)
        return self.db.execute(sql + " ORDER BY started_at DESC, run_id DESC LIMIT 1", args).fetchone()

    # --- gmail -------------------------------------------------------------

    def gmail_missing_ids(self, account: str, ids: list[str]) -> list[str]:
        """Return the ids from ``ids`` that are not stored yet, in the same order."""
        known: set[str] = set()
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            marks = ",".join("?" * len(chunk))
            known.update(
                row[0]
                for row in self.db.execute(
                    f"SELECT id FROM gmail_messages WHERE account=? AND id IN ({marks})",
                    [account, *chunk],
                )
            )
        return [message_id for message_id in ids if message_id not in known]

    def gmail_ids_since(self, account: str, after_ms: int) -> set[str]:
        return {
            row[0]
            for row in self.db.execute(
                "SELECT id FROM gmail_messages WHERE account=? AND internal_date>=?",
                (account, after_ms),
            )
        }

    def gmail_has(self, account: str, message_id: str) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM gmail_messages WHERE account=? AND id=?", (account, message_id)
            ).fetchone()
            is not None
        )

    def gmail_upsert(self, account: str, records: list[MailRecord]) -> None:
        if not records:
            return
        now = int(time.time())
        with self._tx():
            for r in records:
                self.db.execute(
                    "INSERT INTO gmail_messages(account, id, thread_id, history_id, internal_date, subject, "
                    "sender, recipients, cc, bcc, headers, labels, snippet, body, body_truncated, attachments, "
                    "size_estimate, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(account, id) DO UPDATE SET thread_id=excluded.thread_id, "
                    "history_id=excluded.history_id, internal_date=excluded.internal_date, subject=excluded.subject, "
                    "sender=excluded.sender, recipients=excluded.recipients, cc=excluded.cc, bcc=excluded.bcc, "
                    "headers=excluded.headers, labels=excluded.labels, snippet=excluded.snippet, body=excluded.body, "
                    "body_truncated=excluded.body_truncated, attachments=excluded.attachments, "
                    "size_estimate=excluded.size_estimate, fetched_at=excluded.fetched_at",
                    (
                        account, r.id, r.thread_id, r.history_id, r.internal_date, r.subject, r.sender,
                        r.recipients, r.cc, r.bcc, json.dumps(r.headers), json.dumps(r.labels), r.snippet,
                        r.body, int(r.body_truncated), json.dumps(r.attachments), r.size_estimate, now,
                    ),
                )
                people = "; ".join(part for part in (r.sender, r.recipients, r.cc) if part)
                body = r.body or r.snippet
                if r.attachments:
                    body = f"{body}\n\nAttachments: {', '.join(r.attachments)}"
                self._upsert_doc(
                    account, "mail", r.id, r.subject, body, people, r.internal_date // 1000,
                    mail_link(account, r.thread_id),
                )

    def gmail_update_labels(self, account: str, message_id: str, labels: list[str]) -> None:
        self.db.execute(
            "UPDATE gmail_messages SET labels=? WHERE account=? AND id=?",
            (json.dumps(labels), account, message_id),
        )

    def gmail_delete(self, account: str, ids: list[str]) -> int:
        removed = 0
        with self._tx():
            for message_id in ids:
                cursor = self.db.execute(
                    "DELETE FROM gmail_messages WHERE account=? AND id=?", (account, message_id)
                )
                removed += cursor.rowcount
                self._delete_doc(account, "mail", message_id)
        return removed

    def gmail_get(self, account: str, message_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT id, thread_id, subject, sender, recipients, cc, headers, internal_date "
            "FROM gmail_messages WHERE account=? AND id=?",
            (account, message_id),
        ).fetchone()

    def mail_thread(self, account: str, message_id: str) -> list[sqlite3.Row]:
        """Other stored messages in the same thread, oldest first."""
        return self.db.execute(
            "SELECT d.doc_id, m.id, m.subject, m.sender, m.internal_date FROM gmail_messages m "
            "JOIN docs d ON d.account=m.account AND d.kind='mail' AND d.source_id=m.id "
            "WHERE m.account=? AND m.thread_id=(SELECT thread_id FROM gmail_messages WHERE account=? AND id=?) "
            "ORDER BY m.internal_date",
            (account, account, message_id),
        ).fetchall()

    # --- calendar ----------------------------------------------------------

    def calendar_set_calendars(self, account: str, calendars: list[dict]) -> None:
        with self._tx():
            self.db.execute("DELETE FROM calendars WHERE account=?", (account,))
            for c in calendars:
                self.db.execute(
                    "INSERT INTO calendars(account, id, summary, access_role, is_primary, time_zone) VALUES (?,?,?,?,?,?)",
                    (
                        account, c["id"], c.get("summaryOverride") or c.get("summary", ""),
                        c.get("accessRole", ""), int(bool(c.get("primary"))), c.get("timeZone"),
                    ),
                )

    def calendar_event_ids(self, account: str, calendar_id: str) -> set[str]:
        return {
            row[0]
            for row in self.db.execute(
                "SELECT id FROM calendar_events WHERE account=? AND calendar_id=?", (account, calendar_id)
            )
        }

    def calendar_upsert_events(self, account: str, calendar_id: str, records: list[EventRecord]) -> None:
        if not records:
            return
        now = int(time.time())
        with self._tx():
            for r in records:
                self.db.execute(
                    "INSERT INTO calendar_events(account, calendar_id, id, status, event_type, summary, description, "
                    "location, start_ts, end_ts, start_raw, end_raw, all_day, organizer, attendees, my_response, "
                    "meeting_link, html_link, recurring_event_id, updated, fetched_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(account, calendar_id, id) DO UPDATE SET status=excluded.status, "
                    "event_type=excluded.event_type, summary=excluded.summary, description=excluded.description, "
                    "location=excluded.location, start_ts=excluded.start_ts, end_ts=excluded.end_ts, "
                    "start_raw=excluded.start_raw, end_raw=excluded.end_raw, all_day=excluded.all_day, "
                    "organizer=excluded.organizer, attendees=excluded.attendees, my_response=excluded.my_response, "
                    "meeting_link=excluded.meeting_link, html_link=excluded.html_link, "
                    "recurring_event_id=excluded.recurring_event_id, updated=excluded.updated, "
                    "fetched_at=excluded.fetched_at",
                    (
                        account, calendar_id, r.id, r.status, r.event_type, r.summary, r.description, r.location,
                        r.start_ts, r.end_ts, r.start_raw, r.end_raw, int(r.all_day), r.organizer,
                        json.dumps(r.attendees), r.my_response, r.meeting_link, r.html_link,
                        r.recurring_event_id, r.updated, now,
                    ),
                )
                source_id = f"{calendar_id}:{r.id}"
                if not r.indexed:
                    self._delete_doc(account, "event", source_id)
                    continue
                people = "; ".join(
                    part
                    for part in [r.organizer]
                    + [_person(a.get("name"), a.get("email")) for a in r.attendees]
                    if part
                )
                body_parts = [r.description]
                if r.location:
                    body_parts.append(f"Location: {r.location}")
                if r.meeting_link:
                    body_parts.append(f"Meeting link: {r.meeting_link}")
                if r.start_raw:
                    body_parts.append(f"When: {r.start_raw} to {r.end_raw or '?'}")
                self._upsert_doc(
                    account, "event", source_id, r.summary,
                    "\n".join(part for part in body_parts if part), people, r.start_ts, r.html_link,
                )

    def calendar_delete_events(self, account: str, calendar_id: str, ids: list[str]) -> int:
        removed = 0
        with self._tx():
            for event_id in ids:
                cursor = self.db.execute(
                    "DELETE FROM calendar_events WHERE account=? AND calendar_id=? AND id=?",
                    (account, calendar_id, event_id),
                )
                removed += cursor.rowcount
                self._delete_doc(account, "event", f"{calendar_id}:{event_id}")
        return removed

    def calendar_drop_calendar(self, account: str, calendar_id: str) -> int:
        return self.calendar_delete_events(
            account, calendar_id, sorted(self.calendar_event_ids(account, calendar_id))
        )

    # --- drive -------------------------------------------------------------

    def drive_upsert_files(self, account: str, records: list[FileRecord]) -> None:
        if not records:
            return
        now = int(time.time())
        with self._tx():
            for r in records:
                self.db.execute(
                    "INSERT INTO drive_files(account, id, name, mime_type, modified_time, viewed_by_me_time, "
                    "modified_by_me_time, created_time, owners, last_modifying_user, web_view_link, drive_id, "
                    "parents, size, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(account, id) DO UPDATE SET name=excluded.name, mime_type=excluded.mime_type, "
                    "modified_time=excluded.modified_time, viewed_by_me_time=excluded.viewed_by_me_time, "
                    "modified_by_me_time=excluded.modified_by_me_time, created_time=excluded.created_time, "
                    "owners=excluded.owners, last_modifying_user=excluded.last_modifying_user, "
                    "web_view_link=excluded.web_view_link, drive_id=excluded.drive_id, parents=excluded.parents, "
                    "size=excluded.size, fetched_at=excluded.fetched_at",
                    (
                        account, r.id, r.name, r.mime_type, r.modified_time, r.viewed_by_me_time,
                        r.modified_by_me_time, r.created_time, json.dumps(r.owners), r.last_modifying_user,
                        r.web_view_link, r.drive_id, json.dumps(r.parents), r.size, now,
                    ),
                )
                self._refresh_file_doc(account, r.id)

    def drive_set_text(self, account: str, file_id: str, text: str, truncated: bool,
                       modified_time: str | None, note: str | None = None) -> None:
        """Store exported text. ``note`` records a partial export (kept as export_error)."""
        with self._tx():
            self.db.execute(
                "UPDATE drive_files SET text=?, text_truncated=?, text_modified_time=?, export_error=? "
                "WHERE account=? AND id=?",
                (text, int(truncated), modified_time, note, account, file_id),
            )
            self._refresh_file_doc(account, file_id)

    def drive_requeue_exports(self, account: str, note_prefix: str) -> int:
        """Mark files whose export note starts with ``note_prefix`` for a fresh export."""
        cursor = self.db.execute(
            "UPDATE drive_files SET text_modified_time=NULL, export_error=NULL, export_attempts=0 "
            "WHERE account=? AND export_error LIKE ?",
            (account, note_prefix + "%"),
        )
        return cursor.rowcount

    def drive_set_export_error(self, account: str, file_id: str, error: str) -> None:
        self.db.execute(
            "UPDATE drive_files SET export_error=?, export_attempts=export_attempts+1 WHERE account=? AND id=?",
            (error[:500], account, file_id),
        )

    def drive_delete(self, account: str, ids: list[str]) -> int:
        removed = 0
        with self._tx():
            for file_id in ids:
                cursor = self.db.execute(
                    "DELETE FROM drive_files WHERE account=? AND id=?", (account, file_id)
                )
                removed += cursor.rowcount
                self._delete_doc(account, "file", file_id)
        return removed

    def drive_get(self, account: str, file_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM drive_files WHERE account=? AND id=?", (account, file_id)
        ).fetchone()

    def drive_ids(self, account: str) -> set[str]:
        return {row[0] for row in self.db.execute("SELECT id FROM drive_files WHERE account=?", (account,))}

    def drive_pending_exports(self, account: str, mime_types: tuple[str, ...], max_attempts: int = 3) -> list[sqlite3.Row]:
        """Exportable files whose text is missing or older than the file."""
        marks = ",".join("?" * len(mime_types))
        return self.db.execute(
            f"SELECT * FROM drive_files WHERE account=? AND mime_type IN ({marks}) "
            "AND (text_modified_time IS NULL OR text_modified_time IS NOT modified_time) "
            "AND export_attempts < ? ORDER BY COALESCE(viewed_by_me_time, modified_time) DESC",
            [account, *mime_types, max_attempts],
        ).fetchall()

    def _refresh_file_doc(self, account: str, file_id: str) -> None:
        row = self.drive_get(account, file_id)
        if row is None:
            return
        owners = [_person(o.get("displayName"), o.get("emailAddress")) for o in json.loads(row["owners"])]
        people = "; ".join(part for part in owners + [row["last_modifying_user"]] if part)
        self._upsert_doc(
            account, "file", file_id, row["name"], row["text"] or "", people,
            iso_to_ts(row["modified_time"]), row["web_view_link"],
        )

    # --- docs --------------------------------------------------------------

    def _upsert_doc(self, account: str, kind: str, source_id: str, title: str, body: str,
                    people: str, ts: int | None, link: str | None) -> None:
        self.db.execute(
            "INSERT INTO docs(account, kind, source_id, title, body, people, ts, link) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(account, kind, source_id) DO UPDATE SET title=excluded.title, body=excluded.body, "
            "people=excluded.people, ts=excluded.ts, link=excluded.link",
            (account, kind, source_id, title or "", body or "", people or "", ts, link),
        )

    def _delete_doc(self, account: str, kind: str, source_id: str) -> None:
        self.db.execute(
            "DELETE FROM docs WHERE account=? AND kind=? AND source_id=?", (account, kind, source_id)
        )

    def get_doc(self, doc_id: int) -> Doc | None:
        row = self.db.execute(
            "SELECT doc_id, account, kind, source_id, title, body, people, ts, link, project FROM docs WHERE doc_id=?",
            (doc_id,),
        ).fetchone()
        return Doc(**dict(row)) if row else None

    def search(
        self,
        query: str = "",
        *,
        kind: str | None = None,
        project: str | None = None,
        account: str | None = None,
        after: int | None = None,
        before: int | None = None,
        limit: int = 10,
        order: str = "relevance",
    ) -> list[SearchHit]:
        """Full-text search over ``docs``.

        With an empty ``query`` the filters alone select rows, ordered by time.
        ``order`` is ``relevance``, ``newest`` or ``oldest``.
        """
        if kind is not None and kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        filters, args = [], []
        for column, value in (("d.kind", kind), ("d.project", project), ("d.account", account)):
            if value is not None:
                filters.append(f"{column} = ?")
                args.append(value)
        if after is not None:
            filters.append("d.ts >= ?")
            args.append(after)
        if before is not None:
            filters.append("d.ts < ?")
            args.append(before)
        limit = max(1, min(int(limit), 100))
        time_order = "d.ts ASC" if order == "oldest" else "d.ts DESC"

        match_all = build_match(query, "AND")
        if match_all is None:
            where = f"WHERE {' AND '.join(filters)}" if filters else ""
            rows = self.db.execute(
                "SELECT d.doc_id, d.account, d.kind, d.source_id, d.title, d.people, d.ts, d.link, d.project, "
                f"substr(d.body, 1, 200) AS snippet FROM docs d {where} ORDER BY {time_order} LIMIT ?",
                [*args, limit],
            ).fetchall()
            return [SearchHit(**dict(row)) for row in rows]

        hits: list[SearchHit] = []
        seen: set[int] = set()
        for match in dict.fromkeys([match_all, build_match(query, "OR")]):
            if match is None or len(hits) >= limit:
                continue
            ranking = "bm25(docs_fts, 6.0, 1.0, 3.0)" if order == "relevance" else time_order
            where = " AND ".join(["docs_fts MATCH ?", *filters])
            try:
                rows = self.db.execute(
                    "SELECT d.doc_id, d.account, d.kind, d.source_id, d.title, d.people, d.ts, d.link, d.project, "
                    "snippet(docs_fts, -1, '[', ']', ' ... ', 30) AS snippet "
                    f"FROM docs_fts JOIN docs d ON d.doc_id = docs_fts.rowid WHERE {where} "
                    f"ORDER BY {ranking} LIMIT ?",
                    [match, *args, limit],
                ).fetchall()
            except sqlite3.OperationalError:
                continue  # a query FTS5 cannot parse matches nothing
            for row in rows:
                if row["doc_id"] not in seen and len(hits) < limit:
                    seen.add(row["doc_id"])
                    hits.append(SearchHit(**dict(row)))
        return hits

    def tag_projects(self, project_names: list[str]) -> dict[str, int]:
        """Tag docs whose title or body names a project as a whole word.

        Conservative on purpose: a doc that names no project, or more than
        one, keeps a NULL tag. Returns the number of docs per project.
        """
        counts: dict[str, int] = {}
        matches: dict[int, set[str]] = {}
        for name in project_names:
            match = build_match(f'"{name}"', "AND")
            if match is None:
                continue
            pattern = re.compile(rf"(?<!\w){re.escape(name)}(?!\w)")
            for row in self.db.execute(
                "SELECT d.doc_id, d.title, d.body FROM docs_fts JOIN docs d ON d.doc_id = docs_fts.rowid "
                "WHERE docs_fts MATCH ? AND d.account != 'local'",
                (match,),
            ):
                # FTS is case-insensitive; project names are matched case-sensitively.
                if pattern.search(row["title"]) or pattern.search(row["body"]):
                    matches.setdefault(row["doc_id"], set()).add(name)
        with self._tx():
            self.db.execute(
                "UPDATE docs SET project=NULL WHERE project IS NOT NULL AND account != 'local'"
            )
            for doc_id, names in matches.items():
                if len(names) == 1:
                    name = next(iter(names))
                    self.db.execute("UPDATE docs SET project=? WHERE doc_id=?", (name, doc_id))
                    counts[name] = counts.get(name, 0) + 1
        return counts

    # --- projects and their folders ------------------------------------------

    def project_rows(self) -> list[dict]:
        return [
            {"name": row["name"], "people": json.loads(row["people"]), "folders": json.loads(row["folders"])}
            for row in self.db.execute("SELECT name, people, folders FROM projects ORDER BY created_at, name")
        ]

    def project_save(self, name: str, people: list[str], folders: list[str]) -> None:
        with self._tx():
            self.db.execute(
                "INSERT INTO projects(name, people, folders, created_at) VALUES (?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET people=excluded.people, folders=excluded.folders",
                (name, json.dumps(people), json.dumps(folders), int(time.time())),
            )

    def project_delete(self, name: str) -> bool:
        with self._tx():
            self.local_delete(name, list(self.local_known(name)))
            return self.db.execute("DELETE FROM projects WHERE name=?", (name,)).rowcount > 0

    def local_known(self, project: str) -> dict[str, tuple[float, int]]:
        """Indexed files of a project: path -> (mtime, size)."""
        return {
            row["path"]: (row["mtime"], row["size"])
            for row in self.db.execute("SELECT path, mtime, size FROM local_files WHERE project=?", (project,))
        }

    def local_upsert(self, project: str, path: str, title: str, body: str, mtime: float, size: int) -> None:
        source_id = f"{project}:{path}"
        with self._tx():
            self.db.execute(
                "INSERT INTO local_files(project, path, mtime, size) VALUES (?,?,?,?) "
                "ON CONFLICT(project, path) DO UPDATE SET mtime=excluded.mtime, size=excluded.size",
                (project, path, mtime, size),
            )
            self._upsert_doc(LOCAL_ACCOUNT, "file", source_id, title, body, "", int(mtime), f"file://{path}")
            self.db.execute(
                "UPDATE docs SET project=? WHERE account=? AND kind='file' AND source_id=?",
                (project, LOCAL_ACCOUNT, source_id),
            )

    def local_delete(self, project: str, paths: list[str]) -> int:
        removed = 0
        for path in paths:
            removed += self.db.execute(
                "DELETE FROM local_files WHERE project=? AND path=?", (project, path)
            ).rowcount
            self._delete_doc(LOCAL_ACCOUNT, "file", f"{project}:{path}")
        return removed

    # --- notes Jarvis writes itself ----------------------------------------

    def note_upsert(self, path: str, title: str, body: str, ts: int, project: str | None,
                    prefix: str = NOTE_PREFIX) -> None:
        """Index a meeting note (or, with ``PREP_PREFIX``, a preparation sheet) so later questions can find it.

        These are kept apart from ``local_files``: a folder sync removes what is
        no longer in a project's folders, and Jarvis's own writing does not live in one.
        """
        source_id = f"{prefix}{path}"
        with self._tx():
            self._upsert_doc(LOCAL_ACCOUNT, "file", source_id, title, body, "", ts, f"file://{path}")
            self.db.execute(
                "UPDATE docs SET project=? WHERE account=? AND kind='file' AND source_id=?",
                (project, LOCAL_ACCOUNT, source_id),
            )

    def notes(self, project: str | None = None, limit: int = 10) -> list[Doc]:
        """Meeting notes, newest first."""
        where, args = "", [LOCAL_ACCOUNT, f"{NOTE_PREFIX}%"]
        if project is not None:
            where = " AND project=?"
            args.append(project)
        rows = self.db.execute(
            "SELECT doc_id, account, kind, source_id, title, body, people, ts, link, project FROM docs "
            f"WHERE account=? AND kind='file' AND source_id LIKE ?{where} ORDER BY ts DESC LIMIT ?",
            [*args, limit],
        ).fetchall()
        return [Doc(**dict(row)) for row in rows]

    def note_delete(self, path: str) -> None:
        with self._tx():
            self._delete_doc(LOCAL_ACCOUNT, "file", f"{NOTE_PREFIX}{path}")

    # --- what the user told Jarvis to remember -----------------------------------

    def fact_add(self, text: str, project: str | None = None, source: str = "user") -> int:
        with self._tx():
            cursor = self.db.execute(
                "INSERT INTO facts(project, text, source, ts) VALUES (?,?,?,?)",
                (project, text.strip(), source, int(time.time())),
            )
        return int(cursor.lastrowid)

    def fact_delete(self, fact_id: int) -> bool:
        with self._tx():
            return self.db.execute("DELETE FROM facts WHERE id=?", (fact_id,)).rowcount > 0

    def facts(self, project: str | None = None, limit: int = 60) -> list[dict]:
        """Newest first. With ``project``: that project's facts and the general ones."""
        where, args = "", []
        if project is not None:
            where = "WHERE project=? OR project IS NULL"
            args.append(project)
        rows = self.db.execute(
            f"SELECT id, project, text, source, ts FROM facts {where} ORDER BY ts DESC, id DESC LIMIT ?",
            [*args, limit],
        ).fetchall()
        return [dict(row) for row in rows]

    # --- reminders ---------------------------------------------------------------

    def reminder_add(self, due_ts: int, text: str) -> int:
        with self._tx():
            cursor = self.db.execute(
                "INSERT INTO reminders(due_ts, text, ts) VALUES (?,?,?)", (int(due_ts), text.strip(), int(time.time()))
            )
        return int(cursor.lastrowid)

    def reminders(self, *, due_by: int | None = None) -> list[dict]:
        """Reminders still to come, soonest first. With ``due_by``: only those due by then."""
        where, args = "", []
        if due_by is not None:
            where = " AND due_ts <= ?"
            args.append(int(due_by))
        rows = self.db.execute(
            f"SELECT id, due_ts, text FROM reminders WHERE done=0{where} ORDER BY due_ts, id", args
        ).fetchall()
        return [dict(row) for row in rows]

    def reminder_done(self, reminder_id: int) -> bool:
        with self._tx():
            return self.db.execute("UPDATE reminders SET done=1 WHERE id=? AND done=0", (reminder_id,)).rowcount > 0

    # --- what Jarvis made and did ------------------------------------------------

    def artifact_add(self, kind: str, title: str, *, project: str | None = None, path: str | None = None,
                     link: str | None = None, body: str = "", meta: dict | None = None,
                     ts: int | None = None) -> int:
        if kind not in ARTIFACT_KINDS:
            raise ValueError(f"kind must be one of {', '.join(ARTIFACT_KINDS)}")
        with self._tx():
            cursor = self.db.execute(
                "INSERT INTO artifacts(kind, title, project, path, link, body, meta, ts) VALUES (?,?,?,?,?,?,?,?)",
                (kind, title, project, path, link, body, json.dumps(meta or {}), ts or int(time.time())),
            )
        return int(cursor.lastrowid)

    def artifacts(self, kind: str | None = None, *, project: str | None = None, since: int | None = None,
                  limit: int = 50) -> list[dict]:
        """Newest first."""
        filters, args = [], []
        for column, value in (("kind", kind), ("project", project)):
            if value is not None:
                filters.append(f"{column}=?")
                args.append(value)
        if since is not None:
            filters.append("ts >= ?")
            args.append(since)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        rows = self.db.execute(
            f"SELECT id, kind, title, project, path, link, body, meta, ts FROM artifacts {where} "
            "ORDER BY ts DESC, id DESC LIMIT ?",
            [*args, limit],
        ).fetchall()
        return [{**dict(row), "meta": json.loads(row["meta"] or "{}")} for row in rows]

    def artifact_change(self, artifact_id: int, *, title: str, project: str | None) -> None:
        with self._tx():
            self.db.execute("UPDATE artifacts SET title=?, project=? WHERE id=?", (title, project, artifact_id))

    def artifact_delete(self, artifact_id: int) -> None:
        with self._tx():
            self.db.execute("DELETE FROM artifacts WHERE id=?", (artifact_id,))

    def artifact_counts(self) -> dict[str, int]:
        return {row[0]: row[1] for row in self.db.execute("SELECT kind, COUNT(*) FROM artifacts GROUP BY kind")}

    def artifact_made(self, path: str) -> bool:
        """True when Jarvis itself created the file or folder at ``path``."""
        return self.db.execute("SELECT 1 FROM artifacts WHERE path=? LIMIT 1", (path,)).fetchone() is not None

    def log(self, kind: str, text: str) -> None:
        with self._tx():
            self.db.execute("INSERT INTO activity(ts, kind, text) VALUES (?,?,?)", (int(time.time()), kind, text))

    def activity(self, limit: int = 200) -> list[dict]:
        rows = self.db.execute(
            "SELECT ts, kind, text FROM activity ORDER BY ts DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    # --- status ------------------------------------------------------------

    def stats(self) -> dict:
        def one(sql: str, args: tuple = ()) -> sqlite3.Row:
            return self.db.execute(sql, args).fetchone()

        accounts = sorted(
            {row[0] for row in self.db.execute("SELECT DISTINCT account FROM sync_state")}
            | {row[0] for row in self.db.execute("SELECT DISTINCT account FROM docs")}
        )
        accounts = [account for account in accounts if account != LOCAL_ACCOUNT]
        result: dict = {"path": str(self.path), "size_bytes": self.path.stat().st_size, "accounts": {}}
        result["folders"] = {
            row[0]: row[1]
            for row in self.db.execute("SELECT project, COUNT(*) FROM local_files GROUP BY project")
        }
        for account in accounts:
            mail = one(
                "SELECT COUNT(*) n, MIN(internal_date) lo, MAX(internal_date) hi, COUNT(DISTINCT thread_id) threads "
                "FROM gmail_messages WHERE account=?", (account,))
            events = one(
                "SELECT COUNT(*) n, MIN(start_ts) lo, MAX(start_ts) hi FROM calendar_events WHERE account=?",
                (account,))
            files = one(
                "SELECT COUNT(*) n, SUM(text IS NOT NULL) with_text, SUM(export_error IS NOT NULL) failed, "
                "MIN(modified_time) lo, MAX(modified_time) hi FROM drive_files WHERE account=?", (account,))
            projects = {
                row[0]: row[1]
                for row in self.db.execute(
                    "SELECT project, COUNT(*) FROM docs WHERE account=? AND project IS NOT NULL GROUP BY project",
                    (account,))
            }
            result["accounts"][account] = {
                "mail": {"count": mail["n"], "threads": mail["threads"],
                         "oldest": _ms(mail["lo"]), "newest": _ms(mail["hi"])},
                "events": {"count": events["n"], "oldest": events["lo"], "newest": events["hi"],
                           "calendars": one("SELECT COUNT(*) FROM calendars WHERE account=?", (account,))[0]},
                "files": {"count": files["n"], "with_text": files["with_text"] or 0,
                          "export_failures": files["failed"] or 0,
                          "oldest": iso_to_ts(files["lo"]), "newest": iso_to_ts(files["hi"])},
                "docs": one("SELECT COUNT(*) FROM docs WHERE account=?", (account,))[0],
                "projects": projects,
                "state": {
                    f"{row['source']}.{row['key']}": row["value"]
                    for row in self.db.execute(
                        "SELECT source, key, value FROM sync_state WHERE account=? AND key NOT LIKE '%token%' "
                        "ORDER BY source, key", (account,))
                },
                "last_runs": {
                    source: _run(self.last_run(account, source))
                    for source in ("gmail", "calendar", "drive")
                },
            }
        return result


# --- helpers ---------------------------------------------------------------

_QUERY_TOKEN = re.compile(r'"([^"]*)"|(\S+)')
_WORD = re.compile(r"\w+", re.UNICODE)


def build_match(query: str, joiner: str = "AND") -> str | None:
    """Turn free text into a safe FTS5 MATCH expression, or None if empty.

    Every word becomes a quoted term, so FTS5 operators and punctuation in the
    input cannot change the query. Text in double quotes stays a phrase.
    """
    terms: list[str] = []
    for phrase, word in _QUERY_TOKEN.findall(query or ""):
        words = _WORD.findall(phrase or word)
        if words:
            terms.append('"' + " ".join(words) + '"')
    if not terms:
        return None
    return f" {joiner} ".join(dict.fromkeys(terms))


def mail_link(account: str, thread_id: str) -> str:
    return f"https://mail.google.com/mail/?authuser={account}#all/{thread_id}"


def iso_to_ts(value: str | None) -> int | None:
    """Seconds since the epoch for an RFC 3339 timestamp, or None."""
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _person(name: str | None, email: str | None) -> str:
    if name and email:
        return f"{name} <{email}>"
    return name or email or ""


def _ms(value: int | None) -> int | None:
    return None if value is None else value // 1000


def _run(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {
        "started_at": row["started_at"], "finished_at": row["finished_at"], "status": row["status"],
        "summary": json.loads(row["summary"] or "{}"), "error": row["error"],
    }
