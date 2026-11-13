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

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

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


class AskRequest(BaseModel):
    question: str
    id: str | None = None  # echoed on the progress events for this question
    web: bool | None = None  # accepted for older interfaces; the assistant decides by itself


class AutoRequest(BaseModel):
    enabled: bool


class CallReport(BaseModel):
    active: bool = False
    app: str | None = None


class AppReport(BaseModel):
    call: CallReport = Field(default_factory=CallReport)
    headphones: bool = False
    permissions: dict[str, str] = Field(default_factory=dict)


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
        await runtime.autopilot.report(body.call.active, body.call.app, body.headphones, body.permissions)
        return runtime.pulse()

    @app.post("/api/auto")
    async def set_auto(body: AutoRequest) -> dict:
        runtime.autopilot.enabled = body.enabled
        runtime.publish_state()
        return runtime.state()

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

    @app.post("/api/ask")
    async def ask(body: AskRequest) -> dict:
        from jarvis.agents import AgentError

        question = body.question.strip()
        if not question:
            raise HTTPException(422, "Ask something.")
        started = time.monotonic()
        tag = {"id": body.id} if body.id else {}
        try:
            answer = await runtime.assistant.ask(
                question,
                on_step=lambda step: runtime.hub.publish({"type": "ask_step", "text": step[:160], **tag}),
                on_text=lambda text: runtime.hub.publish({"type": "ask_delta", "text": text, **tag}),
            )
        except AgentError as exc:
            raise HTTPException(502, str(exc)) from None
        return {
            "text": answer.text, "web_queries": answer.web_queries, "drafts": answer.drafts,
            "seconds": round(time.monotonic() - started, 1),
        }

    @app.post("/api/ask/reset")
    async def ask_reset() -> dict:
        await runtime.assistant.reset()
        return {"ok": True}

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
