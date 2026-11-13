"""Email drafts.

Jarvis saves a draft in Gmail's Drafts folder and keeps Gmail's id for it. The
user reads it and sends it, or throws it away. Jarvis can also send a draft it
saved, but only after the user has said yes to this very mail: the whole mail
is shown as it stands in Gmail now, the yes is an ``Approval`` for that draft's
id, and ``GmailApi.send_draft`` checks it once more before anything leaves.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from email.message import EmailMessage
from email.utils import getaddresses

from jarvis import google_auth
from jarvis.approvals import Approval
from jarvis.config import Config
from jarvis.ratelimit import error_status
from jarvis.store import Store

BODY_MAX_CHARS = 20_000
_ADDRESS = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")


class DraftError(ValueError):
    """The draft could not be saved. The message is safe to show the user."""


@dataclass(frozen=True)
class Draft:
    account: str
    to: list[str]
    cc: list[str]
    subject: str
    thread_id: str | None
    in_reply_to: str | None = None
    references: str | None = None

    @property
    def link(self) -> str:
        return f"https://mail.google.com/mail/?authuser={self.account}#drafts"

    def summary(self) -> dict:
        return {**asdict(self), "link": self.link}


def addresses(value: str | list[str] | None) -> list[str]:
    """Plain addresses out of a header value or a list, in order, without repeats."""
    if not value:
        return []
    parts = value if isinstance(value, list) else [value]
    found = [address.strip() for _, address in getaddresses(parts) if address.strip()]
    return list(dict.fromkeys(found))


def reply_subject(subject: str) -> str:
    return subject if subject.lower().startswith("re:") else f"Re: {subject}".strip()


def plan(config: Config, store: Store, *, to: list[str] | None, cc: list[str] | None,
         subject: str | None, reply_to: int | None) -> Draft:
    """Work out where a draft goes. Reads the local copy only.

    For a reply, the recipient, subject and thread come from the mail being
    answered unless the caller overrides them.
    """
    account = config.primary_account.email.lower()
    thread_id = in_reply_to = references = None
    recipients, copies = addresses(to), addresses(cc)
    if reply_to is not None:
        doc = store.get_doc(int(reply_to))
        if doc is None or doc.kind != "mail":
            raise DraftError("reply_to must be the id of a mail found by search.")
        original = store.gmail_get(doc.account, doc.source_id)
        if original is None:
            raise DraftError("That mail is no longer in the local copy.")
        account = doc.account
        headers = json.loads(original["headers"] or "{}")
        thread_id = original["thread_id"]
        in_reply_to = headers.get("message-id")
        references = " ".join(part for part in (headers.get("references"), in_reply_to) if part) or None
        recipients = recipients or addresses(headers.get("reply-to") or original["sender"])
        subject = subject or reply_subject(original["subject"])
    if not recipients:
        raise DraftError("A draft needs at least one recipient address.")
    bad = [address for address in recipients + copies if not _ADDRESS.match(address)]
    if bad:
        raise DraftError(f"Not an email address: {', '.join(bad)}")
    if not (subject or "").strip():
        raise DraftError("A draft needs a subject.")
    return Draft(account, recipients, copies, subject.strip(), thread_id, in_reply_to, references)


def encode(draft: Draft, body: str) -> str:
    """The draft as Gmail wants it: an RFC 5322 message, base64url-encoded."""
    message = EmailMessage()
    message["To"] = ", ".join(draft.to)
    if draft.cc:
        message["Cc"] = ", ".join(draft.cc)
    message["Subject"] = draft.subject
    if draft.in_reply_to:
        message["In-Reply-To"] = draft.in_reply_to
    if draft.references:
        message["References"] = draft.references
    message.set_content(body[:BODY_MAX_CHARS])
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


def _gmail(account: str):
    """Gmail for one signed-in account. Tests put a fake in its place."""
    from jarvis import google_api

    try:
        creds = google_auth.load_credentials(account)
    except Exception as exc:
        raise DraftError(f"Google sign-in no longer works: {google_auth.describe_error(exc)}") from None
    if creds is None:
        raise DraftError(f"{account} is not signed in.")
    return google_api.GmailApi(creds)


def save(draft: Draft, body: str) -> dict:
    """Put the draft in Gmail's Drafts folder. Makes network calls; run it off the event loop.

    The result carries Gmail's id for the draft as ``draft_id``: sending it later needs that.
    """
    if not body.strip():
        raise DraftError("A draft needs some text.")
    api = _gmail(draft.account)
    try:
        created = api.create_draft(encode(draft, body), draft.thread_id)
    except Exception as exc:
        raise DraftError(f"Gmail did not save the draft: {google_auth.describe_error(exc)}") from None
    return {**draft.summary(), "draft_id": str(created.get("id") or "")}


async def save_async(config: Config, store: Store, *, body: str, to: list[str] | None = None,
                     cc: list[str] | None = None, subject: str | None = None,
                     reply_to: int | None = None) -> dict:
    draft = plan(config, store, to=to, cc=cc, subject=subject, reply_to=reply_to)  # reads the store here
    return await asyncio.to_thread(save, draft, body)


# --- sending, after the user's yes ---------------------------------------------------


@dataclass(frozen=True)
class StoredDraft:
    """A draft Jarvis saved: its number in the record of what Jarvis made, and Gmail's id for it."""

    number: int
    account: str
    draft_id: str
    subject: str


@dataclass(frozen=True)
class Outgoing:
    """A draft as it stands in Gmail: the whole mail as it would go out."""

    sender: str
    to: str
    cc: str
    bcc: str
    subject: str
    body: str
    attachments: tuple[str, ...] = ()

    def text(self) -> str:
        lines = [f"From: {self.sender}", f"To: {self.to}"]
        lines += [f"{name}: {value}" for name, value in (("Cc", self.cc), ("Bcc", self.bcc)) if value]
        lines.append(f"Subject: {self.subject}")
        if self.attachments:
            lines.append("Attachments: " + ", ".join(self.attachments))
        return "\n".join(lines) + "\n\n" + self.body

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.text().encode("utf-8")).hexdigest()


def stored(store: Store, number: int) -> StoredDraft:
    """The saved draft Jarvis made under this number, or DraftError."""
    row = store.artifact_get(int(number))
    if row is None or row["kind"] != "draft":
        raise DraftError("That is not a draft Jarvis saved. Use the number it was saved under.")
    if not row["meta"].get("draft_id"):
        raise DraftError("Jarvis has no Gmail id for that draft (it was saved before ids were kept). It can be sent from Gmail's Drafts folder.")
    return StoredDraft(row["id"], row["meta"].get("account") or "", row["meta"]["draft_id"], row["title"])


def outgoing(resource: dict, account: str) -> Outgoing:
    """Read a draft as Gmail returns it (format full). DraftError if it could not be shown whole."""
    from jarvis import gmail_sync

    if not resource.get("message"):
        raise DraftError("Gmail returned no mail for that draft.")
    record = gmail_sync.parse_message(resource["message"])
    if record.body_truncated:
        raise DraftError("The draft is too long to show in full here. Send it from Gmail's Drafts folder.")
    if not (record.recipients or record.cc or record.bcc):
        raise DraftError("The draft has no recipient.")
    return Outgoing(
        record.sender or account, record.recipients, record.cc, record.bcc, record.subject, record.body,
        tuple(record.attachments),
    )


def _fetch(api, stored_draft: StoredDraft) -> Outgoing:
    try:
        resource = api.get_draft(stored_draft.draft_id)
    except Exception as exc:
        if error_status(exc) == 404:
            raise DraftError("The draft is no longer in Gmail: it was sent or deleted.") from None
        raise DraftError(f"Gmail would not show the draft: {google_auth.describe_error(exc)}") from None
    return outgoing(resource, stored_draft.account)


def read(stored_draft: StoredDraft) -> Outgoing:
    """The draft as it is in Gmail right now, which is what would go out. Run it off the event loop."""
    return _fetch(_gmail(stored_draft.account), stored_draft)


def deliver(stored_draft: StoredDraft, shown: Outgoing, approval: Approval) -> None:
    """Deliver a saved draft. Only with the user's yes for it, and only if it is still what they were shown.

    Makes network calls; run it off the event loop.
    """
    from jarvis import google_api

    if not approval.allows("send_mail", stored_draft.draft_id):
        raise DraftError("The user has not said yes to sending this draft, so it was not sent.")
    api = _gmail(stored_draft.account)
    if _fetch(api, stored_draft).digest != shown.digest:
        raise DraftError("The draft changed in Gmail while the user was deciding. Nothing was sent; ask again to show them the new text.")
    try:
        api.send_draft(stored_draft.draft_id, approval)
    except google_api.NotApproved as exc:
        raise DraftError(str(exc)) from None
    except Exception as exc:
        raise DraftError(
            f"Gmail did not confirm the send ({google_auth.describe_error(exc)}). It may or may not have gone: "
            "look in Gmail's Sent folder before trying again."
        ) from None
