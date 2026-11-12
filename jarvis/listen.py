"""Live transcript: JarvisEars audio -> on-device speech-to-text.

``jarvis listen`` prints one line per finished sentence, labelled Them (system
audio) or You (microphone), and saves the same lines to a transcript file.
Audio is recognised on this Mac and is never stored.

How it recognises speech: the model is accurate when it sees a whole stretch of
speech at once and poor when fed quarter-second pieces. So each source keeps
the audio of the sentence in progress and decodes that whole window again every
half second. Sentences that have settled are emitted and cut off the window.
"""

from __future__ import annotations

import argparse
import collections
import json
import socket
import struct
import subprocess
import sys
import threading
import time
import wave
from datetime import datetime
from pathlib import Path

import mlx.core as mx
import numpy as np
from parakeet_mlx import from_pretrained
from parakeet_mlx.audio import get_logmel

MODEL = "mlx-community/parakeet-tdt-0.6b-v2"
SAMPLE_RATE = 16_000
APP = Path(__file__).resolve().parent.parent / "ears" / "build" / "JarvisEars.app"
HEADER = struct.Struct("<2sBBI")
SOURCES = {0: "system", 1: "mic"}
STATUS = 2
LABELS = {"system": "Them", "mic": "You"}
SENTENCE_END = (".", "?", "!")

STEP_SECONDS = 0.5  # new audio needed before the window is decoded again
SETTLE_SECONDS = 1.5  # a sentence must have ended this long ago to be final
MAX_WINDOW_SECONDS = 15.0  # cut an unbroken stretch of speech at this length
MIN_WINDOW_SECONDS = 0.3


class HelperAudio:
    """Starts JarvisEars and collects the audio it sends, per source."""

    def __init__(self) -> None:
        self.pending: dict[str, collections.deque] = {
            name: collections.deque() for name in SOURCES.values()
        }
        self.alive = True
        server = socket.create_server(("127.0.0.1", 0))
        port = server.getsockname()[1]
        subprocess.run(["open", "-n", str(APP), "--args", "--port", str(port)], check=True)
        server.settimeout(20)
        self.conn, _ = server.accept()
        threading.Thread(target=self._read, daemon=True).start()

    def _receive(self, count: int) -> bytes | None:
        chunks = []
        while count:
            chunk = self.conn.recv(count)
            if not chunk:
                return None
            chunks.append(chunk)
            count -= len(chunk)
        return b"".join(chunks)

    def _read(self) -> None:
        try:
            while True:
                header = self._receive(HEADER.size)
                if header is None:
                    break
                magic, kind, _, length = HEADER.unpack(header)
                payload = self._receive(length) if length else b""
                if magic != b"JE" or payload is None:
                    break
                if kind == STATUS:
                    event = json.loads(payload)
                    if event.get("event") != "level":
                        print("status:", json.dumps(event), file=sys.stderr, flush=True)
                elif kind in SOURCES:
                    self.pending[SOURCES[kind]].append(np.frombuffer(payload, dtype="<f4"))
        except OSError:
            pass
        self.alive = False

    def poll(self, source: str) -> list[np.ndarray]:
        queue = self.pending[source]
        return [queue.popleft() for _ in range(len(queue))]

    def close(self) -> None:
        self.conn.close()


class WavAudio:
    """Replays a WAV file as the system stream, a slice per poll."""

    def __init__(self, path: Path, step_seconds: float) -> None:
        with wave.open(str(path), "rb") as wav:
            if wav.getframerate() != SAMPLE_RATE or wav.getnchannels() != 1:
                raise SystemExit("--wav needs a 16 kHz mono file")
            raw = wav.readframes(wav.getnframes())
        self.samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        self.step = int(step_seconds * SAMPLE_RATE)
        self.position = 0

    @property
    def alive(self) -> bool:
        return self.position < len(self.samples)

    def poll(self, source: str) -> list[np.ndarray]:
        if source != "system" or not self.alive:
            return []
        chunk = self.samples[self.position : self.position + self.step]
        self.position += self.step
        return [chunk]

    def close(self) -> None:
        pass


class Gate:
    """Lets audio through while someone is speaking, plus a little before and after."""

    def __init__(self, floor: float, hangover: float = 0.9, preroll: float = 0.4) -> None:
        self.floor = floor
        self.noise = floor
        self.hangover = hangover
        self.preroll_samples = int(preroll * SAMPLE_RATE)
        self.held: collections.deque = collections.deque()
        self.held_samples = 0
        self.clock = 0.0
        self.open_until = -1.0
        self.is_open = False
        self.just_closed = False

    def filter(self, chunk: np.ndarray) -> np.ndarray | None:
        self.just_closed = False
        if len(chunk) == 0:
            return None
        level = float(np.sqrt(np.mean(np.square(chunk, dtype=np.float64))))
        self.clock += len(chunk) / SAMPLE_RATE
        # Follow the background level down quickly and up slowly.
        self.noise = level if level < self.noise else self.noise * 0.999 + level * 0.001
        if level > max(self.floor, self.noise * 4):
            self.open_until = self.clock + self.hangover
        if self.clock <= self.open_until:
            self.is_open = True
            if self.held:
                chunk = np.concatenate([*self.held, chunk])
                self.held.clear()
                self.held_samples = 0
            return chunk
        if self.is_open:
            self.is_open = False
            self.just_closed = True
        self.held.append(chunk)
        self.held_samples += len(chunk)
        while self.held and self.held_samples - len(self.held[0]) >= self.preroll_samples:
            self.held_samples -= len(self.held.popleft())
        return None


class WindowText:
    """Keeps the audio of the sentence in progress and decodes it as a whole.

    `live` sources are decoded every half second, so `partial` tracks what is
    being said right now. Other sources are decoded only when the speaker
    pauses, which is enough for the written record and costs far less.
    """

    def __init__(self, model, *, live: bool) -> None:
        self.model = model
        self.live = live
        self.window = np.zeros(0, dtype=np.float32)
        self.fresh = 0  # samples added since the last decode
        self.partial = ""
        self.decodes = 0
        self.decode_seconds = 0.0
        self.slowest = 0.0
        self.audio_seconds = 0.0

    def add(self, samples: np.ndarray) -> None:
        self.window = np.concatenate([self.window, samples])
        self.fresh += len(samples)
        self.audio_seconds += len(samples) / SAMPLE_RATE

    def step(self, *, pause: bool = False) -> list[str]:
        """Returns the sentences that are now final. `pause` means the speaker stopped."""
        seconds = len(self.window) / SAMPLE_RATE
        if seconds < MIN_WINDOW_SECONDS:
            if pause:
                self._reset()
            return []
        too_long = seconds > MAX_WINDOW_SECONDS
        due = self.live and self.fresh >= STEP_SECONDS * SAMPLE_RATE
        if not (pause or too_long or due):
            return []

        tokens = self._decode()
        if pause:
            self._reset()
            return self._sentences(tokens)

        # A sentence is final once it ended a while ago and the next word has begun.
        horizon = seconds - SETTLE_SECONDS
        cut = None
        for index in range(len(tokens) - 1):
            if tokens[index].end > horizon:
                break
            if self._ends_sentence(tokens[index]) and tokens[index + 1].text.startswith(" "):
                cut = index
        if cut is None and too_long:
            # No sentence break in a long stretch: cut at the last settled word.
            for index in range(len(tokens) - 1):
                if tokens[index].end > horizon:
                    break
                if tokens[index + 1].text.startswith(" "):
                    cut = index

        finished: list[str] = []
        if cut is not None:
            finished = self._sentences(tokens[: cut + 1])
            keep_from = (tokens[cut].end + tokens[cut + 1].start) / 2
            self.window = self.window[int(max(0.0, keep_from - 0.1) * SAMPLE_RATE) :]
            tokens = tokens[cut + 1 :]
        self.partial = self._join(tokens)
        return finished

    def _decode(self) -> list:
        started = time.perf_counter()
        # The feature code only handles float32 input.
        mel = get_logmel(mx.array(self.window), self.model.preprocessor_config)
        tokens = list(self.model.generate(mel)[0].tokens)
        took = time.perf_counter() - started
        self.decodes += 1
        self.decode_seconds += took
        self.slowest = max(self.slowest, took)
        self.fresh = 0
        return tokens

    def _reset(self) -> None:
        self.window = np.zeros(0, dtype=np.float32)
        self.fresh = 0
        self.partial = ""

    def _sentences(self, tokens) -> list[str]:
        sentences, current = [], []
        for index, token in enumerate(tokens):
            current.append(token)
            last = index == len(tokens) - 1
            if self._ends_sentence(token) and (last or tokens[index + 1].text.startswith(" ")):
                sentences.append(self._join(current))
                current = []
        if current:
            sentences.append(self._join(current))
        return [sentence for sentence in sentences if sentence]

    @staticmethod
    def _ends_sentence(token) -> bool:
        return token.text.rstrip().endswith(SENTENCE_END)

    @staticmethod
    def _join(tokens) -> str:
        return "".join(token.text for token in tokens).strip()


def run(args) -> int:
    """Listen until ``args.seconds`` pass (or Ctrl-C when it is None).

    ``args`` carries: seconds, wav, say, say_after, partials, out (a path for
    the transcript, or None).
    """
    started_loading = time.perf_counter()
    model = from_pretrained(MODEL)
    model.generate(get_logmel(mx.zeros(SAMPLE_RATE), model.preprocessor_config))  # warm up
    load_seconds = time.perf_counter() - started_loading

    poll_seconds = 0.1
    sources = ["system"] if args.wav else ["system", "mic"]
    texts = {source: WindowText(model, live=source == "system") for source in sources}
    gates = {"system": Gate(floor=0.0008), "mic": Gate(floor=0.006)}
    audio = WavAudio(args.wav, poll_seconds) if args.wav else HelperAudio()

    echo_guard = getattr(args, "echo_guard", True)
    started = time.monotonic()
    spoken = False
    shown_partial = dict.fromkeys(sources, "")

    def emit(source: str, text: str) -> None:
        elapsed = audio.position / SAMPLE_RATE if args.wav else time.monotonic() - started
        print(f"[{int(elapsed // 60):02d}:{int(elapsed % 60):02d}] {LABELS[source]}: {text}", flush=True)
        if args.out:
            record = {"at": datetime.now().astimezone().isoformat(timespec="seconds"),
                      "speaker": LABELS[source], "text": text}
            with open(args.out, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    try:
        while audio.alive and (args.wav or args.seconds is None or time.monotonic() - started < args.seconds):
            if not args.wav:
                time.sleep(poll_seconds)
                if args.say and not spoken and time.monotonic() - started >= args.say_after:
                    subprocess.Popen(["say", args.say])
                    spoken = True
            for source in sources:
                gate, text = gates[source], texts[source]
                paused = False
                for chunk in audio.poll(source):
                    if source == "mic" and echo_guard and gates["system"].is_open:
                        # While the other side is talking, the microphone mostly
                        # hears the speakers, which would come out as garbled "You" lines.
                        chunk = np.zeros_like(chunk)
                    kept = gate.filter(chunk)
                    paused = paused or gate.just_closed
                    if kept is not None:
                        text.add(kept)
                for sentence in text.step(pause=paused):
                    emit(source, sentence)
                if args.partials and text.partial != shown_partial[source]:
                    shown_partial[source] = text.partial
                    if text.partial:
                        print(f"        {LABELS[source]} ... {text.partial}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        audio.close()

    for source in sources:
        for sentence in texts[source].step(pause=True):
            emit(source, sentence)

    stats = {"model_load_seconds": round(load_seconds, 1)}
    for source, text in texts.items():
        stats[source] = {
            "speech_seconds": round(text.audio_seconds, 1),
            "decodes": text.decodes,
            "mean_decode_ms": round(1000 * text.decode_seconds / text.decodes) if text.decodes else None,
            "slowest_decode_ms": round(1000 * text.slowest),
            "compute_share_of_speech": round(text.decode_seconds / text.audio_seconds, 2)
            if text.audio_seconds
            else None,
        }
    print(json.dumps(stats, indent=2))
    return 0

