"""L29 (integrates L28's "necessary in adapters/chat.ts" note): `decode()`
forwards `error_class`/`trace_id`/`step_id` onto the `error`/`terminal`
ChatEvent as `errorClass`/`traceId`/`stepId` — the OBS-01/OBS-03/ARCH-01
fields the server already puts on the wire (`agent_runs.py`'s
`_observability_fields`, `llm_core.py`'s `_stream_error_chunk`) — additively:
an event missing a field decodes exactly as it did before this lote.

Runs studio/checks/l29-error-trace-decode.check.mjs, which drives the real
`decode()` (not a re-implementation of its logic) through esbuild, mirroring
tests/test_studio_ask_user_options_js.py's own pattern for this file.
"""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parent.parent
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (ROOT / "node_modules" / "esbuild" / "lib" / "main.js").exists()


@pytest.mark.skipif(not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed")
def test_decode_forwards_error_trace_fields():
    result = subprocess.run(
        ["node", "studio/checks/l29-error-trace-decode.check.mjs"], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok l29-error-trace-decode" in result.stdout


def test_chat_event_declares_the_four_fields_as_optional():
    """Optional, not required: an event from a server that predates this
    lote must still type-check as a ChatEvent."""
    source = (ROOT / "studio/src/adapters/chat.ts").read_text(encoding="utf-8")
    for field in ("errorClass?: string", "traceId?: string", "stepId?: string", "versionMismatch?: boolean"):
        assert field in source, f"ChatEvent is missing `{field}`"
