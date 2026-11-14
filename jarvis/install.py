"""Set Jarvis up to run by itself.

``jarvis install`` copies the Jarvis app to ~/Applications, tells it how to
start the background service, and registers it with launchd so it starts at
login and comes back if it crashes. ``jarvis uninstall`` undoes all of that
and leaves the data alone.

The app is copied out of the project folder because launchd cannot start a
program that lives under ~/Documents.

The running Jarvis does not use the code in the project folder either. It runs
from a release: a copy of the last commit with its own Python environment,
kept next to the data. Work in progress here therefore cannot disturb a call;
``jarvis deploy`` moves the release to the current commit.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import signal
import subprocess
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


def release_dir() -> Path:
    return paths.data_dir() / "release"


def logs_dir() -> Path:
    return Path.home() / "Library" / "Logs" / "Jarvis"


def app_config(port: int) -> dict:
    """What the app needs to start the background service: the release's own program, the user's settings."""
    release = release_dir()
    return {
        "daemon": [
            "/usr/bin/env", f"JARVIS_CONFIG={PROJECT / 'config.toml'}",
            str(release / ".venv" / "bin" / "jarvis"), "daemon", "--port", str(port),
        ],
        "cwd": str(release),
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
    if is_loaded():
        _run(["launchctl", "bootout", f"{_domain()}/{LABEL}"], check=False)
        for _ in range(50):  # the app closes its background service on the way out
            if not is_loaded():
                break
            time.sleep(0.2)
        progress("Stopped the running Jarvis.")
    _stop_opened_by_hand(progress)


def _launchd_pid() -> int | None:
    """The process launchd runs for Jarvis, if it runs one."""
    found = _run(["launchctl", "print", f"{_domain()}/{LABEL}"], check=False)
    match = re.search(r"^\s*pid = (\d+)", found.stdout or "", re.MULTILINE)
    return int(match.group(1)) if match else None


def _opened_by_hand() -> list[int]:
    """Copies of the installed app that launchd did not start: opened from Finder after a Quit, say. launchd cannot
    stop them, and one left running keeps the old version going and turns the new one away (the app's lock)."""
    program = str(installed_app() / "Contents" / "MacOS" / "Jarvis")
    found = _run(["pgrep", "-x", "-f", program], check=False)  # exactly the app: not its capture helper
    launchds = _launchd_pid()
    return [int(pid) for pid in found.stdout.split() if pid.isdigit() and int(pid) != launchds]


def _stop_opened_by_hand(progress: Progress) -> None:
    running = _opened_by_hand()
    if not running:
        return
    for pid in running:
        try:
            os.kill(pid, signal.SIGTERM)  # the app takes it as Quit, and closes its background service too
        except ProcessLookupError:
            pass
    for _ in range(50):
        if not _opened_by_hand():
            break
        time.sleep(0.2)
    progress("Stopped the Jarvis that was opened by hand.")


def answering(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=2) as response:
            return response.status == 200
    except OSError:
        return False


def build_interface(progress: Progress = print, *, force: bool = False) -> None:
    if not force and (PROJECT / "ui" / "dist" / "index.html").exists():
        return
    if shutil.which("npm") is None:
        raise InstallError("The interface is not built and npm is not installed. Install Node.js, then run this again.")
    progress("Building the interface ...")
    if not (PROJECT / "ui" / "node_modules").is_dir():
        _run(["npm", "install", "--no-audit", "--no-fund"], cwd=PROJECT / "ui")
    _run(["npm", "run", "build"], cwd=PROJECT / "ui")


def deploy(progress: Progress = print) -> str:
    """Bring the release to the current commit and return its short id.

    Only committed code goes in. The interface is built here and copied over,
    so the release needs no Node.js tooling of its own.
    """
    uv = shutil.which("uv")
    if uv is None:
        raise InstallError("uv is not installed; it is needed to set up the release's Python environment.")
    head = _run(["git", "-C", str(PROJECT), "rev-parse", "HEAD"]).stdout.strip()
    if _run(["git", "-C", str(PROJECT), "status", "--porcelain", "--untracked-files=no"]).stdout.strip():
        progress("Note: changes that are not committed yet are left out of the release.")
    build_interface(progress)
    release = release_dir()
    paths.ensure_data_dir()
    if not (release / ".git").is_dir():
        _run(["git", "clone", "--quiet", "--no-checkout", str(PROJECT), str(release)])
    _run(["git", "-C", str(release), "fetch", "--quiet", "origin"])
    _run(["git", "-C", str(release), "checkout", "--quiet", "--detach", head])
    progress("Setting up the release's Python environment ...")
    _run([uv, "sync", "--frozen", "--no-dev", "--project", str(release)])
    fetch_ear_model(progress)
    dist = release / "ui" / "dist"
    if dist.exists():
        shutil.rmtree(dist)
    shutil.copytree(PROJECT / "ui" / "dist", dist)
    progress(f"Release: {release} at {head[:7]}")
    return head[:7]


def fetch_ear_model(progress: Progress = print) -> bool:
    """Put the ear's models in the data folder, if they are not there yet: the voice-activity model, and the one that
    tells when the user has finished. A failure is only a warning: everything else works without them, and the next
    deploy tries again."""
    from jarvis import ear

    try:
        ear.download()
        return True
    except Exception as exc:
        if ear.available():  # only the turn model is missing: a message then ends after a fixed pause
            progress(f"Warning: the ear's turn model could not be fetched ({exc}); it ends messages after a fixed pause.")
        else:
            progress(f"Warning: the ear's voice model could not be fetched ({exc}); \"Jarvis\" will not wake it yet.")
        return False


def restart(progress: Progress = print) -> None:
    """Start the installed Jarvis again so it picks up a new release."""
    if is_loaded():
        _stop_opened_by_hand(progress)
        _run(["launchctl", "kickstart", "-k", f"{_domain()}/{LABEL}"])
        progress("Restarted Jarvis.")


def install(port: int = DEFAULT_PORT, *, rebuild: bool = False, progress: Progress = print) -> None:
    if rebuild or not BUILT_APP.is_dir():
        progress("Building the app ...")
        _run(["bash", str(PROJECT / "app" / "build.sh")])
    deploy(progress)

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
    for folder in (installed_app(), release_dir()):
        if folder.exists():
            shutil.rmtree(folder)
    progress("Removed the app and its login entry. Your data and settings are untouched.")
