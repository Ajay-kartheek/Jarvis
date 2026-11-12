"""The engagements Jarvis works on: a name, the people involved and folders on this Mac.

Projects are added and changed at run time with ``jarvis project``; they live
in the local store. The ``[[projects]]`` entries in config.toml only seed the
list the first time.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from jarvis.config import Config
from jarvis.store import Store


class ProjectError(ValueError):
    """A project command could not be carried out. The message is safe to show."""


@dataclass(frozen=True)
class Project:
    name: str
    people: tuple[str, ...] = ()
    folders: tuple[str, ...] = ()


def _from_row(row: dict) -> Project:
    return Project(row["name"], tuple(row["people"]), tuple(row["folders"]))


def load(store: Store, config: Config | None = None) -> list[Project]:
    """Every project, seeding the registry from config.toml if it is empty."""
    rows = store.project_rows()
    if not rows and config is not None and config.projects:
        for seed in config.projects:
            store.project_save(seed.name, list(seed.people), [])
        rows = store.project_rows()
    return [_from_row(row) for row in rows]


def find(store: Store, name: str, config: Config | None = None) -> Project | None:
    wanted = name.strip().lower()
    return next((p for p in load(store, config) if p.name.lower() == wanted), None)


def add(store: Store, name: str, *, folders: list[str] | None = None,
        people: list[str] | None = None, config: Config | None = None) -> Project:
    """Create a project, or add folders and people to an existing one."""
    name = name.strip()
    if not name:
        raise ProjectError("A project needs a name.")
    resolved = []
    for folder in folders or []:
        path = Path(folder).expanduser().resolve()
        if not path.is_dir():
            raise ProjectError(f"{folder} is not a folder.")
        resolved.append(str(path))
    existing = find(store, name, config)
    merged_folders = list(existing.folders) if existing else []
    merged_people = list(existing.people) if existing else []
    merged_folders += [folder for folder in resolved if folder not in merged_folders]
    known = {person.lower() for person in merged_people}
    merged_people += [p.strip() for p in people or [] if p.strip() and p.strip().lower() not in known]
    final_name = existing.name if existing else name
    store.project_save(final_name, merged_people, merged_folders)
    return Project(final_name, tuple(merged_people), tuple(merged_folders))


def drop_folder(store: Store, name: str, folder: str, config: Config | None = None) -> Project:
    project = find(store, name, config)
    if project is None:
        raise ProjectError(f"There is no project called {name}.")
    target = str(Path(folder).expanduser().resolve())
    if target not in project.folders:
        raise ProjectError(f"{project.name} does not use {folder}.")
    remaining = [f for f in project.folders if f != target]
    store.project_save(project.name, list(project.people), remaining)
    stale = [path for path in store.local_known(project.name) if path == target or path.startswith(target + "/")]
    store.local_delete(project.name, stale)
    return Project(project.name, project.people, tuple(remaining))


def remove(store: Store, name: str, config: Config | None = None) -> bool:
    project = find(store, name, config)
    return bool(project) and store.project_delete(project.name)
