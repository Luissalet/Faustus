"""Lote 69b's Studio checks, run the same way `tests/test_l55_studio_checks_js.py`
runs its `.check.mjs` files:

* `studio/checks/l69b-edit01-conflict.check.mjs` — EDIT-01's `saveDoc` sends
  `expected_content`, a 409 surfaces as `ApiError(status: 409)` for the
  editor's conflict banner, and a forced save omits it.
* `studio/checks/l69b-edit03-diff.check.mjs` — EDIT-03's formatting-only
  chunk classifier (`normalizeForFormatCompare`/`isFormatOnlyChunk`) and
  binary/large-file detection (`looksBinary`/`DIFF_LARGE_FILE_CHARS`).
* `studio/checks/l69b-ver06-review.check.mjs` — VER-06's review adapter
  parses `services/review_state.py`'s wire shape, keeping human approval and
  automatic test results as two separate facts.
* `studio/checks/l69b-idx01-relocate-list.check.mjs` — IDX-01's "Reubicar"
  action from the Projects list (not just from inside a project) and the
  recent-folders adapter both it and Project.tsx's Browse fallback use.
* `studio/checks/l69b-mem01-rules.check.mjs` — MEM-01's sensitivity/
  confidence_state parsing and the forgetRule/deleteRule split.
* `studio/checks/l69b-obs01-trace.check.mjs` — OBS-01's `traceForCall`/
  `callTraceFrom`: wire-shape parsing and the session_id-optional URL.
* `studio/checks/l69b-ctx03-manifest-search.check.mjs` — CTX-03's
  `manifestItemMatches`: the client-side "search within this packet"
  filter over an already-loaded manifest's rows.
* `studio/checks/l69b-web02-duplicate-stale.check.mjs` — WEB-02's
  `sourceFrom` (studio/src/adapters/research.ts): parses duplicate_of/
  stale/age_days when a source carries them, stays silent when it doesn't.
* `studio/checks/l69b-res01-coverage.check.mjs` — RES-01's `CoverageMap`
  (studio/src/screens/research/Research.tsx) keeps every node of a long
  brief's schema, and RES-04's `phaseLabel` 'writing' case prefers the
  server's per-part message over the generic line.

Needs node and the repo's node_modules (esbuild bundles the TS) — skipped
otherwise, same guard as the existing JS checks.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECKS = [
    _REPO / "studio" / "checks" / "l69b-edit01-conflict.check.mjs",
    _REPO / "studio" / "checks" / "l69b-edit03-diff.check.mjs",
    _REPO / "studio" / "checks" / "l69b-ver06-review.check.mjs",
    _REPO / "studio" / "checks" / "l69b-idx01-relocate-list.check.mjs",
    _REPO / "studio" / "checks" / "l69b-mem01-rules.check.mjs",
    _REPO / "studio" / "checks" / "l69b-obs01-trace.check.mjs",
    _REPO / "studio" / "checks" / "l69b-ctx03-manifest-search.check.mjs",
    _REPO / "studio" / "checks" / "l69b-web02-duplicate-stale.check.mjs",
    _REPO / "studio" / "checks" / "l69b-res01-coverage.check.mjs",
]
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(
    not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed"
)


@pytest.mark.parametrize("check", _CHECKS, ids=lambda p: p.name)
def test_l69b_js_check_passes(check):
    proc = subprocess.run(
        ["node", str(check)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
    assert "FAIL" not in proc.stdout
