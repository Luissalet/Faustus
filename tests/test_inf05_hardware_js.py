"""INF-05 (Lote C — Studio) — physical GPU topology, the per-GPU memory
budget, the three context limits, and profile activation.

Two independent checks:

1. `studio/checks/hardware.check.mjs`, which drives the real
   `studio/src/adapters/hardware.ts` (parsers, `gbLabel`, `sourceLabel`,
   `linkLabel`, `reconciliationLabel`, `estimateRangeLabel`, `basisLabel`,
   `verdictLabel`, the three context-limit labels, `consumerLabel`,
   `sharedSpillLabel`, `staleLabel`) through esbuild rather than
   re-implementing their logic in Python — same pattern
   `tests/test_inf04_bench_js.py` uses for `adapters/bench.ts`.

2. A static source scan of `studio/src/screens/cookbook/Servers.tsx` and
   `studio/src/screens/cookbook/Optimize.tsx` for CONTRATO_INF05.md Lote
   C's one non-negotiable UI rule: "Nothing runs automatically except
   initial loads (no annotate/activate/relaunch from a `useEffect`)".
   Concretely: `annotateTopology(` must never appear inside a `useEffect`
   in `Servers.tsx`, and `activateProfile(`/`deactivateProfile(` must never
   appear inside a `useEffect` in `Optimize.tsx` — only an explicit click
   (Annotate…/Activate/Deactivate/Roll back) ever calls them. This is a
   plain regex/bracket-matching scan, the same technique
   `tests/test_inf04_bench_js.py` already uses for `startBench` in
   `Optimize.tsx`, and `tests/test_studio_guards.py` uses Studio-wide.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SERVERS = ROOT / "studio" / "src" / "screens" / "cookbook" / "Servers.tsx"
OPTIMIZE = ROOT / "studio" / "src" / "screens" / "cookbook" / "Optimize.tsx"
SERVE_FORM = ROOT / "studio" / "src" / "screens" / "cookbook" / "ServeForm.tsx"
HARDWARE_ADAPTER = ROOT / "studio" / "src" / "adapters" / "hardware.ts"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (ROOT / "node_modules" / "esbuild" / "lib" / "main.js").exists()
_SKIP = pytest.mark.skipif(not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed")


@_SKIP
def test_hardware_helpers_js():
    result = subprocess.run(
        ["node", "studio/checks/hardware.check.mjs"], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "all checks passed" in result.stdout


def _call_bodies(text: str, callee: str) -> list[str]:
    """Every argument list of a `callee(...)` call in `text`, balanced-paren
    matched (so a nested `(...)` inside the call, e.g. an arrow function's
    own parameter list, does not close the scan early)."""
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


def test_files_exist():
    assert HARDWARE_ADAPTER.exists(), "studio/src/adapters/hardware.ts is missing"
    assert SERVERS.exists(), "studio/src/screens/cookbook/Servers.tsx is missing"
    assert OPTIMIZE.exists(), "studio/src/screens/cookbook/Optimize.tsx is missing"
    assert SERVE_FORM.exists(), "studio/src/screens/cookbook/ServeForm.tsx is missing"


def test_servers_never_annotates_from_useeffect():
    text = SERVERS.read_text(encoding="utf-8")

    effect_bodies = _call_bodies(text, "useEffect")
    assert effect_bodies, (
        "Servers.tsx has no useEffect at all -- if that's now genuinely true, "
        "this guard has nothing left to check and should be revisited, not silently passed"
    )
    for body in effect_bodies:
        assert "annotateTopology(" not in body, (
            "Servers.tsx must never call annotateTopology() from inside a useEffect -- "
            "a person's transport annotation is only ever the result of an explicit "
            "'Annotate...' dialog Save click:\n" + body
        )

    # The other half of the same rule: annotateTopology must actually be
    # wired somewhere OUTSIDE any effect (the Annotate dialog's Save
    # button), otherwise this guard would trivially pass by never calling
    # it at all.
    assert "annotateTopology(" in text, (
        "Servers.tsx never calls annotateTopology() anywhere -- the Annotate... "
        "dialog's Save button must call it explicitly"
    )

    # hw-topology/hw-reconcile/hw-refresh/hw-budget are the exact testids
    # CONTRATO_INF05.md Lote C names for this screen's physical-GPU section.
    for testid in ("hw-topology", "hw-reconcile", "hw-refresh", "hw-budget"):
        assert f'data-testid="{testid}"' in text or f"testId=\"{testid}\"" in text, (
            f"Servers.tsx is missing the {testid!r} testid CONTRATO_INF05.md Lote C names"
        )


def test_optimize_never_activates_from_useeffect():
    text = OPTIMIZE.read_text(encoding="utf-8")

    effect_bodies = _call_bodies(text, "useEffect")
    for body in effect_bodies:
        assert "activateProfile(" not in body, (
            "Optimize.tsx must never call activateProfile() from inside a useEffect -- "
            "only an explicit 'Activate'/'Roll back' click does (§13: nothing here "
            "reactivates or reverts on its own):\n" + body
        )
        assert "deactivateProfile(" not in body, (
            "Optimize.tsx must never call deactivateProfile() from inside a useEffect -- "
            "only an explicit 'Deactivate' click does:\n" + body
        )

    assert "activateProfile(" in text, (
        "Optimize.tsx never calls activateProfile() anywhere -- the Activate/Roll back "
        "buttons (bench-activate/bench-rollback) must call it explicitly"
    )
    assert "deactivateProfile(" in text, (
        "Optimize.tsx never calls deactivateProfile() anywhere -- the Deactivate button "
        "(bench-deactivate) must call it explicitly"
    )

    for testid in ("bench-activate", "bench-deactivate", "bench-rollback"):
        assert f'testId="{testid}"' in text or f'data-testid="{testid}"' in text, (
            f"Optimize.tsx is missing the {testid!r} testid CONTRATO_INF05.md Lote C names"
        )


def test_serve_form_never_relaunches_from_useeffect():
    """ServeForm.tsx's memory-estimate refresh (`getBudget`/`getContextLimits`,
    a plain read) may run from a `useEffect` on ctx/slots/GPU changes --
    CONTRATO_INF05.md Lote C explicitly asks for that debounce. What may
    NEVER run from a `useEffect` is an actual launch/relaunch: neither
    `launchServe(` nor a direct `serveModel(` call may appear inside one --
    a serve only ever starts from the Launch button's `onClick`, or from
    the VRAM admission dialog's own explicit decision callback (which is
    itself only ever invoked by that dialog in response to a click, never
    by React on mount)."""
    text = SERVE_FORM.read_text(encoding="utf-8")

    effect_bodies = _call_bodies(text, "useEffect")
    assert effect_bodies, (
        "ServeForm.tsx has no useEffect at all -- if that's now genuinely true, "
        "this guard has nothing left to check and should be revisited, not silently passed"
    )
    for body in effect_bodies:
        assert "launchServe(" not in body, (
            "ServeForm.tsx must never call launchServe() from inside a useEffect -- "
            "only the Launch button's onClick may:\n" + body
        )

    assert "serve-estimate" in text, "ServeForm.tsx is missing the 'serve-estimate' testid"
    assert "serve-context-limits" in text, "ServeForm.tsx is missing the 'serve-context-limits' testid"
    assert "getBudget(" in text, "ServeForm.tsx never calls getBudget() -- the memory estimate never loads"
    assert "getContextLimits(" in text, "ServeForm.tsx never calls getContextLimits() -- the context-limits line never loads"
