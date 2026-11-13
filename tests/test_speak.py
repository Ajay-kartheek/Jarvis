"""The speaking voice: what is said (clean-up and sentences) and the sound it makes.

No test loads Kokoro and none makes a sound: the macOS voice is forced for
every test, and ``say`` is only ever run with ``-o``, which writes a file.
"""

from __future__ import annotations

import io
import os
import random
import threading
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from jarvis import speak

SAY_IS_HERE = os.access(speak.SAY, os.X_OK)
needs_say = pytest.mark.skipif(not SAY_IS_HERE, reason="/usr/bin/say is not here")


@pytest.fixture(autouse=True)
def system_voice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(speak.ENGINE_ENV, "system")


def wav_format(data: bytes) -> tuple[int, int, int, float]:
    """Channels, bits, rate and seconds of a WAV file's bytes."""
    with wave.open(io.BytesIO(data)) as wav:
        return wav.getnchannels(), wav.getsampwidth() * 8, wav.getframerate(), wav.getnframes() / wav.getframerate()


# --- speakable --------------------------------------------------------------


@pytest.mark.parametrize(
    "text, spoken",
    [
        ("## Plan for Thursday", "Plan for Thursday"),
        ("### 2. Open questions", "Open questions"),
        ("This is **bold**, *italic*, __strong__, _soft_ and ~~gone~~.", "This is bold, italic, strong, soft and gone."),
        ("***All of it*** matters.", "All of it matters."),
        ("Open the [budget sheet](https://example.com/b.xlsx) now.", "Open the budget sheet now."),
        ("See [the docs][1] first.", "See the docs first."),
        ("![a chart](https://example.com/c.png)Revenue is up.", "Revenue is up."),
        ("Edit `config.toml` and run `jarvis sync`.", "Edit config.toml and run jarvis sync."),
        ("The name is snake_case_name here.", "The name is snake case name here."),
        ("More at https://example.com/plan?x=1.", "More at."),
        ("Read (https://example.com/a) and www.example.org/b, then stop.", "Read and, then stop."),
        ("Write to s.rao@example.com today.", "Write to s dot rao at example dot com today."),
        ("> Quoted words", "Quoted words"),
        ("- [ ] Send the draft", "Send the draft"),
        ("Q&A on R&D: 5% more, 10 °C, 2 × 3 → six.", "Q and A on R and D: 5 percent more, 10 degrees C, 2 times 3 to six."),
        ("Done ✅ 🎉 well done", "Done well done"),
        ("Pages 10–12 and 9:00 am - 10:00 am.", "Pages 10 to 12 and 9 am to 10 am."),
        ("Tools, e.g. a hammer, i.e. a heavy one, etc.", "Tools, for example a hammer, that is a heavy one, et cetera."),
        ("It costs ₹2.5 lakh.", "It costs 2.5 lakh rupees."),
        ("Due 2026-11-14.", "Due 14th of November 2026."),
        ("Due 2026-02-03 or 2026-13-45.", "Due 3rd of February 2026 or 2026-13-45."),
        ("At 10:00, 9:05 and 3:30 pm.", "At 10 o'clock, 9 oh 5 and 3:30 pm."),
        ("At 10:00 am sharp.", "At 10 am sharp."),
    ],
)
def test_speakable_takes_out_what_cannot_be_said(text, spoken):
    assert speak.speakable(text) == spoken


def test_speakable_makes_a_markdown_reply_into_plain_sentences():
    reply = (
        "## Today\n\n"
        "- **10:00**: Atlas sync with Priya, see [the invite](https://example.com/i).\n"
        "- Borealis review at 2 p.m.\n\n"
        "More at https://example.com/plan."
    )
    assert speak.speakable(reply) == (
        "Today. 10 o'clock: Atlas sync with Priya, see the invite. Borealis review at 2 p.m. More at."
    )


def test_speakable_leaves_code_blocks_unread_and_flattens_tables():
    text = "Run this:\n```python\nprint('hello. world')\n```\nThen look:\n| Name | Role |\n|------|------|\n| Priya | PM |\n"
    assert speak.speakable(text) == "Run this. Then look. Name, Role. Priya, PM"


@pytest.mark.parametrize(
    "text",
    [
        "Hello there.",
        "Hello there",
        "Mr. Rao met Dr. Kapoor at 3:30 p.m. on Thursday.",
        "It is 2.5 percent, or 1,200 pounds, in Northwind's Q3 plan (see notes).",
        "Don’t worry, it’s fine — really!",
    ],
)
def test_speakable_leaves_plain_sentences_alone(text):
    assert speak.speakable(text) == text


@pytest.mark.parametrize(
    "text", ["", "   ", "---", "***", "=====", "```", "...", "🎉", "| --- | --- |", "[1]: https://example.com", "![x](y.png)"]
)
def test_speakable_of_nothing_to_say_is_empty(text):
    assert speak.speakable(text) == ""


def test_speakable_is_idempotent():
    samples = [
        "## Plan\n- **Bold** [link](https://x.com/a), `code`, 2026-11-14 at 10:00 and 5% & more → done.",
        "Mr. Rao, e.g. at 9:05, wrote to a.b@example.com about ₹2 lakh — see www.example.org.",
        "1. First\n2. Second item\n\n> quoted _text_",
    ]
    for text in samples:
        once = speak.speakable(text)
        assert speak.speakable(once) == once


# --- sentences --------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Hello there. How are you? I am fine.", ["Hello there.", "How are you?", "I am fine."]),
        ("Hello there. How are you? I am fine", ["Hello there.", "How are you?", "I am fine"]),
        ("One sentence only", ["One sentence only"]),
        ("What?! Really... Yes! OK.", ["What?!", "Really...", "Yes!", "OK."]),
        ("", []),
        ("   \n\n  ", []),
        ("---\n***\n...", []),
    ],
)
def test_sentences_splits_on_end_marks(text, expected):
    assert speak.sentences(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Mr. Rao met Dr. Kapoor. They agreed.", ["Mr. Rao met Dr. Kapoor.", "They agreed."]),
        ("Use tools, e.g. a hammer, i.e. a heavy one. Then rest.", ["Use tools, e.g. a hammer, i.e. a heavy one.", "Then rest."]),
        ("S. Priya Raman wrote it. J. K. Rowling did not.", ["S. Priya Raman wrote it.", "J. K. Rowling did not."]),
        ("It starts at 3 p.m. They left.", ["It starts at 3 p.m.", "They left."]),
        ("It is about 3 p.m. today. Fine.", ["It is about 3 p.m. today.", "Fine."]),
        ("Tools, etc. Then we stop.", ["Tools, etc.", "Then we stop."]),
        ("Tools, etc. and more. Done.", ["Tools, etc. and more.", "Done."]),
        ("No. 5 is here. No. That is wrong.", ["No. 5 is here.", "No.", "That is wrong."]),
        ("Acme Inc. was sold. Next.", ["Acme Inc. was sold.", "Next."]),
        ("approx. five, vs. last year. Done.", ["approx. five, vs. last year.", "Done."]),
    ],
)
def test_sentences_does_not_split_after_abbreviations(text, expected):
    assert speak.sentences(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("It is 2.5 percent. Next.", ["It is 2.5 percent.", "Next."]),
        ("Version 1.2.3 is out. Update it.", ["Version 1.2.3 is out.", "Update it."]),
        ("It rose to 2.5. Then it fell.", ["It rose to 2.5.", "Then it fell."]),
        ("Open notes.md. Then report.", ["Open notes.md.", "Then report."]),
        ("See https://example.com/a.b. Then stop.", ["See https://example.com/a.b.", "Then stop."]),
    ],
)
def test_sentences_does_not_split_inside_numbers_and_names(text, expected):
    assert speak.sentences(text) == expected


def test_sentences_keeps_quotes_and_brackets_with_their_sentence():
    text = 'He said "stop." Then he left. (See above.) **Done.** Next.'
    assert speak.sentences(text) == ['He said "stop."', "Then he left.", "(See above.)", "**Done.**", "Next."]


def test_sentences_makes_a_sentence_of_each_heading_bullet_and_line():
    text = "## Summary\n\n- First point.\n- Second point without a stop\n1. Numbered item. And more.\n\nClosing line"
    assert speak.sentences(text) == [
        "## Summary", "- First point.", "- Second point without a stop", "1. Numbered item.", "And more.", "Closing line",
    ]


def test_sentences_skips_code_blocks():
    text = "Before the code.\n```python\nprint('x. y')\nmore code\n```\nAfter the code. Last."
    assert speak.sentences(text) == ["Before the code.", "After the code.", "Last."]


def test_a_block_of_code_on_one_line_is_not_taken_for_the_start_of_one():
    text = "Use ```print(1)``` here. Then go on.\n```x```\nStill read."
    assert speak.sentences(text) == ["Use ```print(1)``` here.", "Then go on.", "```x```", "Still read."]
    assert speak.speakable(text) == "Use print(1) here. Then go on. x. Still read."


def test_sentences_skips_a_code_block_that_is_never_closed():
    assert speak.sentences("Before.\n```\nprint('x. y')") == ["Before."]


# --- SentenceStream ---------------------------------------------------------


def test_stream_waits_for_a_sentence_to_be_over():
    stream = speak.SentenceStream()
    assert stream.feed("Hello wor") == []
    assert stream.feed("Hello world.") == []  # may still turn into "Hello world.5"
    assert stream.feed("Hello world. ") == []
    assert stream.feed("Hello world. How") == ["Hello world."]
    assert stream.feed("Hello world. How are you?") == []
    assert stream.feed("Hello world. How are you? I am") == ["How are you?"]
    assert stream.feed("Hello world. How are you? I am fine") == []


def test_stream_returns_the_last_sentence_when_the_reply_has_no_final_full_stop():
    stream = speak.SentenceStream()
    assert stream.feed("Hello world. I am fine") == ["Hello world."]
    assert stream.finish("Hello world. I am fine") == ["I am fine"]
    assert stream.finish("Hello world. I am fine") == []


def test_stream_finish_returns_a_final_sentence_that_ends_in_a_full_stop():
    stream = speak.SentenceStream()
    assert stream.feed("Hello world. How are you?") == ["Hello world."]
    assert stream.finish("Hello world. How are you?") == ["How are you?"]


def test_stream_returns_nothing_new_for_the_same_text():
    stream = speak.SentenceStream()
    text = "One. Two. Three"
    assert stream.feed(text) == ["One.", "Two."]
    assert stream.feed(text) == []
    assert stream.feed(text) == []
    assert stream.finish(text) == ["Three"]
    assert stream.feed(text) == []
    assert stream.finish(text) == []


def test_stream_finish_alone_returns_everything():
    assert speak.SentenceStream().finish("One. Two. Three") == ["One.", "Two.", "Three"]
    assert speak.SentenceStream().finish("") == []


def test_stream_does_not_split_after_an_abbreviation_while_the_text_grows():
    stream = speak.SentenceStream()
    steps = ["Mr.", "Mr. Rao", "Mr. Rao is here. Dr.", "Mr. Rao is here. Dr. Kapoor", "Mr. Rao is here. Dr. Kapoor, e.g. waved. Then"]
    assert [stream.feed(text) for text in steps] == [[], [], ["Mr. Rao is here."], [], ["Dr. Kapoor, e.g. waved."]]
    assert stream.finish("Mr. Rao is here. Dr. Kapoor, e.g. waved. Then stop") == ["Then stop"]


def test_stream_does_not_split_a_decimal_that_is_still_arriving():
    stream = speak.SentenceStream()
    assert stream.feed("The ratio is 2.") == []
    assert stream.feed("The ratio is 2.5") == []
    assert stream.feed("The ratio is 2.5 to one. Next") == ["The ratio is 2.5 to one."]
    assert stream.feed("The ratio is 2.5 to one. Next one is 3.") == []
    assert stream.finish("The ratio is 2.5 to one. Next one is 3.7 here.") == ["Next one is 3.7 here."]


def test_stream_hands_out_a_bullet_when_its_line_is_done():
    stream = speak.SentenceStream()
    assert stream.feed("## Title") == []
    assert stream.feed("## Title\n") == ["## Title"]
    assert stream.feed("## Title\n- one") == []
    assert stream.feed("## Title\n- one\n- two\n") == ["- one", "- two"]
    assert stream.finish("## Title\n- one\n- two\n\nAfter the list") == ["After the list"]


def test_stream_holds_back_code_until_it_is_known_to_be_code():
    stream = speak.SentenceStream()
    text = "Look.\n```\nx = 1. y\n```\nDone. Bye"
    got: list[str] = []
    for end in range(1, len(text) + 1):
        got += stream.feed(text[:end])
    got += stream.finish(text)
    assert got == ["Look.", "Done.", "Bye"]


REPLY = """Here's what I found:

## Today
- **10:00**: Atlas sync with Priya (Teams link in the invite).
- Meera sent the deck: [Budget v3](https://docs.google.com/spreadsheets/d/abc123). Mr. Rao has not.

You have 3 unread mails from Ravi, e.g. the Atlas invoice (due 2026-11-20, about 2.5 lakh).

```python
print("hi. there")
```

Want me to draft a reply? I can also check `jarvis/notes.py`. Shall I?"""


def test_stream_gives_the_same_sentences_however_the_text_arrives():
    expected = speak.sentences(REPLY)
    assert expected == [
        "Here's what I found:",
        "## Today",
        "- **10:00**: Atlas sync with Priya (Teams link in the invite).",
        "- Meera sent the deck: [Budget v3](https://docs.google.com/spreadsheets/d/abc123).",
        "Mr. Rao has not.",
        "You have 3 unread mails from Ravi, e.g. the Atlas invoice (due 2026-11-20, about 2.5 lakh).",
        "Want me to draft a reply?",
        "I can also check `jarvis/notes.py`.",
        "Shall I?",
    ]
    rng = random.Random(7)
    for _ in range(20):
        stream = speak.SentenceStream()
        got: list[str] = []
        end = 0
        while end < len(REPLY):
            end = min(len(REPLY), end + rng.randint(1, 15))
            got += stream.feed(REPLY[:end])
        got += stream.finish(REPLY)
        assert got == expected


# --- which voice ------------------------------------------------------------


def test_engine_is_the_system_voice_when_that_is_asked_for():
    assert speak.engine() == ("system" if SAY_IS_HERE else None)


def test_engine_is_none_without_any_voice(monkeypatch):
    monkeypatch.setattr(speak, "SAY", "/nonexistent/say")
    assert speak.engine() is None


def test_engine_picks_kokoro_when_it_can_be_used_and_never_loads_it(monkeypatch):
    monkeypatch.delenv(speak.ENGINE_ENV)
    monkeypatch.setattr(speak, "_kokoro_ready", lambda: True)
    monkeypatch.setattr(speak, "_failed", False)

    def load():
        raise AssertionError("engine() must not load a model")

    monkeypatch.setattr(speak, "_load", load)
    assert speak.engine() == "kokoro"


def test_engine_falls_back_to_the_system_voice_when_kokoro_is_missing_or_has_failed(monkeypatch):
    monkeypatch.delenv(speak.ENGINE_ENV)
    monkeypatch.setattr(speak, "_failed", False)
    monkeypatch.setattr(speak, "_kokoro_ready", lambda: False)
    assert speak.engine() == ("system" if SAY_IS_HERE else None)
    monkeypatch.setattr(speak, "_kokoro_ready", lambda: True)
    monkeypatch.setattr(speak, "_failed", True)
    assert speak.engine() == ("system" if SAY_IS_HERE else None)


def test_a_missing_package_or_cache_means_kokoro_is_not_ready(monkeypatch):
    monkeypatch.setattr(speak, "PACKAGES", ("surely_not_installed_anywhere",))
    assert speak._kokoro_ready() is False
    monkeypatch.setattr(speak, "PACKAGES", ())
    monkeypatch.setattr(speak, "_snapshot", lambda: None)
    assert speak._kokoro_ready() is False


LISTING = """Albert              en_US    # Hello! My name is Albert.
Aman                en_IN    # Hello! My name is Aman.
Daniel              en_GB    # Hello! My name is Daniel.
Eddy (English (UK)) en_GB    # Hello! My name is Eddy.
Grandma (English (UK)) en_GB    # Hello! My name is Grandma.
Karen               en_AU    # Hello! My name is Karen.
"""


def test_the_system_voice_is_daniel_when_nothing_better_is_installed():
    assert speak.best_voice(LISTING) == "Daniel"


def test_the_system_voice_prefers_premium_then_enhanced_british_voices():
    enhanced = LISTING + "Oliver (Enhanced)    en_GB    # Hello! My name is Oliver.\n"
    assert speak.best_voice(enhanced) == "Oliver (Enhanced)"
    premium = enhanced + "Serena (Premium)     en_GB    # Hello! My name is Serena.\n"
    assert speak.best_voice(premium) == "Serena (Premium)"


def test_the_system_voice_prefers_a_man_among_voices_of_the_same_quality():
    both = "Kate (Enhanced)       en_GB    # Hello!\nOliver (Enhanced)     en_GB    # Hello!\n"
    assert speak.best_voice(both) == "Oliver (Enhanced)"


def test_the_system_voice_is_the_default_when_no_british_voice_fits():
    american = "Samantha (Premium)   en_US    # Hello!\nEddy (English (UK)) en_GB    # Hello!\nAlex                en_US    # Hello!\n"
    assert speak.best_voice(american) is None
    assert speak.best_voice("") is None


# --- synthesize -------------------------------------------------------------


@needs_say
def test_synthesize_gives_a_mono_16_bit_wav():
    data = speak.synthesize("Hello there. This is a test.")
    channels, bits, rate, seconds = wav_format(data)
    assert (channels, bits, rate) == (1, 16, speak.SAY_RATE)
    assert seconds > 0.5
    assert data[12:16] == b"fmt " and b"FLLR" not in data  # a bare file, not the one macOS writes


def macos_style_wav(pcm: bytes, channels: int = 1) -> bytes:
    """A WAV file the way ``say`` writes one: a JUNK chunk before ``fmt `` and a filler chunk before the sound."""
    fmt = b"fmt " + (16).to_bytes(4, "little") + (1).to_bytes(2, "little") + channels.to_bytes(2, "little")
    fmt += (22050).to_bytes(4, "little") + (22050 * 2 * channels).to_bytes(4, "little")
    fmt += (2 * channels).to_bytes(2, "little") + (16).to_bytes(2, "little")
    body = b"WAVE" + b"JUNK" + (4).to_bytes(4, "little") + bytes(4) + fmt
    body += b"FLLR" + (6).to_bytes(4, "little") + bytes(6) + b"data" + len(pcm).to_bytes(4, "little") + pcm
    return b"RIFF" + len(body).to_bytes(4, "little") + body


def test_the_system_sound_is_handed_on_in_a_bare_wav_file():
    pcm = bytes(range(100)) * 4
    plain = speak._plain_wav(macos_style_wav(pcm))
    assert plain[:4] == b"RIFF" and plain[12:16] == b"fmt " and b"JUNK" not in plain and b"FLLR" not in plain
    assert plain[-len(pcm):] == pcm
    assert wav_format(plain)[:3] == (1, 16, 22050)


def test_system_sound_that_is_not_mono_16_bit_is_refused():
    with pytest.raises(RuntimeError, match="mono, 16-bit"):
        speak._plain_wav(macos_style_wav(bytes(400), channels=2))


@needs_say
def test_synthesize_reads_text_that_starts_with_a_dash():
    assert wav_format(speak.synthesize("- not an option, just a bullet"))[3] > 0.5


@needs_say
def test_synthesize_can_be_called_from_several_threads_at_once():
    sounds: list[bytes] = []
    errors: list[Exception] = []

    def speak_it(number: int) -> None:
        try:
            sounds.append(speak.synthesize(f"This is voice number {number}, speaking with the others."))
        except Exception as exc:  # reported below
            errors.append(exc)

    threads = [threading.Thread(target=speak_it, args=(number,)) for number in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert [wav_format(sound)[:3] for sound in sounds] == [(1, 16, speak.SAY_RATE)] * 4


def test_the_system_voice_only_ever_writes_a_file(monkeypatch):
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[1] == "-o":
            Path(command[2]).write_bytes(speak._wav(bytes(400), speak.SAY_RATE))
        return type("Done", (), {"stdout": "", "stderr": b""})()

    monkeypatch.setattr(speak.subprocess, "run", fake_run)
    monkeypatch.setattr(speak, "_say_available", lambda: True)
    monkeypatch.setattr(speak, "_system_voice", lambda: "Daniel")
    speak.synthesize("- hello")
    (command,) = commands
    assert command[:2] == [speak.SAY, "-o"] and command[3] == f"--data-format=LEI16@{speak.SAY_RATE}"
    assert command[4:] == ["-v", "Daniel", "-f", "-"]  # the text goes in on stdin
    assert not {"-a", "-n", "--audio-device", "--network-send"} & set(command)


def test_even_the_list_of_voices_is_asked_for_with_an_output_file(monkeypatch):
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return type("Done", (), {"stdout": "Daniel              en_GB    # Hello! My name is Daniel.\n"})()

    monkeypatch.setattr(speak.subprocess, "run", fake_run)
    assert speak._system_voice.__wrapped__() == "Daniel"
    assert commands == [[speak.SAY, "-o", os.devnull, "-v", "?"]]


def test_text_with_nothing_to_say_is_a_moment_of_quiet_and_needs_no_voice(monkeypatch):
    def refuse(text):
        raise AssertionError("no voice should be asked")

    monkeypatch.setattr(speak, "_system_wav", refuse)
    channels, bits, _, seconds = wav_format(speak.synthesize(" ... --- "))
    assert (channels, bits) == (1, 16)
    assert 0 < seconds < 0.5


def test_names_both_voices_get_wrong_are_respelled(monkeypatch):
    said: list[str] = []
    monkeypatch.setattr(speak, "_system_wav", lambda text: said.append(text) or b"")
    speak.synthesize("AWS is not AWSOME, but we use AWS.")
    assert said == ["A.W.S. is not AWSOME, but we use A.W.S."]


def test_synthesize_uses_the_system_voice_when_kokoro_fails(monkeypatch):
    def broken(text):
        raise RuntimeError("the model fell over")

    monkeypatch.setattr(speak, "engine", lambda: "kokoro")
    monkeypatch.setattr(speak, "_kokoro_wav", broken)
    monkeypatch.setattr(speak, "_system_wav", lambda text: b"system voice")
    assert speak.synthesize("Hello there.") == b"system voice"


def test_synthesize_without_any_voice_says_so(monkeypatch):
    monkeypatch.setattr(speak, "SAY", "/nonexistent/say")
    with pytest.raises(RuntimeError, match="no voice"):
        speak.synthesize("Hello there.")


def test_the_end_of_a_sentence_is_marked_for_the_voice():
    assert speak._ended("Hello there") == "Hello there."
    assert speak._ended("Hello there,") == "Hello there."
    assert speak._ended("Hello there?") == "Hello there?"
    assert speak._ended('He said "go."') == 'He said "go."'
    assert speak._ended("  Hello there!  ") == "Hello there!"


# --- the Kokoro path, with a stand-in for the model -------------------------


class FakePipeline:
    """Speaks a tone with a second of quiet on each side, and notes whether two calls ever overlap."""

    def __init__(self) -> None:
        self.active = 0
        self.most_at_once = 0
        self.level = 0.25
        self.calls: list[tuple[str, str, float]] = []

    def __call__(self, text: str, voice: str, speed: float):
        self.active += 1
        self.most_at_once = max(self.most_at_once, self.active)
        self.calls.append((text, voice, speed))
        time.sleep(0.02)
        self.active -= 1
        tone = self.level * np.sin(np.linspace(0, 440 * 2 * np.pi, speak.RATE // 2)).astype(np.float32)
        quiet = np.zeros(speak.RATE, dtype=np.float32)
        yield "", "", np.concatenate([quiet, tone, quiet]).reshape(1, -1)


@pytest.fixture
def fake_kokoro(monkeypatch: pytest.MonkeyPatch) -> FakePipeline:
    pipeline = FakePipeline()
    monkeypatch.setattr(speak, "_load", lambda: pipeline)
    monkeypatch.setattr(speak, "_voice_file", "/cache/voices/bm_george.safetensors")
    return pipeline


def test_kokoro_sound_is_trimmed_and_encoded_as_mono_16_bit(fake_kokoro):
    data = speak._kokoro_wav("Hello there")
    channels, bits, rate, seconds = wav_format(data)
    assert (channels, bits, rate) == (1, 16, speak.RATE)
    assert seconds == pytest.approx(0.5 + speak.PAD_BEFORE + speak.PAD_AFTER, abs=0.02)  # not the 2.5 s it was made as
    with wave.open(io.BytesIO(data)) as wav:
        samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
    assert abs(int(samples.max()) - 0.25 * speak.GAIN * 32767) < 200  # the tone, at the level the voices are matched to


def test_kokoro_sound_that_is_too_loud_is_cut_off_not_wrapped_round(fake_kokoro):
    fake_kokoro.level = 0.9
    with wave.open(io.BytesIO(speak._kokoro_wav("Hello there"))) as wav:
        samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
    assert samples.max() == 32767 and samples.min() == -32767


def test_kokoro_is_given_the_voice_file_the_speed_and_a_finished_sentence(fake_kokoro):
    speak._kokoro_wav("Hello there")
    assert fake_kokoro.calls == [("Hello there.", "/cache/voices/bm_george.safetensors", speak.SPEED)]


def test_kokoro_never_speaks_two_sentences_at_once(fake_kokoro):
    threads = [threading.Thread(target=speak._kokoro_wav, args=(f"Sentence {number}.",)) for number in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(fake_kokoro.calls) == 6
    assert fake_kokoro.most_at_once == 1


def test_kokoro_that_makes_no_sound_is_an_error_so_the_system_voice_can_step_in(monkeypatch):
    monkeypatch.setattr(speak, "_load", lambda: lambda text, voice, speed: iter(()))
    with pytest.raises(RuntimeError, match="no sound"):
        speak._kokoro_wav("日本語")


def test_the_cache_is_only_looked_at_never_filled_by_a_check(monkeypatch, tmp_path):
    asked: list[tuple[str, dict]] = []

    def hub(repo, **kwargs):
        asked.append((repo, kwargs))
        return str(tmp_path)

    monkeypatch.setattr("huggingface_hub.snapshot_download", hub)
    assert speak._snapshot() is None  # the folder has none of the files
    for name in speak._wanted():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(b"x")
    assert speak._snapshot() == tmp_path
    assert [repo for repo, _ in asked] == [speak.MODEL, speak.MODEL]
    assert all(kwargs["local_files_only"] is True for _, kwargs in asked)


def test_download_asks_for_only_the_files_the_voice_needs(monkeypatch, tmp_path):
    asked: list[tuple[str, dict]] = []
    monkeypatch.setattr("huggingface_hub.snapshot_download", lambda repo, **kwargs: asked.append((repo, kwargs)) or str(tmp_path))
    assert speak.download() == tmp_path
    patterns = ["config.json", "kokoro-v1_0.safetensors", "voices/bm_george.safetensors"]
    assert asked == [(speak.MODEL, {"allow_patterns": patterns})]


def test_load_builds_the_british_pipeline_from_the_cache_and_reads_the_voice_by_path(monkeypatch, tmp_path):
    built: dict = {}
    monkeypatch.setattr(speak, "_pipeline", None)
    monkeypatch.setattr(speak, "_voice_file", "")
    monkeypatch.setattr(speak, "_failed", False)
    monkeypatch.setattr(speak, "_snapshot", lambda: tmp_path)
    monkeypatch.setattr("mlx_audio.tts.load", lambda path, **kwargs: built.update(loaded=(path, kwargs)) or "the model")
    monkeypatch.setattr(
        "mlx_audio.tts.models.kokoro.KokoroPipeline", lambda **kwargs: built.update(pipeline=kwargs) or "the pipeline"
    )
    assert speak._load() == "the pipeline"
    assert speak._load() == "the pipeline"  # kept: the second call builds nothing
    assert built["loaded"] == (tmp_path, {"model_type": "kokoro"})
    assert built["pipeline"] == {"lang_code": "b", "model": "the model", "repo_id": speak.MODEL, "model_path": tmp_path}
    assert speak._voice_file == str(tmp_path / "voices" / "bm_george.safetensors")
    assert speak._failed is False


def test_warm_loads_kokoro_once_and_a_failure_is_only_logged(monkeypatch, caplog):
    monkeypatch.delenv(speak.ENGINE_ENV)
    monkeypatch.setattr(speak, "_kokoro_ready", lambda: True)
    monkeypatch.setattr(speak, "_failed", False)
    loads: list[int] = []
    monkeypatch.setattr(speak, "_load", lambda: loads.append(1))
    speak.warm()
    assert loads == [1]

    def broken():
        raise OSError("disk gone")

    monkeypatch.setattr(speak, "_load", broken)
    with caplog.at_level("WARNING", logger="jarvis"):
        speak.warm()
    assert "disk gone" in caplog.text


def test_a_model_that_cannot_be_loaded_is_given_up_on(monkeypatch):
    monkeypatch.setattr(speak, "_pipeline", None)
    monkeypatch.setattr(speak, "_failed", False)
    monkeypatch.setattr(speak, "_snapshot", lambda: None)
    with pytest.raises(FileNotFoundError, match="download"):
        speak._load()
    assert speak._failed is True


def test_unload_without_a_model_does_nothing(monkeypatch):
    monkeypatch.setattr(speak, "_pipeline", None)
    speak.unload()
    assert speak._pipeline is None


def test_unload_lets_go_of_the_model(monkeypatch):
    monkeypatch.setattr(speak, "_pipeline", object())
    speak.unload()
    assert speak._pipeline is None


def test_warm_does_nothing_for_the_system_voice(monkeypatch):
    monkeypatch.setattr(speak, "_load", lambda: pytest.fail("the system voice has nothing to load"))
    speak.warm()
