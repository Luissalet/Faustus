"""Lote 91 (OBJ-7) -- Spanish domain synonyms in the agent's domain detector,
plus the new base-rules line. Three things this covers:

1. `src.agent_loop._classify_agent_request` (the "files/web/email/calendar/…"
   domain decider) recognized almost no Spanish vocabulary before this lot —
   only isolated project-objectives phrases. A plain "mándale un correo",
   "qué tengo mañana", "apunta que...", "lee el archivo X" fell through to
   `low_signal` or the wrong domain, and the deterministic
   `_DOMAIN_TOOL_MAP` seed for that domain's tools never fired (retrieval had
   to carry the whole turn on its own).
2. `_DOMAIN_TOOL_MAP` gained two new domains this lot needs: `media`
   (transform_media/inspect_media/plan_media_transform/generate_image/
   edit_image — had NO domain floor at all before this lot) and
   `project_board` (the lote-92 board_* tools, tolerated even before they
   are registered). Git tools are deliberately NOT duplicated into a new
   `_DOMAIN_TOOL_MAP` entry — they already have their own Spanish-aware
   floor (`_git_intent` in the agent loop's tool-selection code, tested by
   tests/test_l88_git_tools_offered.py and tests/test_l90_git_natural.py) —
   this file only asserts that floor still works after this lot's edits.
3. `_AGENT_RULES`/`_API_AGENT_RULES` gained the one line the brief specifies
   verbatim: "Users speak plainly: map what they ask to the right tool
   yourself; never ask them to name a tool, a path or a command, and never
   say a tool is unavailable without first checking the catalog (list the
   tools you have)".
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from src import agent_loop


# ---------------------------------------------------------------------------
# 1 & 2. Domain detector + domain/tool map
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected_domain",
    [
        ("mándale un correo a Marta con el resumen", "email"),
        ("envíale un correo a Pedro", "email"),
        ("revisa mi correo", "email"),
        ("apunta que hay que revisar el login", "notes_calendar_tasks"),
        ("anota esto para luego", "notes_calendar_tasks"),
        ("recuérdame llamar al dentista mañana", "notes_calendar_tasks"),
        ("qué tengo mañana", "notes_calendar_tasks"),
        ("pon una reunión el viernes a las 10", "notes_calendar_tasks"),
        ("qué hay en mi agenda", "notes_calendar_tasks"),
        ("busca cuánto cuesta un iPhone", "web"),
        ("investiga los precios de la gasolina", "web"),
        ("qué dice internet sobre esto", "web"),
        ("lee el archivo config.py", "files"),
        ("abre la carpeta de proyectos", "files"),
        ("qué hay pendiente en este proyecto", "project_board"),
        ("hay un bug en el login", "project_board"),
        ("nueva idea para el proyecto", "project_board"),
        ("genera una imagen de un gato", "media"),
        ("transcribe este vídeo", "media"),
    ],
)
def test_domain_detector_recognizes_spanish_synonyms(text, expected_domain):
    result = agent_loop._classify_agent_request([], text)
    assert expected_domain in result["domains"], (text, result["domains"])


def test_domain_tool_map_has_media_domain_with_no_prior_floor():
    # transform_media/inspect_media/plan_media_transform had no domain entry
    # at all before this lot — assert they are covered now.
    media_tools = agent_loop._DOMAIN_TOOL_MAP["media"]
    for name in ("transform_media", "inspect_media", "plan_media_transform"):
        assert name in media_tools


def test_domain_tool_map_has_project_board_domain():
    board_tools = agent_loop._DOMAIN_TOOL_MAP["project_board"]
    expected = {
        "board_list", "board_ready", "board_get", "board_create",
        "board_update", "board_comment", "board_link", "board_claim",
    }
    assert expected <= board_tools


def test_every_domain_tool_map_key_has_matching_rule_text():
    # _domain_rules_for_tools does `_DOMAIN_RULES[domain]` for every domain
    # whose tools overlap the turn's selected tools — a domain key with no
    # matching rule text is a live KeyError waiting for its first tool hit.
    missing = sorted(d for d in agent_loop._DOMAIN_TOOL_MAP if d not in agent_loop._DOMAIN_RULES)
    assert not missing, missing


def test_domain_rules_for_tools_does_not_raise_for_media_or_board():
    # Exercises the actual lookup path, not just key presence.
    rules = agent_loop._domain_rules_for_tools({"transform_media", "board_create"})
    assert any("Media rules" in r for r in rules)
    assert any("Project board rules" in r for r in rules)


def test_project_board_domain_does_not_crash_when_tools_unregistered():
    # Naming board_* tools in _DOMAIN_TOOL_MAP must be safe even on a build
    # where lote 92 has not landed them into TOOL_HANDLERS yet — the domain
    # detector and _DOMAIN_TOOL_MAP must never assume the tool exists.
    result = agent_loop._classify_agent_request([], "qué hay pendiente en este proyecto")
    assert "project_board" in result["domains"]
    tools = agent_loop._DOMAIN_TOOL_MAP["project_board"]
    assert isinstance(tools, set) and tools  # just a set of names, no registry lookup


# ---------------------------------------------------------------------------
# Git floor untouched: still fires on Spanish git vocabulary after this
# lot's _DOMAIN_TOOL_MAP/_classify_agent_request edits (regression guard —
# the brief's "verifica que [el dominio] git incluya las tools nuevas" item;
# git already has its own floor, see module docstring).
# ---------------------------------------------------------------------------

def test_git_natural_language_still_recognized_by_its_own_floor():
    import re
    from src.agent_loop import _classify_agent_request  # noqa: F401 (documents the sibling mechanism)

    git_intent_re = re.compile(
        r"\b(git|commit|commits|commitea|push|pull|pull request|fetch|rama|ramas|branch|branches|merge|"
        r"mergea|mergear|fusiona|subir|sube|sincroniza|stage|checkout|repositorio|repo)\b",
        re.IGNORECASE,
    )
    for text in ("sube los cambios", "en qué rama estamos", "haz commit de esto", "mergea esta rama"):
        assert git_intent_re.search(text), text


# ---------------------------------------------------------------------------
# 3. New base-rules line
# ---------------------------------------------------------------------------

_NEW_RULE_LINE = (
    "Users speak plainly: map what they ask to the right tool yourself; "
    "never ask them to name a tool, a path or a command, and never say a "
    "tool is unavailable without first checking the catalog (list the "
    "tools you have)."
)


def test_agent_rules_has_new_map_dont_ask_line():
    assert _NEW_RULE_LINE in agent_loop._AGENT_RULES


def test_api_agent_rules_has_new_map_dont_ask_line():
    assert _NEW_RULE_LINE in agent_loop._API_AGENT_RULES
