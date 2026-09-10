"""Lote 39 / CALL-03: the tool card's "original -> correccion". Proves that
`restoreFromMetadata`/`apply` (studio/src/screens/studio/model.ts) surface a
tool call's `argument_errors`/`repairs` (as `_validate_native_tool_call`,
src/agent_loop.py, attaches them to the persisted `tool_events[i]` entry and
to the live `tool_output` payload) onto `Step.argumentErrors`/`Step.repairs`
-- on history restore today, and already wired for `apply()`'s live path the
day `adapters/chat.ts`'s `decode()` forwards the fields onto the
`ChatEvent` (see this lote's report for that fichero-ajeno change) -- and
that a record from before CALL-03, carrying neither field, restores exactly
as it always did.

Runs studio/checks/l39-tool-repairs.check.mjs, which drives the real
module (not a re-implementation) through esbuild, mirroring
tests/test_l29_studio_error_trace_decode_js.py's own pattern for this file.
"""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parent.parent
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (ROOT / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed")


def test_tool_repairs_restore_and_apply():
    result = subprocess.run(
        ["node", "studio/checks/l39-tool-repairs.check.mjs"], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ALL OK" in result.stdout
    assert "FAIL" not in result.stdout


def test_step_declares_the_two_new_fields_as_optional():
    """Optional, not required: a Step from before CALL-03 (no repairs, no
    argument_errors) must still type-check."""
    source = (ROOT / "studio/src/screens/studio/model.ts").read_text(encoding="utf-8")
    assert "argumentErrors?:" in source
    assert "repairs?:" in source
