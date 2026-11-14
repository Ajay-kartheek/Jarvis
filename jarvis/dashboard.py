"""What the dashboard shows: what Jarvis knows, what it made and what it did.

Plain reads of the local store, shaped for the interface. Nothing here calls a
model or the network.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from jarvis import dossier
from jarvis import projects as project_registry
from jarvis.config import Config
from jarvis.store import open_store

if TYPE_CHECKING:
    from jarvis.runtime import Runtime

LIBRARY_KINDS = {"preps": ("prep",), "notes": ("notes",), "briefs": ("brief",), "drafts": ("draft",), "files": ("file", "folder")}
PER_SHELF = 60


def iso(ts: int | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="minutes")


def _item(row: dict) -> dict:
    return {
        "id": row["id"], "kind": row["kind"], "title": row["title"], "engagement": row["project"],
        "at": iso(row["ts"]), "body": row["body"] or None, "path": row["path"], "link": row["link"],
    }


def overview(runtime: "Runtime") -> dict:
    with open_store() as store:
        stats = store.stats()
        made = store.artifact_counts()
    counts = {"mail": 0, "events": 0, "files": 0}
    sources = []
    for account, info in stats["accounts"].items():
        counts["mail"] += info["mail"]["count"]
        counts["events"] += info["events"]["count"]
        counts["files"] += info["files"]["count"]
        for name, run in info["last_runs"].items():
            sources.append({
                "name": name, "account": account,
                "last_run": iso(run.get("finished_at") or run.get("started_at")) if run else None,
                "status": run.get("status") if run else "never",
                "detail": (run.get("error") or "") if run else "",
            })
    for name, files in sorted(stats["folders"].items()):
        sources.append({"name": "folder", "account": name, "last_run": runtime.last_sync, "status": "ok", "detail": f"{files} files"})
    state = runtime.state()
    return {
        "status": state["status"], "last_sync": state["last_sync"], "syncing": state["syncing"],
        "notices": state["notices"],
        "counts": {
            **counts, "notes": made.get("notes", 0), "preps": made.get("prep", 0), "drafts": made.get("draft", 0),
            "created": made.get("file", 0) + made.get("folder", 0),
        },
        "sources": sources, "engagements": state["engagements"], "tasks": state["tasks"],
    }


def library() -> dict:
    with open_store() as store:
        shelves = {}
        for shelf, kinds in LIBRARY_KINDS.items():
            rows = [row for kind in kinds for row in store.artifacts(kind, limit=PER_SHELF)]
            rows.sort(key=lambda row: (row["ts"], row["id"]), reverse=True)
            shelves[shelf] = [_item(row) for row in rows[:PER_SHELF]]
    return shelves


def facts() -> list[dict]:
    with open_store() as store:
        return [
            {"id": fact["id"], "text": fact["text"], "engagement": fact["project"], "at": iso(fact["ts"])}
            for fact in store.facts(limit=200)
        ]


def forget(fact_id: int) -> bool:
    with open_store() as store:
        return store.fact_delete(fact_id)


def _promise(row: dict) -> dict:
    return {key: row[key] for key in ("id", "engagement", "owner", "text", "due", "status")}


def promises() -> list[dict]:
    """Who owes what, open ones first."""
    with open_store() as store:
        return [_promise(row) for row in store.promises()]


def engagement(config: Config, name: str) -> dict | None:
    """Everything Jarvis keeps on one engagement, or None when there is no such engagement."""
    with open_store() as store:
        project = project_registry.find(store, name, config)
        if project is None:
            return None
        files = len(store.local_known(project.name))
        facts = [fact for fact in store.facts(project.name, limit=200) if fact["project"] == project.name]
        owed = store.promises(project.name)
    return {
        "name": project.name, "people": list(project.people), "folders": len(project.folders), "files": files,
        "dossier": dossier.read(project.name) or None,
        "facts": [{"id": fact["id"], "text": fact["text"], "engagement": fact["project"], "at": iso(fact["ts"])} for fact in facts],
        "promises": [_promise(row) for row in owed],
    }


def activity(limit: int = 200) -> list[dict]:
    with open_store() as store:
        return [{"at": iso(row["ts"]), "kind": row["kind"], "text": row["text"]} for row in store.activity(limit)]
