"""Wiring tests for the shared-memory feature.

A perfect endpoint nobody calls is the quietest way to ship nothing, so each
half of this feature gets a test that it is actually plugged in: the collector
into `/api/system/usage`, the warning into the usage widget, the fit advisor
into the model-controls popover, and the card into the Cookbook hardware page.
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROUTES = (ROOT / "routes" / "system_usage_routes.py").read_text(encoding="utf-8")
SYS_USAGE_JS = (ROOT / "static" / "js" / "sysUsage.js").read_text(encoding="utf-8")
MODEL_CONTROLS_JS = (ROOT / "static" / "js" / "modelControls.js").read_text(encoding="utf-8")
HWFIT_JS = (ROOT / "static" / "js" / "cookbook-hwfit.js").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def test_collector_is_wired_into_the_usage_payload():
    assert "from src import gpu_shared_memory" in ROUTES
    assert "gpu_shared_memory.collect" in ROUTES
    assert '"gpu_mem": gpu_mem' in ROUTES
    assert '"sysmem_fallback": policy' in ROUTES


def test_endpoints_the_ui_calls_exist():
    for route in ('@router.get("/vram-fit")', '@router.get("/gpu/policy")',
                  '@router.post("/gpu/policy/open")'):
        assert route in ROUTES, route


@pytest.mark.parametrize("needle", [
    "d.gpu_mem",                 # the widget reads the new block
    "spilling",                  # and the flag that matters
    "PCIe spill",                # says so on the pill, not only in the panel
    "Shared GPU memory",         # and gets its own section in the panel
])
def test_usage_widget_surfaces_the_spill(needle):
    assert needle in SYS_USAGE_JS


def test_fit_advisor_is_reachable_from_the_model_controls():
    assert "Fit to VRAM" in MODEL_CONTROLS_JS
    assert "/api/system/vram-fit?model=" in MODEL_CONTROLS_JS
    assert "_runFit(model)" in MODEL_CONTROLS_JS
    # It has to apply something, not just describe it.
    assert "setOverride('num_ctx'" in MODEL_CONTROLS_JS
    assert "setOverride('num_gpu'" in MODEL_CONTROLS_JS


def test_num_gpu_survives_the_whole_override_path():
    chat_routes = (ROOT / "routes" / "chat_routes.py").read_text(encoding="utf-8")
    llm_core = (ROOT / "src" / "llm_core.py").read_text(encoding="utf-8")
    assert 'data.get("num_gpu")' in chat_routes          # accepted from the client
    assert '"num_gpu"' in llm_core                        # allowed through
    # and it must reach the Ollama `options` block, where it means anything
    assert 'for k in ("top_p", "top_k", "seed", "num_ctx", "num_gpu"' in llm_core


def test_hwfit_card_is_defined_and_called():
    assert "async function _renderGpuMemoryCard(sys)" in HWFIT_JS
    assert "_renderGpuMemoryCard(sys);" in HWFIT_JS.split("export function _hwfitRenderHw")[1]
    assert "'/api/system/usage'" in HWFIT_JS
    assert "'/api/system/gpu/policy/open'" in HWFIT_JS


def test_hwfit_card_styles_exist():
    for rule in (".hwfit-gpumem-nums", ".hwfit-gpumem-note", ".hwfit-gpumem-step",
                 ".hwfit-gpumem.hwfit-gpumem-spill"):
        assert rule in CSS, rule


def test_card_hides_itself_where_the_counters_do_not_exist():
    # Linux, no NVIDIA, a remote host: `supported` is false and the card must
    # disappear rather than render zeros that look like a healthy reading.
    assert "if (!gm.supported) { if (box) box.remove(); return; }" in HWFIT_JS
