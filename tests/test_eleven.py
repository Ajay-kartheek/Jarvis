"""The cloud voice: each sentence sent with what came before it, and the voice on this Mac whenever it cannot speak."""

import json

import httpx
import pytest

from jarvis import eleven, speak


@pytest.fixture
def cloud(monkeypatch):
    """ElevenLabs as a stand-in: answers come from ``cloud.answer``, requests are kept in ``cloud.asked``."""

    class Cloud:
        asked: list[httpx.Request] = []
        answer = staticmethod(lambda request: httpx.Response(200, content=b"ID3 mp3 bytes"))

    def handle(request: httpx.Request) -> httpx.Response:
        Cloud.asked.append(request)
        return Cloud.answer(request)

    monkeypatch.setattr(eleven, "_key", lambda: "sk_test")
    monkeypatch.setattr(eleven, "_client", httpx.Client(base_url=eleven.API, transport=httpx.MockTransport(handle)))
    monkeypatch.setattr(eleven.time, "sleep", lambda seconds: None)
    Cloud.asked = []
    return Cloud


def test_a_sentence_goes_with_what_was_said_before_it(cloud):
    assert eleven.synthesize("Shall I send the invite?", "Right, I've checked your calendar. " * 30) == b"ID3 mp3 bytes"
    [request] = cloud.asked
    body = json.loads(request.content)
    assert request.url.path == f"/v1/text-to-speech/{eleven.VOICE}" and request.url.params["output_format"] == eleven.FORMAT
    assert request.headers["xi-api-key"] == "sk_test"
    assert body["text"] == "Shall I send the invite?" and body["model_id"] == eleven.MODEL
    assert len(body["previous_text"]) <= eleven.CONTEXT_CHARS and body["previous_text"].endswith("calendar.")


def test_the_first_sentence_has_nothing_before_it(cloud):
    eleven.synthesize("Good morning.")
    assert "previous_text" not in json.loads(cloud.asked[0].content)


def test_used_up_credits_rest_the_cloud_voice_and_the_mac_speaks(cloud, monkeypatch):
    cloud.answer = staticmethod(lambda request: httpx.Response(
        401, json={"detail": {"status": "quota_exceeded", "message": "This request exceeds your quota."}}))
    monkeypatch.setattr(speak, "local_engine", lambda: "system")
    monkeypatch.setattr(speak, "_system_wav", lambda text: b"RIFF local")
    assert speak.engine() == "elevenlabs"
    assert speak.synthesize("Good morning.") == b"RIFF local"
    assert not eleven.available() and "credits are used up" in eleven.trouble()
    assert speak.engine() == "system"  # resting: the next sentences do not even try
    speak.synthesize("And another thing.")
    assert len(cloud.asked) == 1


def test_a_busy_service_is_asked_once_more(cloud):
    answers = iter([httpx.Response(429, json={"detail": {"status": "too_many_concurrent_requests"}}),
                    httpx.Response(200, content=b"ID3 second time")])
    cloud.answer = staticmethod(lambda request: next(answers))
    assert eleven.synthesize("Hello.") == b"ID3 second time"
    assert eleven.available()


def test_an_unreachable_service_rests_only_briefly(cloud, monkeypatch):
    def fail(request):
        raise httpx.ConnectError("no network")

    cloud.answer = staticmethod(fail)
    with pytest.raises(eleven.Unavailable, match="could not be reached"):
        eleven.synthesize("Hello.")
    assert not eleven.available()
    monkeypatch.setattr(eleven.time, "monotonic", lambda: eleven._resting_until + 1)
    assert eleven.available()


def test_without_a_key_there_is_no_cloud_voice():
    assert not eleven.available() and speak.engine() != "elevenlabs"
    with pytest.raises(eleven.Unavailable):
        eleven.synthesize("Hello.")


def test_the_credits_left_are_read_and_kept_a_while(cloud):
    cloud.answer = staticmethod(lambda request: httpx.Response(
        200, json={"character_count": 5726, "character_limit": 10000, "next_character_count_reset_unix": 1765500000}))
    assert eleven.credits() == {"used": 5726, "limit": 10000, "resets": 1765500000}
    eleven.credits()
    assert len(cloud.asked) == 1 and cloud.asked[0].url.path == "/v1/user/subscription"
