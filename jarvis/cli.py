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
        str | None, typer.Option("--only", help="Sync one source: gmail, calendar or drive.")
    ] = None,
    account: Annotated[
        str | None, typer.Option("--account", help="Sync one account (default: all).")
    ] = None,
    max_messages: Annotated[
        int | None,
        typer.Option("--max-messages", help="Stop the Gmail backfill after this many (for testing)."),
    ] = None,
) -> None:
    """Copy new mail, calendar events and Drive files into the local store."""
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
    """Sign a Google account in (read-only access plus Gmail drafts)."""
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


@app.command("status")
def status() -> None:
    """Show what the local copy holds and when each source last synced."""
    from jarvis.store import open_store
    from jarvis.timeutil import day, minute

    with open_store() as store:
        stats = store.stats()
    typer.echo(f"Store: {stats['path']} ({stats['size_bytes'] / 1_000_000:.1f} MB)")
    if not stats["accounts"]:
        typer.echo("Nothing synced yet. Run: jarvis sync")
        return
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


if __name__ == "__main__":
    app()
