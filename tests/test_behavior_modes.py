"""Tests for `src/behavior_modes.py` — CONTRATO_MODOS Lote A.

No LLM is ever involved. `save_user_mode`/`delete_user_mode` are pointed at
a tmp_path file (never the real `DATA_DIR/behavior_modes.json`) via
monkeypatching `behavior_modes._user_modes_path`.
"""
from __future__ import annotations

import pytest

from src import behavior_modes


BUILTIN_IDS = {
    "default", "adversarial", "socratic", "terse",
    "mentor", "red_team", "observer", "editor",
}


@pytest.fixture(autouse=True)
def _user_modes_file(tmp_path, monkeypatch):
    """Every test gets its own empty user-modes store."""
    path = tmp_path / "behavior_modes.json"
    monkeypatch.setattr(behavior_modes, "_user_modes_path", lambda: str(path))
    return path


# ── load_builtin ─────────────────────────────────────────────────────── #

def test_all_eight_builtins_load_and_validate():
    modes = behavior_modes.load_builtin()
    assert set(modes.keys()) == BUILTIN_IDS
    for mode_id, mode in modes.items():
        assert mode.id == mode_id
        assert mode.builtin is True
        assert isinstance(mode.version, int)
        assert mode.name.get("en") and mode.name.get("es")
        assert mode.description.get("en") and mode.description.get("es")
        assert isinstance(mode.prompt, str)
        assert isinstance(mode.checks, dict)


def test_adversarial_keeps_luis_rules_literally():
    """Luis's own three rules must survive verbatim in the shipped prompt —
    this module must never rewrite `config/behavior_modes/*.json`."""
    mode = behavior_modes.load_builtin()["adversarial"]
    assert "Never start with agreement" in mode.prompt
    assert "[Certain]" in mode.prompt
    assert "Great question" in mode.prompt


def test_default_mode_has_no_prompt():
    mode = behavior_modes.load_builtin()["default"]
    assert mode.prompt == ""
    assert mode.checks == {}


# ── list_modes / get_mode ────────────────────────────────────────────── #

def test_list_modes_default_first_then_builtins_by_name():
    modes = behavior_modes.list_modes(owner=None)
    assert modes[0].id == "default"
    ids = [m.id for m in modes]
    assert set(ids) == BUILTIN_IDS
    non_default_names = [m.name["en"].casefold() for m in modes[1:]]
    assert non_default_names == sorted(non_default_names)


def test_list_modes_includes_only_this_owners_user_modes():
    behavior_modes.save_user_mode("alice", {
        "id": "my_mode", "name": {"en": "Mine", "es": "Mío"},
        "description": {"en": "d", "es": "d"}, "prompt": "Be mine.",
    })
    alice_modes = behavior_modes.list_modes(owner="alice")
    bob_modes = behavior_modes.list_modes(owner="bob")
    assert "my_mode" in [m.id for m in alice_modes]
    assert "my_mode" not in [m.id for m in bob_modes]


def test_get_mode_builtin_and_unknown():
    assert behavior_modes.get_mode("adversarial") is not None
    assert behavior_modes.get_mode("nonexistent_mode_xyz") is None
    assert behavior_modes.get_mode(None) is None
    assert behavior_modes.get_mode("") is None


# ── save_user_mode ───────────────────────────────────────────────────── #

def test_save_user_mode_rejects_builtin_id():
    with pytest.raises(behavior_modes.ModeError) as excinfo:
        behavior_modes.save_user_mode("alice", {
            "id": "adversarial", "name": {"en": "X", "es": "X"},
            "description": {"en": "d", "es": "d"}, "prompt": "hi",
        })
    assert str(excinfo.value) == "builtin"


@pytest.mark.parametrize("bad_id", ["Bad", "1bad", "b", "bad-id", "bad id", ""])
def test_save_user_mode_rejects_bad_slug(bad_id):
    with pytest.raises(behavior_modes.ModeError) as excinfo:
        behavior_modes.save_user_mode("alice", {
            "id": bad_id, "name": {"en": "X", "es": "X"},
            "description": {"en": "d", "es": "d"}, "prompt": "hi",
        })
    assert str(excinfo.value) == "invalid"


def test_save_user_mode_rejects_long_prompt():
    with pytest.raises(behavior_modes.ModeError) as excinfo:
        behavior_modes.save_user_mode("alice", {
            "id": "too_long", "name": {"en": "X", "es": "X"},
            "description": {"en": "d", "es": "d"},
            "prompt": "a" * (behavior_modes.MAX_PROMPT_CHARS + 1),
        })
    assert str(excinfo.value) == "invalid"


def test_save_user_mode_rejects_unknown_check_key():
    with pytest.raises(behavior_modes.ModeError) as excinfo:
        behavior_modes.save_user_mode("alice", {
            "id": "bad_checks", "name": {"en": "X", "es": "X"},
            "description": {"en": "d", "es": "d"}, "prompt": "hi",
            "checks": {"not_a_real_check": True},
        })
    assert str(excinfo.value) == "invalid"


def test_save_user_mode_round_trips_and_versions_bump():
    mode = behavior_modes.save_user_mode("alice", {
        "id": "iterate", "name": {"en": "Iterate", "es": "Iterar"},
        "description": {"en": "d", "es": "d"}, "prompt": "v1",
        "checks": {"max_words": 50},
    })
    assert mode.version == 1
    assert mode.owner == "alice"
    assert mode.builtin is False

    updated = behavior_modes.save_user_mode("alice", {
        "id": "iterate", "name": {"en": "Iterate", "es": "Iterar"},
        "description": {"en": "d", "es": "d"}, "prompt": "v2",
        "checks": {"max_words": 50},
    })
    assert updated.version == 2
    fetched = behavior_modes.get_mode("iterate", owner="alice")
    assert fetched.prompt == "v2"


def test_save_user_mode_rejects_taking_over_another_owners_id():
    behavior_modes.save_user_mode("alice", {
        "id": "shared_id", "name": {"en": "X", "es": "X"},
        "description": {"en": "d", "es": "d"}, "prompt": "p",
    })
    with pytest.raises(behavior_modes.ModeError) as excinfo:
        behavior_modes.save_user_mode("bob", {
            "id": "shared_id", "name": {"en": "Y", "es": "Y"},
            "description": {"en": "d", "es": "d"}, "prompt": "p2",
        })
    assert str(excinfo.value) == "invalid"


# ── delete_user_mode ─────────────────────────────────────────────────── #

def test_delete_user_mode_rejects_builtin():
    with pytest.raises(behavior_modes.ModeError) as excinfo:
        behavior_modes.delete_user_mode("alice", "terse")
    assert str(excinfo.value) == "builtin"


def test_delete_user_mode_not_found_for_missing_or_other_owner():
    behavior_modes.save_user_mode("alice", {
        "id": "alices_mode", "name": {"en": "X", "es": "X"},
        "description": {"en": "d", "es": "d"}, "prompt": "p",
    })
    with pytest.raises(behavior_modes.ModeError) as excinfo:
        behavior_modes.delete_user_mode("bob", "alices_mode")
    assert str(excinfo.value) == "not_found"
    with pytest.raises(behavior_modes.ModeError) as excinfo:
        behavior_modes.delete_user_mode("alice", "does_not_exist")
    assert str(excinfo.value) == "not_found"


def test_delete_user_mode_succeeds_for_owner():
    behavior_modes.save_user_mode("alice", {
        "id": "gone_soon", "name": {"en": "X", "es": "X"},
        "description": {"en": "d", "es": "d"}, "prompt": "p",
    })
    behavior_modes.delete_user_mode("alice", "gone_soon")
    assert behavior_modes.get_mode("gone_soon", owner="alice") is None


# ── resolve ──────────────────────────────────────────────────────────── #

def test_resolve_precedence_requested_wins():
    mode = behavior_modes.resolve(
        requested="terse", session_mode="mentor", default_setting="socratic", owner=None,
    )
    assert mode.id == "terse"


def test_resolve_precedence_session_wins_over_default():
    mode = behavior_modes.resolve(
        requested=None, session_mode="mentor", default_setting="socratic", owner=None,
    )
    assert mode.id == "mentor"


def test_resolve_precedence_default_setting_wins_over_hardcoded():
    mode = behavior_modes.resolve(
        requested=None, session_mode=None, default_setting="socratic", owner=None,
    )
    assert mode.id == "socratic"


def test_resolve_falls_back_to_default_with_nothing_set():
    mode = behavior_modes.resolve(requested=None, session_mode=None, default_setting=None, owner=None)
    assert mode.id == "default"


def test_resolve_unknown_id_falls_through_every_level_to_default():
    mode = behavior_modes.resolve(
        requested="totally_unknown_mode",
        session_mode="also_unknown",
        default_setting="still_unknown",
        owner=None,
    )
    assert mode.id == "default"


def test_resolve_unknown_requested_falls_through_to_session():
    mode = behavior_modes.resolve(
        requested="totally_unknown_mode", session_mode="terse", default_setting=None, owner=None,
    )
    assert mode.id == "terse"


# ── system_block ─────────────────────────────────────────────────────── #

def test_system_block_none_for_default():
    assert behavior_modes.system_block(behavior_modes.get_mode("default")) is None
    assert behavior_modes.system_block(None) is None


def test_system_block_has_header_and_prompt_for_non_default():
    mode = behavior_modes.get_mode("adversarial")
    block = behavior_modes.system_block(mode)
    assert block is not None
    assert block.startswith('Behaviour mode "Adversarial":')
    assert "Never start with agreement" in block


# ── check_response ───────────────────────────────────────────────────── #

def test_check_response_empty_text_runs_nothing():
    mode = behavior_modes.get_mode("adversarial")
    result = behavior_modes.check_response(mode, "")
    assert result == {"checked": [], "violations": []}


def test_check_response_confidence_tags_positive_and_negative():
    mode = behavior_modes.get_mode("adversarial")
    ok = behavior_modes.check_response(mode, "[Certain] This is true. What now?")
    assert "confidence_tags" in ok["checked"]
    assert not any(v["rule"] == "confidence_tags" for v in ok["violations"])

    bad = behavior_modes.check_response(mode, "This is true, no tags here. What now?")
    assert any(v["rule"] == "confidence_tags" for v in bad["violations"])

    ok_es = behavior_modes.check_response(mode, "[Seguro] Esto es cierto. ¿Y ahora?")
    assert not any(v["rule"] == "confidence_tags" for v in ok_es["violations"])


def test_check_response_first_sentence_challenge_positive_and_negative():
    mode = behavior_modes.get_mode("adversarial")
    bad = behavior_modes.check_response(mode, "[Certain] Yes, that is correct. Next?")
    assert any(v["rule"] == "first_sentence" for v in bad["violations"])

    bad_es = behavior_modes.check_response(mode, "[Seguro] Tienes razón en eso. ¿Y ahora?")
    assert any(v["rule"] == "first_sentence" for v in bad_es["violations"])

    ok = behavior_modes.check_response(mode, "[Certain] That assumption is shaky. What backs it?")
    assert not any(v["rule"] == "first_sentence" for v in ok["violations"])


def test_check_response_forbidden_phrases_positive_and_negative_and_code_block_excluded():
    mode = behavior_modes.get_mode("adversarial")
    bad = behavior_modes.check_response(mode, "[Certain] Great question, let's dig in.")
    assert any(v["rule"] == "forbidden_phrases" for v in bad["violations"])

    bad_es = behavior_modes.check_response(mode, "[Seguro] Absolutamente, tienes razón.")
    assert any(v["rule"] == "forbidden_phrases" for v in bad_es["violations"])

    ok = behavior_modes.check_response(mode, "[Certain] That claim needs evidence.")
    assert not any(v["rule"] == "forbidden_phrases" for v in ok["violations"])

    in_code = behavior_modes.check_response(
        mode,
        "[Certain] Here is the snippet:\n```\n# Great question\nprint('hi')\n```\nThat stands alone.",
    )
    assert not any(v["rule"] == "forbidden_phrases" for v in in_code["violations"])


def test_check_response_ends_with_question_positive_and_negative():
    mode = behavior_modes.get_mode("mentor")
    ok = behavior_modes.check_response(mode, "Here is the shape of it.\n\nDoes that land?")
    assert not any(v["rule"] == "ends_with_question" for v in ok["violations"])
    bad = behavior_modes.check_response(mode, "Here is the shape of it.\n\nNo question here.")
    assert any(v["rule"] == "ends_with_question" for v in bad["violations"])


def test_check_response_max_questions_positive_and_negative():
    mode = behavior_modes.get_mode("socratic")
    ok = behavior_modes.check_response(mode, "What led you there?")
    assert not any(v["rule"] == "max_questions" for v in ok["violations"])
    bad = behavior_modes.check_response(mode, "What led you there? And why that? And then?")
    assert any(v["rule"] == "max_questions" for v in bad["violations"])


def test_check_response_max_words_positive_and_negative():
    mode = behavior_modes.get_mode("terse")
    ok = behavior_modes.check_response(mode, "Short answer.")
    assert not any(v["rule"] == "max_words" for v in ok["violations"])
    bad = behavior_modes.check_response(mode, " ".join(["word"] * 200))
    assert any(v["rule"] == "max_words" for v in bad["violations"])


def test_check_response_none_mode_is_a_noop():
    assert behavior_modes.check_response(None, "some text") == {"checked": [], "violations": []}
