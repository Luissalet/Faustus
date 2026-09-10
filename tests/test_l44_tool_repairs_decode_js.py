"""Lote 44 (closes an L39 gap): `studio/src/adapters/chat.ts`'s `decode()`
forwards a `tool_output` event's wire `argument_errors`/`repairs` onto the
`ChatEvent` as `argumentErrors`/`repairs` — the "fichero ajeno" change
`studio/checks/l39-tool-repairs.check.mjs`'s own comments named as still
needed for the live path (history restore already worked off the persisted
event directly).

Runs studio/checks/l44-tool-repairs-decode.check.mjs, which drives the real
`decode()` AND the real `model.ts` `apply()`/`blankTurn` together (bundled
with esbuild, neither re-implemented), mirroring
tests/test_l29_studio_error_trace_decode_js.py's own pattern for this file.

Revert proof (COMUN.md rule 5): with the `argumentErrors`/`repairs` lines
this lote added to `decode()`'s `tool_output` case removed (`cp`-backed,
never git), the check script's first block and its end-to-end block both
fail — `ev.argumentErrors`/`ev.repairs` come back `undefined` and the applied
step never gets a repair.
"""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parent.parent
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (ROOT / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed")


def test_decode_forwards_argument_errors_and_repairs_end_to_end():
    result = subprocess.run(
        ["node", "studio/checks/l44-tool-repairs-decode.check.mjs"], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ALL OK" in result.stdout
    assert "FAIL" not in result.stdout
