"""Jarvis reads from Google, may save a mail draft, and delivers mail in one place only.

Google's compose permission (needed to save drafts) also allows delivery, so
"never send" cannot be enforced by permissions. These tests scan the package
source instead. They fail if a delivery call, endpoint or wider scope appears
anywhere but in the one place that is allowed: ``GmailApi.send_draft`` in
``jarvis/google_api.py``, which refuses unless the user has said yes to that
very draft. They also fail if any Google API call is made other than a read, the
draft save, and that send, or if anything but ``jarvis/drafts.py`` calls it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from jarvis.approvals import Approval
from jarvis.google_api import GmailApi, NotApproved
from jarvis.ratelimit import RateLimiter

PACKAGE = Path(__file__).resolve().parent.parent / "jarvis"

# Gmail resources whose ``send`` method delivers mail.
SENDING_RESOURCES = {"messages", "drafts"}

FORBIDDEN_TEXT = (
    "messages/send",
    "drafts/send",
    "auth/gmail.send",
    "auth/gmail.modify",
)
# The full-mailbox scope, as a whole string. Links to the Gmail web app are fine.
FORBIDDEN_CONSTANTS = {"send", "https://mail.google.com/", "https://mail.google.com"}

# Modules allowed to use an attribute called ``send`` on something that is not
# Gmail (a socket, say). Empty: nothing needs it today.
SEND_ATTRIBUTE_ALLOWED: set[str] = set()

# Accessors of the Google API clients, and the only methods Jarvis may call on them.
GOOGLE_RESOURCES = {
    "users", "messages", "drafts", "threads", "labels", "history", "settings", "attachments",
    "calendarList", "calendars", "events", "acl", "freebusy",
    "files", "changes", "permissions", "revisions", "comments", "replies", "drives", "about",
    "spreadsheets", "values", "sheets",
}
READ_METHODS = {
    "list", "get", "getProfile", "export", "export_media", "get_media",
    "getStartPageToken", "batchGet", "list_next",
}


# The changes Jarvis may make in Google, and the only module that may make each: saving a draft, and delivering one.
ALLOWED_WRITES = {("drafts", "create"): {"jarvis/google_api.py"}, ("drafts", "send"): {"jarvis/google_api.py"}}
# The one delivery call (a resource and its ``send`` method) and where it may be.
ALLOWED_SENDS = {("drafts", "send"): {"jarvis/google_api.py"}}
# The wrapper that holds that call, and the only module that may use it.
SEND_WRAPPER = "send_draft"
SEND_WRAPPER_USERS = {"jarvis/drafts.py"}


def find_send_violations(source: str, filename: str) -> list[str]:
    problems = [f"{filename}: contains {text!r}" for text in FORBIDDEN_TEXT if text in source]
    for node in ast.walk(ast.parse(source, filename=filename)):
        if isinstance(node, ast.Attribute) and node.attr == "send":
            receiver = node.value
            chained = (
                isinstance(receiver, ast.Call)
                and isinstance(receiver.func, ast.Attribute)
                and receiver.func.attr in SENDING_RESOURCES
            )
            if chained:
                if filename not in ALLOWED_SENDS.get((receiver.func.attr, "send"), ()):
                    problems.append(f"{filename}:{node.lineno}: .{receiver.func.attr}().send")
            elif Path(filename).name not in SEND_ATTRIBUTE_ALLOWED:
                problems.append(f"{filename}:{node.lineno}: an attribute called send")
        # Blocks reaching the method indirectly, e.g. getattr(resource, "send").
        if isinstance(node, ast.Constant) and node.value in FORBIDDEN_CONSTANTS:
            problems.append(f"{filename}:{node.lineno}: the string {node.value!r}")
    return problems


def find_wrapper_violations(source: str, filename: str) -> list[str]:
    """Any use of the send wrapper (a call, or the method passed around) outside the one module that may."""
    if filename in SEND_WRAPPER_USERS or filename == "jarvis/google_api.py":
        return []
    return [
        f"{filename}:{node.lineno}: uses {SEND_WRAPPER}"
        for node in ast.walk(ast.parse(source, filename=filename))
        if isinstance(node, ast.Attribute) and node.attr == SEND_WRAPPER
    ]


def find_write_violations(source: str, filename: str) -> list[str]:
    """Calls like ``service.files().update(...)``: anything but a read."""
    problems = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        receiver = node.func.value
        on_resource = (
            isinstance(receiver, ast.Call)
            and isinstance(receiver.func, ast.Attribute)
            and receiver.func.attr in GOOGLE_RESOURCES
        )
        method = node.func.attr
        if on_resource and filename in ALLOWED_WRITES.get((receiver.func.attr, method), ()):
            continue
        if on_resource and method not in READ_METHODS and method not in GOOGLE_RESOURCES:
            problems.append(f"{filename}:{node.lineno}: .{receiver.func.attr}().{method}()")
    return problems


def _package_files() -> list[Path]:
    files = sorted(PACKAGE.rglob("*.py"))
    assert files, f"no source files found under {PACKAGE}"
    return files


def test_package_never_sends_mail():
    problems = []
    for path in _package_files():
        problems += find_send_violations(path.read_text(), str(path.relative_to(PACKAGE.parent)))

    assert problems == []


def test_package_only_reads_from_google():
    problems = []
    for path in _package_files():
        problems += find_write_violations(path.read_text(), str(path.relative_to(PACKAGE.parent)))

    assert problems == []


def test_only_drafts_py_uses_the_send_wrapper():
    problems = []
    for path in _package_files():
        problems += find_wrapper_violations(path.read_text(), str(path.relative_to(PACKAGE.parent)))

    assert problems == []


def _send_calls() -> list[tuple[str, ast.FunctionDef, ast.Call]]:
    """Every ``.drafts().send(...)`` in the package with the function it sits in."""
    found = []
    for path in _package_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for function in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
            for call in (n for n in ast.walk(function) if isinstance(n, ast.Call)):
                func = call.func
                if (
                    isinstance(func, ast.Attribute) and func.attr == "send"
                    and isinstance(func.value, ast.Call) and isinstance(func.value.func, ast.Attribute)
                    and func.value.func.attr == "drafts"
                ):
                    found.append((str(path.relative_to(PACKAGE.parent)), function, call))
    return found


def test_there_is_exactly_one_delivery_call_and_the_approval_check_comes_first():
    found = _send_calls()
    assert [(name, function.name) for name, function, _ in found] == [("jarvis/google_api.py", SEND_WRAPPER)]

    function = found[0][1]
    body = [node for node in function.body if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))]
    guard = body[0]  # the first statement: if not approval.allows("send_mail", draft_id): raise ...
    assert isinstance(guard, ast.If) and isinstance(guard.test, ast.UnaryOp) and isinstance(guard.test.op, ast.Not)
    check = guard.test.operand
    assert ast.unparse(check) == "approval.allows('send_mail', draft_id)"
    assert isinstance(guard.body[0], ast.Raise)
    assert guard.lineno < found[0][2].lineno


def test_the_delivery_wrapper_is_not_used_anywhere_else_in_the_package():
    code = "api.send_draft(draft_id, approval)\n"
    assert find_wrapper_violations(code, "jarvis/core.py")
    assert find_wrapper_violations("go = api.send_draft\ngo(i, a)\n", "jarvis/work.py")
    assert find_wrapper_violations(code, "jarvis/drafts.py") == []


# The scanners themselves must catch each case, or the tests above are hollow.


def test_scanner_catches_a_message_send():
    code = "service.users().messages().send(userId='me', body=b).execute()\n"
    assert find_send_violations(code, "jarvis/mail.py")
    assert find_send_violations(code, "jarvis/google_api.py")  # not even the allowed module may send a message


def test_scanner_allows_the_draft_send_in_one_module_only():
    code = "service.users().drafts().send(userId='me', body={'id': i}).execute()\n"
    assert find_send_violations(code, "jarvis/google_api.py") == []
    assert find_write_violations(code, "jarvis/google_api.py") == []
    for other in ("jarvis/drafts.py", "jarvis/core.py", "jarvis/work.py", "jarvis/mail.py"):
        assert find_send_violations(code, other), other
        assert find_write_violations(code, other), other
    saved = "d = service.users().drafts()\nd.send(userId='me', body=b)\n"
    assert find_send_violations(saved, "jarvis/google_api.py")  # only the one chained form is allowed


def test_scanner_catches_a_draft_send_across_lines():
    code = "(\n    service.users()\n    .drafts()\n    .send(userId='me', body=b)\n)\n"
    assert find_send_violations(code, "jarvis/mail.py")


def test_scanner_catches_a_send_on_a_saved_resource():
    code = "drafts = service.users().drafts()\ndrafts.send(userId='me', body=b)\n"
    assert find_send_violations(code, "jarvis/anything.py")


def test_scanner_catches_getattr():
    code = "getattr(service.users().drafts(), 'send')(userId='me', body=b)\n"
    assert find_send_violations(code, "jarvis/mail.py")


def test_scanner_catches_raw_endpoints_and_wider_scopes():
    for text in FORBIDDEN_TEXT:
        assert find_send_violations(f"X = 'https://example/{text}'\n", "jarvis/mail.py")
    assert find_send_violations("SCOPE = 'https://mail.google.com/'\n", "jarvis/auth.py")


def test_scanner_allows_links_to_the_gmail_web_app():
    code = "link = f'https://mail.google.com/mail/?authuser={a}#all/{t}'\n"
    assert find_send_violations(code, "jarvis/store.py") == []


def test_write_scanner_catches_changes_to_google_data():
    for call in (
        "svc.users().messages().modify(userId='me', id=i, body=b)",
        "svc.users().messages().trash(userId='me', id=i)",
        "svc.users().drafts().create(userId='me', body=b)",
        "svc.events().insert(calendarId='primary', body=b)",
        "svc.events().patch(calendarId='primary', eventId=e, body=b)",
        "svc.files().update(fileId=f, body=b)",
        "svc.files().delete(fileId=f)",
        "svc.spreadsheets().values().update(spreadsheetId=s, range=r, body=b)",
        "svc.permissions().create(fileId=f, body=b)",
    ):
        assert find_write_violations(call + ".execute()\n", "jarvis/x.py"), call


def test_write_scanner_allows_saving_a_draft_in_one_module_only():
    call = "svc.users().drafts().create(userId='me', body=b).execute()\n"
    assert find_write_violations(call, "jarvis/google_api.py") == []
    assert find_write_violations(call, "jarvis/drafts.py")
    assert find_write_violations(call, "jarvis/core.py")


def test_write_scanner_still_catches_everything_else_a_draft_can_do():
    for call in ("svc.users().drafts().update(userId='me', id=i, body=b)", "svc.users().drafts().delete(userId='me', id=i)"):
        assert find_write_violations(call + ".execute()\n", "jarvis/google_api.py"), call


def test_write_scanner_allows_reads():
    for call in (
        "svc.users().getProfile(userId='me')",
        "svc.users().messages().list(userId='me')",
        "svc.users().messages().get(userId='me', id=i)",
        "svc.users().history().list(userId='me', startHistoryId=h)",
        "svc.calendarList().list()",
        "svc.events().list(calendarId=c)",
        "svc.files().list(q=q)",
        "svc.files().export(fileId=f, mimeType=m)",
        "svc.changes().getStartPageToken()",
        "svc.changes().list(pageToken=t)",
        "svc.spreadsheets().get(spreadsheetId=s)",
        "svc.spreadsheets().values().batchGet(spreadsheetId=s, ranges=r)",
        "svc.about().get(fields='user')",
    ):
        assert find_write_violations(call + ".execute()\n", "jarvis/x.py") == [], call


# --- the interlock: the method itself refuses without the user's yes -----------------------


class _FakeResource:
    """Stands in for googleapiclient's chained resources and records what would have been sent."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._last = ""

    def users(self):
        return self

    def drafts(self):
        return self

    def send(self, **kwargs):
        self.calls.append(("send", kwargs))
        return self

    def execute(self):
        return {"id": "sent-1", "labelIds": ["SENT"]}


def _api() -> tuple[GmailApi, _FakeResource]:
    api = GmailApi.__new__(GmailApi)  # no network: the service is a recorder
    api._svc = _FakeResource()
    api._limiter = RateLimiter(1_000_000)
    return api, api._svc


def _approval(kind="send_mail", subject="r42", granted=True) -> Approval:
    return Approval("a1", kind, subject, "Send", "the mail", 0.0, granted=granted, by="click")


def test_sending_raises_unless_the_user_said_yes_to_that_very_draft():
    for approval in (
        _approval(granted=None),                   # nobody answered
        _approval(granted=False),                  # the user said no
        _approval(subject="r43"),                  # a yes, but for another draft
        _approval(kind="run"),                     # a yes, but to something else
        _approval(kind="write", subject="r42"),
    ):
        api, service = _api()
        with pytest.raises(NotApproved):
            api.send_draft("r42", approval)
        assert service.calls == [], approval


def test_a_yes_for_that_draft_sends_it_once_and_only_that_draft():
    api, service = _api()
    assert api.send_draft("r42", _approval())["id"] == "sent-1"
    assert service.calls == [("send", {"userId": "me", "body": {"id": "r42"}})]
