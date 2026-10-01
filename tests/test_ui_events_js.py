"""The agent's ui_control requests reach Studio and are applied
(studio/src/shell/uiEvents.ts). Studio used to drop them: «pon el tema
forest» answered "Theme changed" and nothing changed. Also: two built-in
palettes renamed (graphite, terracotta) keep a theme saved under the old
name, and the embers/perlin-flow background effects no longer throw."""
from __future__ import annotations

import asyncio
from pathlib import Path
import shutil
import subprocess

import pytest

from src.ai_interaction import do_ui_control

ROOT = Path(__file__).resolve().parent.parent
_OK = shutil.which("node") is not None and (ROOT / "node_modules" / "esbuild" / "lib" / "main.js").exists() \
    and (ROOT / "node_modules" / "happy-dom").exists()


@pytest.mark.skipif(not _OK, reason="node + node_modules/esbuild + happy-dom needed")
def test_studio_applies_ui_events():
    result = subprocess.run(
        ["node", "studio/checks/ui-events.check.mjs"], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ALL OK" in result.stdout


@pytest.mark.parametrize("name", ["graphite", "terracotta"])
def test_the_renamed_palettes_are_known_to_the_agent(name):
    r = asyncio.run(do_ui_control(f"set_theme {name}"))
    assert r.get("ui_event") == "set_theme" and r.get("theme_name") == name, r


def test_create_theme_carries_its_background_effect():
    r = asyncio.run(do_ui_control("create_theme volcan #1a0f0a #ffd7b0 #24140c #5a2a14 #ff5a1f bgPattern=embers"))
    assert r.get("ui_event") == "create_theme" and r.get("bg") == {"pattern": "embers"}, r


def test_the_stream_forwards_what_studio_needs():
    src = (ROOT / "src" / "agent_loop.py").read_text(encoding="utf-8")
    start = src.index('tool_output_data["ui_event"] = result["ui_event"]')
    block = src[start:start + 1200]
    for key in ('"theme_name"', '"colors"', '"bg"', '"selector"', '"label"', '"panel"', '"uid"', '"body"', '"mode"', '"model"'):
        assert key in block, key



@pytest.mark.parametrize("text", [
    "Pon el tema forest en la interfaz.",
    "Cambia la paleta a terracotta",
    "Hazme un tema volcán con brasas",
    "Abre mis notas",
    "Activa la búsqueda web",
    "Pasa a modo chat",
    "Cambia de modelo a qwen",
])
def test_spanish_screen_requests_reach_ui_control(text):
    import src.agent_tools  # noqa: F401  (agent_tools <-> tool_parsing import cycle)
    from src.agent_loop import _classify_agent_request
    assert "ui" in (_classify_agent_request([], text).get("domains") or set())


@pytest.mark.parametrize("text", ["Háblame sobre el tema de la inflación", "Arregla el bug del login"])
def test_a_topic_is_not_a_theme(text):
    import src.agent_tools  # noqa: F401
    from src.agent_loop import _classify_agent_request
    assert "ui" not in (_classify_agent_request([], text).get("domains") or set())


def test_a_reply_about_a_theme_change_is_backed_by_the_screen_tool():
    from src.agent_harness import TurnLedger
    ledger = TurnLedger(workspace=None, user_text="Pon el tema forest")
    ledger.record("ui_control", "set_theme forest", {"ui_event": "set_theme", "theme_name": "forest", "results": "Theme changed to 'forest'"}, 1)
    check = ledger.check_completion("Listo: el tema **forest** ya está aplicado en la interfaz.")
    assert "claims_without_mutation" not in check["reasons"], check
    fresh = TurnLedger(workspace=None, user_text="Pon el tema forest")
    assert "claims_without_mutation" in fresh.check_completion("Listo: el tema **forest** ya está aplicado en la interfaz.")["reasons"]
