"""CONTRATO_MODOS Lote B (Studio) — behaviour modes.

Two independent checks, same split `tests/test_inf04_bench_js.py` uses for
INF-04's benchmark screen:

1. `studio/checks/behavior_modes.check.mjs`, which drives the real
   `studio/src/adapters/behaviorModes.ts` (`modeLabel`, `modeDescription`,
   `violationsLabel`, `findModeByIdOrName`, `resolveModeCommand`) through
   esbuild rather than re-implementing their logic in Python, and includes
   its own static guard that neither `Composer.tsx` nor `Studio.tsx` ever
   calls `setSessionMode(` from inside a `useEffect` — CONTRATO_MODOS.md's
   "Nunca cambia solo".

2. A second, independent static scan here (kept in Python for the same
   reason `test_inf04_bench_js.py` keeps its own: no esbuild/JSX parsing
   needed, just balanced-paren text scanning) that:
   - the new screens/files this lote owns actually exist;
   - `/mode` (and its alias `/modo`) are registered in `commands.ts`, in the
     Chat category;
   - `Studio.tsx`'s `sendTurn({...})` call site forwards a behaviour mode
     (`behaviorMode: options.behaviorMode`) — the same "is the wire actually
     connected" check `studio-autonomy-wiring.check.mjs` does for
     `knobs.autonomyPreset`, restated in Python so this lote does not need a
     second `.mjs` file for one assertion;
   - the transcript chip/badge and the Settings section carry the exact
     `data-testid`s CONTRATO_MODOS.md names, so a rename of any one of them
     is caught here rather than discovered by a person clicking around.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STUDIO_SRC = ROOT / "studio" / "src"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (ROOT / "node_modules" / "esbuild" / "lib" / "main.js").exists()
_SKIP = pytest.mark.skipif(not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed")


@_SKIP
def test_behavior_modes_helpers_js():
    result = subprocess.run(
        ["node", "studio/checks/behavior_modes.check.mjs"], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "all checks passed" in result.stdout


def _call_bodies(text: str, callee: str) -> list[str]:
    """Every argument list of a `callee(...)` call in `text`, balanced-paren
    matched — same helper `tests/test_inf04_bench_js.py` uses."""
    bodies: list[str] = []
    marker = f"{callee}("
    idx = 0
    while True:
        pos = text.find(marker, idx)
        if pos == -1:
            break
        start = pos + len(marker)
        depth = 1
        i = start
        while i < len(text) and depth > 0:
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
            i += 1
        bodies.append(text[start:i])
        idx = i
    return bodies


def _read(relpath: str) -> str:
    path = STUDIO_SRC / relpath
    assert path.exists(), f"missing file: studio/src/{relpath}"
    return path.read_text(encoding="utf-8")


def test_new_files_exist():
    assert (STUDIO_SRC / "adapters" / "behaviorModes.ts").exists()
    assert (STUDIO_SRC / "screens" / "settings" / "BehaviorModes.tsx").exists()
    assert (ROOT / "studio" / "checks" / "behavior_modes.check.mjs").exists()


def test_mode_command_registered():
    text = _read("screens/studio/commands.ts")
    assert re.search(r"name:\s*'mode'", text), "/mode is not registered in commands.ts"
    m = re.search(r"\{[^{}]*name:\s*'mode'[^{}]*\}", text, re.DOTALL)
    assert m, "could not find the /mode command's own object literal"
    entry = m.group(0)
    assert "'modo'" in entry, "/mode must alias /modo (CONTRATO_MODOS.md's Spanish alias)"
    assert "category: 'Chat'" in entry, "/mode belongs in the Chat category"


def test_studio_sends_behavior_mode_on_first_turn():
    """Mirrors `studio/checks/studio-autonomy-wiring.check.mjs`'s own check
    for `knobs.autonomyPreset` — restated here in Python rather than as a
    second `.mjs` file, since this lote's `.mjs` already carries the
    setSessionMode/useEffect guard and one more esbuild bundle buys nothing
    a text scan doesn't already give for a single field."""
    text = _read("screens/Studio.tsx")
    call_start = text.index("for await (const event of sendTurn({")
    body_start = text.index("{", call_start + len("for await (const event of sendTurn("))
    depth = 0
    i = body_start
    for i in range(body_start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                break
    call_body = text[body_start : i + 1]
    assert re.search(r"behaviorMode\s*:\s*options\.behaviorMode", call_body), (
        "Studio.tsx's sendTurn({...}) must forward `behaviorMode: options.behaviorMode` -- "
        "otherwise a session's first turn never actually carries the picked mode"
    )


def test_setsessionmode_never_called_from_a_useeffect_python_side():
    """The same rule `behavior_modes.check.mjs` guards (skipped there when
    node/esbuild are unavailable) restated as a plain Python scan, so the
    guard still runs even on a machine without node."""
    for relpath in ("screens/studio/Composer.tsx", "screens/Studio.tsx"):
        text = _read(relpath)
        effect_bodies = _call_bodies(text, "useEffect")
        assert effect_bodies, f"{relpath} has no useEffect at all -- this guard has nothing to check"
        for body in effect_bodies:
            assert "setSessionMode(" not in body, (
                f"{relpath} must never call setSessionMode(...) from inside a useEffect:\n" + body
            )
    assert "setSessionMode(" in _read("screens/Studio.tsx"), (
        "Studio.tsx never calls setSessionMode(...) anywhere -- the picker/`/mode` must call it explicitly"
    )


def test_composer_chip_testid():
    text = _read("screens/studio/Composer.tsx")
    assert 'data-testid="behavior-mode-chip"' in text


def test_transcript_mode_testids():
    text = _read("screens/studio/Transcript.tsx")
    assert 'data-testid="turn-mode"' in text
    assert 'data-testid="turn-mode-warning"' in text


def test_settings_modes_testids():
    text = _read("screens/settings/BehaviorModes.tsx")
    for testid in ("modes-list", "modes-new", "modes-save", "modes-try", "modes-default"):
        assert f'testId="{testid}"' in text or f'data-testid="{testid}"' in text, f"missing testid {testid!r}"


def test_settings_screen_wires_modes_section():
    text = _read("screens/Settings.tsx")
    assert "'modes'" in text, "Settings.tsx's SectionKey must include 'modes'"
    assert "BehaviorModesSection" in text
    # Visible to any signed-in user, not admin-gated at the nav level --
    # only "Set as default" inside the section itself is admin-only.
    section_line = next((line for line in text.splitlines() if "key: 'modes'" in line), "")
    assert section_line, "SECTIONS is missing the 'modes' entry"
    assert "admin: true" not in section_line, "the Behaviour modes SECTION must not be admin-gated"
