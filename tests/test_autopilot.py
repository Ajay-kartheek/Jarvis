from __future__ import annotations

import asyncio

from jarvis.runtime import Autopilot

GRANTED = {"microphone": "granted", "system_audio": "granted"}
TEAMS = "com.microsoft.teams2"


class FakeMeetings:
    def __init__(self) -> None:
        self.running = False
        self.starts: list[tuple] = []
        self.stops = 0
        self.fail = False  # a start that dies at once, as when Claude cannot be reached

    async def start(self, project=None, headphones=None) -> None:
        self.starts.append((project, headphones))
        self.running = not self.fail

    async def stop(self) -> None:
        self.running = False
        self.stops += 1


class FakeRuntime:
    def __init__(self) -> None:
        self.meeting = FakeMeetings()
        self.notices: dict[str, str] = {}
        self.published = 0

    def publish_state(self) -> None:
        self.published += 1

    def spawn(self, work):
        return asyncio.ensure_future(work)

    def notify(self, key: str, text: str) -> None:
        self.notices[key] = text

    def clear(self, key: str) -> None:
        self.notices.pop(key, None)


def pilot() -> tuple[Autopilot, FakeRuntime]:
    runtime = FakeRuntime()
    return Autopilot(runtime), runtime


def report(autopilot: Autopilot, active: bool, *, headphones: bool = False, permissions: dict | None = None) -> None:
    asyncio.run(autopilot.report(active, TEAMS if active else None, headphones, permissions or GRANTED))


def test_a_call_starts_listening_and_its_end_stops_it():
    autopilot, runtime = pilot()
    report(autopilot, False)
    assert runtime.meeting.starts == []

    report(autopilot, True)
    report(autopilot, True)  # the app repeats itself every two seconds
    assert runtime.meeting.starts == [(None, None)]
    assert autopilot.snapshot()["call"] == {"active": True, "app": TEAMS}

    report(autopilot, False)
    assert runtime.meeting.stops == 1 and not runtime.meeting.running


def test_stopping_by_hand_holds_until_the_next_call():
    autopilot, runtime = pilot()
    report(autopilot, True)
    autopilot.user_stopped()
    runtime.meeting.running = False
    report(autopilot, True)
    assert len(runtime.meeting.starts) == 1  # same call: stay stopped

    report(autopilot, False)
    report(autopilot, True)  # a new call
    assert len(runtime.meeting.starts) == 2


def test_a_meeting_started_by_hand_is_not_stopped_by_the_end_of_a_call():
    autopilot, runtime = pilot()
    runtime.meeting.running = True  # the user pressed Start before any call
    autopilot.user_started()
    report(autopilot, True)
    report(autopilot, False)
    assert runtime.meeting.starts == [] and runtime.meeting.stops == 0


def test_nothing_starts_while_automatic_listening_is_off():
    autopilot, runtime = pilot()
    autopilot.enabled = False
    report(autopilot, True)
    assert runtime.meeting.starts == []


def test_a_denied_permission_is_explained_instead_of_starting():
    autopilot, runtime = pilot()
    report(autopilot, True, permissions={"microphone": "denied", "system_audio": "granted"})
    assert runtime.meeting.starts == []
    assert "Microphone" in runtime.notices["permissions"]

    report(autopilot, True)  # switched on in System Settings
    assert "permissions" not in runtime.notices
    assert len(runtime.meeting.starts) == 1


def test_a_failed_start_is_tried_again_a_few_times_only():
    autopilot, runtime = pilot()
    runtime.meeting.fail = True
    autopilot.RETRY_SECONDS = 0
    for _ in range(10):
        report(autopilot, True)
    assert len(runtime.meeting.starts) == Autopilot.MAX_ATTEMPTS


def test_headphones_follow_the_latest_report():
    autopilot, _ = pilot()
    report(autopilot, True, headphones=False)
    assert autopilot.headphones_now() is False
    report(autopilot, True, headphones=True)  # plugged in during the call
    assert autopilot.headphones_now() is True
