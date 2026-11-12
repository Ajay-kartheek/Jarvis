from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fakes import DAY, NOW_S, FakeGmail, FakeHttpError, b64, mail

from jarvis import gmail_sync
from jarvis.store import Store

ACCOUNT = "me@example.com"
NOW = datetime.fromtimestamp(NOW_S, timezone.utc)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "jarvis.db") as opened:
        yield opened


def run(api, store, **kwargs):
    kwargs.setdefault("now", NOW)
    return gmail_sync.sync(api, store, ACCOUNT, kwargs.pop("months", 12), **kwargs)


def stored_ids(store) -> list[str]:
    return [row[0] for row in store.db.execute("SELECT id FROM gmail_messages ORDER BY internal_date DESC")]


# --- parsing ---------------------------------------------------------------


def test_plain_text_is_preferred_over_html():
    record = gmail_sync.parse_message(mail("m1", body="plain version", html="<p>html version</p>"))

    assert record.body == "plain version"


def test_html_is_stripped_when_there_is_no_plain_text():
    html = "<html><head><style>p{color:red}</style></head><body><p>Hi&nbsp;Ajay</p><script>x()</script><div>Bye</div></body></html>"
    record = gmail_sync.parse_message(mail("m1", body=None, html=html))

    assert record.body == "Hi Ajay\n\nBye"


def test_attachment_names_are_kept_but_not_their_content():
    record = gmail_sync.parse_message(mail("m1", attachments=("SOW v3.pdf", "plan.xlsx")))

    assert record.attachments == ["SOW v3.pdf", "plan.xlsx"]
    assert "SOW v3.pdf" not in record.body


def test_only_useful_headers_are_kept():
    record = gmail_sync.parse_message(mail("m1", subject="Pricing", sender="Ann <ann@example.com>"))

    assert record.subject == "Pricing"
    assert record.sender == "Ann <ann@example.com>"
    assert record.recipients == "me@example.com"
    assert set(record.headers) == {"from", "to", "subject", "date"}


def test_body_is_capped_at_30k_characters():
    record = gmail_sync.parse_message(mail("m1", body="x" * 40_000))

    assert len(record.body) == 30_000
    assert record.body_truncated is True


def test_declared_charset_is_used():
    message = mail("m1", body=None)
    message["payload"]["parts"] = [
        {
            "mimeType": "text/plain",
            "filename": "",
            "headers": [{"name": "Content-Type", "value": 'text/plain; charset="iso-8859-1"'}],
            "body": {"data": b64("café", "iso-8859-1")},
        }
    ]

    assert gmail_sync.parse_message(message).body == "café"


def test_nested_multipart_is_walked():
    message = mail("m1", body=None)
    message["payload"]["parts"] = [
        {
            "mimeType": "multipart/alternative",
            "filename": "",
            "parts": [
                {"mimeType": "text/plain", "filename": "", "body": {"data": b64("inner text")}},
                {"mimeType": "text/html", "filename": "", "body": {"data": b64("<b>inner html</b>")}},
            ],
        },
        {"mimeType": "image/png", "filename": "logo.png", "body": {"attachmentId": "A"}},
    ]

    record = gmail_sync.parse_message(message)

    assert record.body == "inner text"
    assert record.attachments == ["logo.png"]


def test_query_skips_promotions_social_drafts_and_chats():
    query = gmail_sync.build_query(1234)

    assert query.startswith("after:1234 ")
    for part in ("-category:promotions", "-category:social", "-in:drafts", "-in:chats"):
        assert part in query


def test_window_is_whole_months_back():
    assert gmail_sync.window_start(12, NOW) == int(NOW.replace(year=NOW.year - 1).timestamp())


# --- backfill --------------------------------------------------------------


def test_backfill_stores_the_window_newest_first(store):
    api = FakeGmail([mail(f"m{i}", days_ago=i) for i in range(1, 8)] + [mail("old", days_ago=500)])

    summary = run(api, store)

    assert stored_ids(store) == [f"m{i}" for i in range(1, 8)]
    assert api.fetched == [f"m{i}" for i in range(1, 8)]  # newest first, old one never fetched
    assert summary["mode"] == "backfill" and summary["complete"] is True
    assert store.get_state(ACCOUNT, "gmail", "backfill_done") == "1"
    assert store.get_state(ACCOUNT, "gmail", "backfill_page_token") is None
    assert store.get_state(ACCOUNT, "gmail", "history_id") == "100"


def test_backfill_makes_mail_searchable(store):
    api = FakeGmail([mail("m1", subject="Pricing for Atlas", body="The revised quote is attached")])

    run(api, store)

    (hit,) = store.search("revised quote")
    assert (hit.kind, hit.title, hit.source_id) == ("mail", "Pricing for Atlas", "m1")
    assert hit.link == "https://mail.google.com/mail/?authuser=me@example.com#all/t-m1"
    assert "Ann" in hit.people


def test_messages_with_skipped_labels_are_not_stored(store):
    api = FakeGmail(
        [mail("ok"), mail("promo", labels=("CATEGORY_PROMOTIONS",)), mail("spam", labels=("SPAM",)),
         mail("bin", labels=("TRASH",)), mail("social", labels=("CATEGORY_SOCIAL",))]
    )
    api.list_everything = True  # even if Gmail listed them, the label check drops them

    summary = run(api, store)

    assert stored_ids(store) == ["ok"]
    assert summary["skipped"] == 4


def test_sample_run_stops_early_and_the_next_run_finishes_without_refetching(store):
    api = FakeGmail([mail(f"m{i}", days_ago=i) for i in range(1, 8)])

    first = run(api, store, max_messages=2)

    assert first["complete"] is False and first["stored"] == 2
    assert store.get_state(ACCOUNT, "gmail", "backfill_done") is None

    second = run(api, store)

    assert second["complete"] is True
    assert sorted(api.fetched) == sorted(f"m{i}" for i in range(1, 8))  # each fetched once
    assert len(stored_ids(store)) == 7


def test_interrupted_backfill_resumes_from_the_saved_page(store):
    api = FakeGmail([mail(f"m{i}", days_ago=i) for i in range(1, 10)], page_size=3)
    api.fail_get_after = 2  # the third page's fetch fails

    with pytest.raises(FakeHttpError):
        run(api, store)

    assert len(stored_ids(store)) == 6
    assert store.get_state(ACCOUNT, "gmail", "backfill_page_token") == "6"

    api.fail_get_after = None
    api.list_tokens.clear()
    run(api, store)

    assert api.list_tokens[0] == "6"  # did not start again from the top
    assert len(stored_ids(store)) == 9
    assert len(api.fetched) == 9


def test_stale_page_token_restarts_from_the_top_without_refetching(store):
    api = FakeGmail([mail(f"m{i}", days_ago=i) for i in range(1, 7)], page_size=3)
    api.fail_get_after = 1
    with pytest.raises(FakeHttpError):
        run(api, store)
    api.fail_get_after = None
    api.stale_tokens.add("3")

    run(api, store)

    assert len(stored_ids(store)) == 6
    assert len(api.fetched) == 6


# --- incremental -----------------------------------------------------------


def test_incremental_picks_up_new_mail(store):
    api = FakeGmail([mail("m1")])
    run(api, store)
    api.deliver(mail("m2", days_ago=0, subject="Fresh"))

    summary = run(api, store)

    assert summary["mode"] == "incremental"
    assert stored_ids(store) == ["m2", "m1"]
    assert store.get_state(ACCOUNT, "gmail", "history_id") == "101"
    assert api.calls["list"] == 1  # no re-listing after the backfill


def test_incremental_removes_deleted_mail_from_the_store_and_the_index(store):
    api = FakeGmail([mail("m1", body="keepme"), mail("m2", body="dropme")])
    run(api, store)
    api.delete("m2")

    run(api, store)

    assert stored_ids(store) == ["m1"]
    assert store.search("dropme") == []
    assert len(store.search("keepme")) == 1


def test_mail_moved_to_trash_leaves_the_mirror(store):
    api = FakeGmail([mail("m1"), mail("m2", days_ago=2)])
    run(api, store)
    api.relabel("m2", ("TRASH",))

    run(api, store)

    assert stored_ids(store) == ["m1"]


def test_label_changes_are_mirrored(store):
    api = FakeGmail([mail("m1", labels=("INBOX", "UNREAD"))])
    run(api, store)
    api.relabel("m1", ("INBOX", "STARRED"))

    run(api, store)

    (labels,) = store.db.execute("SELECT labels FROM gmail_messages WHERE id='m1'").fetchone()
    assert labels == '["INBOX", "STARRED"]'


def test_mail_rescued_from_promotions_enters_the_mirror(store):
    api = FakeGmail([mail("m1"), mail("promo", labels=("CATEGORY_PROMOTIONS",))])
    run(api, store)
    assert stored_ids(store) == ["m1"]
    api.relabel("promo", ("INBOX",))

    run(api, store)

    assert sorted(stored_ids(store)) == ["m1", "promo"]


def test_expired_history_falls_back_to_a_reconcile(store):
    api = FakeGmail([mail("m1"), mail("m2", days_ago=2), mail("m3", days_ago=3)])
    run(api, store)
    # Changes the history API can no longer report:
    del api.messages["m2"]
    api.messages["m4"] = mail("m4", days_ago=0)
    api.history_id = 500
    api.history_expired = True

    summary = run(api, store)

    assert summary["mode"] == "incremental+reconcile"
    assert sorted(stored_ids(store)) == ["m1", "m3", "m4"]
    assert store.get_state(ACCOUNT, "gmail", "history_id") == "500"
    assert api.fetched.count("m1") == 1  # stored mail is not fetched again


def test_widening_the_window_in_config_picks_up_older_mail(store):
    api = FakeGmail([mail("recent", days_ago=10), mail("older", days_ago=200)])
    run(api, store, months=3)
    assert stored_ids(store) == ["recent"]

    summary = run(api, store, months=12)

    assert summary["mode"] == "incremental+widen"
    assert stored_ids(store) == ["recent", "older"]


def test_mail_older_than_the_window_is_ignored_by_incremental(store):
    api = FakeGmail([mail("m1")])
    run(api, store)
    api.deliver(mail("ancient", days_ago=500))

    run(api, store)

    assert stored_ids(store) == ["m1"]
    assert NOW_S - 500 * DAY < gmail_sync.window_start(12, NOW)
