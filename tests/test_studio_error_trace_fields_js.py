"""Lote 28 - typed trace/error fields on Turn, consumed with fallback.

`Turn.errorClass`/`traceId`/`stepId`/`versionMismatch` (model.ts) are read
defensively off the raw `error`/`terminal` event via `errorTraceFields()`,
since `adapters/chat.ts`'s `ChatEvent` union does not declare them yet (a
different lote owns that file). Runs studio/checks/error-trace-fields.check.mjs,
which proves both halves: absent today (a client on an older build, or
before `decode()` forwards the fields) behaves exactly as before this lote,
and present (once forwarded) actually populates the turn.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECK = REPO_ROOT / "studio" / "checks" / "error-trace-fields.check.mjs"
MODEL_TS = (REPO_ROOT / "studio" / "src" / "screens" / "studio" / "model.ts").read_text(encoding="utf-8")
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (REPO_ROOT / "node_modules" / "esbuild" / "lib" / "main.js").exists()


@pytest.mark.skipif(not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed")
def test_error_trace_fields_check_passes():
    proc = subprocess.run(
        ["node", str(CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(REPO_ROOT), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout


def test_turn_declares_the_four_fields_as_optional():
    """Optional, not required: a turn built before this lote (or restored
    from history with no such metadata) must still type-check as a Turn."""
    for field in ("errorClass?: string", "traceId?: string", "stepId?: string", "versionMismatch?: boolean"):
        assert field in MODEL_TS, f"Turn is missing `{field}`"


def test_error_and_terminal_both_read_the_same_helper():
    """Both failure paths must go through one reader, not two independent
    (and driftable) copies of the same field list."""
    assert MODEL_TS.count("errorTraceFields(event)") == 2, (
        "expected exactly the 'error' and 'terminal' cases to call errorTraceFields()"
    )
