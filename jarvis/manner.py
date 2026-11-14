"""How Jarvis keeps a spoken conversation going: the short lines he says himself, and when a conversation is over.

Nothing here reaches a model. These are the few words a person says without thinking, which are better said at once
than written: "Right, checking." the moment a request that takes work has been heard, "Still looking." when a job
runs long, "Very good." when the user says that is all. Each is one of a few fixed lines, so the voice makes it once
and keeps it (see ``speak.synthesize``), and it is heard without waiting.

A spoken message may also end the conversation by itself: "that's all", "thanks", "stop". And a message the ear took
without Jarvis's name, in a conversation, may have been meant for someone else in the room; the model answers such
a message with ``NOT_FOR_ME``, which is never shown or said.
"""

from __future__ import annotations

import collections
import re

# Said the moment a request that takes work has been heard: a question to look into, or something to do.
LOOKING = ("Right, checking.", "One moment.", "Let me see.")
DOING = ("On it.", "Right away.", "Will do.")
# Said when Jarvis begins a slow tool and nothing has been said yet. Quick tools are answered before a line would be.
STARTING = {
    "refresh": "Checking for anything new.",
    "web": "Let me look that up.",
    "search": "One moment.",
    "read": "One moment.",
    "work_log": "One moment.",
    "draft": "Drafting it now.",
    "create_workspace": "Setting it up now.",
    "write_file": "Writing it now.",
}
# Said on a long job, the first and then the second time it has been quiet for a while (at most two).
PROGRESS = {
    "refresh": ("Still fetching the latest.", "Nearly there."),
    "web": ("Still searching the web.", "The web is taking its time."),
    "search": ("Still looking.", "Going through it now."),
    "read": ("Still looking.", "Going through it now."),
    "work_log": ("Still looking.", "Going through it now."),
    "draft": ("Still writing the draft.", "Nearly done."),
}
WORKING = ("Still working on it.", "Nearly there.")
# Said when the user ends the conversation; a plain "stop" gets nothing at all.
CLOSING = ("Very good.", "Of course.", "Any time.")
NOT_FOR_ME = "[not for me]"

# The lines said the moment the user stops, which the voice makes ahead of time. The rest are said while Jarvis
# works, where the half second the cloud voice takes to make one the first time is not missed.
AT_ONCE = (*LOOKING, *DOING, *CLOSING)

# A request that takes work: something to look up in the user's mail, calendar or files, or something to do. Only a
# guess from the words, so it leans towards saying nothing: small talk and questions the model answers from what it
# knows ("what time is it", "how are you") get no line, and a request it misses is still acknowledged once the
# model starts a slow tool (see STARTING).
_ACTION = (
    r"draft|write|reply|respond|send|forward|prepare|prep|remind|schedule|book|set up|create|start|open|"
    r"update|refresh|summari[sz]e|research|compare|analy[sz]e"
)
_WORK = re.compile(
    rf"\b(?:{_ACTION}|check|find|look (?:up|for|into|at|through|over)|search|dig|pull up|fetch|get me|show me|"
    r"read|go through|figure out|work out|"
    r"calendar|meetings?|agenda|schedule|inbox|mails?|e-?mails?|messages?|notes|dossier|promises?|tasks?|folders?|"
    r"files?|deck|documents?|docs?|proposal|report|invoice|estimate|reminders?|"
    r"any(?:thing)? new|latest|today's|tomorrow's|yesterday's|did \w+ (?:reply|respond|send|write|answer|get back))\b",
    re.IGNORECASE,
)
# The polite start of a request: what an action verb may follow and still begin it ("could you draft...").
_POLITE = r"(?:(?:jarvis|please|ok(?:ay)?|right|so|and|also|now|can you|could you|would you|will you|i need you to|"
_POLITE += r"i want you to|i'd like you to|go ahead and|just)\W+)*"
_DOES = re.compile(rf"^\W*{_POLITE}(?:{_ACTION})\b", re.IGNORECASE)

# What the model may write before using a tool, which is not said when a line was said already ("Checking." and then
# "Let me check your calendar."). Only a short sentence that is nothing more than that: one that goes on to the
# answer ("Let me check: you have three meetings") is said.
_PREAMBLE = re.compile(
    r"^(?:(?:ok(?:ay)?|sure|right|alright|of course|certainly|absolutely|got it)[,.!]?\s+)?"
    r"(?:let me (?:check|look|see|find|search|pull|get|have a look|take a look|dig|grab|fetch|think)\b|"
    r"let's (?:see|check|look|have a look|take a look)\b|"
    r"i'?ll (?:check|look|take a look|have a look|find|search|pull|get|go|see|draft|write|dig|grab|fetch)\b|"
    r"i will (?:check|look|find|search|draft|write)\b|i'?m (?:checking|looking|pulling|searching|drafting)\b|"
    r"one moment|just a (?:moment|sec(?:ond)?)|give me a (?:moment|sec(?:ond)?)|checking\b|looking (?:that|it|now)\b|"
    r"on it\b)",
    re.IGNORECASE,
)
PREAMBLE_CHARS = 80
_GOES_ON = re.compile(r"[:;]|\s[-–—]\s")

# The user ending the conversation, the whole message: "that's all", "thanks", "no, I'm good", "bye".
_FILLER = r"(?:ok(?:ay)?|right|alright|no|nope|great|perfect|good|cool|fine|lovely|brilliant|excellent|nice|got it)"
_THANKS = r"(?:thanks?(?: a lot| very much| so much)?|thank you(?: very much| so much)?|cheers|much appreciated)"
_ENDS = re.compile(
    rf"^(?:{_FILLER}\s+)*(?:that's (?:all|it|everything)|that will be all|that'll be all|nothing else|i'm done|"
    rf"we're done|i'm good|we're good|all good|stop listening|go to sleep|goodbye|good bye|bye(?: bye)?|"
    rf"good ?night|see you(?: later)?|never ?mind|{_THANKS})(?:\s+(?:for now|{_THANKS}|bye))*$"
)
# ...or wanting him to be quiet: no answer at all.
_STOP = re.compile(
    rf"^(?:{_FILLER}\s+)*(?:stop|stop it|stop talking|be quiet|quiet|shush|hush|enough|that's enough)(?:\s+(?:now|please))*$"
)

_said: collections.Counter = collections.Counter()  # how many times each kind of line has been said, to take turns


def needs_work(message: str) -> bool:
    """Whether ``message`` is likely a request that takes work, and deserves "Right, checking." at once."""
    return bool(_WORK.search(message))


def acknowledgement(message: str) -> str:
    """What to say at once to ``message``, a request that takes work: "On it." to a thing to do, "Right, checking." to
    a question. The lines take turns."""
    return _next("doing", DOING) if _DOES.match(message) else _next("looking", LOOKING)


def starting(tool: str) -> str | None:
    """What to say as Jarvis begins ``tool`` with nothing said yet, or None for a tool that is over before it would be
    heard."""
    return STARTING.get(_short(tool))


def progress(tool: str, count: int) -> str:
    """What to say the ``count``th time (from 0) a long job at ``tool`` has been quiet for a while."""
    lines = PROGRESS.get(_short(tool), WORKING)
    return lines[min(count, len(lines) - 1)]


def is_preamble(sentence: str) -> bool:
    """Whether ``sentence`` only says that Jarvis is about to look ("Let me check your calendar.")."""
    sentence = sentence.strip()
    return len(sentence) <= PREAMBLE_CHARS and bool(_PREAMBLE.match(sentence)) and not _GOES_ON.search(sentence)


def ending(message: str) -> str | None:
    """If the spoken ``message`` ends the conversation, what Jarvis says to that ("" for nothing at all); else None."""
    words = _plain(message)
    if _STOP.match(words):
        return ""
    if _ENDS.match(words):
        return _next("closing", CLOSING)
    return None


def not_for_me(reply: str) -> bool:
    """Whether the model's ``reply`` says the message was not meant for Jarvis."""
    return reply.strip().strip(".\"'").lower() == NOT_FOR_ME


def maybe_not_for_me(so_far: str) -> bool:
    """Whether a reply that begins ``so_far`` may still turn out to be ``NOT_FOR_ME``: it is held back until it is
    known."""
    so_far = so_far.strip().lower()
    return bool(so_far) and (NOT_FOR_ME.startswith(so_far) or not_for_me(so_far))


def _next(kind: str, lines: tuple[str, ...]) -> str:
    line = lines[_said[kind] % len(lines)]
    _said[kind] += 1
    return line


def _short(tool: str) -> str:
    return tool.rsplit("__", 1)[-1]


def _plain(message: str) -> str:
    """``message`` in lower case, without Jarvis's name, punctuation or curly apostrophes, the words single-spaced."""
    words = message.lower().replace("’", "'")
    words = re.sub(r"\bjarvis\b", " ", words)
    return " ".join(re.sub(r"[^\w\s']", " ", words).split())
