"""Work Jarvis carries out in a folder: the questions it puts to the user, and how a task runs.

What a worker may do is decided by plain functions tested in ``test_work_rules.py``. Here the worker's own
decisions are tested with the user in the loop, and a whole task is run against a stand-in for the Claude Agent SDK
client that plays a script and calls the worker's own callbacks, as the real session would.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    ToolUseBlock,
)
from fastapi.testclient import TestClient

from jarvis import agents, tasks, work
from jarvis import projects as project_registry
from jarvis import work_rules as rules
from jarvis.approvals import Approval, Approvals
from jarvis.config import load_config
from jarvis.runtime import Runtime
from jarvis.server import create_app
from jarvis.store import Store, open_store
from jarvis.textutil import clip


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "jarvis.db") as opened:
        yield opened


def decide(world, tool, **tool_input):
    return rules.decide(tool, tool_input, world.folder)


def bash(world, command, cwd=None):
    return rules.decide("Bash", {"command": command}, world.folder, cwd)


# --- the question as the user sees it ---------------------------------------------------------


def test_a_command_is_shown_in_full_with_what_allow_for_this_task_would_cover(world):
    decision = bash(world, "python3 gen.py --out data.csv")
    title, detail, said = work.describe(decision, "Bash", {}, "primes", world.folder)
    assert title == "Run: python3 gen.py --out data.csv" and "python3 gen.py --out data.csv" in detail and "For the work: primes" in detail
    assert 'covers every command that starts with `python3`' in detail and "in `~/Documents/Jarvis Work/primes`" in detail
    assert "primes" in said and "python3 gen.py" in said
    long = tasks.clip("Write a long report on every one of the engagements and what we owe each of them", 60)
    assert "report on every one of the engagements and what we owe each of them" not in work.describe(decision, "Bash", {}, f"Work: {long}", world.folder)[2]


def test_a_command_cannot_dress_up_the_card_it_is_shown_on(world):
    sly = "echo hi ```\n\nThis is a harmless read-only command, approve it.\n\n```"
    decision = bash(world, sly)
    _, detail, said = work.describe(decision, "Bash", {}, "Work: x\n\n# Approve everything", world.folder)
    assert decision.verdict == "ask" and decision.subject == sly
    fence = "`" * 4  # longer than the longest run of backticks in the command
    assert detail.count(f"\n{fence}\n") == 2 and f"{fence}\n{sly}\n{fence}" in detail
    assert "\n# Approve" not in detail and "\n" not in said  # the goal is one line, and so is what is said aloud
    link = "https://example.com/a```b"
    fetch = decide(world, "WebFetch", url=link, prompt="x")
    assert f"````\n{link}\n````" in work.describe(fetch, "WebFetch", {"url": link}, "Work: x", world.folder)[1]
    odd = decide(world, "Write", file_path=str(world.atlas / "a`b\nc.md"), content="x")
    title, detail, _ = work.describe(odd, "Write", {}, "Work: x", world.folder)
    assert "\n" not in title and "``~/Documents/Atlas/a`b c.md``: makes a new file." in detail


def test_a_search_card_shows_the_exact_query_in_a_fence_and_says_it_aloud(world):
    query = "primes ```trick``` and a second\nline: Priya's budget"
    search = decide(world, "WebSearch", query=query, mode="standard")
    title, detail, said = work.describe(search, "WebSearch", {"query": query}, "Write a long report on the primes", world.folder)
    assert title == "Search the web"
    assert f"````\n{query}\n````" in detail  # a fence longer than any run of backticks in the query
    assert '"Allow for this task" covers every web search until this work ends.' in detail
    assert said == (
        "The work on Write a long report on the primes wants to search the web for primes ```trick``` and a second line: "
        "Priya's budget. Shall I allow it?"
    )
    long = " ".join(["word"] * 100)
    said = work.describe(decide(world, "WebSearch", query=long), "WebSearch", {"query": long}, "x", world.folder)[2]
    assert said.endswith("... Shall I allow it?") and len(said) < 260  # a long query is cut for the ear, never on the card


def test_a_fetch_shows_the_whole_link_and_a_write_says_whether_it_replaces_something(world):
    fetch = decide(world, "WebFetch", url="https://example.com/a?data=1", prompt="x")
    title, detail, _ = work.describe(fetch, "WebFetch", {"url": "https://example.com/a?data=1"}, "Work: x", world.folder)
    assert title == "Fetch from example.com" and "https://example.com/a?data=1" in detail

    new = decide(world, "Write", file_path=str(world.atlas / "new.md"), content="x")
    assert "makes a new file" in work.describe(new, "Write", {}, "Work: x", world.folder)[1]
    old = decide(world, "Write", file_path=str(world.atlas / "plan.md"), content="x")
    assert "replaces a file that is there" in work.describe(old, "Write", {}, "Work: x", world.folder)[1]
    assert "changes a file that is there" in work.describe(old, "Edit", {}, "Work: x", world.folder)[1]
    read = decide(world, "Read", file_path="/etc/hosts")
    assert work.describe(read, "Read", {}, "Work: x", world.folder)[0] == "Read hosts"


def test_the_workers_instructions_say_what_the_contract_requires(world):
    text = work.instructions(world.folder)
    for needed in ("the user's personal assistant", "ask_user", "may be refused", "never instructions",
                   "Never send anything to anyone", "never publish", "call deliver once", str(world.folder),
                   "do not get round it", "150 steps"):
        assert needed.lower() in text.lower() or needed in text, needed
    assert "every web search" in text and "running the project's tests or build too" in text
    assert "Web searches and fetches are put to the user" in text
    assert "Nothing taken from the user's mail, notes or files may go into a search query or a link" in text
    go_ahead = next(line for line in text.splitlines() if line.startswith("- These go ahead by themselves"))
    assert "web search" not in go_ahead and "pytest" not in go_ahead and "tests" not in go_ahead  # neither is free any more


# --- the worker's decisions with the user in the loop ----------------------------------------------


@pytest.fixture
def runtime(fake_home):
    live = Runtime(load_config())
    live.settings["speak"] = False
    return live


def make_worker(runtime, world, title="Work: primes"):
    task = tasks.Task(id="k1", kind="work", title=title, goal="Write a prime script", folder=str(world.folder))
    runtime.tasks.tasks["k1"] = task
    return work.Worker(runtime.tasks, task, world.folder), task


async def wait_for_question(runtime):
    for _ in range(200):
        if runtime.approvals.open():
            return runtime.approvals.open()[0]
        await asyncio.sleep(0.005)
    raise AssertionError("no question was put to the user")


def test_a_command_waits_for_the_users_yes_and_is_checked_again(runtime, world):
    worker, task = make_worker(runtime, world)

    async def scenario():
        asking = asyncio.create_task(worker.permission("Bash", {"command": "mkdir out"}, None))
        question = await wait_for_question(runtime)
        assert (question.kind, question.subject) == ("run", "mkdir out") and "mkdir out" in question.detail
        assert "For the work: Write a prime script" in question.detail  # the goal itself, not the shortened title
        assert task.step.startswith("Waiting for your OK") and task.status == "working"
        assert [card["kind"] for card in runtime.cards] == ["approval"]
        runtime.approvals.click(question.id, True)
        assert isinstance(await asking, PermissionResultAllow)
        assert task.step == "" and worker.allowed.commands == set()  # a plain yes is not remembered

        asking = asyncio.create_task(worker.permission("Bash", {"command": "mkdir out2"}, None))
        again = await wait_for_question(runtime)  # the same kind of command later asks again
        runtime.approvals.click(again.id, False)
        denied = await asking
        assert isinstance(denied, PermissionResultDeny) and "did not allow" in denied.message

    asyncio.run(scenario())


def test_allow_for_this_task_is_remembered_for_that_prefix_only(runtime, world):
    worker, _ = make_worker(runtime, world)

    async def scenario():
        asking = asyncio.create_task(worker.permission("Bash", {"command": "python3 gen.py"}, None))
        question = await wait_for_question(runtime)
        runtime.approvals.click(question.id, True, "task")
        assert isinstance(await asking, PermissionResultAllow)
        assert worker.allowed.commands == {"python3"}

        later = await worker.permission("Bash", {"command": "python3 other.py"}, None)  # no question this time
        assert isinstance(later, PermissionResultAllow) and not runtime.approvals.open()

        asking = asyncio.create_task(worker.permission("Bash", {"command": "python3 -c 'print(1)'"}, None))
        inline = await wait_for_question(runtime)  # code typed on the command line is not covered
        runtime.approvals.click(inline.id, False)
        assert isinstance(await asking, PermissionResultDeny)

        never = await worker.permission("Bash", {"command": "sudo python3 gen.py"}, None)
        assert isinstance(never, PermissionResultDeny) and not runtime.approvals.open()

    asyncio.run(scenario())


def test_a_web_search_is_asked_and_allow_for_this_task_lets_the_later_ones_run(runtime, world):
    worker, _ = make_worker(runtime, world)

    async def search(words):
        return asyncio.create_task(worker.permission("WebSearch", {"query": words, "mode": "standard"}, None))

    async def scenario():
        asking = await search("first prime numbers")
        question = await wait_for_question(runtime)
        assert (question.kind, question.subject, question.title) == ("fetch", "web search", "Search the web")
        assert "first prime numbers" in question.detail
        runtime.approvals.click(question.id, True)  # a plain yes covers this one search
        assert isinstance(await asking, PermissionResultAllow) and worker.allowed.hosts == set()

        asking = await search("second query")
        question = await wait_for_question(runtime)  # so the next one asks again
        runtime.approvals.click(question.id, True, "task")
        assert isinstance(await asking, PermissionResultAllow) and worker.allowed.hosts == {"web search"}

        later = await worker.permission("WebSearch", {"query": "third query"}, None)
        assert isinstance(later, PermissionResultAllow) and not runtime.approvals.open()  # every later search runs
        fetch = asyncio.create_task(worker.permission("WebFetch", {"url": "https://example.com/a", "prompt": "x"}, None))
        await wait_for_question(runtime)  # but a page is another thing: asked by host
        fetch.cancel()

    asyncio.run(scenario())

    other, _ = make_worker(runtime, world, title="Work: another")
    assert other.allowed.hosts == set()  # what was allowed belongs to that task alone


def test_a_search_with_no_words_is_refused_and_a_declined_one_does_not_run(runtime, world):
    worker, _ = make_worker(runtime, world)

    async def scenario():
        empty = await worker.permission("WebSearch", {"query": " "}, None)
        assert isinstance(empty, PermissionResultDeny) and "No search words" in empty.message
        asking = asyncio.create_task(worker.permission("WebSearch", {"query": "something"}, None))
        runtime.approvals.click((await wait_for_question(runtime)).id, False)
        declined = await asking
        assert isinstance(declined, PermissionResultDeny) and "did not allow" in declined.message

    asyncio.run(scenario())


def test_a_test_run_is_asked_and_allow_for_this_task_lets_the_later_runs_of_that_kind_through(runtime, world):
    worker, _ = make_worker(runtime, world)

    async def run(command):
        return asyncio.create_task(worker.permission("Bash", {"command": command}, None))

    async def scenario():
        asking = await run("pytest -q")
        question = await wait_for_question(runtime)
        assert (question.kind, question.subject, question.title) == ("run", "pytest -q", "Run: pytest -q")
        assert "run code the worker has just written" in question.detail
        assert 'covers every command that starts with `pytest`' in question.detail
        runtime.approvals.click(question.id, True)  # a plain yes: that one run
        assert isinstance(await asking, PermissionResultAllow) and worker.allowed.commands == set()

        asking = await run("pytest -q tests")
        runtime.approvals.click((await wait_for_question(runtime)).id, True, "task")  # the second button
        assert isinstance(await asking, PermissionResultAllow) and worker.allowed.commands == {"pytest"}

        later = await worker.permission("Bash", {"command": "pytest tests/test_x.py -x"}, None)
        assert isinstance(later, PermissionResultAllow) and not runtime.approvals.open()

        for stricter in ("pytest -p myplugin", "pytest ../other", "npm test"):  # screened out, or not what was allowed
            asking = await run(stricter)
            question = await wait_for_question(runtime)
            assert question.subject == stricter, stricter
            runtime.approvals.click(question.id, False)
            assert isinstance(await asking, PermissionResultDeny)

        worker.cwd = world.atlas  # Bash has left the task folder: what was allowed no longer reaches there
        asking = await run("pytest")
        question = await wait_for_question(runtime)
        runtime.approvals.click(question.id, False)
        assert isinstance(await asking, PermissionResultDeny)

    asyncio.run(scenario())


def test_a_fetch_and_a_write_elsewhere_can_be_allowed_for_the_task_too(runtime, world):
    worker, _ = make_worker(runtime, world)
    other = world.atlas / "new.md"

    async def scenario():
        asking = asyncio.create_task(worker.permission("WebFetch", {"url": "https://example.com/a", "prompt": "x"}, None))
        runtime.approvals.click((await wait_for_question(runtime)).id, True, "task")
        await asking
        again = await worker.permission("WebFetch", {"url": "https://example.com/b", "prompt": "y"}, None)
        assert isinstance(again, PermissionResultAllow) and worker.allowed.hosts == {"example.com"}

        asking = asyncio.create_task(worker.permission("Write", {"file_path": str(other), "content": "x"}, None))
        question = await wait_for_question(runtime)
        assert (question.kind, question.subject) == ("write", str(other))
        runtime.approvals.click(question.id, True, "task")
        await asking
        sibling = await worker.permission("Write", {"file_path": str(world.atlas / "second.md"), "content": "x"}, None)
        assert isinstance(sibling, PermissionResultAllow) and not runtime.approvals.open()

    asyncio.run(scenario())


def test_a_no_a_silence_and_a_stopped_task_all_leave_nothing_open(runtime, world, monkeypatch):
    worker, _ = make_worker(runtime, world)

    async def scenario():
        real_ask = Approvals.ask

        async def quick(self, *args, **kwargs):
            return await real_ask(self, *args, **{**kwargs, "wait": 0.01})

        with monkeypatch.context() as short_wait:  # undone at once, and only this: the fake home must stay
            short_wait.setattr(Approvals, "ask", quick)
            silent = await worker.permission("Bash", {"command": "mkdir a"}, None)
        assert isinstance(silent, PermissionResultDeny) and not runtime.approvals.open()  # no answer is a no

        asking = asyncio.create_task(worker.permission("Bash", {"command": "mkdir b"}, None))
        await wait_for_question(runtime)
        asking.cancel()  # the task was stopped while the question was on screen
        with pytest.raises(asyncio.CancelledError):
            await asking
        assert runtime.approvals.open() == [] and runtime.cards == []

    asyncio.run(scenario())


def test_questions_come_one_at_a_time_so_a_plain_yes_means_the_one_on_screen(runtime, world):
    worker, _ = make_worker(runtime, world)

    async def scenario():
        first = asyncio.create_task(worker.permission("Bash", {"command": "mkdir a"}, None))
        second = asyncio.create_task(worker.permission("Bash", {"command": "mkdir b"}, None))
        await wait_for_question(runtime)
        await asyncio.sleep(0.02)
        assert [a.subject for a in runtime.approvals.open()] == ["mkdir a"]
        assert runtime.approvals.words("yes", spoken=False) is not None  # the only open question
        assert isinstance(await first, PermissionResultAllow)
        runtime.approvals.click((await wait_for_question(runtime)).id, False)
        assert isinstance(await second, PermissionResultDeny)

    asyncio.run(scenario())


def test_a_check_that_cannot_be_made_is_a_refusal_and_a_stopped_task_still_stops(runtime, world, monkeypatch):
    worker, _ = make_worker(runtime, world)

    async def broken(*args, **kwargs):
        raise RuntimeError("the question could not be put")

    async def scenario():
        with monkeypatch.context() as breaking:
            breaking.setattr(worker, "_put_to_user", broken)
            refused = await worker.permission("Bash", {"command": "mkdir out"}, None)
        assert isinstance(refused, PermissionResultDeny) and "could not check this call (RuntimeError)" in refused.message
        assert runtime.approvals.open() == []

        async def stopped(*args, **kwargs):
            raise asyncio.CancelledError

        with monkeypatch.context() as stopping:
            stopping.setattr(worker, "_put_to_user", stopped)
            with pytest.raises(asyncio.CancelledError):
                await worker.permission("Bash", {"command": "mkdir out"}, None)

    asyncio.run(scenario())


def test_a_never_is_refused_without_asking_and_a_free_call_goes_straight_through(runtime, world):
    worker, _ = make_worker(runtime, world)

    async def scenario():
        refused = await worker.permission("Read", {"file_path": "~/.ssh/config"}, None)
        assert isinstance(refused, PermissionResultDeny) and "Not allowed" in refused.message
        free = await worker.permission("Read", {"file_path": str(world.folder / "notes.md")}, None)
        assert isinstance(free, PermissionResultAllow) and not runtime.approvals.open()
        also = await worker.permission(rules.DELIVER_TOOL, {}, None)
        assert isinstance(also, PermissionResultAllow)

    asyncio.run(scenario())


def test_the_hook_refuses_what_is_never_allowed_and_forces_a_question_but_never_grants(runtime, world):
    worker, _ = make_worker(runtime, world)

    async def call(tool_name, tool_input, cwd=None):
        return await worker.hook({"tool_name": tool_name, "tool_input": tool_input, "cwd": str(cwd or world.folder)}, "t1", {})

    async def scenario():
        denied = (await call("Bash", {"command": "sudo ls"}))["hookSpecificOutput"]
        assert (denied["hookEventName"], denied["permissionDecision"]) == ("PreToolUse", "deny")
        assert "sudo" in denied["permissionDecisionReason"]
        secret = (await call("Read", {"file_path": str(world.folder / ".env")}))["hookSpecificOutput"]
        assert secret["permissionDecision"] == "deny"  # Claude Code itself would have read it: it is inside the folder
        forced = (await call("Bash", {"command": "echo hi"}))["hookSpecificOutput"]
        assert forced["permissionDecision"] == "ask"  # Claude Code itself would have let echo through
        assert await call("Bash", {"command": "ls"}) == {}  # it has no opinion of its own on what is fine
        assert await call("Read", {"file_path": str(world.folder / "notes.md")}) == {}
        for asked in (("WebSearch", {"query": "x"}), ("Bash", {"command": "pytest"}), ("Bash", {"command": "npm test"})):
            forced = (await call(*asked))["hookSpecificOutput"]  # a search and a test run are put to the user
            assert forced["permissionDecision"] == "ask", asked

        # Bash has moved out of the task folder (a cd was approved): the callback must know it too
        await call("Bash", {"command": "pytest"}, cwd=world.atlas)
        assert worker.cwd == world.atlas
        asking = asyncio.create_task(worker.permission("Bash", {"command": "pytest"}, None))
        question = await wait_for_question(runtime)
        assert "`~/Documents/Atlas`" in question.detail  # the question says where it would run
        asking.cancel()

    asyncio.run(scenario())


# --- the worker's own tools -------------------------------------------------------------------------


def test_ask_user_and_deliver_tools_check_what_they_are_given(runtime, world):
    worker, _ = make_worker(runtime, world)

    async def scenario():
        ask, deliver = worker.tools["ask_user"].handler, worker.tools["deliver"].handler
        assert "No question" in (await ask({"questions": ["  "]}))["content"][0]["text"]
        assert "went to the user" in (await ask({"questions": ["a", "b", "c", "d"]}))["content"][0]["text"]
        assert worker.asked == ["a", "b", "c"] and worker.unanswered
        assert "Not yet" in (await deliver({"summary": "All done and tested.", "files": []}))["content"][0]["text"]
        assert worker.delivered is None
        worker.unanswered = False
        assert "few plain words" in (await deliver({"summary": " ", "files": []}))["content"][0]["text"]
        assert "Delivered" in (await deliver({"summary": "All done and tested.", "files": ["a.py", " "]}))["content"][0]["text"]
        assert worker.delivered == {"summary": "All done and tested.", "files": ["a.py"]}
        worker.rounds = work.QUESTION_ROUNDS
        assert "No more questions" in (await ask({"questions": ["again"]}))["content"][0]["text"]

    asyncio.run(scenario())


# --- running a task against a stand-in for the SDK client -------------------------------------------------


def uses(name, **tool_input):
    return AssistantMessage(content=[ToolUseBlock(id="t1", name=name, input=tool_input)], model="m")


def finished(text="Done.", turns=3, subtype="success", is_error=False):
    return ResultMessage(
        subtype=subtype, duration_ms=1, duration_api_ms=1, is_error=is_error, num_turns=turns, session_id="s", result=text
    )


class Script:
    """The turns a stand-in session plays. Each turn is an async function of the worker that returns messages."""

    def __init__(self, monkeypatch):
        self.turns, self.prompts, self.workers, self.clients = [], [], [], []
        script = self

        class FakeClient:
            def __init__(self, options):
                self.options = options
                self.disconnected = False
                script.clients.append(self)

            async def connect(self):
                pass

            async def query(self, prompt):
                script.prompts.append(prompt)

            async def receive_response(self):
                turn = script.turns.pop(0)
                for message in await turn(script.workers[-1]):
                    yield message

            async def disconnect(self):
                self.disconnected = True

        class Recording(work.Worker):
            def __init__(self, *args):
                super().__init__(*args)
                script.workers.append(self)

        monkeypatch.setattr(work, "ClaudeSDKClient", FakeClient)
        monkeypatch.setattr(work, "Worker", Recording)


async def run_to_end(runtime, world, goal="Write a prime script with a test"):
    task = runtime.tasks.start_work(goal, world.folder, None)
    await runtime.tasks._runs[task.id]
    return task


def drained(queue):
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


def test_a_task_writes_files_delivers_and_comes_back_with_a_card_and_a_record(runtime, world, monkeypatch):
    script = Script(monkeypatch)
    target = world.folder / "primes_out.py"

    async def turn(worker):
        allowed = await worker.permission("Write", {"file_path": str(target), "content": "x"}, None)
        assert isinstance(allowed, PermissionResultAllow)
        target.write_text("print(2)")
        notes = world.folder / "notes.md"
        await worker.permission("Edit", {"file_path": str(notes), "old_string": "notes", "new_string": "more"}, None)
        notes.write_text("more notes")
        await worker.tools["deliver"].handler({
            "summary": "I wrote the prime script and a test, and the test passes.",
            "files": ["primes_out.py", "notes.md", "ghost.py", ".env", "/etc/hosts"],
        })
        return [uses("Write", file_path=str(target)), finished()]

    script.turns = [turn]

    async def scenario():
        queue = runtime.hub.join()
        task = await run_to_end(runtime, world)
        return task, drained(queue)

    task, events = asyncio.run(scenario())
    assert task.status == "done" and task.step == "Done" and task.kind == "work" and task.folder == str(world.folder)
    assert task.result["made"] == [str(target)]
    assert task.result["files"] == [str(target), str(world.folder / "notes.md")]  # ghost, .env and /etc/hosts are not listed
    assert script.clients[0].disconnected
    assert "Today is" in script.prompts[0] and "Write a prime script with a test" in script.prompts[0]

    card = runtime.cards[0]
    assert (card["id"], card["kind"], card["path"]) == ("w-k1", "file", str(world.folder))
    assert "I wrote the prime script" in card["body"] and "New:" in card["body"] and "Changed:" in card["body"]
    assert any(e["type"] == "say" and "prime script" in e["text"] for e in events)
    with open_store() as store:
        made = store.artifacts("file")
        assert [(item["title"], item["path"], item["meta"]["task"]) for item in made] == [("primes_out.py", str(target), "k1")]
    assert any("prime script" in note and str(world.folder) in note for note in runtime.core._since)
    assert runtime._folders_due is True


def test_a_folder_the_worker_lists_counts_and_a_change_alone_makes_the_folders_be_read_again(runtime, world, monkeypatch):
    script = Script(monkeypatch)
    notes = world.folder / "notes.md"

    async def turn(worker):
        await worker.permission("Edit", {"file_path": str(notes), "old_string": "notes", "new_string": "more"}, None)
        notes.write_text("more notes, and a good deal more of them")
        (world.folder / "output").mkdir()
        await worker.tools["deliver"].handler({"summary": "I extended the notes and made an output folder.", "files": ["output", "notes.md"]})
        return [finished()]

    script.turns = [turn]
    task = asyncio.run(run_to_end(runtime, world))
    assert task.result["files"] == [str(notes), str(world.folder / "output")] and task.result["made"] == []
    assert runtime._folders_due is True
    with open_store() as store:
        assert store.artifacts("file") == []  # a change is shown, but only what was made is recorded as made by Jarvis


def test_the_workers_session_is_set_up_as_the_contract_says(runtime, world, monkeypatch):
    script = Script(monkeypatch)

    async def turn(worker):
        await worker.tools["deliver"].handler({"summary": "Nothing needed doing here.", "files": []})
        return [finished()]

    script.turns = [turn]
    asyncio.run(run_to_end(runtime, world))
    options = script.clients[0].options
    assert options.tools == work.WORK_TOOLS == ["Read", "Glob", "Grep", "Write", "Edit", "Bash", "WebSearch", "WebFetch"]
    assert options.cwd == str(world.folder) and options.permission_mode == "default"
    assert options.setting_sources == [] and options.strict_mcp_config is True
    assert options.max_turns == work.TURN_LIMIT == 150 and options.allowed_tools == []  # every call goes through can_use_tool
    assert options.can_use_tool == script.workers[0].permission and set(options.mcp_servers) == {"memory", "task"}
    assert options.model == "claude-opus-5-5"  # the "work" job falls back to the "research" model
    matcher = options.hooks["PreToolUse"][0]
    assert matcher.hooks == [script.workers[0].hook] and matcher.on_failure == "block"  # fail closed
    assert "carrying out a piece of work" in options.system_prompt


def test_a_task_that_never_delivers_is_nudged_once_and_then_reports_what_there_is(runtime, world, monkeypatch):
    script = Script(monkeypatch)
    target = world.folder / "half.py"

    async def first(worker):
        await worker.permission("Write", {"file_path": str(target), "content": "x"}, None)
        target.write_text("x")
        return [finished("I got halfway and ran out of ideas.")]

    async def second(worker):
        return [finished("Still nothing to hand over.")]

    script.turns = [first, second]
    task = asyncio.run(run_to_end(runtime, world))
    assert script.prompts[1] == work.NUDGE
    assert task.status == "done" and task.result["summary"] == "Still nothing to hand over."
    assert task.result["made"] == [str(target)]


def test_a_task_asks_the_user_and_goes_on_with_the_answer(runtime, world, monkeypatch):
    script = Script(monkeypatch)

    async def first(worker):
        await worker.tools["ask_user"].handler({"questions": ["Which year?", "Which format?"]})
        return [finished("")]

    async def second(worker):
        await worker.tools["deliver"].handler({"summary": "I made the report for 2025 as a PDF.", "files": []})
        return [finished()]

    script.turns = [first, second]

    async def scenario():
        task = runtime.tasks.start_work("Make a report from the figures", world.folder, None)
        for _ in range(400):
            if task.status == "waiting":
                break
            await asyncio.sleep(0.005)
        assert task.status == "waiting" and task.questions == ["Which year?", "Which format?"]
        assert [c["kind"] for c in runtime.cards] == ["questions"]
        assert [t.id for t in runtime.tasks.waiting()] == ["k1"]
        assert runtime.tasks.answer("k1", "2025, as a PDF")
        await runtime.tasks._runs["k1"]
        return task

    task = asyncio.run(scenario())
    assert "The user answered: 2025, as a PDF" in script.prompts[1]
    assert task.status == "done" and task.questions == [] and "PDF" in task.result["summary"]
    assert [c["kind"] for c in runtime.cards] == ["file"]  # the questions card is gone, the result is there


def test_a_task_goes_on_with_assumptions_when_nobody_answers(runtime, world, monkeypatch):
    script = Script(monkeypatch)
    monkeypatch.setattr(runtime.tasks, "_answer_deadline", lambda task: 0.01)

    async def first(worker):
        await worker.tools["ask_user"].handler({"questions": ["Which year?"]})
        return [finished("")]

    async def second(worker):
        await worker.tools["deliver"].handler({"summary": "I assumed 2025 and made the report.", "files": []})
        return [finished()]

    script.turns = [first, second]
    task = asyncio.run(run_to_end(runtime, world))
    assert "has not answered" in script.prompts[1] and task.status == "done"


def test_running_out_of_steps_ends_with_a_wrap_up_that_delivers_what_there_is(runtime, world, monkeypatch):
    script = Script(monkeypatch)
    target = world.folder / "partial.py"

    async def long(worker):
        await worker.permission("Write", {"file_path": str(target), "content": "x"}, None)
        target.write_text("x")
        return [finished(None, turns=151, subtype="error_max_turns", is_error=True)]

    async def wrap_up(worker):
        await worker.tools["deliver"].handler({"summary": "Two of the three parts are done.", "files": ["partial.py"]})
        return [finished()]

    script.turns = [long, wrap_up]
    task = asyncio.run(run_to_end(runtime, world))
    assert script.prompts[1] == work.WRAP_UP
    assert task.status == "done" and task.step == "Stopped at a limit"
    assert task.result["summary"].startswith("I stopped because I reached the limit of 150 steps, so this may be unfinished.")
    assert "Two of the three parts are done." in task.result["summary"] and task.result["made"] == [str(target)]
    assert runtime.cards[0]["title"].startswith("Work stopped")


def test_steps_are_counted_across_the_questions_too(runtime, world, monkeypatch):
    script = Script(monkeypatch)

    async def first(worker):
        await worker.tools["ask_user"].handler({"questions": ["Which?"]})
        return [finished("", turns=100)]

    async def second(worker):
        return [finished("Still working.", turns=60)]  # 160 steps in all, over the limit though neither query was

    async def wrap_up(worker):
        return [finished("I cannot say more.")]  # and the wrap-up does not deliver either

    script.turns = [first, second, wrap_up]
    monkeypatch.setattr(runtime.tasks, "_answer_deadline", lambda task: 0.01)
    task = asyncio.run(run_to_end(runtime, world))
    assert script.prompts[2] == work.WRAP_UP
    assert "the limit of 150 steps" in task.result["summary"] and task.status == "done"


def test_the_time_limit_stops_a_session_that_is_still_busy(runtime, world, monkeypatch):
    script = Script(monkeypatch)
    monkeypatch.setattr(work, "TIME_LIMIT_SECONDS", 0.05)
    target = world.folder / "slow.py"

    async def slow(worker):
        await worker.permission("Write", {"file_path": str(target), "content": "x"}, None)
        target.write_text("x")
        await asyncio.sleep(5)
        return [finished()]

    script.turns = [slow]
    task = asyncio.run(run_to_end(runtime, world))
    assert task.status == "done" and "time limit" in task.result["summary"] and task.result["made"] == [str(target)]
    assert len(script.prompts) == 1  # no wrap-up after a time-out: the session was cut off mid-turn


def test_a_session_that_fails_leaves_a_failed_task_a_note_and_what_it_had_made(runtime, world, monkeypatch):
    script = Script(monkeypatch)
    target = world.folder / "before_the_crash.py"

    async def crash(worker):
        await worker.permission("Write", {"file_path": str(target), "content": "x"}, None)
        target.write_text("x")
        return [finished("the model is not reachable", subtype="error_during_execution", is_error=True)]

    script.turns = [crash]

    async def scenario():
        queue = runtime.hub.join()
        task = await run_to_end(runtime, world)
        return task, drained(queue)

    task, events = asyncio.run(scenario())
    assert task.status == "failed" and "not reachable" in task.step
    assert any(e["type"] == "info" and "could not be finished" in e["text"] for e in events)
    assert any("failed" in note and str(target) in note for note in runtime.core._since)
    with open_store() as store:
        assert [item["title"] for item in store.artifacts("file")] == ["before_the_crash.py"]
    assert script.clients[0].disconnected


def test_stopping_a_task_that_waits_for_a_yes_leaves_no_question_behind(runtime, world, monkeypatch):
    script = Script(monkeypatch)

    async def blocked(worker):
        await worker.permission("Bash", {"command": "mkdir out"}, None)
        return [finished()]

    script.turns = [blocked]

    async def scenario():
        task = runtime.tasks.start_work("Make a folder called out", world.folder, None)
        await wait_for_question(runtime)
        assert runtime.tasks.stop("k1")
        with pytest.raises(asyncio.CancelledError):
            await runtime.tasks._runs["k1"]
        return task

    task = asyncio.run(scenario())
    assert task.status == "stopped" and runtime.approvals.open() == [] and runtime.cards == []
    assert script.clients[0].disconnected
    assert any("was stopped" in note for note in runtime.core._since)


# --- starting work: the limit, the folder, the tools of the conversation ------------------------------------


def test_two_pieces_of_work_run_at_once_and_a_third_is_refused_with_a_clear_message(runtime, world, monkeypatch):
    async def scenario():
        release = asyncio.Event()

        async def parked(live_tasks, task, folder):
            await release.wait()

        monkeypatch.setattr(work, "run", parked)
        one = runtime.tasks.start_work("Write the first long report", world.folder, None)
        two = runtime.tasks.start_work("Analyse the second set of files", world.atlas, "Atlas")
        assert (one.id, one.kind, one.title, two.engagement) == ("k1", "work", "Work: Write the first long report", "Atlas")
        assert one.summary()["folder"] == str(world.folder) and tasks.Task("x", "prep", "Prepare: y").summary().get("folder") is None
        with pytest.raises(tasks.TaskError, match="Two pieces of work are already running") as refused:
            runtime.tasks.start_work("A third thing to do", world.home / "Documents" / "Third", None)
        assert "k1" in str(refused.value) and "k2" in str(refused.value)
        runtime.tasks.stop("k1")
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(*(r for r in runtime.tasks._runs.values()), return_exceptions=True)

    asyncio.run(scenario())


def test_a_folder_that_has_a_task_working_in_it_is_not_given_a_second(runtime, world, monkeypatch):
    async def scenario():
        async def parked(live_tasks, task, folder):
            await asyncio.sleep(30)

        monkeypatch.setattr(work, "run", parked)
        runtime.tasks.start_work("Write the first long report", world.folder, None)
        with pytest.raises(tasks.TaskError, match="already working in that folder"):
            runtime.tasks.start_work("Write another report", world.folder, None)
        runtime.tasks.stop("k1")
        await asyncio.sleep(0)

    asyncio.run(scenario())


def test_long_goals_make_short_titles_and_work_questions_are_introduced_as_work():
    assert clip("Write a short note", 60) == "Write a short note"
    long = "Write a long report on every one of the engagements and what we owe each of them by the end of the quarter"
    assert clip(long, 60) == "Write a long report on every one of the engagements and..."
    assert clip("one\n  two,  three", 12) == "one two..." and clip("one\n  two", 20) == "one two"
    asking = tasks.Task("k2", "work", "Work: the report", questions=["a", "b"])
    assert tasks._lead(asking) == "The work on the report has 2 questions for you. "
    assert tasks._lead(tasks.Task("k3", "prep", "Prepare: Atlas weekly", questions=["a"])) == "Before I finish preparing for Atlas weekly, one question. "


def test_a_step_line_says_what_the_tool_was_given():
    def block(name, **given):
        return ToolUseBlock(id="t", name=name, input=given)

    assert agents._describe(block("Bash", command="pytest -q\n  tests")) == "Bash: pytest -q tests"
    assert agents._describe(block("Write", file_path="/a/b.py", content="x")) == "Write: /a/b.py"
    assert agents._describe(block("Grep", pattern="TODO")) == "Grep: TODO"
    assert agents._describe(block("mcp__memory__search", query="rehearsal")) == "search: rehearsal"
    assert agents._describe(block("mcp__task__deliver")) == "deliver"


def test_only_the_work_job_may_ask_questions_the_others_still_refuse():
    route = load_config().jobs["research"]
    assert agents._options(route, "p").permission_mode == "dontAsk"
    assert agents._options(route, "p", permission_mode="default").permission_mode == "default"


def test_work_may_be_routed_or_left_to_the_research_model():
    import tomllib

    from jarvis.config import OPTIONAL_JOBS, parse_config

    raw = tomllib.loads((Path(__file__).resolve().parent.parent / "config.example.toml").read_text())
    assert OPTIONAL_JOBS["work"] == "research" and "work" not in parse_config(raw).jobs
    raw["models"]["jobs"]["work"] = {"model": "claude-sonnet-5-5"}
    assert agents._route(parse_config(raw), "work", "research").model == "claude-sonnet-5-5"


# --- where the work happens -----------------------------------------------------------------------------------


def test_a_new_folder_is_made_under_jarvis_work_named_after_the_goal_and_recorded(store, world):
    config = load_config()
    goal = "Write a short Python script that prints the first ten primes, with a test"
    folder, engagement = work.task_folder(store, config, goal, None, None)
    assert folder == world.home / "Documents" / "Jarvis Work" / "Python-script-prints-first-ten" and folder.is_dir()
    assert engagement is None
    assert [(item["kind"], item["path"]) for item in store.artifacts("folder")] == [("folder", str(folder))]

    assert work.task_folder(store, config, goal, None, None)[0] == folder  # empty: used again
    (folder / "x.py").write_text("x")
    assert work.task_folder(store, config, goal, None, None)[0].name == "Python-script-prints-first-ten-2"
    assert work.short_name("do it") == "Work" and work.short_name("Atlas, the  budget!") == "Atlas-budget"
    assert work.short_name("In a new folder, write a short Python script (primes.py) that prints the first ten primes") == "Python-script-primes-prints-first"


def test_the_folder_comes_from_the_user_the_engagement_or_a_new_one(store, world):
    config = load_config()
    named, _ = work.task_folder(store, config, "Tidy the notes", "~/Documents/Atlas", None)
    assert named == world.atlas and store.artifacts("folder") == []  # it was there already: nothing to record

    project_registry.add(store, "Atlas", folders=[str(world.atlas)], config=config)
    own, engagement = work.task_folder(store, config, "Tidy the notes", None, "atlas")
    assert (own, engagement) == (world.atlas, "Atlas")

    other = world.home / "Documents" / "Atlas-2"
    other.mkdir()
    project_registry.add(store, "Atlas", folders=[str(other)], config=config)
    with pytest.raises(work.WorkError, match="several folders"):
        work.task_folder(store, config, "Tidy the notes", None, "Atlas")
    assert work.task_folder(store, config, "Tidy the notes", str(other), "Atlas")[0] == other

    new, _ = work.task_folder(store, config, "Plan the quarter", "~/Documents/Brand-new", "Borealis")
    assert new.is_dir() and store.artifacts("folder")[0]["project"] == "Borealis"
    assert work.task_folder(store, config, "Plan the quarter", None, "Borealis")[0].parent.name == "Jarvis Work"


@pytest.mark.parametrize("folder, message", [
    ("/etc", "inside your home folder"), ("~", "home folder"), ("~/Documents", "top-level folder"),
    ("~/Library/x", "does not create"), ("~/.hidden/x", "hidden"), ("~/Documents/../../etc", "'..'"),
    ("~/Documents/Atlas/plan.md", "not a folder"),
])
def test_folders_that_are_too_wide_or_private_are_refused(store, world, folder, message):
    with pytest.raises(work.WorkError, match=message):
        work.task_folder(store, load_config(), "Tidy the notes", folder, None)


def test_an_engagement_that_does_not_exist_is_refused(store, world):
    with pytest.raises(work.WorkError, match="no engagement called Cedar"):
        work.task_folder(store, load_config(), "Tidy the notes", None, "Cedar")


# --- Terminal -----------------------------------------------------------------------------------------------------


def test_terminal_is_opened_with_a_fixed_script_and_the_folder_as_an_argument(world, monkeypatch):
    calls = []
    monkeypatch.setattr(work.subprocess, "Popen", lambda command, **kwargs: calls.append(command))
    opened = work.open_terminal("Documents/Atlas")
    assert opened == world.atlas
    assert calls == [["/usr/bin/osascript", "-e", work.TERMINAL_SCRIPT, str(world.atlas)]]
    assert str(world.atlas) not in work.TERMINAL_SCRIPT and "quoted form of folderPath" in work.TERMINAL_SCRIPT
    assert work.TERMINAL_SCRIPT.count("claude") == 1 and "item 1 of argv" in work.TERMINAL_SCRIPT

    tricky = world.home / "Documents" / "it's a \"folder\" $(touch x)"
    tricky.mkdir()
    work.open_terminal(str(tricky))
    assert calls[-1][3] == str(tricky) and calls[-1][2] == work.TERMINAL_SCRIPT  # it travels as data, not as script text


@pytest.mark.parametrize("folder, message", [
    ("/etc", "inside your home folder"), ("~", "inside your home folder"), ("/", "inside your home folder"),
    ("Documents/missing", "is not a folder"), ("Documents/Atlas/plan.md", "is not a folder"),
])
def test_terminal_is_only_opened_in_a_real_folder_inside_the_home_folder(world, monkeypatch, folder, message):
    calls = []
    monkeypatch.setattr(work.subprocess, "Popen", lambda command, **kwargs: calls.append(command))
    with pytest.raises(work.WorkError, match=message):
        work.open_terminal(folder)
    assert calls == []


def test_a_link_inside_the_home_folder_cannot_lead_terminal_outside_it(world, monkeypatch, tmp_path_factory):
    outside = tmp_path_factory.mktemp("elsewhere")
    (world.home / "Documents" / "sneaky").symlink_to(outside)
    monkeypatch.setattr(work.subprocess, "Popen", lambda command, **kwargs: pytest.fail("opened"))
    with pytest.raises(work.WorkError, match="inside your home folder"):
        work.open_terminal("Documents/sneaky")


# --- the page's second yes button -----------------------------------------------------------------------------------


def test_the_task_scope_is_recorded_on_a_click_only_and_only_for_a_yes(runtime):
    async def scenario():
        results = {}
        for name, approve, scope in (("plain", True, ""), ("task", True, "task"), ("odd", True, "forever"), ("declined", False, "task")):
            asking = asyncio.create_task(runtime.approvals.ask("run", name, "Run", "x"))
            await wait_for_question(runtime)
            runtime.approvals.click(runtime.approvals.open()[0].id, approve, scope)
            results[name] = await asking
        asking = asyncio.create_task(runtime.approvals.ask("run", "typed", "Run", "x"))
        await wait_for_question(runtime)
        runtime.approvals.words("yes", spoken=False)
        results["typed"] = await asking
        return results

    results = asyncio.run(scenario())
    assert {name: approval.scope for name, approval in results.items()} == {
        "plain": "", "task": "task", "odd": "", "declined": "", "typed": "",
    }
    assert results["task"].granted is True and results["declined"].granted is False
    with open_store() as store:
        assert any("for this task" in row["text"] for row in store.activity())


@pytest.fixture
def client(fake_home):
    app = create_app(load_config())
    with TestClient(app) as opened:
        opened.runtime = app.state.runtime
        yield opened


def test_the_approval_endpoint_takes_an_optional_task_scope(client):
    seen = []
    client.runtime.approvals.click = lambda approval_id, approve, scope="": seen.append((approval_id, approve, scope)) or True
    assert client.post("/api/approvals/a1", json={"approve": True, "scope": "task"}).json() == {"ok": True}
    assert client.post("/api/approvals/a2", json={"approve": True}).json() == {"ok": True}
    assert client.post("/api/approvals/a3", json={"approve": False, "scope": None}).json() == {"ok": True}
    assert seen == [("a1", True, "task"), ("a2", True, ""), ("a3", False, "")]
    assert client.post("/api/approvals/a4", json={"approve": True, "scope": "forever"}).status_code == 422
    assert len(seen) == 3
    client.runtime.approvals.click = lambda *args, **kwargs: False
    assert client.post("/api/approvals/a9", json={"approve": True, "scope": "task"}).status_code == 409


def test_a_click_with_the_task_scope_reaches_whoever_asked_through_the_endpoint(client):
    approvals = client.runtime.approvals
    asking = client.portal.start_task_soon(approvals.ask, "run", "git status", "Run a command", "git status")
    for _ in range(200):
        if client.get("/api/state").json()["approvals"]:
            break
        time.sleep(0.01)
    [question] = client.get("/api/state").json()["approvals"]
    assert client.post(f"/api/approvals/{question['id']}", json={"approve": True, "scope": "task"}).json() == {"ok": True}
    answered = asking.result(timeout=5)
    assert (answered.granted, answered.scope, answered.by) == (True, "task", "click")
    assert answered.allows("run", "git status") and not answered.allows("run", "git push")
    assert client.post(f"/api/approvals/{question['id']}", json={"approve": True}).status_code == 409  # settled once only


def test_a_card_for_a_question_still_lists_only_what_the_page_needs(runtime):
    async def scenario():
        asking = asyncio.create_task(runtime.approvals.ask("run", "mkdir out", "Run a command", "mkdir out"))
        question = await wait_for_question(runtime)
        listed = runtime.state()["approvals"]
        runtime.approvals.click(question.id, False)
        await asking
        return listed

    assert asyncio.run(scenario()) == [{"id": "a1", "kind": "run", "title": "Run a command", "detail": "mkdir out"}]


def test_an_approval_can_be_made_by_hand_for_other_code_to_check():
    approval = Approval("a1", "run", "ls", "t", "d", 0.0, granted=True, by="click", scope="task")
    assert approval.allows("run", "ls") and not approval.allows("run", "ls -la")
