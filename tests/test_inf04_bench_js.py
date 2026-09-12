"""INF-04 (Lote B — Studio) — the local-inference benchmark screen.

Two independent checks:

1. `studio/checks/bench.check.mjs`, which drives the real
   `studio/src/adapters/bench.ts` (`estimateLabel`, `formatDelta`,
   `verdictTone`, `canPromote`, `isRunInFlight`) through esbuild rather than
   re-implementing their logic in Python — same pattern
   `tests/test_inf03_timeline_js.py`/`tests/test_inf01_serve_js.py` use.

2. A static source scan of `studio/src/screens/cookbook/Optimize.tsx` for
   CONTRATO_INF04.md's one non-negotiable UI rule (§01/§09): opening this
   screen, or a remount/reconnect once a run exists, must NEVER start a
   benchmark on its own — only an explicit click on "Start benchmark" does.
   Concretely: `startBench(` (the adapter's own launch call, see
   `adapters/bench.ts`'s module doc comment) must never appear inside a
   `useEffect(...)` call. This is a plain regex/bracket-matching scan, the
   same technique `tests/test_studio_guards.py` already uses for Studio-wide
   guards (kept in Python, not in the `.mjs` check, because it needs no
   esbuild/JSX parsing — just balanced-paren text scanning over the already
   pure TypeScript source).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
OPTIMIZE = ROOT / "studio" / "src" / "screens" / "cookbook" / "Optimize.tsx"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (ROOT / "node_modules" / "esbuild" / "lib" / "main.js").exists()
_SKIP = pytest.mark.skipif(not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed")


@_SKIP
def test_bench_helpers_js():
    result = subprocess.run(
        ["node", "studio/checks/bench.check.mjs"], cwd=ROOT,
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


def test_optimize_screen_exists():
    assert OPTIMIZE.exists(), "studio/src/screens/cookbook/Optimize.tsx is missing"


def test_optimize_never_starts_from_useeffect():
    text = OPTIMIZE.read_text(encoding="utf-8")

    effect_bodies = _call_bodies(text, "useEffect")
    assert effect_bodies, (
        "Optimize.tsx has no useEffect at all -- if that's now genuinely true, "
        "this guard has nothing left to check and should be revisited, not silently passed"
    )
    for body in effect_bodies:
        assert "startBench(" not in body, (
            "Optimize.tsx must never call startBench() from inside a useEffect -- "
            "mounting the screen or reconnecting must only read GET /runs/{id} "
            "(T19), never relaunch a benchmark:\n" + body
        )

    # The other half of the same rule, made concrete: the feature must
    # actually be wired somewhere OUTSIDE any effect (the Start benchmark
    # button's own onClick) -- otherwise this guard would trivially pass by
    # never calling startBench at all.
    assert "startBench(" in text, (
        "Optimize.tsx never calls startBench() anywhere -- the Start benchmark "
        "button (bench-start) must call it explicitly"
    )
