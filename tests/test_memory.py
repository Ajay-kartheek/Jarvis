"""The Memory page's side of the server: facts typed in, the words Jarvis listens for, and how names are said."""

from __future__ import annotations

from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from jarvis import dossier, vocabulary
from jarvis.config import load_config
from jarvis.server import create_app
from jarvis.store import open_store


@pytest.fixture
def client(fake_home):
    with TestClient(create_app(load_config())) as opened:
        yield opened


def _logged(client) -> list[str]:
    return [row["text"] for row in client.get("/api/activity").json()]


# --- what Jarvis remembers ------------------------------------------------------------------


def test_a_fact_typed_on_the_dashboard_is_kept_for_its_engagement_listed_and_logged(client):
    made = client.post("/api/facts", json={"text": "  Ravi   decides on dates ", "engagement": "atlas"})
    assert made.status_code == 200
    fact = made.json()
    assert fact["text"] == "Ravi decides on dates" and fact["engagement"] == "Atlas"  # tidied, and the engagement as it is known
    assert fact["id"] and fact["at"]
    assert client.post("/api/facts", json={"text": "Prefers short mails"}).json()["engagement"] is None
    assert client.post("/api/facts", json={"text": "Prefers plain words", "engagement": "  "}).json()["engagement"] is None

    listed = client.get("/api/facts").json()
    assert [(item["text"], item["engagement"]) for item in listed] == [
        ("Prefers plain words", None), ("Prefers short mails", None), ("Ravi decides on dates", "Atlas"),
    ]
    assert listed[-1]["id"] == fact["id"] and set(listed[-1]) == set(fact)
    assert [item["text"] for item in client.get("/api/engagements/Atlas").json()["facts"]][-1] == "Ravi decides on dates"
    assert "Remembered: Ravi decides on dates" in _logged(client)
    assert client.delete(f"/api/facts/{fact['id']}").json() == {"ok": True}  # and it can be forgotten again


@pytest.mark.parametrize("text", ["abc", "   a  b   ", "", "x" * 601])
def test_a_fact_is_four_to_six_hundred_characters(client, text):
    refused = client.post("/api/facts", json={"text": text})
    assert refused.status_code == 422 and refused.json()["detail"] == "A fact is 4 to 600 characters long."
    assert client.get("/api/facts").json() == [] and _logged(client) == []


def test_a_fact_may_be_as_short_and_as_long_as_that_and_needs_an_engagement_that_exists(client):
    assert client.post("/api/facts", json={"text": "abcd"}).status_code == 200
    assert client.post("/api/facts", json={"text": "x" * 600}).status_code == 200
    refused = client.post("/api/facts", json={"text": "Ravi decides on dates", "engagement": "Nowhere"})
    assert refused.status_code == 422 and refused.json()["detail"] == "There is no engagement with that name."
    assert len(client.get("/api/facts").json()) == 2


def test_other_web_pages_cannot_change_what_jarvis_knows(client):
    other = {"Origin": "https://example.com"}
    assert client.post("/api/facts", json={"text": "Something to remember"}, headers=other).status_code == 403
    assert client.post("/api/vocabulary", json={"word": "Zelmora"}, headers=other).status_code == 403
    assert client.post("/api/pronunciations", json={"written": "a", "said": "b"}, headers=other).status_code == 403
    assert client.delete("/api/vocabulary/Zelmora", headers=other).status_code == 403
    assert client.get("/api/facts").json() == [] and client.get("/api/pronunciations").json() == []


# --- the words Jarvis listens for -------------------------------------------------------------


def test_the_words_jarvis_listens_for_come_with_where_they_came_from(client):
    dossier.save("Atlas", "# Atlas\n\n## Terms and names\n- Ledger Sync: the nightly job\n")
    with open_store() as store:
        vocabulary.teach(store, "Zelmora")
    page = client.get("/api/vocabulary").json()
    assert set(page) == {"taught", "automatic"} and page["taught"] == ["Zelmora"]
    automatic = {item["word"]: item["source"] for item in page["automatic"]}
    assert automatic["Atlas"] == "engagement" and automatic["Borealis"] == "engagement"
    assert automatic["Priya"] == "person" and automatic["Kiran"] == "person"
    assert automatic["Ledger Sync"] == "dossier" and automatic["Jarvis"] == "common"
    assert "Zelmora" not in automatic  # the words taught are the other list
    assert [item["word"] for item in page["automatic"]][:3] == ["Atlas", "Priya", "Ravi"]
    assert all(set(item) == {"word", "source"} for item in page["automatic"])


def test_a_word_can_be_taught_and_taken_back_whatever_its_case(client):
    taught = client.post("/api/vocabulary", json={"word": "  Zelmora   Quill "})
    assert taught.status_code == 200 and taught.json()["taught"] == ["Zelmora Quill"]
    assert "Learnt the word: Zelmora Quill" in _logged(client)
    assert client.post("/api/vocabulary", json={"word": "zelmora quill"}).json()["taught"] == ["Zelmora Quill"]

    # A common term that is taught moves to the taught list; taken back, it is among the automatic words again.
    page = client.post("/api/vocabulary", json={"word": "Kubernetes"}).json()
    assert page["taught"] == ["Zelmora Quill", "Kubernetes"]
    assert "Kubernetes" not in [item["word"] for item in page["automatic"]]
    back = client.delete("/api/vocabulary/kubernetes")
    assert back.status_code == 200 and back.json()["taught"] == ["Zelmora Quill"]
    assert {"word": "Kubernetes", "source": "common"} in back.json()["automatic"]
    assert client.get("/api/vocabulary").json() == back.json()

    gone = client.delete("/api/vocabulary/Kubernetes")
    assert gone.status_code == 404 and gone.json()["detail"] == "Jarvis was not taught that word."
    with open_store() as store:
        assert vocabulary.taught(store) == ["Zelmora Quill"]


@pytest.mark.parametrize("word", ["a", "   ", "", "x" * 61, "dev@example.com", "https://example.com/page", "go://x"])
def test_a_word_is_two_to_sixty_characters_and_neither_an_address_nor_a_link(client, word):
    refused = client.post("/api/vocabulary", json={"word": word})
    assert refused.status_code == 422 and refused.json()["detail"] == "That is not a word or name to learn."
    assert client.get("/api/vocabulary").json()["taught"] == []
    assert not any("Learnt" in line for line in _logged(client))


def test_a_word_may_be_as_short_and_as_long_as_that_and_may_have_a_slash_in_it(client):
    for word in ("Qx", "y" * 60, "CI/CD", "100% sure?"):
        assert client.post("/api/vocabulary", json={"word": word}).status_code == 200
    assert client.get("/api/vocabulary").json()["taught"] == ["Qx", "y" * 60, "CI/CD", "100% sure?"]
    for word in ("CI/CD", "100% sure?"):  # the browser sends a slash as %2F, and the server must read it as one
        assert client.delete(f"/api/vocabulary/{quote(word, safe='')}").status_code == 200
    assert client.get("/api/vocabulary").json()["taught"] == ["Qx", "y" * 60]


# --- how names are said ------------------------------------------------------------------------


def test_how_names_are_said_can_be_listed_added_changed_and_removed(client):
    assert client.get("/api/pronunciations").json() == []
    added = client.post("/api/pronunciations", json={"written": " Zelmora  Quill", "said": "Zel-more-uh Kwill"})
    assert added.status_code == 200
    assert added.json() == [{"written": "Zelmora Quill", "said": "Zel-more-uh Kwill"}]
    client.post("/api/pronunciations", json={"written": "Borrowdale", "said": "Burr-oh-dale"})
    changed = client.post("/api/pronunciations", json={"written": "ZELMORA QUILL", "said": "Zel-mor-a Quill"}).json()
    assert changed == [
        {"written": "Zelmora Quill", "said": "Zel-mor-a Quill"}, {"written": "Borrowdale", "said": "Burr-oh-dale"},
    ]  # a name that is there already keeps its place
    assert client.get("/api/pronunciations").json() == changed

    removed = client.delete("/api/pronunciations/borrowdale")
    assert removed.status_code == 200 and removed.json() == [{"written": "Zelmora Quill", "said": "Zel-mor-a Quill"}]
    gone = client.delete("/api/pronunciations/Borrowdale")
    assert gone.status_code == 404 and gone.json()["detail"] == "Jarvis has no special way of saying that name."

    with open_store() as store:  # what is kept is what the speaking path reads
        pairs = vocabulary.pronunciations(store)
    assert pairs == {"Zelmora Quill": "Zel-mor-a Quill"}
    assert vocabulary.respell("Zelmora Quill is here.", pairs) == "Zel-mor-a Quill is here."
    assert "Learnt to say Zelmora Quill as Zel-more-uh Kwill" in _logged(client)


@pytest.mark.parametrize("body", [
    {"written": "", "said": "x"},
    {"written": "x", "said": ""},
    {"written": "   ", "said": "x"},
    {"written": "x", "said": " \n "},
    {"written": "x" * 61, "said": "x"},
    {"written": "x", "said": "y" * 61},
])
def test_each_side_of_a_name_is_one_to_sixty_characters(client, body):
    refused = client.post("/api/pronunciations", json=body)
    assert refused.status_code == 422 and refused.json()["detail"] == "Each side is 1 to 60 characters long."
    assert client.get("/api/pronunciations").json() == [] and _logged(client) == []
    assert client.post("/api/pronunciations", json={"written": "x" * 60, "said": "y" * 60}).status_code == 200
    assert client.post("/api/pronunciations", json={"written": "C++/Go", "said": "z"}).status_code == 200


def test_two_hundred_names_are_kept_and_no_more_but_one_that_is_there_can_change(client):
    with open_store() as store:
        for number in range(vocabulary.MAX_PAIRS):
            vocabulary.pronounce(store, f"Name {number}", f"Said {number}")
    full = client.post("/api/pronunciations", json={"written": "One more", "said": "x"})
    assert full.status_code == 422 and "200" in full.json()["detail"]
    assert len(client.get("/api/pronunciations").json()) == 200
    again = client.post("/api/pronunciations", json={"written": "name 7", "said": "Changed"})
    assert again.status_code == 200 and len(again.json()) == 200
    assert {"written": "Name 7", "said": "Changed"} in again.json()
    assert client.delete("/api/pronunciations/Name 0").status_code == 200
    assert client.post("/api/pronunciations", json={"written": "One more", "said": "x"}).status_code == 200
