from __future__ import annotations

import pytest

from jarvis.store import EventRecord, FileRecord, MailRecord, Store, build_match

A = "me@example.com"


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "jarvis.db") as opened:
        yield opened


def _mail(message_id="m1", subject="Pricing update", body="The revised quote for Atlas", ts=1_700_000_000, **kw):
    return MailRecord(id=message_id, thread_id=kw.pop("thread_id", "t1"), internal_date=ts * 1000,
                      subject=subject, body=body, sender="Ann <ann@example.com>", **kw)


def test_database_uses_wal_and_has_every_table(store):
    assert store.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    tables = {row[0] for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"gmail_messages", "calendars", "calendar_events", "drive_files", "sync_state",
            "sync_runs", "docs", "docs_fts", "meta"} <= tables


def test_reopening_keeps_data(tmp_path):
    with Store(tmp_path / "j.db") as first:
        first.gmail_upsert(A, [_mail()])
    with Store(tmp_path / "j.db") as second:
        assert len(second.search("quote")) == 1


def test_state_round_trip(store):
    assert store.get_state(A, "gmail", "history_id") is None
    store.set_state(A, "gmail", "history_id", 5)
    store.set_state(A, "gmail", "history_id", 6)
    assert store.get_state(A, "gmail", "history_id") == "6"
    assert store.get_state("other@example.com", "gmail", "history_id") is None
    store.delete_state(A, "gmail", "history_id")
    assert store.get_state(A, "gmail", "history_id", "none") == "none"


def test_runs_are_recorded(store):
    run_id = store.start_run(A, "gmail")
    store.finish_run(run_id, "ok", {"stored": 3})

    last = store.last_run(A, "gmail")
    assert (last["status"], last["summary"]) == ("ok", '{"stored": 3}')
    assert store.last_run(A, "drive") is None


def test_upserting_the_same_mail_twice_keeps_one_row_and_reindexes(store):
    store.gmail_upsert(A, [_mail(body="first wording")])
    store.gmail_upsert(A, [_mail(body="second wording")])

    assert store.db.execute("SELECT COUNT(*) FROM docs").fetchone()[0] == 1
    assert store.search("first") == []
    assert len(store.search("second")) == 1


def test_all_words_must_match_before_falling_back_to_any_word(store):
    store.gmail_upsert(A, [_mail("m1", subject="one", body="pricing and timeline"),
                           _mail("m2", subject="two", body="timeline only")])

    assert [hit.source_id for hit in store.search("pricing timeline")][0] == "m1"
    assert {hit.source_id for hit in store.search("pricing timeline")} == {"m1", "m2"}
    assert {hit.source_id for hit in store.search("what about the timeline")} == {"m1", "m2"}


def test_search_text_cannot_break_the_query(store):
    store.gmail_upsert(A, [_mail(body="go-live is on 2 Dec")])

    for query in ('go-live', 'AND OR NOT', '"unbalanced', "body:secret*", "(", "a:b", "2 Dec?"):
        store.search(query)  # must not raise
    assert len(store.search("go-live")) == 1
    assert build_match("  ") is None
    assert build_match('say "exact phrase" now') == '"say" AND "exact phrase" AND "now"'


def test_filters_by_kind_time_and_account(store):
    store.gmail_upsert(A, [_mail("m1", body="atlas", ts=1000), _mail("m2", body="atlas", ts=2000)])
    store.gmail_upsert("b@example.com", [_mail("m3", body="atlas", ts=3000)])
    store.calendar_upsert_events(A, "cal", [EventRecord(id="e1", summary="Atlas sync", start_ts=1500)])

    assert {h.source_id for h in store.search("atlas", kind="mail")} == {"m1", "m2", "m3"}
    assert {h.source_id for h in store.search("atlas", kind="event")} == {"cal:e1"}
    assert {h.source_id for h in store.search("atlas", after=1500, before=2500)} == {"m2", "cal:e1"}
    assert {h.source_id for h in store.search("atlas", account="b@example.com")} == {"m3"}
    with pytest.raises(ValueError):
        store.search("atlas", kind="note")


def test_empty_query_lists_by_time(store):
    store.calendar_upsert_events(A, "cal", [
        EventRecord(id="e1", summary="First", start_ts=100),
        EventRecord(id="e2", summary="Second", start_ts=200),
        EventRecord(id="e3", summary="Third", start_ts=300),
    ])

    assert [h.title for h in store.search("", kind="event", after=150)] == ["Third", "Second"]
    assert [h.title for h in store.search("", kind="event", order="oldest", limit=2)] == ["First", "Second"]


def test_ordering_by_newest(store):
    store.gmail_upsert(A, [_mail("old", body="atlas atlas atlas", ts=1000), _mail("new", body="atlas", ts=9000)])

    assert [h.source_id for h in store.search("atlas", order="newest")] == ["new", "old"]


def test_deleting_mail_removes_it_from_search(store):
    store.gmail_upsert(A, [_mail("m1", body="uniqueword")])

    assert store.gmail_delete(A, ["m1", "missing"]) == 1
    assert store.search("uniqueword") == []
    assert store.db.execute("SELECT COUNT(*) FROM docs").fetchone()[0] == 0


def test_missing_ids_keeps_order(store):
    store.gmail_upsert(A, [_mail("m2")])

    assert store.gmail_missing_ids(A, ["m3", "m2", "m1"]) == ["m3", "m1"]
    assert store.gmail_missing_ids("other@example.com", ["m2"]) == ["m2"]


def test_thread_lists_sibling_messages(store):
    store.gmail_upsert(A, [_mail("m1", ts=100, thread_id="t9"), _mail("m2", ts=200, thread_id="t9"),
                           _mail("m3", ts=300, thread_id="other")])

    assert [row["id"] for row in store.mail_thread(A, "m2")] == ["m1", "m2"]


def test_events_can_be_kept_out_of_the_index(store):
    store.calendar_upsert_events(A, "cal", [
        EventRecord(id="e1", summary="Office", start_ts=100, indexed=False),
        EventRecord(id="e2", summary="Review", start_ts=200,
                    attendees=[{"email": "ann@example.com", "name": "Ann", "response": "accepted"}],
                    meeting_link="https://meet.example/abc", html_link="https://cal.example/e2"),
    ])

    assert store.db.execute("SELECT COUNT(*) FROM calendar_events").fetchone()[0] == 2
    (hit,) = store.search("", kind="event")
    assert (hit.title, hit.link) == ("Review", "https://cal.example/e2")
    assert "Ann <ann@example.com>" in hit.people
    assert "meet.example/abc" in store.get_doc(hit.doc_id).body


def test_deleting_events(store):
    store.calendar_upsert_events(A, "cal", [EventRecord(id="e1", summary="Review", start_ts=1)])

    assert store.calendar_delete_events(A, "cal", ["e1"]) == 1
    assert store.search("review") == []
    assert store.calendar_event_ids(A, "cal") == set()


def test_file_text_becomes_searchable_once_exported(store):
    store.drive_upsert_files(A, [FileRecord(id="f1", name="SOW v3", mime_type="application/vnd.google-apps.document",
                                            modified_time="2026-01-02T03:04:05Z", web_view_link="https://docs.example/f1")])

    assert len(store.search("SOW")) == 1          # by title straight away
    assert store.search("milestone") == []

    store.drive_set_text(A, "f1", "Milestone one is due in March", False, "2026-01-02T03:04:05Z")

    (hit,) = store.search("milestone")
    assert (hit.kind, hit.title, hit.link) == ("file", "SOW v3", "https://docs.example/f1")
    assert hit.ts == 1767323045


def test_metadata_refresh_keeps_exported_text(store):
    record = FileRecord(id="f1", name="Plan", mime_type="application/vnd.google-apps.document",
                        modified_time="2026-01-02T03:04:05Z")
    store.drive_upsert_files(A, [record])
    store.drive_set_text(A, "f1", "kept text", False, "2026-01-02T03:04:05Z")

    record.name = "Plan (renamed)"
    store.drive_upsert_files(A, [record])

    assert len(store.search("kept")) == 1
    assert store.search("renamed")[0].title == "Plan (renamed)"


def test_pending_exports_are_files_whose_text_is_missing_or_stale(store):
    doc = "application/vnd.google-apps.document"
    store.drive_upsert_files(A, [
        FileRecord(id="fresh", mime_type=doc, modified_time="2026-01-01T00:00:00Z"),
        FileRecord(id="stale", mime_type=doc, modified_time="2026-02-01T00:00:00Z"),
        FileRecord(id="never", mime_type=doc, modified_time="2026-01-01T00:00:00Z"),
        FileRecord(id="pdf", mime_type="application/pdf", modified_time="2026-01-01T00:00:00Z"),
        FileRecord(id="broken", mime_type=doc, modified_time="2026-01-01T00:00:00Z"),
    ])
    store.drive_set_text(A, "fresh", "t", False, "2026-01-01T00:00:00Z")
    store.drive_set_text(A, "stale", "t", False, "2026-01-01T00:00:00Z")
    for _ in range(3):
        store.drive_set_export_error(A, "broken", "too large")

    assert {row["id"] for row in store.drive_pending_exports(A, (doc,))} == {"stale", "never"}


def test_project_tags_need_a_whole_word_in_the_right_case(store):
    store.gmail_upsert(A, [
        _mail("m1", subject="ATL kickoff", body="agenda"),
        _mail("m2", subject="notes", body="the ivp of the equation"),
        _mail("m3", subject="Re: plan", body="BOR review on Monday"),
        _mail("m4", subject="both", body="ATL and BOR status"),
        _mail("m5", subject="AATLX", body="nothing"),
    ])

    counts = store.tag_projects(["ATL", "BOR"])

    tags = dict(store.db.execute("SELECT source_id, project FROM docs"))
    assert tags == {"m1": "ATL", "m2": None, "m3": "BOR", "m4": None, "m5": None}
    assert counts == {"ATL": 1, "BOR": 1}
    assert [h.source_id for h in store.search("", project="ATL")] == ["m1"]


def test_stats_summarise_the_store(store):
    store.gmail_upsert(A, [_mail("m1", ts=1000), _mail("m2", ts=2000, thread_id="t2")])
    store.set_state(A, "gmail", "backfill_done", "1")
    store.set_state(A, "drive", "changes_token", "secret-ish")
    store.finish_run(store.start_run(A, "gmail"), "ok", {"stored": 2})

    stats = store.stats()["accounts"][A]

    assert stats["mail"] == {"count": 2, "threads": 2, "oldest": 1000, "newest": 2000}
    assert stats["docs"] == 2
    assert stats["state"] == {"gmail.backfill_done": "1"}  # tokens are not shown
    assert stats["last_runs"]["gmail"]["status"] == "ok"
    assert stats["last_runs"]["calendar"] is None
