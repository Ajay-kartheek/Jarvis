from __future__ import annotations

import plistlib

import pytest
from fastapi.testclient import TestClient

from jarvis import install
from jarvis.config import load_config
from jarvis.server import create_app


@pytest.fixture
def client(fake_home):
    app = create_app(load_config())
    runtime = app.state.runtime
    calls: list[str] = []

    async def start(project=None, headphones=None):
        calls.append("start")
        runtime.meeting.status = "listening"

    async def stop():
        calls.append("stop")
        runtime.meeting.status = "idle"

    runtime.meeting.start = start  # no real listening in tests
    runtime.meeting.stop = stop
    type(runtime.meeting).running = property(lambda self: self.status != "idle")
    with TestClient(app) as opened:
        opened.calls = calls
        opened.runtime = runtime
        yield opened
    del type(runtime.meeting).running


def _report(active: bool) -> dict:
    return {
        "call": {"active": active, "app": "com.microsoft.teams2" if active else None},
        "headphones": True,
        "permissions": {"microphone": "granted", "system_audio": "granted"},
    }


def test_state_says_what_the_interface_needs(client):
    state = client.get("/api/state").json()
    assert state["status"] == "idle" and state["auto"] is True and state["app"] is False
    assert [e["name"] for e in state["engagements"]] == ["Atlas", "Borealis"]
    assert state["notices"] == [] and state["call"] == {"active": False, "app": None}


def test_other_web_pages_are_refused(client):
    assert client.get("/api/state", headers={"Origin": "https://example.com"}).status_code == 403
    assert client.get("/api/state", headers={"Origin": "http://127.0.0.1:8765"}).status_code == 200


def test_the_apps_reports_drive_listening(client):
    assert client.post("/api/app", json=_report(False)).json() == {"status": "idle", "attention": 0}
    assert client.get("/api/state").json()["app"] is True

    assert client.post("/api/app", json=_report(True)).json()["status"] == "listening"
    client.post("/api/app", json=_report(True))
    assert client.calls == ["start"]
    assert client.get("/api/state").json()["headphones"] is True

    assert client.post("/api/app", json=_report(False)).json()["status"] == "idle"
    assert client.calls == ["start", "stop"]


def test_turning_automatic_listening_off(client):
    assert client.post("/api/auto", json={"enabled": False}).json()["auto"] is False
    client.post("/api/app", json=_report(True))
    assert client.calls == []


def test_stop_by_hand_is_respected_for_the_rest_of_the_call(client):
    client.post("/api/app", json=_report(True))
    client.post("/api/meeting/stop")
    client.post("/api/app", json=_report(True))
    assert client.calls == ["start", "stop"]


def test_suggest_needs_a_meeting_and_ask_needs_a_question(client):
    assert client.post("/api/meeting/suggest").status_code == 409
    assert client.post("/api/ask", json={"question": "  "}).status_code == 422


def test_today_and_notes_answer_on_an_empty_store(client):
    today = client.get("/api/today").json()
    assert today["meetings"] == [] and today["notes"] == [] and today["brief"] is None
    assert client.get("/api/notes").json() == []


def test_notices_raise_attention_once(client):
    runtime = client.runtime
    runtime.notify("signin", "Sign-in stopped working.")
    runtime.notify("signin", "Sign-in stopped working.")
    state = client.get("/api/state").json()
    assert state["attention"] == 1 and state["notices"] == [{"key": "signin", "text": "Sign-in stopped working."}]
    runtime.clear("signin")
    assert client.get("/api/state").json()["notices"] == []


def test_the_engagement_is_announced_only_when_it_becomes_clear_mid_call(client):
    runtime = client.runtime
    queue = runtime.hub.join()

    def infos() -> list[str]:
        found = []
        while not queue.empty():
            event = queue.get_nowait()
            if event["type"] == "info":
                found.append(event["text"])
        return found

    runtime.meeting._on_event({"type": "meeting", "title": "Atlas weekly", "engagement": "Atlas", "brief_chars": 10})
    assert infos() == []  # known from the calendar: nothing to announce
    assert runtime.meeting.meeting["engagement"] == "Atlas"

    runtime.meeting.meeting = None
    runtime.meeting._on_event({"type": "meeting", "title": "Unscheduled call", "engagement": None, "brief_chars": 10})
    started = runtime.meeting.meeting["started_at"]
    runtime.meeting._on_event({"type": "meeting", "title": "Unscheduled call", "engagement": "Borealis", "brief_chars": 10})
    assert infos() == ["This call is about Borealis."]
    assert runtime.meeting.meeting == {"title": "Unscheduled call", "engagement": "Borealis", "started_at": started}


# --- set-up files -----------------------------------------------------------------


def test_the_login_entry_runs_the_installed_app_and_restarts_it_after_a_crash(fake_home):
    plist = install.agent_plist(install.installed_app())
    assert plist["ProgramArguments"] == [str(fake_home / "Applications/Jarvis.app/Contents/MacOS/Jarvis")]
    assert plist["RunAtLoad"] is True and plist["KeepAlive"] == {"SuccessfulExit": False}
    assert plistlib.loads(plistlib.dumps(plist))["Label"] == install.LABEL


def test_the_app_is_told_how_to_start_the_service():
    settings = install.app_config(8765)
    assert settings["daemon"][-3:] == ["daemon", "--port", "8765"]
    assert settings["daemon"][-4].endswith("/release/.venv/bin/jarvis") and settings["port"] == 8765
    assert settings["daemon"][1].startswith("JARVIS_CONFIG=") and settings["cwd"].endswith("/release")
    assert "com.microsoft.teams" in settings["call_apps"]
    assert not any("FaceTime" in app or "WhatsApp" in app for app in settings["call_apps"])  # personal calls stay private
