"""Facts, things Jarvis made, folders and files, preparation tasks and the conversation plumbing."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from jarvis import dashboard, meeting, tasks, workspace
from jarvis.agents import AgentError, Answer
from jarvis.config import load_config
from jarvis.projects import Project
from jarvis.server import create_app
from jarvis.store import EventRecord, PREP_PREFIX, Store, open_store

ME = "you@example.com"  # the account in config.example.toml
ATLAS = Project("Atlas", ("Priya", "Ravi"), ())
BOREALIS = Project("Borealis", ("Meera", "Arjun", "Kiran Rao"), ())


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "jarvis.db") as opened:
        yield opened


# --- the store -----------------------------------------------------------------


def test_facts_belong_to_an_engagement_or_to_none(store):
    general = store.fact_add("Prefers short mails")
    atlas = store.fact_add("Ravi is the technical lead", "Atlas")
    store.fact_add("Meera signs off budgets", "Borealis")
    assert [fact["text"] for fact in store.facts("Atlas")] == ["Ravi is the technical lead", "Prefers short mails"]
    assert len(store.facts()) == 3
    assert store.fact_delete(atlas) and not store.fact_delete(atlas)
    assert [fact["id"] for fact in store.facts("Atlas")] == [general]


def test_things_jarvis_made_are_listed_newest_first_and_counted(store):
    store.artifact_add("prep", "Atlas weekly", project="Atlas", body="sheet", meta={"meeting": {"start": "x"}}, ts=100)
    store.artifact_add("draft", "Re: Rehearsal", link="https://mail.google.com/mail/?authuser=a#drafts", ts=200)
    store.artifact_add("file", "CLAUDE.md", path="/tmp/x/CLAUDE.md", ts=300)
    assert [item["kind"] for item in store.artifacts()] == ["file", "draft", "prep"]
    assert store.artifacts("prep", project="Atlas")[0]["meta"] == {"meeting": {"start": "x"}}
    assert store.artifacts("prep", since=150) == []
    assert store.artifact_counts() == {"prep": 1, "draft": 1, "file": 1}
    assert store.artifact_made("/tmp/x/CLAUDE.md") and not store.artifact_made("/tmp/x/other.md")
    with pytest.raises(ValueError):
        store.artifact_add("poem", "x")


def test_the_activity_log_is_newest_first(store):
    store.log("sync", "Synced gmail: 3 changes")
    store.log("task", "Prepared: Atlas weekly")
    assert [row["text"] for row in store.activity(limit=1)] == ["Prepared: Atlas weekly"]


def test_preparation_sheets_are_searchable_but_are_not_notes(store):
    store.note_upsert("/preps/a.md", "Preparation: Atlas weekly", "Ask Ravi about the sign-off", 1_700_000_000, "Atlas", prefix=PREP_PREFIX)
    assert [hit.title for hit in store.search("sign-off")] == ["Preparation: Atlas weekly"]
    assert store.notes("Atlas") == []


# --- folders and files ---------------------------------------------------------


def test_paths_must_be_ordinary_places_in_the_home_folder(fake_home):
    assert workspace.resolve("~/Documents/Atlas") == fake_home / "Documents" / "Atlas"
    assert workspace.resolve("Documents/Atlas/notes.md") == fake_home / "Documents" / "Atlas" / "notes.md"
    for bad in ("/etc/hosts", "~", "~/Library/Preferences/x.plist", "~/.ssh/config", "~/Documents/../../etc/x",
                "~/Documents/.hidden/file.md", ""):
        with pytest.raises(workspace.WorkspaceError):
            workspace.resolve(bad)


def test_jarvis_writes_new_files_and_updates_only_its_own(store, fake_home):
    theirs = fake_home / "Documents" / "plan.md"
    theirs.parent.mkdir(parents=True)
    theirs.write_text("the user's own plan")
    with pytest.raises(workspace.WorkspaceError, match="left alone"):
        workspace.write_file(store, str(theirs), "overwritten")
    assert theirs.read_text() == "the user's own plan"

    mine = workspace.write_file(store, "~/Documents/Atlas/status.md", "first")
    assert mine.read_text() == "first"
    workspace.write_file(store, "~/Documents/Atlas/status.md", "second")  # its own file: may be updated
    assert mine.read_text() == "second"
    assert [item["title"] for item in store.artifacts("file")] == ["status.md"]
    with pytest.raises(workspace.WorkspaceError, match="too long"):
        workspace.write_file(store, "~/Documents/Atlas/big.md", "x" * (workspace.MAX_FILE_CHARS + 1))


def test_a_working_folder_gets_a_brief_a_repository_and_is_registered(store, fake_home):
    config = load_config()
    made = workspace.create_workspace(store, config, "Cedar Labs", "# Cedar Labs\n\nNew client.", people=["Dev"])
    folder = fake_home / "Documents" / "Cedar-Labs"
    assert made["path"] == str(folder) and made["new_engagement"] is True
    assert (folder / "CLAUDE.md").read_text().startswith("# Cedar Labs")
    assert (folder / ".git").is_dir()
    from jarvis import projects

    project = projects.find(store, "Cedar Labs", config)
    assert project.people == ("Dev",) and len(project.folders) == 1

    again = workspace.create_workspace(store, config, "Cedar Labs", "# Cedar Labs\n\nUpdated brief.")
    assert again["new_engagement"] is False and "Updated brief" in (folder / "CLAUDE.md").read_text()


def test_a_working_folder_is_not_made_over_someone_elses_files(store, fake_home):
    busy = fake_home / "Documents" / "Atlas"
    busy.mkdir(parents=True)
    (busy / "contract.docx").write_text("x")
    with pytest.raises(workspace.WorkspaceError, match="not empty"):
        workspace.create_workspace(store, load_config(), "Atlas", "# Atlas")
    assert not (busy / "CLAUDE.md").exists()


# --- which meeting is meant -------------------------------------------------------


def _events(store, now):
    def event(event_id, title, offset_hours, attendees):
        start = int(now + offset_hours * 3600)
        return EventRecord(id=event_id, summary=title, start_ts=start, end_ts=start + 1800, attendees=attendees)

    inside = [{"name": "Colleague", "email": "colleague@example.com"}]
    outside = [{"name": "Priya", "email": "priya@client.example"}]
    store.calendar_upsert_events(ME, "primary", [
        event("e1", "Atlas - internal sync", 1, inside),
        event("e2", "Atlas <> Client weekly", 3, outside + inside),
        event("e3", "Borealis support review", 5, [{"name": "Meera", "email": "meera@other.example"}]),
    ])


def test_the_agenda_marks_meetings_with_outside_people(store):
    now = datetime(2026, 1, 5, 9, 0).timestamp()
    _events(store, now)
    found = meeting.agenda(store, ME, [ATLAS, BOREALIS], int(now), int(now + 86_400))
    assert [(m["title"], m["engagement"], m["outside"]) for m in found] == [
        ("Atlas - internal sync", "Atlas", False),
        ("Atlas <> Client weekly", "Atlas", True),
        ("Borealis support review", "Borealis", True),
    ]


def test_finding_the_meeting_someone_means(store):
    now = datetime(2026, 1, 5, 9, 0).timestamp()
    _events(store, now)
    known = [ATLAS, BOREALIS]
    assert meeting.find_meeting(store, ME, known, engagement="Atlas", now=now)["title"] == "Atlas - internal sync"
    assert meeting.find_meeting(store, ME, known, engagement="atlas", title="client weekly", now=now)["title"] == "Atlas <> Client weekly"
    assert meeting.find_meeting(store, ME, known, engagement="Borealis", now=now)["title"] == "Borealis support review"
    assert meeting.find_meeting(store, ME, known, title="support review", when=now + 5 * 3600, now=now)["title"] == "Borealis support review"
    assert meeting.find_meeting(store, ME, known, engagement="Cedar", now=now) is None
    assert meeting.find_meeting(store, ME, known, now=now)["title"] == "Atlas - internal sync"  # nothing named: the next one


def test_the_live_brief_starts_from_the_preparation_and_what_jarvis_was_told(store, fake_home):
    now = 1_700_000_000
    store.artifact_add("prep", "Atlas weekly", project="Atlas", body="**Goal for this call**: agree the rehearsal date.", ts=now - 3600)
    store.fact_add("Ravi decides on dates, not Priya", "Atlas")
    text = meeting.build_brief(store, ATLAS, None, now)
    assert "PREPARATION FOR THIS MEETING" in text and "agree the rehearsal date" in text
    assert "WHAT THE USER HAS TOLD JARVIS" in text and "Ravi decides on dates" in text
    old = meeting.build_brief(store, ATLAS, None, now + 3 * 86_400)
    assert "PREPARATION FOR THIS MEETING" not in old  # yesterday's sheet is not today's preparation


# --- preparing ---------------------------------------------------------------------


def test_what_the_preparing_session_is_told(store, fake_home):
    store.fact_add("Ravi is the technical lead", "Atlas")
    task = tasks.Task(
        id="k1", kind="prep", title="Prepare: Atlas <> Client weekly", engagement="Atlas",
        meeting={"title": "Atlas <> Client weekly", "start": "2026-01-05T12:00+00:00", "end": "2026-01-05T12:30+00:00",
                 "people": ["Priya", "Ravi"]},
        focus="I want the rehearsal date settled",
    )
    text = tasks.prep_request(task, store, load_config())
    assert "MEETING: Atlas <> Client weekly" in text and "With: Priya, Ravi" in text
    assert 'Search with project "Atlas"' in text and "Ravi is the technical lead" in text
    assert "I want the rehearsal date settled" in text


def test_prep_file_names_are_safe():
    assert tasks.file_name(datetime(2026, 1, 5, 12, 0), "Atlas <> Client: weekly?") == "2026-01-05 1200 Atlas Client weekly.md"


# --- the running service -------------------------------------------------------------


@pytest.fixture
def client(fake_home):
    app = create_app(load_config())
    runtime = app.state.runtime

    async def reply(text, *, spoken=False, named=True, on_step=None, on_text=None, on_tool=None):
        if "fail" in text:
            raise AgentError("The model is not reachable.")
        if on_step:
            on_step("search: rehearsal")
        if on_text:
            on_text("Wednesday at ten.")
        return Answer("Wednesday at ten.", [], 0.1)

    runtime.core.talk = reply  # no model in tests
    with TestClient(app) as opened:
        opened.runtime = runtime
        yield opened


def _drain(queue) -> list[dict]:
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


def test_a_typed_message_gets_a_reply_and_the_events_in_order(client):
    queue = client.runtime.hub.join()
    answer = client.post("/api/talk", json={"text": "When is the rehearsal?", "id": "t-test"}).json()
    assert answer["text"] == "Wednesday at ten." and answer["speak"] is False and answer["id"] == "t-test"
    kinds = [event["type"] for event in _drain(queue) if event["type"] != "state"]
    assert kinds == ["turn", "step", "reply_delta", "reply"]
    state = client.get("/api/state").json()
    assert [(turn["role"], turn["text"]) for turn in state["conversation"]] == [
        ("user", "When is the rehearsal?"), ("jarvis", "Wednesday at ten."),
    ]
    assert state["busy"] is False and state["voice"]["hear"] is False


def test_the_older_ask_address_still_works_and_empty_messages_are_refused(client):
    assert client.post("/api/ask", json={"question": "When is the rehearsal?"}).json()["text"] == "Wednesday at ten."
    assert client.post("/api/talk", json={"text": "  "}).status_code == 422
    failed = client.post("/api/talk", json={"text": "please fail"})
    assert failed.status_code == 502 and "not reachable" in failed.json()["detail"]


def test_forgetting_the_conversation(client):
    client.post("/api/talk", json={"text": "hello"})
    assert client.post("/api/talk/reset").json() == {"ok": True}
    assert client.get("/api/state").json()["conversation"] == []


def test_cards_are_shown_replaced_and_dismissed(client):
    runtime = client.runtime
    first = runtime.show_card({"kind": "note", "title": "Open items", "body": "- one"})
    runtime.show_card({"id": first["id"], "kind": "note", "title": "Open items", "body": "- one\n- two"})
    runtime.show_card({"kind": "prep", "title": "Preparation: Atlas weekly", "body": "sheet"})
    cards = client.get("/api/state").json()["cards"]
    assert [card["title"] for card in cards] == ["Preparation: Atlas weekly", "Open items"]
    assert cards[1]["body"].endswith("two")
    client.post(f"/api/cards/{first['id']}/dismiss")
    assert [card["kind"] for card in client.get("/api/state").json()["cards"]] == ["prep"]


def test_settings_are_kept_across_a_restart(client, fake_home):
    assert client.get("/api/settings").json() == {
        "speak": True, "auto_prep": True, "proactive": True, "wake_word": True, "auto": True,
    }
    changed = client.post("/api/settings", json={"speak": False, "auto": False, "proactive": False}).json()
    assert changed == {"speak": False, "auto_prep": True, "proactive": False, "wake_word": True, "auto": False}
    reopened = create_app(load_config()).state.runtime
    assert reopened.settings["speak"] is False and reopened.autopilot.enabled is False
    assert reopened.settings["proactive"] is False and reopened.settings["wake_word"] is True


def test_jarvis_speaking_up_is_shown_even_when_it_stays_silent(client):
    queue = client.runtime.hub.join()
    client.runtime.say("The preparation for Atlas weekly is ready.")
    events = [event for event in _drain(queue) if event["type"] in ("say", "speech")]
    assert [event["type"] for event in events] == ["say"]  # nobody spoke to it recently: no sound
    assert client.get("/api/state").json()["conversation"][-1]["role"] == "jarvis"


def test_task_answers_only_reach_a_task_that_asked(client):
    assert client.get("/api/tasks").json() == []
    assert client.post("/api/tasks/k9/answer", json={"text": "Friday"}).status_code == 409
    assert client.post("/api/tasks/k9/stop").status_code == 409


def test_voice_is_refused_while_a_call_is_being_listened_to(client, monkeypatch):
    with monkeypatch.context() as during:  # undone before the service shuts down at the end of the test
        during.setattr(type(client.runtime.meeting), "running", property(lambda self: True))
        with client.websocket_connect("/ws/voice") as socket:
            assert json.loads(socket.receive_text()) == {"error": "busy"}


def test_the_dashboard_reads_what_jarvis_knows_made_and_did(client):
    with open_store() as store:
        store.artifact_add("prep", "Atlas weekly", project="Atlas", body="sheet")
        store.artifact_add("folder", "Cedar-Labs", project="Cedar Labs", path="/tmp/Cedar-Labs")
        fact = store.fact_add("Ravi is the technical lead", "Atlas")
        store.log("task", "Prepared: Atlas weekly")
    overview = client.get("/api/overview").json()
    assert overview["counts"]["preps"] == 1 and overview["counts"]["created"] == 1
    assert [e["name"] for e in overview["engagements"]] == ["Atlas", "Borealis"]
    library = client.get("/api/library").json()
    assert [item["title"] for item in library["preps"]] == ["Atlas weekly"]
    assert library["files"][0]["path"] == "/tmp/Cedar-Labs" and library["notes"] == []
    assert client.get("/api/facts").json()[0]["engagement"] == "Atlas"
    assert client.delete(f"/api/facts/{fact}").json() == {"ok": True}
    assert client.delete(f"/api/facts/{fact}").status_code == 404
    assert client.get("/api/activity").json()[0]["text"] == "Prepared: Atlas weekly"
    assert dashboard.iso(None) is None


def test_jarvis_prepares_unasked_only_for_client_meetings_that_are_close(client):
    runtime = client.runtime
    now = datetime(2026, 1, 5, 9, 0).timestamp()
    with open_store() as store:
        def event(event_id, title, minutes, email):
            start = int(now + minutes * 60)
            return EventRecord(id=event_id, summary=title, start_ts=start, end_ts=start + 1800,
                               attendees=[{"name": "Guest", "email": email}])

        store.calendar_upsert_events(ME, "primary", [
            event("e1", "Atlas - internal sync", 30, "colleague@example.com"),   # nobody from outside
            event("e2", "Atlas <> Client weekly", 35, "priya@client.example"),   # this one
            event("e3", "Borealis support review", 300, "meera@other.example"),  # hours away
            event("e4", "Lunch with a vendor", 40, "rep@vendor.example"),        # no engagement
        ])
    started = []
    runtime.tasks.start_prep = lambda meeting, engagement, **kw: started.append((meeting["title"], engagement, kw)) or object()
    runtime.auto_prepare(now)
    runtime.auto_prepare(now + 60)  # the next minute must not start it again
    assert started == [("Atlas <> Client weekly", "Atlas", {"auto": True})]

    runtime.change_settings({"auto_prep": False})
    runtime._auto_prepared.clear()
    runtime.auto_prepare(now)
    assert len(started) == 1


def test_a_reply_is_spoken_sentence_by_sentence_as_it_grows():
    from jarvis.runtime import _Speaker

    class FakeStream:
        """Stands in for the sentence splitter: a sentence ends at a full stop."""

        def __init__(self):
            self.given = 0

        def feed(self, text):
            done = text[: text.rfind(".") + 1] if "." in text else ""
            fresh, self.given = done[self.given:], max(self.given, len(done))
            return [part.strip() + "." for part in fresh.split(".") if part.strip()]

        def finish(self, text):
            rest = text[self.given:].strip()
            self.given = len(text)
            return [rest] if rest else []

    class FakeRuntime:
        def __init__(self):
            self.spoken = []

        def _speak(self, speech_id, seq, sentence, keep=False):
            self.spoken.append((seq, sentence))
            return True

    runtime = FakeRuntime()
    speaker = _Speaker(runtime, "t1")
    speaker._make, speaker._stream = FakeStream, FakeStream()

    speaker.feed("Let me check")
    speaker.feed("Let me check.")
    speaker.feed("The rehearsal")  # a tool ran; a new line began
    speaker.feed("The rehearsal is on Wednesday. It starts")
    speaker.finish("The rehearsal is on Wednesday. It starts at ten")
    assert runtime.spoken == [(0, "Let me check."), (1, "The rehearsal is on Wednesday."), (2, "It starts at ten")]


# --- what was built ------------------------------------------------------------------


def test_the_work_log_reads_commits_pending_changes_and_changed_files(store, tmp_path):
    import subprocess

    from jarvis import worklog

    folder = tmp_path / "Atlas"
    inner = folder / "service"
    inner.mkdir(parents=True)

    def git(where, *arguments):
        subprocess.run(["git", "-C", str(where), "-c", "user.name=T", "-c", "user.email=t@example.com", *arguments],
                       check=True, capture_output=True)

    git(inner, "init", "--quiet")
    (inner / "rules.py").write_text("LIMIT = 5\n")
    git(inner, "add", "rules.py")
    git(inner, "commit", "--quiet", "-m", "Add the price limit rule")
    (inner / "notes.md").write_text("draft\n")  # not committed

    project = Project("Atlas", ("Priya",), (str(folder),))
    assert worklog.repositories(folder) == [inner]
    store.local_upsert("Atlas", str(inner / "rules.py"), "Atlas/service/rules.py", "LIMIT = 5", 1_900_000_000, 10)
    text = worklog.report(store, project, datetime(2020, 1, 1))
    assert "Repository Atlas/service: 1 commits" in text and "Add the price limit rule" in text
    assert "A rules.py" in text and "?? notes.md" in text
    assert "Atlas/service/rules.py" in text and "Files changed since then" in text

    quiet = worklog.report(store, Project("Borealis", (), (str(tmp_path / "missing"),)), datetime(2020, 1, 1))
    assert "is not there right now" in quiet


def test_todays_preparation_sheets_come_back_after_a_restart(client):
    runtime = client.runtime
    soon = datetime.now().astimezone().replace(microsecond=0)
    over = {"start": "2020-01-05T10:00+00:00", "end": "2020-01-05T10:30+00:00"}
    ahead = {"start": soon.isoformat(), "end": soon.replace(year=soon.year + 1).isoformat()}
    with open_store() as store:
        store.artifact_add("prep", "Atlas weekly", project="Atlas", body="old sheet", meta={"meeting": over})
        store.artifact_add("prep", "Borealis review", project="Borealis", body="first try", meta={"meeting": ahead}, ts=int(soon.timestamp()) - 60)
        store.artifact_add("prep", "Borealis review", project="Borealis", body="the sheet", meta={"meeting": ahead})
    runtime.restore_cards()
    cards = client.get("/api/state").json()["cards"]
    assert [(card["kind"], card["title"], card["body"]) for card in cards] == [
        ("prep", "Preparation: Borealis review", "the sheet"),
    ]


def test_a_reminder_is_said_and_shown_once_when_it_is_due(client):
    runtime = client.runtime
    now = datetime(2026, 1, 5, 18, 0).timestamp()
    with open_store() as store:
        store.reminder_add(int(now) - 30, "Go to the store")
        later = store.reminder_add(int(now) + 3600, "Call Priya")
        store.reminder_add(int(now) - 5 * 3600, "Send the checklist")  # the Mac was asleep then
    runtime.settings["speak"] = False  # the voice needs a running service; what is said is checked as text
    queue = runtime.hub.join()
    assert runtime.remind_due(now) == 2
    said = [event["text"] for event in _drain(queue) if event["type"] == "say"]
    assert said[0] == "A reminder: Send the checklist (it was due at 13:00 on Monday)" and said[1] == "A reminder: Go to the store"
    assert [card["title"] for card in client.get("/api/state").json()["cards"]] == ["Reminder", "Reminder"]
    assert runtime.remind_due(now) == 0  # not twice
    with open_store() as store:
        assert [item["id"] for item in store.reminders()] == [later]
        assert store.reminder_done(later) and not store.reminder_done(later)


def test_a_meeting_the_user_called_off_does_not_name_a_call_in_its_slot(store):
    now = datetime(2026, 1, 5, 15, 20).timestamp()
    start = int(datetime(2026, 1, 5, 15, 0).timestamp())
    store.calendar_upsert_events(ME, "primary", [
        EventRecord(id="e1", summary="Atlas <> Client weekly", start_ts=start, end_ts=start + 1800,
                    attendees=[{"name": "Priya", "email": "priya@client.example"}]),
    ])
    assert meeting.current_event(store, ME, now)["title"] == "Atlas <> Client weekly"
    meeting.mark_skipped(store, "Atlas <> Client weekly", start)
    assert meeting.current_event(store, ME, now) is None  # a call now is some other call
    assert meeting.agenda(store, ME, [ATLAS], start - 60, start + 60)[0]["skipped"] is True


def test_the_latest_call_can_be_refiled_or_discarded(fake_home):
    from jarvis import notes

    assert notes.correct_latest(title="x") is None
    folder = fake_home / "notes"
    folder.mkdir()
    path = folder / "call.md"
    path.write_text("notes")
    with open_store() as store:
        store.note_upsert(str(path), "Meeting notes: Atlas <> Client weekly", "We tested the microphone.", 1_700_000_000, "Atlas")
        store.artifact_add("notes", "Atlas <> Client weekly", project="Atlas", path=str(path), body="We tested the microphone.", ts=1_700_000_000)

    assert notes.correct_latest(title="Test call with a colleague", engagement="") == "Atlas <> Client weekly"
    with open_store() as store:
        assert store.notes("Atlas") == []  # no longer part of the engagement's record
        assert [note.title for note in store.notes()] == ["Meeting notes: Test call with a colleague"]
        assert store.artifacts("notes")[0]["project"] is None

    assert notes.correct_latest(discard=True) == "Test call with a colleague"
    with open_store() as store:
        assert store.notes() == [] and store.artifacts("notes") == []
    assert not path.exists()


# --- the one "yes" ---------------------------------------------------------------------


@pytest.mark.parametrize("text, answer", [
    ("yes", True), ("Yes.", True), ("yeah go ahead", None), ("Go ahead", True), ("send it", True),
    ("Yes, please", True), ("okay Jarvis", True), ("no", False), ("No thanks", False), ("cancel it", False),
    ("don't", False), ("yes but change the subject first", None), ("send it to Priya instead", None),
    ("what does it say?", None), ("", None),
])
def test_only_a_plain_yes_or_no_counts_as_an_answer(text, answer):
    from jarvis import approvals

    assert approvals.plain_answer(text) is answer


def test_an_approval_is_settled_by_a_click_or_by_a_plain_word_and_never_twice(client):
    import time

    runtime = client.runtime
    runtime.settings["speak"] = False

    async def scenario():
        asking = asyncio.create_task(runtime.approvals.ask("send_mail", "d1", "Send the reply to Priya", "To: priya@example.com"))
        await asyncio.sleep(0)
        first = runtime.approvals.open()[0]
        assert [card["kind"] for card in runtime.cards] == ["approval"]
        assert runtime.approvals.words("what does it say?", spoken=False) is None  # not an answer: stays open
        assert runtime.approvals.click(first.id, True) and not runtime.approvals.click(first.id, True)
        granted = await asking
        assert granted.granted is True and granted.by == "click"
        assert granted.allows("send_mail", "d1") and not granted.allows("send_mail", "d2") and not granted.allows("run", "d1")
        assert runtime.cards == []

        asking = asyncio.create_task(runtime.approvals.ask("run", "git push", "Run a command", "git push"))
        await asyncio.sleep(0)
        open_now = runtime.approvals.open()[0]
        open_now.asked_at = time.monotonic() - 600  # asked ten minutes ago
        assert runtime.approvals.words("yes", spoken=True) is None  # a spoken yes that late takes a click
        declined = runtime.approvals.words("no", spoken=False)
        assert declined is not None and (await asking).granted is False

        quick = await runtime.approvals.ask("run", "x", "Run", "x", wait=0.01)
        assert quick.granted is False and quick.by == "timeout"  # no answer is a no

    asyncio.run(scenario())


def test_a_plain_yes_goes_to_the_open_question_and_not_to_the_model(client):
    runtime = client.runtime
    runtime.settings["speak"] = False
    seen = []

    async def reply(text, **kw):
        seen.append(text)
        return Answer("model reply", [], 0.1)

    runtime.core.talk = reply

    async def scenario():
        asking = asyncio.create_task(runtime.approvals.ask("send_mail", "d1", "Send the reply to Priya", "body"))
        await asyncio.sleep(0)
        answer = await runtime.talk("yes send it", spoken=False)
        assert answer["text"] == "Going ahead." and seen == []
        assert (await asking).granted is True
        assert (await runtime.talk("yes", spoken=False))["text"] == "model reply"  # nothing open: an ordinary message

    asyncio.run(scenario())
    assert client.post("/api/approvals/a99", json={"approve": True}).status_code == 409
