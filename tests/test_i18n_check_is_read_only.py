"""`scripts/i18n_es.py --check` must never rewrite the generated catalogue.

It used to regenerate studio/src/i18n/es.ts from the table, which silently
deleted any string that lived only in the generated file.  The table is the
source; the check reports a mismatch, it does not "fix" it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENERATED = ROOT / "studio" / "src" / "i18n" / "es.ts"


def test_check_does_not_touch_the_generated_catalogue() -> None:
    before = GENERATED.read_bytes()
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "i18n_es.py"), "--check"],
        cwd=ROOT,
        capture_output=True,
    )
    assert GENERATED.read_bytes() == before, (
        "i18n_es.py --check rewrote studio/src/i18n/es.ts; --check must only report"
    )
