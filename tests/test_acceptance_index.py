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

import ast
import functools
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

# The walk is a static (ast) read, never an import: importing every
# tests/test_*.py in-process from *this* test ran their module-level side
# effects a second time under a different module name (``tests.test_x`` vs
# pytest's ``test_x``) — e.g. tests/test_caldav_writeback_route.py rebinds
# ``routes.calendar_routes.SessionLocal`` to a temp DB at import, so the
# second import pointed the route at a database the first module never
# wrote to, and its tests failed whenever this file happened to run first
# on the same xdist worker. Reading decorators from the source has no such
# side effects and sees exactly the same markers.


def _case_ids() -> list[str]:
    data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    return [c["id"] for c in data["cases"]]


class _Marked:
    """What the index needs from a test function: its acceptance case ids
    and whether it is xfail-marked."""

    __slots__ = ("case_ids", "xfail")

    def __init__(self, case_ids, xfail):
        self.case_ids = list(case_ids)
        self.xfail = bool(xfail)


def _mark_name(dec) -> str | None:
    """'acceptance' for ``@pytest.mark.acceptance(...)``, 'xfail' for
    ``@pytest.mark.xfail`` / ``@pytest.mark.xfail(...)``, else None."""
    node = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute):
        if isinstance(node.value.value, ast.Name) and node.value.value.id == "pytest" and node.value.attr == "mark":
            return node.attr
    return None


def _marked(decorators) -> _Marked | None:
    case_ids, xfail = [], False
    for dec in decorators:
        name = _mark_name(dec)
        if name == "acceptance" and isinstance(dec, ast.Call) and dec.args:
            first = dec.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                case_ids.append(first.value)
        elif name == "xfail":
            xfail = True
    return _Marked(case_ids, xfail) if (case_ids or xfail) else None


def _iter_test_functions():
    """Yield (path, qualified_name, _Marked) for every marked test_* function
    or Test* method in every test_*.py under tests/ — from the source, no
    imports."""
    for path in sorted(TESTS_DIR.rglob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            # A file that does not parse fails ordinary collection loudly;
            # not this index's job.
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                marked = _marked(node.decorator_list)
                if marked:
                    yield path, node.name, marked
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name.startswith("test_"):
                        marked = _marked(sub.decorator_list)
                        if marked:
                            yield path, f"{node.name}.{sub.name}", marked


def _acceptance_case_ids(fn) -> list[str]:
    return list(getattr(fn, "case_ids", ()) or ())


def _has_xfail(fn) -> bool:
    return bool(getattr(fn, "xfail", False))


def _collect() -> dict[str, list[tuple[Path, str, object]]]:
    """case_id -> [(file, qualified_test_name, function), ...]."""
    return dict(_collect_cached())


@functools.lru_cache(maxsize=1)
def _collect_cached():
    """One source walk per process (~4 s over the whole tests/ tree); the
    36 parametrized state checks share it."""
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
