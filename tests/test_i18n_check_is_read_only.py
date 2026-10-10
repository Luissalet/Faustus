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


def _module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("i18n_es_under_test", ROOT / "scripts" / "i18n_es.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_generating_never_loses_a_row_that_only_the_catalogue_has(tmp_path, monkeypatch) -> None:
    """A string added straight to es.ts is copied into the table before the
    catalogue is regenerated, instead of being deleted by the regeneration."""
    mod = _module()
    tsv = tmp_path / "es.tsv"
    out = tmp_path / "es.ts"
    tsv.write_text("Save\tGuardar\n", encoding="utf-8")
    out.write_text('export const es = {\n  "Save": "Guardar",\n  "Two\\nlines": "Dos\\nlíneas",\n'
                   '  "Spent today": "Gastado hoy",\n};\n', encoding="utf-8")
    monkeypatch.setattr(mod, "TSV", str(tsv))
    monkeypatch.setattr(mod, "OUT", str(out))
    monkeypatch.setattr(sys, "argv", ["i18n_es.py"])
    assert mod.main() == 0
    table = tsv.read_text(encoding="utf-8")
    assert "Spent today\tGastado hoy\n" in table and "Two\\nlines\tDos\\nlíneas\n" in table
    generated = out.read_text(encoding="utf-8")
    assert '"Spent today": "Gastado hoy"' in generated and '"Save": "Guardar"' in generated
    # --prune is the deliberate way to drop a row
    tsv.write_text("Save\tGuardar\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["i18n_es.py", "--prune"])
    assert mod.main() == 0
    assert "Spent today" not in out.read_text(encoding="utf-8")
