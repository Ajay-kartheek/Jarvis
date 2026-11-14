from __future__ import annotations

import json
from datetime import datetime

import pytest

from jarvis import brief, meeting, notes, runtime
from jarvis.projects import Project
from jarvis.store import EventRecord, MailRecord, Store

ATLAS = Project("Atlas", ("Priya", "Ravi Kumar"), ())
BOREALIS = Project("Borealis", ("Meera", "Arjun"), ())
KNOWN = [ATLAS, BOREALIS]
ME = "me@example.com"


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "jarvis.db") as opened:
        yield opened


# --- which engagement a call is about ---------------------------------------


def test_the_project_name_settles_it():
    assert meeting.spot_project(KNOWN, "Let us start with the Atlas migration.") is ATLAS


def test_two_people_of_one_project_settle_it():
    assert meeting.spot_project(KNOWN, "Meera said Arjun will join later.") is BOREALIS


def test_one_first_name_or_a_tie_is_not_enough():
    assert meeting.spot_project(KNOWN, "Priya is running late.") is None
    assert meeting.spot_project(KNOWN, "Atlas and Borealis are both on the agenda.") is None
    assert meeting.spot_project(KNOWN, "Nothing relevant here.") is None


def test_names_match_whole_words_and_first_names():
    assert meeting.spot_project(KNOWN, "The atlases are on the shelf, said Ravindra.") is None
    assert meeting.spot_project(KNOWN, "Ravi and Priya agreed.") is ATLAS


# --- notes ---------------------------------------------------------------------


def _transcript(path, lines):
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


def test_transcript_reading_skips_jarvis_and_broken_lines(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(
        '{"at": "2026-01-05T10:00:00+00:00", "speaker": "Them", "text": "Hello"}\n'
        'not json\n'
        '{"at": "2026-01-05T10:00:05+00:00", "speaker": "Jarvis", "text": "Say hello back"}\n'
        '{"at": "2026-01-05T10:00:09+00:00", "speaker": "You", "text": "Hi"}\n',
        encoding="utf-8",
    )
    assert [line["speaker"] for line in notes.read_transcript(path)] == ["Them", "You"]
    assert notes.read_transcript(tmp_path / "missing.jsonl") == []


def test_only_a_real_conversation_gets_notes():
    short = [{"speaker": "Them", "text": "Hello, can you hear me?"}] * 3
    long_enough = [{"speaker": "Them", "text": "We agreed to move the rehearsal to Wednesday morning at ten."}] * 10
    assert not notes.worth_notes(short)
    assert notes.worth_notes(long_enough)


def test_a_very_long_transcript_keeps_its_start_and_end():
    lines = [{"at": "2026-01-05T10:00:00+00:00", "speaker": "Them", "text": f"line {n} " + "x" * 200} for n in range(2000)]
    text = notes.render(lines)
    assert len(text) < notes.TRANSCRIPT_CHARS + 200
    assert "line 0 " in text and "line 1999 " in text and "left out" in text


def test_note_file_names_are_safe():
    name = notes.file_name(datetime(2026, 1, 5, 9, 30), 'Atlas <> Client: "weekly" / sync?')
    assert name == "2026-01-05 0930 Atlas Client weekly sync.md"


def test_notes_are_found_by_search_and_survive_a_folder_sync(store):
    store.note_upsert("/notes/a.md", "Meeting notes: Atlas weekly", "Rehearsal moved to Wednesday", 1_700_000_000, "Atlas")
    hits = store.search("rehearsal", project="Atlas")
    assert [hit.title for hit in hits] == ["Meeting notes: Atlas weekly"]
    assert store.local_known("Atlas") == {}  # not a folder file, so a folder sync will not remove it
    assert [note.title for note in store.notes("Atlas")] == ["Meeting notes: Atlas weekly"]
    assert store.notes("Borealis") == []

    store.note_delete("/notes/a.md")
    assert store.notes() == []


def test_the_brief_for_a_meeting_carries_the_last_notes(store, fake_home):
    store.note_upsert("/notes/a.md", "Meeting notes: Atlas weekly", "Ravi owns the sign-off.", 1_700_000_000, "Atlas")
    text = meeting.build_brief(store, ATLAS, None, 1_700_100_000)
    assert "NOTES OF AN EARLIER MEETING" in text and "Ravi owns the sign-off." in text


# --- transcripts on disk ---------------------------------------------------------


def test_meeting_details_are_kept_beside_the_transcript(tmp_path):
    transcript = tmp_path / "2026-01-05-093000.jsonl"
    assert runtime.read_meta(transcript) == {}
    runtime.write_meta(transcript, {"title": "Atlas weekly", "engagement": "Atlas"})
    assert runtime.read_meta(transcript)["engagement"] == "Atlas"


def test_old_transcripts_are_removed_with_their_details(tmp_path):
    old, new = tmp_path / "old.jsonl", tmp_path / "new.jsonl"
    for path in (old, new):
        path.write_text("{}\n")
        runtime.write_meta(path, {"title": "x"})
    import os

    os.utime(old, (1_000_000, 1_000_000))
    assert runtime.purge_transcripts(90, folder=tmp_path, now=1_000_000 + 91 * 86_400) == 1
    assert not old.exists() and not runtime.meta_path(old).exists()
    assert new.exists() and runtime.meta_path(new).exists()


def test_only_a_refused_sign_in_counts_as_lost():
    assert runtime._sign_in_lost("sign-in no longer works: RefreshError: invalid_grant")
    assert runtime._sign_in_lost("not signed in")
    assert not runtime._sign_in_lost("sign-in no longer works: TransportError: no route to host")


# --- the morning brief -----------------------------------------------------------


def test_new_mail_counts_from_the_last_working_evening():
    monday = datetime(2026, 1, 5, 8, 0)
    assert brief.since_when(monday) == datetime(2026, 1, 2, 17, 0)  # Friday
    wednesday = datetime(2026, 1, 7, 8, 0)
    assert brief.since_when(wednesday) == datetime(2026, 1, 6, 12, 0)  # at least twenty hours back


def test_the_brief_material_lists_meetings_and_the_last_message_of_each_thread(store):
    now = datetime(2026, 1, 5, 8, 0)
    ts = int(datetime(2026, 1, 4, 18, 0).timestamp())
    store.gmail_upsert(ME, [
        MailRecord(id="m1", thread_id="t1", internal_date=ts * 1000, subject="Rehearsal date",
                   sender="Ravi <ravi@example.com>", recipients=ME, snippet="Can we move it?"),
        MailRecord(id="m2", thread_id="t1", internal_date=(ts + 60) * 1000, subject="Re: Rehearsal date",
                   sender=f"Me <{ME}>", recipients="ravi@example.com", snippet="Yes, Wednesday.", labels=["SENT"]),
        MailRecord(id="m3", thread_id="t2", internal_date=(ts + 120) * 1000, subject="Support terms",
                   sender="Meera <meera@example.com>", recipients=ME, snippet="Please confirm by Friday."),
    ])
    start = int(datetime(2026, 1, 5, 10, 0).timestamp())
    store.calendar_upsert_events(ME, "primary", [
        EventRecord(id="e1", summary="Atlas weekly", start_ts=start, end_ts=start + 1800,
                    attendees=[{"name": "Priya", "email": "priya@example.com"}]),
    ])
    from jarvis.config import load_config

    config = load_config()
    account = config.primary_account.email.lower()
    # The example config has its own address; reuse the rows under it.
    store.db.execute("UPDATE gmail_messages SET account=?", (account,))
    store.db.execute("UPDATE calendar_events SET account=?", (account,))
    store.db.execute("UPDATE docs SET account=?", (account,))

    text = brief.gather(config, store, now)
    assert "10:00-10:30 | Atlas weekly | Atlas" in text
    assert "Support terms" in text and "Re: Rehearsal date" in text
    assert text.count("Rehearsal date") == 1  # only the thread's last message
    assert "THE USER SENT THIS LAST MESSAGE" in text


# --- how sure the recogniser was ---------------------------------------------------


def test_a_sentence_carries_the_recognisers_confidence():
    from types import SimpleNamespace

    from jarvis.listen import Heard, WindowText

    def token(text, confidence):
        return SimpleNamespace(text=text, confidence=confidence)

    sure = WindowText._join([token(" Good", 0.98), token(" morning.", 0.94)])
    unsure = WindowText._join([token(" Spin", 0.41), token(" a", 0.5), token(" cutlery.", 0.38)])
    assert sure == "Good morning." and isinstance(sure, Heard) and sure.confidence > 0.9
    assert unsure.confidence < 0.6
    assert WindowText._join([]).confidence == 1.0
