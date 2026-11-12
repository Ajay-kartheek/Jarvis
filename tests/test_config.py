from __future__ import annotations

import copy
from pathlib import Path

import pytest

from jarvis.config import BACKENDS, JOBS, ConfigError, load_config, parse_config

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _valid() -> dict:
    return {
        "ingest": {"months": 12},
        "retention": {"transcript_days": 90},
        "accounts": [{"email": "a@example.com", "primary": True}],
        "projects": [{"name": "P1", "people": ["X"]}],
        "models": {"jobs": {job: {"model": "m"} for job in JOBS}},
    }


def test_repo_config_loads_with_agreed_values():
    config = load_config(PROJECT_ROOT / "config.example.toml")

    assert [a.email for a in config.accounts] == ["you@example.com"]
    assert config.primary_account.email == "you@example.com"
    assert {p.name: p.people for p in config.projects} == {
        "Atlas": ("Priya", "Ravi"),
        "Borealis": ("Meera", "Arjun", "Kiran Rao"),
    }
    assert config.ingest_months == 12
    assert config.transcript_retention_days == 90
    assert {job: route.model for job, route in config.jobs.items()} == {
        "live_answers": "claude-haiku-5-5",
        "mail_sorting": "claude-haiku-5-5",
        "drafts": "claude-sonnet-5-5",
        "briefs": "claude-sonnet-5-5",
        "research": "claude-opus-5-5",
        "project_files": "claude-opus-5-5",
        "ask": "claude-sonnet-5-5",
        "web": "claude-sonnet-5-5",
    }


def test_every_job_defaults_to_the_max_login():
    config = load_config(PROJECT_ROOT / "config.example.toml")

    assert config.default_backend == "max_login"
    assert {route.backend for route in config.jobs.values()} == {"max_login"}


def test_allowed_backends():
    assert BACKENDS == ("max_login", "vertex", "api_key")


def test_job_backend_can_be_overridden_per_job():
    raw = _valid()
    raw["models"]["jobs"]["live_answers"]["backend"] = "vertex"

    config = parse_config(raw)

    assert config.route("live_answers").backend == "vertex"
    assert config.route("drafts").backend == "max_login"


def test_default_backend_applies_to_jobs_without_one():
    raw = _valid()
    raw["models"]["default_backend"] = "api_key"

    assert parse_config(raw).route("research").backend == "api_key"


def test_account_lookup_ignores_case():
    config = parse_config(_valid())

    assert config.account("A@Example.com") is not None
    assert config.account("nobody@example.com") is None


def test_config_path_from_environment(tmp_path, monkeypatch):
    target = tmp_path / "other.toml"
    target.write_text((PROJECT_ROOT / "config.example.toml").read_text())
    monkeypatch.setenv("JARVIS_CONFIG", str(target))

    assert load_config().source == target


def test_missing_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda r: r["models"]["jobs"]["drafts"].update(backend="bedrock"), "must be one of"),
        (lambda r: r["models"].update(default_backend="plan"), "must be one of"),
        (lambda r: r["models"]["jobs"].pop("research"), "missing route"),
        (lambda r: r["models"]["jobs"].update(extra={"model": "m"}), "unknown job"),
        (lambda r: r["models"]["jobs"]["briefs"].update(model=""), "model is required"),
        (lambda r: r["ingest"].update(months=0), "positive whole number"),
        (lambda r: r["retention"].update(transcript_days="90"), "positive whole number"),
        (lambda r: r.update(accounts=[]), "at least one"),
        (lambda r: r["accounts"][0].update(primary=False), "exactly one account"),
        (
            lambda r: r["accounts"].append({"email": "b@example.com", "primary": True}),
            "exactly one account",
        ),
        (
            lambda r: r["accounts"].append({"email": "A@example.com"}),
            "listed twice",
        ),
        (lambda r: r["accounts"][0].update(email="not-an-email"), "not an email"),
        (lambda r: r["projects"].append({"name": "p1"}), "listed twice"),
        (lambda r: r["projects"][0].update(people="X"), "list of names"),
    ],
)
def test_invalid_config_is_rejected(mutate, message):
    raw = copy.deepcopy(_valid())
    mutate(raw)

    with pytest.raises(ConfigError, match=message):
        parse_config(raw)
