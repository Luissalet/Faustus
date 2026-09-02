"""GPU placement policy (src/gpu_policy.py): fill card N first.

Measured on the two-card box: `main_gpu` pins, and a model too big for the
pinned card is NOT split — the rest goes to the CPU (54/66 layers, 10 tok/s
instead of 19–24). `tensor_split` is ignored. So the policy pins only what
fits and leaves the rest to Ollama.
"""
from __future__ import annotations

import json

import pytest

from src import gpu_policy as gp

GIB = 1024 ** 3
SNAP = {"supported": True, "count": 2, "gpus": [
    {"index": 0, "name": "NVIDIA GeForce RTX 4070 Ti", "total": 12282 * 1024 ** 2, "used": 0, "free": 0},
    {"index": 1, "name": "NVIDIA GeForce RTX 5060 Ti", "total": 16311 * 1024 ** 2, "used": 0, "free": 0},
]}
SIZES = {"qwen3.5:9b": int(6.6 * GIB), "qwen3.8:27b-q4_K_M": int(17.0 * GIB), "qwen3.8:27b-q8_0": int(29 * GIB),
         "mistral-small:24b": int(14.0 * GIB)}


@pytest.fixture
def box(monkeypatch):
    from src import gpu_shared_memory as gsm
    monkeypatch.setattr(gsm, "vram_snapshot", lambda: SNAP)
    monkeypatch.setattr(gp, "model_sizes", lambda base, timeout=3.0: dict(SIZES))
    state = {"prefer": -1}
    monkeypatch.setattr(gp, "preferred_index", lambda: state["prefer"])
    gp.reset_cache()
    yield state
    gp.reset_cache()


def test_fits_card_keeps_a_cuda_context_and_room_for_the_context():
    total16 = 16311 * 1024 ** 2
    assert gp.fits_card(int(6.6 * GIB), total16)
    assert gp.fits_card(int(12.0 * GIB), total16)      # ~12.3 GB is the most a 16 GB card takes
    # 14 GB of weights on a 16 GB card: weights fit, the KV cache does not
    assert not gp.fits_card(int(14.0 * GIB), total16)
    assert not gp.fits_card(int(17.0 * GIB), total16)
    assert not gp.fits_card(0, total16) and not gp.fits_card(1, 0)


def test_auto_adds_nothing(box):
    assert gp.preferred_main_gpu("http://127.0.0.1:11434/v1", "qwen3.5:9b") is None


def test_prefer_pins_what_fits_and_leaves_the_rest_to_ollama(box):
    box["prefer"] = 1
    assert gp.preferred_main_gpu("http://127.0.0.1:11434/v1", "qwen3.5:9b") == 1
    # 17 GB on a 16 GB card would go to the CPU: no pin, Ollama splits it
    assert gp.preferred_main_gpu("http://127.0.0.1:11434/v1", "qwen3.8:27b-q4_K_M") is None
    assert gp.preferred_main_gpu("http://127.0.0.1:11434/v1", "qwen3.8:27b-q8_0") is None
    # the 14 GB one: weights fit, context would not → not pinned either
    assert gp.preferred_main_gpu("http://127.0.0.1:11434/v1", "mistral-small:24b") is None
    # a model the server does not list: unknown size → no pin
    assert gp.preferred_main_gpu("http://127.0.0.1:11434/v1", "ghost:1b") is None
    # name forms: bare name → :latest, library prefix
    gp.reset_cache()
    assert gp.preferred_main_gpu("http://localhost:11434/api/chat", "library/qwen3.5:9b") == 1


def test_only_the_local_ollama_gets_a_policy(box, monkeypatch):
    box["prefer"] = 1
    assert gp.preferred_main_gpu("https://api.openai.com/v1", "qwen3.5:9b") is None
    assert gp.preferred_main_gpu("http://192.168.1.20:11434/v1", "qwen3.5:9b") is None
    from src import model_load_options as mlo
    monkeypatch.setattr(mlo, "is_declared_ollama_host", lambda url: "192.168.1.20" in url)
    assert gp.preferred_main_gpu("http://192.168.1.20:11434/v1", "qwen3.5:9b") == 1


def test_a_card_that_does_not_exist_pins_nothing(box):
    box["prefer"] = 3
    assert gp.preferred_main_gpu("http://127.0.0.1:11434/v1", "qwen3.5:9b") is None


def test_describe_names_the_card(box):
    box["prefer"] = 1
    d = gp.describe(SNAP["gpus"])
    assert d == {"prefer": 1, "name": "NVIDIA GeForce RTX 5060 Ti", "mode": "prefer"}
    box["prefer"] = -1
    assert gp.describe(SNAP["gpus"])["mode"] == "auto"


def test_model_sizes_reads_api_tags_and_survives_a_dead_server(monkeypatch):
    import httpx

    class R:
        status_code = 200

        def json(self):
            return {"models": [{"name": "qwen3.5:9b", "size": 7}, {"model": "x:1b", "size": 2}]}

    monkeypatch.setattr(httpx, "get", lambda url, timeout=3.0: R())
    gp.reset_cache()
    assert gp.model_sizes("http://127.0.0.1:11434") == {"qwen3.5:9b": 7, "x:1b": 2}

    def boom(url, timeout=3.0):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(httpx, "get", boom)
    # cached answer survives a dead server; a cold miss is empty, not an error
    assert gp.model_sizes("http://127.0.0.1:11434") == {"qwen3.5:9b": 7, "x:1b": 2}
    assert gp.model_sizes("http://127.0.0.1:11435") == {}


# ── the hooks: every chat request and the Load button ────────────────────────

def test_llm_core_load_defaults_carry_the_policy_under_a_per_model_pin(box, monkeypatch):
    from src import llm_core
    from src import model_load_options as mlo
    box["prefer"] = 1
    monkeypatch.setattr(mlo, "resolve_for_request", lambda url, model: {"num_ctx": 8192})
    assert llm_core._model_load_defaults("http://127.0.0.1:11434/v1", "qwen3.5:9b") == {"num_ctx": 8192, "main_gpu": 1}
    # a per-model pin wins over the policy
    monkeypatch.setattr(mlo, "resolve_for_request", lambda url, model: {"main_gpu": 0})
    assert llm_core._model_load_defaults("http://127.0.0.1:11434/v1", "qwen3.5:9b") == {"main_gpu": 0}
    # a model that does not fit the preferred card: no main_gpu at all
    monkeypatch.setattr(mlo, "resolve_for_request", lambda url, model: {})
    assert llm_core._model_load_defaults("http://127.0.0.1:11434/v1", "qwen3.8:27b-q4_K_M") == {}


def test_set_preferred_index_validates_and_persists(tmp_path, monkeypatch):
    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    settings_mod._invalidate_caches()
    try:
        assert gp.set_preferred_index(1) == 1
        assert settings_mod.get_setting("gpu_placement_prefer") == 1
        assert gp.preferred_index() == 1
        with pytest.raises(ValueError):
            gp.set_preferred_index(42)
        assert gp.set_preferred_index(-1) == -1
    finally:
        settings_mod._invalidate_caches()




def test_the_js_offers_the_policy_only_with_two_cards_and_warns_on_a_bad_pin():
    import subprocess
    from pathlib import Path
    repo = Path(__file__).resolve().parents[1]
    src = (repo / "static/js/localModels.js").read_text(encoding="utf-8")
    assert "lm-placement" in src and "/placement" in src and "placementSelectHtml(vram, policy)" in src
    node = subprocess.run(["node", "--input-type=module"], input=(
        src.replace("import uiModule from './ui.js';", "const uiModule = {};")
           .replace("import { invalidateSettings } from './appConfig.js';", "const invalidateSettings = () => {};")
           .replace("export function", "function").replace("export default localModelsModule;", "")
           .replace("export const", "const")
        + """
const gpus = [{index: 0, name: 'NVIDIA GeForce RTX 4070 Ti', total_bytes: 12282 * 1048576},
              {index: 1, name: 'NVIDIA GeForce RTX 5060 Ti', total_bytes: 16311 * 1048576}];
const vram2 = {count: 2, gpus};
console.log(JSON.stringify({
  auto: placementSelectHtml(vram2, {prefer: -1}),
  one: placementSelectHtml(vram2, {prefer: 1}),
  single: placementSelectHtml({count: 1, gpus: [gpus[0]]}, {prefer: 1}),
  warnBig: pinWarningHtml(1, 17 * 1073741824, gpus),
  warnSmall: pinWarningHtml(1, 6.6 * 1073741824, gpus),
  warnAuto: pinWarningHtml(null, 17 * 1073741824, gpus),
}));
"""), capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert node.returncode == 0, node.stderr
    out = json.loads(node.stdout.strip().splitlines()[-1])
    assert 'value="-1" selected' in out["auto"] and 'Fill GPU 1 first — RTX 5060 Ti (16 GB)' in out["auto"]
    assert 'value="1" selected' in out["one"] and out["single"] == ""
    assert "will not fit RTX 5060 Ti" in out["warnBig"] and "runs on the CPU" in out["warnBig"]
    assert out["warnSmall"] == "" and out["warnAuto"] == ""
