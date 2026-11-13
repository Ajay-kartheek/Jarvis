"""What a work task may do: the permission decisions, as plain functions that need no model.

:func:`decide` says, for one tool call of the worker, whether it is allowed,
must be put to the user, or is never done. The worker (``jarvis/work.py``)
asks it twice for every call, in its hook and in its permission callback, and
this module knows nothing of models, approvals or the network.

=========================================  =====================================================================
allowed without asking                     reading and searching in the user's own folders; writing in the task
                                           folder and in ~/Documents/Jarvis Notes; the read-only commands ls,
                                           cat, head, tail, wc, grep, rg, find (no -delete or -exec),
                                           git status/log/diff/show, pwd, which
put to the user, who may refuse            every other command, running the project's tests or build included
                                           (pytest, python -m pytest, uv run pytest, npm test, npm run build,
                                           make test: they run code the worker has just written); a file
                                           anywhere else in the home folder; every web search (the worker can
                                           read the user's mail and notes, and words sent to a search engine
                                           can carry them out); every web page (by host); reading outside the
                                           user's own folders (outside the home folder, ~/Library, hidden
                                           files of the home folder)
never, whatever the user says              sudo; writing outside the home folder, or in hidden, Library, .git or
                                           .claude folders; reading keys, tokens, .env files, credential-named
                                           files, .git/config and the settings kept in ~/.ssh, ~/.config,
                                           ~/.claude and the like; the whole home folder as a place to search
=========================================  =====================================================================

A command is read as plain commands joined by ; && || | and new lines, and
anything else (a redirect, $( ), a backtick, a variable, a wide glob) is put
to the user in full. A glob is judged by the files it would match. "Allow for
this task" remembers a command's first word (two for git, npm and the like,
and ``python -m`` with its module), a host, "web search", or a folder. A plain
yes covers that one call. Nothing is remembered for commands that run other
commands, reach the network, delete in bulk or move the shell, nor for a
chain, nor for a command that is asked only because of a risky flag or because
it runs outside the task folder: those are asked every time.
"""

from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from jarvis import agents

GLOB_MATCHES = 500  # the most files of one glob that are judged one by one
ASK_TOOL = "mcp__task__ask_user"
DELIVER_TOOL = "mcp__task__deliver"
OWN_TOOLS = frozenset([*agents.PRIVATE_TOOLS, ASK_TOOL, DELIVER_TOOL])
NOTES_FOLDER = ("Documents", "Jarvis Notes")
WEB_SEARCH = "web search"  # the subject of the question about a search, as a host is of a fetch


# --- the decision ------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    verdict: str  # "allow", "ask" or "never"
    reason: str = ""  # in words the worker and the user can both read
    kind: str = ""  # what an "ask" is about: run, write, read or fetch
    subject: str = ""  # exactly what is asked about: the command, the path, the host
    covers: str = ""  # what "Allow for this task" would remember: a command prefix, a host or a folder; "" if nothing


ALLOW = Decision("allow")


def never(reason: str) -> Decision:
    return Decision("never", reason)


def ask(reason: str, kind: str, subject: str, covers: str = "") -> Decision:
    return Decision("ask", reason, kind, subject, covers)


def decide(tool_name: str, tool_input: dict, folder: Path, cwd: Path | None = None) -> Decision:
    """What one tool call may do. ``folder`` is the task folder, ``cwd`` where Bash is running now.

    Pure: it looks at the disk only to follow links, and calls no model.
    """
    rule = _RULES.get(tool_name)
    if rule is None:
        return ALLOW if tool_name in OWN_TOOLS else never(f"{tool_name} is not one of the tools of this work.")
    try:
        return rule(tool_input or {}, folder, cwd or folder)
    except (OSError, ValueError, TypeError):  # a path that cannot even be read as a path: do not guess
        return never(f"It was not possible to tell what {tool_name} would touch, so it is not done.")


def _worst(decisions: list[Decision]) -> Decision:
    order = {"never": 0, "ask": 1, "allow": 2}
    return min(decisions, key=lambda decision: order[decision.verdict])


@dataclass
class Allowed:
    """What the user allowed "for this task". Kept for that task only, never across tasks."""

    commands: set[str] = field(default_factory=set)
    hosts: set[str] = field(default_factory=set)
    folders: set[str] = field(default_factory=set)

    def remember(self, decision: Decision) -> None:
        if decision.covers:
            {"run": self.commands, "fetch": self.hosts}.get(decision.kind, self.folders).add(decision.covers)

    def covers(self, decision: Decision) -> bool:
        """True when an earlier "Allow for this task" already settles this question. Never overrides "never"."""
        if decision.verdict != "ask" or not decision.covers:
            return False
        if decision.kind == "run":
            return decision.covers in self.commands
        if decision.kind == "fetch":
            return decision.covers in self.hosts
        return any(under(Path(decision.covers), Path(folder)) for folder in self.folders)


# --- where a path leads ------------------------------------------------------------

# Folders of the home folder that hold keys, settings and logins, and places on the system that hold secrets.
PROTECTED = (".ssh", ".config", ".claude", ".aws", ".gnupg", ".kube", ".docker", "Library/Keychains")
SYSTEM_SECRETS = (
    "/private/etc/ssh", "/private/etc/sudoers", "/private/etc/sudoers.d", "/private/etc/master.passwd",
    "/private/var/db/dslocal", "/private/var/root", "/Library/Keychains",
)
# Source code is judged by what it is: a file called tokens.py or password_reset.py is code, not a credential.
SOURCE_SUFFIXES = frozenset({
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".java", ".kt", ".swift", ".c", ".h", ".cc",
    ".cpp", ".hpp", ".cs", ".rb", ".php", ".sh", ".sql", ".md", ".rst", ".html", ".css", ".vue", ".svelte",
})
_PLACEHOLDER_SUFFIXES = (".example", ".sample", ".template", ".dist")
_KEY_FILE = re.compile(
    r"^(id_(rsa|dsa|ecdsa|ed25519)(_.*)?|\.netrc|\.npmrc|\.pypirc|\.pgpass|\.git-credentials|\.htpasswd"
    r"|.+\.(pem|key|p12|pfx|jks|keystore|ppk|kdbx))$"
)
_CREDENTIAL_WORD = re.compile(
    r"(?<![a-z0-9])(credentials?|secrets?|passwords?|passwd|tokens?|api[-_ ]?keys?|private[-_ ]?keys?|access[-_ ]?keys?)(?![a-z0-9])"
)


def home() -> Path:
    return Path(os.path.realpath(Path.home()))


def notes_folder() -> Path:
    return Path(os.path.realpath(Path.home().joinpath(*NOTES_FOLDER)))


def real(path: str, base: Path) -> Path:
    """Where a path from the model really leads: ``~`` opened, a relative path taken from ``base``, links followed."""
    text = path.strip().removeprefix("file://")
    if text == "~" or text.startswith("~/"):
        target = Path.home() / text[2:]
    elif text.startswith("~"):
        target = Path(os.path.expanduser(text))  # ~someone/...
    else:
        target = Path(text)
    if not target.is_absolute():
        target = base / target
    return Path(os.path.realpath(target))


def shown(path: Path | str) -> str:
    """A path as the user would write it: the home folder as ~."""
    text, mine = str(path), str(Path.home())
    return "~" + text[len(mine):] if text == mine or text.startswith(mine + "/") else text


def under(path: Path, root: Path) -> bool:
    """True when ``path`` is ``root`` or inside it. Case is ignored, as it is on the Mac's disk."""
    inside, top = path.parts, root.parts
    return len(inside) >= len(top) and all(a.casefold() == b.casefold() for a, b in zip(inside, top))


def _protected(path: Path, mine: Path) -> bool:
    roots = [mine / name for name in PROTECTED]
    roots += [Path(os.path.realpath(root)) for root in roots]  # the folder may itself be a link
    return any(under(path, root) for root in roots)


def secret_name(name: str) -> bool:
    """True for a file named like a credential (a key, a token, a password, a .env file)."""
    low = name.casefold()
    if low.endswith(_PLACEHOLDER_SUFFIXES):
        return False
    if low == ".env" or low.startswith(".env.") or low.endswith(".env") or _KEY_FILE.match(low):
        return True
    return Path(low).suffix not in SOURCE_SUFFIXES and bool(_CREDENTIAL_WORD.search(low))


def read_zone(path: Path) -> tuple[str, str]:
    """Reading ``path`` is "work" (go ahead), "ask" or "never", and why."""
    mine = home()
    if not under(path, mine):
        if any(under(path, Path(root)) for root in SYSTEM_SECRETS):
            return "never", f"{shown(path)} holds secrets of this Mac."
        return "ask", f"{shown(path)} is outside your home folder."
    inside = path.parts[len(mine.parts):]
    if not inside:
        return "never", "That is the whole home folder, which holds secrets. Name a folder inside it."
    if _protected(path, mine):
        return "never", f"{shown(path)} holds secrets (keys, settings or logins)."
    if (secret_name(path.name) and not path.is_dir()) or (path.name == "config" and path.parent.name == ".git"):
        return "never", f"{path.name} is named like a credential, or can hold one (a remote's login)."
    if inside[0].startswith(".") or inside[0].casefold() == "library":
        return "ask", f"{shown(path)} is settings or app data, not one of your own folders."
    return "work", ""


def write_zone(path: Path, folder: Path) -> tuple[str, str]:
    """Writing ``path`` is "allow" (inside the task folder or the notes folder), "ask" or "never", and why."""
    mine = home()
    if not under(path, mine):
        return "never", f"{shown(path)} is outside your home folder, and nothing is written there."
    inside = path.parts[len(mine.parts):]
    if not inside or inside[0].startswith(".") or inside[0].casefold() == "library" or _protected(path, mine):
        return "never", f"{shown(path)} is settings, secrets or app data, which Jarvis never writes."
    if any(part.casefold() in (".git", ".claude") for part in inside) or path.name == ".mcp.json":
        return "never", "Git's own files and Claude Code's settings are not written by hand."
    if under(path, folder) or under(path, notes_folder()):
        return "allow", ""
    return "ask", f"{shown(path)} is outside the task folder."


# --- files, searches and the web -----------------------------------------------------


def _reading(text: str, base: Path) -> Decision:
    if not text.strip():
        return never("No path was given.")
    path = real(text, base)
    zone, why = read_zone(path)
    if zone == "never":
        return never(why)
    if zone == "ask":
        return ask(why, "read", str(path), covers=str(path if path.is_dir() else path.parent))
    return ALLOW


def _read(tool_input: dict, folder: Path, cwd: Path) -> Decision:
    return _reading(str(tool_input.get("file_path") or ""), folder)


def _search(tool_input: dict, folder: Path, cwd: Path) -> Decision:
    """Glob and Grep: where the search starts, and for a Glob where its own pattern points."""
    root = str(tool_input.get("path") or ".")
    found = [_reading(root, folder)]
    pattern = str(tool_input.get("pattern") or "")
    if pattern.startswith(("/", "~")) or ".." in pattern.split("/"):
        literal = []
        for part in pattern.split("/"):
            if any(mark in part for mark in "*?[{"):
                break
            literal.append(part)
        found.append(_reading("/".join(literal) or ".", real(root, folder)))
    return _worst(found)


def _write(tool_input: dict, folder: Path, cwd: Path) -> Decision:
    text = str(tool_input.get("file_path") or "")
    if not text.strip():
        return never("No path was given.")
    path = real(text, folder)
    zone, why = write_zone(path, folder)
    if zone == "never":
        return never(why)
    if zone == "ask":
        return ask(why, "write", str(path), covers=str(path.parent))
    return ALLOW


def host_of(url: str) -> str | None:
    """The host a link goes to, with its port when that is not the usual one; None unless it is a plain web link."""
    text = url.strip()
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    if "@" in parts.netloc or "\\" in text or any(char.isspace() for char in text):
        return None  # the link could lead somewhere other than where it reads
    host = parts.hostname.rstrip(".").lower()
    return host if port in (None, {"http": 80, "https": 443}[parts.scheme]) else f"{host}:{port}"


def _web_search(tool_input: dict, folder: Path, cwd: Path) -> Decision:
    if not str(tool_input.get("query") or "").strip():
        return never("No search words were given.")
    return ask(
        "The worker can read the user's mail and notes, and words sent to a search engine can carry them out, so a search is put to the user.",
        "fetch", WEB_SEARCH, covers=WEB_SEARCH,
    )


def _fetch(tool_input: dict, folder: Path, cwd: Path) -> Decision:
    host = host_of(str(tool_input.get("url") or ""))
    if host is None:
        return never("Only plain web links (http or https, without a user name) are fetched.")
    return ask(f"{host} has not been allowed for this task.", "fetch", host, covers=host)


# --- commands ------------------------------------------------------------------------

READ_ONLY = frozenset({"ls", "cat", "head", "tail", "wc", "grep", "rg", "find", "pwd", "which"})
GIT_READ = frozenset({"status", "log", "diff", "show"})
GIT_RISKY = ("--output", "--no-index", "--ext-diff")  # write a file, read outside the repository, run another program
FIND_RISKY = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"})
RG_RISKY = ("--pre", "--pre-glob", "--hostname-bin")  # run another program on the files
WRITERS = frozenset({
    "cp", "mv", "rm", "rmdir", "mkdir", "touch", "tee", "ln", "install", "chmod", "chown", "truncate", "unlink",
    "rsync", "dd",
})
ELEVATED = frozenset({"sudo", "doas", "su", "pkexec"})
TEST_COMMANDS = (["npm", "test"], ["npm", "run", "build"], ["make", "test"])
PYTEST_FORMS = (["pytest"], ["python", "-m", "pytest"], ["python3", "-m", "pytest"], ["uv", "run", "pytest"])
PYTEST_RISKY = ("-p", "-c", "-o", "--rootdir", "--confcutdir", "--override-ini", "--basetemp", "--import-mode", "--pyargs")

# "Allow for this task" never remembers these, so each is put to the user every time: commands that run whatever
# follows, reach the network or another machine, delete or change files in bulk, show the environment or move the
# shell elsewhere, and the forms of git, npm, pip and others that publish, install or fetch.
NOT_REMEMBERED = frozenset({
    "bash", "sh", "zsh", "fish", "dash", "ksh", "env", "xargs", "nohup", "exec", "eval", "command", "builtin", "time",
    "timeout", "nice", "watch", "source", ".", "osascript", "open",
    "curl", "wget", "nc", "ncat", "telnet", "ssh", "scp", "sftp", "ftp", "rsync", "gh", "docker", "brew",
    "rm", "rmdir", "mv", "dd", "truncate", "unlink", "shred", "chmod", "chown", "kill", "killall", "pkill", "launchctl",
    "defaults", "crontab", "printenv", "export", "set", "cd", "pushd", "popd", *ELEVATED,
    "git push", "git pull", "git fetch", "git clone", "git remote", "git config", "git credential",
    "npm install", "npm i", "npm ci", "npm publish", "npm exec", "npm login", "pip install", "pip3 install",
    "uv add", "uv pip", "uv tool", "cargo install", "cargo publish", "go install", "go get",
})
SUBCOMMAND_TOOLS = frozenset({"git", "npm", "pnpm", "yarn", "pip", "pip3", "uv", "cargo", "go", "make"})
INTERPRETERS = frozenset({"python", "python3", "node", "ruby", "perl", "php", "deno", "bun", "swift"})
INLINE_CODE = frozenset({"-c", "-e", "-E", "-p", "-pe", "-ne", "--eval", "--print"})

_ELEVATED_TEXT = re.compile(r"(?<![\w./-])(?:sudo|doas|pkexec)(?![\w-])")
_HOME_SECRETS = re.compile(
    r"(?:~|\$HOME|\$\{HOME\}|/Users/[^/\s'\"]+|/home/[^/\s'\"]+)/(?:\.(?:ssh|gnupg|aws|kube|docker|claude|config)\b|Library/Keychains)",
    re.IGNORECASE,
)
_KEY_NAMES = re.compile(r"\bid_(?:rsa|dsa|ecdsa|ed25519)\b(?!\.pub)")
_REDIRECT = re.compile(r">>?\s*(?!&)([^\s;&|<>()]+)")
_PLAIN_GLOB = re.compile(r"^(?!\.)(?!.*(?:^|/)\.)[\w@%+=:,./*?\[\]-]+$")


def split_command(command: str) -> list[list[str]] | None:
    """The plain commands of a line, each as its words; None when the line is anything more than that.

    Understands quotes, backslashes and the separators ; && || | and a new
    line. A redirect, a background job, $( ), a backtick, a $variable, braces,
    a comment or a glob that could leave the folder make the line "not plain":
    then the answer is None and the caller has to ask.
    """
    parts: list[list[str]] = []
    words: list[str] = []
    chars: list[str] = []
    started = glob = False  # a word is under way (even an empty one, ''); it holds an unquoted * ? or [
    quote = ""

    def end_word() -> bool:
        nonlocal started, glob
        if not started:
            return True
        word = "".join(chars)
        plain = not glob or (_PLAIN_GLOB.match(word) and ".." not in word)
        words.append(word)
        chars.clear()
        started = glob = False
        return bool(plain)

    def end_part() -> bool:
        ok = end_word()
        if words:
            parts.append(list(words))
            words.clear()
        return ok

    index = 0
    while index < len(command):
        char = command[index]
        following = command[index + 1] if index + 1 < len(command) else ""
        index += 1
        if quote == "'":
            if char == "'":
                quote = ""
            else:
                chars.append(char)
        elif quote == '"':
            if char == '"':
                quote = ""
            elif char in "$`":
                return None
            elif char == "\\" and following in ('"', "\\", "$", "`"):
                chars.append(following)
                index += 1
            else:
                chars.append(char)
        elif char in "'\"":
            quote, started = char, True
        elif char == "\\":
            if not following or following == "\n":
                return None
            chars.append(following)
            started = True
            index += 1
        elif char in " \t":
            if not end_word():
                return None
        elif char in ";\n|&":
            if char == "&" and following != "&":
                return None  # a background job, or a redirect such as 2>&1
            if not end_part():
                return None
            if following == char and char in "|&":
                index += 1
        elif char in "<>`$(){}#\r\x00":
            return None
        else:
            glob = glob or char in "*?["
            chars.append(char)
            started = True
    if quote or not end_part():
        return None
    return parts or None


def _path_words(name: str, args: list[str]) -> list[str]:
    """The words of a command that may name a file or folder: not the options, links or the pattern of a search."""
    if name == "find":  # the places to search come before the first test or action
        stop = next((i for i, arg in enumerate(args) if arg.startswith(("-", "!", "("))), len(args))
        return args[:stop]
    own_pattern = any(arg in ("-e", "-f", "--regexp", "--file") or arg.startswith(("--regexp=", "--file=")) for arg in args)
    pattern_wanted = name in ("grep", "rg") and not own_pattern
    found: list[str] = []
    skip = False
    for arg in args:
        if skip:
            skip = False
        elif arg in ("-e", "--regexp"):
            skip = True
        elif arg.startswith("-") and len(arg) > 1:
            if "=" in arg:
                found.append(arg.split("=", 1)[1])
        elif "://" in arg:
            continue
        elif pattern_wanted:
            pattern_wanted = False
        else:
            found.append(arg)
    return found


def _paths_of(word: str, cwd: Path) -> list[Path]:
    """Where a word of a command leads. A glob leads to every file it matches, so each of those is judged too."""
    if not any(mark in word for mark in "*?["):
        return [real(word, cwd)]
    found = sorted(glob.glob(word, root_dir=str(cwd)))[:GLOB_MATCHES]  # like the shell, hidden files only when named
    return [real(name, cwd) for name in found] or [real(word, cwd)]


def _read_only(words: list[str]) -> bool:
    name, args = words[0], words[1:]
    if name == "git":
        return bool(args) and args[0] in GIT_READ and not any(arg.split("=")[0] in GIT_RISKY for arg in args[1:])
    if name == "find":
        return not any(arg in FIND_RISKY for arg in args)
    if name == "rg":
        return not any(arg.split("=")[0] in RG_RISKY for arg in args)
    return name in READ_ONLY


def _is_test_or_build(words: list[str], folder: Path, cwd: Path) -> bool:
    """A plain run of pytest, npm test, npm run build, uv run pytest or make test, with Bash in the task folder."""
    if not under(cwd, folder):
        return False
    if words in [list(form) for form in TEST_COMMANDS]:
        return True
    for form in PYTEST_FORMS:
        if words[: len(form)] == list(form):
            return all(_test_argument(arg, folder, cwd) for arg in words[len(form):])
    return False


def _test_argument(arg: str, folder: Path, cwd: Path) -> bool:
    if arg.split("=", 1)[0] in PYTEST_RISKY:
        return False  # these load plugins or settings from somewhere else
    if "/" in arg or arg.startswith((".", "~")):
        return under(real(arg.split("::")[0], cwd), folder)
    return True


def command_prefix(words: list[str]) -> str:
    """What "Allow for this task" would remember of a single command: its first word, or two for tools like git.

    "" when the command has to be put to the user each time.
    """
    first = words[0]
    if first in NOT_REMEMBERED or "=" in first:
        return ""
    prefix = first
    if first in INTERPRETERS:  # a script, yes; code typed on the command line, no
        if any(arg in INLINE_CODE or arg.startswith("-c") for arg in words[1:]):
            return ""
        if words[1:2] == ["-m"] and len(words) > 2:  # python -m pytest says what it runs, python alone does not
            prefix = " ".join(words[:3])
    elif first in SUBCOMMAND_TOOLS:
        if len(words) < 2 or words[1].startswith("-"):
            return ""
        run_script = first in ("npm", "pnpm", "yarn", "uv") and words[1] == "run" and len(words) > 2
        prefix = " ".join(words[: 3 if run_script else 2])
    return "" if prefix in NOT_REMEMBERED else prefix


def _starts_like_a_test(words: list[str]) -> bool:
    return any(words[: len(form)] == list(form) for form in (*PYTEST_FORMS, *TEST_COMMANDS))


def _near_miss(words: list[str], folder: Path, cwd: Path) -> bool:
    """True when a command is asked only because of a flag or a place that the rules screen out.

    "Allow for this task" must not turn that screen off, so such a command is asked every time. A plain test or
    build run in the task folder is not one: it may be remembered.
    """
    name, args = words[0], words[1:]
    if name in READ_ONLY or (name == "git" and args[:1] and args[0] in GIT_READ):
        return True
    return _starts_like_a_test(words) and not _is_test_or_build(words, folder, cwd)


def _barred(command: str, folder: Path, cwd: Path) -> str | None:
    """Why a command is never run whatever it is, judged from its text alone; None if nothing bars it."""
    if _ELEVATED_TEXT.search(command):
        return "sudo is never run, not even with the user's yes."
    if _HOME_SECRETS.search(command) or _KEY_NAMES.search(command):
        return "The command names a place that holds secrets (keys, settings or logins)."
    for target in _REDIRECT.findall(command):
        target = target.strip("'\"")
        if target in ("/dev/null", "/dev/stdout", "/dev/stderr"):
            continue
        zone, why = write_zone(real(target, cwd), folder)
        if zone == "never":
            return why
    return None


def _classify(words: list[str], folder: Path, cwd: Path) -> tuple[str, str]:
    """One plain command: ("allow" | "ask" | "never", why)."""
    name, args = words[0], words[1:]
    if name in ELEVATED:
        return "never", f"{name} is never run, not even with the user's yes."
    asked = ""
    targets = _path_words(name, args) or ([str(cwd)] if _read_only(words) else [])  # a search with no place named looks here
    if name == "git":  # git show HEAD:path reads a file of an earlier commit
        targets += [arg.split(":", 1)[1] for arg in args if ":" in arg and not arg.startswith("-")]
    for path in (path for target in targets for path in _paths_of(target, cwd)):
        zone, why = read_zone(path)
        if zone == "never":
            return "never", why
        if name in WRITERS:
            zone_for_writing, why_not = write_zone(path, folder)
            if zone_for_writing == "never":
                return "never", why_not
        if zone == "ask":
            asked = why
    if asked:
        return "ask", asked
    if _read_only(words):
        return "allow", ""
    if _starts_like_a_test(words):
        return "ask", "Tests and builds run code the worker has just written, so each run is put to the user."
    return "ask", "It is not one of the commands that run without asking."


def decide_bash(command: str, folder: Path, cwd: Path) -> Decision:
    """A Bash command: a line of plain commands that are all read-only goes ahead; anything else is asked."""
    if not command.strip():
        return never("There is no command.")
    barred = _barred(command, folder, cwd)
    if barred:
        return never(barred)
    parts = split_command(command)
    if parts is None:
        return ask("It is more than one plain command (it chains, redirects, substitutes or uses a wide glob), so it is shown to you in full.", "run", command)
    verdicts = [_classify(words, folder, cwd) for words in parts]
    for verdict, why in verdicts:
        if verdict == "never":
            return never(why)
    asked = [why for verdict, why in verdicts if verdict == "ask"]
    if not asked:
        return ALLOW
    single = len(parts) == 1 and not _near_miss(parts[0], folder, cwd)
    return ask(asked[0], "run", command, covers=command_prefix(parts[0]) if single else "")


_RULES = {
    "Read": _read,
    "Glob": _search,
    "Grep": _search,
    "Write": _write,
    "Edit": _write,
    "WebSearch": _web_search,
    "WebFetch": _fetch,
    "Bash": lambda tool_input, folder, cwd: decide_bash(str(tool_input.get("command") or ""), folder, cwd),
}
