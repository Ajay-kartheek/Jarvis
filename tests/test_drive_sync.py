from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fakes import FakeDrive, FakeHttpError

from jarvis import drive_sync
from jarvis.store import Store

ACCOUNT = "me@example.com"
DOC, SHEET, SLIDES = drive_sync.DOC, drive_sync.SHEET, drive_sync.SLIDES


def stamp(days_ago: int) -> str:
    moment = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def file(file_id: str, mime: str = DOC, *, modified: int = 5, **extra) -> dict:
    return {
        "id": file_id,
        "name": f"File {file_id}",
        "mimeType": mime,
        "modifiedTime": stamp(modified),
        "viewedByMeTime": stamp(modified),
        "owners": [{"displayName": "Owner", "emailAddress": "owner@example.com"}],
        "webViewLink": f"https://example.com/{file_id}",
        **extra,
    }


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "jarvis.db") as opened:
        yield opened


def run(api, store, **kwargs):
    return drive_sync.sync(api, store, ACCOUNT, 12, progress=lambda _: None, **kwargs)


def text_of(store, file_id: str) -> str | None:
    return store.drive_get(ACCOUNT, file_id)["text"]


def test_first_run_lists_the_window_and_reads_each_kind(store):
    api = FakeDrive([file("d1"), file("s1", SHEET), file("p1", SLIDES), file("x1", "application/pdf")])
    api.exports[("d1", "text/markdown")] = b"# Plan\n\nShip on Friday."
    api.exports[("p1", "text/plain")] = b"Slide one\r\nSlide two"

    summary = run(api, store)

    assert summary["listed"] == 4
    assert summary["exported"] == 3
    assert summary["pending_exports"] == 0
    assert "Ship on Friday." in text_of(store, "d1")
    assert text_of(store, "p1") == "Slide one\nSlide two"
    assert "## Tab: Sheet1" in text_of(store, "s1")
    assert "a | b" in text_of(store, "s1")
    assert text_of(store, "x1") is None  # other files keep name and link only
    assert store.get_state(ACCOUNT, "drive", "changes_token") == "10"


def test_query_covers_files_the_account_opened_and_skips_folders_and_trash(store):
    api = FakeDrive([file("d1")])
    run(api, store)
    query = api.queries[0]
    assert "trashed = false" in query
    assert "viewedByMeTime >" in query and "modifiedTime" not in query
    assert drive_sync.FOLDER in query


def test_doc_text_is_found_by_search(store):
    api = FakeDrive([file("d1")])
    api.exports[("d1", "text/markdown")] = b"The rollout slips to December."
    run(api, store)
    hits = store.search("rollout December")
    assert [hit.title for hit in hits] == ["File d1"]


def test_later_runs_follow_the_change_feed(store):
    api = FakeDrive([file("d1"), file("d2")])
    run(api, store)
    listings = api.calls["list"]

    changed = file("d1", modified=0)
    api.exports[("d1", "text/markdown")] = b"Second version"
    api.touch(changed)
    api.touch(file("d3", modified=0))
    api.remove("d2")
    summary = run(api, store)

    assert api.calls["list"] == listings  # no second full listing
    assert summary["upserted"] == 2 and summary["deleted"] == 1
    assert store.drive_ids(ACCOUNT) == {"d1", "d3"}
    assert text_of(store, "d1") == "Second version"
    assert store.get_state(ACCOUNT, "drive", "changes_token") == "11"


def test_changes_outside_the_window_and_trashed_files_are_ignored(store):
    api = FakeDrive([file("d1")])
    run(api, store)
    api.touch(file("old", modified=800))
    api.touch(file("d1", trashed=True))
    run(api, store)
    assert store.drive_ids(ACCOUNT) == set()


def test_expired_change_feed_falls_back_to_a_full_listing(store):
    api = FakeDrive([file("d1"), file("d2")])
    run(api, store)
    api.token_expired = True
    api.files.pop("d2")
    summary = run(api, store)
    assert summary["deleted"] == 1
    assert store.drive_ids(ACCOUNT) == {"d1"}


def test_a_failed_export_is_recorded_and_the_rest_carry_on(store):
    api = FakeDrive([file("bad"), file("good")])
    api.exports[("bad", "text/markdown")] = FakeHttpError(403, "exportSizeLimitExceeded")
    api.exports[("good", "text/markdown")] = b"fine"
    summary = run(api, store)
    assert summary["exported"] == 1 and summary["export_failed"] == 1
    assert text_of(store, "good") == "fine"
    assert "403" in store.drive_get(ACCOUNT, "bad")["export_error"]


def test_unchanged_files_are_not_read_again(store):
    api = FakeDrive([file("d1")])
    run(api, store)
    exports = api.calls["export"]
    run(api, store)
    assert api.calls["export"] == exports


def test_large_sheets_are_cut_and_say_so(store):
    api = FakeDrive([file("s1", SHEET)])
    api.tabs["s1"] = [
        {"title": "Q3's plan", "gridProperties": {"rowCount": 5000, "columnCount": 4}},
        {"title": "Chart", "sheetType": "OBJECT"},
    ]
    run(api, store)
    assert api.value_ranges == [["'Q3''s plan'!A1:Z300"]]
    assert "cut to 300 rows" in store.drive_get(ACCOUNT, "s1")["export_error"]


def test_export_limit_leaves_the_rest_pending(store):
    api = FakeDrive([file("d1"), file("d2"), file("d3")])
    summary = run(api, store, max_exports=2)
    assert summary["exported"] == 2 and summary["pending_exports"] == 1
