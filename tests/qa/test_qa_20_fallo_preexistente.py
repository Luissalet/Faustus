"""QA-20 · Fallo preexistente (docs/spec/v2/acceptance_scenarios.json).

Estimulo: baseline contiene un test roto y el patch rompe otro nuevo.
Resultado exigido (literal): "Separa ambos; no culpa todo al patch ni
esconde regresion bajo fallo previo."

Requisitos: VER-02.

Estado: verde. `src/project_tests.py::compare_with_baseline` (VER-02,
existente) re-ejecuta los mismos ficheros de test contra el arbol del
checkpoint y separa `pre_existing` de `new_failures` por identidad de test.
Este test corre pytest DE VERDAD dos veces (arbol "antes" con un test ya
roto, arbol "despues" con ese mismo test roto MAS uno nuevo que rompe el
patch) y comprueba que la comparacion separa ambos correctamente, en vez de
culpar todo al patch o esconder la regresion nueva bajo el fallo previo.
`workspace_checkpoints.export_tree` se sustituye por una copia de carpeta
real (no exporta de un repo git de verdad) - lo unico simulado es de donde
sale el arbol "antes", no la logica de comparacion ni la ejecucion de tests.
"""
import shutil
import sys
import textwrap

import pytest

from src import project_tests
from src import workspace_checkpoints as wc

pytestmark = pytest.mark.qa_state("green")


BEFORE_TEST = textwrap.dedent("""
    def test_already_broken():
        assert 1 == 2  # broken before this turn too

    def test_stays_fine():
        assert 1 == 1
""")

AFTER_TEST = textwrap.dedent("""
    def test_already_broken():
        assert 1 == 2  # still broken - pre-existing

    def test_stays_fine():
        assert 1 == 1

    def test_the_patch_just_broke():
        assert False  # a genuine new regression introduced by this turn
""")


def test_baseline_comparison_separates_pre_existing_from_new_regressions(tmp_path, monkeypatch):
    current_ws = tmp_path / "current"
    checkpoint_src = tmp_path / "checkpoint_source"
    for d in (current_ws, checkpoint_src):
        (d / "tests").mkdir(parents=True)
        (d / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (current_ws / "tests" / "test_thing.py").write_text(AFTER_TEST, encoding="utf-8")
    (checkpoint_src / "tests" / "test_thing.py").write_text(BEFORE_TEST, encoding="utf-8")

    def _fake_export_tree(workspace, sha, dest):
        shutil.copytree(str(checkpoint_src), dest, dirs_exist_ok=True)
        return True

    monkeypatch.setattr(wc, "export_tree", _fake_export_tree)

    spec = {"kind": "pytest", "argv": [sys.executable, "-m", "pytest", "-q"]}
    res = project_tests.run_tests(str(current_ws), spec, test_files=["tests/test_thing.py"])
    assert res["ran"] and not res["ok"]
    assert len(res["failures"]) == 2  # both the old and the new failure

    result = project_tests.compare_with_baseline(
        str(current_ws), "fake_sha", spec, res, changed=["src/thing.py"],
    )

    assert any("test_already_broken" in f for f in result["pre_existing"])
    assert any("test_the_patch_just_broke" in f for f in result["new_failures"])
    assert not any("test_already_broken" in f for f in result["new_failures"])
    assert not any("test_the_patch_just_broke" in f for f in result["pre_existing"])
    assert result.get("pre_existing_only") is not True  # a real regression is NOT hidden
