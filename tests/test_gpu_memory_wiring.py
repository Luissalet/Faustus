"""Wiring tests for the shared-memory feature.

A perfect endpoint nobody calls is the quietest way to ship nothing, so each
half of this feature gets a test that it is actually plugged in: the collector
into `/api/system/usage`, the warning into the vitals, and the fit advisor
into the local-model options where its answer can be applied.
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROUTES = (ROOT / "routes" / "system_usage_routes.py").read_text(encoding="utf-8")
VITALS = (ROOT / "studio" / "src" / "screens" / "studio" / "Vitals.tsx").read_text(encoding="utf-8")
ADAPTER = (ROOT / "studio" / "src" / "adapters" / "usage.ts").read_text(encoding="utf-8")
LOCAL_MODELS = (ROOT / "studio" / "src" / "screens" / "settings" / "LocalModels.tsx").read_text(encoding="utf-8")
ADAPTER_LM = (ROOT / "studio" / "src" / "adapters" / "localModels.ts").read_text(encoding="utf-8")


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
    "d.gpu_mem",                  # the vitals read the block
    "spilling",                   # and the flag that matters
    "PCIe spill",                 # said on the pill, not only in the panel
    "Shared GPU memory",          # with its own section in the panel
])
def test_the_vitals_surface_the_spill(needle):
    """Weights paging over PCIe is the failure every other gauge hides: VRAM
    looks fine, utilisation looks fine, and the model runs at a tenth of the
    speed. It has to be the one thing that shouts."""
    text = VITALS + ADAPTER
    assert needle in text, needle


def test_the_fit_advisor_is_reachable_and_applies_something():
    """Describing the problem is half a feature: the advisor has to fill the
    two fields that fix it."""
    assert "Fit to VRAM" in LOCAL_MODELS
    assert "vramFit(" in LOCAL_MODELS and "/api/system/vram-fit" in ADAPTER_LM
    assert "setCtx(String(p.num_ctx))" in LOCAL_MODELS, "num_ctx must be applied"
    assert "setGpu(p.num_gpu == null ? '' : String(p.num_gpu))" in LOCAL_MODELS, (
        "num_gpu must be applied - and left empty when the server says null, "
        "which means 'let Ollama decide'"
    )


def test_the_plan_is_shown_in_the_servers_own_words():
    assert 'data-testid="vram-plan"' in LOCAL_MODELS
    assert "plan.steps.map" in LOCAL_MODELS, "the steps are the advice; they must be shown"


def test_the_shared_memory_section_hides_where_the_counters_do_not_exist():
    """Linux, no NVIDIA, a remote host: `supported` is false, and zeros there
    read as a healthy card. Better to show nothing."""
    section = VITALS[VITALS.index("function SharedSection"):]
    assert "supported" in section[:400], "the section must check `supported` before drawing"
    assert "return null" in section[:400], "and disappear rather than render zeros"
def test_num_gpu_survives_the_whole_override_path():
    chat_routes = (ROOT / "routes" / "chat_routes.py").read_text(encoding="utf-8")
    llm_core = (ROOT / "src" / "llm_core.py").read_text(encoding="utf-8")
    assert 'data.get("num_gpu")' in chat_routes          # accepted from the client
    assert '"num_gpu"' in llm_core                        # allowed through
    # and it must reach the Ollama `options` block, where it means anything
    assert 'for k in ("top_p", "top_k", "seed", "num_ctx", "num_gpu"' in llm_core


