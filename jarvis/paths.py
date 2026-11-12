"""Where Jarvis keeps things at run time.

Secrets and mirrored data live outside the project folder. Nothing here
creates a directory at import time; commands call the ``ensure_*`` helpers
when they actually need the path.
"""

from __future__ import annotations

import os
from pathlib import Path

SECRETS_DIR_MODE = 0o700
SECRET_FILE_MODE = 0o600


def secrets_dir() -> Path:
    return Path.home() / ".config" / "jarvis"


def data_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "Jarvis"


def google_client_file() -> Path:
    return secrets_dir() / "google_client.json"


def ensure_secrets_dir() -> Path:
    path = secrets_dir()
    path.mkdir(mode=SECRETS_DIR_MODE, parents=True, exist_ok=True)
    # mkdir's mode is masked by umask and ignored for an existing directory.
    os.chmod(path, SECRETS_DIR_MODE)
    return path


def ensure_data_dir() -> Path:
    path = data_dir()
    path.mkdir(mode=SECRETS_DIR_MODE, parents=True, exist_ok=True)
    os.chmod(path, SECRETS_DIR_MODE)
    return path


def write_secret_file(path: Path, content: bytes) -> None:
    """Write ``content`` to ``path`` readable by the owner only."""
    ensure_secrets_dir()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, SECRET_FILE_MODE)
    try:
        os.write(fd, content)
    finally:
        os.close(fd)
    os.chmod(path, SECRET_FILE_MODE)
