"""What a work task may do: the permission decisions, tested without a model.

Each test builds a small home folder (the ``world`` fixture) with a task folder in it, another project folder and
the places that must stay private, and asks :func:`jarvis.work_rules.decide` what a tool call may do.
"""

from __future__ import annotations

import pytest

from jarvis import agents
from jarvis import work_rules as rules


def decide(world, tool, **tool_input):
    return rules.decide(tool, tool_input, world.folder)


# --- reading and searching -----------------------------------------------------------


def test_reading_is_free_in_the_users_folders_and_secrets_are_never_read(world):
    folder, home = world.folder, world.home
    for free in (folder / "notes.md", "notes.md", folder / ".env.example", folder / "password_reset.py",
                 world.atlas / "plan.md"):
        assert decide(world, "Read", file_path=str(free)).verdict == "allow", free
    for secret in (folder / ".env", home / ".ssh" / "config", "~/.ssh/config", "~/.SSH/config",
                   folder / "ssh-link" / "config", "~/.aws/credentials", "~/Library/Keychains/login.keychain-db",
                   "~/.claude/settings.json", "~/.config/gh/hosts.yml", folder / "client_secret_1.json",
                   folder / "id_rsa", folder / "server.pem", folder / "token.json", folder / ".git" / "config"):
        decision = decide(world, "Read", file_path=str(secret))
        assert decision.verdict == "never" and decision.reason, secret


def test_a_source_file_is_not_a_credential_because_of_its_name(world):
    for name in ("tokens.py", "token_service.ts", "secrets.md", "password_reset.py"):
        (world.folder / name).write_text("code")
        assert decide(world, "Read", file_path=name).verdict == "allow", name
    for name in ("passwords.txt", "api_key.json", "secrets.yaml", "credentials", "prod.env", ".env.local"):
        assert decide(world, "Read", file_path=name).verdict == "never", name


def test_outside_the_home_folder_and_in_settings_reading_is_put_to_the_user(world):
    outside = decide(world, "Read", file_path="/etc/hosts")
    assert (outside.verdict, outside.kind) == ("ask", "read") and outside.subject.endswith("/etc/hosts")
    assert decide(world, "Read", file_path="~/Library/Preferences/x.plist").verdict == "ask"
    assert decide(world, "Read", file_path="~/.zshrc").verdict == "ask"
    assert decide(world, "Read", file_path="/etc/ssh/ssh_host_rsa_key").verdict == "never"


def test_a_search_may_not_start_at_the_whole_home_folder_or_reach_a_secret_by_its_pattern(world):
    assert decide(world, "Grep", pattern="x").verdict == "allow"
    assert decide(world, "Grep", pattern="x", path="~/Documents").verdict == "allow"
    assert decide(world, "Grep", pattern="x", path="~").verdict == "never"
    assert decide(world, "Glob", pattern="**/*.py").verdict == "allow"
    assert decide(world, "Glob", pattern="~/.ssh/*").verdict == "never"
    assert decide(world, "Glob", pattern="../../../.ssh/*").verdict == "never"  # three levels up is the home folder
    assert decide(world, "Glob", pattern="/etc/*").verdict == "ask"


def test_a_folder_is_not_taken_for_a_credential_because_of_the_words_in_its_name(world):
    odd = world.home / "Documents" / "password-policy"
    odd.mkdir()
    assert rules.decide("Grep", {"pattern": "x", "path": str(odd)}, odd).verdict == "allow"


# --- writing -------------------------------------------------------------------------


def test_writing_is_free_in_the_task_folder_and_the_notes_folder_only(world):
    notes = world.home / "Documents" / "Jarvis Notes" / "summary.md"
    for free in ("b.py", str(world.folder / "sub" / "c.py"), str(notes)):
        assert decide(world, "Write", file_path=free, content="x").verdict == "allow", free
    assert decide(world, "Edit", file_path="notes.md", old_string="a", new_string="b").verdict == "allow"

    elsewhere = decide(world, "Write", file_path=str(world.atlas / "new.md"), content="x")
    assert (elsewhere.verdict, elsewhere.kind, elsewhere.subject) == ("ask", "write", str(world.atlas / "new.md"))
    assert elsewhere.covers == str(world.atlas)


def test_some_writes_are_never_allowed_whatever_the_user_says(world):
    for path in ("/tmp/x.py", "/etc/hosts", "~/.zshrc", "~/.ssh/authorized_keys", "~/Library/LaunchAgents/x.plist",
                 str(world.folder / ".git" / "hooks" / "pre-commit"), str(world.folder / ".claude" / "settings.json"),
                 str(world.folder / ".mcp.json"), str(world.folder / "ssh-link" / "authorized_keys"), "~"):
        assert decide(world, "Write", file_path=path, content="x").verdict == "never", path
    assert decide(world, "Write", file_path=str(world.folder / ".gitignore"), content="x").verdict == "allow"


def test_a_path_that_climbs_out_of_the_folder_is_judged_where_it_lands(world):
    assert decide(world, "Write", file_path="../other.md", content="x").verdict == "ask"
    assert decide(world, "Write", file_path="../../../../../../tmp/x", content="x").verdict == "never"


# --- the web and the rest ----------------------------------------------------------------


def test_a_search_is_asked_as_such_and_a_fetch_is_asked_by_host(world):
    search = decide(world, "WebSearch", query="python primes", mode="standard")
    assert (search.verdict, search.kind, search.subject, search.covers) == ("ask", "fetch", "web search", "web search")
    assert "mail and notes" in search.reason  # why: words sent to a search engine can carry them out
    assert decide(world, "WebSearch", query="  ").verdict == "never" and decide(world, "WebSearch").verdict == "never"
    fetch = decide(world, "WebFetch", url="https://Docs.Example.com./page?q=1", prompt="x")
    assert (fetch.verdict, fetch.kind, fetch.subject, fetch.covers) == ("ask", "fetch", "docs.example.com", "docs.example.com")
    assert decide(world, "WebFetch", url="http://localhost:3000/x", prompt="x").subject == "localhost:3000"
    assert decide(world, "WebFetch", url="https://example.com:443/", prompt="x").subject == "example.com"


def test_only_plain_web_links_are_fetched(world):
    for url in ("file:///etc/passwd", "ftp://example.com/x", "https://user@evil.example/", "https://evil.example\\@good.example/",
                "https://exa mple.com/", "https://example.com:notaport/", "not a link", ""):
        assert decide(world, "WebFetch", url=url, prompt="x").verdict == "never", url


def test_unknown_tools_are_refused_and_the_workers_own_tools_are_free(world):
    assert decide(world, "NotebookEdit").verdict == "never"
    assert decide(world, "mcp__other__delete_everything").verdict == "never"
    for name in (*agents.PRIVATE_TOOLS, rules.ASK_TOOL, rules.DELIVER_TOOL):
        assert decide(world, name).verdict == "allow", name
    assert decide(world, "Read", file_path="a\x00b").verdict == "never"  # cannot be read as a path: not guessed


# --- commands -------------------------------------------------------------------------


def bash(world, command, cwd=None):
    return rules.decide("Bash", {"command": command}, world.folder, cwd)


ALLOWED_COMMANDS = [
    "ls", "ls -la src", "cat notes.md", "head -n 5 notes.md", "tail -20 notes.md", "wc -l notes.md",
    "grep -rn foo .", 'grep -E "a|b" notes.md', "rg foo", "rg -n --glob '*.py' foo src", 'find . -name "*.py"',
    "git status", "git log --oneline -5", "git diff", "git show HEAD", "pwd", "which python3",
    "ls | wc -l", "git status && git diff", "cat notes.md | grep x | head -3", "ls; pwd",
    "ls *.py", "wc -l src/*.py", "cat .env.example", "grep -rn '.env' .",
]
ASKED_COMMANDS = [
    "pytest", "pytest -q tests/test_x.py::test_y", "python -m pytest -x", "python3 -m pytest", "uv run pytest -q",
    "npm test", "npm run build", "make test", "pytest | tail", "pytest && git status",  # they run what the worker wrote
    "echo hi", "mkdir out", "python3 gen.py", "git push", "git commit -m 'a b'", "git add .", "rm notes.md",
    "curl https://example.com", "cd sub", "touch x", "pip install requests", "npm install", "npm run lint",
    "ls; rm notes.md", "ls > out.txt", "ls >> out.txt", "ls &", "ls 2>&1", "cat < notes.md", "ls $(pwd)", "ls `pwd`",
    'echo "$HOME"', "ls .*", "ls ../*", "ls ~/*", "find . -exec rm {} \\;", "find . -delete", "find . -fprint out.txt",
    "rg --pre cat x", "git diff --output=out.txt", "git diff --no-index a b", "git -C other status",
    "pytest -p myplugin", "pytest ../other", "pytest --rootdir=/", "python -m pytest -c other.ini",
    "npm test -- --watch", "npm run build && npm publish", "make", "ls -la 'unclosed", "echo a\\", "ls {a,b}",
    "ls # comment", "env", "bash -c ls", "python3 -c 'print(1)'", "cat /etc/hosts", "ls /usr/local",
]
NEVER_COMMANDS = [
    "sudo ls", "ls && sudo rm x", "echo hi | sudo tee x", "doas ls", "su", "ls; su -",
    "cat ~/.ssh/id_rsa", "cat .env", "ls ~/.ssh", "cat ssh-link/config", "cat ~/.aws/credentials", "cp .env copy.txt",
    "grep -f .env notes.md", "rg --file=~/.ssh/config x", "ls ~", "find ~ -name x", "cd ~",
    "cp notes.md /etc/b", "mv notes.md /tmp/x", "rm /tmp/x", "touch /usr/local/x", "mkdir -p /opt/x", "tee /etc/x",
    "echo x > /etc/hosts", "echo x >> ~/.zshrc", "echo x > ../../../../tmp/y", "echo x > .git/hooks/pre-commit",
    "python3 -c \"print(open('~/.ssh/id_rsa'))\"", "cat $HOME/.aws/credentials", "ls /Users/me/.gnupg", "ln -s ~/.ssh k",
]


@pytest.mark.parametrize("command", ALLOWED_COMMANDS)
def test_only_read_only_commands_run_without_asking(world, command):
    assert bash(world, command).verdict == "allow", command


@pytest.mark.parametrize("command", ASKED_COMMANDS)
def test_every_other_command_is_put_to_the_user_with_the_exact_command(world, command):
    decision = bash(world, command)
    assert (decision.verdict, decision.kind, decision.subject) == ("ask", "run", command), command


@pytest.mark.parametrize("command", NEVER_COMMANDS)
def test_some_commands_are_never_run(world, command):
    decision = bash(world, command)
    assert decision.verdict == "never" and decision.reason, command


def test_a_glob_is_judged_by_the_files_it_would_match_not_by_how_it_reads(world):
    assert bash(world, "cat *.md").verdict == "allow"
    assert bash(world, "wc -l *").verdict == "never"  # * matches ssh-link, a link into ~/.ssh
    (world.folder / "ssh-link").unlink()
    assert bash(world, "wc -l *").verdict == "allow"  # nothing private is left for it to match
    for name in ("credentials.json", "server.pem", "id_rsa", "secrets.yaml"):
        (world.folder / name).write_text("x")
    for command in ("cat *", "head -n 3 *.json", "cat *.pem", "grep -rn x *", "cat cred*", "cat [is]*"):
        decision = bash(world, command)
        assert decision.verdict == "never" and "credential" in decision.reason, command
    assert bash(world, "cat *.md").verdict == "allow" and bash(world, "cat primes*").verdict == "allow"
    assert bash(world, "cat nothing*").verdict == "allow"  # matches nothing: the shell would only complain


def test_a_file_of_an_earlier_commit_is_not_a_way_round_the_secret_names(world):
    assert bash(world, "git show HEAD:primes.py").verdict == "allow"
    assert bash(world, "git show HEAD:.env").verdict == "never"
    assert bash(world, "git show HEAD~1:config/.env.local").verdict == "never"
    assert bash(world, "git diff HEAD -- .env").verdict == "never"
    assert bash(world, "git log --format=%H:%s").verdict == "allow"  # a colon in an option is not a path


def test_tests_and_builds_are_asked_and_a_plain_run_in_the_task_folder_may_be_remembered(world):
    for command, prefix in (
        ("pytest -q", "pytest"), ("python -m pytest -x tests", "python -m pytest"), ("python3 -m pytest", "python3 -m pytest"),
        ("uv run pytest -q", "uv run pytest"), ("npm test", "npm test"), ("npm run build", "npm run build"),
        ("make test", "make test"),
    ):
        decision = bash(world, command, cwd=world.folder / "sub")  # a folder inside the task folder is still in it
        assert (decision.verdict, decision.kind, decision.subject, decision.covers) == ("ask", "run", command, prefix), command
        assert "run code the worker has just written" in decision.reason
    for command in ("pytest -p myplugin", "pytest ../other", "pytest --rootdir=/", "python -m pytest -c other.ini",
                    "npm test -- --watch", "uv run pytest -p x"):
        decision = bash(world, command)  # the flags and places that were screened out before are asked every time
        assert (decision.verdict, decision.covers) == ("ask", ""), command
    for command in ("pytest", "npm test", "make test"):  # Bash has left the task folder: asked every time as well
        for elsewhere in (world.atlas, world.home):
            decision = bash(world, command, cwd=elsewhere)
            assert (decision.verdict, decision.covers) == ("ask", ""), (command, elsewhere)
    assert bash(world, "ls", cwd=world.atlas).verdict == "allow"  # reading is free in the user's folders


def test_a_test_run_is_remembered_as_that_test_run_and_nothing_wider(world):
    allowed = rules.Allowed()
    allowed.remember(bash(world, "pytest -q"))
    assert allowed.covers(bash(world, "pytest tests/test_x.py -x"))
    assert not allowed.covers(bash(world, "pytest -p myplugin")) and not allowed.covers(bash(world, "pytest ../other"))
    assert not allowed.covers(bash(world, "pytest", cwd=world.atlas))  # Bash has moved out of the task folder
    assert not allowed.covers(bash(world, "pytest | tail")) and not allowed.covers(bash(world, "python3 gen.py"))
    assert not allowed.covers(bash(world, "python -m pytest")) and not allowed.covers(bash(world, "npm test"))
    assert bash(world, "pytest -q").verdict == "ask"  # remembering belongs to the worker's task: the decision stays an ask

    allowed.remember(bash(world, "python3 -m unittest -v"))
    assert allowed.covers(bash(world, "python3 -m unittest discover tests"))
    assert not allowed.covers(bash(world, "python3 gen.py")) and not allowed.covers(bash(world, "python3 -m http.server"))


def test_a_search_with_no_place_named_looks_where_bash_is(world):
    ssh = world.home / ".ssh"
    assert bash(world, "grep -rn Host", cwd=ssh).verdict == "never"
    assert bash(world, "ls", cwd=ssh).verdict == "never"


def test_quotes_hide_separators_and_a_line_is_split_where_the_shell_would(world):
    assert rules.split_command("ls -la && echo 'a b;c' | wc -l; pwd") == [["ls", "-la"], ["echo", "a b;c"], ["wc", "-l"], ["pwd"]]
    assert rules.split_command('grep "a|b" "x y"') == [["grep", "a|b", "x y"]]
    assert rules.split_command("ls\npwd") == [["ls"], ["pwd"]]
    assert rules.split_command("echo 'it''s'") == [["echo", "its"]]
    assert rules.split_command("echo \"it's\"") == [["echo", "it's"]]
    for unclear in ("ls >o", "ls<f", "ls&", "a=$(b)", "echo `x`", "echo \"$x\"", "echo ${x}", "ls (", "ls #x", "x\\", "ls 'a"):
        assert rules.split_command(unclear) is None, unclear
    assert rules.split_command("   ") is None


# --- what "Allow for this task" remembers --------------------------------------------------


def test_what_allow_for_this_task_would_remember(world):
    prefixes = {
        "python3 gen.py": "python3", "mkdir out": "mkdir", "git commit -m x": "git commit", "npm run lint": "npm run lint",
        "pytest -q": "pytest", "python -m pytest -x": "python -m pytest", "python3 -m unittest -v": "python3 -m unittest",
        "uv run pytest": "uv run pytest", "npm test": "npm test", "npm run build": "npm run build", "make test": "make test",
        "python -m": "python", "python -m pytest -c x.ini": "", "python -m pytest -p x": "",
        "uv run mytool": "uv run mytool", "make deploy": "make deploy", "./build.sh --fast": "./build.sh",
        "python3 -c 'x'": "", "node -e x": "", "python3 -cprint(1)": "",
        "bash -c ls": "", "curl https://example.com": "", "ssh host": "", "env": "", "cd sub": "", "rm x": "", "mv a b": "",
        "git push": "", "git remote add x y": "", "git -C x status": "", "git": "", "npm install": "", "pip install x": "",
        "FOO=1 python3 x.py": "",
    }
    for command, expected in prefixes.items():
        assert rules.command_prefix(command.split()) == expected, command
    assert bash(world, "python3 gen.py").covers == "python3"
    assert bash(world, "ls; mkdir out").covers == ""  # a chain says nothing about its parts
    assert bash(world, "cat /etc/hosts").covers == ""  # a read-only command asked for its place is asked every time
    assert bash(world, "rg --pre cat x").covers == "" and bash(world, "pytest -p x").covers == ""
    assert bash(world, "pytest -q").covers == "pytest" and bash(world, "npm run build").covers == "npm run build"
    assert bash(world, "git diff --output=x").covers == ""


def test_what_the_user_allowed_is_remembered_by_kind_and_never_overrides_a_never(world):
    allowed = rules.Allowed()
    first = bash(world, "python3 gen.py")
    assert not allowed.covers(first)
    allowed.remember(first)
    assert allowed.covers(bash(world, "python3 other.py")) and allowed.covers(first)
    assert not allowed.covers(bash(world, "python3 -c 'x'")) and not allowed.covers(bash(world, "node a.js"))
    assert not allowed.covers(bash(world, "sudo python3 x")) and not allowed.covers(bash(world, "python3 ~/.ssh/x"))
    assert not allowed.covers(rules.ALLOW)

    host = decide(world, "WebFetch", url="https://example.com/a", prompt="x")
    allowed.remember(host)
    assert allowed.covers(decide(world, "WebFetch", url="https://example.com/b", prompt="y"))
    assert not allowed.covers(decide(world, "WebFetch", url="https://other.example/b", prompt="y"))

    write = decide(world, "Write", file_path=str(world.atlas / "a.md"), content="x")
    allowed.remember(write)
    assert allowed.covers(decide(world, "Write", file_path=str(world.atlas / "sub" / "b.md"), content="x"))
    assert not allowed.covers(decide(world, "Write", file_path=str(world.home / "Documents" / "b.md"), content="x"))
    assert not rules.Allowed().covers(first)  # another task knows nothing of it
