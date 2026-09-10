"""QA-25 · KV desconocida (docs/spec/v2/acceptance_scenarios.json).

Estimulo: modelo nuevo sin medicion con contexto largo.
Resultado exigido (literal): "Tamano de pesos como minimo y KV desconocida
visible; no declarar encaje seguro."

Requisitos: HW-01.

Estado: verde. `src/vram_fit.py::kv_bytes_per_token_estimated` devuelve
`(None, "no ...")` cuando falta metadata GGUF suficiente, en vez de inventar
un numero, y `plan()` NUNCA marca `fits: True` cuando `kv_bytes_per_token`
es None/0: cae directo a la rama "no cabe entero", que reporta los pesos
como base y deja `kv_bytes_per_token`/`kv_source` visibles como
desconocidos en el resultado.
"""
import pytest

from src.vram_fit import kv_bytes_per_token_estimated, plan

pytestmark = pytest.mark.qa_state("green")


def test_missing_gguf_metadata_never_invents_a_kv_estimate():
    per_token, note = kv_bytes_per_token_estimated({"general.architecture": "llama"})
    assert per_token is None
    assert "metadata" in note


def test_plan_never_declares_a_safe_fit_when_kv_is_unknown():
    result = plan(
        vram_total_bytes=24 * 1024 * 1024 * 1024,
        file_size_bytes=20 * 1024 * 1024 * 1024,  # weights alone: the minimum size
        n_layers=48,
        kv_bytes_per_token=None,
        kv_source="unknown",
        target_ctx=131072,  # "contexto largo"
    )
    assert result["fits"] is False
    assert result["kv_bytes_per_token"] is None
    assert result["kv_source"] == "unknown"
    # The weight size is still visible as the base of the estimate.
    assert result["file_size_bytes"] == 20 * 1024 * 1024 * 1024
    assert result["estimated_vram_bytes"] > 0
