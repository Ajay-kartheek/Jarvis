"""The ear in the service: when the app is asked for the microphone, the /ws/ear socket, and what closes the ear.

No model, microphone, sound or network: the voice model is a file of a few bytes, the ear's own listening is
replaced where a test needs the socket to be taken.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jarvis import ear, install, voice
from jarvis.config import load_config
from jarvis.runtime import Runtime
from jarvis.server import create_app

TEAMS = "com.microsoft.teams2"


@pytest.fixture
def client(fake_home, monkeypatch):
    app = create_app(load_config())
    runtime = app.state.runtime

    async def start(project=None, headphones=None):
        runtime.meeting.status = "listening"
        runtime.update_ear()

    async def stop():
        runtime.meeting.status = "idle"
        runtime.update_ear()

    runtime.meeting.start = start  # no real listening in tests
    runtime.meeting.stop = stop
    monkeypatch.setattr(type(runtime.meeting), "running", property(lambda self: self.status != "idle"))
    with TestClient(app) as opened:
        opened.runtime = runtime
        yield opened


def voice_model() -> None:
    """The voice model on this Mac (a stand-in: nothing loads it here)."""
    ear.folder().mkdir(parents=True, exist_ok=True)
    (ear.folder() / ear.VOICE).write_bytes(b"model")


def report(client, *, call: bool = False, microphone: str = "granted", headphones: bool = False) -> dict:
    return client.post("/api/app", json={
        "call": {"active": call, "app": TEAMS if call else None},
        "headphones": headphones,
        "permissions": {"microphone": microphone, "system_audio": "granted"},
    }).json()


def test_the_app_is_asked_for_the_ear_only_when_it_is_on_can_run_and_nothing_else_has_the_microphone(client):
    assert report(client)["ear"] is False  # the voice model is not on this Mac yet
    voice_model()
    assert report(client)["ear"] is True
    assert report(client, microphone="denied")["ear"] is False
    assert report(client)["ear"] is True
    client.post("/api/settings", json={"wake_word": False})
    assert report(client)["ear"] is False
    client.post("/api/settings", json={"wake_word": True})
    assert report(client)["ear"] is True
    assert report(client, call=True)["ear"] is False  # a call: the autopilot listens to it
    assert report(client)["ear"] is True
    client.post("/api/meeting/start")  # listening to a call started by hand
    assert report(client)["ear"] is False
    client.post("/api/meeting/stop")
    assert report(client)["ear"] is True


def test_the_ear_socket_turns_the_app_away_when_the_ear_is_not_wanted(client):
    report(client)  # no voice model
    with client.websocket_connect("/ws/ear") as socket:
        assert socket.receive_json() == {"error": "unavailable"}
        with pytest.raises(WebSocketDisconnect):
            socket.receive_text()


def test_the_ear_socket_says_busy_during_a_call(client):
    voice_model()
    report(client, call=True)
    with client.websocket_connect("/ws/ear") as socket:
        assert socket.receive_json() == {"error": "busy"}
        with pytest.raises(WebSocketDisconnect):
            socket.receive_text()


def test_the_ear_socket_refuses_other_web_pages(client):
    voice_model()
    report(client)
    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect("/ws/ear", headers={"origin": "https://example.com"}) as socket:
            socket.receive_text()
    assert refused.value.code == 1008


def test_the_ear_socket_hands_the_stream_to_the_ear_when_it_is_wanted(client, monkeypatch):
    voice_model()
    report(client)
    taken = []

    async def serve(websocket):
        taken.append((await websocket.receive())["bytes"])

    monkeypatch.setattr(client.runtime.ear, "serve", serve)
    with client.websocket_connect("/ws/ear") as socket:
        socket.send_bytes(b"\x00\x00\x00\x00" * 1600)
        with pytest.raises(WebSocketDisconnect):
            socket.receive_text()  # closed once the ear is done with it
    assert taken == [b"\x00\x00\x00\x00" * 1600]


def test_the_ear_itself_refuses_when_its_voice_model_cannot_be_loaded(client):
    voice_model()  # a few bytes, not a model
    report(client)
    with client.websocket_connect("/ws/ear") as socket:
        assert socket.receive_json() == {"error": "unavailable"}
    assert client.runtime.ear.state == "off"


def test_the_page_saying_jarvis_voice_started_and_stopped_tells_the_ear(client, monkeypatch):
    told = []
    monkeypatch.setattr(client.runtime.ear, "speaking_started", lambda: told.append("started"))
    monkeypatch.setattr(client.runtime.ear, "speaking_ended", lambda: told.append("ended"))
    client.post("/api/voice/playing", json={"id": "s1", "playing": True})
    client.post("/api/voice/playing", json={"id": "s1", "playing": False})
    assert told == ["started", "ended"]


def test_a_held_key_message_closes_the_ear_until_it_is_over(client, monkeypatch):
    seen = []

    async def hear(websocket, runtime):
        seen.append(set(runtime.ear._paused))
        if len(seen) == 2:
            raise RuntimeError("the microphone went away")

    monkeypatch.setattr(voice, "_hear", hear)
    for _ in range(2):
        try:
            with client.websocket_connect("/ws/voice"):
                pass
        except RuntimeError:
            pass
    assert seen == [{"message"}, {"message"}]
    assert client.runtime.ear._paused == set()  # opened again, even after a message that failed


def test_a_call_closes_the_ear_at_once_and_its_end_opens_it_again(client):
    voice_model()
    report(client)
    assert client.runtime.ear._paused == set()
    report(client, call=True)
    assert "call" in client.runtime.ear._paused
    report(client)
    assert client.runtime.ear._paused == set()


def test_listening_to_a_call_closes_the_ear_until_the_listening_is_over(fake_home, monkeypatch):
    async def scene():
        runtime = Runtime(load_config())
        over = asyncio.Event()
        seen = []

        async def run(project, headphones, out):
            await over.wait()

        monkeypatch.setattr(runtime.meeting, "_run", run)
        await runtime.meeting.start()
        seen.append(set(runtime.ear._paused))
        over.set()
        await asyncio.sleep(0.01)
        seen.append(set(runtime.ear._paused))
        return seen

    assert asyncio.run(scene()) == [{"call"}, set()]


def test_turning_the_setting_off_closes_the_ear_at_once(client):
    client.post("/api/settings", json={"wake_word": False})
    assert client.runtime.ear._paused == {"setting"}
    client.post("/api/settings", json={"wake_word": True})
    assert client.runtime.ear._paused == set()


def test_the_ear_knows_whether_jarvis_speaks_into_headphones(client):
    report(client, headphones=True)
    assert client.runtime.ear.headphones is True
    report(client, headphones=False)
    assert client.runtime.ear.headphones is False


def test_the_state_says_whether_the_ear_is_on_and_awake(client):
    assert client.get("/api/state").json()["ear"] == {"on": False, "awake": False}
    client.runtime.ear._phase = ear.ASLEEP
    assert client.get("/api/state").json()["ear"] == {"on": True, "awake": False}
    client.runtime.ear._phase = ear.FOLLOW_UP
    assert client.get("/api/state").json()["ear"] == {"on": True, "awake": True}


def test_what_the_ear_takes_is_answered_like_a_spoken_message(fake_home):
    from jarvis.agents import AgentError

    async def scene():
        runtime = Runtime(load_config())
        said = []

        async def talk(text, *, spoken=False, turn_id=None):
            said.append((text, spoken))
            if len(said) == 2:
                raise AgentError("Claude could not be reached")  # shown as the turn's error, nothing more
            return {}

        runtime.talk = talk
        runtime.ear._on_message("What time is it?")
        runtime.ear._on_message("And tomorrow?")
        await asyncio.sleep(0.01)
        assert not runtime._tasks  # both done, the failure included
        assert runtime.ear._on_listening == runtime.warm_voice  # the conversation gets ready while the user speaks
        return said

    assert asyncio.run(scene()) == [("What time is it?", True), ("And tomorrow?", True)]


def test_deploy_fetches_the_ears_voice_model_once_the_release_is_set_up(fake_home, monkeypatch):
    steps: list[str] = []

    def run(command, *, check=True, cwd=None):
        steps.append(" ".join(Path(part).name if index == 0 else part for index, part in enumerate(command[:2])))
        return SimpleNamespace(returncode=0, stdout="0123456789abcdef\n" if "rev-parse" in command else "", stderr="")

    monkeypatch.setattr(install.shutil, "which", lambda name: f"/opt/{name}")
    monkeypatch.setattr(install, "_run", run)
    monkeypatch.setattr(install, "build_interface", lambda progress: None)
    monkeypatch.setattr(install.shutil, "copytree", lambda source, target: steps.append("copy the interface"))
    monkeypatch.setattr(ear, "download", lambda: steps.append("fetch the voice model") or ear.folder())
    said: list[str] = []
    assert install.deploy(progress=said.append) == "0123456"
    assert steps.index("fetch the voice model") == steps.index("uv sync") + 1
    assert not any(line.startswith("Warning") for line in said)

    def offline():
        raise OSError("no network")

    monkeypatch.setattr(ear, "download", offline)
    said.clear()
    assert install.deploy(progress=said.append) == "0123456"  # deployed all the same
    assert any(line.startswith("Warning") and "no network" in line for line in said)
