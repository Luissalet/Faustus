"""The story bible — the structured continuity state that lets a local model
catch what a generic assistant never catches.

The point being tested throughout: extraction and contradiction detection are
deterministic and LLM-free, a contradiction always names the bible fact it
contradicts, edits go through a typed-delta compiler where a human edit wins,
and nothing here may raise into a review hot path — a broken bible costs the
feature, not the turn.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import story_bible as sb  # noqa: E402


@pytest.fixture()
def workspace(tmp_path):
    ws = tmp_path / "novela"
    ws.mkdir()
    return str(ws)


@pytest.fixture()
def project(workspace):
    return {"id": "p1", "name": "Novela", "workspace": workspace}


def _marta_bible():
    return {
        "characters": [{
            "id": "CHAR-1", "name": "Marta", "aliases": ["la doctora"],
            "killed": False, "updated_at": "2024-01-01T00:00:00Z", "last_actor": "user",
            "facts": [{"text": "Marta tenía los ojos verdes",
                       "source": "capítulo 3, p. 41",
                       "first_seen": "2024-01-01T00:00:00Z",
                       "updated_at": "2024-01-01T00:00:00Z"}],
        }],
        "timeline": [], "facts": [], "places": [],
    }


# ── store: round trip, atomicity, corruption ──────────────────────────


def test_bible_round_trips_and_writes_atomically(project):
    bible = _marta_bible()
    sb.save_bible(project, bible)
    path = sb.bible_path(project)
    assert os.path.isfile(path) and not os.path.exists(path + ".tmp")
    with open(path, encoding="utf-8") as fh:
        assert isinstance(json.load(fh), dict)
    back = sb.load_bible(project)
    assert back["characters"][0]["name"] == "Marta"
    assert back["characters"][0]["facts"][0]["source"] == "capítulo 3, p. 41"


def test_missing_bible_is_an_empty_one_not_an_error(project):
    assert sb.load_bible(project) == sb.empty_bible()
    assert sb.payload(project)["counts"] == {"characters": 0, "timeline": 0,
                                             "facts": 0, "places": 0}


def test_corrupt_bible_is_kept_as_corrupt_and_rebuilt(project):
    sb.save_bible(project, _marta_bible())
    path = sb.bible_path(project)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('{"characters": [ this is not json')
    assert sb.load_bible(project) == sb.empty_bible()
    # Nothing is silently destroyed: the broken copy is still on disk.
    assert os.path.isfile(path + ".corrupt")
    # And the store is usable again immediately.
    sb.save_bible(project, _marta_bible())
    assert sb.load_bible(project)["characters"][0]["name"] == "Marta"


def test_a_bible_of_the_wrong_shape_is_treated_as_corrupt(project):
    os.makedirs(os.path.dirname(sb.bible_path(project)), exist_ok=True)
    with open(sb.bible_path(project), "w", encoding="utf-8") as fh:
        fh.write('{"characters": "Marta"}')
    assert sb.load_bible(project) == sb.empty_bible()
    assert os.path.isfile(sb.bible_path(project) + ".corrupt")


def test_a_project_without_a_folder_cannot_be_saved_but_still_reads(project):
    orphan = {"id": "p2", "name": "no folder", "workspace": ""}
    assert sb.load_bible(orphan) == sb.empty_bible()
    with pytest.raises(sb.StoryBibleError):
        sb.save_bible(orphan, _marta_bible())


# ── extraction: deterministic, recall first, no LLM ───────────────────


def test_extract_finds_repeated_proper_nouns_and_not_sentence_starters():
    text = ("Marta cruzó el patio. Entonces Marta miró el cielo. "
            "Entonces empezó a llover.")
    found = sb.extract_candidates(text, sb.empty_bible())
    names = [c["name"] for c in found["characters"]]
    assert "Marta" in names
    # "Entonces" is capitalized twice, but only ever at a sentence start.
    assert not any(n.lower().startswith("entonces") for n in names)


def test_a_word_that_is_only_ever_sentence_initial_is_not_a_name():
    # Two capitals, never once mid-sentence: punctuation, not a character.
    found = sb.extract_candidates("Volvió la calma. Volvió a llover.", sb.empty_bible())
    assert [c["name"] for c in found["characters"]] == []


def test_extract_reads_spanish_and_english_attributes():
    es = sb.extract_candidates("Marta tenía los ojos verdes. Marta sonrió.",
                               sb.empty_bible())
    eyes = [f for f in es["facts"] if f["key"] == "eyes"]
    assert eyes and eyes[0]["value"] == "green" and eyes[0]["subject"] == "Marta"
    assert eyes[0]["source"].startswith("Marta tenía")

    en = sb.extract_candidates("Marta had green eyes. Marta smiled.", sb.empty_bible())
    eyes = [f for f in en["facts"] if f["key"] == "eyes"]
    assert eyes and eyes[0]["value"] == "green"


def test_extract_marks_what_the_bible_already_knows():
    found = sb.extract_candidates("Marta habló con Nuria. Después Nuria buscó a Marta.",
                                  _marta_bible())
    by_name = {c["name"]: c for c in found["characters"]}
    assert by_name["Marta"]["new"] is False
    assert by_name["Nuria"]["new"] is True


def test_extraction_is_deterministic():
    text = "Marta y Nuria hablaron. Marta tenía los ojos verdes. Nuria callaba."
    first = sb.extract_candidates(text, sb.empty_bible())
    second = sb.extract_candidates(text, sb.empty_bible())
    assert first == second


def test_extract_never_raises_on_junk():
    for junk in (None, "", 12345, {"nope": True}, "\x00\x00", "###\n\n---"):
        assert isinstance(sb.extract_candidates(junk, None), dict)


# ── the payoff: continuity ────────────────────────────────────────────


def test_green_eyes_in_chapter_three(project):
    """The whole feature in one test: the bible says Marta's eyes are green,
    chapter 9 says they are blue, and the finding names the fact it
    contradicts so the author can judge."""
    bible = _marta_bible()
    text = "Marta bajó del coche. Marta tenía los ojos azules y no dijo nada."
    findings = sb.check_continuity(text, bible)
    contradictions = [f for f in findings if f["kind"] == "contradiction"]
    assert len(contradictions) == 1
    finding = contradictions[0]
    assert finding["subject"] == "Marta" and finding["key"] == "eyes"
    assert finding["stated_value"] == "blue" and finding["bible_value"] == "green"
    assert finding["bible_fact"]["text"] == "Marta tenía los ojos verdes"
    assert finding["bible_fact"]["source"] == "capítulo 3, p. 41"
    # The span points at the offending words in the passage as given.
    span = finding["text_span"]
    assert text[span["start"]:span["end"]] == span["quote"] == "ojos azules"
    assert finding["confidence"] >= 0.8


def test_the_same_colour_across_languages_is_not_a_contradiction():
    bible = _marta_bible()
    assert not [f for f in sb.check_continuity("Marta had green eyes.", bible)
                if f["kind"] == "contradiction"]
    assert not [f for f in sb.check_continuity("Marta tenía los ojos verdes.", bible)
                if f["kind"] == "contradiction"]


def test_a_contradiction_needs_the_same_subject():
    bible = _marta_bible()
    text = "Nuria bajó del coche. Nuria tenía los ojos azules. Nuria calló."
    assert not [f for f in sb.check_continuity(text, bible)
                if f["kind"] == "contradiction"]


def test_an_unrecorded_character_is_reported_as_unknown():
    bible = _marta_bible()
    text = "Marta esperaba a Nuria. Nuria llegó tarde y se sentó junto a Marta."
    unknown = [f for f in sb.check_continuity(text, bible)
               if f["kind"] == "unknown_character"]
    assert [f["subject"] for f in unknown] == ["Nuria"]
    # It is a prompt to add someone, not an accusation.
    assert unknown[0]["confidence"] < 0.6 and unknown[0]["bible_fact"] is None


def test_an_alias_is_not_an_unknown_character():
    bible = _marta_bible()
    text = "La doctora entró. La doctora se quitó el abrigo."
    unknown = [f["subject"].lower() for f in sb.check_continuity(text, bible)
               if f["kind"] == "unknown_character"]
    assert "la doctora" not in unknown


def test_a_recorded_event_moved_in_time_is_a_timeline_finding():
    bible = sb.empty_bible()
    bible["timeline"].append({"id": "TL-1", "when": "1998",
                              "what": "el incendio del almacén",
                              "source": "capítulo 1"})
    findings = sb.check_continuity("El incendio del almacén fue en 2004.", bible)
    timeline = [f for f in findings if f["kind"] == "timeline"]
    assert timeline and timeline[0]["bible_fact"]["when"] == "1998"
    assert "2004" in timeline[0]["detail"]


def test_check_continuity_never_raises(project):
    for junk_text in (None, "", 42, ["x"]):
        assert sb.check_continuity(junk_text, _marta_bible()) == []
    for junk_bible in (None, {}, {"characters": "nope"}, 7):
        assert isinstance(sb.check_continuity("Marta tenía los ojos azules.",
                                              junk_bible), list)


# ── the typed-delta compiler ──────────────────────────────────────────


def test_add_edit_and_kill_compile_and_persist(project):
    result = sb.apply_deltas(project, [
        {"op": "ADD", "kind": "character", "name": "Marta",
         "aliases": ["la doctora"],
         "facts": [{"text": "ojos verdes", "key": "eyes", "value": "verdes",
                    "source": "cap. 3, p. 41"}],
         "rationale": "the author confirmed it"},
        {"op": "ADD", "kind": "timeline", "when": "1998",
         "what": "el incendio", "rationale": "stated in chapter 1"},
    ], "user")
    assert not result["conflicts"]
    assert [a["id"] for a in result["applied"]] == ["CHAR-1", "TL-1"]
    assert result["bible"]["characters"][0]["facts"][0]["value"] == "green"

    edited = sb.apply_deltas(project, [
        {"op": "EDIT", "id": "CHAR-1", "aliases": ["la doctora", "Marta Ruiz"],
         "rationale": "another alias"}], "user")
    assert not edited["conflicts"]
    assert "Marta Ruiz" in sb.load_bible(project)["characters"][0]["aliases"]

    killed = sb.apply_deltas(project, [
        {"op": "KILL", "id": "TL-1", "rationale": "cut from the manuscript"}], "agent")
    assert not killed["conflicts"]
    # The record is kept so history stays diffable; the live view hides it.
    assert sb.load_bible(project)["timeline"][0]["killed"] is True
    assert killed["bible"]["timeline"] == []


def test_every_bad_delta_is_a_recorded_conflict_not_an_exception(project):
    result = sb.apply_deltas(project, [
        "not an object",
        {"op": "NUKE", "id": "CHAR-1"},
        {"op": "ADD", "kind": "character"},                 # no name
        {"op": "ADD", "kind": "unicorn", "name": "x"},
        {"op": "EDIT", "id": "CHAR-99", "name": "ghost"},
        {"op": "KILL", "id": "CHAR-99"},
    ], "agent")
    assert result["applied"] == []
    reasons = " | ".join(c["reason"] for c in result["conflicts"])
    assert "not an object" in reasons and "unknown op" in reasons
    assert "requires a name" in reasons and "unknown kind" in reasons
    assert "does not exist" in reasons


def test_an_agent_kill_without_a_rationale_is_refused(project):
    sb.apply_deltas(project, [{"op": "ADD", "kind": "character", "name": "Marta"}], "user")
    result = sb.apply_deltas(project, [{"op": "KILL", "id": "CHAR-1"}], "agent")
    assert result["applied"] == []
    assert "rationale" in result["conflicts"][0]["reason"]
    # A human may kill without justifying themselves.
    assert sb.apply_deltas(project, [{"op": "KILL", "id": "CHAR-1"}], "user")["applied"]


def test_human_edits_win_over_a_stale_agent_edit(project):
    sb.apply_deltas(project, [{"op": "ADD", "kind": "fact", "subject": "Marta",
                               "text": "ojos verdes"}], "user")
    stale = sb.load_bible(project)["facts"][0]["updated_at"]
    # The user changes it after the state the agent last saw.
    bible = sb.load_bible(project)
    bible["facts"][0]["updated_at"] = "2999-01-01T00:00:00Z"
    bible["facts"][0]["last_actor"] = "user"
    sb.save_bible(project, bible)

    result = sb.apply_deltas(project, [{"op": "EDIT", "id": "FACT-1",
                                        "text": "ojos azules",
                                        "base_updated_at": stale}], "agent")
    assert result["applied"] == []
    assert "human edit wins" in result["conflicts"][0]["reason"]
    assert sb.load_bible(project)["facts"][0]["text"] == "ojos verdes"


def test_a_duplicate_character_is_a_conflict(project):
    sb.apply_deltas(project, [{"op": "ADD", "kind": "character", "name": "Marta"}], "user")
    result = sb.apply_deltas(project, [{"op": "ADD", "kind": "character",
                                        "name": "marta"}], "user")
    assert result["applied"] == [] and "already exists" in result["conflicts"][0]["reason"]


def test_an_empty_edit_is_a_no_op_not_a_conflict(project):
    sb.apply_deltas(project, [{"op": "ADD", "kind": "character", "name": "Marta"}], "user")
    result = sb.apply_deltas(project, [{"op": "EDIT", "id": "CHAR-1"}], "user")
    assert result["applied"] == [] and result["conflicts"] == []


# ── prompt rendering ──────────────────────────────────────────────────


def test_findings_render_with_the_fact_they_contradict(project):
    sb.save_bible(project, _marta_bible())
    block = sb.story_block(project, "Marta llegó. Marta tenía los ojos azules.")
    assert "Marta" in block
    assert "ojos azules" in block and "ojos verdes" in block
    assert "capítulo 3, p. 41" in block


def test_story_block_is_empty_when_there_is_nothing_to_say(project):
    assert sb.story_block(project, "Un texto cualquiera sin nombres.") == ""


def test_story_block_never_raises(project):
    class Exploding(dict):
        def get(self, *_a, **_k):
            raise RuntimeError("boom")

    assert sb.story_block(Exploding(), "Marta tenía los ojos azules.") == ""
    assert sb.render_findings([None, {"kind": "x"}, "junk"]) != "…"
