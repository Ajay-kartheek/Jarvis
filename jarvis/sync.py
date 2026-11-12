"""Run the mirrors for the configured accounts."""

from __future__ import annotations

import fcntl
import re
import time
from collections.abc import Callable
from contextlib import contextmanager

from jarvis import google_auth, paths, projects
from jarvis.config import Config
from jarvis.store import Store, open_store

SOURCES = ("gmail", "calendar", "drive")
# Project folders on this Mac are synced once, not per account.
FOLDERS = "folders"

Progress = Callable[[str], None]


@contextmanager
def source_lock(account: str, source: str):
    """Hold a per-account, per-source lock. Yields False if another sync has it."""
    folder = paths.ensure_data_dir() / "locks"
    folder.mkdir(exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", account.lower()).strip("-")
    handle = open(folder / f"{slug}.{source}.lock", "w")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        handle.close()


def run_sync(
    config: Config,
    *,
    only: str | None = None,
    account: str | None = None,
    progress: Progress = print,
    max_messages: int | None = None,
    store: Store | None = None,
) -> list[dict]:
    """Sync every configured account (or one) and return a result per source."""
    if only is not None and only not in (*SOURCES, FOLDERS):
        raise ValueError(f"--only must be one of {', '.join((*SOURCES, FOLDERS))}")
    results: list[dict] = []
    own_store = store is None
    store = store or open_store()
    try:
        for entry in config.accounts if only != FOLDERS else ():
            email = google_auth.normalise_email(entry.email)
            if account and google_auth.normalise_email(account) != email:
                continue
            try:
                creds = google_auth.load_credentials(email)
            except Exception as exc:
                creds, problem = None, f"sign-in no longer works: {google_auth.describe_error(exc)}"
            else:
                problem = "not signed in"
            if creds is None:
                progress(f"{email}: {problem}")
                results.append({"account": email, "source": "*", "status": "error", "error": problem})
                continue
            for source in SOURCES:
                if only and source != only:
                    continue
                results.append(_run_source(store, config, email, source, creds, progress, max_messages))
        known_projects = projects.load(store, config)
        if only in (None, FOLDERS):
            results.append(_run_folders(store, known_projects, progress))
        store.tag_projects([project.name for project in known_projects])
    finally:
        if own_store:
            store.close()
    return results


def _run_source(store: Store, config: Config, email: str, source: str, creds,
                progress: Progress, max_messages: int | None) -> dict:
    result = {"account": email, "source": source}
    with source_lock(email, source) as acquired:
        if not acquired:
            progress(f"{source}: another sync is already running, skipped")
            return {**result, "status": "busy"}
        run_id = store.start_run(email, source)
        started = time.monotonic()
        try:
            summary = _dispatch(store, config, email, source, creds, progress, max_messages)
        except BaseException as exc:
            status = "error" if isinstance(exc, Exception) else "interrupted"
            detail = google_auth.describe_error(exc)
            store.finish_run(run_id, status, {"seconds": round(time.monotonic() - started, 1)}, detail)
            if not isinstance(exc, Exception):
                raise
            progress(f"{source}: failed: {detail}")
            return {**result, "status": "error", "error": detail}
        summary["seconds"] = round(time.monotonic() - started, 1)
        store.finish_run(run_id, "ok", summary)
        return {**result, "status": "ok", "summary": summary}


def _run_folders(store: Store, known_projects: list, progress: Progress) -> dict:
    from jarvis import folder_sync

    result = {"account": "this Mac", "source": FOLDERS}
    with source_lock("local", FOLDERS) as acquired:
        if not acquired:
            progress("folders: another sync is already running, skipped")
            return {**result, "status": "busy"}
        try:
            summary = folder_sync.sync(store, known_projects, progress=progress)
        except Exception as exc:
            detail = google_auth.describe_error(exc)
            progress(f"folders: failed: {detail}")
            return {**result, "status": "error", "error": detail}
    return {**result, "status": "ok", "summary": summary}


def _dispatch(store: Store, config: Config, email: str, source: str, creds,
              progress: Progress, max_messages: int | None) -> dict:
    # Imported here so one mirror's dependencies never block another's.
    from jarvis import google_api

    if source == "gmail":
        from jarvis import gmail_sync

        return gmail_sync.sync(
            google_api.GmailApi(creds, on_retry=progress), store, email, config.ingest_months,
            progress=progress, max_messages=max_messages,
        )
    if source == "calendar":
        from jarvis import calendar_sync

        return calendar_sync.sync(
            google_api.CalendarApi(creds), store, email, config.ingest_months, progress=progress
        )
    from jarvis import drive_sync

    return drive_sync.sync(
        google_api.DriveApi(creds), store, email, config.ingest_months, progress=progress
    )
