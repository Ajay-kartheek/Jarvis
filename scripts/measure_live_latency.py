"""How fast can a live answer start on the Claude plan login?

Keeps one Agent SDK session open on the live-answer model with a long,
made-up project brief as the system prompt, asks short questions as the other
speaker would, and times how long the first words take.

    uv run python scripts/measure_live_latency.py [--turns 8] [--model claude-haiku-5-5]
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, StreamEvent

QUESTIONS = [
    "Can you confirm the go-live date and what is still blocking it?",
    "Who owns the single sign-on setup on your side?",
    "What did we agree about the rollback plan?",
    "How many environments are we deploying to in phase one?",
    "Is the data migration script ready for the dry run?",
    "What is the status of the security review?",
    "Do we still need sign-off on the statement of work?",
    "When is the next checkpoint with the steering group?",
    "What was the outcome of last week's load test?",
    "Which open items are waiting on us?",
]


def synthetic_brief(paragraphs: int) -> str:
    """A made-up project brief of roughly ``paragraphs`` * 45 tokens."""
    facts = [
        "Go-live for Project Atlas is planned for 2 December, moved from 18 November.",
        "The only open blocker is the client's single sign-on configuration, owned by Ravi.",
        "Rollback is a blue-green switch with a 30 minute window, agreed on 4 November.",
        "Phase one covers three environments: development, staging and production.",
        "The migration script passed its first dry run; the second is on 20 November.",
        "The security review found two medium issues, both fixed and awaiting retest.",
        "Statement of work version 3 is with the client's legal team for signature.",
        "The steering group meets on the last Thursday of each month.",
        "The load test on 6 November held 400 requests a second at 180 ms median.",
    ]
    lines = []
    for index in range(paragraphs):
        lines.append(f"Note {index + 1}: {facts[index % len(facts)]} Reference item {1000 + index}.")
    return (
        "You are Jarvis. During a live meeting you suggest what the user should say next. "
        "Answer in at most two short sentences, using only the brief below. "
        "If the brief does not cover it, say what to check.\n\nBRIEF\n" + "\n".join(lines)
    )


async def ask(client: ClaudeSDKClient, question: str) -> tuple[float, float, str]:
    started = time.perf_counter()
    first = None
    text = ""
    await client.query(f'The other speaker just said: "{question}"')
    async for message in client.receive_response():
        if isinstance(message, StreamEvent):
            event = message.event
            delta = event.get("delta", {}) if event.get("type") == "content_block_delta" else {}
            if delta.get("type") == "text_delta":
                if first is None:
                    first = time.perf_counter() - started
                text += delta.get("text", "")
        elif isinstance(message, ResultMessage) and not text:
            text = message.result or ""
    total = time.perf_counter() - started
    return (first if first is not None else total), total, text


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--turns", type=int, default=8)
    parser.add_argument("--model", default="claude-haiku-5-5")
    parser.add_argument("--paragraphs", type=int, default=450, help="size of the made-up brief")
    args = parser.parse_args()

    options = ClaudeAgentOptions(
        model=args.model,
        system_prompt=synthetic_brief(args.paragraphs),
        tools=[],
        permission_mode="dontAsk",
        setting_sources=[],
        strict_mcp_config=True,
        thinking={"type": "disabled"},
        include_partial_messages=True,
        max_turns=1,
    )
    connect_started = time.perf_counter()
    async with ClaudeSDKClient(options) as client:
        connect = time.perf_counter() - connect_started
        warm_first, warm_total, _ = await ask(client, "Are we ready to start?")
        print(f"session start {connect:.2f} s; warm-up first words {warm_first:.2f} s, done {warm_total:.2f} s")
        firsts, totals = [], []
        for index in range(args.turns):
            first, total, text = await ask(client, QUESTIONS[index % len(QUESTIONS)])
            firsts.append(first)
            totals.append(total)
            print(f"turn {index + 1}: first words {first:.2f} s, done {total:.2f} s | {text[:70]!r}")
        print(
            f"first words: median {statistics.median(firsts):.2f} s, worst {max(firsts):.2f} s; "
            f"done: median {statistics.median(totals):.2f} s, worst {max(totals):.2f} s "
            f"({args.model}, thinking off, brief of {args.paragraphs} notes)"
        )


if __name__ == "__main__":
    asyncio.run(main())
