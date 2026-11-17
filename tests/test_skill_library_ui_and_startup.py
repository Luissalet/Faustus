"""tests/test_skill_library_ui_and_startup.py - accessible interfaces by default, the UI behaviour and style skills, the startup pipeline.

The accessibility skill comes with every turn that builds an interface, installed or not (`src/interface_work.py`); `ui-sdk` and
`anime-ui-controls` come with it on a clear signal; the eleven startup skills install like any library skill and are offered by the
per-turn selector for their kind of request. Every adapted skill credits its source and licence in its text and in
`skills/library/THIRD_PARTY_NOTICES.md` with the commit it was read at.
"""
from __future__ import annotations

import json
import re
import sys
import types
from pathlib import Path

import pytest

from src import interface_work, skill_library

LIBRARY = Path(skill_library.LIBRARY_DIR)
NOTICES = (LIBRARY / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")

SEED = ["seed-scout", "seed-market", "seed-model", "seed-plan", "seed-deck", "seed-investors", "seed-closer", "seed-coach", "seed-terms",
        "seed-dataroom", "seed-updates"]
#: slug -> (repository named in the skill and the notices, licence)
ADDED = {"ui-sdk": ("vercel-labs/ui", "Apache-2.0"), "anime-ui-controls": ("finktheartist/sakuga-vfx", "MIT"),
         **{slug: ("Jakeschincariol/seed-skill", "MIT") for slug in SEED}}
OWN = ["accessible-interfaces"]


def _row(slug):
    return next((line for line in NOTICES.splitlines() if line.startswith("|") and f"`{slug}`" in line), "")


@pytest.mark.parametrize("slug", sorted(ADDED))
def test_each_adapted_skill_credits_its_source_and_is_in_the_notices(slug):
    repo, licence = ADDED[slug]
    text = (LIBRARY / slug / "SKILL.md").read_text(encoding="utf-8")
    assert f"github.com/{repo}" in text and re.search(r"\b" + re.escape(licence), text), slug
    row = _row(slug)
    assert f"github.com/{repo}" in row and licence in row, f"{slug} has no row in THIRD_PARTY_NOTICES.md"
    commit = re.search(r"@ `([0-9a-f]{40})`", row)
    assert commit and commit.group(1) in text, f"{slug}: the notice and the skill must name the same commit"


def test_the_credit_pack_is_credited_and_not_copied():
    assert "KanaWorks_AI (@KanaWorks_AI)" in NOTICES and "kanaworks-sakuga-vfx" in NOTICES
    assert not [p for p in LIBRARY.rglob("*") if p.suffix in {".webp", ".png", ".zip"} and "kanaworks" in p.name.lower()]
    skill = (LIBRARY / "anime-ui-controls" / "SKILL.md").read_text(encoding="utf-8")
    assert "KanaWorks_AI (@KanaWorks_AI)" in skill and "forbids redistributing" in skill


def test_nothing_that_needs_a_second_engine_was_added():
    for slug in [*ADDED, *OWN]:
        assert not list((LIBRARY / slug).rglob("*.py")) and not list((LIBRARY / slug).rglob("*.mjs")), slug
        assert not list((LIBRARY / slug).rglob("*.js")), slug


@pytest.mark.parametrize("slug", [*sorted(ADDED), *OWN])
def test_each_skill_installs_and_uninstalls(slug, tmp_path, monkeypatch):
    monkeypatch.setattr(skill_library, "DATA_DIR", str(tmp_path), raising=False)
    assert skill_library.install("alice", [slug])["results"][0]["status"] == "installed"
    assert slug in skill_library.installed("alice")
    assert skill_library.uninstall("alice", [slug])["results"][0]["status"] == "removed"


def test_the_references_travel_with_their_skill(tmp_path, monkeypatch):
    monkeypatch.setattr(skill_library, "DATA_DIR", str(tmp_path), raising=False)
    skill_library.install("alice", ["ui-sdk", "seed-coach"])
    mgr = skill_library._skills_manager()
    assert "Combobox" in mgr.read_skill_reference("ui-sdk", "references/pick-a-component.md", owner="alice")
    assert "?" in mgr.read_skill_reference("seed-coach", "references/question-bank.md", owner="alice")


def test_the_startup_skills_form_one_pipeline():
    order = ["seed-scout", "seed-market", "seed-model", "seed-plan", "seed-deck", "seed-investors", "seed-closer", "seed-coach", "seed-terms",
             "seed-dataroom", "seed-updates"]
    for slug in order:
        text = (LIBRARY / slug / "SKILL.md").read_text(encoding="utf-8")
        assert all(other in text for other in (order[0], order[-1])), slug
        assert slug == order[-1] or "Next:" in text or "next is" in text, slug
    closer = (LIBRARY / "seed-closer" / "SKILL.md").read_text(encoding="utf-8").lower()
    assert "confirm" in closer and "people" in closer


@pytest.mark.parametrize("prompt, slug", [
    ("research my competitors from their app store reviews and tell me what customers complain about", "seed-scout"),
    ("size the market for my startup: TAM, SAM and SOM with sources", "seed-market"),
    ("find seed investors who write cheques in my sector and stage", "seed-investors"),
    ("write the business plan for my startup from the model and the market", "seed-plan"),
    ("build the financial model for my startup with monthly projections, burn and runway", "seed-model"),
    ("turn the plan into a ten-slide pitch deck for investors", "seed-deck"),
    ("read this term sheet: is it a good deal, what does the SAFE cap mean and how much will I own after the round", "seed-terms"),
    ("help me build a data room for my investors", "seed-dataroom"),
    ("draft the outreach emails to investors and follow up until the call is booked", "seed-closer"),
    ("practice the questions investors will ask me before my pitch", "seed-coach"),
    ("write this month's investor update with the metrics", "seed-updates"),
    ("build a command palette with keyboard navigation using ui-sdk components", "ui-sdk"),
    ("I want anime style buttons and toggles with impact effects for my site", "anime-ui-controls"),
    ("make this form usable from the keyboard with visible focus and aria labels", "accessible-interfaces"),
])
def test_the_selector_offers_each_skill_for_its_kind_of_request(prompt, slug, tmp_path, monkeypatch):
    from src.skills_runtime import selector
    monkeypatch.setattr(skill_library, "DATA_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(selector, "_embedder_failed", True)     # lexical and trigger lanes only: no model download
    skill_library.install("alice", [*sorted(ADDED), *OWN])
    mgr = skill_library._skills_manager()
    chosen = [s["name"] for s in selector.select(mgr, "alice", prompt, skills=mgr.load(owner="alice"),
                                                 threshold=None, max_items=5, min_confidence=0.0)]
    assert slug in chosen, (prompt, chosen)


# ── interface work ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "build me a landing page for my bakery", "crea una página web para mi tienda con un formulario de contacto",
    "add a dark mode toggle to the settings screen", "make a dashboard with tabs and a modal dialog in React", "refactor index.html",
    "diseña una pantalla de login", "write a checkout form with validation", "<button>Save</button> make this nicer",
])
def test_interface_work_is_recognised(text):
    assert interface_work.wants_interface(text) and interface_work.interface_slugs(text)[0] == "accessible-interfaces"


@pytest.mark.parametrize("text", [
    "", "explain how TCP works", "fix the app crash on startup", "write a poem about autumn", "what is the capital of France",
    "summarise this contract", "haz un resumen del informe",
])
def test_other_work_is_left_alone(text):
    assert interface_work.interface_slugs(text) == []
    selected = [{"name": "x"}]
    assert interface_work.with_interface_skills(text, selected) == (selected, 0)


def test_the_extra_skills_ride_along_on_a_clear_signal():
    assert interface_work.interface_slugs("using @ui-sdk/react build a command palette") == ["accessible-interfaces", "ui-sdk"]
    assert interface_work.interface_slugs("haz un sitio web de anime con botones llamativos") == ["accessible-interfaces", "anime-ui-controls"]
    assert interface_work.interface_slugs("build a pixel art login screen") == ["accessible-interfaces", "anime-ui-controls"]
    assert interface_work.interface_slugs("write a manga recommendation") == []


def test_the_bundled_skill_is_added_once_with_its_own_room():
    skills, extra = interface_work.with_interface_skills("build a landing page", [{"name": "hallmark-page-design"}])
    assert [s["name"] for s in skills] == ["hallmark-page-design", "accessible-interfaces"]
    assert skills[1]["source"] == "library" and skills[1]["procedure"] and extra > 800
    again, none = interface_work.with_interface_skills("build a landing page", skills)
    assert [s["name"] for s in again] == [s["name"] for s in skills] and none == 0


def _assemble(monkeypatch, tmp_path, installed, user_text, *, skills_enabled=True, injected=3):
    from services.memory.skills import SkillsManager
    from src import agent_loop
    from src.skills_runtime import selector
    import src.constants as constants
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    prefs = types.ModuleType("routes.prefs_routes")
    prefs._load_for_user = lambda *a: {"skills_enabled": skills_enabled, "memory_enabled": False}
    monkeypatch.setitem(sys.modules, "routes.prefs_routes", prefs)
    settings = lambda key, default=None: injected if key == "skill_max_injected" else default
    monkeypatch.setattr("src.settings.get_setting", settings)
    monkeypatch.setattr(agent_loop, "get_setting", settings)
    monkeypatch.setattr(SkillsManager, "load", lambda *a, **kw: installed)
    monkeypatch.setattr(SkillsManager, "index_for", lambda *a, **kw: [])
    monkeypatch.setattr(selector, "select", lambda *a, **kw: list(installed))
    monkeypatch.setattr(selector, "record_outcome_from_reaction", lambda *a: None)
    monkeypatch.setattr(SkillsManager, "record_use", lambda self, name, **kw: None)
    monkeypatch.setattr(selector, "remember_surfaced", lambda *a: None)
    messages, _ = agent_loop._build_system_prompt(messages=[{"role": "user", "content": user_text}], model="test", active_document=None,
                                                  mcp_mgr=None, owner="test-owner", session_id="s1")
    return [m for m in messages if (m.get("metadata") or {}).get("source") == "skills"]


def test_a_real_turn_that_builds_a_page_carries_the_accessibility_skill(monkeypatch, tmp_path):
    message, = _assemble(monkeypatch, tmp_path, [], "Build me a landing page for my bakery with a contact form")
    content = message["content"]
    assert "### accessible-interfaces" in content and "visible `:focus-visible` indicator" in content
    receipt = message["metadata"]["skill_disclosure"]["fragments"]
    assert [f["skill"] for f in receipt] == ["accessible-interfaces"] and receipt[0]["source"] == "library" and receipt[0]["level"] == 1
    assert message["metadata"]["trusted"] is False


def test_the_selected_skills_keep_their_room_beside_the_added_one(monkeypatch, tmp_path):
    big = {"name": "my-style-guide", "description": "house style " * 120}
    message, = _assemble(monkeypatch, tmp_path, [big], "Design a settings screen with tabs and a dialog")
    assert "### my-style-guide" in message["content"] and "### accessible-interfaces" in message["content"]


def test_other_turns_get_no_interface_skill(monkeypatch, tmp_path):
    assert _assemble(monkeypatch, tmp_path, [], "Explain how TCP slow start works") == []


def test_the_person_can_switch_skills_off(monkeypatch, tmp_path):
    assert _assemble(monkeypatch, tmp_path, [], "Build me a landing page", skills_enabled=False) == []
    assert _assemble(monkeypatch, tmp_path, [], "Build me a landing page", injected=0) == []


def test_a_style_request_loads_the_style_skill_too(monkeypatch, tmp_path):
    message, = _assemble(monkeypatch, tmp_path, [], "Build a page for my anime game with a pixel art loading bar")
    names = [f["skill"] for f in message["metadata"]["skill_disclosure"]["fragments"]]
    assert names == ["accessible-interfaces", "anime-ui-controls"]
    assert "ui_set_install" in message["content"]
    json.dumps(message["metadata"]["skill_disclosure"])
