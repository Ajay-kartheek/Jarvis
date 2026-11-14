"""The ear: hearing "Jarvis" at the start of an utterance, taking the words that follow, the follow-up, and being
interrupted.

No test loads a model, opens a microphone, makes a sound or uses the network. The sound is made up: every word is
a quarter of a second of its own sample value, which the stand-ins for the voice model and the recogniser read
back, so a stretch of sound cut anywhere holds exactly the words said in it, and a word is recognised once most of
it has been heard. Unless a test says otherwise the work that normally goes to the ear's thread runs in place, so
every event can be tied to the sound that caused it. The figures these tests run with, and why, are in jarvis/ear.py.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import logging
import threading

import numpy as np
import pytest

from jarvis import ear, speech

RATE = ear.SAMPLE_RATE
WORD = 0.25  # seconds a word takes
QUIET, BABBLE, NOISE, COUGH, JARVIS = 0.0, 0.1, 0.2, 0.45, 0.7  # sample values: none, voices far off, a door, a cough
# (the voice model takes babble, a cough and Jarvis for a voice; the recogniser makes words of none of them)
LEFT_OVER = 0.0002  # what is left of Jarvis's voice once the app has cancelled it (-74 dBFS): a voice, too faint for words
SPOKEN: dict[float, str] = {}  # each word said in a test, by the sample value that stands for it
HIS = "It is ten past nine and the next meeting is at eleven.".split()  # what Jarvis says, from the speakers
MODEL = object()  # what speech.acquire hands out


def quiet(seconds: float) -> np.ndarray:
    return np.full(int(seconds * RATE), QUIET, dtype=np.float32)


def noise(seconds: float) -> np.ndarray:
    """A door, a keyboard: loud, and not a voice."""
    return np.full(int(seconds * RATE), NOISE, dtype=np.float32)


def babble(seconds: float) -> np.ndarray:
    """Other people talking further off, without a break: a voice to the voice model, 14 dB below the user's words."""
    return np.full(int(seconds * RATE), BABBLE, dtype=np.float32)


def words(text: str) -> np.ndarray:
    """The user saying ``text``, a quarter of a second for each word."""
    parts = []
    for word in text.split():
        value = np.float32(0.5 + 0.0001 * (len(SPOKEN) + 1))
        SPOKEN[float(value)] = word
        parts.append(np.full(int(WORD * RATE), value, dtype=np.float32))
    return np.concatenate(parts)


def call() -> np.ndarray:
    """ "Hey Jarvis," """
    return words("Hey Jarvis,")


def name() -> np.ndarray:
    """ "Jarvis," on its own."""
    return words("Jarvis,")


def jarvis(seconds: float) -> np.ndarray:
    """Jarvis's own voice, as the microphone hears it from the speakers."""
    return np.full(int(seconds * RATE), JARVIS, dtype=np.float32)


def left_over(seconds: float) -> np.ndarray:
    """Jarvis's own voice as the microphone hears it when the app cancels it: faint, but still a voice."""
    return np.full(int(seconds * RATE), LEFT_OVER, dtype=np.float32)


def recognised(sound: np.ndarray) -> list[tuple[str, float, float]]:
    """What the recogniser stand-in makes of ``sound``: each word of which most was heard, with its times."""
    found = []
    edges = np.flatnonzero(np.diff(sound)) + 1
    start = 0
    for stretch in np.split(sound, edges):
        if not len(stretch):
            continue
        value, begin, end = float(stretch[0]), start / RATE, (start + len(stretch)) / RATE
        start += len(stretch)
        if value in SPOKEN and end - begin >= 0.8 * WORD:
            found.append((" " + SPOKEN[value], begin, end))
        elif value == np.float32(JARVIS):
            for index in range(int((end - begin) / WORD)):
                found.append((" " + HIS[index % len(HIS)], begin + index * WORD, begin + (index + 1) * WORD))
    return found


def transcript(sound: np.ndarray) -> str:
    return "".join(piece for piece, _, _ in recognised(sound)).strip()


class FakeVoice:
    """Takes the user's words, a cough and Jarvis's own voice for a voice, and nothing else."""

    def __init__(self) -> None:
        self.resets = self.windows = 0
        self.threads: set[int] = set()
        self._left = np.zeros(0, dtype=np.float32)

    def reset(self) -> None:
        self.resets += 1
        self._left = self._left[:0]

    def heard(self, samples: np.ndarray) -> list[float]:
        self.threads.add(threading.get_ident())
        sound = np.concatenate([self._left, samples])
        whole = len(sound) - len(sound) % ear.WINDOW
        self._left = sound[whole:]
        self.windows += whole // ear.WINDOW
        return [0.9 if window.mean() > 0.4 or (window == np.float32(BABBLE)).any()
                or (window == np.float32(LEFT_OVER)).any() else 0.05
                for window in sound[:whole].reshape(-1, ear.WINDOW)]


class FakeUtterance:
    def __init__(self, model, words=()) -> None:
        assert model is MODEL
        self.sound = np.zeros(0, dtype=np.float32)
        self.threads: set[int] = set()
        World.utterances.append(self)

    def add(self, samples: np.ndarray) -> None:
        self.sound = np.concatenate([self.sound, samples])

    def step(self) -> str:
        self.threads.add(threading.get_ident())
        return transcript(self.sound)

    def finish(self) -> str:
        return transcript(self.sound)


class Socket:
    """The app's end of a connection: sound in frames, and things to do at given points in it."""

    def __init__(self, world: "World", parts, frame: int = 1600, threads: bool = False) -> None:
        self.world = world
        self.parts = list(parts)
        self.frame = frame * 4  # in bytes
        self.threads = threads
        self.pending = b""
        self.sent: list[dict] = []

    async def receive(self) -> dict:
        await self.world.settle(self.threads)
        while not self.pending and self.parts:
            part = self.parts.pop(0)
            if callable(part):
                part()
                await self.world.settle(self.threads)
            elif isinstance(part, (bytes, str)):
                return {"type": "websocket.receive", "text" if isinstance(part, str) else "bytes": part}
            else:
                self.pending = np.asarray(part, dtype="<f4").tobytes()
        if not self.pending:
            return {"type": "websocket.disconnect"}
        data, self.pending = self.pending[: self.frame], self.pending[self.frame:]
        self.world.clock += len(data) / 4 / RATE
        return {"type": "websocket.receive", "bytes": data}

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)


class World:
    """An ear with stand-ins for everything it listens with, and a record of all it did."""

    utterances: list[FakeUtterance] = []

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        World.utterances = []
        self.clock = 0.0  # seconds of sound the app has sent
        self.events: list[tuple[float, dict]] = []
        self.messages: list[str] = []
        self.voices: list[FakeVoice] = []
        self.decoded: list[tuple[float, float]] = []  # (when, seconds of sound) of each look at an utterance's start
        self.decoding: set[int] = set()  # the threads that did that
        self.leaned: list[bool] = []  # ...and whether a reading leaning towards the name was asked for
        self.leaning: dict[str, str] = {}  # what that reading says, by what the plain one said (else the same)
        self.listening = 0  # times the ear said a message began
        self.held = self.acquired = self.released = 0
        self.acquiring: set[int] = set()  # the threads that asked for the recogniser
        self.loading: asyncio.Event | None = None  # set it to let a recogniser that is "loading" arrive
        self.broken: Exception | None = None  # the recogniser cannot be had
        self.failing: Exception | None = None  # the recogniser fails on the sound
        self.ear = ear.Ear(self._publish, self._on_message, on_listening=self._on_listening)
        monkeypatch.setattr(ear, "_load", self._load)
        monkeypatch.setattr(ear, "_utterance", FakeUtterance)
        monkeypatch.setattr(ear, "_recognise", self._recognise)
        # The stand-in puts the end of a word where it is; the real recogniser puts the end of the name a little early,
        # which the ear makes up for. Here the message starts where the name ends.
        monkeypatch.setattr(ear, "CUT_SECONDS", 0.0)
        # A stand-in word is a stretch of one level, which after two seconds the ear would take for a background of
        # voices; only the tests about such a background let the ear look for one.
        monkeypatch.setattr(ear, "BACKGROUND_SECONDS", 10_000.0)
        monkeypatch.setattr(speech, "acquire", self._acquire)
        monkeypatch.setattr(speech, "release", self._release)
        monkeypatch.setattr(speech, "unload_if_idle", lambda seconds: False)
        self.busy = 0  # things under way on the ear's own thread
        self._own_thread = ear.Ear._work
        monkeypatch.setattr(ear.Ear, "_work", lambda listener, function, *args: self._in_place(function, *args))

    def _load(self):
        self.voices.append(FakeVoice())
        return self.voices[-1]

    def _recognise(self, model, samples: np.ndarray, lean: bool = False):
        assert model is MODEL
        if self.failing:
            raise self.failing
        self.decoded.append((round(self.clock, 2), len(samples) / RATE))
        self.decoding.add(threading.get_ident())
        self.leaned.append(lean)
        plain = recognised(samples)
        if not lean:
            return plain, None
        text = "".join(piece for piece, _, _ in plain).strip()
        other = self.leaning.get(text, text).split()
        begin, end = (plain[0][1], plain[-1][2]) if plain else (0.0, len(samples) / RATE)
        step = (end - begin) / max(1, len(other))  # the words where the plain reading heard them, about
        return plain, [(" " + word, begin + index * step, begin + (index + 1) * step)
                       for index, word in enumerate(other)]

    def _acquire(self):
        if self.broken:
            raise self.broken
        self.acquiring.add(threading.get_ident())
        self.acquired += 1
        self.held += 1
        return MODEL

    def _release(self) -> None:
        self.released += 1
        self.held -= 1

    def _in_place(self, function, *args) -> asyncio.Future:
        """Instead of the ear's own thread: done at once, where it is asked for."""
        if function is ear._recogniser and self.loading is not None:
            return asyncio.ensure_future(self._when_loaded())
        done = asyncio.get_running_loop().create_future()
        try:
            done.set_result(function(*args))
        except Exception as exc:
            done.set_exception(exc)
        return done

    async def _when_loaded(self):
        await self.loading.wait()
        return self._acquire()

    def _on_its_thread(self, listener, function, *args) -> asyncio.Future:
        """The ear's own thread after all, keeping count of what is under way on it."""
        self.busy += 1
        started = self._own_thread(listener, function, *args)
        started.add_done_callback(lambda _: setattr(self, "busy", self.busy - 1))
        return started

    def _publish(self, event: dict) -> None:
        self.events.append((round(self.clock, 2), event))

    def _on_message(self, text: str) -> None:
        self.events.append((round(self.clock, 2), {"type": "message", "text": text}))
        self.messages.append(text)

    def _on_listening(self) -> None:
        self.listening += 1

    async def settle(self, threads: bool = False) -> None:
        """Let the ear finish with the sound it has been sent."""
        idle = 0
        while idle < 5:  # between two steps of sound the ear's thread is idle for a turn of the loop at most
            await asyncio.sleep(0.0005 if threads else 0)
            idle = idle + 1 if self.busy == 0 else 0

    def play(self, *parts, frame: int = 1600, threads: bool = False, echo: str | None = None) -> Socket:
        """One connection: the parts in order, then it closes. ``echo``: what the app says of Jarvis's voice in it."""
        socket = Socket(self, parts, frame, threads)
        if threads:
            # put back by monkeypatch when the test is over
            ear.Ear._work = lambda listener, function, *args: self._on_its_thread(listener, function, *args)
        asyncio.run(self._serve(socket, echo))
        return socket

    async def _serve(self, socket: Socket, echo: str | None = None) -> None:
        await self.ear.serve(socket, echo=echo)
        await self.settle()

    # --- reading the record ---

    def told(self, *, levels: bool = False) -> list[tuple]:
        """What was published, in order, in short: ("ear", "awake"), ("voice", "heard", text), ("message", text)."""
        short = []
        for _, event in self.events:
            if event["type"] == "level" and not levels:
                continue
            short.append(tuple(str(event[key]) for key in ("type", "phase", "text") if key in event))
        return short

    def when(self, *short: str) -> list[float]:
        """The times (seconds of sound sent) at which such an event was published."""
        return [
            at for at, event in self.events
            if tuple(str(event[key]) for key in ("type", "phase", "text") if key in event)[: len(short)] == short
        ]

    def partials(self) -> list[str]:
        return [event["text"] for _, event in self.events if event.get("phase") == "partial"]


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    SPOKEN.clear()
    return World(monkeypatch)


ASLEEP, AWAKE, OFF = ("ear", "asleep"), ("ear", "awake"), ("ear", "off")
LISTENING, CANCELLED = ("voice", "listening"), ("voice", "cancelled")
MOST_LOOKS = int((ear.SPOT_SECONDS - ear.SPOT_FIRST_SECONDS) / ear.SPOT_EVERY_SECONDS) + 1  # at one utterance's start


def heard(text: str) -> list[tuple]:
    return [("voice", "heard", text), ("message", text)]


def without_partials(told: list[tuple]) -> list[tuple]:
    return [event for event in told if event[:2] != ("voice", "partial")]


def spoken_values(sound: np.ndarray) -> list[str]:
    """The words whose sound is in ``sound``, however little of each."""
    return [SPOKEN[value] for value in dict.fromkeys(float(sample) for sample in sound) if value in SPOKEN]


# --- asleep -------------------------------------------------------------------


def test_the_ear_is_off_until_the_app_connects_and_asleep_while_it_listens_for_the_name(world):
    seen = []
    assert world.ear.state == "off"
    world.play(quiet(1), lambda: seen.append(world.ear.state), quiet(1))
    assert seen == ["asleep"] and world.ear.state == "off"
    assert world.told() == [ASLEEP, OFF]
    assert world.decoded == [] and world.acquired == 0  # no voice: the recogniser was never asked


def test_asleep_talk_that_does_not_begin_with_the_name_is_looked_at_briefly_and_let_go(world):
    world.play(quiet(1), words("the numbers look better than last quarter"), quiet(1), noise(2), jarvis(1.5),
               quiet(1))
    assert world.told() == [ASLEEP, OFF] and world.messages == []
    assert world.ear.stats["utterances"] == 2  # the talk, and the voice from the speakers
    assert 0 < len(world.decoded) <= 2 * MOST_LOOKS and world.ear.stats["wakes"] == 0
    assert world.acquired == world.released and world.held == 0  # given back after each


def test_under_voices_that_never_stop_the_name_still_wakes_the_ear_and_the_message_still_ends(world, monkeypatch):
    monkeypatch.setattr(ear, "BACKGROUND_SECONDS", 2.0)
    world.play(quiet(1), babble(7), words("Jarvis, what time is it?"), babble(5), frame=ear.FRAME)
    assert world.messages == ["What time is it?"]
    stopped = 1 + 7 + 5 * WORD
    assert world.when("voice", "heard")[0] - stopped <= ear.END_SECONDS + 0.2  # not when the talk nearby stops


def test_under_voices_that_never_stop_the_bare_name_between_pauses_is_not_a_call(world, monkeypatch):
    monkeypatch.setattr(ear, "BACKGROUND_SECONDS", 2.0)  # pauses told by loudness: too rough for the name alone
    world.play(quiet(1), babble(7), words("Jarvis,"), babble(3), words("Hey Jarvis, are you there?"), babble(3),
               frame=ear.FRAME)
    assert world.messages == ["Are you there?"] and world.told().count(LISTENING) == 1


def test_voices_that_never_stop_wake_nothing_and_are_looked_at_once(world, monkeypatch):
    monkeypatch.setattr(ear, "BACKGROUND_SECONDS", 2.0)
    world.play(quiet(1), babble(30), frame=ear.FRAME)
    assert world.told() == [ASLEEP, OFF] and world.ear.stats["utterances"] == 1 and world.held == 0


def test_the_door_and_the_keyboard_are_not_looked_at(world):
    world.play(quiet(1), noise(3), quiet(1), noise(0.2), quiet(1))
    assert world.decoded == [] and world.acquired == 0 and world.ear.stats["utterances"] == 0


def test_asleep_no_more_than_two_seconds_of_sound_are_kept(world):
    world.play(words("one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen"))
    assert len(world.ear._ring) == ear.RING_SECONDS * RATE


# --- the name, at the start of an utterance -------------------------------------


def test_hey_jarvis_wakes_the_ear_and_the_words_after_a_pause_become_a_message(world):
    seen = []
    world.play(quiet(2), call(), quiet(0.8), words("What is on my calendar today?"), quiet(1.5),
               lambda: seen.append(world.ear.state), quiet(0.5))
    assert without_partials(world.told()) == [ASLEEP, AWAKE, LISTENING, *heard("What is on my calendar today?"), OFF]
    assert world.messages == ["What is on my calendar today?"]
    assert seen == ["awake"]  # still awake: the answer and a follow-up are to come
    assert world.listening == 1


def test_the_bare_name_set_off_from_what_follows_wakes_the_ear(world):
    world.play(quiet(2), words("Jarvis, what time is my next meeting?"), quiet(2))
    assert world.messages == ["What time is my next meeting?"]
    assert all("jarvis" not in partial.lower() for partial in world.partials())
    assert transcript(world.utterances[0].sound) == "what time is my next meeting?"  # no more than a scrap of the name


def test_ok_jarvis_and_hi_jarvis_wake_the_ear(world):
    world.play(quiet(1), words("OK Jarvis, stop."), quiet(2), words("Hi Jarvis, are you there?"), quiet(2))
    assert world.messages == ["Stop.", "Are you there?"]


def test_the_bare_name_and_a_pause_wakes_the_ear_once_the_speaker_has_stopped(world):
    world.play(quiet(2), name(), quiet(1.0), words("open the notes from yesterday"), quiet(2), frame=ear.FRAME)
    assert world.messages == ["Open the notes from yesterday"]
    stopped = 2 + WORD
    woke = world.when("ear", "awake")[0]
    assert stopped + ear.PAUSE_SECONDS - 0.04 <= woke <= stopped + ear.PAUSE_SECONDS + 0.12  # it waits for the pause
    assert spoken_values(world.utterances[0].sound) == "open the notes from yesterday".split()


def test_the_name_in_the_middle_of_a_sentence_does_not_wake_the_ear(world):
    world.play(quiet(1), words("I am building Jarvis for myself."), quiet(2),
               words("so the idea is that Jarvis listens during calls"), quiet(2))
    assert world.told() == [ASLEEP, OFF] and world.messages == []


def test_the_bare_name_run_straight_on_into_a_sentence_does_not_wake_the_ear(world):
    world.play(quiet(1), words("Jarvis is still syncing the calendar."), quiet(2),
               words("Jarvis needs a restart after the update."), quiet(2))
    assert world.told() == [ASLEEP, OFF] and world.messages == []


def test_someone_else_called_does_not_wake_the_ear(world):
    world.play(quiet(1), words("Hey Travis, are you coming to lunch?"), quiet(2), words("Davis, the report is ready."),
               quiet(2), words("Hey, what time is it?"), quiet(2))
    assert world.told() == [ASLEEP, OFF]


def test_the_name_right_after_other_talk_without_a_pause_is_not_the_start_of_an_utterance(world):
    world.play(quiet(1), words("so I told them we would ship on Friday"), quiet(0.15),
               words("Jarvis, what time is it?"), quiet(2))
    assert world.told() == [ASLEEP, OFF]


def test_the_name_after_other_talk_and_a_pause_wakes_the_ear(world):
    world.play(quiet(1), words("so I told them we would ship on Friday"), quiet(0.6),
               words("Jarvis, what time is it?"), quiet(2))
    assert world.messages == ["What time is it?"]
    assert "Friday" not in spoken_values(world.utterances[0].sound)


def test_the_ear_wakes_soon_after_the_name_has_been_said(world):
    world.play(quiet(2), call(), quiet(3), frame=ear.FRAME)
    assert world.when("ear", "awake") == world.when("voice", "listening")
    ended = 2 + 2 * WORD
    assert ended - 0.1 < world.when("ear", "awake")[0] <= ended + ear.SPOT_EVERY_SECONDS + 0.08


# --- what it costs ----------------------------------------------------------------


def test_an_utterance_that_has_not_begun_with_the_name_within_two_seconds_is_not_looked_at_again(world):
    held = []
    world.play(quiet(1), words(" ".join(["again"] * 24)), lambda: held.append(world.held),
               words("Jarvis, are you there?"), quiet(2), frame=ear.FRAME)  # no pause before the name
    assert world.told() == [ASLEEP, OFF]
    assert held == [0]  # given back at the cut-off, while the talk went on
    assert len(world.decoded) == MOST_LOOKS
    assert max(seconds for _, seconds in world.decoded) <= ear.SPOT_PREROLL_SECONDS + 0.2 + ear.SPOT_SECONDS
    assert max(at for at, _ in world.decoded) <= 1 + 0.2 + ear.SPOT_SECONDS


def test_after_the_next_pause_the_name_is_looked_for_again(world):
    world.play(quiet(1), words(" ".join(["again"] * 24)), quiet(0.6), words("Jarvis, are you there?"), quiet(2))
    assert world.messages == ["Are you there?"]


def test_the_start_of_an_utterance_is_looked_at_every_so_often_not_at_every_step(world):
    world.play(quiet(1), words("good grief the train to the coast leaves at a quarter past nine"), quiet(2),
               frame=ear.FRAME)
    times = [at for at, _ in world.decoded]
    assert len(times) == MOST_LOOKS
    gaps = np.diff(times)
    assert all(abs(gap - ear.SPOT_EVERY_SECONDS) < 0.09 for gap in gaps)


def test_talk_whose_first_words_are_plainly_not_the_name_is_let_go_at_once(world):
    held = []
    world.play(quiet(1), words("the train to the coast leaves at a quarter past nine"), lambda: held.append(world.held),
               quiet(2), words("so George said the build is green"), quiet(2), frame=ear.FRAME)
    first = [at for at, _ in world.decoded if at < 4]
    assert len(first) <= 2  # as soon as two words were heard: "the train"
    assert len(world.decoded) - len(first) == MOST_LOOKS  # "so George" could still have been a mishearing of a call
    assert held == [0] and world.told() == [ASLEEP, OFF]


def test_a_reading_leaning_towards_the_name_is_asked_for_only_at_the_last_look(world):
    world.play(quiet(1), words("good grief the train to the coast leaves at a quarter past nine"), quiet(2),
               words("good morning"), quiet(2), frame=ear.FRAME)
    first, second = world.leaned[:MOST_LOOKS], world.leaned[MOST_LOOKS:]
    assert first == [False] * (MOST_LOOKS - 1) + [True]  # at the cut-off
    assert len(second) >= 2 and second[-1] and not any(second[:-1])  # ...and when the utterance ended


def test_the_leaning_reading_hears_the_call_alone_that_the_plain_one_misheard(world):
    world.leaning = {"He joined this.": "He jarvis."}  # as the recogniser read "Hey Jarvis." said by itself
    world.play(quiet(1), words("He joined this."), quiet(1), words("what time is it?"), quiet(2))
    assert world.messages == ["What time is it?"]


def test_the_leaning_reading_restores_the_call_that_the_plain_one_ran_together(world):
    world.leaning = {"Jarvis opened the notes from yesterday.": "Jarvis, open the notes from yesterday."}
    world.play(quiet(1), words("Jarvis opened the notes from yesterday."), quiet(2))
    assert world.messages == ["Opened the notes from yesterday."]  # what the message's own recogniser heard


def test_the_leaning_reading_is_not_believed_where_it_puts_the_name_in_place_of_another_word(world):
    world.leaning = {
        "Chavez, the build is green.": "Jarvis, the build is green.",  # another name at the start
        "Java is installed on the server.": "Jarvis, is installed on the server.",  # no more than a guess
        "I think we can ship it tomorrow.": "Jarvis.",  # nothing like the name
        "John?": "Jarvis?",  # someone else called by his name alone
        "Hey John, the taxi is here.": "Hey Jarvis, the taxi is here.",  # ...or with more said
    }
    world.play(quiet(1), words("Chavez, the build is green."), quiet(2), words("I think we can ship it tomorrow."),
               quiet(2), words("John?"), quiet(2))
    world.play(quiet(1), words("Java is installed on the server."), quiet(2), words("Hey John, the taxi is here."),
               quiet(2))
    assert world.messages == [] and LISTENING not in world.told()


def test_a_short_utterance_is_decided_when_it_ends(world):
    world.play(quiet(1), words("good morning"), quiet(3), frame=ear.FRAME)
    assert len(world.decoded) <= 3 and world.held == 0
    assert world.decoded[-1][0] <= 1 + 2 * WORD + ear.PAUSE_SECONDS + 0.12


# --- the end of a message -----------------------------------------------------


def test_the_message_ends_about_a_second_after_the_user_stops(world):
    world.play(quiet(1), call(), quiet(0.5), words("what time is it?"), quiet(3), frame=ear.FRAME)
    stopped = 1 + 0.5 + 0.5 + 1.0
    assert ear.END_SECONDS <= world.when("voice", "heard")[0] - stopped <= ear.END_SECONDS + 0.2


def test_a_long_pause_after_the_name_is_not_the_end(world):
    world.play(quiet(1), call(), quiet(3.0), words("what time is it?"), quiet(2))
    assert world.messages == ["What time is it?"]


def test_a_short_pause_inside_the_message_is_not_the_end(world):
    world.play(quiet(1), call(), words("remind me to"), quiet(0.6), words("call the bank"), quiet(2))
    assert world.messages == ["Remind me to call the bank"]


def test_a_sound_that_is_no_voice_does_not_keep_the_message_open(world):
    world.play(quiet(1), words("Jarvis, what time is it?"), noise(5), frame=ear.FRAME)
    stopped = 1 + 5 * WORD
    assert world.messages == ["What time is it?"]
    assert world.when("voice", "heard")[0] - stopped <= ear.END_SECONDS + 0.2


def test_nobody_speaks_after_the_name_and_the_ear_gives_up(world):
    world.play(quiet(1), name(), quiet(7), frame=ear.FRAME)
    assert world.told() == [ASLEEP, AWAKE, LISTENING, CANCELLED, ASLEEP, OFF]
    assert world.messages == []
    waited = world.when("voice", "cancelled")[0] - (1 + WORD)  # counted from the end of the name
    assert ear.GIVE_UP_SECONDS <= waited <= ear.GIVE_UP_SECONDS + 0.2


def test_only_a_noise_after_the_name_and_the_ear_gives_up(world):
    world.play(quiet(1), call(), quiet(0.5), noise(1), quiet(6))
    assert world.told() == [ASLEEP, AWAKE, LISTENING, CANCELLED, ASLEEP, OFF]


def test_no_message_is_longer_than_the_limit(world):
    world.play(quiet(1), call(), words(" ".join(["again"] * 160)), quiet(2), frame=ear.FRAME)  # forty seconds
    took = world.when("voice", "heard")[0] - world.when("voice", "listening")[0]
    assert ear.MAX_SECONDS <= took <= ear.MAX_SECONDS + 0.2
    assert len(world.messages) == 1 and 110 <= len(world.messages[0].split()) <= 121


def test_after_a_message_is_given_up_the_name_wakes_the_ear_again(world):
    world.play(quiet(1), call(), quiet(6), call(), words("are you there?"), quiet(2))
    assert without_partials(world.told()) == [
        ASLEEP, AWAKE, LISTENING, CANCELLED, ASLEEP, AWAKE, LISTENING, *heard("Are you there?"), OFF,
    ]


# --- what the recogniser of the message is given --------------------------------


def test_the_words_appear_while_the_user_is_still_talking(world):
    world.play(quiet(1), call(), quiet(0.5), words("remind me to call the bank at five"), quiet(2), frame=ear.FRAME)
    partials = world.partials()
    assert partials[-1] == "Remind me to call the bank at five"
    assert len(partials) >= 6 and all(later.startswith(earlier) for earlier, later in zip(partials, partials[1:]))
    spoken_from, spoken_until = 1 + 0.5 + 0.5, 1 + 0.5 + 0.5 + 2.0
    assert sum(spoken_from < at < spoken_until for at in world.when("voice", "partial")) >= 5


def test_the_loudness_is_reported_while_a_message_is_taken_and_only_then(world):
    world.play(quiet(1), words("nobody is listening to this"), quiet(1), call(), words("how loud am I?"), quiet(2))
    levels = [(at, event["level"]) for at, event in world.events if event["type"] == "level"]
    woke, done = world.when("voice", "listening")[0], world.when("voice", "heard")[0]
    assert levels and all(woke <= at <= done for at, _ in levels)
    assert all(event["source"] == "voice" for _, event in world.events if event["type"] == "level")
    assert max(level for _, level in levels) == 1.0 and min(level for _, level in levels) == 0.0
    assert len(levels) >= 10 * (done - woke)  # about twelve a second


def test_words_that_run_on_from_the_name_reach_the_recogniser_whole_and_without_the_name(world):
    world.play(words("this was said to somebody else"), quiet(3), call(), words("are you there?"), quiet(2))
    assert world.messages == ["Are you there?"]
    given = spoken_values(world.utterances[0].sound)
    assert given[-3:] == ["are", "you", "there?"] and "else" not in given and "Hey" not in given
    assert transcript(world.utterances[0].sound) == "are you there?"


def test_after_a_pause_the_recogniser_never_hears_the_name(world):
    world.play(quiet(1), call(), quiet(0.7), words("are you there?"), quiet(2))
    assert world.messages == ["Are you there?"]
    given = world.utterances[0].sound
    assert spoken_values(given) == ["are", "you", "there?"]
    first_word = np.flatnonzero(given > 0.4)[0] / RATE
    assert 0.2 <= first_word <= ear.ONSET_PREROLL_SECONDS  # a little quiet, then the first word from its start
    assert all(partial in "Are you there?" for partial in world.partials())


def test_the_recogniser_of_the_message_is_given_nothing_until_the_user_speaks(world):
    given = []
    world.play(quiet(1), call(), quiet(3), lambda: given.append(len(world.utterances[0].sound)), words("now"),
               quiet(2))
    assert given == [0] and world.messages == ["Now"]


def test_talk_just_before_the_name_is_left_out_of_the_message(world):
    world.play(quiet(1), words("so I told them we would ship on Friday"), quiet(0.5), call(), words("what is next?"),
               quiet(2))
    assert world.messages == ["What is next?"]


def test_the_name_said_twice_is_one_message_without_either(world):
    world.play(quiet(1), call(), quiet(1.0), call(), quiet(0.6), words("are you there?"), quiet(2))
    assert without_partials(world.told()) == [ASLEEP, AWAKE, LISTENING, *heard("Are you there?"), OFF]


def test_the_name_alone_is_never_a_message_whatever_the_recogniser_would_make_of_it(world, monkeypatch):
    class Mishearing(FakeUtterance):
        def step(self) -> str:
            return "He joined this."  # what the real one wrote for the name by itself, for some voices

        finish = step

    monkeypatch.setattr(ear, "_utterance", Mishearing)
    world.play(quiet(1), call(), quiet(6))
    assert world.told() == [ASLEEP, AWAKE, LISTENING, CANCELLED, ASLEEP, OFF]
    assert world.messages == [] and len(world.utterances[0].sound) == 0  # it was never asked


# --- the recogniser -----------------------------------------------------------


def test_the_recogniser_is_held_only_while_an_utterance_is_looked_at_or_a_message_taken(world):
    held = []
    world.play(
        quiet(1), lambda: held.append(world.held),
        call(), words("what time"), lambda: held.append(world.held), words("is it?"), quiet(2),
        lambda: held.append(world.held),
        words("good news on this"), lambda: held.append(world.held), quiet(1), lambda: held.append(world.held),
        call(), quiet(6), lambda: held.append(world.held),
    )
    assert held == [0, 1, 0, 1, 0, 0]
    assert world.acquired == world.released == 3  # one each: the recogniser that heard the name takes the message


def test_words_said_while_the_recogniser_is_still_loading_are_not_lost(world):
    async def scene():
        world.loading = asyncio.Event()
        socket = Socket(world, [quiet(1), words("Jarvis, what time is it?"), quiet(3), world.loading.set, quiet(2)])
        await world.ear.serve(socket)
        await world.settle()

    asyncio.run(scene())
    assert world.messages == ["What time is it?"]
    assert world.when("voice", "listening")[0] >= 1 + 5 * WORD + 3.0  # only once the recogniser had come
    assert world.acquired == world.released == 1


def test_a_recogniser_that_cannot_be_had_is_not_asked_for_again_for_a_while_and_nothing_is_shown(world, monkeypatch,
                                                                                                    caplog):
    monkeypatch.setattr(ear, "RETRY_SECONDS", 4.0)
    world.broken = RuntimeError("the model is not on this Mac")
    with caplog.at_level(logging.WARNING, logger="jarvis"):
        world.play(quiet(1), call(), words("what time is it?"), quiet(1), call(), quiet(1),
                   lambda: setattr(world, "broken", None), quiet(2), call(), words("and now?"), quiet(2))
    assert without_partials(world.told()) == [ASLEEP, AWAKE, LISTENING, *heard("And now?"), OFF]
    assert "the model is not on this Mac" in caplog.text  # in the log only: asleep, nobody asked Jarvis anything
    assert world.acquired == world.released == 1


def test_a_recogniser_that_fails_on_the_sound_is_given_back_and_the_ear_sleeps_on(world):
    world.failing = RuntimeError("out of memory")
    world.play(quiet(1), call(), words("what time is it?"), quiet(2))
    assert world.told() == [ASLEEP, OFF]
    assert world.acquired == world.released == 1


def test_a_recogniser_that_fails_while_taking_the_message_is_said_and_given_back(world, monkeypatch):
    class Failing(FakeUtterance):
        def step(self) -> str:
            raise RuntimeError("out of memory")

    monkeypatch.setattr(ear, "_utterance", Failing)
    world.play(quiet(1), call(), words("what time is it?"), quiet(2))
    assert world.told() == [
        ASLEEP, AWAKE, LISTENING, ("info", "Jarvis could not hear that: out of memory"), CANCELLED, ASLEEP, OFF,
    ]
    assert world.acquired == world.released == 1


class Weights:
    """A recogniser whose weights another thread loaded: they cannot be made real here until it is loaded again."""

    def __init__(self, lazy: bool) -> None:
        self.lazy = lazy

    def parameters(self):
        if self.lazy:
            raise RuntimeError("There is no Stream(cpu, 1) in current thread.")
        return {}


def test_a_recogniser_another_thread_loaded_is_loaded_again_by_the_ears_thread(monkeypatch):
    import mlx.core as mx

    given = [Weights(lazy=True), Weights(lazy=False)]
    calls = []
    monkeypatch.setattr(mx, "eval", lambda tree: calls.append("eval"))
    monkeypatch.setattr(speech, "acquire", lambda: calls.append("acquire") or given.pop(0))
    monkeypatch.setattr(speech, "release", lambda: calls.append("release"))
    monkeypatch.setattr(speech, "unload_if_idle", lambda seconds: calls.append(f"unload {seconds}") or True)
    model = ear._recogniser()
    assert isinstance(model, Weights) and not model.lazy
    assert calls == ["acquire", "release", "unload 0", "acquire", "eval"]


def test_a_recogniser_in_use_elsewhere_is_left_alone_and_the_ear_tries_again_later(monkeypatch):
    calls = []
    monkeypatch.setattr(speech, "acquire", lambda: calls.append("acquire") or Weights(lazy=True))
    monkeypatch.setattr(speech, "release", lambda: calls.append("release"))
    monkeypatch.setattr(speech, "unload_if_idle", lambda seconds: calls.append("unload") or False)
    with pytest.raises(RuntimeError, match="Stream"):
        ear._recogniser()
    assert calls == ["acquire", "release", "unload"]


# --- the answer, and the follow-up ----------------------------------------------


def answered(world: World, seconds: float = 2.0) -> list:
    """Jarvis thinks for a second and then says his answer aloud."""
    return [quiet(1), world.ear.speaking_started, jarvis(seconds), world.ear.speaking_ended]


def test_after_the_answer_a_follow_up_needs_no_name(world):
    seen = []
    world.play(
        quiet(1), call(), words("what time is it?"), quiet(1.5), *answered(world),
        lambda: seen.append(world.ear.state), quiet(2), words("and what is next today?"), quiet(1.5), *answered(world),
        quiet(1), words("thank you"), quiet(2),
    )
    assert world.messages == ["What time is it?", "And what is next today?", "Thank you"]
    assert seen == ["awake"]
    assert without_partials(world.told()) == [
        ASLEEP, AWAKE, LISTENING, *heard("What time is it?"),
        LISTENING, *heard("And what is next today?"), LISTENING, *heard("Thank you"), OFF,
    ]
    assert world.listening == 3


def test_a_follow_up_is_taken_from_its_first_word(world):
    world.play(quiet(1), call(), words("what time is it?"), quiet(1.5), *answered(world), quiet(3),
               words("one two three four five six"), quiet(2), frame=ear.FRAME)
    assert world.messages[-1] == "One two three four five six"
    assert world.when("voice", "listening")[1] - (1 + 0.5 + 1 + 1.5 + 1 + 2 + 3) <= 0.25  # it shows at once


def test_the_follow_up_time_runs_out_and_then_only_the_name_counts(world):
    world.play(quiet(1), call(), words("what time is it?"), quiet(1.5), *answered(world),
               quiet(9), words("is anyone there?"), quiet(2), frame=ear.FRAME)
    assert without_partials(world.told()) == [ASLEEP, AWAKE, LISTENING, *heard("What time is it?"), ASLEEP, OFF]
    ended = 1 + 0.5 + 1 + 1.5 + 1 + 2
    waited = world.when("ear", "asleep")[1] - ended
    assert ear.FOLLOW_UP_SECONDS <= waited <= ear.FOLLOW_UP_SECONDS + ear.ECHO_SECONDS + 0.1


def test_a_noise_during_the_follow_up_time_is_not_a_message(world):
    world.play(quiet(1), call(), words("what time is it?"), quiet(1.5), *answered(world),
               quiet(1), noise(2), quiet(1), words("and tomorrow?"), quiet(2))
    assert world.messages == ["What time is it?", "And tomorrow?"]
    assert world.told().count(LISTENING) == 2 and CANCELLED not in world.told()


def test_a_cough_in_the_follow_up_time_is_let_go_and_the_user_can_still_answer(world):
    cough = np.full(int(0.3 * RATE), COUGH, dtype=np.float32)  # a voice to the voice model, no words to the recogniser
    world.play(quiet(1), call(), words("what time is it?"), quiet(1.5), *answered(world),
               quiet(1), cough, quiet(2), words("and tomorrow?"), quiet(2))
    assert world.messages == ["What time is it?", "And tomorrow?"]
    assert without_partials(world.told()) == [
        ASLEEP, AWAKE, LISTENING, *heard("What time is it?"),
        LISTENING, CANCELLED, LISTENING, *heard("And tomorrow?"), OFF,
    ]


def test_jarvis_speaking_up_by_himself_can_be_answered_without_the_name(world):
    world.play(quiet(1), world.ear.speaking_started, jarvis(2), world.ear.speaking_ended, quiet(1),
               words("remind me again in an hour"), quiet(2))
    assert without_partials(world.told()) == [ASLEEP, AWAKE, LISTENING, *heard("Remind me again in an hour"), OFF]


def test_with_no_spoken_answer_the_ear_goes_back_to_sleep_after_a_while(world):
    world.play(quiet(1), call(), words("what time is it?"), quiet(30), words("hello?"), quiet(2), frame=ear.FRAME)
    assert without_partials(world.told()) == [ASLEEP, AWAKE, LISTENING, *heard("What time is it?"), ASLEEP, OFF]
    waited = world.when("ear", "asleep")[1] - world.when("voice", "heard")[0]
    assert ear.REPLY_SECONDS <= waited <= ear.REPLY_SECONDS + 0.2


def test_a_long_spoken_answer_keeps_the_ear_awake_until_it_is_over(world):
    world.play(quiet(1), call(), words("read me the brief"), quiet(1.5), *answered(world, seconds=40),
               quiet(1), words("thank you"), quiet(2))
    assert world.messages == ["Read me the brief", "Thank you"]
    assert ASLEEP not in world.told()[1:]


# --- Jarvis's own voice ---------------------------------------------------------


def test_jarvis_own_voice_is_never_taken_for_the_user(world):
    world.play(quiet(1), call(), words("what time is it?"), quiet(1.5), *answered(world, seconds=5), quiet(10))
    assert world.messages == ["What time is it?"]
    assert world.told().count(LISTENING) == 1
    assert all("ten past nine" not in partial for partial in world.partials())


def test_the_end_of_jarvis_last_word_still_in_the_room_is_not_a_follow_up(world):
    world.play(quiet(1), call(), words("what time is it?"), quiet(1.5), quiet(1), world.ear.speaking_started,
               jarvis(2), world.ear.speaking_ended, jarvis(0.6), quiet(9))  # a slow page, a live room
    assert world.told().count(LISTENING) == 1 and world.messages == ["What time is it?"]


def test_a_quick_answer_as_soon_as_jarvis_has_stopped_is_heard(world):
    world.play(quiet(1), call(), words("shall I send it?"), quiet(1.5), quiet(1), world.ear.speaking_started,
               jarvis(2), world.ear.speaking_ended, quiet(0.4), words("yes"), quiet(2))
    assert world.messages[-1] == "Yes"


def test_an_answer_right_after_the_last_of_jarvis_voice_is_heard_without_it(world):
    world.play(quiet(1), world.ear.speaking_started, jarvis(2), world.ear.speaking_ended, jarvis(0.5), quiet(0.2),
               words("yes go ahead"), quiet(2))
    assert world.messages == ["Yes go ahead"]
    assert not (world.utterances[0].sound == np.float32(JARVIS)).any()


def test_the_name_right_after_jarvis_has_stopped_brings_none_of_his_voice_along(world):
    world.play(quiet(1), world.ear.speaking_started, jarvis(2), world.ear.speaking_ended, jarvis(0.2), quiet(0.2),
               call(), words("what now?"), quiet(2))
    assert world.messages == ["What now?"]
    assert not (world.utterances[0].sound == np.float32(JARVIS)).any()


def test_the_name_interrupts_jarvis_and_the_words_after_it_are_a_new_message(world):
    seen = []
    world.play(
        quiet(1), call(), words("read me the brief"), quiet(1.5), quiet(1), world.ear.speaking_started, jarvis(2),
        quiet(0.4), call(), jarvis(0.5), lambda: seen.append(world.told()[-1]), world.ear.speaking_ended,
        quiet(0.6), words("what time is it?"), quiet(2),  # the page stopped him
    )
    assert seen == [LISTENING]  # published again: that is what stops the playback
    assert world.messages == ["Read me the brief", "What time is it?"]
    assert without_partials(world.told()) == [
        ASLEEP, AWAKE, LISTENING, *heard("Read me the brief"), LISTENING, *heard("What time is it?"), OFF,
    ]


def test_what_jarvis_was_saying_when_he_was_interrupted_does_not_reach_the_recogniser(world):
    world.play(quiet(1), world.ear.speaking_started, jarvis(3), quiet(0.4), call(), jarvis(0.5), quiet(0.5),
               words("not now"), quiet(2))
    assert world.messages == ["Not now"]
    assert not (world.utterances[0].sound == np.float32(JARVIS)).any()
    assert spoken_values(world.utterances[0].sound) == ["not", "now"]  # the name said over his voice: not for it
    assert all(partial in "Not now" for partial in world.partials())


def test_his_own_voice_is_looked_at_only_for_the_name_and_only_for_two_seconds_a_sentence(world):
    world.play(quiet(1), world.ear.speaking_started, jarvis(6), quiet(0.5), jarvis(6), world.ear.speaking_ended,
               quiet(1))
    assert world.messages == [] and LISTENING not in world.told()
    assert 2 <= len(world.decoded) <= 2 * MOST_LOOKS and world.held == 0


def test_the_name_said_as_his_sentence_ends_still_wakes_the_ear(world):
    world.play(quiet(1), world.ear.speaking_started, jarvis(2), quiet(0.4), words("Hey"), world.ear.speaking_ended,
               words("Jarvis,"), quiet(1), words("what now?"), quiet(2))
    assert world.messages == ["What now?"]
    assert world.told().count(LISTENING) == 1


def test_his_last_words_still_waiting_to_be_looked_at_give_way_to_a_quick_answer(world):
    async def scene():
        world.loading = asyncio.Event()  # the recogniser is still loading while he speaks
        socket = Socket(world, [quiet(1), world.ear.speaking_started, jarvis(1.0), world.ear.speaking_ended,
                                quiet(0.6), words("yes please"), quiet(0.5), world.loading.set, quiet(2)])
        await world.ear.serve(socket)
        await world.settle()

    asyncio.run(scene())
    assert world.messages == ["Yes please"]
    assert world.acquired == world.released == 2  # the look at his words, given back unused, and the answer


def test_with_headphones_the_words_right_after_the_interrupting_name_are_heard(world):
    world.ear.headphones = True  # his voice never reaches the microphone
    world.play(quiet(1), world.ear.speaking_started, quiet(2), call(), words("stop reading"), quiet(2), quiet(1),
               world.ear.speaking_started, quiet(2), name(), quiet(0.3), words("next one"), quiet(2))
    assert world.messages == ["Stop reading", "Next one"]


def test_without_headphones_the_words_said_over_his_voice_are_not_the_message(world):
    world.play(quiet(1), world.ear.speaking_started, quiet(2), call(), words("stop reading"), quiet(5))
    assert world.told() == [ASLEEP, AWAKE, LISTENING, CANCELLED, ASLEEP, OFF]  # stopped, and nothing taken


def test_the_name_alone_stops_jarvis_and_his_last_words_are_not_a_message(world):
    world.play(quiet(1), world.ear.speaking_started, jarvis(3), quiet(0.4), call(), jarvis(0.5),
               world.ear.speaking_ended, quiet(6))
    assert world.told() == [ASLEEP, AWAKE, LISTENING, CANCELLED, ASLEEP, OFF]


def test_the_name_interrupts_jarvis_even_when_the_ear_was_asleep(world):
    world.play(quiet(1), world.ear.speaking_started, jarvis(2), quiet(0.4), call(), quiet(0.8), words("not now"),
               quiet(2))
    assert without_partials(world.told()) == [ASLEEP, AWAKE, LISTENING, *heard("Not now"), OFF]


def test_jarvis_starting_to_speak_ends_a_message_that_is_being_taken(world):
    seen = []
    world.play(quiet(1), call(), words("what time"), world.ear.speaking_started,
               lambda: seen.append((world.ear.state, world.held)), jarvis(3), world.ear.speaking_ended, quiet(1),
               words("what time is it?"), quiet(2))
    assert seen == [("awake", 0)]
    assert without_partials(world.told()) == [
        ASLEEP, AWAKE, LISTENING, CANCELLED, LISTENING, *heard("What time is it?"), OFF,
    ]
    assert all("ten past nine" not in message for message in world.messages)


def test_jarvis_speaking_again_closes_the_follow_up_time_until_he_has_finished(world):
    world.play(quiet(1), call(), words("what time is it?"), quiet(1.5), *answered(world), quiet(1),
               world.ear.speaking_started, jarvis(3), world.ear.speaking_ended, quiet(7), words("thank you"), quiet(2))
    assert world.messages == ["What time is it?", "Thank you"]  # seven seconds after he last spoke, ten after the first


# --- the microphone is taken, the connection goes -------------------------------


def test_pause_stops_a_message_at_once_and_nothing_is_heard_until_resume(world):
    seen = []
    world.play(
        quiet(1), call(), words("what time"), world.ear.pause, lambda: seen.append((world.ear.state, world.held)),
        words("is it?"), quiet(1), call(), words("can you hear me?"), quiet(2),
        world.ear.resume, lambda: seen.append((world.ear.state, world.held)),
        quiet(1), call(), words("and now?"), quiet(2),
    )
    assert seen == [("off", 0), ("asleep", 0)]
    assert without_partials(world.told()) == [
        ASLEEP, AWAKE, LISTENING, CANCELLED, OFF, ASLEEP, AWAKE, LISTENING, *heard("And now?"), OFF,
    ]


def test_pause_lets_go_of_an_utterance_being_looked_at(world):
    seen = []
    world.play(quiet(1), words("Jarvis"), world.ear.pause, lambda: seen.append(world.held), quiet(1),
               world.ear.resume, words("what time is it?"), quiet(2))
    assert seen == [0] and world.told() == [ASLEEP, OFF, ASLEEP, OFF]  # the halves are not joined


def test_the_ear_stays_closed_until_every_reason_is_taken_back(world):
    seen = []
    world.play(
        quiet(1), lambda: world.ear.pause("call"), lambda: world.ear.pause("message"),
        lambda: world.ear.resume("message"), lambda: seen.append(world.ear.state),
        call(), words("are you there?"), quiet(2),
        lambda: world.ear.resume("call"), lambda: seen.append(world.ear.state),
        call(), words("and now?"), quiet(2),
    )
    assert seen == ["off", "asleep"] and world.messages == ["And now?"]


def test_sound_from_before_a_pause_is_not_joined_to_sound_after_it(world):
    world.play(quiet(1), words("Hey"), world.ear.pause, quiet(1), world.ear.resume, words("Jarvis, are you there?"),
               quiet(2))
    assert world.messages == ["Are you there?"]  # the second half, which begins with the name, is a call by itself
    world.events.clear()
    world.play(quiet(1), words("so the"), world.ear.pause, quiet(1), world.ear.resume, words("Jarvis is great"),
               quiet(2))
    assert world.told() == [ASLEEP, OFF, ASLEEP, OFF]
    assert world.voices[-1].resets == 2  # when the connection began, and when listening began again


def test_pause_and_resume_do_nothing_twice_and_nothing_without_a_connection(world):
    world.ear.resume()
    world.ear.pause()
    world.ear.pause()
    assert world.ear.state == "off" and world.events == []
    world.play(quiet(1), call(), quiet(1))  # connected while paused: nothing is listened to
    assert world.told() == [] and world.voices[0].windows == 0
    world.ear.resume()
    world.ear.resume()
    assert world.ear.state == "off"
    world.play(quiet(1), call(), words("hello again"), quiet(2))
    assert world.messages == ["Hello again"]


def test_the_connection_closing_ends_a_message_that_is_being_taken(world):
    world.play(quiet(1), call(), words("what time"))
    assert world.told()[-2:] == [CANCELLED, OFF] and world.messages == []
    assert world.ear.state == "off" and world.acquired == world.released == 1


def test_the_connection_closing_lets_go_of_an_utterance_being_looked_at(world):
    world.play(quiet(1), words("the numbers"))
    assert world.told() == [ASLEEP, OFF] and world.acquired == world.released == 1


def test_each_connection_starts_asleep_whatever_the_last_one_was_doing(world):
    world.play(quiet(1), call(), words("what time is it?"), quiet(1.5), *answered(world))  # closed awake
    world.events.clear()
    world.play(quiet(1), words("this is not for Jarvis"), quiet(2), call(), words("but this is"), quiet(2))
    assert without_partials(world.told()) == [ASLEEP, AWAKE, LISTENING, *heard("But this is"), OFF]


def test_a_newer_connection_takes_over_from_one_that_has_not_closed(world):
    async def scene():
        stuck = asyncio.Event()

        class Hanging(Socket):
            async def receive(self) -> dict:
                if not self.parts and not self.pending:
                    await stuck.wait()  # the app never closed it
                return await super().receive()

        first = asyncio.create_task(world.ear.serve(Hanging(world, [quiet(1), call(), words("what time")])))
        while world.ear.state != "awake" and not first.done():
            await asyncio.sleep(0)
        second = Socket(world, [quiet(1), call(), words("is it working?"), quiet(2), stuck.set, quiet(1)])
        await world.ear.serve(second)
        await first
        await world.settle()

    asyncio.run(scene())
    assert without_partials(world.told()) == [
        ASLEEP, AWAKE, LISTENING, CANCELLED, ASLEEP, AWAKE, LISTENING, *heard("Is it working?"), OFF,
    ]
    assert world.held == 0 and world.ear.state == "off"


def test_what_the_service_says_between_connections_is_remembered(world):
    world.ear.speaking_started()
    world.ear.speaking_ended()
    world.ear.pause()
    assert world.ear.state == "off" and world.events == []
    world.play(quiet(1), world.ear.resume, quiet(1), call(), words("hello again"), quiet(2))
    assert without_partials(world.told()) == [ASLEEP, AWAKE, LISTENING, *heard("Hello again"), OFF]


# --- frames -------------------------------------------------------------------


def scene_events(world: World, frame: int) -> list[tuple]:
    world.play(quiet(0.5), call(), words("what time is it?"), quiet(1.5), quiet(1), world.ear.speaking_started,
               jarvis(2.0), world.ear.speaking_ended, quiet(1.0), words("and what is next?"), quiet(2.0), frame=frame)
    return without_partials(world.told())


@pytest.mark.parametrize("frame", [160, 1000, 1280, 1600, 1601, 4096, 48_000])
def test_frames_of_any_size_give_the_same_messages(world, frame):
    assert scene_events(world, frame) == [
        ASLEEP, AWAKE, LISTENING, *heard("What time is it?"), LISTENING, *heard("And what is next?"), OFF,
    ]


def test_seconds_of_sound_in_one_frame_are_heard_like_any_other(world):
    sound = np.concatenate([quiet(1), call(), words("what time is it?"), quiet(2)])
    world.play(sound.astype("<f4").tobytes(), quiet(0.5))  # one frame of nearly five seconds, and the stream goes on
    assert world.messages == ["What time is it?"]


def test_frames_that_are_empty_broken_or_text_are_passed_over(world):
    sound = np.concatenate([quiet(1), call(), words("what time is it?"), quiet(2)]).astype("<f4").tobytes()
    half = len(sound) // 2 - len(sound) // 2 % 4
    world.play(b"", '{"cancel": true}', b"\x01", sound[:half] + b"\x00\x00\x80", "hello", b"\x00\x00", sound[half:])
    assert world.messages == ["What time is it?"]  # a frame's odd last bytes are dropped; the samples stay in step


def test_sound_that_is_not_a_number_does_not_reach_the_listeners(world):
    bad = np.array([np.nan, np.inf, -np.inf] * 1600, dtype=np.float32)
    world.play(quiet(1), bad, call(), words("still there?"), quiet(2))
    assert world.messages == ["Still there?"]


def test_when_the_ear_cannot_keep_up_the_oldest_sound_is_dropped(world, monkeypatch):
    monkeypatch.setattr(ear, "PENDING_FRAMES", 5)

    async def scene():
        frames: asyncio.Queue = asyncio.Queue()
        sent = [np.full(1600, value, dtype="<f4").tobytes() for value in range(20)]

        class Burst:
            async def receive(self):
                return {"type": "websocket.receive", "bytes": sent.pop(0)} if sent else {"type": "websocket.disconnect"}

        world.ear._connection = 7
        await world.ear._receive(Burst(), frames, 7)  # nobody takes anything out meanwhile
        kept = [frames.get_nowait() for _ in range(frames.qsize())]
        return kept, world.ear._received, world.ear._heard

    kept, received, heard_ = asyncio.run(scene())
    assert [np.frombuffer(data, dtype="<f4")[0] for data in kept[:-1]] == [15, 16, 17, 18, 19] and kept[-1] is None
    assert received == 20 * 1600 and heard_ == 15 * 1600  # what was dropped counts as heard: the clock stays right


# --- off the event loop -------------------------------------------------------


def test_listening_and_recognising_happen_off_the_event_loop(world):
    loop_thread = []
    world.play(lambda: loop_thread.append(threading.get_ident()), quiet(1), call(), words("what time is it?"),
               quiet(2), threads=True)
    assert world.messages == ["What time is it?"]
    assert world.voices[0].threads and loop_thread[0] not in world.voices[0].threads
    assert world.decoding and loop_thread[0] not in world.decoding
    assert world.utterances[0].threads and loop_thread[0] not in world.utterances[0].threads
    assert world.acquired == world.released == 1
    # one thread for all of it: the recogniser has to be used first by the thread that loaded it
    assert world.voices[0].threads == world.decoding == world.utterances[0].threads == world.acquiring
    assert len(world.acquiring) == 1


def test_a_whole_conversation_with_real_threads(world):
    world.play(
        quiet(1), call(), words("what time is it?"), quiet(1.5), quiet(1), world.ear.speaking_started, jarvis(2),
        world.ear.speaking_ended, quiet(1), words("and tomorrow?"), quiet(1.5), quiet(1), world.ear.speaking_started,
        jarvis(1), quiet(0.4), call(), quiet(0.7), words("never mind"), quiet(2), threads=True,
    )
    assert world.messages == ["What time is it?", "And tomorrow?", "Never mind"]


# --- the words ------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, said",
    [
        ("what time is it?", "What time is it?"),
        ("Hey Jarvis, what time is it?", "What time is it?"),  # said again, or in a follow-up out of habit
        ("Hey, Jarvis. What time is it?", "What time is it?"),
        ("hey jarvis what time is it", "What time is it"),
        ("Okay Jarvis, stop.", "Stop."),
        ("Jarvis, what time is it?", "What time is it?"),
        ("Hey Jarvis. Hey Jarvis, are you there?", "Are you there?"),
        ("Hey Jarvis, Jarvis, are you there?", "Are you there?"),
        ("A. Javis. What time is it?", "What time is it?"),  # as the recogniser writes the name when it is unsure
        ("Hey Jervis, what time is it?", "What time is it?"),
        ("Hey Jarvis.", ""),
        ("Javis?", ""),
        ("", ""),
        ("Jarvis is a good name.", "Jarvis is a good name."),  # the name is only taken off where it is a call
        ("Hey Jarvis, ask Jarvis to wait.", "Ask Jarvis to wait."),
        ("Tell Jarvis I said thanks.", "Tell Jarvis I said thanks."),
        ("Hey Travis, what time is it?", "Hey Travis, what time is it?"),  # someone else was called: Jarvis should see
        ("Hey, what time is it?", "Hey, what time is it?"),
    ],
)
def test_the_name_is_taken_off_the_start_of_the_words_and_nothing_else_is(text, said):
    assert ear._said(text) == said


CALLS = [  # the start of an utterance, whether the speaker has stopped, and whether it calls Jarvis
    ("Jarvis, what time is it?", False, True),
    ("Jarvis. What time is it?", False, True),
    ("Jarvis? Are you there?", False, True),
    ("Hey Jarvis", False, True),
    ("Hey Jarvis, what", False, True),
    ("hey jarvis what time is it", False, True),
    ("Hey, Jarvis.", False, True),
    ("OK Jarvis, stop.", False, True),
    ("Okay Jarvis", False, True),
    ("Hi Jarvis.", False, True),
    ("Hello Jarvis, what is next?", False, True),
    ("Jarvis", True, True),  # the name by itself, and then a pause
    ("Jarvis.", True, True),
    ("Jarvis?", True, True),
    ("Jarvis", False, False),  # ...but while the speaker goes on it may be "Jarvis is ...": wait
    ("Jarvis.", False, False),
    ("Jarvis is still syncing the calendar.", True, False),
    ("Jarvis needs a restart after the update", False, False),
    ("Jarvis's memory is full.", True, False),
    ("I am building Jarvis for myself.", True, False),
    ("The new version of Jarvis starts much faster.", True, False),
    ("Hey Travis, are you coming to lunch?", False, False),
    ("Davis, the report is ready.", False, False),
    ("Hey Jasper, the car is here.", False, False),
    ("Hey Java developers, listen up.", False, False),
    ("Hey, service is slow today.", False, False),
    ("Hey", True, False),
    ("", True, False),
    ("Javis, what is next?", False, True),  # the name as the recogniser writes it when it is unsure
    ("Hey Jervis", False, True),
    ("Hey Javis.", False, True),
    ("A. Jarvis.", False, True),
    ("Okay Jalvis.", False, True),
    ("Jaravis, draft a reply to the last mail.", False, True),
    ("K. Javis.", False, True),
    ("Hejaruvis.", True, True),
    ("Okay Jaribus.", False, True),  # in a live room
    ("Jarambis, draft a reply.", False, True),  # with background noise
    ("Jeeves, bring the car around.", False, False),  # other words that begin the same way
    ("Jeevis, bring the car around.", False, False),  # ...as the recogniser wrote it in a live room
    ("Java is installed on the server.", True, False),
    ("Javier, can you hear me?", False, False),
    ("Jasper, the car is here.", False, False),
    ("Chavez, the build is green.", False, False),
]


@pytest.mark.parametrize("text, ended, calls", CALLS)
def test_an_utterance_calls_jarvis_only_when_it_begins_with_his_name(text, ended, calls):
    found = ear._called(text, ended)
    assert (found is not None) == calls
    if calls:
        assert text[:found].lower().endswith("s") and text[found:found + 1] in ("", ",", ".", "?", " ")


def test_the_end_of_the_name_is_found_among_the_pieces_the_recogniser_wrote():
    tokens = [(" Hey", 0.1, 0.3), (" Jar", 0.35, 0.5), ("vis", 0.5, 0.7), (",", 0.7, 0.7), (" what", 0.8, 1.0)]
    text = "".join(piece for piece, _, _ in tokens)
    assert ear._ends_at(tokens, ear._called(text, False)) == 0.7
    assert ear._ends_at([], 3) == 0.0


def test_quiet_speech_is_made_louder_for_the_recogniser_and_loud_speech_is_left_alone():
    loud, soft = np.full(RATE, 0.3, dtype=np.float32), np.full(RATE, 0.01, dtype=np.float32)
    assert ear._gain(loud) == 1.0
    assert ear._gain(soft) == pytest.approx(ear.SPEECH_LEVEL / 0.01)
    assert ear._gain(np.concatenate([quiet(2), soft])) == pytest.approx(ear.SPEECH_LEVEL / 0.01)  # pauses don't count
    assert ear._gain(np.full(RATE, 0.0001, dtype=np.float32)) == ear.MOST_GAIN  # a murmur is not made a shout
    assert ear._gain(np.zeros(100, dtype=np.float32)) == 1.0


def test_the_looks_and_the_message_both_get_the_quiet_speech_made_louder(monkeypatch):
    async def scene():
        given = []
        monkeypatch.setattr(ear, "_recognise", lambda model, samples, lean=False: given.append(samples.max()) or (
            [(" Jarvis,", 0.1, 0.4), (" hello", 0.5, 0.7)], None))
        loading = asyncio.get_running_loop().create_future()
        loading.set_result(MODEL)
        spotter = ear._Spotter(np.full(800, 0.01, dtype=np.float32), loading, his=False)
        spotter.ready()
        while spotter.found is None:
            spotter.hear(np.full(ear.FRAME, 0.01, dtype=np.float32), [0.9, 0.9])
        assert given and given[-1] == pytest.approx(0.01 * ear.SPEECH_LEVEL / 0.01)

        class Taking:
            def __init__(self) -> None:
                self.added: list[float] = []

            def add(self, samples):
                if len(samples):
                    self.added.append(float(samples.max()))

            def step(self):
                return "hello"

        message = ear._Message(spotter.message(), loading, called=True, spoke=True, gain=spotter.gain)
        message.utterance = Taking()
        message.hear(np.full(ear.FRAME, 0.01, dtype=np.float32), [0.9, 0.9])
        added = message.utterance.added
        assert added and all(level == pytest.approx(ear.SPEECH_LEVEL) for level in added)

        follow_up = ear._Message(np.full(800, 0.02, dtype=np.float32), loading, called=False)
        follow_up.utterance = Taking()
        follow_up.hear(np.full(ear.FRAME, 0.02, dtype=np.float32), [0.9, 0.9])
        assert follow_up.gain == pytest.approx(ear.SPEECH_LEVEL / 0.02)  # found from its own first words

    asyncio.run(scene())


def test_a_background_of_voices_is_told_from_the_voice_nearest_the_microphone():
    heard = ear._Background()
    windows = round(ear.BACKGROUND_SECONDS * RATE / ear.WINDOW)

    def feed(value: float, count: int, chance: float = 0.9) -> list[float]:
        sound = np.full(count * ear.WINDOW, value, dtype=np.float32)
        return heard.heard(sound, [chance] * count)

    assert feed(0.0, 30, chance=0.05) == [0.05] * 30
    assert feed(0.05, windows - 1) == [0.9] * (windows - 1)  # a voice: until it has gone on for a while, it is one
    assert feed(0.05, 200)[-1] == 0.0  # by now the level of the last few seconds: a background
    assert feed(0.4, 5) == [0.9] * 5  # 18 dB above it: the user
    assert feed(0.05, 3) == [0.0] * 3
    assert feed(0.0, 1, chance=0.05) == [0.05]  # a real pause: the voice model is believed again
    assert feed(0.05, 10) == [0.9] * 10


def test_an_utterance_begins_with_a_voice_after_a_pause():
    windows = round(ear.PAUSE_SECONDS * RATE / ear.WINDOW)

    def starts(chances: list[float]) -> list:
        heard = ear._Starts()
        found = []
        for start in range(0, len(chances), 5):
            piece = chances[start:start + 5]
            began = heard.heard(piece, len(piece) * ear.WINDOW)
            if began is not None:
                found.append((start + len(piece), began))
        return found

    voice, none = [0.9], [0.05]
    assert starts(voice * 8) == [(5, (5 * ear.WINDOW, windows * ear.WINDOW))]  # the stream's start counts as a pause
    assert len(starts(voice * 10 + none * (windows - 1) + voice * 10)) == 1  # a short pause: the same utterance
    found = starts(voice * 10 + none * windows + voice * 10)
    assert len(found) == 2 and found[1][1][1] == windows * ear.WINDOW
    blip = starts(voice * 10 + none * windows + voice * 2 + none * 3 + voice * 5)  # a click is part of the quiet
    assert len(blip) == 2 and blip[1][1][1] == (windows + 2 + 3) * ear.WINDOW


# --- the voice model: on this Mac, or not -------------------------------------------


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DATA", str(tmp_path))
    sessions = ear._sessions
    sessions.cache_clear()
    yield tmp_path
    sessions.cache_clear()


def test_available_is_false_until_the_voice_model_is_there(data):
    assert ear.available() is False
    ear.folder().mkdir(parents=True)
    assert ear.available() is False
    (ear.folder() / ear.VOICE).write_bytes(b"model")
    assert ear.available() is True
    assert ear.folder() == data / "models" / "ear"


def test_available_is_false_without_onnxruntime_and_loads_nothing(data, monkeypatch):
    ear.folder().mkdir(parents=True)
    (ear.folder() / ear.VOICE).write_bytes(b"model")
    monkeypatch.setattr(ear, "_sessions", lambda: pytest.fail("available() must not load a model"))
    assert ear.available() is True
    real = importlib.util.find_spec
    hidden = lambda name, *rest: None if name == "onnxruntime" else real(name, *rest)  # noqa: E731
    monkeypatch.setattr(importlib.util, "find_spec", hidden)
    assert ear.available() is False


def test_without_the_voice_model_a_connection_is_refused_and_the_ear_stays_off(data):
    events: list[dict] = []
    listener = ear.Ear(events.append, lambda text: pytest.fail("no message"))

    class App:
        sent: list[dict] = []
        asked = 0

        async def receive(self):
            App.asked += 1
            return {"type": "websocket.disconnect"}

        async def send_json(self, message):
            App.sent.append(message)

    asyncio.run(listener.serve(App()))
    assert App.sent == [{"error": "unavailable"}] and App.asked == 0
    assert events == [] and listener.state == "off"


class Fetched:
    """What the network would have answered."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.asked: list[str] = []

    def open(self, url: str, timeout: float):
        self.asked.append(url)
        content = self.files[url.rsplit("/", 1)[1]]

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self) -> bytes:
                return content

        return Response()


def test_download_fetches_the_voice_model_once_and_only_keeps_what_it_expected(data, monkeypatch):
    files = {name: f"the model {name}".encode() for name in ear.FILES}
    expected = {name: (source, hashlib.sha256(files[name]).hexdigest()) for name, (source, _) in ear.FILES.items()}
    monkeypatch.setattr(ear, "FILES", expected)
    network = Fetched(files)
    monkeypatch.setattr(ear.urllib.request, "urlopen", network.open)

    assert ear.download() == data / "models" / "ear"
    assert {path.name: path.read_bytes() for path in ear.folder().iterdir()} == files
    assert network.asked == [source + name for name, (source, _) in expected.items()]
    assert ear.available() is True

    ear.download()
    assert len(network.asked) == 1  # there and as expected: nothing is fetched again

    (ear.folder() / ear.VOICE).write_bytes(b"damaged")
    ear.download()
    assert network.asked[1:] == [expected[ear.VOICE][0] + ear.VOICE]
    assert (ear.folder() / ear.VOICE).read_bytes() == files[ear.VOICE]

    (ear.folder() / ear.VOICE).unlink()
    network.files = {ear.VOICE: b"something else"}
    with pytest.raises(RuntimeError, match="not the file that was expected"):
        ear.download()
    assert not (ear.folder() / ear.VOICE).exists() and ear.available() is False


def test_the_only_file_fetched_is_silero_voice_model_over_https_and_checked():
    assert set(ear.FILES) == {ear.VOICE}
    for source, digest in ear.FILES.values():
        assert source.startswith("https://") and source.endswith("/") and len(digest) == 64
        assert "silero" in source and "openwakeword" not in source.lower()  # no wake-word model, whatever its terms


# --- the voice model, fed by a stand-in for its file ------------------------------


class Session:
    """A stand-in for the model file: remembers what it was given."""

    def __init__(self, answer) -> None:
        self.answer = answer
        self.given: list[dict] = []

    def run(self, outputs, inputs: dict):
        self.given.append(inputs)
        return self.answer(inputs)


def test_the_voice_model_gets_whole_windows_and_keeps_what_is_left_over(monkeypatch):
    session = Session(lambda inputs: [np.array([[0.75]], dtype=np.float32), inputs["state"] + 1])
    monkeypatch.setattr(ear, "_sessions", lambda: {ear.VOICE: session})
    voice = ear._Voice()
    assert voice.heard(np.zeros(ear.FRAME, dtype=np.float32)) == [0.75, 0.75]  # 1280 samples: two windows and a half
    assert voice.heard(np.zeros(ear.FRAME, dtype=np.float32)) == [0.75, 0.75, 0.75]
    assert voice.heard(np.zeros(100, dtype=np.float32)) == []
    given = session.given
    assert len(given) == 5 and all(inputs["input"].shape == (1, ear.VOICE_CONTEXT + ear.WINDOW) for inputs in given)
    assert int(given[0]["sr"]) == RATE and given[4]["state"][0, 0, 0] == 4  # the state goes from window to window
    voice.reset()
    assert voice.heard(np.zeros(ear.WINDOW, dtype=np.float32)) == [0.75] and given[5]["state"][0, 0, 0] == 0


# --- talking over Jarvis, his voice cancelled out of the microphone -------------------------------------------------

HUSH = {"type": "hush"}


def test_with_his_voice_cancelled_talk_over_him_stops_him_at_once_and_is_the_next_message(world):
    socket = world.play(quiet(1), world.ear.speaking_started, left_over(1.5), words("wait what about the budget"),
                        left_over(0.5), world.ear.speaking_ended, quiet(2), echo="cancelled", frame=800)
    assert socket.sent == [HUSH]
    assert world.messages == ["Wait what about the budget"]
    assert world.listening == 1  # the service is told too: it stops what he was saying, and gets ready
    began = 1 + 1.5
    assert 0 < world.when("voice", "listening")[0] - began <= ear.BARGE_SECONDS + 0.15  # within half a second
    assert without_partials(world.told()) == [ASLEEP, AWAKE, LISTENING, *heard("Wait what about the budget"), OFF]


def test_a_single_word_over_him_is_enough(world):
    socket = world.play(quiet(1), world.ear.speaking_started, quiet(1), words("stop"), quiet(2), echo="cancelled")
    assert socket.sent == [HUSH] and world.messages == ["Stop"]


def test_his_name_over_him_stops_him_too_and_is_left_out_of_the_message(world):
    world.play(quiet(1), world.ear.speaking_started, quiet(1), call(), words("read the next one"), quiet(2),
               echo="cancelled")
    assert world.messages == ["Read the next one"]


def test_what_is_left_of_his_voice_after_cancelling_never_stops_him(world):
    socket = world.play(quiet(1), world.ear.speaking_started, left_over(4), quiet(0.3), left_over(4),
                        world.ear.speaking_ended, quiet(1), echo="cancelled")
    assert socket.sent == [] and world.messages == [] and LISTENING not in world.told()


def test_a_message_over_him_that_is_only_his_own_words_coming_back_is_dropped(world):
    world.ear._said = lambda: " ".join(HIS)
    socket = world.play(quiet(1), world.ear.speaking_started, jarvis(3), world.ear.speaking_ended, quiet(2),
                        echo="cancelled")  # cancelling failed: his voice reached the microphone loud
    assert socket.sent == [HUSH]  # he was stopped, which cannot be undone; but he does not answer himself
    assert world.messages == [] and CANCELLED in world.told() and world.ear.stats["echoes"] == 1
    assert without_partials(world.told())[-3:] == [CANCELLED, ASLEEP, OFF]


def test_a_cough_over_him_is_too_short_to_stop_him(world):
    cough = np.full(int(0.15 * RATE), COUGH, dtype=np.float32)
    socket = world.play(quiet(1), world.ear.speaking_started, quiet(1), cough, quiet(1), echo="cancelled")
    assert socket.sent == [] and LISTENING not in world.told()


def test_without_cancelling_talk_over_him_does_not_stop_him(world):
    socket = world.play(quiet(1), world.ear.speaking_started, jarvis(1), words("wait what about the budget"),
                        jarvis(1), lambda: world.events.append((0, {"type": "ended"})), world.ear.speaking_ended,
                        quiet(1))
    assert socket.sent == []
    assert ("ended",) in world.told() and LISTENING not in world.told()[: world.told().index(("ended",))]


def test_when_he_is_not_speaking_talk_is_not_taken_without_the_name(world):
    socket = world.play(quiet(1), words("wait what about the budget"), quiet(2), echo="cancelled")
    assert socket.sent == [] and world.messages == []


def test_his_own_words_coming_back_are_told_from_the_users():
    said = "It is ten past nine and the next meeting is at eleven."
    assert ear._echoes("the next meeting is at eleven", said)
    assert ear._echoes("It is ten past nine and", said)
    assert not ear._echoes("stop", said)
    assert not ear._echoes("wait, what about the next meeting", said)  # quoting him is a question
    assert not ear._echoes("open the calendar for tomorrow instead", said)
