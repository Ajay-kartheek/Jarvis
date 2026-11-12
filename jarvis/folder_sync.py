"""Index the folders of each project into the local store.

Reads documents and source code so that ``jarvis ask`` and the meeting
answers can use them. What is left out, on purpose:

- anything git ignores (when the folder is a git repository);
- dependency, build and cache folders;
- files that look like secrets (``.env``, keys, credential downloads);
- binaries, lock files and anything too large to be worth reading.

Only files that changed since the last run are read again.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from fnmatch import fnmatch
from pathlib import Path

from jarvis.projects import Project
from jarvis.store import Store
from jarvis.textutil import cap, tidy

SKIP_DIRS = frozenset({
    ".git", "node_modules", "site-packages", "__pycache__", ".venv", "venv", "env", "dist",
    "build", ".next", ".nuxt", "target", ".idea", ".vscode", ".terraform", ".cache", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "coverage", ".gradle", "Pods", "DerivedData", ".claude",
})
TEXT_EXTENSIONS = frozenset({
    ".md", ".txt", ".rst", ".adoc", ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".java",
    ".kt", ".go", ".rs", ".swift", ".c", ".h", ".cpp", ".cs", ".rb", ".php", ".sql", ".sh",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".tf", ".html", ".css", ".scss", ".graphql",
    ".proto", ".json", ".csv", ".xml",
})
DOCUMENT_EXTENSIONS = frozenset({".docx", ".xlsx", ".pptx", ".pdf"})
# Data-like text is only worth reading when it is small.
SMALL_ONLY = frozenset({".json", ".csv", ".xml"})
SMALL_ONLY_BYTES = 100_000
MAX_TEXT_BYTES = 400_000
MAX_DOCUMENT_BYTES = 25_000_000
SKIP_NAMES = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.crt", "*.cer", "id_rsa*", "id_ed25519*",
    "client_secret*.json", "credentials*.json", "*service-account*.json", "*service_account*.json",
    ".npmrc", ".pypirc", ".netrc", "*.tfvars", "*.tfstate*", "*.lock", "package-lock.json",
    "pnpm-lock.yaml", "*.min.js", "*.min.css", "*.map", "~$*",
)
BODY_CAP = 60_000
MAX_FILES_PER_FOLDER = 6_000
SHEET_ROWS = 200
SHEET_COLUMNS = 26
PDF_PAGES = 80

Progress = Callable[[str], None]


def wanted(path: Path, size: int) -> bool:
    name = path.name
    if any(fnmatch(name, pattern) for pattern in SKIP_NAMES):
        return False
    suffix = path.suffix.lower()
    if suffix in DOCUMENT_EXTENSIONS:
        return 0 < size <= MAX_DOCUMENT_BYTES
    if suffix not in TEXT_EXTENSIONS:
        return False
    if suffix in SMALL_ONLY and size > SMALL_ONLY_BYTES:
        return False
    return 0 < size <= MAX_TEXT_BYTES


def list_files(folder: Path) -> list[Path]:
    """Files under ``folder`` worth considering, honouring .gitignore where there is one."""
    found: list[Path] = []
    _collect(folder, found)
    return found


def _git_listing(folder: Path) -> list[str] | None:
    if not (folder / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(folder), "ls-files", "-co", "--exclude-standard", "-z"],
            capture_output=True, check=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return [item for item in result.stdout.decode("utf-8", errors="replace").split("\0") if item]


def _collect(folder: Path, found: list[Path]) -> None:
    listing = _git_listing(folder)
    if listing is None:
        for root, dirs, files in os.walk(folder):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            # A repository inside a plain folder brings its own ignore rules.
            for name in list(dirs):
                if (Path(root) / name / ".git").exists():
                    dirs.remove(name)
                    _collect(Path(root) / name, found)
            found.extend(Path(root) / name for name in sorted(files))
        return
    for item in listing:
        parts = Path(item).parts
        if any(part in SKIP_DIRS for part in parts):
            continue
        path = folder / item
        if item.endswith("/"):  # an untracked repository nested inside this one
            if path.is_dir():
                _collect(path, found)
        else:
            found.append(path)


def read_text(path: Path) -> str | None:
    """The text of a file, or None if it should not be indexed."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".docx":
            return _docx(path)
        if suffix == ".xlsx":
            return _xlsx(path)
        if suffix == ".pptx":
            return _pptx(path)
        if suffix == ".pdf":
            return _pdf(path)
        raw = path.read_bytes()
    except Exception:  # unreadable, encrypted or damaged: skip it
        return None
    if b"\0" in raw[:4096]:
        return None
    text = raw.decode("utf-8", errors="replace")
    if "PRIVATE KEY-----" in text:
        return None
    return text


def _docx(path: Path) -> str:
    from docx import Document

    document = Document(str(path))
    parts = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text.strip() for cell in row.cells))
    return "\n".join(parts)


def _xlsx(path: Path) -> str:
    from openpyxl import load_workbook

    workbook = load_workbook(str(path), read_only=True, data_only=True)
    parts = []
    try:
        for sheet in workbook.worksheets:
            lines = []
            for row in sheet.iter_rows(max_row=SHEET_ROWS, max_col=SHEET_COLUMNS, values_only=True):
                cells = ["" if value is None else str(value) for value in row]
                line = " | ".join(cells).rstrip(" |")
                if line.strip(" |"):
                    lines.append(line)
            if lines:
                parts.append(f"## Tab: {sheet.title}\n" + "\n".join(lines))
    finally:
        workbook.close()
    return "\n\n".join(parts)


def _pptx(path: Path) -> str:
    from pptx import Presentation

    parts = []
    for number, slide in enumerate(Presentation(str(path)).slides, start=1):
        texts = [shape.text_frame.text for shape in slide.shapes if shape.has_text_frame]
        body = "\n".join(text for text in texts if text.strip())
        if body:
            parts.append(f"## Slide {number}\n{body}")
    return "\n\n".join(parts)


def _pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = [page.extract_text() or "" for page in reader.pages[:PDF_PAGES]]
    return "\n\n".join(pages)


def sync_project(store: Store, project: Project, *, progress: Progress = print) -> dict:
    summary = {"folders": len(project.folders), "indexed": 0, "unchanged": 0, "removed": 0,
               "skipped": 0, "missing_folders": 0}
    known = store.local_known(project.name)
    seen: set[str] = set()
    for folder_text in project.folders:
        folder = Path(folder_text)
        if not folder.is_dir():
            summary["missing_folders"] += 1
            progress(f"folders: {project.name}: {folder} is missing, skipped")
            # Keep what was indexed from it; the disk may only be unmounted.
            seen.update(path for path in known if path.startswith(folder_text + "/"))
            continue
        candidates = []
        for path in list_files(folder):
            try:
                info = path.stat()
            except OSError:
                continue
            if wanted(path, info.st_size):
                candidates.append((path, info))
        if len(candidates) > MAX_FILES_PER_FOLDER:
            progress(
                f"folders: {project.name}: {folder} has {len(candidates)} readable files; "
                f"taking the {MAX_FILES_PER_FOLDER} most recently changed"
            )
            candidates.sort(key=lambda item: item[1].st_mtime, reverse=True)
            candidates = candidates[:MAX_FILES_PER_FOLDER]
        for count, (path, info) in enumerate(candidates, start=1):
            key = str(path)
            seen.add(key)
            if known.get(key) == (info.st_mtime, info.st_size):
                summary["unchanged"] += 1
                continue
            text = read_text(path)
            if text is None or not text.strip():
                summary["skipped"] += 1
                seen.discard(key)
                continue
            body, _ = cap(tidy(text), BODY_CAP)
            title = f"{folder.name}/{path.relative_to(folder)}"
            store.local_upsert(project.name, key, title, body, info.st_mtime, info.st_size)
            summary["indexed"] += 1
            if count % 500 == 0:
                progress(f"folders: {project.name}: {count} of {len(candidates)} files checked")
    summary["removed"] = store.local_delete(project.name, sorted(set(known) - seen))
    return summary


def sync(store: Store, projects: list[Project], *, progress: Progress = print) -> dict:
    """Index every project's folders. Returns a summary per project."""
    results = {}
    for project in projects:
        if not project.folders:
            continue
        results[project.name] = sync_project(store, project, progress=progress)
        progress(f"folders: {project.name}: {results[project.name]}")
    return results
