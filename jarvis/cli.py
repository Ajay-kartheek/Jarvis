"""Command-line entry point: ``jarvis``."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated

import typer

from jarvis import google_auth, paths
from jarvis.config import Config, ConfigError, load_config

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Jarvis: personal assistant for mail, calendar, Drive and meetings.",
)
auth_app = typer.Typer(no_args_is_help=True, help="Google sign-in.")
app.add_typer(auth_app, name="auth")
project_app = typer.Typer(no_args_is_help=True, help="The engagements Jarvis works on, and their folders.")
app.add_typer(project_app, name="project")


def _config() -> Config:
    try:
        return load_config()
    except ConfigError as exc:
        typer.secho(f"Config error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from None


@app.command("config")
def show_config() -> None:
    """Show the settings Jarvis is running with."""
    config = _config()
    typer.echo(f"Config file: {config.source}")
    typer.echo(f"Secrets:     {paths.secrets_dir()}")
    typer.echo(f"Data:        {paths.data_dir()}")
    typer.echo("Accounts:")
    for account in config.accounts:
        typer.echo(f"  {account.email}{' (primary)' if account.primary else ''}")
    typer.echo("Projects:")
    for project in config.projects:
        typer.echo(f"  {project.name}: {', '.join(project.people) or '-'}")
    typer.echo(f"Ingest: last {config.ingest_months} months")
    typer.echo(f"Transcripts kept: {config.transcript_retention_days} days")
    typer.echo("Models:")
    for job, route in config.jobs.items():
        typer.echo(f"  {job:<14} {route.model:<20} via {route.backend}")


def _stamped(message: str) -> None:
    typer.echo(f"{time.strftime('%H:%M:%S')} {message}")


@app.command("sync")
def sync_command(
    only: Annotated[
        str | None, typer.Option("--only", help="Sync one source: gmail, calendar, drive or folders.")
    ] = None,
    account: Annotated[
        str | None, typer.Option("--account", help="Sync one account (default: all).")
    ] = None,
    max_messages: Annotated[
        int | None,
        typer.Option("--max-messages", help="Stop the Gmail backfill after this many (for testing)."),
    ] = None,
) -> None:
    """Copy new mail, calendar events, Drive files and project folders into the local store."""
    from jarvis import sync as sync_module

    config = _config()
    if only is not None and only not in sync_module.SOURCES:
        typer.secho(
            f"--only must be one of {', '.join(sync_module.SOURCES)}", fg=typer.colors.RED, err=True
        )
        raise typer.Exit(2)
    results = sync_module.run_sync(
        config, only=only, account=account, progress=_stamped, max_messages=max_messages
    )
    failed = False
    for result in results:
        label = f"{result['account']} {result['source']}"
        if result["status"] == "ok":
            counters = ", ".join(f"{key}={value}" for key, value in result["summary"].items())
            _stamped(f"{label}: done ({counters})")
        elif result["status"] == "busy":
            _stamped(f"{label}: skipped, already running")
        else:
            failed = True
            typer.secho(f"{label}: {result.get('error')}", fg=typer.colors.RED)
    if failed:
        raise typer.Exit(1)


@auth_app.command("add")
def auth_add(
    email: Annotated[str, typer.Argument(help="Google account to sign in.")],
    client: Annotated[
        Path | None,
        typer.Option(
            "--client",
            exists=True,
            dir_okay=False,
            readable=True,
            help="OAuth 'Desktop app' client JSON downloaded from Google Cloud. "
            "Needed the first time only.",
        ),
    ] = None,
    timeout: Annotated[
        int, typer.Option("--timeout", help="Seconds to wait for the browser sign-in.")
    ] = google_auth.DEFAULT_CONSENT_TIMEOUT_SECONDS,
) -> None:
    """Sign a Google account in: reading, Gmail drafts (sent only after the user says yes) and calendar events."""
    config = _config()
    if client is None and not paths.google_client_file().exists():
        typer.secho(
            "No Google client file yet. Pass --client <path to the downloaded JSON>.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)

    typer.echo(f"Opening the browser to sign in {email} ...")
    try:
        actual = google_auth.add_account(
            email,
            client,
            consent=lambda address: google_auth.run_consent_flow(address, timeout),
        )
    except google_auth.AuthError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None

    typer.secho(f"Signed in as {actual}. Credentials are in the Keychain.", fg=typer.colors.GREEN)
    if config.account(email) is None:
        typer.echo(
            f"Note: {email} is not in config.toml yet. Add an [[accounts]] entry "
            "for it so Jarvis uses it."
        )
    typer.echo("Check access with: jarvis auth status")


@auth_app.command("status")
def auth_status() -> None:
    """Show, per account, whether Jarvis is signed in and each API answers."""
    config = _config()
    failed = False
    for account in config.accounts:
        label = f"{account.email}{' (primary)' if account.primary else ''}"
        typer.echo(label)
        if not google_auth.has_credentials(account.email):
            failed = True
            typer.secho(
                f"  not signed in. Run: jarvis auth add {account.email} --client <file>",
                fg=typer.colors.YELLOW,
            )
            continue
        try:
            creds = google_auth.load_credentials(account.email)
        except Exception as exc:  # expired or revoked sign-in, no network, ...
            failed = True
            typer.secho(
                f"  sign-in no longer works: {google_auth.describe_error(exc)}",
                fg=typer.colors.RED,
            )
            continue
        typer.echo("  credential: stored in the Keychain")
        for check in google_auth.check_apis(creds):
            if check.ok:
                typer.secho(f"  {check.name}: OK ({check.detail})", fg=typer.colors.GREEN)
            else:
                failed = True
                typer.secho(f"  {check.name}: {check.detail}", fg=typer.colors.RED)
    if failed:
        raise typer.Exit(1)


@app.command("key")
def key_command(
    service: Annotated[str, typer.Argument(help="Whose key: elevenlabs.")],
) -> None:
    """Keep an API key in the Keychain. It is pasted into a macOS dialog, so it never passes through a terminal or
    a chat, and it is never shown."""
    import subprocess

    from jarvis import keychain

    if service not in keychain.KEY_SERVICES:
        typer.secho(f"Unknown service {service!r}. Known: {', '.join(keychain.KEY_SERVICES)}", fg=typer.colors.RED)
        raise typer.Exit(2)
    script = (
        f'text returned of (display dialog "Paste your {service} API key. Jarvis keeps it in the Keychain." '
        'default answer "" with hidden answer with title "Jarvis" buttons {"Cancel", "Save"} default button "Save")'
    )
    asked = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True)
    key = asked.stdout.strip()
    if asked.returncode != 0 or not key:
        typer.echo("Nothing was saved.")
        raise typer.Exit(1)
    keychain.save_secret(keychain.KEY_SERVICES[service], keychain.KEY_USER, key)
    typer.echo(f"Saved the {service} key in the Keychain ({len(key)} characters).")


@app.command("status")
def status() -> None:
    """Show what the local copy holds and when each source last synced."""
    from jarvis.store import open_store
    from jarvis.timeutil import day, minute

    with open_store() as store:
        stats = store.stats()
    typer.echo(f"Store: {stats['path']} ({stats['size_bytes'] / 1_000_000:.1f} MB)")
    if not stats["accounts"] and not stats["folders"]:
        typer.echo("Nothing synced yet. Run: jarvis sync")
        return
    if stats["folders"]:
        indexed = ", ".join(f"{name} {count}" for name, count in sorted(stats["folders"].items()))
        typer.echo(f"Project folders: {indexed} files indexed")
    for account, info in stats["accounts"].items():
        mail, events, files = info["mail"], info["events"], info["files"]
        typer.echo(account)
        typer.echo(
            f"  mail:   {mail['count']} messages in {mail['threads']} threads"
            f" ({day(mail['oldest'])} to {day(mail['newest'])})"
        )
        typer.echo(
            f"  events: {events['count']} in {events['calendars']} calendars"
            f" ({day(events['oldest'])} to {day(events['newest'])})"
        )
        typer.echo(
            f"  files:  {files['count']}, {files['with_text']} with text,"
            f" {files['export_failures']} with export notes"
        )
        if info["projects"]:
            tagged = ", ".join(f"{name} {count}" for name, count in sorted(info["projects"].items()))
            typer.echo(f"  tagged: {tagged}")
        for source, run in info["last_runs"].items():
            if run is None:
                typer.echo(f"  {source}: never synced")
                continue
            line = f"  {source}: last run {run.get('status')} at {minute(run.get('finished_at') or run.get('started_at'))}"
            if run.get("error"):
                line += f" ({run['error']})"
            typer.echo(line)


@app.command("search")
def search(
    query: Annotated[str, typer.Argument(help="Words to look for.")],
    kind: Annotated[str | None, typer.Option(help="mail, event or file.")] = None,
    project: Annotated[str | None, typer.Option(help="Only items tagged with this project.")] = None,
    limit: Annotated[int, typer.Option(help="How many results to show.")] = 10,
    newest: Annotated[bool, typer.Option("--newest", help="Newest first instead of best match.")] = False,
) -> None:
    """Search the local copy of mail, calendar and Drive. No AI involved."""
    from jarvis.store import open_store
    from jarvis.timeutil import day

    if kind not in (None, "mail", "event", "file"):
        typer.secho("--kind must be mail, event or file", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    with open_store() as store:
        hits = store.search(
            query, kind=kind, project=project, limit=limit,
            order="newest" if newest else "relevance",
        )
    if not hits:
        typer.echo("No matches.")
        return
    for hit in hits:
        tag = f" [{hit.project}]" if hit.project else ""
        typer.secho(f"{day(hit.ts)}  {hit.kind:<5} {hit.title}{tag}", bold=True)
        if hit.people:
            typer.echo(f"    {hit.people[:120]}")
        if hit.snippet:
            typer.echo(f"    {' '.join(hit.snippet.split())[:240]}")
        if hit.link:
            typer.echo(f"    {hit.link}")


def _show_step(step: str) -> None:
    typer.secho(f"  · {step[:110]}", fg=typer.colors.BRIGHT_BLACK, err=True)


def _show_answer(answer) -> None:
    typer.echo(answer.text)
    if answer.web_queries:
        typer.echo("\nSent to the web: " + " | ".join(answer.web_queries))
    typer.secho(f"\n({answer.seconds:.0f} s)", fg=typer.colors.BRIGHT_BLACK, err=True)


@app.command("ask")
def ask(
    question: Annotated[str, typer.Argument(help="What you want to know.")],
    web: Annotated[
        bool, typer.Option("--web", help="Also let a separate web agent research up to three short queries.")
    ] = False,
) -> None:
    """Answer from your mail, calendar and Drive. Nothing leaves the Mac except to Claude."""
    from jarvis import agents

    try:
        answer = agents.ask(question, _config(), web=web, notify=_show_step)
    except agents.AgentError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None
    _show_answer(answer)


@app.command("web")
def web(question: Annotated[str, typer.Argument(help="What to look up on the public web.")]) -> None:
    """Answer from the public web only. This agent cannot see your mail or files."""
    from jarvis import agents

    try:
        answer = agents.web(question, _config(), notify=_show_step)
    except agents.AgentError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None
    _show_answer(answer)


@app.command("listen")
def listen(
    seconds: Annotated[
        float | None, typer.Option(help="Stop after this many seconds. Default: until Ctrl-C.")
    ] = None,
    partials: Annotated[bool, typer.Option("--partials", help="Also show the sentence being spoken.")] = False,
    headphones: Annotated[
        bool,
        typer.Option("--headphones", help="Keep the microphone on while others talk (no echo on headphones)."),
    ] = False,
    say: Annotated[str | None, typer.Option(hidden=True, help="Speak this sentence as a self-test.")] = None,
) -> None:
    """Transcribe a call live on this Mac: Them (system audio) and You (microphone)."""
    from datetime import datetime
    from types import SimpleNamespace

    # Imported here because it loads the speech model's libraries.
    from jarvis import listen as listener

    folder = paths.ensure_data_dir() / "transcripts"
    folder.mkdir(exist_ok=True)
    out = folder / f"{datetime.now():%Y-%m-%d-%H%M}.jsonl"
    typer.echo(f"Listening. Transcript: {out}  (Ctrl-C to stop)")
    code = listener.run(
        SimpleNamespace(
            seconds=seconds, wav=None, say=say, say_after=8.0, partials=partials, out=out,
            echo_guard=not headphones,
        )
    )
    raise typer.Exit(code)


def _show_project(project) -> None:
    typer.secho(project.name, bold=True)
    typer.echo(f"  people:  {', '.join(project.people) or '-'}")
    if not project.folders:
        typer.echo("  folders: -")
    for folder in project.folders:
        typer.echo(f"  folder:  {folder}")


@project_app.command("add")
def project_add(
    name: Annotated[str, typer.Argument(help="Project name, for example Atlas.")],
    folder: Annotated[
        list[Path] | None, typer.Option("--folder", "-f", help="A folder on this Mac. Repeat for several.")
    ] = None,
    person: Annotated[
        list[str] | None, typer.Option("--person", "-p", help="Someone on the project. Repeat for several.")
    ] = None,
    index: Annotated[bool, typer.Option(help="Read the folders straight away.")] = True,
) -> None:
    """Add a project, or add folders and people to one that exists."""
    from jarvis import folder_sync, projects
    from jarvis.store import open_store

    config = _config()
    with open_store() as store:
        try:
            project = projects.add(
                store, name, folders=[str(f) for f in folder or []], people=person or [], config=config
            )
        except projects.ProjectError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(2) from None
        _show_project(project)
        if index and project.folders:
            summary = folder_sync.sync_project(store, project, progress=typer.echo)
            typer.echo(
                f"Read {summary['indexed']} files ({summary['unchanged']} unchanged, "
                f"{summary['skipped']} skipped, {summary['removed']} removed)."
            )


@project_app.command("list")
def project_list() -> None:
    """Show every project with its people and folders."""
    from jarvis import projects
    from jarvis.store import open_store

    with open_store() as store:
        found = projects.load(store, _config())
    if not found:
        typer.echo("No projects yet. Add one: jarvis project add <name> --folder <path>")
    for project in found:
        _show_project(project)


@project_app.command("drop-folder")
def project_drop_folder(
    name: Annotated[str, typer.Argument(help="Project name.")],
    folder: Annotated[Path, typer.Argument(help="The folder to stop using.")],
) -> None:
    """Stop using a folder for a project and forget what was read from it."""
    from jarvis import projects
    from jarvis.store import open_store

    with open_store() as store:
        try:
            project = projects.drop_folder(store, name, str(folder), _config())
        except projects.ProjectError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(2) from None
    _show_project(project)


@project_app.command("remove")
def project_remove(name: Annotated[str, typer.Argument(help="Project name.")]) -> None:
    """Remove a project and forget what was read from its folders. Mail and files on disk are untouched."""
    from jarvis import projects
    from jarvis.store import open_store

    with open_store() as store:
        removed = projects.remove(store, name, _config())
    typer.echo(f"Removed {name}." if removed else f"There is no project called {name}.")


@app.command("meeting")
def meeting(
    project: Annotated[
        str | None, typer.Option("--project", help="The engagement this call belongs to. Default: from the calendar.")
    ] = None,
    seconds: Annotated[
        float | None, typer.Option(help="Stop after this many seconds. Default: until Ctrl-C.")
    ] = None,
    headphones: Annotated[
        bool,
        typer.Option("--headphones", help="Keep the microphone on while others talk (no echo on headphones)."),
    ] = False,
    say: Annotated[str | None, typer.Option(hidden=True, help="Speak this sentence as a self-test.")] = None,
) -> None:
    """Listen to a call and suggest what to say, using the engagement's mail, calendar and folders."""
    import asyncio
    from datetime import datetime

    from jarvis import meeting as live

    folder = paths.ensure_data_dir() / "transcripts"
    folder.mkdir(exist_ok=True)
    out = folder / f"{datetime.now():%Y-%m-%d-%H%M}.jsonl"

    def show(event: dict) -> None:
        kind = event["type"]
        if kind == "meeting":
            engagement = event["engagement"] or "not identified (pass --project to set it)"
            typer.secho(
                f"Meeting: {event['title']} | Engagement: {engagement} | Brief: {event['brief_chars']:,} characters",
                fg=typer.colors.BRIGHT_BLACK,
            )
        elif kind == "ready":
            typer.secho("Ready. Listening.", fg=typer.colors.BRIGHT_BLACK)
        elif kind == "sentence":
            typer.echo(f"[{datetime.now():%H:%M:%S}] {event['speaker']}: {event['text']}")
        elif kind == "suggestion":
            typer.secho(f"           SAY  {event['text']}  ({event['seconds']:.1f} s)", fg=typer.colors.GREEN, bold=True)

    try:
        asyncio.run(
            live.run(
                _config(), project_name=project, seconds=seconds, headphones=headphones, say=say,
                out=out, emit=show,
            )
        )
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from None
    except KeyboardInterrupt:
        pass
    typer.secho(f"Transcript: {out}", fg=typer.colors.BRIGHT_BLACK)


@app.command("ui")
def ui(
    port: Annotated[int, typer.Option(help="Port on 127.0.0.1.")] = 8765,
    browser: Annotated[bool, typer.Option(help="Open the interface in the browser.")] = True,
) -> None:
    """Start the Jarvis interface: meeting panel, ask and today."""
    import threading
    import webbrowser

    import uvicorn

    from jarvis.server import create_app

    url = f"http://127.0.0.1:{port}"
    typer.echo(f"Jarvis interface: {url}  (Ctrl-C to stop)")
    if browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(create_app(_config()), host="127.0.0.1", port=port, log_level="warning")


def _exit_with_parent() -> None:
    """Stop when the Jarvis app goes away: it holds the other end of our standard input."""
    import os
    import signal
    import stat
    import sys
    import threading

    if not stat.S_ISFIFO(os.fstat(sys.stdin.fileno()).st_mode):
        return

    def wait() -> None:
        while sys.stdin.buffer.read(4096):
            pass
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=wait, daemon=True).start()


@app.command("daemon")
def daemon(port: Annotated[int, typer.Option(help="Port on 127.0.0.1.")] = 8765) -> None:
    """Run Jarvis in the background: automatic listening on calls, syncs, notes, the brief and the interface.

    The Jarvis app starts this by itself; `jarvis install` sets that up.
    """
    import logging
    import os

    import uvicorn

    from jarvis.server import create_app

    logging.basicConfig(format="%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    logging.getLogger("jarvis").setLevel(logging.INFO)
    config = _config()
    if os.environ.get("JARVIS_APP"):
        _exit_with_parent()
    logging.getLogger("jarvis").info("daemon starting on port %d", port)
    uvicorn.run(
        create_app(config, background=True), host="127.0.0.1", port=port, log_level="warning",
        timeout_graceful_shutdown=3,  # the app keeps its connection open; do not wait for it on the way out
    )


@app.command("install")
def install_command(
    port: Annotated[int, typer.Option(help="Port on 127.0.0.1 for the interface.")] = 8765,
    rebuild: Annotated[bool, typer.Option("--rebuild", help="Build the app again first.")] = False,
) -> None:
    """Make Jarvis run by itself: install the app, start it now and at every login."""
    from jarvis import install

    _config()  # fail early on a broken config.toml
    try:
        install.install(port, rebuild=rebuild, progress=typer.echo)
    except install.InstallError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None
    typer.echo("Control+Option+J shows and hides Jarvis. It listens by itself when a call starts.")


@app.command("deploy")
def deploy_command() -> None:
    """Move the running Jarvis to the code of the latest commit, then restart it."""
    from jarvis import install

    try:
        install.deploy(progress=typer.echo)
        install.restart(progress=typer.echo)
    except install.InstallError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None


@app.command("uninstall")
def uninstall_command() -> None:
    """Stop Jarvis running by itself and remove the app. Data and settings stay."""
    from jarvis import install

    install.uninstall(progress=typer.echo)


if __name__ == "__main__":
    app()
