"""QA-07 · Schema peligroso (docs/spec/v2/acceptance_scenarios.json).

Estimulo: argumentos con tipo incorrecto, campo extra y ruta fuera de scope.
Resultado exigido (literal): "Error localizado y autorizacion independiente;
reparacion no elimina proteccion."

Requisitos: CALL-02, CALL-03, SEC-03.

Estado: verde. `src/tool_schemas.py::validate_tool_arguments` /
`repair_tool_arguments` ya cubren este caso literal (ver
tests/test_tool_args_validation.py, cuyo docstring cita "QA-07"). Este test
repite el escenario con una herramienta distinta para confirmar el
mecanismo de forma independiente: un tipo erroneo, un campo desconocido y
una ruta que escapa del scope en la misma llamada producen tres errores
localizados por campo, y la reparacion acotada arregla solo el tipo -
jamas silencia el error de ruta fuera de scope (la proteccion de SEC-03).
"""
import src.agent_tools  # noqa: F401  (breaks the tool_schemas<->agent_tools import cycle)
import pytest

from src.tool_schemas import repair_tool_arguments, validate_tool_arguments

pytestmark = pytest.mark.qa_state("green")


def test_three_localized_errors_and_repair_never_erases_the_path_error():
    args = {
        "source": "input.png",
        "path": "../../../etc/passwd",  # out-of-scope path
        "format": "png",
        "quality": "75",                # wrong type: schema wants int
        "unexpected_field": True,       # unknown field
    }
    errors = validate_tool_arguments("plan_media_transform", args)
    by_kind = {e.kind: e for e in errors}
    assert set(by_kind) == {"wrong_type", "unknown_field", "path_scope"}
    assert by_kind["path_scope"].field == "path"

    repaired, applied = repair_tool_arguments("plan_media_transform", args, errors)
    # Repair is bounded to the fixable type coercion only.
    assert {a["field"] for a in applied} == {"quality"}
    # The dangerous path is untouched byte-for-byte, so an independent
    # authorization check downstream still sees the escaping path.
    assert repaired["path"] == "../../../etc/passwd"

    remaining = validate_tool_arguments("plan_media_transform", repaired)
    remaining_kinds = {e.kind for e in remaining}
    assert "path_scope" in remaining_kinds
    assert "wrong_type" not in remaining_kinds
