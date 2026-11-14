"""Keeping a spoken conversation going: the short lines Jarvis says himself, and when a conversation is over."""

import asyncio

import pytest
from claude_agent_sdk import ResultMessage, StreamEvent

from jarvis import manner, runtime as runtime_module, speak
from jarvis.config import load_config
from jarvis.core import Answer, Core, stamp
from jarvis.runtime import Runtime


@pytest.fixture
def runtime(fake_home, monkeypatch):
    monkeypatch.setattr("jarvis.speak.synthesize", lambda *args: b"RIFF")
    made = Runtime(load_config())
    monkeypatch.setattr(made, "_may_speak", lambda: True)
    made.ended = []
    monkeypatch.setattr(made.ear, "end_conversation", lambda: made.ended.append(True), raising=False)
    return made


def _events(queue) -> list[dict]:
    found = []
    while not queue.empty():
        found.append(queue.get_nowait())
    return found


def _said(events: list[dict]) -> list[str]:
    return [event["text"] for event in events if event["type"] == "speech"]


def _model(*steps, reply: str):
    """A stand-in for the model: each step is text written so far, or ("tool", name), or ("wait", seconds)."""
    asked = []

    async def talk(text, *, spoken=False, named=True, on_step=None, on_text=None, on_tool=None):
        asked.append((text, spoken, named))
        for step in steps:
            if isinstance(step, tuple) and step[0] == "tool":
                on_tool(step[1])
            elif isinstance(step, tuple) and step[0] == "wait":
                await asyncio.sleep(step[1])
            else:
                on_text(step)
        return Answer(reply, [], 0.1)

    return talk, asked


# --- the lines themselves ---------------------------------------------------------


@pytest.mark.parametrize("message", [
    "What's on my calendar tomorrow?", "Did the client reply to my mail?", "Check my inbox", "Find the latest deck",
    "Draft a reply saying I'll send it on Friday", "Could you prepare me for the review?",
])
def test_a_request_that_takes_work_is_acknowledged(message):
    assert manner.needs_work(message)


@pytest.mark.parametrize("message", ["What time is it?", "How are you?", "Tell me a joke", "Good morning"])
def test_small_talk_and_what_the_model_knows_get_no_line(message):
    assert not manner.needs_work(message)


def test_a_thing_to_do_is_answered_with_on_it_and_a_question_with_checking():
    assert manner.acknowledgement("Could you draft a reply to it?") in manner.DOING
    assert manner.acknowledgement("Please send the mail to the team") in manner.DOING
    assert manner.acknowledgement("What's on my calendar tomorrow?") in manner.LOOKING


def test_the_lines_take_turns():
    said = {manner.acknowledgement("What's in my inbox?") for _ in manner.LOOKING}
    assert said == set(manner.LOOKING)


def test_a_slow_tool_has_a_line_and_a_quick_one_none():
    assert manner.starting("mcp__jarvis__web") == "Let me look that up."
    assert manner.starting("mcp__memory__search") == "One moment."
    assert manner.starting("mcp__jarvis__remind") is None


def test_a_long_job_says_what_it_is_doing_and_then_something_else():
    first, second = manner.progress("mcp__jarvis__web", 0), manner.progress("mcp__jarvis__web", 1)
    assert first == "Still searching the web." and second != first
    assert manner.progress("mcp__jarvis__remind", 0) == manner.WORKING[0]
    assert manner.progress("mcp__memory__search", 5) == manner.PROGRESS["search"][-1]


@pytest.mark.parametrize("sentence", [
    "Let me check your calendar.", "Sure, I'll look into that.", "One moment.", "I'm checking your mail now.",
    "Let's see.",
])
def test_saying_only_that_he_is_about_to_look_is_a_preamble(sentence):
    assert manner.is_preamble(sentence)


@pytest.mark.parametrize("sentence", [
    "Let me check: you have three meetings tomorrow.", "Let me know which one you mean.",
    "You have three meetings tomorrow.", "Let me draft that for you.",
])
def test_a_sentence_that_says_more_is_not_a_preamble(sentence):
    assert not manner.is_preamble(sentence)


@pytest.mark.parametrize("message", [
    "That's all, thanks Jarvis.", "No, that's it.", "Thanks.", "Thank you so much", "OK, I'm good.", "Bye!",
    "That's all for now", "Never mind.",
])
def test_the_user_ends_the_conversation(message):
    assert manner.ending(message) in manner.CLOSING


@pytest.mark.parametrize("message", ["Stop.", "OK stop talking please", "Quiet", "That's enough."])
def test_wanting_him_quiet_gets_no_answer_at_all(message):
    assert manner.ending(message) == ""


@pytest.mark.parametrize("message", [
    "Thanks, and check the mail too", "That's all I needed to know about the budget, what's next?", "Stop the recording",
])
def test_a_message_that_goes_on_does_not_end_it(message):
    assert manner.ending(message) is None


def test_the_reply_for_someone_else_is_known_and_held_back_until_known():
    assert manner.not_for_me("[not for me]") and manner.not_for_me(" [Not for me]. ")
    assert manner.maybe_not_for_me("[") and manner.maybe_not_for_me("[not for")
    assert not manner.maybe_not_for_me("Tomorrow at ten.") and not manner.maybe_not_for_me("")
    assert not manner.not_for_me("It is not for me to say.")


def test_the_lines_said_the_moment_the_user_stops_are_made_ahead():
    assert {"Right, checking.", "On it.", "Very good."} <= set(manner.AT_ONCE)
    assert len(manner.AT_ONCE) == len(set(manner.AT_ONCE))


# --- in a conversation --------------------------------------------------------------


def test_a_spoken_request_that_takes_work_is_acknowledged_at_once_and_his_own_let_me_check_is_not_said(runtime, monkeypatch):
    talk, asked = _model(
        "Let me check your calendar.", ("tool", "mcp__memory__search"), "You have two meetings tomorrow.",
        reply="You have two meetings tomorrow.",
    )
    monkeypatch.setattr(runtime.core, "talk", talk)

    async def scene():
        queue = runtime.hub.join()
        await runtime.talk("What's on my calendar tomorrow?", spoken=True)
        return _events(queue)

    said = _said(asyncio.run(scene()))
    assert said[0] in manner.LOOKING
    assert said[1:] == ["You have two meetings tomorrow."]


def test_a_typed_request_says_nothing(runtime, monkeypatch):
    talk, _ = _model(reply="You have two meetings tomorrow.")
    monkeypatch.setattr(runtime.core, "talk", talk)

    async def scene():
        queue = runtime.hub.join()
        await runtime.talk("What's on my calendar tomorrow?")
        return _events(queue)

    assert _said(asyncio.run(scene())) == []


def test_what_he_wrote_before_a_tool_is_said_as_the_tool_begins(runtime, monkeypatch):
    talk, _ = _model(
        "Let me look at the budget sheet.", ("tool", "mcp__memory__search"), ("wait", 0.01), "It is within the limit.",
        reply="It is within the limit.",
    )
    monkeypatch.setattr(runtime.core, "talk", talk)
    order = []
    original = runtime._speak
    monkeypatch.setattr(runtime, "_speak", lambda *args, **kwargs: order.append(args[2]) or original(*args, **kwargs))

    async def scene():
        await runtime.talk("Hmm, and the budget?", spoken=True)

    asyncio.run(scene())
    assert order == ["Let me look at the budget sheet.", "It is within the limit."]  # nothing else: it was said


def test_a_slow_tool_begun_with_nothing_said_is_announced(runtime, monkeypatch):
    talk, _ = _model(("tool", "mcp__jarvis__web"), "It was released last week.", reply="It was released last week.")
    monkeypatch.setattr(runtime.core, "talk", talk)

    async def scene():
        queue = runtime.hub.join()
        await runtime.talk("Hmm, when did that come out?", spoken=True)
        return _events(queue)

    assert _said(asyncio.run(scene())) == ["Let me look that up.", "It was released last week."]


def test_a_long_job_gets_a_word_of_progress(runtime, monkeypatch):
    monkeypatch.setattr(runtime_module, "PROGRESS_AFTER_SECONDS", 0.05)
    monkeypatch.setattr(runtime_module, "MIND_EVERY_SECONDS", 0.01)
    monkeypatch.setattr(runtime_module, "SPOKEN_CHARS_PER_SECOND", 1e6)
    talk, _ = _model(("tool", "mcp__jarvis__refresh"), ("wait", 0.3), reply="Nothing new since this morning.")
    monkeypatch.setattr(runtime.core, "talk", talk)

    async def scene():
        queue = runtime.hub.join()
        await runtime.talk("Anything new in my inbox?", spoken=True)
        return _events(queue)

    said = _said(asyncio.run(scene()))
    assert said[0] in manner.LOOKING
    assert said[1:] == ["Still fetching the latest.", "Nearly there.", "Nothing new since this morning."]


def test_thats_all_ends_the_conversation_without_a_model(runtime, monkeypatch):
    monkeypatch.setattr(runtime.core, "talk", lambda *args, **kwargs: pytest.fail("no model is asked"))

    async def scene():
        queue = runtime.hub.join()
        reply = await runtime.talk("That's all, thanks Jarvis.", spoken=True)
        return reply, _events(queue)

    reply, events = asyncio.run(scene())
    assert reply["text"] in manner.CLOSING and reply["speak"] is True
    assert _said(events) == [reply["text"]]
    assert runtime.ended == [True]


def test_stop_ends_it_in_silence(runtime, monkeypatch):
    monkeypatch.setattr(runtime.core, "talk", lambda *args, **kwargs: pytest.fail("no model is asked"))

    async def scene():
        queue = runtime.hub.join()
        reply = await runtime.talk("Stop.", spoken=True)
        return reply, _events(queue)

    reply, events = asyncio.run(scene())
    assert reply["text"] == "" and _said(events) == []
    assert runtime.ended == [True]


def test_typed_thanks_is_answered_by_the_model(runtime, monkeypatch):
    talk, asked = _model(reply="Any time.")
    monkeypatch.setattr(runtime.core, "talk", talk)
    asyncio.run(runtime.talk("Thanks."))
    assert asked == [("Thanks.", False, True)] and runtime.ended == []


def test_a_follow_up_meant_for_someone_else_is_taken_back_and_nothing_is_said(runtime, monkeypatch):
    talk, asked = _model("[not", "[not for me]", reply="[not for me]")
    monkeypatch.setattr(runtime.core, "talk", talk)

    async def scene():
        queue = runtime.hub.join()
        reply = await runtime.talk("Can you check the build on your side?", spoken=True, named=False)
        return reply, _events(queue)

    reply, events = asyncio.run(scene())
    assert asked == [("Can you check the build on your side?", True, False)]
    assert _said(events) == []  # not even "Right, checking.": without the name it may not be his to answer
    assert not [event for event in events if event["type"] == "reply_delta"]
    assert reply["ignored"] is True and reply["text"] == ""
    assert [event["ignored"] for event in events if event["type"] == "reply"] == [True]
    assert all(line["text"] != "Can you check the build on your side?" for line in runtime.conversation)
    assert runtime.ended == [True]


def test_a_follow_up_meant_for_him_is_answered(runtime, monkeypatch):
    talk, _ = _model("At four.", reply="At four.")
    monkeypatch.setattr(runtime.core, "talk", talk)

    async def scene():
        queue = runtime.hub.join()
        await runtime.talk("And the one after that?", spoken=True, named=False)
        return _events(queue)

    assert _said(asyncio.run(scene())) == ["At four."]
    assert runtime.ended == []


def test_the_ear_says_whether_a_message_began_with_the_name(runtime, monkeypatch):
    heard = []

    async def talk(text, *, spoken=False, named=True, turn_id=None):
        heard.append((text, spoken, named))

    monkeypatch.setattr(runtime, "talk", talk)

    async def scene():
        runtime.ear.by_name = False
        runtime._heard_by_ear("And tomorrow?")
        await asyncio.sleep(0.01)

    asyncio.run(scene())
    assert heard == [("And tomorrow?", True, False)]


# --- the model's side ---------------------------------------------------------------


def test_the_model_is_told_when_a_spoken_message_came_without_the_name():
    assert "[spoken, no name] And tomorrow?" in stamp("And tomorrow?", spoken=True, named=False)
    assert "[spoken] And tomorrow?" in stamp("And tomorrow?", spoken=True)
    assert "[typed] And tomorrow?" in stamp("And tomorrow?", named=False)


def test_a_tool_is_known_as_the_model_begins_to_write_it(fake_home):
    from types import SimpleNamespace

    conversation = Core(load_config(), SimpleNamespace())
    tools = []

    class Session:
        async def query(self, prompt):
            pass

        async def receive_response(self):
            yield StreamEvent(uuid="1", session_id="s", event={"type": "message_start"})
            block = {"type": "tool_use", "id": "t1", "name": "mcp__jarvis__refresh", "input": {}}
            yield StreamEvent(uuid="2", session_id="s", event={"type": "content_block_start", "index": 0, "content_block": block})
            yield ResultMessage(
                subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="s",
                result="Nothing new.",
            )

    conversation._client = Session()
    assert asyncio.run(conversation._turn("Anything new?", None, None, tools.append)) == "Nothing new."
    assert tools == ["mcp__jarvis__refresh"]


def test_a_name_the_user_said_how_to_say_is_said_that_way_and_shown_as_written(runtime, monkeypatch):
    from jarvis import vocabulary
    from jarvis.store import open_store

    spoken = []
    monkeypatch.setattr("jarvis.speak.synthesize", lambda text, before="", keep=False: spoken.append(text) or b"RIFF")
    with open_store() as store:
        vocabulary.pronounce(store, "Quorvia", "Kor-vee-ah")

    async def scene():
        queue = runtime.hub.join()
        runtime._speak("s1", 0, "Quorvia replied this morning.")
        await asyncio.sleep(0.05)
        return _events(queue)

    events = asyncio.run(scene())
    assert spoken == ["Kor-vee-ah replied this morning."]
    assert [event["text"] for event in events if event["type"] == "speech"] == ["Quorvia replied this morning."]


# --- the voice keeps his lines ------------------------------------------------------


def test_a_line_is_made_once_by_each_voice_and_kept(fake_home, monkeypatch):
    made = []
    monkeypatch.setattr(speak, "engine", lambda: "system")
    monkeypatch.setattr(speak, "_made", lambda text, previous: made.append((text, previous)) or (b"RIFF-a", "system"))
    assert speak.synthesize("On it.", "What came before.", keep=True) == b"RIFF-a"
    assert speak.synthesize("On it.", keep=True) == b"RIFF-a"
    assert made == [("On it.", "")]  # made once, and without the tune of what came before
    speak.synthesize("A sentence of a reply.")
    speak.synthesize("A sentence of a reply.")
    assert made[1:] == [("A sentence of a reply.", ""), ("A sentence of a reply.", "")]  # a reply is never kept


def test_a_line_made_by_a_stand_in_voice_is_kept_as_that_voices(fake_home, monkeypatch):
    monkeypatch.setattr(speak, "engine", lambda: "elevenlabs")
    monkeypatch.setattr(speak, "_made", lambda text, previous: (b"RIFF-kokoro", "kokoro"))
    speak.synthesize("One moment.", keep=True)
    assert not speak._kept("elevenlabs", "One moment.").exists()
    assert speak._kept("kokoro", "One moment.").read_bytes() == b"RIFF-kokoro"
