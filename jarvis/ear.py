"""Jarvis's ear: "Jarvis, ..." instead of a held key, and then a conversation.

While the app has no other use for the microphone it streams it here, in the same frames as a held-key message.
Asleep, the sound goes to a small model that tells a voice from other sounds, and is forgotten after two seconds.
When someone begins to speak after a pause, the start of what they say is recognised as it grows, by the
recogniser every other listener uses. If it begins with Jarvis's name ("Jarvis, ...", "Hey Jarvis", "OK Jarvis",
or the name as the recogniser writes it when it is unsure of it) the ear wakes, and the words after the name are
the start of the message. The name further into a sentence does not count ("I am building Jarvis for myself"),
nor the bare name run straight on into other words ("Jarvis is still syncing"). What has not begun with the name
within two seconds, or plainly begins with something else, is not recognised any further until the next pause, so
that a conversation nearby costs little.

Awake, the words are recognised as they arrive, and when the user stops they go on as a spoken message, exactly as
from a held key. After Jarvis's answer has been heard the ear stays awake a few seconds, so that a follow-up needs
no name. While Jarvis himself is speaking only the name counts, which is how he is interrupted; his own voice,
which the microphone hears as well, is never taken for a message. Through the speakers that means the name is heard
when it follows a pause in what the microphone hears, between his sentences say; through headphones, at any time.

Unless the app cancels his voice out of the microphone (it says so when it connects: ``echo=cancelled``). Then the
user can simply talk over him: a quarter of a second of confident voice while he speaks stops him at once (the app
is told ``{"type": "hush"}`` on the same connection, and the service is told through ``on_listening``) and is the
start of the user's next message, name or not. What is left of his voice after cancelling was measured about 100 dB
down, far below the level a voice must have to count; and a message that turns out to be mostly his own words, three
or more in a row, is dropped rather than answered.

Whether someone is speaking is decided by Silero's voice-activity model (MIT), because loudness alone takes a
keyboard or a door for a voice. It is fetched once by ``download`` into the data folder and loaded by path; nothing
is fetched at run time. The recogniser is taken when a voice begins and given back once the utterance has been
decided; the service frees it after a quiet period.
"""

from __future__ import annotations

import asyncio
import collections
import functools
import hashlib
import importlib.util
import logging
import re
import time
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from jarvis import paths, speech

log = logging.getLogger("jarvis")

SAMPLE_RATE = 16_000
FRAME = 1280  # 80 ms: the step of everything the ear decides
WINDOW = 512  # 32 ms: the step of the voice model

VOICE_THRESHOLD = 0.5  # someone is speaking in a window the voice model gives at least this
ONSET_WINDOWS = 3  # that many such windows in a row (a tenth of a second): a voice, not a click
# Where other people talk on without a break the voice model hears a voice all the time, and no utterance would ever
# begin or end. So once it has heard nothing but voice for a while, the voice nearest the microphone is told from the
# rest by loudness: what is not well above the steady level of the last few seconds counts as no voice. (Under
# babble 10 dB below the user, the user's words stood 10 to 18 dB above that level, the babble within 5 dB of it.)
BACKGROUND_SECONDS = 2.0  # a voice without a break for this long is taken for a background of voices
BACKGROUND_SPAN_SECONDS = 5.0  # the background's level: the quietest tenth of this long
ABOVE_BACKGROUND_DB = 8.0  # a voice at least this much louder than the background is the user's

# Hearing the name. Measured on the finished ear, end to end, with 642 calls made by the Mac's 24 English voices,
# the three Indian English ones at three speeds among them ("Jarvis, ...", "Jarvis" and a pause, "Hey/OK/Hi
# Jarvis", run on, or after other talk): 98% woke it in a quiet room (the Indian voices 107 of 108), a median 0.11 s
# after the name ended (0.6 s for nine in ten). Nothing else woke it in 2.8 hours of other talk: prose, "Jarvis"
# inside a sentence or opening one, other people called (short J-names such as Javed and Jayesh among them), real
# recordings of accented English, a call, babble. Harder sound costs calls (Indian voices): a fan 20 dB below the
# voice 84%, 10 dB 71%, a quiet microphone 83%, an echoing room 60%, mostly one voice whose name the recogniser cannot
# read there; under babble 10 dB below the user only 20 to 32%, the recogniser reading the other voices' words.
PAUSE_SECONDS = 0.24  # a voice after this long without one begins an utterance; this long without one ends it.
#   At 0.32 no call said 0.3 s after other talk was heard (0 of 14); at 0.24 all were, for 6% more utterances.
SPOT_PREROLL_SECONDS = 0.3  # sound from before the voice was noticed that goes with it, no more than the pause
SPOT_FIRST_SECONDS = 0.32  # the start of an utterance is first recognised this long after the voice was noticed
SPOT_EVERY_SECONDS = 0.32  # ...and again each time this much more of it has been heard. A look takes about 0.1 s
#   however long the sound; looking every 0.24 s cost a quarter more and caught fewer calls, early looks being
#   fragments.
SPOT_SECONDS = 2.0  # what has not begun with the name by then is not recognised further until the next pause
CUT_SECONDS = 0.1  # the message begins this far after where the recogniser puts the end of the name, which it puts
#   early. Of the messages that ran straight on from the name, from there 83% came out word for word and none began
#   with a scrap of the name; from 0.05 to 0.2 s much the same, from the end itself 81% (with scraps), from 0.3 s 71%.
RETRY_SECONDS = 60.0  # when the recogniser could not be had, the ear asks again only after this long

ONSET_PREROLL_SECONDS = 0.5  # sound from before a voice was noticed that the recogniser gets: its first word, whole
RING_SECONDS = 2.0  # all the sound that is kept while nothing is being taken
END_SECONDS = 0.9  # the user stopped: this long without a voice, once there are words
GIVE_UP_SECONDS = 4.0  # nobody spoke after the name
MAX_SECONDS = 30.0  # no message is longer than this
FOLLOW_UP_SECONDS = 8.0  # after Jarvis has answered, speech without the name is a message
ECHO_SECONDS = 0.3  # ...but not at once: his last word is still on its way here, and then in the room
REPLY_SECONDS = 20.0  # how long to stay awake for an answer that has not begun to be spoken
PENDING_FRAMES = 300  # sound waiting to be attended to beyond this (half a minute) is dropped

# Talking over Jarvis, when the app cancels his voice out of the microphone (see above).
CANCELLED = "cancelled"  # what the app says when it connects, if his voice is cancelled
BARGE_SECONDS = 0.22  # this much voice while he speaks stops him (seven windows: a short "stop" is enough)...
BARGE_THRESHOLD = 0.7  # ...in windows the voice model is at least this sure of
BARGE_DIPS = 1  # a window below that inside the voice is forgiven, a second one in a row starts the count again
BARGE_LEVEL_DB = -65.0  # ...and no quieter than this (dBFS): what is left of his voice was about -100 dBFS
BARGE_GUARD_SECONDS = 0.2  # not in the first moment after he begins: his first syllable and the word of it may differ
BARGE_PREROLL_SECONDS = 0.3  # sound from before the voice that goes with the message
ECHO_SHARE = 0.8  # a message this much of which is his own words, three or more in a row, is his voice coming back
ECHO_WORDS = 4  # ...if it is at least this long: a short "stop" or "wait" never is

VOICE_FILES = "https://raw.githubusercontent.com/snakers4/silero-vad/v6.2.3/src/silero_vad/data/"
VOICE = "silero_vad_16k_op15.onnx"
# Where each file comes from and its SHA-256: what is loaded is what was measured.
FILES = {VOICE: (VOICE_FILES, "7ed98ddbad84ccac4cd0aeb3099049280713df825c610a8ed34543318f1b2c49")}
PACKAGES = ("onnxruntime",)
DOWNLOAD_SECONDS = 120
VOICE_CONTEXT = 64

OFF, ASLEEP, LISTENING, REPLYING, FOLLOW_UP = "off", "asleep", "listening", "replying", "follow-up"


# --- the model file ---------------------------------------------------------------


def folder() -> Path:
    return paths.data_dir() / "models" / "ear"


def available() -> bool:
    """True when the ear can run: the voice model is on this Mac and onnxruntime is installed. Loads nothing."""
    try:
        return all(importlib.util.find_spec(name) for name in PACKAGES) and all(
            (folder() / name).is_file() for name in FILES
        )
    except Exception:
        return False


def download() -> Path:
    """Put the voice model in the data folder (2 MB). The one place here that uses the network."""
    target = paths.ensure_data_dir() / "models" / "ear"
    target.mkdir(parents=True, exist_ok=True)
    for name, (source, digest) in FILES.items():
        path = target / name
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest:
            continue
        with urllib.request.urlopen(source + name, timeout=DOWNLOAD_SECONDS) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != digest:
            raise RuntimeError(f"{source}{name} is not the file that was expected; it was not saved")
        partial = path.with_suffix(".part")
        partial.write_bytes(data)
        partial.replace(path)
    return target


@functools.cache
def _sessions() -> dict:
    """The voice model, read from its file. Kept: it takes a moment to load."""
    import onnxruntime

    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = 1  # the model is tiny: more threads would only keep cores awake
    options.inter_op_num_threads = 1
    missing = [name for name in FILES if not (folder() / name).is_file()]
    if missing:
        raise FileNotFoundError(f"{', '.join(missing)} not in {folder()}; run ear.download() once")
    return {
        name: onnxruntime.InferenceSession(str(folder() / name), options, providers=["CPUExecutionProvider"])
        for name in FILES
    }


def _load() -> "_Voice":
    """What one connection listens with."""
    return _Voice()


class _Voice:
    """Hears whether someone is speaking: a chance from 0 to 1 for every 32 ms of sound."""

    def __init__(self) -> None:
        self._model = _sessions()[VOICE]
        self._rate = np.array(SAMPLE_RATE, dtype=np.int64)
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(VOICE_CONTEXT, dtype=np.float32)
        self._left = np.zeros(0, dtype=np.float32)

    def heard(self, samples: np.ndarray) -> list[float]:
        """The chance of speech in each window that ``samples`` complete; what is left over waits."""
        sound = np.concatenate([self._left, samples])
        chances = []
        for start in range(0, len(sound) - WINDOW + 1, WINDOW):
            window = np.concatenate([self._context, sound[start:start + WINDOW]]).astype(np.float32)
            chance, self._state = self._model.run(
                None, {"input": window[None, :], "state": self._state, "sr": self._rate}
            )
            self._context = window[-VOICE_CONTEXT:]
            chances.append(float(chance[0, 0]))
        self._left = sound[len(chances) * WINDOW:]
        return chances


class _Background:
    """Tells the voice nearest the microphone from a background of other voices that never stops (see above).

    Takes the same sound as the voice model, in the same windows, and the chances it gave them; gives back the chances
    with the windows that are only the background set to none.
    """

    def __init__(self) -> None:
        span = round(BACKGROUND_SPAN_SECONDS * SAMPLE_RATE / WINDOW)
        self._levels: collections.deque = collections.deque(maxlen=span)
        self._needed = round(BACKGROUND_SECONDS * SAMPLE_RATE / WINDOW)
        self.reset()

    def reset(self) -> None:
        self._levels.clear()
        self._voiced = 0  # windows of voice without a break, as the voice model hears them
        self._left = np.zeros(0, dtype=np.float32)
        self.on = False  # the voices around are being told apart by loudness

    def heard(self, samples: np.ndarray, chances: list[float]) -> list[float]:
        sound = np.concatenate([self._left, samples])
        kept = []
        for index, chance in enumerate(chances):
            window = sound[index * WINDOW:(index + 1) * WINDOW]
            level = 10 * float(np.log10(np.mean(np.square(window, dtype=np.float64)) + 1e-12))
            self._levels.append(level)
            self._voiced = self._voiced + 1 if chance >= VOICE_THRESHOLD else 0
            self.on = self._voiced >= self._needed
            if self.on and level < np.percentile(self._levels, 10) + ABOVE_BACKGROUND_DB:
                chance = 0.0
            kept.append(chance)
        self._left = sound[len(chances) * WINDOW:]
        return kept


# --- the recogniser ---------------------------------------------------------------


def _recogniser():
    """The shared recogniser, ready for the ear's own thread. Blocking: run it there, and pair it with a release.

    A recogniser that another thread loaded may still have weights that only that thread can make real, and then
    it cannot be used here; if nobody else is using it, it is let go and loaded again, here.
    """
    model = speech.acquire()
    try:
        _made_real(model)
        return model
    except Exception:
        speech.release()
        if not speech.unload_if_idle(0):
            raise
    model = speech.acquire()
    try:
        _made_real(model)
    except Exception:
        speech.release()
        raise
    return model


def _made_real(model) -> None:
    parameters = getattr(model, "parameters", None)
    if callable(parameters):
        import mlx.core as mx

        mx.eval(parameters())


def _recognise(model, samples: np.ndarray, lean: bool = False) -> tuple[list, list | None]:
    """What the recogniser makes of ``samples``: each piece of a word, and where it starts and ends (s), read greedily;
    and with ``lean`` also read leaning towards Jarvis's name (None when that cannot be had). One pass of the
    encoder serves both readings: it is most of the cost."""
    import mlx.core as mx
    from parakeet_mlx.audio import get_logmel

    mel = get_logmel(mx.array(samples), model.preprocessor_config)
    features, lengths = model.encoder(mel if mel.ndim == 3 else mx.expand_dims(mel, 0))
    mx.eval(features, lengths)
    plain = model.decode(features, lengths)[0][0]
    leaning = None
    config = speech.boosted([NAME]) if lean else None
    if config is not None:
        leaning = model.decode(features, lengths, config=config)[0][0]
    pieces = lambda tokens: [(token.text, token.start, token.end) for token in tokens]  # noqa: E731
    return pieces(plain), None if leaning is None else pieces(leaning)


def _utterance(model, words: tuple[str, ...] = ()):
    """What takes a message's words, read leaning towards the user's names and terms as a held-key message is."""
    from jarvis.voice import Utterance

    return Utterance(model, speech.boosted(words))


# The recogniser reads quiet speech badly: its features floor the sound's power, and quiet speech sinks into the floor
# (a voice 18 dB down, as from a quiet microphone or across the room, was read right half as often). So quiet sound
# is made louder before it is read, to the level loud and clear speech has; louder sound is left as it is.
SPEECH_LEVEL = 0.063  # -24 dBFS while speaking
MOST_GAIN = 16.0  # 24 dB at most


def _gain(samples: np.ndarray) -> float:
    """How much louder ``samples`` should be for the recogniser: its loudest third, in 20 ms steps, to speech level."""
    steps = len(samples) // 320
    if steps == 0:
        return 1.0
    energy = np.mean(np.square(samples[: steps * 320].reshape(steps, 320), dtype=np.float64), axis=1)
    level = float(np.sqrt(np.sort(energy)[-max(1, steps // 3):].mean()))
    return float(np.clip(SPEECH_LEVEL / max(level, 1e-6), 1.0, MOST_GAIN))


# --- the name ---------------------------------------------------------------------

NAME = "Jarvis"
# How the recogniser writes "hey", and the name, when it is unsure of them; measured on the test audio. The name:
# Jarvis, Javis, Jervis, Jovis, Jivus, Jalvis, Jaravis, Jarivis, Jaruvis, Jaramvis, in a live room or with noise also
# Jaribus, Jarbis and Jarambis, and with "hey" run into it ("Hejaruvis"); but not Jeeves or "Jeevis", Java or
# Javier, which are other words.
_GREETING = r"(?:hey|hi|hay|he|a|eh|k|ok|okay|hello|pay|pey)"
_GLUED = r"(?:h?e|hi|pa|pe)?"
_NAME = rf"{_GLUED}j[aeiou](?:[rl][aeiou]?m?)?[vb][iyu]s"
# The name at the start of the words. The bare name counts only as a call ("Jarvis, ...").
_CALL = re.compile(rf"\W*(?:{_GREETING}\W+{_NAME}\b\W*|{_NAME}(?:\s*[^\w\s]+\s*|\s*$))", re.IGNORECASE)
_STARTS = re.compile(rf"\W*(?:(?P<greeting>{_GREETING})\W+)?(?P<name>{_NAME})\b(?P<rest>.*)", re.IGNORECASE | re.DOTALL)
_MARK_THEN_WORD = re.compile(r"\s*[,.!?;:]+\s*\w")
# A reading that leans towards the name is believed in two cases only. The plain reading heard the name and the
# leaning one agrees with it but for the words after it ("Jarvis opened the notes" for "Jarvis, open the notes").
# Or the utterance was short, has ended, and the plain reading heard a greeting and a word much like the name ("He
# joined this." for "Hey Jarvis."). Never where the name would take another word's place ("Chavez, the build is
# green", "Java is installed").
_LIKE_CALL = re.compile(rf"\W*(?:{_GREETING}\W+|(?:h?e|hi|pa|pe))j", re.IGNORECASE)
_WORDS = re.compile(r"[\w']+")
CALL_ALONE_WORDS = 3  # no longer than this, a call by itself
# Once the recogniser has heard this many words of an utterance and neither of the first two holds a letter that
# can spell the first sound of the name (j, g, ch, sh) or something of the rest of it ("Harvest" for "Hey Jarvis"),
# the utterance is let go before its two seconds are up: talk nearby then costs half the looks, for about one call
# in two hundred.
SURE_WORDS = 2
_MAY_CALL = re.compile(r"\W*(?:[\w']+\W+)?[\w']*(?:j|g|ch|sh|arv|vis)", re.IGNORECASE)


def _called(text: str, ended: bool) -> int | None:
    """Where the name ends in ``text``, the start of an utterance, if the utterance is a call to Jarvis; else None.

    With a greeting before it the name is a call at once. The bare name is a call when the recogniser set it off
    from what follows ("Jarvis, what time is it?"), or nothing follows and the speaker has stopped (``ended``).
    """
    found = _STARTS.match(text)
    if found is None:
        return None
    rest = found["rest"]
    if found["greeting"] or _MARK_THEN_WORD.match(rest) or (ended and not re.search(r"\w", rest)):
        return found.end("name")
    return None


def _leaning_call(plain: str, leaning: str | None, ended: bool) -> int | None:
    """Where the name ends in ``leaning``, the reading that leans towards the name, if it is a call that can be
    believed though the plain reading was not one (see above)."""
    if leaning is None:
        return None
    end = _called(leaning, ended)
    if end is None:
        return None
    from jarvis.vocabulary import agrees

    if _STARTS.match(plain) and agrees(plain, leaning):
        return end
    alone = ended and len(_WORDS.findall(plain)) <= CALL_ALONE_WORDS and _LIKE_CALL.match(plain)
    return end if alone and len(_WORDS.findall(leaning)) <= CALL_ALONE_WORDS else None


def _said(text: str) -> str:
    """What the user said to Jarvis: ``text`` without the name, should it be there, said again or still."""
    while found := _CALL.match(text):
        text = text[found.end():]
    text = text.strip()
    return text[:1].upper() + text[1:]


def _echoes(heard: str, said: str) -> bool:
    """Whether ``heard`` is mostly Jarvis's own words coming back: runs of three or more words of what he ``said``
    cover at least ``ECHO_SHARE`` of it. A short "stop" or "wait" never is, nor a question that quotes him."""
    words, his = _WORDS.findall(heard.lower()), _WORDS.findall(said.lower())
    if len(words) < ECHO_WORDS or len(his) < 3:
        return False
    runs = {tuple(his[index:index + 3]) for index in range(len(his) - 2)}
    covered: set[int] = set()
    for index in range(len(words) - 2):
        if tuple(words[index:index + 3]) in runs:
            covered.update((index, index + 1, index + 2))
    return len(covered) >= ECHO_SHARE * len(words)


# --- hearing an utterance begin -------------------------------------------------------


class _Onset:
    """Hears a voice begin: a few windows of it in a row.

    ``after_quiet``: only once there has been a moment without one, for when the sound so far may be
    Jarvis's own voice dying away.
    """

    def __init__(self, *, after_quiet: bool) -> None:
        self.armed = not after_quiet
        self._run = 0

    def began(self, chances: list[float]) -> bool:
        began = False
        for chance in chances:
            if chance < VOICE_THRESHOLD:
                self.armed, self._run = True, 0
            elif self.armed:
                self._run += 1
                began = began or self._run >= ONSET_WINDOWS
        return began


class _BargeIn:
    """Hears the user talk over Jarvis, when his voice is cancelled out of the microphone: a voice the voice model is
    sure of that lasts a quarter of a second, and is loud enough not to be what is left of his."""

    def __init__(self) -> None:
        self._needed = round(BARGE_SECONDS * SAMPLE_RATE / WINDOW)
        self.reset()

    def reset(self) -> None:
        self._run = 0  # sure windows in the current voice
        self._dips = 0  # unsure ones in a row since the last sure one

    def heard(self, chances: list[float], frame: np.ndarray) -> bool:
        """``chances``: the windows the latest ``frame`` completed. True when the user is talking over him."""
        level = 10 * float(np.log10(np.mean(np.square(frame, dtype=np.float64)) + 1e-12))
        loud = level >= BARGE_LEVEL_DB
        for chance in chances:
            if chance >= BARGE_THRESHOLD and loud:
                self._run, self._dips = self._run + 1, 0
            elif self._run and self._dips < BARGE_DIPS:
                self._dips += 1
            else:
                self._run, self._dips = 0, 0
            if self._run >= self._needed:
                return True
        return False


class _Starts:
    """Hears an utterance begin: a voice after a pause. The start of the stream counts as a pause.

    A voice is a few windows of it in a row; a shorter blip is part of the quiet around it.
    """

    def __init__(self) -> None:
        self._needed = round(PAUSE_SECONDS * SAMPLE_RATE / WINDOW)
        self._quiet = self._needed  # windows without a voice before the current run
        self._run = 0
        self._gap = 0  # the quiet before the current run, when it began
        self._began = 0  # the window it began in
        self._windows = 0
        self._samples = 0

    def heard(self, chances: list[float], samples: int) -> tuple[int, int] | None:
        """``chances``: the windows that the latest ``samples`` completed. If an utterance began in them: how far
        back from the end of those samples its voice began, and how long it had been quiet before, in samples."""
        found = None
        for chance in chances:
            if chance >= VOICE_THRESHOLD:
                if self._run == 0:
                    self._gap, self._began = self._quiet, self._windows
                self._run += 1
                if self._run == ONSET_WINDOWS:
                    if self._gap >= self._needed:
                        found = self._began
                    self._quiet = 0
            else:
                if self._run < ONSET_WINDOWS:
                    self._quiet += self._run
                self._run = 0
                self._quiet += 1
            self._windows += 1
        self._samples += samples
        if found is None:
            return None
        return self._samples - found * WINDOW, self._gap * WINDOW


class _Spotter:
    """The start of one utterance while it is looked at for the name.

    It is recognised from just before its first sound, again each time a little more of it has been heard. If it
    begins with the name, the ear wakes and the words after the name are the start of the message. If it has not by
    the time two seconds of it have been heard, or it ends first, or its first words are plainly something else, it
    is let go.
    """

    def __init__(self, before: np.ndarray, loading: asyncio.Future, *, his: bool) -> None:
        self.loading = loading  # the recogniser, when it has come
        self.loading.add_done_callback(_looked_at)
        self.his = his  # Jarvis was speaking while it was said: his voice may be in it
        self.babble = False  # ...or other people were, without a break: its pauses were told by loudness alone
        self.model = None
        self.failed: Exception | None = None
        self.sound = [before]
        self.heard = 0  # samples since the voice was noticed
        self.quiet = 0  # ...and since a voice was last heard
        self.next = int(SPOT_FIRST_SECONDS * SAMPLE_RATE)  # when to recognise it next
        self.found: int | None = None  # where in the sound the message begins, once the name has been found
        self.words = False  # words came after the name
        self.over = False  # it is not a call: let it go
        self.gain = 1.0  # how much louder it was made for the recogniser
        self.decodes = 0
        self.seconds = 0.0  # spent recognising
        self._handed = False

    def ready(self) -> None:
        """Take the recogniser once it is here. Call on the event loop."""
        if self.model is None and self.failed is None and self.loading.done():
            try:
                self.model = self.loading.result()
            except Exception as exc:
                self.failed = exc

    def hear(self, frame: np.ndarray, chances: list[float], babble: bool = False) -> None:
        """Take in the next 80 ms and what the voice model made of it (``babble``: with other voices told apart by
        loudness). Runs on the ear's thread."""
        if self.over or self.found is not None or self.failed is not None:
            return
        self.babble = self.babble or babble
        self.sound.append(frame)
        self.heard += len(frame)
        for chance in chances:
            self.quiet = self.quiet + WINDOW if chance < VOICE_THRESHOLD else 0
        if self.model is None:
            return  # the recogniser has not come yet: the sound waits for it
        ended = self.quiet >= PAUSE_SECONDS * SAMPLE_RATE
        if not ended and self.heard < self.next:
            return
        upcoming = self.next
        while upcoming <= self.heard:  # one look at all of it, should the recogniser have come late
            upcoming += int(SPOT_EVERY_SECONDS * SAMPLE_RATE)
        last = ended or upcoming > SPOT_SECONDS * SAMPLE_RATE
        try:
            started = time.perf_counter()
            sound = np.concatenate(self.sound)
            self.gain = _gain(sound)
            tokens, leaning = _recognise(self.model, sound * self.gain, lean=last)  # leaning: only the last look
            self.decodes += 1
            self.seconds += time.perf_counter() - started
        except Exception as exc:
            self.failed = exc
            return
        text = "".join(piece for piece, _, _ in tokens)
        alone = ended and not self.babble  # the bare name by itself, between pauses told by loudness, is not a call
        end = _called(text, alone)
        if end is None and leaning is not None:
            other = "".join(piece for piece, _, _ in leaning)
            end = _leaning_call(text, other, alone)
            if end is not None:
                tokens, text = leaning, other
        if end is not None:
            self.found = max(0, min(len(sound), int((_ends_at(tokens, end) + CUT_SECONDS) * SAMPLE_RATE)))
            self.words = bool(re.search(r"\w", text[end:]))
            return
        self.next = upcoming
        self.over = last or (len(_WORDS.findall(text)) >= SURE_WORDS and not _MAY_CALL.match(text))

    def message(self) -> np.ndarray:
        """The sound after the name. The recogniser goes with it to the message."""
        self._handed = True
        return np.concatenate(self.sound)[self.found:]

    def close(self) -> None:
        """Give the recogniser back, once it has been given, unless it went on to the message."""
        if not self._handed:
            self.loading.add_done_callback(_release)


def _ends_at(tokens: list[tuple[str, float, float]], index: int) -> float:
    """Where, in seconds, the piece of text that holds the character before ``index`` ends."""
    reached = 0
    for piece, _, end in tokens:
        reached += len(piece)
        if reached >= index:
            return end
    return tokens[-1][2] if tokens else 0.0


# --- one message ----------------------------------------------------------------


class _Message:
    """One spoken message while it is being taken: is the user speaking, and what are the words so far.

    The recogniser is given nothing until the user has begun to speak, and never the name: heard before the words
    it bends them ("Jarvis opened the notes" for "Jarvis, open the notes"). After a pause it gets the sound from
    just before the first word; words that ran straight on from the name it gets from where the name ended.
    """

    def __init__(
        self, before: np.ndarray, loading: asyncio.Future, *, called: bool, interrupting: bool = False,
        spoke: bool | None = None, quiet: int = 0, gain: float | None = None, words: tuple[str, ...] = (),
        over_him: bool = False,
    ) -> None:
        self.called = called  # by the name; otherwise it began with speech in a follow-up
        self.over_him = over_him  # it began while Jarvis was speaking: his words may come back in it
        self.vocabulary = words  # the user's names and terms
        self.loading = loading  # the recogniser, when it has come
        self.loading.add_done_callback(_looked_at)
        self.gain = gain  # how much louder the sound is made for the recogniser; found from its first words if None
        self.kept = [before]  # sound the recogniser has not been given yet
        self.utterance = None
        self.failed: Exception | None = None
        self.samples = 0  # heard since it began
        self.quiet = quiet  # ...and since a voice was last heard, which may be before it began
        self.spoke = (not called) if spoke is None else spoke  # the user has said something, beyond the name
        self.words = ""
        self.shown = ""
        self._theirs = interrupting  # the sound is still Jarvis's voice: neither the user's nor words
        self._gap = 0  # how long it had been quiet when the voice last began
        self._onset = _Onset(after_quiet=False)

    def ready(self) -> None:
        """Take the recogniser once it is here. Call on the event loop."""
        if self.utterance is None and self.failed is None and self.loading.done():
            try:
                self.utterance = _utterance(self.loading.result(), self.vocabulary)
            except Exception as exc:
                self.failed = exc

    def hear(self, frame: np.ndarray, chances: list[float]) -> None:
        """Take in the next 80 ms and what the voice model made of it. Runs on the ear's thread."""
        self.samples += len(frame)
        if self._theirs:  # until the playback has stopped and the room is quiet
            self.quiet += len(frame)
            self._theirs = self.samples <= ECHO_SECONDS * SAMPLE_RATE or min(chances, default=1.0) >= VOICE_THRESHOLD
            return
        for chance in chances:
            if chance < VOICE_THRESHOLD:
                self.quiet += WINDOW
            elif self.quiet:
                self._gap, self.quiet = self.quiet, 0
        self.kept.append(frame)
        if not self.spoke and not self._begins(chances):
            return
        if self.utterance is None:
            return
        try:
            if self.gain is None:
                self.gain = _gain(np.concatenate(self.kept))
            for sound in self.kept:
                self.utterance.add(sound * self.gain)
            self.kept = []
            self.words = _said(self.utterance.step())
        except Exception as exc:
            self.failed = exc

    def _begins(self, chances: list[float]) -> bool:
        """Has the user begun to speak?"""
        self.spoke = self._onset.began(chances)
        noticed = int(ONSET_PREROLL_SECONDS * SAMPLE_RATE)
        if self.spoke and self._gap >= noticed - ONSET_WINDOWS * WINDOW:  # after a pause: only what led up to the voice
            self.kept = [np.concatenate(self.kept)[-noticed:]]
        return self.spoke

    def finish(self) -> str:
        return _said(self.utterance.finish())

    def close(self) -> None:
        """Give the recogniser back, once it has been given."""
        self.loading.add_done_callback(_release)


def _release(loading: asyncio.Future) -> None:
    if not loading.cancelled() and loading.exception() is None:
        speech.release()


def _looked_at(loading: asyncio.Future) -> None:
    """A recogniser that failed to load is reported with the message; asyncio need not warn about it as well."""
    loading.cancelled() or loading.exception()


# --- the ear --------------------------------------------------------------------


class Ear:
    """Listens to the app's microphone stream for the name, and takes the messages that follow.

    ``publish`` gets the events for the interface and ``on_message`` the words of each finished message;
    ``on_listening``, if given, is told when a message begins to be taken, and ``vocabulary``, if given, gives the
    user's names and terms that messages are read leaning towards. All are called on the event loop, and so must the
    methods be.
    """

    def __init__(
        self, publish: Callable[[dict], None], on_message: Callable[[str], None],
        on_listening: Callable[[], None] | None = None, vocabulary: Callable[[], list[str]] | None = None,
        said: Callable[[], str] | None = None,
    ) -> None:
        self._publish = publish
        self._on_message = on_message
        self._on_listening = on_listening
        self._vocabulary = vocabulary
        self._said = said  # what Jarvis has said lately, to know his words when they come back
        self._phase = OFF
        self._connection = 0  # counts connections: only the newest is listened to
        self._serving = False
        self._paused: set[str] = set()  # why the ear is closed: a held-key message, a call, the setting
        self._speaking = False
        self._told = 0  # counts what the service told the ear: sound heard before is not acted on after
        self._message: _Message | None = None
        self._spotter: _Spotter | None = None
        self._ring = np.zeros(0, dtype=np.float32)
        self._received = 0  # samples that have arrived on this connection
        self._heard = 0  # ...and those attended to; the times below are counted the same way
        self._deaf_until = 0
        self._until = 0
        self._spot_from = 0  # no utterance is looked at before this, after the recogniser could not be had
        self._restarted = True
        self._onset = _Onset(after_quiet=True)
        self._starts = _Starts()
        self._barge = _BargeIn()
        self._barge_from = 0  # no talking over him is heard before this, after he began
        self._echo: str | None = None  # what the app said of his voice in the microphone: "cancelled", ...
        self._socket = None  # the app's connection, to tell it to stop his voice
        self._thread: ThreadPoolExecutor | None = None
        self.headphones = False  # Jarvis's voice goes to headphones: the microphone never hears it
        self.stats = {"utterances": 0, "decodes": 0, "seconds": 0.0, "wakes": 0, "barge_ins": 0, "echoes": 0}

    @property
    def state(self) -> str:
        """ "off", "asleep" (only the name counts) or "awake" (from the name to the end of the follow-up time)."""
        return self._phase if self._phase in (OFF, ASLEEP) else "awake"

    # --- what the service tells it ------------------------------------------------

    @property
    def cancelled(self) -> bool:
        """The app cancels Jarvis's voice out of the microphone: the user can talk over him."""
        return self._echo == CANCELLED

    def speaking_started(self) -> None:
        """Jarvis begins to speak. Until he has finished only the name counts: his voice is not the user's. Unless his
        voice is cancelled out of the microphone: then any voice that lasts stops him."""
        if not self._speaking:
            self._barge.reset()
            self._barge_from = self._received + int(BARGE_GUARD_SECONDS * SAMPLE_RATE)
        self._speaking = True
        if self._spotter is not None:
            self._spotter.his = True
        if self._phase in (LISTENING, FOLLOW_UP):
            self._told += 1
            self._drop_message()
            self._enter(REPLYING)

    def speaking_ended(self) -> None:
        """Jarvis has finished speaking. For a few seconds the user can answer without the name.

        An utterance that began while he spoke is still looked at for the name, which may be what stopped him.
        """
        self._speaking = False
        if self._phase in (ASLEEP, REPLYING, FOLLOW_UP):
            self._told += 1
            self._deaf_until = self._received + int(ECHO_SECONDS * SAMPLE_RATE)
            self._until = self._deaf_until + int(FOLLOW_UP_SECONDS * SAMPLE_RATE)
            self._onset = _Onset(after_quiet=True)
            self._enter(FOLLOW_UP)

    def pause(self, reason: str = "message") -> None:
        """Something else takes the microphone, a held-key message or a call, or the user turned the ear off: stop
        listening at once, until every reason given has been taken back with :meth:`resume`."""
        if not self._paused:
            self._told += 1
            self._drop_message()
            self._drop_spotter()
            self._enter(OFF)
        self._paused.add(reason)

    def resume(self, reason: str = "message") -> None:
        if reason in self._paused:
            self._paused.discard(reason)
            if not self._paused:
                self._told += 1
                if self._serving:
                    self._begin()

    # --- the connection -------------------------------------------------------------

    async def serve(self, websocket, echo: str | None = None) -> None:
        """One connection from the app: frames in until it closes. ``echo``: what the app says of Jarvis's voice in the
        sound it sends, "cancelled" when it is cancelled out of it."""
        try:
            voice = await self._work(_load)
            background = _Background()
        except Exception as exc:
            log.warning("the ear cannot listen: %s", exc)
            await _refuse(websocket)
            return
        self._connection += 1
        mine = self._connection
        self._drop_message()  # of a connection this one takes over from
        self._drop_spotter()
        self._serving = True
        self._echo, self._socket = echo, websocket
        log.info("ear: connected; Jarvis's voice in the microphone: %s", echo or "not said")
        self._received = self._heard = self._deaf_until = self._until = self._spot_from = 0
        if not self._paused:
            self._begin()
        frames: asyncio.Queue = asyncio.Queue()
        receiving = asyncio.create_task(self._receive(websocket, frames, mine))
        try:
            left = np.zeros(0, dtype=np.float32)
            while mine == self._connection and (sound := await _next(frames)) is not None:
                sound = np.concatenate([left, sound])
                whole = len(sound) - len(sound) % FRAME
                for start in range(0, whole, FRAME):
                    await self._attend(sound[start:start + FRAME], voice, background, mine)
                left = sound[whole:]
        finally:
            receiving.cancel()
            if mine == self._connection:
                self._serving = False
                self._socket = None
                self._drop_message()
                self._drop_spotter()
                self._enter(OFF)

    async def _receive(self, websocket, frames: asyncio.Queue, mine: int) -> None:
        """Put the sound that arrives in ``frames``, and None when the connection is over."""
        try:
            while (message := await websocket.receive())["type"] != "websocket.disconnect":
                data = message.get("bytes")
                if not data or self._paused or mine != self._connection:
                    continue
                if frames.qsize() >= PENDING_FRAMES:  # the ear is not keeping up: the newest sound matters most
                    self._heard += len(frames.get_nowait()) // 4
                frames.put_nowait(data)
                self._received += len(data) // 4
        except Exception as exc:
            log.warning("the ear's connection failed: %s", exc)
        finally:
            frames.put_nowait(None)

    def _begin(self) -> None:
        """Listening starts, or starts again after a break: nothing heard before counts."""
        self._ring = self._ring[:0]
        self._restarted = True
        self._enter(ASLEEP)

    # --- one step of sound ------------------------------------------------------------

    async def _attend(self, frame: np.ndarray, voice: _Voice, background: _Background, mine: int) -> None:
        """Take in the next 80 ms of sound."""
        self._heard += FRAME
        if self._paused or mine != self._connection:
            return
        told, phase, message, spotter = self._told, self._phase, self._message, self._spotter
        if self._restarted:  # the voice model is only ever used one step at a time, from here
            self._restarted = False
            voice.reset()
            background.reset()
            self._starts = _Starts()
        self._ring = np.concatenate([self._ring, frame])[-int(RING_SECONDS * SAMPLE_RATE):]
        if message is not None:
            message.ready()
        if spotter is not None:
            spotter.ready()
        chances = await self._work(_listen, frame, voice, background, message, spotter)
        if mine != self._connection:
            return
        began = self._starts.heard(chances, len(frame))
        if told != self._told:
            return  # the service said something meanwhile: this sound belongs to the time before
        if phase == LISTENING:
            await self._follow(message, frame)
            return
        if spotter is not None and spotter is self._spotter and self._spotted(spotter):
            return
        if self._speaking and self.cancelled and phase in (ASLEEP, REPLYING) and self._heard > self._barge_from:
            if self._barge.heard(chances, frame):
                self._barge_in()
                return
        if phase == FOLLOW_UP:
            if self._heard <= self._deaf_until:
                return
            if not self._onset.armed:
                self._deaf_until = self._heard  # his voice has not died away yet: none of this is the user's
            if self._onset.began(chances):
                self._drop_spotter()  # an utterance begun over his last words gives way to the answer
                self._take(called=False)
            elif self._heard >= self._until and self._spotter is None:
                self._enter(ASLEEP)
            return
        # asleep, or waiting for Jarvis's answer and hearing it: only the name counts
        if began is not None and self._spotter is None and self._heard >= self._spot_from:
            self._look_at(*began)
        if self._phase == REPLYING and not self._speaking and self._heard >= self._until:
            self._enter(ASLEEP)

    def _barge_in(self) -> None:
        """The user is talking over Jarvis: he stops at once, and what the user says is the next message."""
        self.stats["barge_ins"] += 1
        log.info("ear: the user talked over Jarvis")
        self._drop_spotter()
        self._tell_app({"type": "hush"})
        back = int((BARGE_SECONDS * 2 + BARGE_PREROLL_SECONDS) * SAMPLE_RATE)  # the voice from its start, and before
        self._take(called=False, before=self._ring[-back:], over_him=True)

    def _tell_app(self, message: dict) -> None:
        """Send ``message`` to the app on its connection, without waiting."""
        socket = self._socket
        if socket is None:
            return

        async def send() -> None:
            try:
                await socket.send_json(message)
            except Exception:  # the connection is going; the page stops the voice as well
                pass

        asyncio.ensure_future(send())

    def _look_at(self, back: int, gap: int) -> None:
        """An utterance began ``back`` samples ago, after ``gap`` samples of quiet: look at its start for the name."""
        kept = min(len(self._ring), back + min(gap, int(SPOT_PREROLL_SECONDS * SAMPLE_RATE)))
        self._spotter = _Spotter(self._ring[len(self._ring) - kept:], self._work(_recogniser), his=self._speaking)
        self.stats["utterances"] += 1

    def _spotted(self, spotter: _Spotter) -> bool:
        """What came of the utterance being looked at, so far. True when the ear woke."""
        if spotter.failed is not None:
            log.warning("the ear could not recognise what was said: %s", spotter.failed)
            self._spot_from = self._heard + int(RETRY_SECONDS * SAMPLE_RATE)
            self._drop_spotter()
        elif spotter.found is not None:
            self._spotter = None
            self._count(spotter)
            self.stats["wakes"] += 1
            self._take(called=True, before=spotter.message(), loading=spotter.loading, spoke=spotter.words,
                       quiet=spotter.quiet, his=spotter.his, gain=spotter.gain)
            return True
        elif spotter.over:
            self._drop_spotter()
        return False

    def _take(self, *, called: bool, before: np.ndarray | None = None, loading: asyncio.Future | None = None,
              spoke: bool = False, quiet: int = 0, his: bool = False, gain: float | None = None,
              over_him: bool = False) -> None:
        """Start taking a message.

        Called by the name, ``before`` is the sound after the name, ``loading`` the recogniser that heard it,
        ``quiet`` how long it has been quiet since, and ``his`` whether Jarvis spoke while it was said. Otherwise
        the message is a follow-up that began with a voice just now, and gets the sound from just before it. But
        nothing from while Jarvis was speaking out loud: told "listening" the page stops the playback; what he was
        saying until then, and the words said over it, are not the user's message. (With headphones, or his voice
        cancelled out of the microphone, they are: there is nothing of his in the sound.)
        """
        over_him = over_him or self._speaking or his
        interrupting, self._speaking = over_him and not (self.headphones or self.cancelled), False
        if interrupting:
            before, spoke, quiet, gain = self._ring[:0], False, 0, None  # the level was his as much as the user's
        elif before is None:
            clean = max(0, self._heard - self._deaf_until)
            kept = min(int(ONSET_PREROLL_SECONDS * SAMPLE_RATE), clean, len(self._ring))
            before = self._ring[len(self._ring) - kept:]
        self._enter(LISTENING)
        self._publish({"type": "voice", "phase": "listening"})
        if self._on_listening is not None:
            self._on_listening()
        self._message = _Message(
            before, loading if loading is not None else self._work(_recogniser), called=called,
            interrupting=interrupting, spoke=spoke if called else None, quiet=quiet, gain=gain, words=self._words(),
            over_him=over_him and not interrupting,
        )

    def _his_own(self, text: str) -> bool:
        """Whether ``text`` is mostly what Jarvis said lately."""
        try:
            return self._said is not None and _echoes(text, self._said())
        except Exception:
            return False

    def _words(self) -> tuple[str, ...]:
        try:
            return tuple(self._vocabulary()) if self._vocabulary is not None else ()
        except Exception:  # the plain recogniser is enough to hear a message
            return ()

    async def _follow(self, message: _Message, frame: np.ndarray) -> None:
        """Show how the message is coming along, and end it when the user has stopped."""
        loudness = float(np.sqrt(np.mean(np.square(frame, dtype=np.float64))))
        self._publish({"type": "level", "source": "voice", "level": round(min(1.0, loudness * 12), 3)})
        if message.words != message.shown:
            message.shown = message.words
            self._publish({"type": "voice", "phase": "partial", "text": message.words})
        final = ""
        if message.failed is None and not self._over(message):
            return
        if message.failed is None and message.spoke and message.utterance is not None:
            told = self._told
            try:
                final = await self._work(message.finish)
            except Exception as exc:
                message.failed = exc
            if told != self._told or message is not self._message:
                return
        self._message = None
        message.close()
        if final and message.over_him and self._his_own(final):
            log.info("ear: a message taken over Jarvis's voice was his own words; dropped")
            self.stats["echoes"] += 1
            final = ""
        if message.failed is not None:  # say why nothing came of it instead of leaving the orb listening
            self._publish({"type": "info", "text": f"Jarvis could not hear that: {message.failed}"})
        if final:
            self._publish({"type": "voice", "phase": "heard", "text": final})
            self._until = self._heard + int(REPLY_SECONDS * SAMPLE_RATE)
            self._enter(REPLYING)
            self._on_message(final)
        else:
            self._publish({"type": "voice", "phase": "cancelled"})
            again = not message.called and message.failed is None and self._heard < self._until
            self._onset = _Onset(after_quiet=True)
            self._enter(FOLLOW_UP if again else ASLEEP)  # a cough in the follow-up time does not end it

    def _over(self, message: _Message) -> bool:
        """Has the user finished, or never begun?"""
        quiet = message.quiet / SAMPLE_RATE
        if message.samples >= MAX_SECONDS * SAMPLE_RATE:
            return True
        if not message.spoke:
            return quiet >= GIVE_UP_SECONDS
        if message.utterance is None:  # someone spoke: wait for the recogniser, however long it takes to load
            return False
        return quiet >= (END_SECONDS if message.words or not message.called else GIVE_UP_SECONDS)

    # --- small things -----------------------------------------------------------------

    def _work(self, function, *args) -> asyncio.Future:
        """Start ``function`` on the ear's own thread, where everything heavy runs, one thing after another.

        Not just any thread: the recogniser can only be used for the first time by the thread that loaded it.
        """
        if self._thread is None:
            self._thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis-ear")
        return asyncio.get_running_loop().run_in_executor(self._thread, function, *args)

    def _enter(self, phase: str) -> None:
        before = self.state
        self._phase = phase
        if self.state != before:
            self._publish({"type": "ear", "phase": self.state})

    def _drop_message(self) -> None:
        """Abandon the message being taken, if there is one."""
        if self._message is not None:
            self._message.close()
            self._message = None
            self._publish({"type": "voice", "phase": "cancelled"})

    def _drop_spotter(self) -> None:
        """Stop looking at the utterance being looked at, if there is one."""
        if self._spotter is not None:
            self._count(self._spotter)
            self._spotter.close()
            self._spotter = None

    def _count(self, spotter: _Spotter) -> None:
        self.stats["decodes"] += spotter.decodes
        self.stats["seconds"] += spotter.seconds


def _listen(frame: np.ndarray, voice: _Voice, background: _Background, message: _Message | None,
            spotter: _Spotter | None) -> list[float]:
    """Everything heavy about one step: the chance of a voice, and the words. Runs on the ear's thread."""
    chances = background.heard(frame, voice.heard(frame))
    if message is not None:
        message.hear(frame, chances)
    if spotter is not None:
        spotter.hear(frame, chances, background.on)
    return chances


async def _next(frames: asyncio.Queue) -> np.ndarray | None:
    """All the sound waiting in ``frames``, as samples; None once the connection is over."""
    data = await frames.get()
    pieces = []
    while data is not None:
        pieces.append(np.frombuffer(data[: len(data) - len(data) % 4], dtype="<f4"))
        if frames.empty():
            return np.nan_to_num(np.concatenate(pieces), nan=0.0, posinf=0.0, neginf=0.0)
        data = frames.get_nowait()
    return None


async def _refuse(websocket) -> None:
    """Tell the app this will not work for now, so that it waits before it tries again."""
    try:
        await websocket.send_json({"error": "unavailable"})
    except Exception:
        pass
