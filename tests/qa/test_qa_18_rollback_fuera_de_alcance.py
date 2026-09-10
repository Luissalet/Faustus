"""QA-18 · Rollback fuera de alcance (docs/spec/v2/acceptance_scenarios.json).

Estimulo: entre checkpoint y restore se envia correo y usuario edita otro
archivo.
Resultado exigido (literal): "Explica lo irreversible y conflicto ajeno; no
anuncia reversion universal."

Requisitos: EDIT-04.

Estado: GREEN (lote 20, integrando el trabajo real del lote 19).
`src/workspace_checkpoints.py::checkpoint_coverage(workspace, sha,
external_effects=...)` reporta exactamente lo que un `restore()` a ese
checkpoint SI cubre (`covered`), lo que queda fuera (`uncovered`: rutas
excluidas/binarias que el shadow repo nunca capturo, con su razon) y los
efectos externos irreversibles que el llamador declare (p.ej. un correo ya
enviado). `restore()` ahora adjunta ese informe bajo `coverage` ademas de sus
claves de siempre (`restored`, `deleted`, `failed`, `unchanged` — nada se
quito, compatibilidad intacta) — asi un restore explica el conflicto ajeno
(el `node_modules/` que nunca estuvo en el checkpoint) y lo irreversible (el
correo ya enviado) en vez de anunciar una reversion universal que nunca
existio. `workspace_checkpoints.status()` en si (que reporta el estado
GENERAL de los checkpoints, no el alcance de un restore concreto) sigue sin
tocar — el gap real que QA-18 describe estaba en el RESULTADO DE UN RESTORE,
no en `status()`; ver tests/test_edit_checkpoint_coverage.py, cuya suite
completa (incluida la prueba anti-regresion de que el shape antiguo de
`restore()` no tenia `coverage`) es la cobertura real de EDIT-04. Este test
reutiliza ese mismo escenario end-to-end.
"""
import os

import pytest

from src import workspace_checkpoints as wc

pytestmark = pytest.mark.qa_state("green")
pytestmark = [pytestmark, pytest.mark.skipif(not wc.git_available(), reason="git not available")]


def test_restore_explains_out_of_scope_instead_of_a_universal_reversion(tmp_path):
    """QA-18's literal stimulus (the "conflicto ajeno" half): something
    outside the checkpoint's coverage exists (a vendored dir the shadow repo
    never snapshots). `restore()` must name it instead of reading as a
    plain, unqualified "reverted"."""
    (tmp_path / "a.txt").write_text("hello\n")
    os.makedirs(tmp_path / "node_modules")
    (tmp_path / "node_modules" / "x.js").write_text("vendored, never checkpointed")
    cp = wc.checkpoint(str(tmp_path), "initial")
    assert cp is not None

    (tmp_path / "a.txt").write_text("edited after checkpoint\n")
    result = wc.restore(str(tmp_path), cp["sha"])

    # "no anuncia reversion universal": the restore's own historical keys
    # are still exactly what they were (compatibility, EDIT-04 adds to this
    # shape rather than replacing it) —
    assert result["restored"] == ["a.txt"]
    # — but a caller now has what it needs to explain the conflicto ajeno:
    # node_modules/ was never in the checkpoint's scope to begin with.
    assert "coverage" in result
    uncovered_reasons = {u["path"]: u["reason"] for u in result["coverage"]["uncovered"]}
    assert uncovered_reasons.get("node_modules/") == "excluded_dir_never_snapshotted"


def test_checkpoint_coverage_names_an_irreversible_external_effect(tmp_path):
    """QA-18's literal stimulus (the "irreversible" half): between checkpoint
    and restore, an email was sent — no filesystem restore undoes that.
    `checkpoint_coverage(..., external_effects=...)` is how a caller
    (src/dispatch.py, or a restore-confirmation flow) attaches it to the same
    report `restore()` returns under `coverage`, instead of restore silently
    implying everything was put back."""
    (tmp_path / "a.txt").write_text("hello\n")
    cp = wc.checkpoint(str(tmp_path), "initial")
    assert cp is not None

    external_effects = [{
        "kind": "email_sent",
        "detail": "invoice reminder already sent to client@example.com",
    }]
    coverage = wc.checkpoint_coverage(str(tmp_path), cp["sha"], external_effects=external_effects)

    assert coverage["external_effects"] == external_effects
