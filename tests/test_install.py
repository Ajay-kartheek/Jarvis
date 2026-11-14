"""Installing and deploying stop every running copy of the app, also one that launchd did not start."""

from types import SimpleNamespace

from jarvis import install


def _launchd(loaded: bool, running: set[int], steps: list[str], launchds: int | None = None):
    def run(command, *, check=True, cwd=None):
        if command[0] == "pgrep":
            assert command[1:] == ["-x", "-f", str(install.installed_app() / "Contents" / "MacOS" / "Jarvis")]
            return SimpleNamespace(returncode=0 if running else 1, stdout="".join(f"{pid}\n" for pid in running), stderr="")
        if command[:2] == ["launchctl", "print"]:
            listing = f"\tstate = running\n\tpid = {launchds}\n" if loaded and launchds in running else ""
            return SimpleNamespace(returncode=0 if loaded else 113, stdout=listing, stderr="")
        steps.append(" ".join(command[:2]))
        return SimpleNamespace(returncode=0 if loaded else 113, stdout="", stderr="")

    return run


def _quit(running: set[int], steps: list[str]):
    def kill(pid, sig):
        assert sig == install.signal.SIGTERM  # taken as Quit: the app closes its service too
        steps.append(f"quit {pid}")
        running.discard(pid)

    return kill


def test_stop_quits_a_copy_opened_by_hand_after_a_quit(monkeypatch):
    running, steps, said = {4242}, [], []
    monkeypatch.setattr(install, "_run", _launchd(False, running, steps))
    monkeypatch.setattr(install.os, "kill", _quit(running, steps))
    install.stop(progress=said.append)
    assert steps == ["quit 4242"]
    assert said == ["Stopped the Jarvis that was opened by hand."]


def test_stop_with_nothing_opened_by_hand_only_asks_launchd(monkeypatch):
    running, steps, said = set(), [], []
    monkeypatch.setattr(install, "_run", _launchd(True, running, steps))
    monkeypatch.setattr(install.os, "kill", _quit(running, steps))
    monkeypatch.setattr(install, "is_loaded", iter([True, False]).__next__)
    install.stop(progress=said.append)
    assert steps == ["launchctl bootout"]
    assert said == ["Stopped the running Jarvis."]


def test_restart_quits_a_copy_opened_by_hand_before_launchd_starts_the_new_one(monkeypatch):
    running, steps = {4242}, []
    monkeypatch.setattr(install, "_run", _launchd(True, running, steps))
    monkeypatch.setattr(install.os, "kill", _quit(running, steps))
    install.restart(progress=lambda line: None)
    assert steps == ["quit 4242", "launchctl kickstart"]


def test_restart_leaves_the_copy_launchd_runs_to_launchd(monkeypatch):
    running, steps, said = {5151}, [], []
    monkeypatch.setattr(install, "_run", _launchd(True, running, steps, launchds=5151))
    monkeypatch.setattr(install.os, "kill", _quit(running, steps))
    install.restart(progress=said.append)
    assert steps == ["launchctl kickstart"]  # kickstart -k restarts it; nobody else may quit it first
    assert said == ["Restarted Jarvis."]
