"""Email drafts.

Jarvis saves a draft in Gmail's Drafts folder and stops there: the user reads
it and sends it, or throws it away. Nothing in this package delivers mail.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from dataclasses import asdict, dataclass
from email.message import EmailMessage
from email.utils import getaddresses

from jarvis import google_auth
from jarvis.config import Config
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


def save(draft: Draft, body: str) -> dict:
    """Put the draft in Gmail's Drafts folder. Makes network calls; run it off the event loop."""
    from jarvis import google_api

    if not body.strip():
        raise DraftError("A draft needs some text.")
    try:
        creds = google_auth.load_credentials(draft.account)
    except Exception as exc:
        raise DraftError(f"Google sign-in no longer works: {google_auth.describe_error(exc)}") from None
    if creds is None:
        raise DraftError(f"{draft.account} is not signed in.")
    try:
        google_api.GmailApi(creds).create_draft(encode(draft, body), draft.thread_id)
    except Exception as exc:
        raise DraftError(f"Gmail did not save the draft: {google_auth.describe_error(exc)}") from None
    return draft.summary()


async def save_async(config: Config, store: Store, *, body: str, to: list[str] | None = None,
                     cc: list[str] | None = None, subject: str | None = None,
                     reply_to: int | None = None) -> dict:
    draft = plan(config, store, to=to, cc=cc, subject=subject, reply_to=reply_to)  # reads the store here
    return await asyncio.to_thread(save, draft, body)
