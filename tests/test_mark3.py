"""Speaking up about new mail, standing orders, promises, and the dossier Jarvis keeps on each engagement."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

from jarvis import agents, brief, core, dossier, meeting, promises, tasks, triage
from jarvis.agents import AgentError, Answer
from jarvis.config import load_config
from jarvis.projects import Project
from jarvis.runtime import PROPOSALS, Runtime
from jarvis.server import create_app
from jarvis.store import LOCAL_ACCOUNT, EventRecord, MailRecord, Store, open_store

ME = "you@example.com"  # the account in config.example.toml
ATLAS = Project("Atlas", ("Priya", "Ravi"), ())
NOW = datetime(2026, 1, 5, 9, 0).timestamp()  # a Monday morning

NOTES = """## Summary
We went through the cutover plan.

## Action items
- Me: send the revised cutover plan to Priya (by Friday)
- Ravi: data sign-off (15 January)

## Open questions
- Who owns the rollback step?
"""


@pytest.fixture(autouse=True)
def no_model(monkeypatch):
    """No test here reaches a model: one that needs an answer puts its own stand-in in place."""

    async def refuse(*args, **kwargs):
        raise AssertionError("a test reached for a model")

    monkeypatch.setattr(agents, "run_plain", refuse)
    monkeypatch.setattr(agents, "_run", refuse)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "jarvis.db") as opened:
        yield opened


@pytest.fixture
def client(fake_home):
    app = create_app(load_config())
    runtime = app.state.runtime
    runtime.settings["speak"] = False  # the voice needs a running service; what is said is checked as text

    async def reply(text, *, spoken=False, named=True, on_step=None, on_text=None, on_tool=None):
        return Answer("Done.", [], 0.1)

    runtime.core.talk = reply
    with TestClient(app) as opened:
        opened.runtime = runtime
        yield opened


@pytest.fixture
def talking(fake_home, store):
    """Jarvis's conversation with its tools in reach, and no model."""
    runtime = Runtime(load_config())
    runtime.settings["speak"] = False
    conversation = runtime.core
    conversation._store = store
    conversation._tools()
    return conversation


def _mail(store, message_id, minutes_ago=10, *, at=NOW, sender="Priya <priya@client.example>", subject="Atlas weekly",
          body="Can we move today's weekly to 16:30?", labels=("INBOX", "CATEGORY_PERSONAL"), headers=None,
          thread=None, project=None, to=ME, cc="") -> int:
    """A mail that came in ``minutes_ago`` before ``at``. Returns its id in the local copy."""
    store.gmail_upsert(ME, [MailRecord(
        id=message_id, thread_id=thread or f"t-{message_id}", internal_date=int((at - minutes_ago * 60) * 1000),
        subject=subject, sender=sender, recipients=to, cc=cc, body=body, labels=list(labels), headers=headers or {},
    )])
    doc_id = store.db.execute("SELECT doc_id FROM docs WHERE kind='mail' AND source_id=?", (message_id,)).fetchone()[0]
    if project:
        store.db.execute("UPDATE docs SET project=? WHERE doc_id=?", (project, doc_id))
    return doc_id


def _answers(*texts):
    """Stands in for a model with no tools: gives the answers in turn (the last one again after that) and keeps what it was asked."""
    asked = []

    async def run_plain(config, job, system_prompt, prompt):
        asked.append({"job": job, "system": system_prompt, "prompt": prompt})
        return texts[min(len(asked), len(texts)) - 1]

    run_plain.asked = asked
    return run_plain


def _drain(queue) -> list[dict]:
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


def _said(result) -> str:
    return result["content"][0]["text"]


# --- reading what a model answered -------------------------------------------------------


@pytest.mark.parametrize("text, value", [
    ('[{"a": 1}]', [{"a": 1}]),
    ("Sure. [] Nothing qualifies.", []),
    ("```json\n[1, 2]\n```", [1, 2]),
    ('About [mail 4]: here it is [{"a": 1}] and that is all.', [{"a": 1}]),
    ('{"proposals": []}', {"proposals": []}),
    ("no JSON here", None),
    ("", None),
    ("[unclosed", None),
])
def test_a_json_answer_is_found_in_what_the_model_wrote(text, value):
    assert agents.json_answer(text) == value


# --- which mail is looked at ---------------------------------------------------------------


def test_only_new_mail_a_person_wrote_to_the_user_is_looked_at(store):
    wanted = _mail(store, "m1", minutes_ago=5)
    calendar = _mail(
        store, "m2", minutes_ago=8, subject="Cancelled event: Atlas weekly @ Mon 5 Jan 15:00",
        headers={"auto-submitted": "auto-generated", "sender": "Google Calendar <calendar-notification@google.com>"},
    )  # automatic, but a meeting called off is what speaking up is for
    _mail(store, "m3", sender=f"Me <{ME}>", labels=("SENT",))
    _mail(store, "m4", sender="Atlas News <news@client.example>", headers={"list-unsubscribe": "<mailto:out@client.example>"})
    _mail(store, "m5", sender="Tracker <noreply@tracker.example>")
    _mail(store, "m6", labels=("INBOX", "CATEGORY_UPDATES"))
    _mail(store, "m7", headers={"auto-submitted": "auto-replied"})
    _mail(store, "m8", minutes_ago=30, thread="t-asked", body="Which date suits you?")
    _mail(store, "m9", minutes_ago=20, thread="t-asked", sender=f"Me <{ME}>", labels=("SENT",), body="Wednesday.")
    _mail(store, "m10", minutes_ago=25 * 60)  # yesterday morning: the brief's business, not a reason to interrupt
    copied = _mail(store, "m11", minutes_ago=40, to="Ravi <ravi@client.example>", cc=f"Meera <meera@other.example>, Me <{ME}>")
    unnamed = _mail(store, "m12", minutes_ago=45, to="Atlas team <team@client.example>")
    fresh, passed = triage.select(store, load_config(), NOW)
    assert [mail["doc_id"] for mail in fresh] == [wanted, calendar, copied, unnamed]
    assert [mail["addressed"] for mail in fresh] == [
        "sent to the user", "sent to the user", "the user is only copied", "the user is not named in To or Cc",
    ]
    assert sorted(passed) == ["m3", "m4", "m5", "m6", "m7", "m8", "m9"]  # m8: the user has answered it since


def test_each_new_mail_is_looked_at_once_and_fifteen_at_most_in_a_round(client, monkeypatch):
    runtime = client.runtime
    with open_store() as store:
        for number in range(17):
            _mail(store, f"m{number}", minutes_ago=5 + number, project="Atlas" if number == 0 else None)
        start = int(datetime(2026, 1, 5, 15, 0).timestamp())
        store.calendar_upsert_events(ME, "primary", [EventRecord(
            id="e1", summary="Atlas weekly", start_ts=start, end_ts=start + 1800,
            attendees=[{"name": "Priya", "email": "priya@client.example"}],
        )])
        order = store.watch_add("Tell me when Ravi sends the signed plan")
    model = _answers("[]")
    monkeypatch.setattr(agents, "run_plain", model)

    assert asyncio.run(triage.look(runtime, NOW)) == []
    first = model.asked[0]
    assert first["job"] == "mail_sorting" and first["prompt"].count("[mail ") == 15
    assert "NOW: Monday 05 January 2026, 09:00. Tomorrow is Tuesday 06 January.\nTHE USER: you@example.com" in first["prompt"]
    assert "- Mon 05 Jan 15:00-15:30 | Atlas weekly | Atlas | with Priya" in first["prompt"]
    assert f"[order {order}] Tell me when Ravi sends the signed plan" in first["prompt"]
    assert "1 in thread | engagement: Atlas | sent to the user\nCan we move today's weekly to 16:30?" in first["prompt"]

    asyncio.run(triage.look(runtime, NOW))
    assert model.asked[1]["prompt"].count("[mail ") == 2  # what was left over, in the next round
    asyncio.run(triage.look(runtime, NOW))
    assert len(model.asked) == 2  # nothing new: no model at all


def test_a_round_that_fails_does_not_look_at_the_same_mail_again(client, monkeypatch):
    runtime = client.runtime
    with open_store() as store:
        _mail(store, "m1", at=time.time())
    asked = []

    async def unreachable(config, job, system_prompt, prompt):
        asked.append(prompt)
        raise AgentError("The model is not reachable.")

    monkeypatch.setattr(agents, "run_plain", unreachable)
    assert asyncio.run(runtime.look_at_mail()) == []
    assert asyncio.run(runtime.look_at_mail()) == []
    assert len(asked) == 1 and runtime.cards == []


def test_nothing_is_looked_at_while_speaking_up_is_off_or_the_days_proposals_are_used_up(client, monkeypatch):
    runtime = client.runtime
    model = _answers("[]")
    monkeypatch.setattr(agents, "run_plain", model)
    with open_store() as store:
        _mail(store, "m1", at=time.time())

    client.post("/api/settings", json={"proactive": False})
    assert runtime.propose("Priya moved today's Atlas weekly.", [], mail=1) is False and runtime.cards == []
    assert asyncio.run(runtime.look_at_mail()) == [] and model.asked == []

    with open_store() as store:
        store.set_state(LOCAL_ACCOUNT, PROPOSALS, date.today().isoformat(), 6)
    client.post("/api/settings", json={"proactive": True})
    assert asyncio.run(runtime.look_at_mail()) == [] and model.asked == []

    with open_store() as store:
        store.set_state(LOCAL_ACCOUNT, PROPOSALS, date.today().isoformat(), 0)
    assert asyncio.run(runtime.look_at_mail()) == [] and len(model.asked) == 1  # the mail waited, and is looked at now


def test_a_sync_that_reached_gmail_starts_a_look_at_the_new_mail(client, monkeypatch):
    runtime = client.runtime
    looks = []

    async def look():
        looks.append(1)
        return []

    monkeypatch.setattr(runtime, "look_at_mail", look)

    async def scenario():
        runtime._after_sync([{"account": ME, "source": "calendar", "status": "ok", "summary": {}}])
        runtime._after_sync([{"account": ME, "source": "gmail", "status": "error", "error": "offline"}])
        await asyncio.sleep(0)
        assert looks == []
        runtime._after_sync([{"account": ME, "source": "gmail", "status": "ok", "summary": {"stored": 2}}])
        await asyncio.sleep(0)
        assert looks == [1]

    asyncio.run(scenario())


# --- what the small model may propose -------------------------------------------------------

GOOD = {
    "kind": "meeting_change", "say": "Priya has moved today's Atlas weekly from 15:00 to 16:30.",
    "actions": [
        {"label": "Mark it off", "say": "Today's Atlas weekly at 15:00 is off."},
        {"label": "Draft a reply", "say": "Draft a reply to Priya confirming 16:30."},
    ],
    "mail": 7,
}


def test_what_the_small_model_answers_is_checked_and_whatever_does_not_hold_up_is_dropped():
    def parse(answer, orders=()):
        return triage.parse(answer if isinstance(answer, str) else json.dumps(answer), {7, 8, 9}, set(orders))

    for nonsense in ("", "No proposals today.", "{}", '{"kind": 3}', "[1, 2]", '["a"]', "[{]", "null",
                     '{"proposals": "none"}'):
        assert parse(nonsense) == [], nonsense
    [kept] = parse(f"Here is my answer:\n```json\n{json.dumps([GOOD])}\n```\nThat is all [really].")
    assert kept.kind == "meeting_change" and kept.mail == 7 and kept.say == GOOD["say"]
    assert [action["label"] for action in kept.actions] == ["Mark it off", "Draft a reply"]
    assert parse({"proposals": [GOOD]})[0].say == GOOD["say"]
    assert parse({**GOOD, "mail": "8"})[0].mail == 8  # one proposal without its list, and the number as text

    for broken in ({**GOOD, "kind": "gossip"}, {**GOOD, "say": ""}, {**GOOD, "say": None}, {**GOOD, "mail": 99},
                   {**GOOD, "mail": True}, {**GOOD, "say": "Details at https://example.com/x now."},
                   {**GOOD, "actions": "Mark it off"}, {**GOOD, "say": "x" * 400}, {**GOOD, "mail": None}):
        assert parse([broken]) == [], broken

    answers = [
        {"label": "Draft a short reply", "say": "Draft a reply to Priya."},  # four words
        {"label": "Send it", "say": "Send the signed contract to Priya."},  # not something a press may ask for
        {"label": "Draft", "say": "Draft a reply to priya@client.example."},  # an address
        {"label": "Prepare", "say": "Prepare me from https://example.com/agenda."},  # a link
        "Mark it off",
        {"label": "Mark it off", "say": "Today’s Atlas weekly is off."},
        {"label": "Draft a reply", "say": "Draft a reply to Priya confirming 16:30."},
        {"label": "Remind me", "say": "Remind me at 16:00 to join the Atlas weekly."},
        {"label": "Prepare", "say": "Prepare me for the Atlas weekly."},
    ]
    [trimmed] = parse([{**GOOD, "actions": answers}])
    assert [action["label"] for action in trimmed.actions] == ["Mark it off", "Draft a reply", "Remind me"]

    several = [GOOD, {**GOOD, "say": "The same mail again."}, {**GOOD, "mail": 8}, {**GOOD, "mail": 9}]
    assert [proposal.mail for proposal in parse(several)] == [7, 8]  # one per mail, two at most

    met = {**GOOD, "kind": "watch_hit", "say": "Ravi has sent the signed plan."}
    assert parse([met], orders=[3, 4]) == []  # which order it meets is not said, and two could be meant
    assert parse([{**met, "watch": 4}], orders=[3, 4])[0].watch == 4
    assert parse([met], orders=[3])[0].watch == 3  # only one order could be meant
    assert parse([{**met, "watch": 5}], orders=[]) == []


# --- putting proposals to the user -------------------------------------------------------------


def test_proposals_wait_for_a_call_to_end_and_six_a_day_at_most(client):
    runtime = client.runtime
    queue = runtime.hub.join()
    runtime.autopilot.call_active = True
    assert runtime.propose("Ravi needs the sign-off date today.", [], mail=2, kind="needs_reply")
    assert runtime.propose(
        "Priya moved today's Atlas weekly to 16:30.", [{"label": "Mark it off", "say": "Today's Atlas weekly is off."}],
        mail=1, kind="meeting_change",
    )
    assert runtime.propose("Meera shared the budget.", [], mail=3, kind="fyi")
    assert runtime.cards == [] and runtime.attention == 0  # nothing during the call

    runtime.autopilot.call_active = False
    assert runtime.release_proposals() == 2  # the most pressing two
    cards = client.get("/api/state").json()["cards"]
    assert [card["body"] for card in cards] == ["Ravi needs the sign-off date today.", "Priya moved today's Atlas weekly to 16:30."]
    assert cards[1]["kind"] == "proposal" and cards[1]["mail"] == 1
    assert cards[1]["actions"] == [{"label": "Mark it off", "say": "Today's Atlas weekly is off."}]
    said = [event["text"] for event in _drain(queue) if event["type"] == "say"]
    assert said == ["Priya moved today's Atlas weekly to 16:30.", "Ravi needs the sign-off date today."]
    assert runtime.attention == 2 and runtime.release_proposals() == 0
    assert any("Priya moved today's Atlas weekly to 16:30" in line and "mail [1]" in line for line in runtime.core._since)

    assert [runtime.propose(f"Something else {n}.", [], mail=10 + n) for n in range(5)] == [True, True, True, True, False]
    assert len([card for card in runtime.cards if card["kind"] == "proposal"]) == 6
    with open_store() as store:
        assert sum(row["kind"] == "proposal" for row in store.activity()) == 6


def test_a_standing_order_ends_once_it_is_met_unless_it_says_every_or_whenever(client, monkeypatch):
    runtime = client.runtime
    with open_store() as store:
        once = store.watch_add("Tell me when Ravi sends the signed plan")
        always = store.watch_add("Tell me whenever Meera writes about the budget")
        later = store.watch_add("Tell me when Priya confirms the rehearsal")
        plan = _mail(store, "m1", sender="Ravi <ravi@client.example>", subject="Signed plan", body="Attached, signed.")
        budget = _mail(store, "m2", minutes_ago=12, sender="Meera <meera@other.example>", subject="Budget", body="Approved.")
    model = _answers(json.dumps([
        {"kind": "watch_hit", "say": "Ravi has sent the signed plan.", "actions": [], "mail": plan, "watch": once},
        {"kind": "watch_hit", "say": "Meera wrote that the budget is approved.", "actions": [], "mail": budget, "watch": always},
    ]))
    monkeypatch.setattr(agents, "run_plain", model)
    proposals = asyncio.run(triage.look(runtime, NOW))
    assert [proposal.watch for proposal in proposals] == [once, always]
    assert [card["body"] for card in runtime.cards] == ["Meera wrote that the budget is approved.", "Ravi has sent the signed plan."]
    with open_store() as store:
        left = store.watches()
    assert [order["id"] for order in left] == [always, later] and left[0]["fired_ts"]

    # Met during a call: the order holds until the user has actually been told.
    with open_store() as store:
        rehearsal = _mail(store, "m3", minutes_ago=2, subject="Rehearsal", body="Thursday at ten is confirmed.")
    monkeypatch.setattr(agents, "run_plain", _answers(json.dumps([
        {"kind": "watch_hit", "say": "Priya confirmed the rehearsal for Thursday at ten.", "actions": [], "mail": rehearsal,
         "watch": later},
    ])))
    runtime.autopilot.call_active = True
    asyncio.run(triage.look(runtime, NOW))
    with open_store() as store:
        assert later in [order["id"] for order in store.watches()]
    runtime.autopilot.call_active = False
    runtime.release_proposals()
    with open_store() as store:
        assert [order["id"] for order in store.watches()] == [always]
    assert triage.repeats("let me know every time the build fails") and not triage.repeats("tell me when Priya replies")


def test_the_conversation_keeps_standing_orders(talking):
    tools = talking.tools

    async def scenario():
        assert "the first time" in _said(await tools["watch"].handler({"text": "tell me when Priya sends the signed plan"}))
        assert "every time" in _said(await tools["watch"].handler({"text": "tell me whenever Meera writes"}))
        assert "Say what" in _said(await tools["watch"].handler({"text": " "}))
        listed = _said(await tools["watches"].handler({}))
        assert "#1 | tell me when Priya sends the signed plan" in listed and "#2 | tell me whenever Meera writes" in listed
        assert _said(await tools["unwatch"].handler({"watch_id": 1})) == "Ended."
        assert "no such" in _said(await tools["unwatch"].handler({"watch_id": 1}))
        assert "#1" not in _said(await tools["watches"].handler({}))

    asyncio.run(scenario())


# --- promises ----------------------------------------------------------------------------


@pytest.mark.parametrize("first, second, same", [
    ("Send the revised SOW", "send revised SOW", True),
    ("Review the architecture document", "Review the architecture doc", True),
    ("Share the cutover checklist by Friday", "Share cutover checklist", True),
    ("Send the data sign-off", "Send the rollback plan", False),
    ("Book the rehearsal room", "Confirm the rehearsal date", False),
])
def test_nearly_the_same_words_count_as_the_same_promise(first, second, same):
    assert promises.similar(first, second) is same


def test_the_promises_made_in_a_call_are_recorded_once(fake_home, monkeypatch):
    config = load_config()
    model = _answers(json.dumps([
        {"owner": "Me", "text": "send the revised cutover plan to Priya", "due": "2026-01-09"},
        {"owner": "Ravi", "text": "data sign-off", "due": "15 January"},
        {"owner": "Ravi", "text": "the data sign-off", "due": None},  # the same promise twice
        {"owner": "", "text": "nobody's"}, "junk", {"owner": "me"},
    ]), json.dumps([
        {"owner": "me", "text": "Send revised cutover plan to Priya", "due": "2026-01-09"},  # said again in the next call
        {"owner": "Priya", "text": "book the rehearsal room", "due": "2026-01-07"},
    ]))
    monkeypatch.setattr(agents, "run_plain", model)

    added = asyncio.run(promises.from_notes(config, "Atlas weekly", "Atlas", "2026-01-05T15:00:00+00:00", NOTES, "/notes/a.md"))
    assert len(added) == 2
    asked = model.asked[0]
    assert asked["job"] == "mail_sorting" and "held on Monday 05 January 2026" in asked["prompt"]
    assert "ACTION ITEMS\n- Me: send the revised cutover plan" in asked["prompt"] and "rollback" not in asked["prompt"]
    with open_store() as store:
        found = store.promises("Atlas")
    assert [(p["owner"], p["text"], p["due"], p["status"], p["source"]) for p in found] == [
        ("me", "send the revised cutover plan to Priya", "2026-01-09", "open", "/notes/a.md"),
        ("Ravi", "data sign-off", None, "open", "/notes/a.md"),
    ]

    again = asyncio.run(promises.from_notes(config, "Atlas weekly", "Atlas", "2026-01-12T15:00:00+00:00", NOTES, "/notes/b.md"))
    assert len(again) == 1  # only Priya's is new
    with open_store() as store:
        assert store.promise_close(found[0]["id"]) and not store.promise_close(found[0]["id"])
        assert promises.add(store, "Atlas", "me", "send the revised cutover plan to Priya")[1] is True  # kept before: new now
    quiet = asyncio.run(promises.from_notes(config, "Chat", None, "2026-01-05T15:00:00+00:00", "## Summary\nSmall talk.", "/n/c.md"))
    assert quiet == [] and len(model.asked) == 2  # notes without action items need no model


def test_promises_follow_their_call_when_it_is_refiled_and_go_when_it_is_discarded(fake_home):
    from jarvis import notes

    path = fake_home / "call.md"
    path.write_text("notes")
    with open_store() as store:
        store.note_upsert(str(path), "Meeting notes: Atlas weekly", NOTES, 1_700_000_000, "Atlas")
        store.artifact_add("notes", "Atlas weekly", project="Atlas", path=str(path), body=NOTES, ts=1_700_000_000)
        taken = store.promise_add("Atlas", "me", "send the revised plan", source=str(path))
        other = store.promise_add("Atlas", "Ravi", "data sign-off", source="/notes/other.md")
    notes.correct_latest(engagement="Borealis")
    with open_store() as store:
        assert {p["id"]: p["engagement"] for p in store.promises()} == {taken: "Borealis", other: "Atlas"}
    notes.correct_latest(discard=True)
    with open_store() as store:
        assert [p["id"] for p in store.promises()] == [other]


def test_the_conversation_records_lists_and_closes_promises(talking):
    tools = talking.tools

    async def scenario():
        added = await tools["add_promise"].handler(
            {"engagement": "atlas", "owner": "Me", "text": "send the revised plan", "due": "2026-01-09"})
        assert _said(added) == "Recorded as promise #1."
        again = await tools["add_promise"].handler({"engagement": "Atlas", "owner": "me", "text": "Send revised plan"})
        assert "already on record as promise #1" in _said(again)
        dated = await tools["add_promise"].handler({"engagement": "Atlas", "owner": "Ravi", "text": "sign off", "due": "Friday"})
        assert "due must look like" in _said(dated)
        assert "no engagement called" in _said(await tools["add_promise"].handler({"engagement": "Cedar", "owner": "Ravi", "text": "sign off"}))
        await tools["add_promise"].handler({"engagement": "", "owner": "Ravi", "text": "call back about the invoice"})

        everything = _said(await tools["promises"].handler({}))
        assert "#1 [Atlas] me: send the revised plan (due Friday 09 January 2026)" in everything
        assert "#2 Ravi: call back about the invoice" in everything
        assert "#2" not in _said(await tools["promises"].handler({"engagement": "Atlas"}))
        assert "kept" in _said(await tools["close_promise"].handler({"promise_id": 1}))
        assert "no open promise" in _said(await tools["close_promise"].handler({"promise_id": 1}))
        assert _said(await tools["promises"].handler({"engagement": "Atlas"})) == "No open promises in Atlas."

    asyncio.run(scenario())


def test_the_morning_brief_lists_the_users_own_promises_due_within_three_days(store):
    store.promise_add("Atlas", "me", "send the revised plan", "2026-01-07")
    store.promise_add("Atlas", "me", "share the checklist", "2026-01-02")  # overdue
    store.promise_add("Atlas", "me", "write the handover", "2026-01-20")  # later
    store.promise_add("Atlas", "Ravi", "data sign-off", "2026-01-06")  # not the user's
    store.promise_add(None, "me", "call the bank")  # no date
    kept = store.promise_add("Borealis", "me", "send the invoice", "2026-01-06")
    store.promise_close(kept)
    text = brief.gather(load_config(), store, datetime(2026, 1, 5, 7, 0))
    section = text.split("THE USER'S OWN PROMISES DUE BY Thursday 08 January (still open)\n")[1].split("\n\n")[0]
    assert section.splitlines() == [
        "#2 [Atlas] me: share the checklist (due Friday 02 January 2026) (overdue)",
        "#1 [Atlas] me: send the revised plan (due Wednesday 07 January 2026)",
    ]


# --- the dossier ---------------------------------------------------------------------------


def _long_dossier() -> tuple[str, str, str, str]:
    """A dossier long enough to be cut, with a word near its start, one past 700 characters and one past 3,000."""
    lines = ["## What it is", "Atlas moves the reporting system to the new platform. EARLYWORD", "", "## People"]
    lines += [f"- **Person {n:02d}** (client): reviews the plan every week." for n in range(20)]
    lines += ["MIDDLEWORD", "", "## Where things stand"]
    lines += [f"- Step {n:02d} of the cutover checklist is on track for the fourteenth." for n in range(40)]
    lines += ["LATEWORD"]
    text = "\n".join(lines)
    assert text.index("EARLYWORD") < 600 and 800 < text.index("MIDDLEWORD") < 2_900 and text.index("LATEWORD") > 3_100
    return text, "EARLYWORD", "MIDDLEWORD", "LATEWORD"


def test_the_dossier_goes_into_the_conversation_the_preparation_and_the_live_brief(store, fake_home):
    text, early, middle, late = _long_dossier()
    dossier.save("Atlas", text)
    store.promise_add("Atlas", "me", "send the revised plan", "2026-01-09")
    store.promise_add("Atlas", "Ravi", "data sign-off")
    kept = store.promise_add("Atlas", "Priya", "book the rehearsal room")
    store.promise_close(kept)
    _mail(store, "m1", project="Atlas")

    instructions = core.core_prompt(load_config(), store)
    assert early in instructions and middle not in instructions and late not in instructions
    assert "Atlas:\n## What it is" in instructions and "Borealis:" not in instructions  # Borealis has no dossier yet

    live = meeting.build_brief(store, ATLAS, None, NOW)
    assert early in live and middle in live and late not in live
    assert live.index("WHAT JARVIS KNOWS ABOUT THIS ENGAGEMENT") < live.index("RECENT MAIL")

    sheet = tasks.prep_request(tasks.Task(id="k1", kind="prep", title="Prepare: Atlas weekly", engagement="Atlas"),
                               store, load_config())
    assert early in sheet and middle in sheet and late in sheet
    assert "#1 me: send the revised plan (due Friday 09 January 2026)" in sheet and "#2 Ravi: data sign-off" in sheet
    assert "book the rehearsal room" not in sheet


def test_the_conversation_reads_a_dossier_and_has_it_rebuilt_when_asked(talking, monkeypatch):
    runtime = talking.runtime
    rebuilt = []

    async def rebuild(name, *, asked=False):
        rebuilt.append((name, asked))
        return True

    monkeypatch.setattr(runtime, "rebuild_dossier", rebuild)

    async def scenario():
        assert "no dossier for Atlas yet" in _said(await talking.tools["dossier"].handler({"engagement": "atlas"}))
        dossier.save("Atlas", "## What it is\nAtlas moves the reporting system.")
        assert "Atlas moves the reporting system." in _said(await talking.tools["dossier"].handler({"engagement": "Atlas"}))
        assert "no engagement called" in _said(await talking.tools["dossier"].handler({"engagement": "Cedar"}))
        assert "in the background" in _said(await talking.tools["refresh_dossier"].handler({"engagement": "atlas"}))
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert rebuilt == [("Atlas", True)]


def test_the_conversation_is_told_about_standing_orders_promises_and_dossiers(store, fake_home):
    text = core.core_prompt(load_config(), store)
    for needed in ("watch, watches, unwatch", "every or whenever", "promises, add_promise, close_promise", "refresh_dossier",
                   "(no dossiers yet)"):
        assert needed in text, needed


def test_a_dossier_is_written_by_a_session_that_can_only_read_the_local_copy(fake_home, monkeypatch):
    config = load_config()
    seen = []
    written = (
        "Here is the dossier.\n\n## What it is\nAtlas moves the reporting system.\n\n## People\n- **Priya** (client): sponsor."
        "\n\n## Where things stand\nOn track for the fourteenth.\n\n## Promises\n- Me to Priya: the revised plan."
    )

    async def session(prompt, options, notify):
        seen.append((prompt, options))
        return written

    monkeypatch.setattr(agents, "_run", session)
    with open_store() as store:
        store.promise_add("Atlas", "me", "send the revised plan", "2026-01-09")
        store.fact_add("Ravi decides on dates, not Priya", "Atlas")

    text = asyncio.run(dossier.build(config, "atlas", now=NOW))
    assert text.startswith("## What it is") and dossier.read("Atlas") == text  # what came before the first section is gone
    prompt, options = seen[0]
    assert options.tools == [] and list(options.mcp_servers) == ["memory"] and options.allowed_tools == agents.PRIVATE_TOOLS
    assert options.model == config.route("project_files").model
    assert "ENGAGEMENT: Atlas" in prompt and 'after "2025-11-06"' in prompt
    assert "#1 me: send the revised plan" in prompt and "Ravi decides on dates, not Priya" in prompt
    with open_store() as store:
        assert dossier.built_at(store, "Atlas") == int(NOW)

    async def lost(prompt, options, notify):
        seen.append((prompt, options))
        return "I could not find enough to write it."

    monkeypatch.setattr(agents, "_run", lost)
    assert asyncio.run(dossier.build(config, "Atlas", now=NOW + 86_400)) is None
    assert dossier.read("Atlas") == text  # nothing usable came back: the page stays as it was
    assert "THE DOSSIER AS IT STOOD" in seen[1][0] and "On track for the fourteenth." in seen[1][0]
    assert asyncio.run(dossier.build(config, "Nowhere")) is None


def test_after_a_call_the_promises_are_taken_and_the_dossier_updated_from_the_notes_alone(fake_home, monkeypatch):
    config = load_config()
    dossier.save("Atlas", "## What it is\nAtlas moves reporting.\n\n## Where things stand\nThe rehearsal is on Wednesday.\n\n"
                          "## Promises\nNone yet.")
    model = _answers(
        json.dumps([{"owner": "me", "text": "send the revised cutover plan to Priya", "due": "2026-01-09"}]),
        "## What it is\nAtlas moves reporting.\n\n## Where things stand\nThe rehearsal moved to Thursday.\n\n"
        "## Decisions\n- 5 January 2026: the rehearsal moves to Thursday.\n\n"
        "## Promises\n- Me to Priya: the revised cutover plan (by 9 January 2026)",
    )
    monkeypatch.setattr(agents, "run_plain", model)
    runtime = Runtime(config)
    asyncio.run(runtime.after_notes("Atlas weekly", "Atlas", "2026-01-05T15:00:00+00:00", NOTES, "/notes/a.md"))

    assert [asked["job"] for asked in model.asked] == ["mail_sorting", "project_files"]  # and no reading session
    update = model.asked[1]["prompt"]
    assert "The rehearsal is on Wednesday." in update and "- Me: send the revised cutover plan to Priya" in update
    assert "#1 me: send the revised cutover plan to Priya (due Friday 09 January 2026)" in update
    assert "moved to Thursday" in dossier.read("Atlas")
    with open_store() as store:
        assert [row["kind"] for row in store.activity()][:2] == ["dossier", "promise"]

    for wrong in ("Sorry, I cannot help with that.",
                  "## Where things stand\nThe rehearsal moved again.\n\n## Decisions\nNone.\n\n## Promises\nNone."):
        monkeypatch.setattr(agents, "run_plain", _answers("[]", wrong))
        asyncio.run(runtime.after_notes("Atlas weekly", "Atlas", "2026-01-05T15:00:00+00:00", NOTES, "/notes/b.md"))
        assert "moved to Thursday" in dossier.read("Atlas")  # not a dossier, or only part of one: the page stays


def test_a_dossier_is_rebuilt_once_a_day_and_only_when_something_came_in(store, fake_home):
    config = load_config()
    assert dossier.due(store, config, NOW) == []  # nothing to write it from
    _mail(store, "m1", project="Atlas")
    store.calendar_upsert_events(ME, "primary", [EventRecord(id="e1", summary="Borealis review", start_ts=int(NOW) + 7 * 86_400,
                                                             end_ts=int(NOW) + 7 * 86_400 + 1800)])
    store.db.execute("UPDATE docs SET project='Borealis' WHERE kind='event'")  # a meeting next week is not news
    assert dossier.due(store, config, NOW) == ["Atlas"]

    dossier.save("Atlas", "## What it is\nAtlas moves reporting.")
    store.set_state(LOCAL_ACCOUNT, dossier.BUILT, "Atlas", int(NOW) - 60)
    assert dossier.due(store, config, NOW) == []  # built today
    assert dossier.due(store, config, NOW + 86_400) == []  # nothing new since
    _mail(store, "m2", minutes_ago=-60, project="Atlas")
    assert dossier.due(store, config, NOW + 86_400) == ["Atlas"]


# --- what the dashboard reads ----------------------------------------------------------------


def test_the_dashboard_reads_the_promises_and_everything_kept_on_an_engagement(client):
    with open_store() as store:
        kept = store.promise_add("Atlas", "Priya", "book the rehearsal room", "2026-01-02")
        store.promise_close(kept)
        later = store.promise_add("Atlas", "me", "send the revised plan", "2026-01-09")
        sooner = store.promise_add("Borealis", "Meera", "approve the budget", "2026-01-06")
        store.fact_add("Ravi decides on dates", "Atlas")
        store.fact_add("Prefers short mails")
    dossier.save("Atlas", "## What it is\nAtlas moves the reporting system.")

    listed = client.get("/api/promises").json()
    assert [item["id"] for item in listed] == [sooner, later, kept]  # open first
    assert listed[0] == {"id": sooner, "engagement": "Borealis", "owner": "Meera", "text": "approve the budget",
                         "due": "2026-01-06", "status": "open"}

    page = client.get("/api/engagements/atlas").json()
    assert (page["name"], page["people"], page["folders"], page["files"]) == ("Atlas", ["Priya", "Ravi"], 0, 0)
    assert page["dossier"].startswith("## What it is")
    assert [fact["text"] for fact in page["facts"]] == ["Ravi decides on dates"]
    assert [item["id"] for item in page["promises"]] == [later, kept]
    assert client.get("/api/engagements/Borealis").json()["dossier"] is None
    assert client.get("/api/engagements/Nowhere").status_code == 404


def test_a_proposal_button_can_never_ask_to_send_or_act():
    from jarvis import triage

    for say in (
        "Draft a reply to Priya and send it.",
        "Today's sync is off, so delete the old notes.",
        "Find the deck and forward it to Ravi.",
        "Prepare a summary and run the export.",
        "Mark it off and say yes to the new invite.",
    ):
        assert triage._action({"label": "Do it", "say": say}) is None, say
    assert triage._action({"label": "Draft a reply", "say": "Draft a reply to Priya confirming Monday."}) == {
        "label": "Draft a reply", "say": "Draft a reply to Priya confirming Monday.",
    }
