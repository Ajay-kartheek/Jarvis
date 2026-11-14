"""The local server behind the Jarvis interface.

Serves the built interface from ``ui/dist`` and feeds it over REST and one
WebSocket. It listens on 127.0.0.1 only, and refuses requests that come from
any other web page, so a site open in the browser cannot read the transcript.

The Jarvis app talks to the same server: every two seconds it reports whether
a call is on, and gets back what it needs to show.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from jarvis import dashboard
from jarvis import meeting as live
from jarvis import projects as project_registry
from jarvis.config import Config
from jarvis.runtime import Runtime
from jarvis.store import open_store

UI_DIST = Path(__file__).resolve().parent.parent / "ui" / "dist"
LOCAL_HOSTS = ("127.0.0.1", "localhost")


class StartRequest(BaseModel):
    project: str | None = None
    headphones: bool | None = None  # None: go by what the app reports


class TalkRequest(BaseModel):
    text: str | None = None
    question: str | None = None  # the older name for text
    spoken: bool = False
    id: str | None = None  # echoed on the events of this turn


class AnswerRequest(BaseModel):
    text: str


class ApprovalRequest(BaseModel):
    approve: bool
    scope: Literal["task"] | None = None  # "Allow for this task": the yes holds for the rest of that task


class SettingsRequest(BaseModel):
    auto: bool | None = None
    speak: bool | None = None
    auto_prep: bool | None = None
    proactive: bool | None = None  # Jarvis may speak up by himself about new mail
    wake_word: bool | None = None  # "Hey Jarvis" wakes him


class AutoRequest(BaseModel):
    enabled: bool


class CallReport(BaseModel):
    active: bool = False
    app: str | None = None
    title: str | None = None  # the calling app's front window, when the app may read window titles
    titles: list[str] = Field(default_factory=list)


class AppReport(BaseModel):
    call: CallReport = Field(default_factory=CallReport)
    headphones: bool = False
    permissions: dict[str, str] = Field(default_factory=dict)
    ear_state: str = "off"


class PlayingRequest(BaseModel):
    id: str
    playing: bool


def _local_origin(origin: str | None) -> bool:
    """True for no Origin header (same-origin GETs, the app, command-line tools) or one of ours."""
    if not origin:
        return True
    return any(origin.startswith(f"http://{host}:") or origin == f"http://{host}" for host in LOCAL_HOSTS)


def today(config: Config, runtime: Runtime | None = None) -> dict:
    from jarvis import notes

    now = datetime.now().astimezone()
    start = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    with open_store() as store:
        known = project_registry.load(store, config)
        files = store.stats()["folders"]
        meetings = live.agenda(store, config.primary_account.email.lower(), known, start, start + 86_400)
    return {
        "date": now.date().isoformat(),
        "meetings": meetings,
        "engagements": [
            {"name": p.name, "people": list(p.people), "folders": len(p.folders), "files": files.get(p.name, 0)}
            for p in known
        ],
        "brief": runtime.brief if runtime else None,
        "notes": notes.recent(5),
    }


def create_app(config: Config, *, background: bool = False) -> FastAPI:
    """``background`` turns on the daemon's own work: regular syncs, the brief, notes recovery."""
    runtime = Runtime(config)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if background:
            runtime.start_background()
        yield
        await runtime.shutdown()

    app = FastAPI(title="Jarvis", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.runtime = runtime

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):
        if not _local_origin(request.headers.get("origin")):
            return JSONResponse({"detail": "This server only answers its own interface."}, status_code=403)
        return await call_next(request)

    @app.get("/api/state")
    async def get_state() -> dict:
        return runtime.state()

    @app.post("/api/app")
    async def app_report(body: AppReport) -> dict:
        titles = [title for title in [body.call.title, *body.call.titles] if title]
        await runtime.autopilot.report(
            body.call.active, body.call.app, body.headphones, body.permissions,
            titles=list(dict.fromkeys(titles)), ear_state=body.ear_state,
        )
        runtime.update_ear()  # a call that began closes Jarvis's own ear at once
        return runtime.pulse()

    @app.post("/api/voice/playing")
    async def voice_playing(body: PlayingRequest) -> dict:
        runtime.speaking(body.id, body.playing)
        return {"ok": True}

    @app.get("/api/reminders")
    async def get_reminders() -> list[dict]:
        with open_store() as store:
            return [
                {"id": item["id"], "due": dashboard.iso(item["due_ts"]), "text": item["text"]}
                for item in store.reminders()
            ]

    @app.get("/api/promises")
    async def get_promises() -> list[dict]:
        return dashboard.promises()

    @app.get("/api/engagements/{name}")
    async def get_engagement(name: str) -> dict:
        found = dashboard.engagement(config, name)
        if found is None:
            raise HTTPException(404, "There is no engagement with that name.")
        return found

    @app.post("/api/auto")
    async def set_auto(body: AutoRequest) -> dict:
        runtime.change_settings({"auto": body.enabled})
        return runtime.state()

    @app.get("/api/settings")
    async def get_settings() -> dict:
        return runtime.state()["settings"]

    @app.post("/api/settings")
    async def set_settings(body: SettingsRequest) -> dict:
        return runtime.change_settings(body.model_dump())

    @app.post("/api/meeting/start")
    async def start_meeting(body: StartRequest | None = None) -> dict:
        body = body or StartRequest()
        runtime.autopilot.user_started()
        await runtime.meeting.start(body.project, body.headphones)
        return runtime.state()

    @app.post("/api/meeting/stop")
    async def stop_meeting() -> dict:
        runtime.autopilot.user_stopped()
        await runtime.meeting.stop()
        return runtime.state()

    @app.post("/api/meeting/suggest")
    async def suggest_now() -> dict:
        if not runtime.meeting.suggest():
            raise HTTPException(409, "No meeting is running.")
        return {"ok": True}

    async def talk(body: TalkRequest) -> dict:
        from jarvis.agents import AgentError

        text = (body.text or body.question or "").strip()
        if not text:
            raise HTTPException(422, "Say or type something.")
        try:
            return await runtime.talk(text, spoken=body.spoken, turn_id=body.id)
        except AgentError as exc:
            raise HTTPException(502, str(exc)) from None

    app.post("/api/talk")(talk)
    app.post("/api/ask")(talk)  # the older name

    @app.post("/api/talk/reset")
    async def talk_reset() -> dict:
        await runtime.core.reset()
        runtime.conversation.clear()
        runtime.publish_state()
        return {"ok": True}

    app.post("/api/ask/reset")(talk_reset)

    @app.get("/api/speech/{speech_id}/{seq}")
    async def speech(speech_id: str, seq: int) -> Response:
        try:
            audio = await runtime.speech_audio(speech_id, seq)
        except Exception as exc:  # the voice failed; the words are on screen anyway
            raise HTTPException(503, f"That sentence could not be spoken: {exc}") from None
        if audio is None:
            raise HTTPException(404, "That sentence is no longer kept.")
        return Response(content=audio, media_type="audio/wav")

    @app.websocket("/ws/voice")
    async def voice_socket(websocket: WebSocket) -> None:
        from jarvis import voice

        if not _local_origin(websocket.headers.get("origin")):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        if runtime.meeting.running:  # the microphone belongs to the call
            await websocket.send_json({"error": "busy"})
            await websocket.close()
            return
        await voice.hear(websocket, runtime)

    @app.websocket("/ws/ear")
    async def ear_socket(websocket: WebSocket) -> None:
        """The microphone while nothing else has it, for Jarvis's name: frames as for /ws/voice, for as long as the
        app keeps it open. Turned away with {"error": "busy"} during a call and {"error": "unavailable"} when the
        ear is not wanted; the app then waits a while before it tries again."""
        if not _local_origin(websocket.headers.get("origin")):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        refusal = runtime.ear_refusal()
        if refusal is None:
            await runtime.ear.serve(websocket)
        else:
            await websocket.send_json({"error": refusal})
        try:
            await websocket.close()
        except Exception:  # the app closed it first
            pass

    @app.get("/api/tasks")
    async def get_tasks() -> list[dict]:
        return [task.summary() for task in runtime.tasks.recent()]

    @app.post("/api/tasks/{task_id}/answer")
    async def answer_task(task_id: str, body: AnswerRequest) -> dict:
        if not body.text.strip() or not runtime.tasks.answer(task_id, body.text.strip()):
            raise HTTPException(409, "That task is not waiting for an answer.")
        return {"ok": True}

    @app.post("/api/tasks/{task_id}/stop")
    async def stop_task(task_id: str) -> dict:
        if not runtime.tasks.stop(task_id):
            raise HTTPException(409, "That task is not running.")
        return {"ok": True}

    @app.post("/api/approvals/{approval_id}")
    async def settle_approval(approval_id: str, body: ApprovalRequest) -> dict:
        if not runtime.approvals.click(approval_id, body.approve, body.scope or ""):
            raise HTTPException(409, "That question is no longer open.")
        return {"ok": True}

    @app.post("/api/cards/{card_id}/dismiss")
    async def dismiss_card(card_id: str) -> dict:
        runtime.drop_card(card_id)
        return {"ok": True}

    @app.get("/api/overview")
    async def get_overview() -> dict:
        return dashboard.overview(runtime)

    @app.get("/api/library")
    async def get_library() -> dict:
        return dashboard.library()

    @app.get("/api/facts")
    async def get_facts() -> list[dict]:
        return dashboard.facts()

    @app.delete("/api/facts/{fact_id}")
    async def delete_fact(fact_id: int) -> dict:
        if not dashboard.forget(fact_id):
            raise HTTPException(404, "There is no fact with that number.")
        return {"ok": True}

    @app.get("/api/activity")
    async def get_activity(limit: int = 200) -> list[dict]:
        return dashboard.activity(max(1, min(limit, 1000)))

    @app.get("/api/today")
    async def get_today() -> dict:
        return today(config, runtime)

    @app.get("/api/notes")
    async def get_notes(limit: int = 10) -> list[dict]:
        from jarvis import notes

        return notes.recent(max(1, min(limit, 50)))

    @app.post("/api/sync")
    async def sync_now() -> dict:
        runtime.spawn(runtime.sync_now())
        return {"ok": True}

    @app.post("/api/reconnect")
    async def reconnect() -> dict:
        from jarvis.google_auth import AuthError

        try:
            return {"account": await runtime.reconnect()}
        except AuthError as exc:
            raise HTTPException(502, str(exc)) from None

    @app.websocket("/ws")
    async def socket(websocket: WebSocket) -> None:
        if not _local_origin(websocket.headers.get("origin")):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        queue = runtime.hub.join()

        async def push() -> None:
            try:
                await websocket.send_json({"type": "state", **runtime.state()})
                while (event := await queue.get()) is not None:  # None: the server is closing
                    await websocket.send_json(event)
            except (WebSocketDisconnect, RuntimeError):
                pass

        async def watch() -> None:
            # The interface never speaks on this socket; this returns when it goes away.
            # Without it a closed window would leave push() waiting on the queue for ever.
            try:
                while (await websocket.receive())["type"] != "websocket.disconnect":
                    pass
            except (WebSocketDisconnect, RuntimeError):
                pass

        tasks = [asyncio.create_task(push()), asyncio.create_task(watch())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            runtime.hub.leave(queue)

    if UI_DIST.is_dir():
        app.mount("/", StaticFiles(directory=str(UI_DIST), html=True), name="ui")
    else:

        @app.get("/")
        async def no_interface() -> PlainTextResponse:
            return PlainTextResponse(
                "The interface has not been built yet. Run: cd ui && npm install && npm run build"
            )

    return app
