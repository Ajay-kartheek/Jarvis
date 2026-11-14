"""The words Jarvis should hear right: engagements, people, and the user's own terms.

The speech recogniser knows ordinary English. It does not know the name of a
client, a colleague or a product, and writes the nearest ordinary words
instead. Given a list of such words it leans towards them when the sound fits.
This module makes that list; ``speech.boosted`` turns it into a decoding
setting, and ``agrees`` is the check that keeps the leaning from inventing
words where nothing of the kind was said.
"""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher

from jarvis import projects
from jarvis.config import Config
from jarvis.store import LOCAL_ACCOUNT, Store

# Terms of the trade that come out wrong often enough to be worth listing for everyone.
COMMON = (
    "Jarvis", "Claude", "Claude Code", "agent harness", "inference profile", "API gateway", "statement of work",
    "pull request", "prompt caching", "vector store", "Kubernetes", "Bedrock", "Cognito", "YAML",
)
MAX_WORDS = 200
WEIGHT = 3.0  # measured: 2 to 3 helps, 4 and above starts to cost accuracy
BEAM = 5
MIN_AGREEMENT = 0.5  # the share of words the plain and the leaning reading must have in common
TAUGHT = "vocabulary"  # where the store keeps words the user taught Jarvis

_WORD = re.compile(r"[\w']+")


def taught(store: Store) -> list[str]:
    try:
        found = json.loads(store.get_state(LOCAL_ACCOUNT, TAUGHT, "words") or "[]")
    except ValueError:
        return []
    return [word for word in found if isinstance(word, str)]


def teach(store: Store, word: str) -> list[str]:
    """Add a word or name to the list. Returns the whole taught list."""
    word = " ".join(word.split())
    known = taught(store)
    if word and word.lower() not in {item.lower() for item in known}:
        known.append(word[:60])
        store.set_state(LOCAL_ACCOUNT, TAUGHT, "words", json.dumps(known[-MAX_WORDS:]))
    return known


def dossier_terms(name: str, limit: int = 30) -> list[str]:
    """The names and terms listed under "Terms and names" in an engagement's dossier."""
    from jarvis import dossier

    text = dossier.read(name)
    match = re.search(r"^## Terms and names\s*$(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    if not match:
        return []
    terms = []
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line.startswith(("-", "*")):
            continue
        term = re.split(r"\s+[(\u2014\u2013-]\s*|:\s", line.lstrip("-* ").replace("**", ""), maxsplit=1)[0]
        term = term.strip(" .,;\"'`")
        if 2 <= len(term) <= 40 and len(term.split()) <= 4:
            terms.append(term)
    return terms[:limit]


def words(store: Store, config: Config, extra: list[str] | tuple[str, ...] = ()) -> list[str]:
    """Everything worth leaning towards right now: ``extra`` first (the people in this meeting, say)."""
    found: list[str] = list(extra)
    for project in projects.load(store, config):
        found.append(project.name)
        for person in project.people:
            found.append(person)
            found.append(person.split()[0] if person.split() else "")
        try:
            found.extend(dossier_terms(project.name))
        except Exception:  # a dossier that cannot be read takes nothing away from the rest
            pass
    found.extend(taught(store))
    found.extend(COMMON)
    seen, kept = set(), []
    for word in found:
        word = " ".join(str(word).split())
        if len(word) >= 2 and word.lower() not in seen and "@" not in word:
            seen.add(word.lower())
            kept.append(word)
    return kept[:MAX_WORDS]


def agrees(plain: str, leaning: str) -> bool:
    """True when the leaning reading is the plain one with words corrected, not something else.

    The leaning reading alone invents words on silence and noise. It is only
    kept when the plain reading heard speech too and at least half the words
    are the same.
    """
    before, after = _WORD.findall(plain.lower()), _WORD.findall(leaning.lower())
    if not before or not after or len(after) > 2 * len(before) + 2:
        return False
    return SequenceMatcher(None, before, after).ratio() >= MIN_AGREEMENT
