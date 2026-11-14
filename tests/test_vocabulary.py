from __future__ import annotations

import pytest

from jarvis import vocabulary
from jarvis.config import load_config
from jarvis.store import LOCAL_ACCOUNT, Store


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


def test_terms_come_from_each_dossier_too(store, fake_home):
    from jarvis import dossier

    dossier.save("Atlas", "# Atlas\n\n## People\n- Priya: lead\n\n## Terms and names\n"
                          "- **Price Warden** (heard as \"price warden\")\n- Final Source: the last agent\n"
                          "- PMD — the client's data team\n- a long sentence that is not a term at all here\n\n## Recent work\n- none\n")
    assert vocabulary.dossier_terms("Atlas") == ["Price Warden", "Final Source", "PMD"]
    found = vocabulary.words(store, load_config())
    assert "Price Warden" in found and "Final Source" in found
    assert vocabulary.dossier_terms("Borealis") == []


def test_a_taught_word_comes_off_the_list_whatever_its_case(store):
    vocabulary.teach(store, "Quillon Gate")
    vocabulary.teach(store, "Cutover Desk")
    assert vocabulary.unteach(store, "  quillon   gate ") is True
    assert vocabulary.taught(store) == ["Cutover Desk"]
    assert vocabulary.unteach(store, "Quillon Gate") is False  # already gone
    assert vocabulary.unteach(store, "") is False
    assert vocabulary.taught(store) == ["Cutover Desk"]
    assert "Quillon Gate" not in vocabulary.words(store, load_config())


def test_each_word_says_where_it_came_from_and_the_plain_list_is_the_same_words_in_the_same_order(store, fake_home):
    from jarvis import dossier

    dossier.save("Atlas", "# Atlas\n\n## Terms and names\n- **Ledger Sync**: the nightly job\n- SLA-4 \u2014 the support terms\n")
    vocabulary.teach(store, "Zelmora")
    vocabulary.teach(store, "Kubernetes")  # a common term that the user taught as well
    config = load_config()
    found = vocabulary.sourced(store, config, extra=["Zed Quillfeather", "zed@example.com"])
    assert [word for word, _ in found] == vocabulary.words(store, config, extra=["Zed Quillfeather", "zed@example.com"])
    assert found[:13] == [
        ("Zed Quillfeather", "person"),
        ("Atlas", "engagement"), ("Priya", "person"), ("Ravi", "person"), ("Ledger Sync", "dossier"), ("SLA-4", "dossier"),
        ("Borealis", "engagement"), ("Meera", "person"), ("Arjun", "person"), ("Kiran Rao", "person"), ("Kiran", "person"),
        ("Zelmora", "taught"), ("Kubernetes", "taught"),  # listed once, under the first place it came from
    ]
    assert found[13] == ("Jarvis", "common") and ("Kubernetes", "common") not in found
    assert {source for _, source in found} == {"person", "engagement", "dossier", "taught", "common"}


def test_how_names_are_said_is_kept_changed_in_place_and_dropped(store):
    assert vocabulary.pronunciations(store) == {}
    vocabulary.pronounce(store, "Zelmora  Quill", "Zel-mora Kwill")
    vocabulary.pronounce(store, "Borrowdale", "Burr-oh-dale")
    pairs = vocabulary.pronounce(store, "zelmora quill", "  Zel-more-uh   Kwill ")  # the same name: changed where it is
    assert list(pairs.items()) == [("Zelmora Quill", "Zel-more-uh Kwill"), ("Borrowdale", "Burr-oh-dale")]
    assert vocabulary.pronunciations(store) == pairs
    assert vocabulary.pronounce(store, "", "x") == pairs and vocabulary.pronounce(store, "x", "  ") == pairs
    assert vocabulary.unpronounce(store, " BORROWDALE ") is True
    assert vocabulary.unpronounce(store, "Borrowdale") is False
    assert vocabulary.pronunciations(store) == {"Zelmora Quill": "Zel-more-uh Kwill"}


def test_the_newest_two_hundred_pairs_are_kept_and_a_damaged_list_reads_as_empty(store):
    for number in range(vocabulary.MAX_PAIRS + 5):
        vocabulary.pronounce(store, f"Name {number}", f"Said {number}")
    kept = vocabulary.pronunciations(store)
    assert len(kept) == vocabulary.MAX_PAIRS and "Name 0" not in kept and "Name 204" in kept
    for damaged, expected in [
        ("not json", {}),
        ('["Atlas", "At-las"]', {}),
        ('{"Atlas": "At-las", "Bad": 3, "": "x", "Empty": ""}', {"Atlas": "At-las"}),
    ]:
        store.set_state(LOCAL_ACCOUNT, vocabulary.SAID, "pairs", damaged)
        assert vocabulary.pronunciations(store) == expected


@pytest.mark.parametrize("text, pairs, spoken", [
    ("Zelmora will call.", {"Zelmora": "Zel-more-uh"}, "Zel-more-uh will call."),
    ("ZELMORA and zelmora and Zelmora.", {"Zelmora": "Zel-more-uh"}, "Zel-more-uh and Zel-more-uh and Zel-more-uh."),
    ("Zelmora, Zelmora's team and (Zelmora)", {"Zelmora": "Z"}, "Z, Z's team and (Z)"),
    ("Zelmoran met Zelmorabel and megaZelmora_2.", {"Zelmora": "Z"}, "Zelmoran met Zelmorabel and megaZelmora_2."),  # whole words only
    ("Kiran Rao and Kiran", {"Kiran": "Keeran", "Kiran Rao": "Keeran Rau"}, "Keeran Rau and Keeran"),  # the longer name first
    ("Kiran Rao and Kiran", {"Kiran Rao": "Keeran Rau", "Kiran": "Keeran"}, "Keeran Rau and Keeran"),  # whatever the order given
    ("Kiran\n  Rao spoke.", {"Kiran Rao": "Keeran Rau"}, "Keeran Rau spoke."),  # a line break inside a name
    ("We use C++ and A.B, not AxB or a|b.", {"C++": "see plus plus", "A.B": "ay dot bee", "a|b": "a or b"},
     "We use see plus plus and ay dot bee, not AxB or a or b."),  # characters that mean something in a pattern
    ("Read [v2] (draft) $5 ^ * ? \\d", {"[v2]": "version two", "(draft)": "rough", "$5": "five", "^": "up", "\\d": "back-d"},
     "Read version two rough five up * ? back-d"),
    ("Zelmora", {"Zelmora": "\\1 \\g<0> $1"}, "\\1 \\g<0> $1"),  # what is put in is taken as it is
    ("A B A", {"A": "B", "B": "C"}, "B C B"),  # what is put in is not read again
    ("Zo\u00eb and ZO\u00cb", {"Zo\u00eb": "Zoe-ee"}, "Zoe-ee and Zoe-ee"),
    ("Nothing to say here.", {"Zelmora": "Z"}, "Nothing to say here."),
    ("", {"Zelmora": "Z"}, ""),
    ("Zelmora", {}, "Zelmora"),
    ("Zelmora  ", {"": "x", "   ": "y"}, "Zelmora  "),  # an empty name is no name
])
def test_a_name_is_respelt_as_a_whole_word_in_any_case_and_the_longest_first(text, pairs, spoken):
    assert vocabulary.respell(text, pairs) == spoken


def test_letters_that_match_in_any_case_but_fold_apart_never_break_the_speaking():
    # "i" matches a capital I with a dot above in any case, but its folded form is two characters: left as it is
    assert vocabulary.respell("\u0130 and i and I", {"i": "eye"}) == "\u0130 and eye and eye"
    assert vocabulary.respell("a \u212a b", {"k": "kay"}) == "a kay b"  # the Kelvin sign is a capital K
    many = {f"Name {number}": f"Said {number}" for number in range(vocabulary.MAX_PAIRS)}
    assert vocabulary.respell("Name 7, NAME 199 and Name 20.", many) == "Said 7, Said 199 and Said 20."
