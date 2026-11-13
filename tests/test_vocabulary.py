from __future__ import annotations

import pytest

from jarvis import vocabulary
from jarvis.config import load_config
from jarvis.store import Store


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "jarvis.db") as opened:
        yield opened


def test_the_list_has_engagements_people_taught_words_and_the_common_terms(store):
    config = load_config()
    vocabulary.teach(store, "Price  Warden")
    vocabulary.teach(store, "price warden")  # the same word again changes nothing
    found = vocabulary.words(store, config, extra=["Dev Mehta", "dev@example.com"])
    assert found[0] == "Dev Mehta" and "dev@example.com" not in found  # this meeting's people first; addresses are not words
    for word in ("Atlas", "Borealis", "Priya", "Kiran Rao", "Kiran", "Price Warden", "Claude Code", "Jarvis"):
        assert word in found
    assert len(found) == len({word.lower() for word in found}) <= vocabulary.MAX_WORDS
    assert vocabulary.taught(store) == ["Price Warden"]


@pytest.mark.parametrize("plain, leaning, kept", [
    ("Javis, how many meetings do I have?", "Jarvis, how many meetings do I have?", True),
    ("We use the agent farmers for this.", "We use the agent harness for this.", True),
    ("", "I'm sure I'm sure I'm surprised.", False),          # nothing was heard: the leaning reading made it up
    ("Yeah.", "Sonnet or S3 or Bedrock or Kubernetes today.", False),  # far longer than what was heard
    ("The meeting moved to Monday.", "Kubernetes Bedrock Cognito YAML.", False),
    ("Thanks everyone.", "", False),
])
def test_the_leaning_reading_is_kept_only_when_it_is_the_plain_one_corrected(plain, leaning, kept):
    assert vocabulary.agrees(plain, leaning) is kept
