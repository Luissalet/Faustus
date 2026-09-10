"""Lote 49b (P1) - BENCH-06: `Preview.tsx` itself carries the sandbox QA-32
already requires of every other HTML preview surface in the app.

Same source-reading approach as tests/qa/test_qa_32_preview_malicioso.py
(this checks the real deployed file, not a copy, so deleting the sandbox
attribute fails this test) — extended here to the one HTML preview surface
QA-32's own parametrize list does not cover: the workbench's artifact/
prototype canvas, which did not exist before this lot.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PREVIEW_TSX = (REPO_ROOT / "studio" / "src" / "components" / "Preview.tsx").read_text(encoding="utf-8")
SANDBOX_TS = (REPO_ROOT / "studio" / "src" / "components" / "previewSandbox.ts").read_text(encoding="utf-8")


def test_preview_renders_inside_a_sandboxed_iframe():
    assert "<iframe" in PREVIEW_TSX
    assert re.search(r"sandbox=\{sandboxAttr\(", PREVIEW_TSX), "sandbox must come from the policy function, not a literal"


def test_allow_same_origin_is_never_a_sandbox_token():
    """The one QA-32 rule: never allow-same-origin, with or without scripts.
    Reads the actual `sandboxAttr` function body (not prose/comments, which
    are free to name the token when explaining why it is absent)."""
    body = re.search(r"export function sandboxAttr\([^)]*\)[^{]*\{(.*?)\n\}", SANDBOX_TS, re.DOTALL)
    assert body, "sandboxAttr function not found"
    assert "allow-same-origin" not in body.group(1)


def test_scripts_are_opt_in_only():
    assert "allowScripts" in PREVIEW_TSX
    assert "allowScripts = false" in PREVIEW_TSX, "default prop must be false"


def test_csp_forbids_unsafe_eval_and_network_by_default():
    assert "unsafe-eval" not in SANDBOX_TS
    assert "connect-src 'none'" in SANDBOX_TS
