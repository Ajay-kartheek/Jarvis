"""Folders and files Jarvis creates on this Mac.

Jarvis may make new folders and new files anywhere under the home folder that
is ordinary user space. It never deletes anything, and it never overwrites a
file it did not write itself. Everything it makes is recorded, so the
dashboard can show it and a later request can update it.

A working folder for an engagement is a folder with a ``CLAUDE.md`` brief in
it and a fresh git repository, registered as that engagement's folder so its
contents become part of what Jarvis knows.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from jarvis import projects
from jarvis.config import Config
from jarvis.store import Store

MAX_FILE_CHARS = 400_000
# Parts of the home folder that are not the user's working space.
OFF_LIMITS = ("Library", "Applications", "Music", "Movies", "Pictures", "Public")


class WorkspaceError(ValueError):
    """The folder or file could not be made. The message is safe to show the user."""


def expand(path: str) -> Path:
    """``path`` as an absolute path: ``~`` and bare names are taken from the home folder."""
    text = path.strip().removeprefix("file://")
    if text == "~" or text.startswith("~/"):
        return Path.home() / text[2:]
    target = Path(text)
    return target if target.is_absolute() else Path.home() / target


def resolve(path: str) -> Path:
    """An absolute path under the home folder that Jarvis is allowed to create."""
    if not path.strip():
        raise WorkspaceError("A path is needed.")
    target = expand(path)
    target = Path(*target.parts)  # normalise without touching the disk
    if ".." in target.parts:
        raise WorkspaceError("The path must not contain '..'.")
    home = Path.home()
    try:
        inside = target.relative_to(home)
    except ValueError:
        raise WorkspaceError("Jarvis only creates things inside your home folder.") from None
    if not inside.parts:
        raise WorkspaceError("That is the home folder itself.")
    if inside.parts[0] in OFF_LIMITS:
        raise WorkspaceError(f"Jarvis does not create things in {inside.parts[0]}.")
    if any(part.startswith(".") for part in inside.parts[:-1]) or (
        inside.parts[-1].startswith(".") and inside.parts[-1] not in (".gitignore",)
    ):
        raise WorkspaceError("Jarvis does not create hidden files or write inside hidden folders.")
    # A link could lead outside the home folder: check where an existing parent really is.
    existing = target
    while not existing.exists():
        existing = existing.parent
    if home.resolve() not in (existing.resolve(), *existing.resolve().parents):
        raise WorkspaceError("That path leads outside your home folder.")
    return target


def create_folder(store: Store, path: str, *, project: str | None = None) -> Path:
    target = resolve(path)
    if target.exists() and not target.is_dir():
        raise WorkspaceError(f"{target} exists and is not a folder.")
    made = not target.exists()
    target.mkdir(parents=True, exist_ok=True)
    if made:
        store.artifact_add("folder", target.name, project=project, path=str(target))
        store.log("created", f"Folder {target}")
    return target


def write_file(store: Store, path: str, content: str, *, project: str | None = None) -> Path:
    """Write a new text file, or update one Jarvis wrote earlier. Never touches anyone else's file."""
    target = resolve(path)
    if len(content) > MAX_FILE_CHARS:
        raise WorkspaceError(f"That is too long for one file ({len(content):,} characters).")
    if target.is_dir():
        raise WorkspaceError(f"{target} is a folder.")
    existed = target.exists()
    if existed and not store.artifact_made(str(target)):
        raise WorkspaceError(
            f"{target.name} already exists and was not written by Jarvis, so it is left alone. Use another name."
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    if not existed:
        store.artifact_add("file", target.name, project=project, path=str(target))
    store.log("created", f"{'Updated' if existed else 'File'} {target}")
    return target


def folder_name(name: str) -> str:
    safe = re.sub(r"[^\w\- ]+", " ", name).strip()
    return re.sub(r"\s+", "-", safe) or "Engagement"


def create_workspace(store: Store, config: Config, name: str, brief: str, *,
                     people: list[str] | None = None, folder: str | None = None) -> dict:
    """Set up a working folder for an engagement and make it part of what Jarvis knows.

    The folder gets a ``CLAUDE.md`` (what Claude Code reads when it is started
    there) and an empty git repository, and is registered as the engagement's
    folder. An engagement that does not exist yet is created.
    """
    name = name.strip()
    if not name:
        raise WorkspaceError("The engagement needs a name.")
    target = resolve(folder or f"~/Documents/{folder_name(name)}")
    known = projects.find(store, name, config)
    already_ours = known is not None and str(target.resolve()) in known.folders
    if target.exists() and any(target.iterdir()) and not already_ours and not store.artifact_made(str(target)):
        raise WorkspaceError(f"{target} already exists and is not empty. Name another folder.")
    made = not target.exists()
    target.mkdir(parents=True, exist_ok=True)
    if made:
        store.artifact_add("folder", target.name, project=name, path=str(target), meta={"workspace": True})
    git = "already a git repository"
    if not (target / ".git").exists():
        result = subprocess.run(["git", "init", "--quiet", str(target)], capture_output=True, text=True)
        git = "git repository started" if result.returncode == 0 else "git could not be started"
    wrote = None
    if brief.strip():
        try:
            wrote = write_file(store, str(target / "CLAUDE.md"), brief.strip() + "\n", project=name)
        except WorkspaceError:
            wrote = None  # the user has their own CLAUDE.md there: leave it
    try:
        project = projects.add(store, name, folders=[str(target)], people=people or [], config=config)
    except projects.ProjectError as exc:
        raise WorkspaceError(str(exc)) from None
    store.log("created", f"Working folder for {project.name}: {target}")
    return {
        "name": project.name, "path": str(target), "claude_md": str(wrote) if wrote else None,
        "git": git, "new_engagement": known is None,
    }
