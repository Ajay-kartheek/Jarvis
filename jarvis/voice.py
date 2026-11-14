"""Hearing the user: one spoken message per hold of the key.

The app sends the microphone over a WebSocket while the key is held. The audio
is recognised on this Mac as it arrives, so the words appear while the user is
still talking; when the key is released the transcript is finished and handed
on as a message. No audio is kept.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from jarvis.runtime import Runtime

SAMPLE_RATE = 16_000
MIN_SECONDS = 0.35  # a shorter hold is a slip of the finger
MAX_SECONDS = 180
LEVEL_EVERY_SECONDS = 0.08


class Utterance:
    """The words of one spoken message, recognised as the audio arrives."""

    def __init__(self, model, boosted=None) -> None:
        from jarvis.listen import WindowText

        self._window = WindowText(model, live=True, boosted=boosted)
        self._done: list[str] = []

    def add(self, samples: np.ndarray) -> None:
        self._window.add(samples)

    def step(self) -> str:
        """Recognise again if enough new audio came in. Returns the words so far."""
        self._done.extend(self._window.step())
        return self.text

    def finish(self) -> str:
        self._done.extend(self._window.step(pause=True))
        return " ".join(self._done).strip()

    @property
    def text(self) -> str:
        return " ".join([*self._done, self._window.partial]).strip()


def _ready(config) -> tuple:
    """The recogniser and the setting that leans towards the user's words. Blocking: run it off the event loop."""
    from jarvis import speech, vocabulary
    from jarvis.store import open_store

    model = speech.acquire()
    try:
        with open_store() as store:
            known = vocabulary.words(store, config)
        return model, speech.boosted(known)
    except Exception:  # the plain recogniser is enough to hear a message
        return model, None


async def hear(websocket, runtime: "Runtime") -> None:
    """Receive one utterance on ``websocket`` and pass it on as a spoken message.

    Jarvis's own ear is closed meanwhile: the microphone is the held key's.
    """
    runtime.ear.pause("message")
    try:
        await _hear(websocket, runtime)
    finally:
        runtime.ear.resume("message")


async def _hear(websocket, runtime: "Runtime") -> None:
    from jarvis import speech

    hub = runtime.hub
    hub.publish({"type": "voice", "phase": "listening"})
    runtime.warm_voice()
    loading = asyncio.create_task(asyncio.to_thread(_ready, runtime.config))
    utterance: Utterance | None = None
    waiting: list[np.ndarray] = []  # audio that arrived before the model was ready
    samples_heard = 0
    cancelled = False
    shown = ""
    last_level = 0.0
    try:
        while samples_heard < MAX_SECONDS * SAMPLE_RATE:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message.get("text"):
                try:
                    cancelled = bool(json.loads(message["text"]).get("cancel"))
                except (ValueError, AttributeError):
                    cancelled = False
                if cancelled:
                    break
                continue
            data = message.get("bytes")
            if not data:
                continue
            chunk = np.frombuffer(data[: len(data) - len(data) % 4], dtype="<f4")
            if not len(chunk):
                continue
            samples_heard += len(chunk)
            now = time.monotonic()
            if now - last_level >= LEVEL_EVERY_SECONDS:
                last_level = now
                loudness = float(np.sqrt(np.mean(np.square(chunk, dtype=np.float64))))
                hub.publish({"type": "level", "source": "voice", "level": round(min(1.0, loudness * 12), 3)})
            if utterance is None and loading.done():
                utterance = Utterance(*loading.result())
                for early in waiting:
                    utterance.add(early)
                waiting = []
            if utterance is None:
                waiting.append(chunk)
                continue
            utterance.add(chunk)
            words = await asyncio.to_thread(utterance.step)
            if words != shown:
                shown = words
                hub.publish({"type": "voice", "phase": "partial", "text": words})
    finally:
        final = ""
        try:
            ready = await loading
            if not cancelled and samples_heard >= MIN_SECONDS * SAMPLE_RATE:
                if utterance is None:
                    utterance = Utterance(*ready)
                    for early in waiting:
                        utterance.add(early)
                final = await asyncio.to_thread(utterance.finish)
        except Exception as exc:  # say why nothing came of it instead of leaving the orb listening
            hub.publish({"type": "info", "text": f"Jarvis could not hear that: {exc}"})
        finally:
            speech.release()
        if final:
            hub.publish({"type": "voice", "phase": "heard", "text": final})
            runtime.spawn(runtime.talk(final, spoken=True))
        else:
            hub.publish({"type": "voice", "phase": "cancelled"})
