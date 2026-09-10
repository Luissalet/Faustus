"""Lote 28 - UX-08: `error_class` becomes a title and an action, not JSON.

`src/contracts/errors.py` (OBS-03) has sent `error_class` on every streaming
`event: error` chunk since CALL-06, but nothing on the Studio side read it —
a provider failure reached the screen as whatever string the backend raised,
sometimes the literal `_stream_error_chunk`/§34.5 payload. This runs
studio/checks/error-taxonomy.check.mjs, which checks `describeError`/
`friendlyError` (studio/src/components/errorTaxonomy.ts) against the ten
OBS-03 categories and proves a JSON-shaped failure reason renders as its
human message, not the blob.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECK = REPO_ROOT / "studio" / "checks" / "error-taxonomy.check.mjs"
ERRORS_PY = (REPO_ROOT / "src" / "contracts" / "errors.py").read_text(encoding="utf-8")
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (REPO_ROOT / "node_modules" / "esbuild" / "lib" / "main.js").exists()


@pytest.mark.skipif(not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed")
def test_error_taxonomy_check_passes():
    proc = subprocess.run(
        ["node", str(CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(REPO_ROOT), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout


def test_the_ten_categories_match_the_backend_taxonomy_verbatim():
    """The client-side table is a mirror, not a reinvention: every category
    name here must be a category `src/contracts/errors.py` actually has."""
    ts = (REPO_ROOT / "studio" / "src" / "components" / "errorTaxonomy.ts").read_text(encoding="utf-8")
    for category in (
        "capability", "schema", "permission", "resource", "transport",
        "timeout", "cancelled", "conflict", "verification", "unknown",
    ):
        assert f'"{category}"' in ERRORS_PY, f"{category} is not in errors.py's ERROR_CATEGORIES any more"
        assert f"{category}:" in ts, f"errorTaxonomy.ts is missing a mapping for {category}"


def test_only_transport_and_timeout_are_marked_retryable():
    """Mirrors `_DEFAULTS`: those are the only two categories the backend
    itself calls retryable; getting this wrong would offer "Retry" for a
    permission denial, which retrying can never fix."""
    ts = (REPO_ROOT / "studio" / "src" / "components" / "errorTaxonomy.ts").read_text(encoding="utf-8")
    body = ts[ts.index("CATEGORY_INFO"):]
    retryable_true = body.count("retryable: true")
    assert retryable_true == 2, f"expected exactly 2 retryable categories (transport, timeout), found {retryable_true}"
