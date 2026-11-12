"""The local server behind the Jarvis interface.

Serves the built interface from ``ui/dist`` and feeds it over REST and one
WebSocket. It listens on 127.0.0.1 only, and refuses requests that come from
any other web page, so a site open in the browser cannot read the transcript.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from jarvis import meeting as live
from jarvis import paths
from jarvis import projects as project_registry
from jarvis.config import Config
from jarvis.store import open_store

UI_DIST = Path(__file__).resolve().parent.parent / "ui" / "dist"
LOCAL_HOSTS = ("127.0.0.1", "localhost")


class StartRequest(BaseModel):
    project: str | None = None
    headphones: bool = False


class AskRequest(BaseModel):
    question: str
    web: bool = False


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

    def publish(self, event: dict) -> None:
        for queue in self._queues:
            if queue.full():  # a stalled window must not hold up the meeting
                queue.get_nowait()
            queue.put_nowait(event)


class MeetingController:
    def __init__(self, config: Config, hub: Hub) -> None:
        self.config = config
        self.hub = hub
        self.status = "idle"
        self.meeting: dict | None = None
        self._task: asyncio.Task | None = None
        self._inbox: asyncio.Queue | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def state(self) -> dict:
        with open_store() as store:
            known = project_registry.load(store, self.config)
            files = store.stats()["folders"]
        return {
            "status": self.status,
            "meeting": self.meeting,
            "engagements": [
                {"name": p.name, "people": list(p.people), "folders": len(p.folders), "files": files.get(p.name, 0)}
                for p in known
            ],
        }

    def _set(self, status: str) -> None:
        self.status = status
        self.hub.publish({"type": "state", **self.state()})

    def _on_event(self, event: dict) -> None:
        kind = event["type"]
        if kind == "meeting":
            self.meeting = {
                "title": event["title"], "engagement": event["engagement"],
                "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            self._set("preparing")
            return
        if kind == "ready":
            self._set("listening")
            return
        self.hub.publish(event)
        if kind == "thinking":
            self._set("thinking")
        elif kind in ("suggestion", "no_suggestion"):
            self._set("listening")

    async def start(self, project: str | None, headphones: bool) -> None:
        if self.running:
            return
        folder = paths.ensure_data_dir() / "transcripts"
        folder.mkdir(exist_ok=True)
        out = folder / f"{datetime.now():%Y-%m-%d-%H%M}.jsonl"
        self._inbox = asyncio.Queue()
        self.status = "preparing"
        self._task = asyncio.create_task(self._run(project, headphones, out))

    async def _run(self, project: str | None, headphones: bool, out: Path) -> None:
        try:
            await live.run(
                self.config, project_name=project, headphones=headphones, out=out,
                emit=self._on_event, inbox=self._inbox,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # show the reason in the interface instead of dying silently
            self.hub.publish({"type": "info", "text": f"The meeting stopped: {exc}"})
        finally:
            self.meeting = None
            self._inbox = None
            self._set("idle")

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


def _local_origin(origin: str | None) -> bool:
    """True for no Origin header (same-origin GETs, command-line tools) or one of ours."""
    if not origin:
        return True
    return any(origin.startswith(f"http://{host}:") or origin == f"http://{host}" for host in LOCAL_HOSTS)


def today(config: Config) -> dict:
    now = datetime.now().astimezone()
    start = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    with open_store() as store:
        known = project_registry.load(store, config)
        files = store.stats()["folders"]
        rows = store.db.execute(
            "SELECT summary, start_ts, end_ts, attendees FROM calendar_events WHERE account=? "
            "AND all_day=0 AND COALESCE(status, '') != 'cancelled' "
            "AND COALESCE(event_type, 'default') = 'default' AND start_ts >= ? AND start_ts < ? "
            "ORDER BY start_ts",
            (config.primary_account.email.lower(), start, start + 86_400),
        ).fetchall()
    import json

    meetings = []
    for row in rows:
        people = [
            person.get("name") or person.get("email", "")
            for person in json.loads(row["attendees"] or "[]")
            if not person.get("resource") and not person.get("self")
        ]
        project = live.guess_project(known, {"title": row["summary"], "people": people})
        meetings.append({
            "title": row["summary"],
            "start": datetime.fromtimestamp(row["start_ts"]).astimezone().isoformat(timespec="minutes"),
            "end": datetime.fromtimestamp(row["end_ts"]).astimezone().isoformat(timespec="minutes"),
            "engagement": project.name if project else None,
            "people": [p for p in people if p][:12],
        })
    return {
        "date": now.date().isoformat(),
        "meetings": meetings,
        "engagements": [
            {"name": p.name, "people": list(p.people), "folders": len(p.folders), "files": files.get(p.name, 0)}
            for p in known
        ],
    }


def create_app(config: Config) -> FastAPI:
    app = FastAPI(title="Jarvis", docs_url=None, redoc_url=None, openapi_url=None)
    hub = Hub()
    controller = MeetingController(config, hub)

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):
        if not _local_origin(request.headers.get("origin")):
            return JSONResponse({"detail": "This server only answers its own interface."}, status_code=403)
        return await call_next(request)

    @app.get("/api/state")
    async def get_state() -> dict:
        return controller.state()

    @app.post("/api/meeting/start")
    async def start_meeting(body: StartRequest) -> dict:
        await controller.start(body.project, body.headphones)
        return controller.state()

    @app.post("/api/meeting/stop")
    async def stop_meeting() -> dict:
        await controller.stop()
        return controller.state()

    @app.post("/api/meeting/suggest")
    async def suggest_now() -> dict:
        if not controller.suggest():
            raise HTTPException(409, "No meeting is running.")
        return {"ok": True}

    @app.post("/api/ask")
    async def ask(body: AskRequest) -> dict:
        from jarvis import agents

        started = time.monotonic()
        try:
            answer = await agents.ask_async(body.question, config, web=body.web)
        except agents.AgentError as exc:
            raise HTTPException(502, str(exc)) from None
        return {
            "text": answer.text, "web_queries": answer.web_queries,
            "seconds": round(time.monotonic() - started, 1),
        }

    @app.get("/api/today")
    async def get_today() -> dict:
        return today(config)

    @app.websocket("/ws")
    async def socket(websocket: WebSocket) -> None:
        if not _local_origin(websocket.headers.get("origin")):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        queue = hub.join()
        try:
            await websocket.send_json({"type": "state", **controller.state()})
            while True:
                await websocket.send_json(await queue.get())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            hub.leave(queue)

    @app.on_event("shutdown")
    async def shutdown() -> None:
        await controller.stop()

    if UI_DIST.is_dir():
        app.mount("/", StaticFiles(directory=str(UI_DIST), html=True), name="ui")
    else:

        @app.get("/")
        async def no_interface() -> PlainTextResponse:
            return PlainTextResponse(
                "The interface has not been built yet. Run: cd ui && npm install && npm run build"
            )

    return app
