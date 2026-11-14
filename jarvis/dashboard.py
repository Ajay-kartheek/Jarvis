"""What the dashboard shows: what Jarvis knows, what it made and what it did.

Plain reads of the local store, shaped for the interface, and the few things
the user can change from there: what Jarvis remembers, the words it listens
for and how it says names. Nothing here calls a model or the network.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING

from jarvis import dossier
from jarvis import projects as project_registry
from jarvis import vocabulary as vocab
from jarvis.config import Config
from jarvis.store import open_store

if TYPE_CHECKING:
    from jarvis.runtime import Runtime

LIBRARY_KINDS = {"preps": ("prep",), "notes": ("notes",), "briefs": ("brief",), "drafts": ("draft",), "files": ("file", "folder")}
PER_SHELF = 60
FACT_MIN, FACT_MAX = 4, 600  # a fact is 4 to 600 characters, as the remember tool keeps it
WORD_MIN = 2  # a word to teach is 2 to 60 characters, as the learn_word tool takes it (the 60 is vocabulary.MAX_LENGTH)


class DashboardError(ValueError):
    """A change asked for on the dashboard that cannot be made. The message is safe to show."""


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


def _fact(row: dict) -> dict:
    return {"id": row["id"], "text": row["text"], "engagement": row["project"], "at": iso(row["ts"])}


def facts() -> list[dict]:
    with open_store() as store:
        return [_fact(fact) for fact in store.facts(limit=200)]


def remember(config: Config, text: str, engagement: str | None = None) -> dict:
    """Keep a fact the user typed, as the remember tool does. Returns it as ``facts`` lists it.

    Raises DashboardError when it is too short or too long, or when the engagement it is for does not exist.
    A fact with no engagement is a general one.
    """
    text = " ".join(text.split())
    if not FACT_MIN <= len(text) <= FACT_MAX:
        raise DashboardError(f"A fact is {FACT_MIN} to {FACT_MAX} characters long.")
    name = (engagement or "").strip()
    with open_store() as store:
        match = project_registry.find(store, name, config) if name else None
        if name and match is None:
            raise DashboardError("There is no engagement with that name.")
        number = store.fact_add(text, match.name if match else None)
    return _fact({"id": number, "project": match.name if match else None, "text": text, "ts": int(time.time())})


def forget(fact_id: int) -> bool:
    with open_store() as store:
        return store.fact_delete(fact_id)


def vocabulary(config: Config) -> dict:
    """The words Jarvis listens for: the ones the user taught it, and the ones it found itself.

    Each automatic word says where it came from: "engagement", "person", "dossier" or "common". The recogniser gets
    each word once, so a common term that the user taught as well is shown as taught only.
    """
    with open_store() as store:
        return {
            "taught": vocab.taught(store),
            "automatic": [
                {"word": word, "source": source}
                for word, source in vocab.sourced(store, config)
                if source != "taught"
            ],
        }


def teach(word: str) -> str:
    """Add a word to those the user taught Jarvis, under the rules of the learn_word tool. Returns it as kept.

    Raises DashboardError when it is too short or too long, or is an address or a link.
    """
    word = " ".join(word.split())
    if not WORD_MIN <= len(word) <= vocab.MAX_LENGTH or "@" in word or "://" in word:
        raise DashboardError("That is not a word or name to learn.")
    with open_store() as store:
        vocab.teach(store, word)
    return word


def unteach(word: str) -> bool:
    """Take a word off those the user taught. True when it was one of them."""
    with open_store() as store:
        return vocab.unteach(store, word)


def pronunciations() -> list[dict]:
    """How names are said: each as written, and as it is spelt for the voice, in the order they were added."""
    with open_store() as store:
        return [{"written": written, "said": said} for written, said in vocab.pronunciations(store).items()]


def pronounce(written: str, said: str) -> dict:
    """Set how a name is said. Returns the pair as kept.

    Raises DashboardError when either side is empty or too long, or when the list is full and this is a new name.
    """
    written, said = " ".join(written.split()), " ".join(said.split())
    if not (1 <= len(written) <= vocab.MAX_LENGTH and 1 <= len(said) <= vocab.MAX_LENGTH):
        raise DashboardError(f"Each side is 1 to {vocab.MAX_LENGTH} characters long.")
    with open_store() as store:
        known = {name.casefold() for name in vocab.pronunciations(store)}
        if len(known) >= vocab.MAX_PAIRS and written.casefold() not in known:
            raise DashboardError(f"Jarvis keeps up to {vocab.MAX_PAIRS} names. Remove one to add another.")
        vocab.pronounce(store, written, said)
    return {"written": written, "said": said}


def unpronounce(written: str) -> bool:
    """Drop a name from those said in a special way. True when it was one of them."""
    with open_store() as store:
        return vocab.unpronounce(store, written)


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
        "facts": [_fact(fact) for fact in facts],
        "promises": [_promise(row) for row in owed],
    }


def activity(limit: int = 200) -> list[dict]:
    with open_store() as store:
        return [{"at": iso(row["ts"]), "kind": row["kind"], "text": row["text"]} for row in store.activity(limit)]
