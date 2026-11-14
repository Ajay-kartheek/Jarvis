"""Stopping Jarvis: whatever he was saying stops, and nothing more of it is made."""

import asyncio
import threading
from types import SimpleNamespace

import pytest
from claude_agent_sdk import ResultMessage, StreamEvent
from fastapi.testclient import TestClient

from jarvis.config import load_config
from jarvis.core import Answer, Core
from jarvis.runtime import Runtime
from jarvis.server import create_app


@pytest.fixture
def runtime(fake_home, monkeypatch):
    monkeypatch.setattr("jarvis.speak.synthesize", lambda text: b"RIFF")
    return Runtime(load_config())


def _speech(queue) -> list[tuple[str, int]]:
    said = []
    while not queue.empty():
        event = queue.get_nowait()
        if event["type"] == "speech":
            said.append((event["id"], event["seq"]))
    return said


def test_a_hush_stops_the_rest_of_what_jarvis_was_saying(runtime):
    async def scene():
        queue = runtime.hub.join()
        assert runtime._speak("s1", 0, "The first of three sentences.")
        runtime.hush()
        assert not runtime._speak("s1", 1, "The second of them.")
        assert runtime._speak("s2", 0, "Something new.")  # what he says next is not affected
        return _speech(queue)

    assert asyncio.run(scene()) == [("s1", 0), ("s2", 0)]


def test_a_reply_that_has_not_begun_to_sound_is_stopped_too(runtime):
    async def scene():
        runtime._speaking_turn = "t7"
        runtime.hush()
        return runtime._speak("t7", 0, "Here is the answer.")

    assert asyncio.run(scene()) is False


def test_the_sound_of_a_sentence_cut_short_is_not_handed_out(runtime, monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr("jarvis.speak.synthesize", lambda text: gate.wait(5) and b"RIFF")

    async def scene():
        runtime._speak("s1", 0, "A long sentence that takes a while to make.")
        runtime.hush()
        try:
            return await runtime.speech_audio("s1", 0)
        finally:
            gate.set()

    assert asyncio.run(scene()) is None


def test_a_new_message_stops_jarvis_and_cuts_short_the_answer_he_was_writing(runtime, monkeypatch):
    calls = []

    async def interrupt():
        calls.append("interrupt")

    async def talk(text, *, spoken=False, on_step=None, on_text=None):
        calls.append(text)
        return Answer("Fine.", [], 0.1)

    monkeypatch.setattr(runtime.core, "interrupt", interrupt)
    monkeypatch.setattr(runtime.core, "talk", talk)
    runtime.settings["speak"] = False

    async def scene():
        runtime._speak("s1", 0, "Something long.")
        await runtime.talk("Wait, what is next?")
        return runtime._speak("s1", 1, "The rest of it.")

    assert asyncio.run(scene()) is False
    assert calls == ["interrupt", "Wait, what is next?"]


def test_an_answer_cut_short_ends_quietly_with_what_was_written(fake_home):
    conversation = Core(load_config(), SimpleNamespace())

    class Session:
        async def query(self, prompt):
            pass

        async def interrupt(self):
            pass

        async def receive_response(self):
            yield StreamEvent(uuid="1", session_id="s", event={"type": "message_start"})
            delta = {"type": "text_delta", "text": "The rehearsal is"}
            yield StreamEvent(uuid="2", session_id="s", event={"type": "content_block_delta", "delta": delta})
            await conversation.interrupt()
            yield ResultMessage(
                subtype="error_during_execution", duration_ms=1, duration_api_ms=1, is_error=True, num_turns=1,
                session_id="s", terminal_reason="aborted_streaming",
            )

    conversation._client = Session()
    assert asyncio.run(conversation._turn("When is it?", None, None)) == "The rehearsal is"


def test_nothing_to_cut_short_when_no_answer_is_being_written(fake_home):
    conversation = Core(load_config(), SimpleNamespace())
    conversation._client = SimpleNamespace(interrupt=lambda: pytest.fail("no answer was being written"))
    asyncio.run(conversation.interrupt())


def test_the_page_can_stop_jarvis_talking(fake_home, monkeypatch):
    app = create_app(load_config())
    hushed = []
    monkeypatch.setattr(app.state.runtime, "hush", lambda: hushed.append(True))
    with TestClient(app) as client:
        assert client.post("/api/voice/stop").json() == {"ok": True}
    assert hushed == [True]
