"""Jarvis reads from Google, may save a mail draft, and must never deliver mail.

Google's compose permission (needed to save drafts) also allows delivery, so
"never send" cannot be enforced by permissions. These tests scan the package
source instead and fail if a delivery call, endpoint or wider scope appears,
or if any Google API call is made other than a read or the one draft save.
"""

from __future__ import annotations

import ast
from pathlib import Path

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


# The one change Jarvis may make in Google, and the only module that may make it.
ALLOWED_WRITES = {("drafts", "create"): {"jarvis/google_api.py"}}


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
                problems.append(f"{filename}:{node.lineno}: .{receiver.func.attr}().send")
            elif Path(filename).name not in SEND_ATTRIBUTE_ALLOWED:
                problems.append(f"{filename}:{node.lineno}: an attribute called send")
        # Blocks reaching the method indirectly, e.g. getattr(resource, "send").
        if isinstance(node, ast.Constant) and node.value in FORBIDDEN_CONSTANTS:
            problems.append(f"{filename}:{node.lineno}: the string {node.value!r}")
    return problems


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


# The scanners themselves must catch each case, or the tests above are hollow.


def test_scanner_catches_a_message_send():
    code = "service.users().messages().send(userId='me', body=b).execute()\n"
    assert find_send_violations(code, "jarvis/mail.py")


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
