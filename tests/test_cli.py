from __future__ import annotations

from typer.testing import CliRunner

from jarvis.cli import app

runner = CliRunner()


def test_config_command_shows_accounts_projects_and_routing():
    result = runner.invoke(app, ["config"])

    assert result.exit_code == 0
    assert "you@example.com (primary)" in result.output
    assert "Atlas: Priya, Ravi" in result.output
    assert "Borealis: Meera, Arjun, Kiran Rao" in result.output
    assert "live_answers" in result.output
    assert "via max_login" in result.output


def test_auth_status_when_not_signed_in(fake_keyring):
    result = runner.invoke(app, ["auth", "status"])

    assert result.exit_code == 1
    assert "not signed in" in result.output
    assert "jarvis auth add you@example.com --client <file>" in result.output


def test_auth_add_needs_a_client_file_the_first_time(fake_keyring, fake_home):
    result = runner.invoke(app, ["auth", "add", "user@example.com"])

    assert result.exit_code == 2
    assert "Pass --client" in result.output
    assert fake_keyring.items == {}
