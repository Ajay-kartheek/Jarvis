"""The one "yes": anything that sends, deletes, runs or cannot be undone waits for the user's word.

A part of Jarvis that wants to do such a thing asks here and waits. The
question is shown as a card and, in a conversation, said aloud. It is settled
in exactly two ways, neither of which passes through a model:

- the user presses Approve or Cancel on the card;
- the user's next message is a plain yes or no, typed, or spoken within two
  minutes of the question, while it is the only question open.

No tool lets a model answer for the user, so text in a mail or on a web page
cannot talk Jarvis into approving its own request. Whoever asked gets back an
``Approval``; code that does the irreversible thing checks it again.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jarvis.runtime import Runtime

WAIT_SECONDS = 600
SPOKEN_WITHIN_SECONDS = 120  # a spoken yes counts only this soon after the question

_FILLER = r"(?:\s*(?:jarvis|please|thanks|thank you))*"
_YES = re.compile(
    rf"^{_FILLER}\s*(?:yes|yeah|yep|yup|ok|okay|sure|approve|approved|confirm|confirmed|go ahead|do it|send it|"
    rf"run it|yes send it|yes do it|yes go ahead|yes please){_FILLER}\s*[.!]*$",
    re.IGNORECASE,
)
_NO = re.compile(
    rf"^{_FILLER}\s*(?:no|nope|don'?t|do not|cancel|cancel it|stop|not now|no thanks|no don'?t|leave it){_FILLER}\s*[.!]*$",
    re.IGNORECASE,
)


def plain_answer(text: str) -> bool | None:
    """True for a plain yes, False for a plain no, None for anything else."""
    words = " ".join(text.replace(",", " ").split())
    if _YES.match(words):
        return True
    if _NO.match(words):
        return False
    return None


@dataclass
class Approval:
    """The user's answer to one question. Made and settled only by :class:`Approvals`."""

    id: str
    kind: str  # what would be done: "send_mail", "run", "write", ...
    subject: str  # exactly what it covers, for the code that acts on it to check (a draft id, a command)
    title: str
    detail: str
    asked_at: float
    granted: bool | None = None
    by: str = ""  # "click", "voice", "typed" or "timeout"

    def allows(self, kind: str, subject: str) -> bool:
        """True only for a granted approval of this very thing."""
        return self.granted is True and self.kind == kind and self.subject == subject

    def summary(self) -> dict:
        return {"id": self.id, "kind": self.kind, "title": self.title, "detail": self.detail}


class Approvals:
    def __init__(self, runtime: "Runtime") -> None:
        self.runtime = runtime
        self._open: dict[str, tuple[Approval, asyncio.Future]] = {}
        self._count = 0

    def open(self) -> list[Approval]:
        return [approval for approval, _ in self._open.values()]

    async def ask(self, kind: str, subject: str, title: str, detail: str, *, question: str = "",
                  wait: float = WAIT_SECONDS) -> Approval:
        """Put the question to the user and wait for the answer. No answer in time is a no."""
        self._count += 1
        approval = Approval(f"a{self._count}", kind, subject, title, detail, time.monotonic())
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._open[approval.id] = (approval, future)
        runtime = self.runtime
        runtime.show_card({
            "id": f"card-{approval.id}", "kind": "approval", "title": title, "body": detail, "approval": approval.id,
        })
        runtime.record("approval", f"Asked: {title}")
        runtime.say(question or f"{title}. Shall I go ahead?")
        runtime.call_attention()
        try:
            await asyncio.wait_for(asyncio.shield(future), timeout=wait)
        except asyncio.TimeoutError:
            self._settle(approval.id, False, "timeout")
        return approval

    def _settle(self, approval_id: str, granted: bool, by: str) -> Approval | None:
        entry = self._open.pop(approval_id, None)
        if entry is None:
            return None
        approval, future = entry
        approval.granted, approval.by = granted, by
        if not future.done():
            future.set_result(granted)
        self.runtime.drop_card(f"card-{approval.id}")
        self.runtime.record("approval", f"{'Approved' if granted else 'Declined'} ({by}): {approval.title}")
        self.runtime.publish_state()
        return approval

    def click(self, approval_id: str, approve: bool) -> bool:
        """The user pressed a button on the card."""
        return self._settle(approval_id, approve, "click") is not None

    def words(self, text: str, spoken: bool) -> Approval | None:
        """Take the user's message as the answer if it is a plain yes or no to the one open question."""
        if len(self._open) != 1:
            return None
        answer = plain_answer(text)
        if answer is None:
            return None
        approval = next(iter(self._open.values()))[0]
        if spoken and time.monotonic() - approval.asked_at > SPOKEN_WITHIN_SECONDS:
            return None  # too long ago to be sure what a spoken "yes" refers to: it takes a click
        return self._settle(approval.id, answer, "voice" if spoken else "typed")

    def cancel_all(self) -> None:
        for approval_id in list(self._open):
            self._settle(approval_id, False, "timeout")
