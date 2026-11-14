"""The words Jarvis should hear right and say right: engagements, people, and the user's own terms.

The speech recogniser knows ordinary English. It does not know the name of a
client, a colleague or a product, and writes the nearest ordinary words
instead. Given a list of such words it leans towards them when the sound fits.
This module makes that list; ``speech.boosted`` turns it into a decoding
setting, and ``agrees`` is the check that keeps the leaning from inventing
words where nothing of the kind was said.

The voice has the same trouble the other way round: it says a name as an
ordinary word would sound. The user keeps pairs here, a name as it is written
and the way to spell it so that the voice says it right, and ``respell`` puts
them into what Jarvis is about to say.
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
MAX_LENGTH = 60  # the longest word, name or respelling that is kept
MAX_PAIRS = 200  # as many names to say in a special way as are kept
WEIGHT = 3.0  # measured: 2 to 3 helps, 4 and above starts to cost accuracy
BEAM = 5
MIN_AGREEMENT = 0.5  # the share of words the plain and the leaning reading must have in common
TAUGHT = "vocabulary"  # where the store keeps words the user taught Jarvis
SAID = "pronunciations"  # where it keeps how names are said: each as written, and as it should be spelt for the voice

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
        known.append(word[:MAX_LENGTH])
        store.set_state(LOCAL_ACCOUNT, TAUGHT, "words", json.dumps(known[-MAX_WORDS:]))
    return known


def unteach(store: Store, word: str) -> bool:
    """Take a word off the taught list, whatever its case. True when it was on the list."""
    wanted = " ".join(word.split()).lower()
    known = taught(store)
    kept = [item for item in known if item.lower() != wanted]
    if len(kept) == len(known):
        return False
    store.set_state(LOCAL_ACCOUNT, TAUGHT, "words", json.dumps(kept))
    return True


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


def sourced(store: Store, config: Config, extra: list[str] | tuple[str, ...] = ()) -> list[tuple[str, str]]:
    """Every word ``words`` gives, in the same order, each with where it came from.

    The source is "person" (the people of this meeting, and of each engagement, in full and by first name),
    "engagement", "dossier" (a term from an engagement's dossier), "taught" (a word the user taught Jarvis) or
    "common" (a term of the trade). A word that comes from two places is listed once, under the first.
    """
    found: list[tuple[str, str]] = [(word, "person") for word in extra]
    for project in projects.load(store, config):
        found.append((project.name, "engagement"))
        for person in project.people:
            found.append((person, "person"))
            found.append((person.split()[0] if person.split() else "", "person"))
        try:
            found.extend((term, "dossier") for term in dossier_terms(project.name))
        except Exception:  # a dossier that cannot be read takes nothing away from the rest
            pass
    found.extend((word, "taught") for word in taught(store))
    found.extend((word, "common") for word in COMMON)
    seen, kept = set(), []
    for word, source in found:
        word = " ".join(str(word).split())
        if len(word) >= 2 and word.lower() not in seen and "@" not in word:
            seen.add(word.lower())
            kept.append((word, source))
    return kept[:MAX_WORDS]


def words(store: Store, config: Config, extra: list[str] | tuple[str, ...] = ()) -> list[str]:
    """Everything worth leaning towards right now: ``extra`` first (the people in this meeting, say)."""
    return [word for word, _ in sourced(store, config, extra)]


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


def pronunciations(store: Store) -> dict[str, str]:
    """How the user wants names said: each name as written, and the way to spell it so that the voice says it right."""
    try:
        found = json.loads(store.get_state(LOCAL_ACCOUNT, SAID, "pairs") or "{}")
    except ValueError:
        return {}
    if not isinstance(found, dict):
        return {}
    return {written: said for written, said in found.items() if isinstance(said, str) and written and said}


def pronounce(store: Store, written: str, said: str) -> dict[str, str]:
    """Say how a name is to be said. A name that is there already, in whatever case, keeps its place and takes the
    new respelling. Returns all the pairs."""
    written, said = " ".join(written.split())[:MAX_LENGTH], " ".join(said.split())[:MAX_LENGTH]
    pairs = pronunciations(store)
    if written and said:
        name = next((known for known in pairs if known.casefold() == written.casefold()), written)
        pairs[name] = said
        pairs = dict(list(pairs.items())[-MAX_PAIRS:])
        store.set_state(LOCAL_ACCOUNT, SAID, "pairs", json.dumps(pairs))
    return pairs


def unpronounce(store: Store, written: str) -> bool:
    """Drop a name from the pairs, whatever its case. True when it was there."""
    wanted = " ".join(written.split()).casefold()
    pairs = pronunciations(store)
    kept = {name: said for name, said in pairs.items() if name.casefold() != wanted}
    if len(kept) == len(pairs):
        return False
    store.set_state(LOCAL_ACCOUNT, SAID, "pairs", json.dumps(kept))
    return True


def respell(text: str, pairs: dict[str, str]) -> str:
    """``text`` with each name in ``pairs`` replaced by the way it is to be said.

    A name is found as a whole word, in any case, and a longer name is tried before a shorter one ("Kiran Rao" before
    "Kiran"). A space inside a name matches any run of white space, a line break too. What is put in is not read
    again, so one respelling cannot turn into part of another. Characters that mean something in a pattern are only
    characters here.
    """
    kept = {" ".join(written.split()): said for written, said in pairs.items() if written.strip()}
    if not kept:
        return text
    spoken = {written.casefold(): said for written, said in kept.items()}
    names = sorted(kept, key=len, reverse=True)
    pattern = re.compile(
        r"(?<!\w)(?:" + "|".join(r"\s+".join(map(re.escape, name.split())) for name in names) + r")(?!\w)",
        re.IGNORECASE,
    )
    # A match the lookup does not know (a few letters match each other in any case but fold apart) is left as it is.
    return pattern.sub(lambda match: spoken.get(" ".join(match[0].split()).casefold(), match[0]), text)
