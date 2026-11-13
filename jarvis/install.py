"""Set Jarvis up to run by itself.

``jarvis install`` copies the Jarvis app to ~/Applications, tells it how to
start the background service, and registers it with launchd so it starts at
login and comes back if it crashes. ``jarvis uninstall`` undoes all of that
and leaves the data alone.

The app is copied out of the project folder because launchd cannot start a
program that lives under ~/Documents.
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path

from jarvis import paths

LABEL = "com.ajay.jarvis"
PROJECT = Path(__file__).resolve().parent.parent
BUILT_APP = PROJECT / "app" / "build" / "Jarvis.app"
DEFAULT_PORT = 8765
# Apps whose use of the microphone counts as a call, as bundle-id prefixes: the
# usual work tools, and browsers for calls that run in a tab. Personal calling
# apps (FaceTime, WhatsApp and the like) are left out on purpose.
CALL_APPS = (
    "com.microsoft.teams",
    "us.zoom.xos",
    "com.cisco.webexmeetingsapp",
    "com.webex.meetingmanager",
    "com.tinyspeck.slackmacgap",
    "com.google.Chrome",
    "com.apple.Safari",
    "com.microsoft.edgemac",
    "company.thebrowser.Browser",
    "org.mozilla.firefox",
    "com.brave.Browser",
)

Progress = Callable[[str], None]


class InstallError(RuntimeError):
    """Set-up could not finish. The message is safe to show the user."""


def installed_app() -> Path:
    return Path.home() / "Applications" / "Jarvis.app"


def agent_file() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def app_config_file() -> Path:
    return paths.data_dir() / "app.json"


def logs_dir() -> Path:
    return Path.home() / "Library" / "Logs" / "Jarvis"


def app_config(port: int) -> dict:
    """What the app needs to start the background service."""
    return {
        "daemon": [str(Path(sys.executable).parent / "jarvis"), "daemon", "--port", str(port)],
        "cwd": str(PROJECT),
        "port": port,
        "call_apps": list(CALL_APPS),
        "hide_from_screen_share": True,
    }


def agent_plist(app: Path) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [str(app / "Contents" / "MacOS" / "Jarvis")],
        "RunAtLoad": True,
        # Come back after a crash, stay away after Quit.
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Interactive",
        "LimitLoadToSessionType": "Aqua",
        "AssociatedBundleIdentifiers": [LABEL],
        "StandardOutPath": str(logs_dir() / "launchd.log"),
        "StandardErrorPath": str(logs_dir() / "launchd.log"),
    }


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _run(command: list[str], *, check: bool = True, cwd: Path | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(command, capture_output=True, text=True, cwd=cwd)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()[-1:] or ["no output"]
        raise InstallError(f"{' '.join(command[:3])} failed: {detail[0]}")
    return result


def is_loaded() -> bool:
    return _run(["launchctl", "print", f"{_domain()}/{LABEL}"], check=False).returncode == 0


def stop(progress: Progress = print) -> None:
    if not is_loaded():
        return
    _run(["launchctl", "bootout", f"{_domain()}/{LABEL}"], check=False)
    for _ in range(50):  # the app closes its background service on the way out
        if not is_loaded():
            break
        time.sleep(0.2)
    progress("Stopped the running Jarvis.")


def answering(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=2) as response:
            return response.status == 200
    except OSError:
        return False


def install(port: int = DEFAULT_PORT, *, rebuild: bool = False, progress: Progress = print) -> None:
    if rebuild or not BUILT_APP.is_dir():
        progress("Building the app ...")
        _run(["bash", str(PROJECT / "app" / "build.sh")])
    if not (PROJECT / "ui" / "dist" / "index.html").exists():
        if shutil.which("npm") is None:
            raise InstallError("The interface is not built and npm is not installed. Install Node.js, then run this again.")
        progress("Building the interface ...")
        _run(["npm", "install", "--no-audit", "--no-fund"], cwd=PROJECT / "ui")
        _run(["npm", "run", "build"], cwd=PROJECT / "ui")

    stop(progress)
    target = installed_app()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    _run(["ditto", str(BUILT_APP), str(target)])  # keeps the signature intact
    progress(f"App: {target}")

    paths.ensure_data_dir()
    app_config_file().write_text(json.dumps(app_config(port), indent=2), encoding="utf-8")
    logs_dir().mkdir(parents=True, exist_ok=True)
    agent = agent_file()
    agent.parent.mkdir(parents=True, exist_ok=True)
    agent.write_bytes(plistlib.dumps(agent_plist(target)))
    _run(["launchctl", "bootstrap", _domain(), str(agent)])
    progress("Registered to start at login.")

    for _ in range(60):
        if answering(port):
            progress(f"Jarvis is running: http://127.0.0.1:{port}")
            return
        time.sleep(1)
    raise InstallError(
        "Jarvis was started but its service is not answering yet. If macOS is asking for access to the "
        f"Documents folder, allow it. Logs: {logs_dir()}"
    )


def uninstall(progress: Progress = print) -> None:
    stop(progress)
    agent_file().unlink(missing_ok=True)
    app_config_file().unlink(missing_ok=True)
    if installed_app().exists():
        shutil.rmtree(installed_app())
    progress("Removed the app and its login entry. Your data and settings are untouched.")
