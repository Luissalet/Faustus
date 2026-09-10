"""Lote 11 — every acceptance scenario has a permanent regression, and no one
can quietly drop one.

This is the census, not a re-run of the scenarios themselves: it checks that
every ID in docs/spec/v2/acceptance_scenarios.json has exactly one
tests/qa/test_qa_NN_<slug>.py file, that the file declares its status via the
`qa_state` marker (green/xfail/manual — see tests/qa/conftest.py), and that
docs/spec/v2/QA_ESTADO.md stays honest about those same states. A scenario
whose test file exists but forgot the marker, or whose file went missing, or
whose status drifted from the doc, fails here loudly instead of just being
absent from a pytest run nobody read closely.
"""
from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_PATH = REPO_ROOT / "docs" / "spec" / "v2" / "acceptance_scenarios.json"
QA_DIR = Path(__file__).resolve().parent / "qa"
STATUS_DOC = REPO_ROOT / "docs" / "spec" / "v2" / "QA_ESTADO.md"

VALID_STATES = ("green", "xfail", "manual")


def _scenario_ids() -> list[str]:
    data = json.loads(SCENARIOS_PATH.read_text(encoding="utf-8"))
    return [s["id"] for s in data["scenarios"]]


def _qa_test_files() -> dict[str, Path]:
    """Map QA-NN -> its tests/qa/test_qa_NN_<slug>.py file, if exactly one exists."""
    by_id: dict[str, list[Path]] = {}
    for path in sorted(QA_DIR.glob("test_qa_*.py")):
        m = re.match(r"test_qa_(\d\d)_", path.name)
        if not m:
            continue
        qa_id = f"QA-{m.group(1)}"
        by_id.setdefault(qa_id, []).append(path)
    return {qa_id: paths[0] for qa_id, paths in by_id.items() if len(paths) == 1}, by_id


def _module_for(path: Path):
    rel = path.relative_to(REPO_ROOT).with_suffix("")
    module_name = ".".join(rel.parts)
    return importlib.import_module(module_name)


def _declared_state(module) -> str | None:
    """Read the module-level ``pytestmark = pytest.mark.qa_state("...")``."""
    marks = getattr(module, "pytestmark", None)
    if marks is None:
        return None
    marks = marks if isinstance(marks, (list, tuple)) else [marks]
    for mark in marks:
        if getattr(mark, "name", None) == "qa_state" and mark.args:
            return mark.args[0]
    return None


def test_every_acceptance_scenario_has_exactly_one_qa_test_file():
    ids = _scenario_ids()
    assert len(ids) == 48, f"expected 48 scenarios in acceptance_scenarios.json, found {len(ids)}"

    resolved, all_matches = _qa_test_files()
    missing = [qa_id for qa_id in ids if qa_id not in resolved]
    assert not missing, f"no tests/qa/test_qa_*.py file for: {missing}"

    duplicated = {qa_id: [p.name for p in paths] for qa_id, paths in all_matches.items()
                  if len(paths) > 1}
    assert not duplicated, f"more than one test file claims the same QA id: {duplicated}"

    extra = [qa_id for qa_id in resolved if qa_id not in ids]
    assert not extra, f"test file(s) for an id not in acceptance_scenarios.json: {extra}"


@pytest.mark.parametrize("qa_id", _scenario_ids())
def test_scenario_test_file_declares_a_valid_qa_state(qa_id):
    resolved, _ = _qa_test_files()
    path = resolved.get(qa_id)
    assert path is not None, f"missing tests/qa file for {qa_id}"
    module = _module_for(path)
    state = _declared_state(module)
    assert state is not None, f"{path.name} has no pytestmark = pytest.mark.qa_state(...)"
    assert state in VALID_STATES, f"{path.name} declares an unknown qa_state {state!r}"


def test_qa_estado_doc_lists_every_scenario_with_its_declared_state():
    assert STATUS_DOC.exists(), "docs/spec/v2/QA_ESTADO.md is missing"
    doc_text = STATUS_DOC.read_text(encoding="utf-8")
    resolved, _ = _qa_test_files()

    mismatches = []
    for qa_id in _scenario_ids():
        path = resolved[qa_id]
        module = _module_for(path)
        state = _declared_state(module)
        # The doc table's status column uses a human label per state; both the
        # id and its file name must appear on the same row so the two never
        # drift apart silently.
        row = next((line for line in doc_text.splitlines() if line.startswith(f"| {qa_id} ")), None)
        if row is None:
            mismatches.append(f"{qa_id}: no row in QA_ESTADO.md")
            continue
        if path.name not in row:
            mismatches.append(f"{qa_id}: row does not name {path.name}")
            continue
        label = {"green": "verde", "xfail": "xfail", "manual": "manual"}[state]
        if label not in row.lower():
            mismatches.append(f"{qa_id}: row does not say '{label}' (declared state is {state})")
    assert not mismatches, "QA_ESTADO.md drifted from the qa_state markers:\n" + "\n".join(mismatches)
