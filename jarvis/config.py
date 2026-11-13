"""Load and validate ``config.toml``.

The file is looked up in this order: the path given to :func:`load_config`,
the ``JARVIS_CONFIG`` environment variable, then ``config.toml`` in the
project root.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

BACKENDS = ("max_login", "vertex", "api_key")
DEFAULT_BACKEND = "max_login"

# Every job Jarvis runs must be routed here; a missing entry is a config error.
JOBS = (
    "live_answers",
    "mail_sorting",
    "drafts",
    "briefs",
    "research",
    "project_files",
    "ask",
    "web",
)


# Jobs that may be routed but need not be: each falls back to another job's model.
OPTIONAL_JOBS = {
    "core": "ask",  # Jarvis's own conversation
    "prep": "briefs",  # preparing for a meeting
}


class ConfigError(ValueError):
    """config.toml is missing something or holds an invalid value."""


@dataclass(frozen=True)
class Account:
    email: str
    primary: bool = False


@dataclass(frozen=True)
class Project:
    name: str
    people: tuple[str, ...] = ()


@dataclass(frozen=True)
class JobRoute:
    model: str
    backend: str = DEFAULT_BACKEND


@dataclass(frozen=True)
class Config:
    accounts: tuple[Account, ...]
    projects: tuple[Project, ...]
    ingest_months: int
    transcript_retention_days: int
    default_backend: str
    jobs: dict[str, JobRoute] = field(default_factory=dict)
    source: Path | None = None

    @property
    def primary_account(self) -> Account:
        return next(a for a in self.accounts if a.primary)

    def account(self, email: str) -> Account | None:
        wanted = email.strip().lower()
        return next((a for a in self.accounts if a.email.lower() == wanted), None)

    def route(self, job: str) -> JobRoute:
        try:
            return self.jobs[job]
        except KeyError:
            raise ConfigError(f"no model route for job {job!r}") from None


def default_config_path() -> Path:
    override = os.environ.get("JARVIS_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parent.parent / "config.toml"


def load_config(path: Path | str | None = None) -> Config:
    config_path = Path(path).expanduser() if path else default_config_path()
    try:
        with open(config_path, "rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {config_path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{config_path}: {exc}") from None
    return parse_config(raw, source=config_path)


def parse_config(raw: dict, source: Path | None = None) -> Config:
    accounts = _parse_accounts(raw.get("accounts", []))
    projects = _parse_projects(raw.get("projects", []))
    ingest_months = _positive_int(raw.get("ingest", {}), "months", "ingest.months")
    retention_days = _positive_int(
        raw.get("retention", {}), "transcript_days", "retention.transcript_days"
    )
    models = raw.get("models", {})
    default_backend = _backend(
        models.get("default_backend", DEFAULT_BACKEND), "models.default_backend"
    )
    jobs = _parse_jobs(models.get("jobs", {}), default_backend)
    return Config(
        accounts=accounts,
        projects=projects,
        ingest_months=ingest_months,
        transcript_retention_days=retention_days,
        default_backend=default_backend,
        jobs=jobs,
        source=source,
    )


def _parse_accounts(items: list) -> tuple[Account, ...]:
    if not items:
        raise ConfigError("at least one [[accounts]] entry is required")
    accounts = []
    seen = set()
    for item in items:
        email = str(item.get("email", "")).strip()
        if "@" not in email or email.startswith("@") or email.endswith("@"):
            raise ConfigError(f"accounts: {email!r} is not an email address")
        if email.lower() in seen:
            raise ConfigError(f"accounts: {email} is listed twice")
        seen.add(email.lower())
        accounts.append(Account(email=email, primary=bool(item.get("primary", False))))
    primaries = [a.email for a in accounts if a.primary]
    if len(primaries) != 1:
        raise ConfigError(
            f"exactly one account must have primary = true (found {len(primaries)})"
        )
    return tuple(accounts)


def _parse_projects(items: list) -> tuple[Project, ...]:
    projects = []
    seen = set()
    for item in items:
        name = str(item.get("name", "")).strip()
        if not name:
            raise ConfigError("projects: every entry needs a name")
        if name.lower() in seen:
            raise ConfigError(f"projects: {name} is listed twice")
        seen.add(name.lower())
        people = item.get("people", [])
        if not isinstance(people, list) or not all(isinstance(p, str) for p in people):
            raise ConfigError(f"projects.{name}.people must be a list of names")
        projects.append(Project(name=name, people=tuple(p.strip() for p in people)))
    return tuple(projects)


def _parse_jobs(items: dict, default_backend: str) -> dict[str, JobRoute]:
    unknown = sorted(set(items) - set(JOBS) - set(OPTIONAL_JOBS))
    if unknown:
        raise ConfigError(f"models.jobs: unknown job(s) {', '.join(unknown)}")
    missing = [job for job in JOBS if job not in items]
    if missing:
        raise ConfigError(f"models.jobs: missing route(s) for {', '.join(missing)}")
    jobs = {}
    for job in (*JOBS, *(job for job in OPTIONAL_JOBS if job in items)):
        entry = items[job]
        model = str(entry.get("model", "")).strip()
        if not model:
            raise ConfigError(f"models.jobs.{job}.model is required")
        backend = _backend(
            entry.get("backend", default_backend), f"models.jobs.{job}.backend"
        )
        jobs[job] = JobRoute(model=model, backend=backend)
    return jobs


def _backend(value: object, where: str) -> str:
    if value not in BACKENDS:
        raise ConfigError(f"{where} must be one of {', '.join(BACKENDS)}; got {value!r}")
    return str(value)


def _positive_int(table: dict, key: str, where: str) -> int:
    value = table.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{where} must be a positive whole number; got {value!r}")
    return value
