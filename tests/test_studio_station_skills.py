"""The production studio's station and chain skills: every station of the three branches has one, each says what it
receives and delivers, names the app whose tools it drives, and only points at skills the library really has."""
import os
import re

import pytest

from services.memory.skill_format import Skill
from src import skill_library

LIBRARY = skill_library.LIBRARY_DIR
STATIONS = {
    "studio-writer-production-brief": ["writer"],
    "studio-prospero-character-sheet": ["prospero"],
    "studio-prospero-short-video": ["prospero"],
    "studio-vulcan-image-to-3d": ["vulcan"],
    "studio-phidias-scenery": ["phidias"],
    "studio-nikias-print-color": ["nikias"],
    "studio-blender-headless-render": [],
    "studio-godot-playable-scene": [],
    "studio-figure-print-and-slice": ["phidias", "nikias", "vulcan"],
    "studio-drucker-engagement-brief": ["drucker"],
    "studio-writer-report": ["writer"],
    "studio-cicero-deck-and-rehearsal": ["cicero"],
    "studio-vitruvius-design-critique": ["vitruvius"],
    "studio-headless-web-testing": [],
}
CHAINS = ("studio-chain-visual", "studio-chain-presentation", "studio-chain-web")
_REF = re.compile(r"`([a-z0-9]+(?:-[a-z0-9]+)+)`")


def _text(slug):
    with open(os.path.join(LIBRARY, slug, "SKILL.md"), encoding="utf-8") as fh:
        return fh.read()


def _apps(text):
    match = re.search(r"^apps:\s*\[(.*?)\]", text, re.M)
    return [a.strip() for a in match.group(1).split(",")] if match else []


@pytest.mark.parametrize("slug", sorted(STATIONS))
def test_each_station_says_what_it_receives_and_delivers_and_which_app_it_drives(slug):
    text = _text(slug)
    assert "**Receives:**" in text and "**Delivers:**" in text, slug
    assert _apps(text) == STATIONS[slug]
    assert Skill.from_markdown(text).verification


@pytest.mark.parametrize("slug", CHAINS)
def test_each_chain_names_its_hand_offs_and_defects(slug):
    text = _text(slug)
    assert "## Hand-offs" in text and "## Defects" in text
    assert not _apps(text), "a chain is for whoever directs, whatever the app"


@pytest.mark.parametrize("slug", sorted(STATIONS) + list(CHAINS))
def test_every_skill_a_studio_skill_points_at_exists(slug):
    slugs = set(os.listdir(LIBRARY))
    named = {ref for ref in _REF.findall(_text(slug)) if ref.split("-")[0] in ("studio", "game", "print", "blender",
             "character", "presentation", "engagement", "company", "hoard", "e2e", "gamedev")}
    missing = sorted(n for n in named if n not in slugs)
    assert not missing, f"{slug} points at skills the library does not have: {missing}"


def test_every_chain_reaches_every_station_of_its_branch():
    branches = {
        "studio-chain-visual": ["studio-writer-production-brief", "studio-prospero-character-sheet",
                                "studio-prospero-short-video", "studio-vulcan-image-to-3d", "studio-phidias-scenery",
                                "studio-nikias-print-color", "studio-blender-headless-render",
                                "studio-godot-playable-scene", "studio-figure-print-and-slice"],
        "studio-chain-presentation": ["studio-drucker-engagement-brief", "studio-writer-report",
                                      "studio-cicero-deck-and-rehearsal"],
        "studio-chain-web": ["studio-vitruvius-design-critique", "studio-headless-web-testing",
                             "studio-blender-headless-render"],
    }
    for chain, stations in branches.items():
        text = _text(chain)
        assert [s for s in stations if f"`{s}`" not in text] == [], chain
