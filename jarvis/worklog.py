"""What the user has been working on in an engagement: recent commits and recently changed files.

Mail and notes say what was asked and promised; the engagement's folders say
what was actually built. This reads the git history of those folders (and of
repositories one or two levels inside them) and the list of files changed
since a date. It only reads: ``git log`` and ``git status``, never anything
that writes.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path

from jarvis.projects import Project
from jarvis.store import LOCAL_ACCOUNT, Store

REPOSITORY_DEPTH = 2
SKIP = frozenset({"node_modules", ".venv", "venv", "dist", "build", "target", ".git"})
COMMITS = 30
FILES_PER_COMMIT = 12
UNCOMMITTED = 25
CHANGED_FILES = 40
MAX_CHARS = 9_000
GIT_SECONDS = 15


def repositories(folder: Path) -> list[Path]:
    """Git repositories at ``folder`` and up to two levels inside it."""
    found = []

    def look(path: Path, depth: int) -> None:
        if (path / ".git").exists():
            found.append(path)
        if depth == 0:
            return
        try:
            children = sorted(child for child in path.iterdir() if child.is_dir())
        except OSError:
            return
        for child in children:
            if child.name not in SKIP and not child.name.startswith("."):
                look(child, depth - 1)

    look(folder, REPOSITORY_DEPTH)
    return found


def _git(repository: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), *arguments], capture_output=True, text=True, timeout=GIT_SECONDS,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},  # reading must not leave lock files behind
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout if result.returncode == 0 else ""


def commits(repository: Path, since: datetime) -> list[str]:
    """One block of lines per commit since ``since``: date and subject, then the files it touched."""
    raw = _git(
        repository, "log", f"--since={since:%Y-%m-%d %H:%M}", "--no-merges", f"--max-count={COMMITS}",
        "--date=short", "--pretty=format:@@%ad  %s", "--name-status",
    )
    lines: list[str] = []
    files = 0
    for line in raw.splitlines():
        if line.startswith("@@"):
            lines.append("- " + line[2:].strip())
            files = 0
        elif line.strip():
            files += 1
            if files <= FILES_PER_COMMIT:
                lines.append("    " + " ".join(line.split("\t")))
            elif files == FILES_PER_COMMIT + 1:
                lines.append("    ...")
    return lines


def uncommitted(repository: Path) -> list[str]:
    lines = [line.rstrip() for line in _git(repository, "status", "--short").splitlines() if line.strip()]
    extra = len(lines) - UNCOMMITTED
    return lines[:UNCOMMITTED] + ([f"... and {extra} more"] if extra > 0 else [])


def report(store: Store, project: Project, since: datetime) -> str:
    """What changed in ``project``'s folders since ``since``, as text for a model to read."""
    parts = [f"Work in {project.name} since {since:%A %d %B %Y}"]
    for folder_text in project.folders:
        folder = Path(folder_text)
        if not folder.is_dir():
            parts.append(f"{folder} is not there right now.")
            continue
        for repository in repositories(folder):
            name = folder.name if repository == folder else f"{folder.name}/{repository.relative_to(folder)}"
            done = commits(repository, since)
            pending = uncommitted(repository)
            if done:
                count = sum(1 for line in done if line.startswith("- "))
                parts.append(f"Repository {name}: {count} commits\n" + "\n".join(done))
            if pending:
                parts.append(f"Not committed yet in {name}:\n" + "\n".join("    " + line for line in pending))
            if not done and not pending:
                parts.append(f"Repository {name}: nothing since then.")
    changed = store.db.execute(
        "SELECT doc_id, title, ts FROM docs WHERE account=? AND kind='file' AND project=? AND ts >= ? "
        "AND source_id NOT LIKE 'note:%' AND source_id NOT LIKE 'prep:%' ORDER BY ts DESC LIMIT ?",
        (LOCAL_ACCOUNT, project.name, int(since.timestamp()), CHANGED_FILES),
    ).fetchall()
    if changed:
        parts.append(
            "Files changed since then, newest first (read one with its id):\n" + "\n".join(
                f"- [{row['doc_id']}] {datetime.fromtimestamp(row['ts']):%Y-%m-%d %H:%M}  {row['title']}"
                for row in changed
            )
        )
    elif len(parts) == 1:
        parts.append("Nothing in its folders has changed since then.")
    return "\n\n".join(parts)[:MAX_CHARS]
