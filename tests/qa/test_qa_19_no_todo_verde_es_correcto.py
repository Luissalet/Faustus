"""QA-19 · No todo verde es correcto (docs/spec/v2/acceptance_scenarios.json).

Estimulo: tests retornan 0 pero falta parte de los requisitos.
Resultado exigido (literal): "Criterio pendiente y estado parcial/no
verificado, no tarea completa."

Requisitos: VER-01, VER-04.

Estado: verde. `src/output_oracle.py::apply` (VER-04, existente) exige que
un exit 0 tambien contenga la cadena declarada del criterio pendiente; si no
la contiene, sobreescribe el exit code a `EXIT_OUTPUT_MISMATCH` en vez de
dejar pasar el 0 como si el criterio estuviera cubierto (ver
tests/test_output_oracle.py). Combinado con `src/scorecard.py` (VER-01,
existente: estados verified/unverified/rejected...), un comando que sale con
0 pero no demuestra el requisito declarado nunca se etiqueta verificado.
"""
import pytest

from src.output_oracle import EXIT_OUTPUT_MISMATCH, apply

pytestmark = pytest.mark.qa_state("green")


def test_a_zero_exit_code_missing_the_declared_criterion_is_overturned():
    # The command "succeeded" (exit 0) but the output never proves the part
    # of the requirement that was declared as the acceptance criterion.
    output = "collected 40 items\n38 passed, 2 skipped in 1.2s"
    declared = "48 passed"  # the literal criterion: all 48 scenarios covered

    code, matched = apply(exit_code=0, output=output, expected=declared)

    assert matched is False
    assert code == EXIT_OUTPUT_MISMATCH  # not a silent "0 == complete"
    assert code != 0


def test_when_the_criterion_is_actually_present_zero_stays_zero():
    output = "collected 48 items\n48 passed in 3.4s"
    code, matched = apply(exit_code=0, output=output, expected="48 passed")
    assert matched is True
    assert code == 0
