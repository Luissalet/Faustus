"""Lote 65 — Studio: turno de chat (Composer/Transcript/Studio.tsx/model.ts/
chat.ts). Runs the lote's `studio/checks/l65-*.check.mjs` scripts, which
drive the real TypeScript through esbuild rather than re-implementing its
logic in Python — same pattern `tests/test_l29_studio_error_trace_decode_js.py`
and `tests/test_l64_res01_coverage_map.py` already use for this file pair.

Each check demonstrates the closed gap:
  - `l65-chat-model-events`: ask_user `revision` decodes and round-trips;
    the `uncertain` SSE event (UX-02/TASK-03) and `capabilities_changed`
    (MOD-06) decode and mark the turn; research `coverage` (RES-01) and a
    tool_output's `execution_target` (EXEC-01) decode onto the turn/step.
  - `l65-transcript-helpers`: `maskSecrets` (EXEC-02) and
    `reportPartsProgress` (RES-04), Transcript.tsx's own pure helpers.
  - `l65-source-wiring`: the QA-44/SEC-03/ask_user-revision-send/TASK-06/
    UX-06 closures that need real source wiring rather than a runtime call.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parent.parent
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (ROOT / "node_modules" / "esbuild" / "lib" / "main.js").exists()
_SKIP = pytest.mark.skipif(not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed")


def _run(check_name: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["node", f"studio/checks/{check_name}.check.mjs"], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", timeout=90,
    )


@_SKIP
def test_l65_chat_model_events():
    result = _run("l65-chat-model-events")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok l65-chat-model-events" in result.stdout


@_SKIP
def test_l65_transcript_helpers():
    result = _run("l65-transcript-helpers")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok l65-transcript-helpers" in result.stdout


@_SKIP
def test_l65_source_wiring():
    result = _run("l65-source-wiring")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok l65-source-wiring" in result.stdout
