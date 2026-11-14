"""Jarvis's voice from ElevenLabs: a sentence in, the bytes of an MP3 out.

Used whenever an ElevenLabs key is in the Keychain (``jarvis key elevenlabs``). What Jarvis says aloud goes to
ElevenLabs to be spoken, and nothing else does. Each sentence is sent with the ones before it in the same reply
(``previous_text``), so the voice carries the tune from one sentence to the next instead of starting every one afresh.

When the key is missing, the credits are used up, the key is refused or the service cannot be reached, ``synthesize``
raises ``Unavailable`` and the voice on this Mac speaks instead (see ``speak.synthesize``). After a refusal the cloud
voice rests for a while, so a reply does not wait on a request that is bound to fail.
"""

from __future__ import annotations

import logging
import threading
import time

import httpx

from jarvis import keychain

log = logging.getLogger("jarvis")

API = "https://api.elevenlabs.io/v1"
VOICE = "JBFqnCBsd6RMkjVDRZzb"  # George: a warm British man (one of ElevenLabs' own voices, offered until 31 Dec 2026)
MODEL = "eleven_flash_v2_5"  # the fastest: about a third of a second to the first sound, and half the credits
FORMAT = "mp3_44100_128"  # the best a free account may ask for
CONTEXT_CHARS = 400  # how much of what came before is sent along with a sentence
TIMEOUT = httpx.Timeout(12.0, connect=5.0)
AT_ONCE = 2  # requests in flight at most: a free account is allowed two
REST_REFUSED = 30 * 60  # credits used up or key refused: not tried again for this long
REST_UNREACHABLE = 60  # the service could not be reached
CREDITS_FRESH = 10 * 60  # how long a reading of the credits left is trusted

_lock = threading.Lock()
_slots = threading.BoundedSemaphore(AT_ONCE)
_client: httpx.Client | None = None
_resting_until = 0.0
_trouble = ""  # why the cloud voice is resting, in words the user can be shown
_credits: tuple[float, dict] | None = None


class Unavailable(RuntimeError):
    """The cloud voice cannot speak now. The message is safe to show the user."""


def _key() -> str | None:
    try:
        return keychain.load_key("elevenlabs")
    except Exception:  # no Keychain to read (a test machine, say): no cloud voice
        return None


def configured() -> bool:
    """Whether the user has given Jarvis an ElevenLabs key."""
    return bool(_key())


def available() -> bool:
    """Whether the cloud voice should be asked now: there is a key, and it is not resting after a refusal."""
    return time.monotonic() >= _resting_until and configured()


def trouble() -> str:
    """Why the cloud voice is resting, or "" when it is not."""
    return _trouble if time.monotonic() < _resting_until else ""


def _session() -> httpx.Client:
    """One connection kept open, so a sentence does not pay for a new handshake."""
    global _client
    with _lock:
        if _client is None:
            _client = httpx.Client(base_url=API, timeout=TIMEOUT, http2=False)
        return _client


def warm() -> None:
    """Open the connection ahead of the first sentence. Costs no credits."""
    if not available():
        return
    try:
        _session().head("/models")
    except httpx.HTTPError:
        pass


def _rest(seconds: float, why: str) -> None:
    global _resting_until, _trouble
    _resting_until = time.monotonic() + seconds
    _trouble = why
    log.warning("the ElevenLabs voice rests for %d s: %s", seconds, why)


def _refusal(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail") or {}
    except (ValueError, AttributeError):
        detail = {}
    if not isinstance(detail, dict):
        detail = {"message": str(detail)}
    status = str(detail.get("status") or detail.get("code") or "")
    message = str(detail.get("message") or "")
    if status == "quota_exceeded" or "quota" in message.lower() or "credits" in message.lower():
        return "the ElevenLabs credits are used up for this month"
    if response.status_code == 401:
        return "ElevenLabs did not accept the key"
    if response.status_code == 402:
        return "ElevenLabs wants a paid plan for this voice"
    return f"ElevenLabs refused ({response.status_code}{': ' + message[:120] if message else ''})"


def synthesize(text: str, previous: str = "") -> bytes:
    """``text`` spoken by the cloud voice, as MP3 bytes. Blocks; any number of threads may call it.

    ``previous`` is what was said just before in the same reply, sent only as context: it is not spoken again.
    """
    global _credits
    key = _key()
    if not key:
        raise Unavailable("there is no ElevenLabs key")
    if not available():
        raise Unavailable(trouble() or "the ElevenLabs voice is resting")
    body: dict = {"text": text, "model_id": MODEL}
    if previous.strip():
        body["previous_text"] = previous.strip()[-CONTEXT_CHARS:]
    for attempt in range(2):
        try:
            with _slots:
                response = _session().post(
                    f"/text-to-speech/{VOICE}", params={"output_format": FORMAT}, json=body,
                    headers={"xi-api-key": key, "accept": "audio/mpeg"},
                )
        except httpx.HTTPError as exc:
            _rest(REST_UNREACHABLE, "ElevenLabs could not be reached")
            raise Unavailable(f"ElevenLabs could not be reached: {type(exc).__name__}") from None
        if response.status_code == 200 and response.content:
            _credits = None  # what is left has changed
            return response.content
        if response.status_code == 429 and attempt == 0:  # too many at once, or the service is busy: once more
            time.sleep(0.4)
            continue
        why = _refusal(response)
        if response.status_code in (401, 402) or "credits" in why:
            _rest(REST_REFUSED, why)
        raise Unavailable(why)
    raise Unavailable("ElevenLabs is busy")


def credits() -> dict | None:
    """What is left of this month's credits: {"used", "limit", "resets"} (resets: a Unix time), or None if not known.
    Read from ElevenLabs at most every few minutes. Blocks; costs no credits."""
    global _credits
    if _credits and time.monotonic() - _credits[0] < CREDITS_FRESH:
        return _credits[1]
    key = _key()
    if not key:
        return None
    try:
        response = _session().get("/user/subscription", headers={"xi-api-key": key})
        data = response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        data = None
    if not data:
        return None
    reading = {
        "used": int(data.get("character_count") or 0),
        "limit": int(data.get("character_limit") or 0),
        "resets": int(data.get("next_character_count_reset_unix") or 0),
    }
    _credits = (time.monotonic(), reading)
    return reading
