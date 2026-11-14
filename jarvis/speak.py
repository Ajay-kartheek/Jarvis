"""Jarvis's speaking voice: text in, the bytes of a sound file out.

Nothing here plays a sound. The audio is handed to the interface, which decides
when it is heard; that matters when the user is on a call with headphones in.

When the user has given Jarvis an ElevenLabs key, the voice comes from there
(see ``eleven``): what Jarvis says is sent to ElevenLabs, and comes back as MP3.
Whenever that voice cannot speak, the voice on this Mac does.

On this Mac the first choice is Kokoro-82M through mlx-audio, a calm British man
(``bm_george``): about a tenth of real time on Apple silicon, and entirely
offline once its weights are in the Hugging Face cache (``download`` puts them
there once). If Kokoro cannot be used, because a package or the weights are
missing or the model will not load, the macOS voice speaks instead, through
``say -o file``, so a spoken reply never fails for want of the better voice.
Setting ``JARVIS_SPEAK_ENGINE=system`` skips Kokoro altogether.

Nothing is fetched at run time. The weights are read from the local cache and
the voice file by its path, so the library has no reason to go to the network;
spaCy's English model, which Kokoro's text front end needs, is a dependency of
the project and not downloaded on first use.

The rest is text. ``speakable`` turns a sentence of markdown into what reads
well aloud; ``sentences`` and ``SentenceStream`` cut a reply into sentences, so
the first can be spoken while the rest is still being written.
"""

from __future__ import annotations

import functools
import gc
import hashlib
import html
import importlib.util
import io
import logging
import os
import platform
import re
import subprocess
import sys
import tempfile
import threading
import wave
from pathlib import Path

from jarvis import eleven, paths

log = logging.getLogger("jarvis")

SAY = "/usr/bin/say"
ENGINE_ENV = "JARVIS_SPEAK_ENGINE"  # "system": always the macOS voice, Kokoro never loaded (for tests and tight memory)
SAY_RATE = 22_050
SAY_SECONDS = 60

MODEL = "mlx-community/Kokoro-82M-bf16"
WEIGHTS = "kokoro-v1_0.safetensors"
VOICE = "bm_george"  # calm British man. The others cached by default: bm_daniel, bm_fable, bm_lewis
SPEED = 1.0
GAIN = 2.0  # Kokoro's voice is about 6 dB quieter than the macOS one; this keeps a fall-back from being twice as loud
RATE = 24_000
PACKAGES = ("mlx", "mlx_audio", "misaki", "spacy", "en_core_web_sm", "espeakng_loader", "phonemizer", "num2words")
PAD_BEFORE = 0.04  # seconds of quiet kept around a sentence; Kokoro adds about 0.25 and 0.5
PAD_AFTER = 0.2
CACHE_BYTES = 128 * 2**20  # what MLX may keep of freed memory while a sentence is made
# Words both voices get wrong ("AWS" comes out as "awz"), written the way they should be said.
SAY_AS = {"AWS": "A.W.S."}

_lock = threading.Lock()  # one sentence at a time: the model is not for concurrent use, and each needs gigabytes
_pipeline = None
_voice_file = ""
_failed = False


# --- which voice ------------------------------------------------------------


def engine() -> str | None:
    """The voice that will speak: "elevenlabs", "kokoro", "system" or None. Never loads a model."""
    if _cloud_wanted() and eleven.available():
        return "elevenlabs"
    return local_engine()


def local_engine() -> str | None:
    """The voice on this Mac: "kokoro", "system" or None. Looks at the disk, never loads a model."""
    if _kokoro_wanted() and (_pipeline is not None or _kokoro_ready()):
        return "kokoro"
    return "system" if _say_available() else None


def _cloud_wanted() -> bool:
    return os.environ.get(ENGINE_ENV, "").strip().lower() not in ("system", "local")


def _kokoro_wanted() -> bool:
    return os.environ.get(ENGINE_ENV, "").strip().lower() != "system" and not _failed


def _say_available() -> bool:
    return os.access(SAY, os.X_OK)


def _kokoro_ready() -> bool:
    try:
        return (
            sys.platform == "darwin" and platform.machine() == "arm64"
            and all(importlib.util.find_spec(name) for name in PACKAGES)
            and _snapshot() is not None
        )
    except Exception:
        return False


def _wanted() -> list[str]:
    return ["config.json", WEIGHTS, f"voices/{VOICE}.safetensors"]


def _snapshot() -> Path | None:
    """The Kokoro files in the local Hugging Face cache, or None. Never goes to the network."""
    from huggingface_hub import snapshot_download

    try:
        folder = Path(snapshot_download(MODEL, allow_patterns=_wanted(), local_files_only=True))
    except Exception:
        return None
    return folder if all((folder / name).is_file() for name in _wanted()) else None


def download() -> Path:
    """Put the Kokoro files in the Hugging Face cache (about 330 MB). The one place here that uses the network."""
    from huggingface_hub import snapshot_download

    return Path(snapshot_download(MODEL, allow_patterns=_wanted()))


# --- Kokoro -----------------------------------------------------------------


def _load():
    """Kokoro's model and text front end from the local cache. Call with the lock held."""
    global _pipeline, _voice_file, _failed
    if _pipeline is None:
        try:
            from mlx_audio.tts import load
            from mlx_audio.tts.models.kokoro import KokoroPipeline

            folder = _snapshot()
            if folder is None:
                raise FileNotFoundError("the Kokoro files are not in the local cache; run speak.download() once")
            model = load(folder, model_type="kokoro")
            _pipeline = KokoroPipeline(lang_code="b", model=model, repo_id=MODEL, model_path=folder)
            _voice_file = str(folder / "voices" / f"{VOICE}.safetensors")  # a path: no look-up on the hub
        except Exception:
            _failed = True
            raise
    return _pipeline


def warm() -> None:
    """Get the voice ready, so the first sentence does not wait for it: the cloud voice's connection, or Kokoro.
    Does nothing for the system voice."""
    if engine() == "elevenlabs":
        eleven.warm()
        return
    if engine() != "kokoro":
        return
    with _lock:
        try:
            _load()
        except Exception as exc:
            log.warning("the Kokoro voice could not be loaded: %s", exc)


def unload() -> None:
    """Give the model's memory back. The next ``synthesize`` loads it again."""
    global _pipeline
    with _lock:
        if _pipeline is None:
            return
        _pipeline = None
    gc.collect()
    import mlx.core as mx

    mx.clear_cache()


def _kokoro_wav(text: str) -> bytes:
    import numpy as np

    samples = _kokoro_samples(text)
    loud = np.flatnonzero(np.abs(samples) > 0.02 * np.abs(samples).max())
    if len(loud):  # cut the quiet before and after down, so sentences played in a row are not far apart
        samples = samples[max(0, loud[0] - int(PAD_BEFORE * RATE)):loud[-1] + int(PAD_AFTER * RATE)]
    return _wav((np.clip(samples * GAIN, -1.0, 1.0) * 32767).astype("<i2").tobytes(), RATE)


def _kokoro_samples(text: str):
    """``text`` as Kokoro says it: float samples at ``RATE``. One call at a time, whoever asks."""
    import mlx.core as mx
    import numpy as np

    with _lock:
        pipeline = _load()
        # A sentence needs about 1.7 GB of scratch memory. MLX may by default keep everything it frees, so the
        # process peaked near 3.4 GB; with the cache limited it stays near 2 GB, at the same speed.
        keep = mx.set_cache_limit(CACHE_BYTES)
        try:
            made = pipeline(_ended(text), voice=_voice_file, speed=SPEED)  # one piece per line, and per 510 phonemes
            heard = [np.array(audio).reshape(-1) for _, _, audio in made]
        finally:
            mx.set_cache_limit(keep)
            mx.clear_cache()
    if not heard:
        raise RuntimeError("Kokoro made no sound for this text")
    return np.concatenate(heard)


# --- the macOS voice --------------------------------------------------------

_VOICE_LINE = re.compile(r"^(?P<name>.+?)\s+(?P<locale>[a-z]{2}_[A-Z]{2})\s+#")
_BRITISH_MEN = ("Daniel", "Oliver", "Jamie", "Arthur")


def best_voice(listing: str) -> str | None:
    """The best British voice in ``say -v '?'`` output: Premium, then Enhanced, then Daniel; None for the default."""
    best = None
    for line in listing.splitlines():
        found = _VOICE_LINE.match(line)
        if not found or found["locale"] != "en_GB":
            continue
        name = found["name"]
        rank = 0 if "(Premium)" in name else 1 if "(Enhanced)" in name else 2 if name == "Daniel" else None
        if rank is None:
            continue
        key = (rank, name.split(" (")[0] not in _BRITISH_MEN)  # men first, among voices of the same quality
        if best is None or key < best[0]:
            best = (key, name)
    return best[1] if best else None


@functools.cache
def _system_voice() -> str | None:
    try:
        # Listing the voices makes no sound; it is given an output file all the same, so that no call of say
        # in this module could ever be heard.
        command = [SAY, "-o", os.devnull, "-v", "?"]
        listing = subprocess.run(command, capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return best_voice(listing)


def _system_wav(text: str) -> bytes:
    if not _say_available():
        raise RuntimeError("no voice is available: Kokoro cannot be used and /usr/bin/say is missing")
    with tempfile.TemporaryDirectory(prefix="jarvis-say-") as folder:
        path = Path(folder) / "speech.wav"
        command = [SAY, "-o", str(path), f"--data-format=LEI16@{SAY_RATE}"]  # always to a file: nothing is played
        voice = _system_voice()
        if voice:
            command += ["-v", voice]
        command += ["-f", "-"]  # the text on stdin: on the command line a leading "-" would be taken for an option
        try:
            subprocess.run(command, input=_ended(text).encode(), capture_output=True, check=True, timeout=SAY_SECONDS)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"say failed: {exc.stderr.decode(errors='replace').strip()}") from exc
        return _plain_wav(path.read_bytes())


def _plain_wav(data: bytes) -> bytes:
    """The sound in a WAV file, in a bare one: macOS pads its files with filler chunks that some players dislike."""
    with wave.open(io.BytesIO(data)) as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise RuntimeError("the system voice did not make mono, 16-bit sound")
        return _wav(wav.readframes(wav.getnframes()), wav.getframerate())


# --- speaking ---------------------------------------------------------------


def synthesize(text: str, previous: str = "", keep: bool = False) -> bytes:
    """``text`` spoken, as the bytes of one sound file: MP3 from the cloud voice, otherwise WAV (mono, 16-bit).
    ``previous`` is what was said just before in the same reply; the cloud voice takes its tune from it. ``keep``: one
    of Jarvis's own short lines ("Right, checking."), made once by each voice and kept on disk, so that it is heard at
    once and costs the cloud voice nothing the second time. Blocks; any number of threads may call it."""
    if not any(char.isalnum() for char in text):
        return _wav(bytes(2 * RATE // 10), RATE)  # nothing to say: a tenth of a second of quiet
    text = _PRONOUNCED.sub(lambda found: SAY_AS[found[0].rstrip(".")], text)
    if keep and (voice := engine()) is not None:
        try:
            return _kept(voice, text).read_bytes()
        except OSError:
            pass  # not made yet by this voice
    sound, voice = _made(text, "" if keep else previous)
    if keep:
        _keep(_kept(voice, text), sound)
    return sound


def _made(text: str, previous: str) -> tuple[bytes, str]:
    """``text`` spoken by the best voice that can speak now, and which voice that was."""
    if engine() == "elevenlabs":
        try:
            return eleven.synthesize(text, previous), "elevenlabs"
        except eleven.Unavailable as exc:
            log.warning("the ElevenLabs voice could not speak (%s); using the voice on this Mac", exc)
    if local_engine() == "kokoro":
        try:
            return _kokoro_wav(text), "kokoro"
        except Exception as exc:
            log.warning("Kokoro could not speak (%s); using the system voice", exc)
    return _system_wav(text), "system"


def _kept(voice: str, text: str) -> Path:
    """Where ``text`` said by ``voice`` is kept: a file per line and per everything that changes how it sounds."""
    sounds = {
        "elevenlabs": f"{eleven.VOICE} {eleven.MODEL} {eleven.FORMAT}",
        "kokoro": f"{MODEL} {VOICE} {SPEED} {GAIN}",
    }.get(voice, f"{_system_voice()} {SAY_RATE}")
    digest = hashlib.sha256(f"{voice} {sounds}\n{text}".encode()).hexdigest()[:24]
    return paths.data_dir() / "voice" / "lines" / f"{digest}.{'mp3' if voice == 'elevenlabs' else 'wav'}"


def _keep(path: Path, sound: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(".part")
        partial.write_bytes(sound)
        partial.replace(path)
    except OSError as exc:  # it is made again next time
        log.warning("a spoken line could not be kept: %s", exc)


def _wav(pcm: bytes, rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm)
    return buffer.getvalue()


def _ended(text: str) -> str:
    """``text`` with a full stop at its end if it has no end mark, so the voice ends it as a sentence."""
    text = text.strip().rstrip(",;: ")
    return text if _END_OF_SENTENCE.search(text) else text + "."


# A full stop right after the word is taken in: the respelling has its own, and ends the sentence if it is the last.
_PRONOUNCED = re.compile(r"\b(?:" + "|".join(map(re.escape, SAY_AS)) + r")\b\.?")
_END_OF_SENTENCE = re.compile(r"[.!?…][)\"'”’*_]*$")


# --- text: what reads well aloud --------------------------------------------

_FENCE = re.compile(r"^\s*(?:```[^`]*|~~~.*)$")  # opens or closes a block of code; "```x```" on one line does not
_RULE = re.compile(r"^\s*(?:[-*_=]\s*){3,}$")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$")
_DEFINITION = re.compile(r"^\s*\[[^\]]+\]:\s+\S+")
# What opens a line without being said: a heading's #, a quote's >, a bullet, a number, a task box.
_MARKER = re.compile(r"^\s*(?:#{1,6}\s+|>+\s*|(?:[-*+•◦▪‣●○]|\d{1,3}[.)])\s+(?:\[[ xX]\]\s+)?)*")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\((?:[^()]|\([^()]*\))*\)")
_REFERENCE = re.compile(r"\[([^\]]+)\]\[[^\]]*\]")
_FOOTNOTE = re.compile(r"\[\^?\d+\]")
_HTML = re.compile(r"</?[A-Za-z][^>]*>")
_URL = re.compile(r"(?:https?://|www\.)[^\s<>]+")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_STARS = re.compile(r"(\*{1,3})(?=\S)(.+?)(?<=\S)\1")
_UNDERSCORES = re.compile(r"(?<!\w)(_{1,3})(?=\S)(.+?)(?<=\S)\1(?!\w)")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_RANGE = re.compile(r"(?<=\d)\s*–\s*(?=\d)")
_TIMES = re.compile(r"(\d{1,2}:\d{2}(?:\s*[ap]\.?m\b\.?)?)\s*-\s*(?=\d{1,2}:\d{2})", re.IGNORECASE)
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_HOUR = re.compile(r"(?<![\d:])(\d{1,2}):00(?!\d|:\d)(\s*[ap]\.?m\b)?", re.IGNORECASE)
_OH_MINUTE = re.compile(r"(?<![\d:])(\d{1,2}):0(\d)(?!\d|:\d)")
_RUPEES = re.compile(r"₹\s*(\d[\d,]*(?:\.\d+)?(?:\s*(?:lakhs?|crores?|thousand|million|billion|k|m)\b)?)", re.IGNORECASE)
_NOT_SPOKEN = re.compile(r"[^\w\s.,;:!?'’\"“”‘()\-—…/$£€+]")
_DASH = re.compile(r"\s-{1,2}\s")
_AT = re.compile(r"(?<=\w)@")
_NUMBER_SIGN = re.compile(r"#(?=\d)")
_SPACES = re.compile(r"\s+")
_BEFORE_MARK = re.compile(r"\s+([,;:.!?)])")
_EMPTY_BRACKETS = re.compile(r"\(\s*\)|(?<=[(])\s+")
_COMMAS = re.compile(r"(?:,\s*){2,}")
_AFTER_END = re.compile(r"[.!?…:;,][)\"'”’*_]*$")
_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
# Spelled out the way they are said. A dot that closes the word is left for the sentence.
_SPELLED = (
    (re.compile(r"\be\.g\.", re.IGNORECASE), "for example"),
    (re.compile(r"\bi\.e\.", re.IGNORECASE), "that is"),
    (re.compile(r"\bvs\.?(?=\s)", re.IGNORECASE), "versus"),
    (re.compile(r"\bapprox\.", re.IGNORECASE), "approximately"),
    (re.compile(r"\betc(?=\.)", re.IGNORECASE), "et cetera"),
)
_SYMBOLS = {
    "&": " and ", "%": " percent", "=": " equals ", "°": " degrees ", "×": " times ", "÷": " divided by ",
    "±": " plus or minus ", "≈": " about ", "≥": " at least ", "≤": " at most ", "≠": " is not equal to ",
    "→": " to ", "⇒": " to ", "->": " to ", "=>": " to ", "•": ", ", "·": ", ", "–": ", ",
}
_SYMBOL = re.compile("|".join(re.escape(symbol) for symbol in sorted(_SYMBOLS, key=len, reverse=True)))


def speakable(text: str) -> str:
    """``text`` with markdown, links, code marks, bullets and symbols taken out: what remains reads well aloud."""
    lines: list[str] = []
    in_code = False
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if _FENCE.match(raw):
            in_code = not in_code  # code is not read out
        elif not in_code and (line := _spoken_line(raw)):
            lines.append(line)
    spoken = lines[0] if lines else ""
    for line in lines[1:]:
        spoken += (" " if _AFTER_END.search(spoken) else ". ") + line  # a heading or a bullet is a sentence of its own
    return spoken


def _spoken_line(line: str) -> str:
    if _RULE.match(line) or _TABLE_RULE.match(line) or _DEFINITION.match(line):
        return ""
    line = _clean(_in_words(_without_markup(line)))
    return line if any(char.isalnum() for char in line) else ""


def _without_markup(line: str) -> str:
    """Markdown and HTML out; a link keeps its words and loses its address."""
    line = _row(_MARKER.sub("", line, count=1))
    if "&" in line:
        line = html.unescape(line)
    line = _IMAGE.sub("", line)
    line = _LINK.sub(r"\1", line)
    line = _REFERENCE.sub(r"\1", line)
    line = _HTML.sub(" ", line)
    line = _URL.sub(_after_url, line)
    line = _EMAIL.sub(_spoken_email, line)
    line = _FOOTNOTE.sub("", line)
    line = _STRIKE.sub(r"\1", line.replace("`", ""))
    return _UNDERSCORES.sub(r"\2", _STARS.sub(r"\2", line)).replace("_", " ")


def _in_words(line: str) -> str:
    """Dates, times, amounts, abbreviations and symbols written the way they are said."""
    line = _TIMES.sub(r"\1 to ", _RANGE.sub(" to ", line))
    line = _DASH.sub(", ", line)
    line = _ISO_DATE.sub(_spoken_date, line)
    line = _HOUR.sub(lambda found: found[1] + (found[2] or " o'clock"), line)
    line = _OH_MINUTE.sub(r"\1 oh \2", line)
    line = _RUPEES.sub(r"\1 rupees", line)
    for pattern, words in _SPELLED:
        line = pattern.sub(lambda found, words=words: words.capitalize() if found[0][0].isupper() else words, line)
    line = _AT.sub(" at ", line)
    line = _NUMBER_SIGN.sub("number ", line)
    return _SYMBOL.sub(lambda found: _SYMBOLS[found[0]], line)


def _clean(line: str) -> str:
    """Whatever cannot be said out is dropped and the spacing put right."""
    line = _SPACES.sub(" ", _EMPTY_BRACKETS.sub("", _NOT_SPOKEN.sub(" ", line)))
    return _COMMAS.sub(", ", _BEFORE_MARK.sub(r"\1", line)).strip(" ,;:")


def _row(line: str) -> str:
    """A table row as its cells in a row, to be read as one sentence."""
    if "|" not in line or not (line.lstrip().startswith("|") or line.count("|") >= 2):
        return line
    return ", ".join(cell.strip() for cell in line.strip().strip("|").split("|") if cell.strip())


def _after_url(found: re.Match) -> str:
    """A link in running text is left out; what followed it, such as the full stop, stays."""
    url = found[0]
    return url[len(url.rstrip(".,;:!?)]}'\"”’")):]


def _spoken_email(found: re.Match) -> str:
    name, _, host = found[0].partition("@")
    return f"{name.replace('.', ' dot ')} at {host.replace('.', ' dot ')}"


def _ordinal(number: int) -> str:
    suffix = "th" if 10 <= number % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def _spoken_date(found: re.Match) -> str:
    year, month, day = (int(part) for part in found.groups())
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return found[0]
    return f"{_ordinal(day)} of {_MONTHS[month - 1]} {year}"


# --- text: sentences --------------------------------------------------------

_END_MARKS = ".!?…"
# What may follow an end mark and still belong to its sentence: He said "go." (See above.) **Done.**
_CLOSERS = "\"'”’)]}*_~`"
_OPENING_QUOTES = "\"'“‘"
# A full stop after these does not end the sentence, whatever comes next.
_NEVER = frozenset({
    "mr", "mrs", "ms", "mx", "dr", "prof", "st", "mt", "gen", "col", "capt", "lt", "sgt", "rev", "hon", "fr",
    "messrs", "mme", "mlle", "sen", "gov", "pres", "cmdr", "maj", "adm", "vs", "viz", "cf", "approx", "fig",
    "figs", "vol", "pp", "eq", "dept", "ca", "e.g", "i.e",
})
# ...and after these only if the next word starts with a capital: "etc. Then", "No. 5", "Dec. 5", "at 3 p.m. They".
_CAPITAL = frozenset({
    "etc", "inc", "ltd", "co", "corp", "llc", "plc", "jr", "sr", "no", "nos", "jan", "feb", "mar", "apr", "jun",
    "jul", "aug", "sep", "sept", "oct", "nov", "dec",
})
_DOTTED = re.compile(r"(?:[a-z]\.)+[a-z]")  # u.s  a.m  p.m


def sentences(text: str) -> list[str]:
    """``text`` cut into sentences to speak one by one. A line, a heading or a bullet ends a sentence too."""
    return _split(text, final=True)


class SentenceStream:
    """The sentences of a reply that arrives as "the whole text so far", again and again.

    A sentence is handed out once it is certainly over, which is when the next
    one has begun: a full stop at the end of the text so far may still turn
    into "2.5". One stream per text: when the text starts over, make a new one.
    """

    def __init__(self) -> None:
        self._given = 0

    def feed(self, text_so_far: str) -> list[str]:
        """The sentences that are now complete and were not returned before."""
        return self._new(_split(text_so_far, final=False))

    def finish(self, final_text: str) -> list[str]:
        """Whatever is left of the reply, a last sentence without a full stop included."""
        return self._new(_split(final_text, final=True))

    def _new(self, found: list[str]) -> list[str]:
        fresh = found[self._given:]
        self._given += len(fresh)
        return fresh


def _split(text: str, final: bool) -> list[str]:
    """The sentences of ``text``. Unless ``final``, only those that are certainly over."""
    found: list[str] = []
    in_code = False
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for number, line in enumerate(lines):
        if _FENCE.match(line):
            in_code = not in_code
        elif not in_code:
            ended = final or number < len(lines) - 1  # only the last line can still be growing
            found.extend(_scan(line.strip() if ended else line.lstrip(), ended))
    return [sentence for sentence in map(_tidy, found) if sentence]


def _tidy(sentence: str) -> str:
    sentence = " ".join(sentence.split())
    return sentence if any(char.isalnum() for char in sentence) else ""


def _scan(line: str, ended: bool) -> list[str]:
    """The sentences of one line that are certainly over; with ``ended`` the line's rest is one too."""
    found: list[str] = []
    begin = 0
    pos = _MARKER.match(line).end()  # "1." at the start of a line numbers a list item
    size = len(line)
    while pos < size:
        if line[pos] not in _END_MARKS:
            pos += 1
            continue
        stop = pos
        while stop < size and line[stop] in _END_MARKS:
            stop += 1
        close = stop
        while close < size and line[close] in _CLOSERS:
            close += 1
        if close == size:
            break  # the text ends on the mark; "2." may still become "2.5"
        if not line[close].isspace():
            pos = close  # "2.5", "e.g.", "notes.md"
            continue
        over = _is_over(line, pos, stop, close)
        if over is None:
            break  # cannot tell until the next word is here
        if over:
            found.append(line[begin:close])
            begin = close
            while begin < size and line[begin].isspace():
                begin += 1
        pos = max(pos, close)
    if ended and line[begin:].strip():
        found.append(line[begin:])
    return found


def _is_over(line: str, pos: int, stop: int, close: int) -> bool | None:
    """Does the end mark at ``line[pos:stop]``, with a space after it, end the sentence? None: not yet known."""
    strong = all(mark in "!?" for mark in line[pos:stop])
    word = _word_before(line, pos)
    if not strong and (word in _NEVER or (len(word) == 1 and word.isalpha())):  # Mr. Rao, S. Priya Raman
        return False
    following = line[close:].lstrip()
    if not following:
        return None
    if strong:
        return True
    if following[0].islower():
        return False  # "approx. five", "vs. last year", "well... I think"
    if word in _CAPITAL or _DOTTED.fullmatch(word):
        return following[0].isupper() or following[0] in _OPENING_QUOTES
    return True


def _word_before(line: str, pos: int) -> str:
    start = pos
    while start > 0 and not line[start - 1].isspace():
        start -= 1
    return line[start:pos].lower().lstrip("([{\"'“‘*_`~")
