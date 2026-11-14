"""What the Jarvis daemon keeps running between requests.

- the meeting in progress, if any;
- the autopilot, which starts and stops listening from what the Jarvis app
  reports about calls, so nobody has to press Start;
- Jarvis himself (the conversation) and the tasks he runs in the background;
- background jobs: keeping the local copy fresh, the morning brief, meeting
  notes after a call, and clearing old transcripts;
- speaking up by himself when new mail needs the user, and keeping each
  engagement's dossier and promises up to date.

Everything the interface shows comes out of :meth:`Runtime.state` and the
events published on the hub.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from collections.abc import Callable, Coroutine
from datetime import datetime
from pathlib import Path

from jarvis import paths
from jarvis import projects as project_registry
from jarvis.config import Config
from jarvis.store import LOCAL_ACCOUNT, open_store

log = logging.getLogger("jarvis")

SYNC_EVERY_SECONDS = 600
MAIL_EVERY_SECONDS = 150  # new mail and calendar changes are cheap to ask for, so ask often
SYNC_FRESH_FOR_BRIEF_SECONDS = 900
FIRST_SYNC_AFTER_SECONDS = 15
DAILY_CHECK_SECONDS = 300
BRIEF_FROM_HOUR = 6
APP_SILENT_AFTER_SECONDS = 10
ENGAGEMENTS_CACHE_SECONDS = 30
TEST_WAV_ENV = "JARVIS_TEST_WAV"  # replay a recording as the call, for tests
RECOVER_NOTES_WITHIN_SECONDS = 86_400
CARDS_KEPT = 12
CONVERSATION_KEPT = 10
SPEECH_KEPT_SECONDS = 180
SPEAK_UP_WITHIN_SECONDS = 15 * 60  # Jarvis speaks up unasked only this long after the user last spoke to him
MODEL_IDLE_SECONDS = 600  # the speaking voice is freed after this long unused
# The recogniser (1.5 GB) is freed after this long unused. With the ear on, any voice nearby loads it again, and that
# takes well under a second, started as the voice begins: the first wake after it was freed came no later than any
# other (0.47 s after the name against 0.46 s). So it need not be kept long.
RECOGNISER_IDLE_SECONDS = 120
AUTO_PREP_NEAREST = 12 * 60  # prepare for meetings that start between these two, in seconds from now
AUTO_PREP_FURTHEST = 50 * 60
AUTO_PREP_PER_DAY = 3
# wake_word: the ear listens for "Jarvis" while nothing else has the microphone.
DEFAULT_SETTINGS = {"speak": True, "auto_prep": True, "proactive": True, "wake_word": True}
REMINDER_LATE_SECONDS = 15 * 60
PROPOSALS_PER_DAY = 6
PROPOSALS = "proposals"  # where the store counts the proposals made each day
HELD_SHOWN = 2  # of the proposals that waited for a call to end, this many are shown
PROPOSAL_ORDER = ("meeting_change", "watch_hit", "needs_reply", "fyi")  # which held ones go first


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
                if event["engagement"]:
                    self.runtime.hub.publish({"type": "info", "text": f"This call is about {event['engagement']}."})
            if self._out is not None:
                write_meta(self._out, self.meeting)
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
        self._task.add_done_callback(lambda _: self.runtime.update_ear())  # the call has the microphone until then
        self.runtime.update_ear()
        self.runtime.publish_state()

    async def _run(self, project: str | None, headphones: bool | None, out: Path) -> None:
        from jarvis import meeting as live

        wav = os.environ.get(TEST_WAV_ENV)
        try:
            await live.run(
                self.runtime.config, project_name=project, out=out, titles=self.runtime.autopilot.call_titles,
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
            self.runtime.release_proposals()

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
        self.call_titles: list[str] = []  # the calling app's window titles, when the app may read them
        self.ear_state = "off"
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

    async def report(self, active: bool, app: str | None, headphones: bool, permissions: dict[str, str],
                     titles: list[str] | None = None, ear_state: str = "off") -> None:
        meetings = self.runtime.meeting
        before = self.snapshot()
        self.last_report = time.monotonic()
        began = active and not self.call_active
        ended = self.call_active and not active
        self.call_active, self.call_app = active, (app if active else None)
        fresh = [title for title in titles or [] if title][:6] if active else []
        if fresh != self.call_titles:
            self.call_titles = fresh
            if fresh:
                log.info("call windows: %s", " | ".join(fresh))  # to learn what the calling apps name their windows
        self.ear_state = ear_state
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
        from jarvis.approvals import Approvals
        from jarvis.core import Core
        from jarvis.ear import Ear
        from jarvis.tasks import Tasks

        self.config = config
        self.hub = Hub()
        self.meeting = MeetingController(self)
        self.autopilot = Autopilot(self)
        self.tasks = Tasks(self)
        self.approvals = Approvals(self)
        self.core = Core(config, self)
        self.ear = Ear(self.hub.publish, self._heard_by_ear, on_listening=self.warm_voice, vocabulary=self._vocabulary)
        self.attention = 0
        self.notices: dict[str, str] = {}
        self.cards: list[dict] = []  # newest first
        self.conversation: deque = deque(maxlen=CONVERSATION_KEPT)
        self.settings = dict(DEFAULT_SETTINGS)
        self.last_sync: str | None = None
        self.brief: dict | None = None
        self._syncing = False
        self._sync_lock = asyncio.Lock()
        self._synced_at = 0.0
        self._tasks: set[asyncio.Task] = set()
        self._closing = False
        self._engagements: tuple[float, list[dict]] = (0.0, [])
        self._purged_on = ""
        self._ids = 0
        self._speech: dict[tuple[str, int], tuple[float, asyncio.Task]] = {}
        self._spoke_at = 0.0  # when the user last spoke to Jarvis, and when Jarvis last made a sound
        self._sounded_at = 0.0
        self._folders_due = False
        self._voice_playing = False
        self._auto_prepared: set[str] = set()
        self._held: list[dict] = []  # proposals waiting for a call to end
        self._looking = False  # a look at new mail is under way
        self._dossier_lock = asyncio.Lock()  # one dossier is written at a time
        self._rebuilding: set[str] = set()
        self._dossiers_on = ""  # the day the dossiers were last checked
        self._load_settings()
        self.update_ear()

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
            "busy": self.core.busy,
            "tasks": [task.summary() for task in self.tasks.recent()],
            "approvals": [approval.summary() for approval in self.approvals.open()],
            "cards": list(self.cards),
            "conversation": list(self.conversation),
            "settings": {**self.settings, "auto": self.autopilot.enabled},
            "voice": {
                "hear": self.autopilot.app_connected and self.autopilot.permissions.get("microphone") == "granted",
                "speak": _speak_engine() is not None,
            },
            "ear": {"on": self.ear.state != "off", "awake": self.ear.state == "awake"},
        }

    def pulse(self) -> dict:
        """The little the app needs every two seconds."""
        return {"status": self.meeting.status, "attention": self.attention, "ear": self.ear_wanted()}

    # --- the ear -------------------------------------------------------------------

    def ear_wanted(self) -> bool:
        """Whether the app should keep the microphone open for Jarvis's name: the setting is on, the ear can run,
        the microphone is allowed, and no call is on or being listened to."""
        from jarvis import ear

        return (
            bool(self.settings.get("wake_word")) and not self.in_call()
            and self.autopilot.permissions.get("microphone") == "granted" and ear.available()
        )

    def ear_refusal(self) -> str | None:
        """Why a connection to the ear is turned away now: "busy" during a call, "unavailable" when not wanted."""
        if self.in_call():
            return "busy"
        return None if self.ear_wanted() else "unavailable"

    def update_ear(self) -> None:
        """Close the ear while a call is on or the setting is off, and open it again after. The app closes its end
        too, but the microphone of a call must never reach Jarvis's own ears, however late the app's word is."""
        self.ear.headphones = self.autopilot.headphones
        if self.in_call():
            self.ear.pause("call")
        else:
            self.ear.resume("call")
        if self.settings.get("wake_word"):
            self.ear.resume("setting")
        else:
            self.ear.pause("setting")

    def _vocabulary(self) -> list[str]:
        """The user's names and terms, which a message the ear takes is read leaning towards."""
        from jarvis import vocabulary

        with open_store() as store:
            return vocabulary.words(store, self.config)

    def _heard_by_ear(self, text: str) -> None:
        """A message the ear took: Jarvis answers it as he would a held-key one."""
        self.spawn(self._answer_spoken(text))

    async def _answer_spoken(self, text: str) -> None:
        from jarvis.agents import AgentError

        try:
            await self.talk(text, spoken=True)
        except AgentError:
            pass  # already shown as the turn's error

    def speaking(self, speech_id: str, playing: bool) -> None:
        """The page says Jarvis's voice started or stopped playing. The ear must not take his voice for the user's,
        and listens for a follow-up once it has stopped."""
        self._voice_playing = playing
        if playing:
            self.ear.speaking_started()
        else:
            self.ear.speaking_ended()

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

    def _next_id(self, prefix: str) -> str:
        self._ids += 1
        return f"{prefix}{self._ids}"

    # --- cards, activity, settings ---------------------------------------------

    def show_card(self, card: dict) -> dict:
        """Add a card under the orb, or replace the one with the same id."""
        card = {"id": card.get("id") or self._next_id("c"), "at": now_iso(), "body": "", **card}
        self.cards = [card, *(old for old in self.cards if old["id"] != card["id"])][:CARDS_KEPT]
        self.hub.publish({"type": "card", "card": card})
        return card

    def drop_card(self, card_id: str) -> bool:
        kept = [card for card in self.cards if card["id"] != card_id]
        if len(kept) == len(self.cards):
            return False
        self.cards = kept
        self.hub.publish({"type": "card_gone", "id": card_id})
        return True

    def record(self, kind: str, text: str) -> None:
        """One line in the activity log the dashboard shows."""
        try:
            with open_store() as store:
                store.log(kind, text)
        except Exception as exc:  # the log must never break what was being done
            log.warning("activity log failed: %s", exc)

    def note(self, text: str) -> None:
        """Tell Jarvis something he should know by the user's next message."""
        self.core.note(text)

    def _load_settings(self) -> None:
        with open_store() as store:
            for key, default in DEFAULT_SETTINGS.items():
                saved = store.get_state(LOCAL_ACCOUNT, "settings", key)
                self.settings[key] = default if saved is None else saved == "1"
            auto = store.get_state(LOCAL_ACCOUNT, "settings", "auto")
        self.autopilot.enabled = auto != "0"

    def change_settings(self, changes: dict) -> dict:
        with open_store() as store:
            for key, value in changes.items():
                if value is None or key not in (*DEFAULT_SETTINGS, "auto"):
                    continue
                if key == "auto":
                    self.autopilot.enabled = bool(value)
                else:
                    self.settings[key] = bool(value)
                store.set_state(LOCAL_ACCOUNT, "settings", key, "1" if value else "0")
        self.update_ear()
        self.publish_state()
        return {**self.settings, "auto": self.autopilot.enabled}

    # --- talking -----------------------------------------------------------------

    def _may_speak(self) -> bool:
        """Jarvis only makes a sound when nobody else could hear it on a call."""
        return (
            self.settings["speak"] and not self.meeting.running and not self.autopilot.call_active
            and _speak_engine() is not None
        )

    def warm_voice(self) -> None:
        """The user has started talking: get the conversation and the speaking voice ready while they do."""
        self.spawn(self.core.warm())
        if self._may_speak():
            from jarvis import speak

            self._sounded_at = time.monotonic()
            self.spawn(asyncio.to_thread(speak.warm))

    def _speak(self, speech_id: str, seq: int, sentence: str) -> bool:
        """Start turning one sentence into sound and tell the interface where to fetch it."""
        from jarvis import speak

        text = speak.speakable(sentence)
        if not text:
            return False
        now = time.monotonic()
        for key in [key for key, (made, _) in self._speech.items() if now - made > SPEECH_KEPT_SECONDS]:
            del self._speech[key]
        self._speech[(speech_id, seq)] = (now, asyncio.create_task(asyncio.to_thread(speak.synthesize, text)))
        self._sounded_at = now
        self.hub.publish({
            "type": "speech", "id": speech_id, "seq": seq, "text": text, "url": f"/api/speech/{speech_id}/{seq}",
        })
        return True

    async def speech_audio(self, speech_id: str, seq: int) -> bytes | None:
        entry = self._speech.get((speech_id, seq))
        return await entry[1] if entry else None

    def say(self, text: str, *, aloud: bool = False) -> None:
        """Jarvis speaking up by himself: a task's questions, or that something is ready.

        Always shown. Spoken only if the user has been talking to Jarvis in the
        last few minutes, so it never starts talking out of the blue; ``aloud``
        is for what the user asked to be told at that moment, such as a reminder.
        """
        speech_id = self._next_id("s")
        at = now_iso()
        self.hub.publish({"type": "say", "id": speech_id, "text": text, "at": at})
        self.conversation.append({"role": "jarvis", "text": text, "at": at})
        in_conversation = time.monotonic() - self._spoke_at < SPEAK_UP_WITHIN_SECONDS
        if (aloud or in_conversation) and self._may_speak():
            from jarvis import speak

            for seq, sentence in enumerate(speak.sentences(text)):
                self._speak(speech_id, seq, sentence)
        self.publish_state()

    async def talk(self, text: str, *, spoken: bool = False, turn_id: str | None = None) -> dict:
        """One message from the user and Jarvis's reply. Raises AgentError if no reply could be made."""
        from jarvis.agents import AgentError

        turn_id = turn_id or self._next_id("t")
        started = time.monotonic()
        if spoken:
            self._spoke_at = started
        aloud = spoken and self._may_speak()
        speaker = _Speaker(self, turn_id) if aloud else None
        at = now_iso()
        self.hub.publish({"type": "turn", "id": turn_id, "text": text, "spoken": spoken, "at": at})
        self.conversation.append({"role": "user", "text": text, "at": at})

        # A plain yes or no to an open question is the user's answer to it. It never reaches a model.
        settled = self.approvals.words(text, spoken)
        if settled is not None:
            said = "Going ahead." if settled.granted else "Cancelled."
            reply = {"id": turn_id, "text": said, "speak": False, "seconds": 0.0, "at": now_iso()}
            self.hub.publish({"type": "reply", **reply})
            self.conversation.append({"role": "jarvis", "text": said, "at": reply["at"]})
            self.publish_state()
            return {**reply, "web_queries": [], "drafts": []}

        def on_text(so_far: str) -> None:
            self.hub.publish({"type": "reply_delta", "id": turn_id, "text": so_far})
            if speaker:
                speaker.feed(so_far)

        working = self.spawn(self._show_busy())
        try:
            answer = await self.core.talk(
                text, spoken=spoken, on_text=on_text,
                on_step=lambda step: self.hub.publish({"type": "step", "id": turn_id, "text": step[:160]}),
            )
        except AgentError as exc:
            self.hub.publish({"type": "reply_error", "id": turn_id, "text": str(exc)})
            raise
        finally:
            if working is not None:
                working.cancel()
            self.publish_state()
        if speaker:
            speaker.finish(answer.text)
        reply = {
            "id": turn_id, "text": answer.text, "speak": aloud,
            "seconds": round(time.monotonic() - started, 1), "at": now_iso(),
        }
        self.hub.publish({"type": "reply", **reply})
        self.conversation.append({"role": "jarvis", "text": answer.text, "at": reply["at"]})
        self.publish_state()
        return {**reply, "web_queries": answer.web_queries, "drafts": answer.drafts}

    async def _show_busy(self) -> None:
        await asyncio.sleep(0)  # by now the conversation's lock is taken
        self.publish_state()

    # --- speaking up unasked ---------------------------------------------------

    def in_call(self) -> bool:
        return self.meeting.running or self.autopilot.call_active

    def propose(self, text: str, actions: list[dict], *, mail: int | None = None, kind: str = "fyi",
                on_shown: Callable[[], None] | None = None) -> bool:
        """Jarvis speaks up by himself about something the user would want to know now.

        A card with up to three answers (pressing one sends its words as a typed
        message), said like anything else Jarvis says by himself, and logged.
        At most six a day, none while the setting ``proactive`` is off, and none
        during a call: those wait for it to end. ``on_shown`` runs once it is
        shown. Returns False when it was dropped.
        """
        if not self.settings["proactive"]:
            return False
        proposal = {"text": text, "actions": actions, "mail": mail, "kind": kind, "on_shown": on_shown}
        if self.in_call():
            self._held.append(proposal)
            return True
        return self._put_up(proposal)

    def _proposals_today(self, store) -> int:
        return int(store.get_state(LOCAL_ACCOUNT, PROPOSALS, datetime.now().date().isoformat()) or 0)

    def _put_up(self, proposal: dict) -> bool:
        with open_store() as store:
            made = self._proposals_today(store)
            if made >= PROPOSALS_PER_DAY:
                return False
            store.set_state(LOCAL_ACCOUNT, PROPOSALS, datetime.now().date().isoformat(), made + 1)
        text, mail = proposal["text"], proposal["mail"]
        self.show_card({"kind": "proposal", "title": "", "body": text, "actions": proposal["actions"], "mail": mail})
        self.say(text)
        self.call_attention()
        self.record("proposal", f"Spoke up: {text[:200]}")
        self.note(f"You spoke up by yourself and told the user: \"{text}\"" + (f" (about mail [{mail}])." if mail else "."))
        if proposal["on_shown"] is not None:
            try:
                proposal["on_shown"]()
            except Exception as exc:  # what follows from it must not take back what was shown
                log.warning("after a proposal: %s", exc)
        return True

    def release_proposals(self) -> int:
        """After a call: show the proposals that waited for it, the most pressing two at most."""
        if not self._held or self.in_call():
            return 0
        held, self._held = self._held, []
        if not self.settings["proactive"]:
            return 0
        rank = {kind: place for place, kind in enumerate(PROPOSAL_ORDER)}
        chosen = sorted(held, key=lambda proposal: rank.get(proposal["kind"], len(rank)))[:HELD_SHOWN]
        return sum(1 for proposal in chosen if self._put_up(proposal))

    async def look_at_mail(self) -> list:
        """After a sync: let a small model decide whether new mail should interrupt the user."""
        from jarvis import triage

        if self._looking or not self.settings["proactive"] or self.core.busy:
            return []  # the mail waits for the next sync: a reply being written is not interrupted
        with open_store() as store:
            if self._proposals_today(store) >= PROPOSALS_PER_DAY:
                return []
        self._looking = True
        try:
            return await triage.look(self)
        except Exception as exc:
            log.warning("looking at new mail failed: %s", exc)
            return []
        finally:
            self._looking = False

    # --- what Jarvis knows about each engagement ---------------------------------

    async def rebuild_dossier(self, name: str, *, asked: bool = False) -> bool:
        """Write an engagement's dossier again from its material. ``asked``: the user wants to hear when it is done."""
        from jarvis import dossier

        if name in self._rebuilding:
            return False
        self._rebuilding.add(name)
        try:
            async with self._dossier_lock:
                text = await dossier.build(self.config, name)
        except Exception as exc:
            log.warning("the dossier of %s could not be built: %s", name, exc)
            text = None
        finally:
            self._rebuilding.discard(name)
        if text is not None:
            self.record("dossier", f"Rebuilt the dossier of {name}")
        if asked:
            done = f"The {name} dossier is up to date." if text else f"The {name} dossier could not be rebuilt."
            self.note(done)
            self.say(done)
        return text is not None

    async def refresh_dossiers(self) -> list[str]:
        """Once a day: rebuild the dossiers of engagements that had new mail, notes or work since their last build."""
        from jarvis import dossier

        with open_store() as store:
            due = dossier.due(store, self.config, time.time())
        rebuilt = []
        for name in due:
            if self.meeting.running:  # a call gets the machine to itself; the rest wait for tomorrow
                break
            if await self.rebuild_dossier(name):
                rebuilt.append(name)
        return rebuilt

    def rebuilding(self, name: str) -> bool:
        return name in self._rebuilding

    async def after_notes(self, title: str, engagement: str | None, at: str, text: str, source: str) -> None:
        """After a call's notes are written: record the promises made in it, and bring its engagement's dossier up to date."""
        from jarvis import promises

        try:
            added = await promises.from_notes(self.config, title, engagement, at, text, source)
            if added:
                self.record("promise", f"Noted {len(added)} promises from the call \"{title}\"")
        except Exception as exc:
            log.warning("promises from %s failed: %s", title, exc)
        if engagement:
            await self.update_dossier(engagement, title, at, text)

    async def update_dossier(self, engagement: str, title: str, at: str, text: str) -> None:
        from jarvis import dossier

        try:
            async with self._dossier_lock:
                updated = await dossier.update(self.config, engagement, title, at, text)
        except Exception as exc:
            log.warning("the dossier of %s could not be updated: %s", engagement, exc)
            return
        if updated:
            self.record("dossier", f"Updated the dossier of {engagement} from the call \"{title}\"")

    def call_refiled(self, note: dict, engagement: str | None) -> None:
        """The latest call was filed under the wrong engagement (``note`` as it was) and now belongs to ``engagement``.

        The dossier it went into is written again without it; the one it belongs to takes it in.
        """
        if note["project"] == engagement:
            return
        if note["project"]:
            self.spawn(self.rebuild_dossier(note["project"]))
        if engagement:
            at = datetime.fromtimestamp(note["ts"]).astimezone().isoformat(timespec="seconds")
            self.spawn(self.update_dossier(engagement, note["title"], at, note["body"]))

    # --- background work -----------------------------------------------------

    def spawn(self, work: Coroutine) -> asyncio.Task | None:
        if self._closing:  # shutting down: leave it for the next start (see recover_notes)
            work.close()
            return None
        task = asyncio.create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def restore_cards(self) -> None:
        """After a restart, put today's preparation sheets back under the orb: the user may be about to need them."""
        start = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        with open_store() as store:
            sheets = store.artifacts("prep", since=start, limit=8)
        latest: dict[tuple, dict] = {}
        for sheet in sheets:  # newest first: a sheet that was redone replaces the earlier one
            meeting = sheet["meta"].get("meeting") or {}
            latest.setdefault((sheet["title"], meeting.get("start")), sheet)
        for sheet in reversed(list(latest.values())):  # the newest ends up on top
            meeting = sheet["meta"].get("meeting") or {}
            if meeting.get("end") and datetime.fromisoformat(meeting["end"]).timestamp() < time.time():
                continue  # that meeting is over
            self.show_card({
                "id": f"p-{sheet['id']}", "kind": "prep", "title": f"Preparation: {sheet['title']}",
                "body": sheet["body"], "path": sheet["path"],
            })

    def start_background(self) -> None:
        self.load_brief()
        self.restore_cards()
        self.spawn(_warm_up())
        self.spawn(self._sync_loop())
        self.spawn(self._daily_loop())
        self.spawn(self._minute_loop())
        self.spawn(self.recover_notes())

    async def shutdown(self) -> None:
        self._closing = True
        self.approvals.cancel_all()
        for task in list(self._tasks):
            task.cancel()
        await self.meeting.stop()
        await self.core.close()
        self.hub.close()

    async def _sync_loop(self) -> None:
        await asyncio.sleep(FIRST_SYNC_AFTER_SECONDS)
        mail_at = 0.0
        while True:
            now = time.monotonic()
            if self.meeting.running:  # a call gets the machine to itself
                pass
            elif now - self._synced_at >= SYNC_EVERY_SECONDS:
                await self.sync_now()
                mail_at = time.monotonic()
            elif self._folders_due:
                await self.sync_now(only="folders")
            elif now - mail_at >= MAIL_EVERY_SECONDS:
                mail_at = now
                await self.sync_now(only="gmail")
                await self.sync_now(only="calendar")
            await asyncio.sleep(30)

    def folders_changed(self) -> None:
        """Jarvis wrote into an engagement folder: read the folders again soon."""
        self._folders_due = True

    async def sync_now(self, fresh_for: float = 0, only: str | None = None) -> list[dict]:
        """Bring mail, calendar, Drive and project folders up to date.

        Waits for a sync already under way, and does nothing if one finished
        within the last ``fresh_for`` seconds. ``only`` limits it to one source.
        """
        from jarvis import sync as sync_module

        async with self._sync_lock:
            if fresh_for and time.monotonic() - self._synced_at < fresh_for:
                return []
            self._syncing = True
            self._folders_due = False
            self.publish_state()
            try:
                results = await asyncio.to_thread(sync_module.run_sync, self.config, only=only, progress=log.info)
            except Exception as exc:
                log.warning("sync failed: %s", exc)
                return []
            finally:
                self._syncing = False
                if only is None:
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
        news = _sync_news(results)
        if news:
            self.record("sync", news)
        self.last_sync = now_iso()
        self._engagements = (0.0, [])
        self.publish_state()
        if any(result["source"] == "gmail" and result["status"] == "ok" for result in results):
            self.spawn(self.look_at_mail())
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

    async def _minute_loop(self) -> None:
        """Small regular jobs: prepare for meetings that are coming up, hand back memory nobody is using."""
        from jarvis import speech

        await asyncio.sleep(FIRST_SYNC_AFTER_SECONDS + 30)
        while True:
            try:
                self.auto_prepare()
                self.remind_due()
                self.release_proposals()
                if speech.unload_if_idle(RECOGNISER_IDLE_SECONDS):
                    log.info("speech model released")
                if self._sounded_at and time.monotonic() - self._sounded_at > MODEL_IDLE_SECONDS:
                    from jarvis import speak

                    self._sounded_at = 0.0
                    speak.unload()
            except Exception as exc:
                log.warning("minute jobs failed: %s", exc)
            await asyncio.sleep(60)

    def remind_due(self, now: float | None = None) -> int:
        """Say and show the reminders whose time has come. Returns how many."""
        now = now or time.time()
        with open_store() as store:
            due = store.reminders(due_by=int(now))
            for item in due:
                store.reminder_done(item["id"])
        for item in due:
            late = now - item["due_ts"] > REMINDER_LATE_SECONDS  # the Mac was asleep at the time
            was = f" (it was due at {datetime.fromtimestamp(item['due_ts']):%H:%M on %A})" if late else ""
            self.show_card({"id": f"r-{item['id']}", "kind": "note", "title": "Reminder", "body": item["text"] + was})
            self.say(f"A reminder: {item['text']}{was}", aloud=True)
            self.record("reminder", f"Reminded: {item['text'][:160]}")
        if due:
            self.call_attention()
        return len(due)

    def auto_prepare(self, now: float | None = None) -> list:
        """Start preparing, unasked, for meetings with outside people that begin within the hour."""
        from jarvis import meeting as live

        if not self.settings["auto_prep"] or self.meeting.running:
            return []
        now = now or time.time()
        day = datetime.fromtimestamp(now).date().isoformat()
        self._auto_prepared = {key for key in self._auto_prepared if key.startswith(day)}
        if len(self._auto_prepared) >= AUTO_PREP_PER_DAY:
            return []
        with open_store() as store:
            known = project_registry.load(store, self.config)
            account = self.config.primary_account.email.lower()
            upcoming = live.agenda(store, account, known, int(now + AUTO_PREP_NEAREST), int(now + AUTO_PREP_FURTHEST))
            start_of_day = int(datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
            made = {
                (item["meta"].get("meeting") or {}).get("start")
                for item in store.artifacts("prep", since=start_of_day)
            }
        started = []
        for meeting in upcoming:
            key = f"{day} {meeting['start']} {meeting['title']}"
            if not (meeting["outside"] and meeting["engagement"]) or meeting["skipped"] or key in self._auto_prepared:
                continue
            self._auto_prepared.add(key)
            if meeting["start"] in made or self.tasks.for_meeting(meeting, meeting["engagement"]) is not None:
                continue
            if len(self._auto_prepared) > AUTO_PREP_PER_DAY:
                break
            started.append(self.tasks.start_prep(meeting, meeting["engagement"], auto=True))
        return started

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
                self.note("The morning brief for today was written; it is on the dashboard.")
                self.call_attention()
        if self._dossiers_on != today and datetime.now().hour >= BRIEF_FROM_HOUR and not self.meeting.running:
            self._dossiers_on = today
            self.spawn(self.refresh_dossiers())  # minutes of reading: it must not hold up the other daily jobs

    def load_brief(self) -> None:
        from jarvis import brief

        self.brief = brief.load_today()

    async def wrap_up(self, transcript: Path) -> None:
        """After a call: write the notes and say they are ready."""
        from jarvis import notes

        meeting = read_meta(transcript)
        if meeting.get("noted"):
            return
        if not transcript.exists():  # nothing was said: leave no trace of the meeting
            meta_path(transcript).unlink(missing_ok=True)
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
        self.show_card({
            "id": f"n-{transcript.stem}", "kind": "note", "title": f"Notes: {note.title}",
            "body": note.text, "path": str(note.path),
        })
        self.note(f"The call \"{note.title}\" ended and its notes were written; they are on a card and saved.")
        self.call_attention()
        self.spawn(self.after_notes(note.title, note.engagement, note.at, note.text, str(note.path)))

    async def recover_notes(self) -> None:
        """Write the notes of calls that ended while Jarvis was being shut down."""
        folder = paths.data_dir() / "transcripts"
        if not folder.is_dir():
            return
        cutoff = time.time() - RECOVER_NOTES_WITHIN_SECONDS
        for transcript in sorted(folder.glob("*.jsonl")):
            if transcript.stat().st_mtime >= cutoff and meta_path(transcript).exists():
                await self.wrap_up(transcript)


class _Speaker:
    """Turns a reply into spoken sentences while it is still being written.

    The reply arrives as "the whole text so far". When Jarvis says a line, uses
    a tool and then starts a new line, the text starts over; what was said
    before the tool is finished off first.
    """

    def __init__(self, runtime: Runtime, speech_id: str) -> None:
        from jarvis import speak

        self._runtime = runtime
        self._id = speech_id
        self._make = speak.SentenceStream
        self._stream = self._make()
        self._previous = ""
        self._seq = 0

    def _out(self, sentences: list[str]) -> None:
        for sentence in sentences:
            if self._runtime._speak(self._id, self._seq, sentence):
                self._seq += 1

    def feed(self, text: str) -> None:
        if self._previous and not text.startswith(self._previous[: max(1, len(self._previous) // 2)]):
            self._out(self._stream.finish(self._previous))
            self._stream = self._make()
        self._previous = text
        self._out(self._stream.feed(text))

    def finish(self, final: str) -> None:
        self.feed(final)
        self._out(self._stream.finish(final))


def _speak_engine() -> str | None:
    """The voice Jarvis can speak with on this Mac, or None."""
    try:
        from jarvis import speak
    except ImportError:
        return None
    return speak.engine()


def _sync_news(results: list[dict]) -> str:
    """One line for the activity log when a sync brought something in, else ''."""
    parts = []
    for result in results:
        summary = result.get("summary") or {}
        if result["status"] != "ok" or result["source"] == "folders":
            continue
        changed = sum(
            value for key, value in summary.items()
            if isinstance(value, int) and not isinstance(value, bool)
            and any(word in key for word in ("added", "changed", "updated", "fetched", "removed", "exported"))
        )
        if changed:
            parts.append(f"{result['source']}: {changed} changes")
    return "Synced " + ", ".join(parts) if parts else ""


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

