"""Lote T1 — every acceptance-parity case (A01-A36) is accounted
for, and no one can quietly flip a case's declared state without also
wiring (or removing) the test behind it.

This mirrors tests/test_qa_index.py's approach for docs/spec/v2/QA_ESTADO.md,
adapted to a different shape: unlike the 48 QA scenarios (one dedicated file
each, always), most of the 36 acceptance cases have NO test yet ("pendiente"
is the default and expected state for this lot), and the one case that does
have a test (A03) lives inside a pre-existing file
(tests/test_tool_approvals.py) alongside many unrelated tests, not in its
own tests/acceptance/test_a03_*.py module. So instead of requiring one file
per id, this index walks every test_*.py under tests/ (recursively,
including tests/acceptance/), finds every test function/method carrying
``@pytest.mark.acceptance("A0N")``, and cross-checks that against
docs/spec/paridad/ESTADO_ACEPTACION.md:

  - "pendiente": no test anywhere may carry that case's marker yet (once one
    does, the row must be flipped to verde/xfail in the same change).
  - "verde" / "xfail": at least one test must carry the marker, and every
    file that does must be named in that case's doc row; "xfail" also
    requires at least one of those tests to carry ``@pytest.mark.xfail``.

This is the census, not a re-run of the acceptance suite itself — running
the suite and producing per-case JSONL evidence is scripts/acceptance_run.py's
job (see tests/test_acceptance_run.py for that script's own test).
"""
from __future__ import annotations

import importlib
import inspect
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
CASES_PATH = REPO_ROOT / "docs" / "spec" / "paridad" / "acceptance_cases.json"
STATUS_DOC = REPO_ROOT / "docs" / "spec" / "paridad" / "ESTADO_ACEPTACION.md"
PYPROJECT = REPO_ROOT / "pyproject.toml"

VALID_STATES = ("verde", "xfail", "pendiente")

# Files this index must not try to import as ordinary test modules: pytest
# itself would collect them the normal way, but a plain importlib.import_module
# here can trip over fixtures-only conftest.py files or packages with no
# test_ functions. Only test_*.py files are walked (see _iter_test_functions),
# so this is already narrow; nothing to exclude today.


def _case_ids() -> list[str]:
    data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    return [c["id"] for c in data["cases"]]


def _module_name_for(path: Path) -> str:
    rel = path.relative_to(REPO_ROOT).with_suffix("")
    return ".".join(rel.parts)


def _iter_test_functions():
    """Yield (path, qualified_name, function) for every test_*.py under tests/."""
    for path in sorted(TESTS_DIR.rglob("test_*.py")):
        try:
            module = importlib.import_module(_module_name_for(path))
        except Exception:
            # Import failures unrelated to this index (optional deps, etc.)
            # are out of scope here; ordinary pytest collection is where a
            # genuinely broken test module surfaces.
            continue
        for name, obj in vars(module).items():
            if name.startswith("test_") and inspect.isfunction(obj):
                yield path, name, obj
            elif inspect.isclass(obj) and name.startswith("Test"):
                for mname, mobj in vars(obj).items():
                    if mname.startswith("test_") and inspect.isfunction(mobj):
                        yield path, f"{name}.{mname}", mobj


def _acceptance_case_ids(fn) -> list[str]:
    marks = getattr(fn, "pytestmark", None) or []
    return [m.args[0] for m in marks if getattr(m, "name", None) == "acceptance" and m.args]


def _has_xfail(fn) -> bool:
    marks = getattr(fn, "pytestmark", None) or []
    return any(getattr(m, "name", None) == "xfail" for m in marks)


def _collect() -> dict[str, list[tuple[Path, str, object]]]:
    """case_id -> [(file, qualified_test_name, function), ...]."""
    by_case: dict[str, list[tuple[Path, str, object]]] = {}
    for path, qname, fn in _iter_test_functions():
        for case_id in _acceptance_case_ids(fn):
            by_case.setdefault(case_id, []).append((path, qname, fn))
    return by_case


_ROW_RE = re.compile(r"^\|\s*(A\d\d)\s*\|[^|]*\|\s*([A-Za-zÀ-ÿ]+)\s*\|")


def _doc_rows() -> dict[str, tuple[str, str]]:
    """case_id -> (state, full row text) parsed from ESTADO_ACEPTACION.md."""
    text = STATUS_DOC.read_text(encoding="utf-8")
    rows: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        m = _ROW_RE.match(line)
        if m:
            rows[m.group(1)] = (m.group(2).lower(), line)
    return rows


def test_marker_is_registered_in_pyproject():
    text = PYPROJECT.read_text(encoding="utf-8")
    assert "acceptance(case_id)" in text, (
        "pyproject.toml must register the 'acceptance(case_id)' marker under "
        "[tool.pytest.ini_options].markers, or every @pytest.mark.acceptance(...) "
        "use raises PytestUnknownMarkWarning"
    )


def test_every_case_id_used_in_code_is_a_real_case():
    ids = set(_case_ids())
    by_case = _collect()
    unknown = sorted(set(by_case) - ids)
    assert not unknown, (
        f"test(s) carry @pytest.mark.acceptance(...) for unknown case id(s): {unknown} "
        f"(typo, or docs/spec/paridad/acceptance_cases.json is stale)"
    )


def test_acceptance_estado_doc_lists_every_case_with_a_valid_state():
    assert STATUS_DOC.exists(), "docs/spec/paridad/ESTADO_ACEPTACION.md is missing"
    ids = _case_ids()
    assert len(ids) == 36, f"expected 36 acceptance cases in acceptance_cases.json, found {len(ids)}"
    rows = _doc_rows()
    missing = [c for c in ids if c not in rows]
    assert not missing, f"ESTADO_ACEPTACION.md has no row for: {missing}"
    invalid = {c: state for c, (state, _line) in rows.items() if c in ids and state not in VALID_STATES}
    assert not invalid, f"ESTADO_ACEPTACION.md declares an unknown state: {invalid}"


@pytest.mark.parametrize("case_id", _case_ids())
def test_case_state_matches_the_real_markers(case_id):
    rows = _doc_rows()
    by_case = _collect()
    state, row = rows.get(case_id, (None, ""))
    assert state is not None, f"no ESTADO_ACEPTACION.md row for {case_id}"
    found = by_case.get(case_id, [])

    if state == "pendiente":
        assert not found, (
            f"{case_id} is declared 'pendiente' in ESTADO_ACEPTACION.md but "
            f"{[f'{p.name}::{q}' for p, q, _fn in found]} already carries "
            f"@pytest.mark.acceptance('{case_id}') — flip the row to verde/xfail"
        )
        return

    assert found, (
        f"{case_id} is declared {state!r} in ESTADO_ACEPTACION.md but no test "
        f"carries @pytest.mark.acceptance('{case_id}')"
    )
    for path, qname, _fn in found:
        assert path.name in row, f"{case_id}: row does not name {path.name} (test {qname})"
    if state == "xfail":
        assert any(_has_xfail(fn) for _p, _q, fn in found), (
            f"{case_id} declared xfail but none of {[q for _p, q, _f in found]} "
            f"carries @pytest.mark.xfail"
        )
