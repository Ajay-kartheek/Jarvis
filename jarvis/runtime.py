"""What the Jarvis daemon keeps running between requests.

- the meeting in progress, if any;
- the autopilot, which starts and stops listening from what the Jarvis app
  reports about calls, so nobody has to press Start;
- the assistant behind the Ask box;
- background jobs: keeping the local copy fresh, the morning brief, meeting
  notes after a call, and clearing old transcripts.

Everything the interface shows comes out of :meth:`Runtime.state` and the
events published on the hub.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Coroutine
from datetime import datetime
from pathlib import Path

from jarvis import paths
from jarvis import projects as project_registry
from jarvis.config import Config
from jarvis.store import open_store

log = logging.getLogger("jarvis")

SYNC_EVERY_SECONDS = 600
SYNC_FRESH_FOR_BRIEF_SECONDS = 900
FIRST_SYNC_AFTER_SECONDS = 15
DAILY_CHECK_SECONDS = 300
BRIEF_FROM_HOUR = 6
APP_SILENT_AFTER_SECONDS = 10
ENGAGEMENTS_CACHE_SECONDS = 30
TEST_WAV_ENV = "JARVIS_TEST_WAV"  # replay a recording as the call, for tests
RECOVER_NOTES_WITHIN_SECONDS = 86_400


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Hub:
    """Fans events out to every connected interface."""

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue] = set()

    def join(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._queues.add(queue)
        return queue

    def leave(self, queue: asyncio.Queue) -> None:
        self._queues.discard(queue)

    def publish(self, event: dict | None) -> None:
        for queue in self._queues:
            if queue.full():  # a stalled window must not hold up the meeting
                queue.get_nowait()
            queue.put_nowait(event)

    def close(self) -> None:
        """Tell every connected interface's sender to finish."""
        self.publish(None)


class MeetingController:
    def __init__(self, runtime: "Runtime") -> None:
        self.runtime = runtime
        self.status = "idle"
        self.meeting: dict | None = None
        self._task: asyncio.Task | None = None
        self._inbox: asyncio.Queue | None = None
        self._out: Path | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _set(self, status: str) -> None:
        self.status = status
        self.runtime.publish_state()

    def _on_event(self, event: dict) -> None:
        kind = event["type"]
        if kind == "meeting":
            if self.meeting is None:
                self.meeting = {
                    "title": event["title"], "engagement": event["engagement"], "started_at": now_iso(),
                }
                self._set("preparing")
            else:  # the engagement became clear during the call
                self.meeting = {**self.meeting, "engagement": event["engagement"]}
                self.runtime.publish_state()
            if self._out is not None:
                write_meta(self._out, self.meeting)
                if event["engagement"]:
                    self.runtime.hub.publish({"type": "info", "text": f"This call is about {event['engagement']}."})
            return
        if kind == "ready":
            self._set("listening")
            return
        self.runtime.hub.publish(event)
        if kind == "thinking":
            self._set("thinking")
        elif kind in ("suggestion", "no_suggestion"):
            self._set("listening")

    async def start(self, project: str | None = None, headphones: bool | None = None) -> None:
        """Start listening. ``headphones`` None means: go by what the app reports."""
        if self.running:
            return
        folder = paths.ensure_data_dir() / "transcripts"
        folder.mkdir(exist_ok=True)
        out = folder / f"{datetime.now():%Y-%m-%d-%H%M%S}.jsonl"
        self._inbox = asyncio.Queue()
        self._out = out
        self.status = "preparing"
        self._task = asyncio.create_task(self._run(project, headphones, out))
        self.runtime.publish_state()

    async def _run(self, project: str | None, headphones: bool | None, out: Path) -> None:
        from jarvis import meeting as live

        wav = os.environ.get(TEST_WAV_ENV)
        try:
            await live.run(
                self.runtime.config, project_name=project, out=out,
                headphones=self.runtime.autopilot.headphones_now if headphones is None else headphones,
                emit=self._on_event, inbox=self._inbox, wav=Path(wav) if wav else None,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # show the reason in the interface instead of dying silently
            log.warning("meeting stopped: %s", exc)
            self.runtime.hub.publish({"type": "info", "text": f"The meeting stopped: {exc}"})
        finally:
            self.meeting = None
            self._inbox = None
            self._out = None
            self._set("idle")
            self.runtime.spawn(self.runtime.wrap_up(out))

    async def stop(self) -> None:
        if self.running:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    def suggest(self) -> bool:
        if not self.running or self._inbox is None:
            return False
        self._inbox.put_nowait(("command", "suggest", None))
        return True


class Autopilot:
    """Starts and stops listening from the app's reports about calls.

    A call that starts while automatic listening is on starts a meeting; the
    end of that call stops it. If the user stops a meeting by hand, it stays
    stopped until the next call. A meeting the user started by hand belongs to
    the call too if it is running when the call begins or was started during
    it; one started with no call around is left for the user to stop.
    """

    RETRY_SECONDS = 30
    MAX_ATTEMPTS = 3
    STOP_WAIT_SECONDS = 1.5  # the app expects an answer to each report within three seconds

    def __init__(self, runtime: "Runtime", enabled: bool = True) -> None:
        self.runtime = runtime
        self.enabled = enabled
        self.call_active = False
        self.call_app: str | None = None
        self.headphones = False
        self.permissions: dict[str, str] = {}
        self.last_report = 0.0
        self._mine = False  # the running meeting belongs to the current call
        self._dismissed = False  # the user stopped it during this call
        self._attempts = 0
        self._last_attempt = 0.0

    def headphones_now(self) -> bool:
        return self.headphones

    @property
    def app_connected(self) -> bool:
        return time.monotonic() - self.last_report < APP_SILENT_AFTER_SECONDS

    def snapshot(self) -> dict:
        return {
            "auto": self.enabled, "app": self.app_connected, "headphones": self.headphones,
            "call": {"active": self.call_active, "app": self.call_app},
            "permissions": self.permissions,
        }

    def user_stopped(self) -> None:
        self._dismissed = self.call_active
        self._mine = False

    def user_started(self) -> None:
        self._dismissed = False
        self._mine = self.call_active

    def _blocked(self) -> str | None:
        denied = [name for name in ("microphone", "system_audio") if self.permissions.get(name) == "denied"]
        if not denied:
            return None
        names = " and ".join("System Audio Recording" if name == "system_audio" else "Microphone" for name in denied)
        return (
            f"Jarvis cannot listen: {names} is switched off for it. "
            "Turn it on in System Settings > Privacy & Security."
        )

    async def report(self, active: bool, app: str | None, headphones: bool, permissions: dict[str, str]) -> None:
        meetings = self.runtime.meeting
        before = self.snapshot()
        self.last_report = time.monotonic()
        began = active and not self.call_active
        ended = self.call_active and not active
        self.call_active, self.call_app = active, (app if active else None)
        self.headphones, self.permissions = headphones, dict(permissions)

        blocked = self._blocked()
        if blocked:
            self.runtime.notify("permissions", blocked)
        else:
            self.runtime.clear("permissions")

        if began:
            self._dismissed = False
            self._attempts = 0
            self._mine = meetings.running  # already listening: this call takes it over
            log.info("call started in %s", app)
        if ended:
            log.info("call ended")
            if self._mine and meetings.running:
                # Stopping can take a few seconds; it carries on after this report is answered.
                stopping = self.runtime.spawn(meetings.stop())
                if stopping is not None:
                    await asyncio.wait({stopping}, timeout=self.STOP_WAIT_SECONDS)
            self._mine = False
        elif active and self.enabled and not self._dismissed and not meetings.running and not blocked:
            due = self._attempts == 0 or time.monotonic() - self._last_attempt >= self.RETRY_SECONDS
            if due and self._attempts < self.MAX_ATTEMPTS:
                self._attempts += 1
                self._last_attempt = time.monotonic()
                self._mine = True
                await meetings.start(None, None)
        if self.snapshot() != before:
            self.runtime.publish_state()


class Runtime:
    def __init__(self, config: Config) -> None:
        from jarvis.assistant import Assistant

        self.config = config
        self.hub = Hub()
        self.meeting = MeetingController(self)
        self.autopilot = Autopilot(self)
        self.assistant = Assistant(config)
        self.attention = 0
        self.notices: dict[str, str] = {}
        self.last_sync: str | None = None
        self.brief: dict | None = None
        self._syncing = False
        self._sync_lock = asyncio.Lock()
        self._synced_at = 0.0
        self._tasks: set[asyncio.Task] = set()
        self._closing = False
        self._engagements: tuple[float, list[dict]] = (0.0, [])
        self._purged_on = ""

    # --- what the interface sees -------------------------------------------

    def engagements(self, fresh: bool = False) -> list[dict]:
        cached_at, cached = self._engagements
        if not fresh and time.monotonic() - cached_at < ENGAGEMENTS_CACHE_SECONDS:
            return cached
        with open_store() as store:
            known = project_registry.load(store, self.config)
            files = store.stats()["folders"]
        found = [
            {"name": p.name, "people": list(p.people), "folders": len(p.folders), "files": files.get(p.name, 0)}
            for p in known
        ]
        self._engagements = (time.monotonic(), found)
        return found

    def state(self) -> dict:
        return {
            "status": self.meeting.status,
            "meeting": self.meeting.meeting,
            "engagements": self.engagements(),
            **self.autopilot.snapshot(),
            "notices": [{"key": key, "text": text} for key, text in self.notices.items()],
            "attention": self.attention,
            "last_sync": self.last_sync,
            "syncing": self._syncing,
        }

    def pulse(self) -> dict:
        """The little the app needs every two seconds."""
        return {"status": self.meeting.status, "attention": self.attention}

    def publish_state(self) -> None:
        self.hub.publish({"type": "state", **self.state()})

    def call_attention(self) -> None:
        """Ask the app to bring the panel forward: there is something new to read."""
        self.attention += 1
        self.publish_state()

    def notify(self, key: str, text: str) -> None:
        if self.notices.get(key) == text:
            return
        self.notices[key] = text
        self.call_attention()

    def clear(self, key: str) -> None:
        if self.notices.pop(key, None) is not None:
            self.publish_state()

    # --- background work -----------------------------------------------------

    def spawn(self, work: Coroutine) -> asyncio.Task | None:
        if self._closing:  # shutting down: leave it for the next start (see recover_notes)
            work.close()
            return None
        task = asyncio.create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def start_background(self) -> None:
        self.load_brief()
        self.spawn(_warm_up())
        self.spawn(self._sync_loop())
        self.spawn(self._daily_loop())
        self.spawn(self.recover_notes())

    async def shutdown(self) -> None:
        self._closing = True
        for task in list(self._tasks):
            task.cancel()
        await self.meeting.stop()
        await self.assistant.close()
        self.hub.close()

    async def _sync_loop(self) -> None:
        await asyncio.sleep(FIRST_SYNC_AFTER_SECONDS)
        while True:
            due = time.monotonic() - self._synced_at >= SYNC_EVERY_SECONDS
            if due and not self.meeting.running:  # a call gets the machine to itself
                await self.sync_now()
            await asyncio.sleep(30)

    async def sync_now(self, fresh_for: float = 0) -> list[dict]:
        """Bring mail, calendar, Drive and project folders up to date.

        Waits for a sync already under way, and does nothing if one finished
        within the last ``fresh_for`` seconds.
        """
        from jarvis import sync as sync_module

        async with self._sync_lock:
            if fresh_for and time.monotonic() - self._synced_at < fresh_for:
                return []
            self._syncing = True
            self.publish_state()
            try:
                results = await asyncio.to_thread(sync_module.run_sync, self.config, progress=log.info)
            except Exception as exc:
                log.warning("sync failed: %s", exc)
                return []
            finally:
                self._syncing = False
                self._synced_at = time.monotonic()
        return self._after_sync(results)

    def _after_sync(self, results: list[dict]) -> list[dict]:
        from jarvis import sync as sync_module

        for result in results:
            if result["status"] == "error":
                log.warning("sync: %s %s: %s", result["account"], result["source"], result.get("error"))
        expired = [r for r in results if r["status"] == "error" and r["source"] == "*" and _sign_in_lost(r.get("error", ""))]
        if expired:
            self.notify(
                "signin",
                f"Google sign-in for {expired[0]['account']} has stopped working, so mail and calendar are going stale.",
            )
        elif any(r["status"] == "ok" for r in results if r["source"] in sync_module.SOURCES):
            self.clear("signin")
        self.last_sync = now_iso()
        self._engagements = (0.0, [])
        self.publish_state()
        return results

    async def reconnect(self) -> str:
        """Run Google sign-in again in the browser, for the account whose sign-in stopped working."""
        from jarvis import google_auth

        email = self.config.primary_account.email
        actual = await asyncio.to_thread(
            google_auth.add_account, email, None,
            consent=lambda address: google_auth.run_consent_flow(address, google_auth.DEFAULT_CONSENT_TIMEOUT_SECONDS),
        )
        self.clear("signin")
        self.spawn(self.sync_now())
        return actual

    async def _daily_loop(self) -> None:
        await asyncio.sleep(FIRST_SYNC_AFTER_SECONDS + 45)  # after the first sync has had a chance
        while True:
            try:
                await self._daily()
            except Exception as exc:
                log.warning("daily jobs failed: %s", exc)
            await asyncio.sleep(DAILY_CHECK_SECONDS)

    async def _daily(self) -> None:
        from jarvis import brief

        today = datetime.now().date().isoformat()
        if self._purged_on != today:
            self._purged_on = today
            removed = purge_transcripts(self.config.transcript_retention_days)
            if removed:
                log.info("removed %d old transcripts", removed)
        due = (
            (self.brief or {}).get("date") != today
            and datetime.now().hour >= BRIEF_FROM_HOUR
            and not self.meeting.running
        )
        if due:
            await self.sync_now(fresh_for=SYNC_FRESH_FOR_BRIEF_SECONDS)  # the brief wants this morning's mail
            written = await brief.write(self.config)
            if written is not None:
                self.brief = written
                self.hub.publish({"type": "brief", **written})
                self.call_attention()

    def load_brief(self) -> None:
        from jarvis import brief

        self.brief = brief.load_today()

    async def wrap_up(self, transcript: Path) -> None:
        """After a call: write the notes and say they are ready."""
        from jarvis import notes

        meeting = read_meta(transcript)
        if meeting.get("noted"):
            return
        try:
            note = await notes.write(self.config, transcript, meeting)
        except Exception as exc:
            log.warning("notes failed: %s", exc)
            self.hub.publish({"type": "info", "text": f"The meeting notes could not be written: {exc}"})
            return
        write_meta(transcript, {**meeting, "noted": True})
        if note is None:
            return
        log.info("notes written: %s", note.path)
        self.hub.publish({"type": "notes", **note.summary()})
        self.call_attention()

    async def recover_notes(self) -> None:
        """Write the notes of calls that ended while Jarvis was being shut down."""
        folder = paths.data_dir() / "transcripts"
        if not folder.is_dir():
            return
        cutoff = time.time() - RECOVER_NOTES_WITHIN_SECONDS
        for transcript in sorted(folder.glob("*.jsonl")):
            if transcript.stat().st_mtime >= cutoff and meta_path(transcript).exists():
                await self.wrap_up(transcript)


async def _warm_up() -> None:
    """Load the speech libraries ahead of the first call, off the event loop, so a call starts at once."""
    import importlib

    await asyncio.to_thread(importlib.import_module, "jarvis.listen")


def _sign_in_lost(error: str) -> bool:
    """True for a refused or missing sign-in; False for a passing network failure."""
    lowered = error.lower()
    return "not signed in" in lowered or "refresherror" in lowered or "invalid_grant" in lowered


def meta_path(transcript: Path) -> Path:
    return transcript.with_suffix(".meta.json")


def write_meta(transcript: Path, meeting: dict) -> None:
    """Keep what the meeting was next to its transcript, so notes can be written after a restart."""
    meta_path(transcript).write_text(json.dumps(meeting, ensure_ascii=False), encoding="utf-8")


def read_meta(transcript: Path) -> dict:
    try:
        return json.loads(meta_path(transcript).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def purge_transcripts(days: int, folder: Path | None = None, now: float | None = None) -> int:
    """Delete transcripts older than ``days``. The notes written from them are kept."""
    folder = folder or paths.data_dir() / "transcripts"
    cutoff = (now or time.time()) - days * 86_400
    removed = 0
    if folder.is_dir():
        for path in folder.glob("*.jsonl"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    meta_path(path).unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue
    return removed

