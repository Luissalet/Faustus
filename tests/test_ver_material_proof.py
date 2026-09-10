"""VER-04 — material_proof: a "done" criterion needs material evidence, not
just a green exit code (QA-19 at the single-criterion level)."""
from src.contracts.task import AcceptanceCriterion
from src import verification as ver


def _evidence(**paths):
    return {
        "source": "checkpoint", "checkpoint": "deadbeef",
        "added": paths.get("added", []), "modified": paths.get("modified", []),
        "deleted": paths.get("deleted", []),
    }


def _passing_verification():
    return {"mode": "pytest", "ran": True, "ok": True, "summary": "48 passed"}


def test_criterion_with_no_evidence_refs_is_missing_even_with_green_tests():
    # QA-19: "tests en verde pero un requisito sin evidencia -> parcial, no completo".
    c = AcceptanceCriterion(id="c1", description="the export button produces a file")
    task = {"acceptance_criteria": [c], "evidence": _evidence(modified=["app.py"]),
            "verification": _passing_verification()}

    result = ver.material_proof(task)

    assert result["ok"] is False
    assert result["missing"] == ["c1"]


def test_criterion_with_evidence_ref_matching_a_changed_path_is_proved():
    c = AcceptanceCriterion(id="c1", description="x", evidence_refs=("src/export.py",))
    task = {"acceptance_criteria": [c], "evidence": _evidence(modified=["src/export.py"]),
            "verification": _passing_verification()}

    result = ver.material_proof(task)

    assert result["ok"] is True
    assert result["missing"] == []
    assert result["proofs"]["c1"]["verdict"] == "proved"


def test_criterion_with_evidence_ref_not_actually_on_disk_is_missing():
    c = AcceptanceCriterion(id="c1", description="x", evidence_refs=("src/does_not_exist.py",))
    task = {"acceptance_criteria": [c], "evidence": _evidence(modified=["src/export.py"]),
            "verification": _passing_verification()}

    result = ver.material_proof(task)

    assert result["ok"] is False
    assert "c1" in result["missing"]


def test_mixed_criteria_only_the_unproved_one_is_missing():
    proved = AcceptanceCriterion(id="proved", description="x", evidence_refs=("src/export.py",))
    unproved = AcceptanceCriterion(id="unproved", description="y")
    task = {"acceptance_criteria": [proved, unproved], "evidence": _evidence(modified=["src/export.py"]),
            "verification": _passing_verification()}

    result = ver.material_proof(task)

    assert result["missing"] == ["unproved"]
    assert result["ok"] is False


def test_no_criteria_at_all_is_never_ok():
    result = ver.material_proof({"acceptance_criteria": [], "evidence": _evidence(), "verification": {}})
    assert result["ok"] is False
    assert result["missing"] == []
