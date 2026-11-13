from __future__ import annotations

import base64
import email

import pytest

from jarvis import assistant, drafts
from jarvis.config import load_config
from jarvis.store import MailRecord, Store


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "jarvis.db") as opened:
        yield opened


@pytest.fixture
def config():
    return load_config()


def _decode(raw: str):
    return email.message_from_bytes(base64.urlsafe_b64decode(raw))


def _original(store, account):
    store.gmail_upsert(account, [MailRecord(
        id="m1", thread_id="t1", internal_date=1_700_000_000_000, subject="Rehearsal date",
        sender="Ravi Kumar <ravi@example.com>", recipients=account, body="Can we move the rehearsal?",
        headers={"message-id": "<abc@mail.example.com>", "references": "<first@mail.example.com>"},
    )])
    return store.search("rehearsal")[0].doc_id


def test_a_reply_takes_recipient_subject_and_thread_from_the_mail(store, config):
    account = config.primary_account.email.lower()
    draft = drafts.plan(config, store, to=None, cc=None, subject=None, reply_to=_original(store, account))
    assert draft.to == ["ravi@example.com"]
    assert draft.subject == "Re: Rehearsal date"
    assert draft.thread_id == "t1" and draft.account == account

    message = _decode(drafts.encode(draft, "Wednesday works."))
    assert message["In-Reply-To"] == "<abc@mail.example.com>"
    assert message["References"] == "<first@mail.example.com> <abc@mail.example.com>"
    assert message["To"] == "ravi@example.com"
    assert message.get_payload().strip() == "Wednesday works."


def test_a_reply_does_not_stack_re_prefixes():
    assert drafts.reply_subject("Re: Rehearsal date") == "Re: Rehearsal date"
    assert drafts.reply_subject("RE: Rehearsal date") == "RE: Rehearsal date"


def test_a_new_mail_needs_a_recipient_and_a_subject(store, config):
    draft = drafts.plan(config, store, to=["Priya <priya@example.com>"], cc=["ravi@example.com"],
                        subject="Checklist", reply_to=None)
    assert (draft.to, draft.cc, draft.thread_id) == (["priya@example.com"], ["ravi@example.com"], None)
    assert draft.link.endswith("#drafts") and draft.summary()["subject"] == "Checklist"

    with pytest.raises(drafts.DraftError, match="recipient"):
        drafts.plan(config, store, to=[], cc=None, subject="Checklist", reply_to=None)
    with pytest.raises(drafts.DraftError, match="subject"):
        drafts.plan(config, store, to=["priya@example.com"], cc=None, subject=" ", reply_to=None)
    with pytest.raises(drafts.DraftError, match="Not an email address"):
        drafts.plan(config, store, to=["priya"], cc=None, subject="Checklist", reply_to=None)


def test_a_reply_must_point_at_a_mail(store, config):
    store.note_upsert("/notes/a.md", "Meeting notes: Atlas", "text", 1_700_000_000, "Atlas")
    note_id = store.notes()[0].doc_id
    with pytest.raises(drafts.DraftError, match="mail found by search"):
        drafts.plan(config, store, to=None, cc=None, subject=None, reply_to=note_id)
    with pytest.raises(drafts.DraftError):
        drafts.plan(config, store, to=None, cc=None, subject=None, reply_to=99_999)


def test_an_empty_draft_is_refused_before_any_network_call():
    draft = drafts.Draft("me@example.com", ["priya@example.com"], [], "Checklist", None)
    with pytest.raises(drafts.DraftError, match="some text"):
        drafts.save(draft, "   ")


# --- the assistant's text helpers ------------------------------------------------


@pytest.mark.parametrize("text, shown", [
    ("", ""),
    ("WEB", ""),  # may still turn out to be the marker
    ("WEB-ONLY", ""),
    ("web-only\n", ""),
    ("We", ""),
    ("Weekly status is fine", "Weekly status is fine"),
    ("Go-live is 2 December.\n\nWEB:\n- fastapi release", "Go-live is 2 December."),
    ("Answer\nWEB:", "Answer"),
])
def test_routing_markers_never_reach_the_screen(text, shown):
    assert assistant.visible(text) == shown


def test_only_the_users_words_open_the_draft_tool():
    assert assistant.wants_draft("Draft a reply to Ravi saying Wednesday works")
    assert assistant.wants_draft("write to Priya about the checklist")
    assert not assistant.wants_draft("When is the rehearsal?")


def test_each_question_carries_the_time():
    assert assistant.stamp("hello").endswith("] hello") and assistant.stamp("hello").startswith("[")
