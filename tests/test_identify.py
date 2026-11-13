"""Which call is this: the meeting in the calendar, or something else in its slot."""

from __future__ import annotations

import pytest

from jarvis import meeting

EVENT = {"title": "Atlas <> Client: Daily Sync", "people": ["Priya", "Ravi", "Dev"], "start": 0, "end": 0,
         "description": "", "organizer": ""}


@pytest.mark.parametrize("title, name", [
    ("Atlas <> Client: Daily Sync | Microsoft Teams", "Atlas <> Client: Daily Sync"),
    ("Dev Mehta | Microsoft Teams", "Dev Mehta"),
    ("Chat | Dev Mehta | Microsoft Teams", "Chat | Dev Mehta"),
    ("Meet - abc-defg-hij - Google Chrome", "Meet - abc-defg-hij"),
    ("Zoom Meeting", "Zoom Meeting"),
    ("  Weekly   review - Zoom ", "Weekly review"),
    ("", ""),
])
def test_the_apps_own_name_is_taken_off_a_window_title(title, name):
    assert meeting.window_name(title) == name


def test_the_calendar_entry_stands_when_a_window_agrees_or_nothing_is_known():
    assert meeting.identify(EVENT, ["Calendar | Microsoft Teams", "Atlas <> Client: Daily Sync | Microsoft Teams"]) == (EVENT, None)
    assert meeting.identify(EVENT, []) == (EVENT, None)
    assert meeting.identify(EVENT, None) == (EVENT, None)
    assert meeting.identify(EVENT, ["Microsoft Teams", "Chat"]) == (EVENT, None)  # windows that say nothing


def test_a_call_with_a_person_in_the_meetings_slot_is_not_the_meeting():
    # The colleague is even on the invitation: it is still a different call.
    assert meeting.identify(EVENT, ["Dev Mehta | Microsoft Teams"]) == (None, "Call with Dev Mehta")
    assert meeting.identify(EVENT, ["Call with Dev Mehta | Microsoft Teams"]) == (None, "Call with Dev Mehta")
    assert meeting.identify(EVENT, ["Borealis support review 2026 | Microsoft Teams"]) == (None, "Borealis support review 2026")


def test_with_no_calendar_entry_the_window_names_the_call():
    assert meeting.identify(None, ["Meera Nair | Microsoft Teams"]) == (None, "Call with Meera Nair")
    assert meeting.identify(None, ["Microsoft Teams"]) == (None, None)


def test_titles_agree_on_shared_words_not_on_one_common_word():
    assert meeting.titles_agree("Atlas Client Daily Sync", "Atlas <> Client: Daily Sync")
    assert meeting.titles_agree("Daily Sync", "Atlas <> Client: Daily Sync")
    assert not meeting.titles_agree("Dev Mehta", "Atlas <> Client: Daily Sync")
    assert not meeting.titles_agree("", "Atlas <> Client: Daily Sync")
