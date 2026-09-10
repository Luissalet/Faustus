"""QA-21 · Citas vacias (docs/spec/v2/acceptance_scenarios.json).

Estimulo: insertar marcadores que resuelven a fuente que no apoya la frase.
Resultado exigido (literal): "Referencia accesible pero apoyo dudoso; no
sello de informe verificado."

Requisitos: VER-05, RES-06.

Estado: verde. `src/claim_verify.py::verify` (VER-05, existente) juzga apoyo
semantico, no solo la existencia del marcador: una frase con una cifra o un
nombre que no aparece en la fuente citada se marca `supported=False` (capa
4, "the source does not contain figure/name ..."), aunque la fuente en si
sea perfectamente accesible. Este test cita una fuente real (accesible) que
no respalda la afirmacion (una cifra inventada) y comprueba que el veredicto
es apoyo dudoso, no un sello de verificado.
"""
import pytest

from src.claim_verify import verify

pytestmark = pytest.mark.qa_state("green")


def test_an_accessible_source_that_does_not_support_the_claim_is_flagged():
    # The marker resolves to a real, accessible source...
    source = (
        "The 2025 annual report shows revenue grew 8% year over year, "
        "driven mainly by the European market."
    )
    # ...but the cited sentence invents a figure the source never states.
    claim = "Revenue grew 47% year over year according to the 2025 annual report."

    result = verify(claim, source)

    assert result["supported"] is False
    assert result["layer"] == 4  # settled by the missing-figure check
    assert "47" in " ".join(result["unsupported_terms"])
    # This is explicitly NOT a stamp of a verified report.
    assert "not" in result["why"].lower() or "no" in result["why"].lower()
