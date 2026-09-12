"""INF-01 §A: `src/model_architecture.py` — architecture from metadata, not
from a model's name.

Two layers, same split `tests/test_model_calibration.py` uses:

  - `classify_hf_config` in isolation: pure fixture dicts, no network at all
    (T01 dense, T02 empty/contradictory -> unknown, MTP null vs true).
  - `read_hf_config`/`read_ollama_show`/`get_model_architecture` against a
    fake transport (`httpx.MockTransport`, no `respx` dependency in this
    repo) standing in for the HF Hub / a local Ollama — timeout, oversized,
    bad status all resolve to `kind: "unknown", source: "none"` with a
    `note`, never an exception the route would have to translate into one.

No real network, no model load, matching COMUN.md rule 7bis.
"""
from __future__ import annotations

import json

import httpx
import pytest

from src import model_architecture as ma
from src.privacy_policy import PROFILE_LOCAL_ONLY


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """Every test gets an empty cache, on a disk file that never touches
    the real `data/` directory."""
    monkeypatch.setattr(ma, "CACHE_FILE", str(tmp_path / "model_architecture_cache.json"))
    ma.reset_cache()
    yield
    ma.reset_cache()


_REAL_HTTPX_CLIENT = httpx.Client  # captured before any test monkeypatches it


def _mock_client(handler):
    """A drop-in for `httpx.Client(...)` backed by a fake transport — the
    module's own `timeout=`/`follow_redirects=` kwargs are accepted and
    ignored, same as `test_model_calibration.py`'s `_client` helper. Built
    from `_REAL_HTTPX_CLIENT`, never `httpx.Client` (which IS this factory
    once monkeypatched) — using the live name here would recurse forever."""
    def factory(*_args, **_kwargs):
        return _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(handler))
    return factory


# ── classify_hf_config: pure, no network ────────────────────────────────────

def test_classify_dense_config_never_derives_moe_from_a_dense_qwen_config():
    # A Qwen3.5-27B-shaped config: recognizable, no expert fields at all —
    # H01's exact regression case (the name alone used to trigger MoE).
    cfg = {"model_type": "qwen3_5", "architectures": ["Qwen3_5ForCausalLM"], "hidden_size": 5120, "num_hidden_layers": 48}
    info = ma.classify_hf_config(cfg)
    assert info["kind"] == "dense"
    assert info["num_experts"] is None
    assert info["mtp"] is None  # absence -> unknown, never False


def test_classify_moe_config_from_expert_fields():
    cfg = {"model_type": "qwen3_moe", "architectures": ["Qwen3MoeForCausalLM"], "num_experts": 512, "num_experts_per_tok": 10}
    info = ma.classify_hf_config(cfg)
    assert info["kind"] == "moe"
    assert info["num_experts"] == 512


def test_classify_empty_config_is_unknown_not_dense():
    assert ma.classify_hf_config({})["kind"] == "unknown"
    assert ma.classify_hf_config(None)["kind"] == "unknown"


def test_classify_contradictory_moe_fields_is_unknown():
    # num_experts=1 (i.e. "not MoE") alongside a MoE-only sizing field.
    cfg = {"model_type": "x", "num_experts": 1, "moe_intermediate_size": 1024}
    info = ma.classify_hf_config(cfg)
    assert info["kind"] == "unknown"
    assert "contradictory" in (info["note"] or "")


def test_classify_mtp_true_only_when_the_field_says_so_and_positive():
    cfg = {"model_type": "deepseek_v3", "architectures": ["X"], "num_nextn_predict_layers": 1}
    assert ma.classify_hf_config(cfg)["mtp"] is True
    cfg_zero = {"model_type": "deepseek_v3", "architectures": ["X"], "num_nextn_predict_layers": 0}
    assert ma.classify_hf_config(cfg_zero)["mtp"] is None
    cfg_absent = {"model_type": "deepseek_v3", "architectures": ["X"]}
    assert ma.classify_hf_config(cfg_absent)["mtp"] is None


# ── read_hf_config / get_model_architecture(source="hf") ────────────────────

def test_hf_dense_config_end_to_end(monkeypatch):
    cfg = {"model_type": "qwen3_5", "architectures": ["Qwen3_5ForCausalLM"]}

    def handler(request):
        assert request.url.path.endswith("/raw/main/config.json")
        return httpx.Response(200, json=cfg)

    monkeypatch.setattr(ma.httpx, "Client", _mock_client(handler))
    result = ma.get_model_architecture("org/Qwen3.5-27B", source="hf")
    assert result["kind"] == "dense"
    assert result["source"] == "hf_config"
    assert result["mtp"] is None
    assert result["observed_at"]


def test_hf_moe_config_end_to_end(monkeypatch):
    cfg = {"model_type": "x", "architectures": ["X"], "num_experts": 512, "num_experts_per_tok": 10}
    monkeypatch.setattr(ma.httpx, "Client", _mock_client(lambda req: httpx.Response(200, json=cfg)))
    result = ma.get_model_architecture("org/big-moe", source="hf")
    assert result["kind"] == "moe"
    assert result["num_experts"] == 512


def test_hf_timeout_is_unknown_with_a_note_http_200_not_an_exception(monkeypatch):
    def handler(request):
        raise httpx.TimeoutException("deadline exceeded", request=request)

    monkeypatch.setattr(ma.httpx, "Client", _mock_client(handler))
    result = ma.get_model_architecture("org/slow-repo", source="hf")
    assert result["kind"] == "unknown"
    assert result["source"] == "none"
    assert result["observed_at"] is None
    assert "timed out" in (result["note"] or "")


def test_hf_non_200_is_unknown_with_a_note(monkeypatch):
    monkeypatch.setattr(ma.httpx, "Client", _mock_client(lambda req: httpx.Response(404)))
    result = ma.get_model_architecture("org/missing-repo", source="hf")
    assert result["kind"] == "unknown"
    assert result["source"] == "none"
    assert "404" in (result["note"] or "")


def test_hf_oversized_config_is_rejected_by_content_length(monkeypatch):
    big = str(ma._HF_CONFIG_MAX_BYTES + 1)
    monkeypatch.setattr(
        ma.httpx, "Client",
        _mock_client(lambda req: httpx.Response(200, headers={"content-length": big}, content=b"{}")),
    )
    result = ma.get_model_architecture("org/huge-config", source="hf")
    assert result["kind"] == "unknown"
    assert "size limit" in (result["note"] or "")


def test_hf_empty_or_contradictory_config_is_unknown_end_to_end(monkeypatch):
    monkeypatch.setattr(ma.httpx, "Client", _mock_client(lambda req: httpx.Response(200, json={})))
    result = ma.get_model_architecture("org/no-config-fields", source="hf")
    assert result["kind"] == "unknown"
    assert result["source"] == "hf_config"  # a real (empty) config WAS read
    assert result["observed_at"]


# ── local-only privacy profile: no network call at all ──────────────────────

def test_local_only_profile_blocks_the_hf_call_before_any_network(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={})

    monkeypatch.setattr(ma.httpx, "Client", _mock_client(handler))
    result = ma.get_model_architecture("org/private-repo", source="hf", profile=PROFILE_LOCAL_ONLY)
    assert calls == [], "local_only must block the call, not merely ignore its answer"
    assert result["kind"] == "unknown"
    assert result["source"] == "none"
    assert "local-only" in (result["note"] or "").lower() or "local_only" in (result["note"] or "").lower()


# ── ollama_show ───────────────────────────────────────────────────────────

def test_ollama_show_moe_from_expert_count(monkeypatch):
    payload = {
        "model_info": {"general.architecture": "qwen3moe", "qwen3moe.expert_count": 128, "qwen3moe.expert_used_count": 8},
        "details": {"parameter_size": "397B"},
    }
    monkeypatch.setattr(ma.httpx, "Client", _mock_client(lambda req: httpx.Response(200, json=payload)))
    result = ma.get_model_architecture("qwen3.5:9b", source="ollama")
    assert result["kind"] == "moe"
    assert result["num_experts"] == 128
    assert result["source"] == "ollama_show"
    assert result["total_params"] == 397_000_000_000
    assert result["mtp"] is None  # /api/show never reports MTP


def test_ollama_show_dense_from_architecture_without_experts(monkeypatch):
    payload = {"model_info": {"general.architecture": "llama"}, "details": {"parameter_size": "8B"}}
    monkeypatch.setattr(ma.httpx, "Client", _mock_client(lambda req: httpx.Response(200, json=payload)))
    result = ma.get_model_architecture("llama3:8b", source="ollama")
    assert result["kind"] == "dense"


def test_ollama_unreachable_is_unknown_with_a_note(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(ma.httpx, "Client", _mock_client(handler))
    result = ma.get_model_architecture("qwen3.5:9b", source="ollama")
    assert result["kind"] == "unknown"
    assert result["source"] == "none"


# ── auto routing (name shape only, never a network-level assumption) ────────

def test_auto_routes_a_bare_tag_to_ollama_and_never_touches_hf(monkeypatch):
    monkeypatch.setattr(ma, "_try_ollama", lambda repo: {**ma._unknown(repo, "ollama_show", None), "kind": "dense", "source": "ollama_show"})

    def _fail_if_called(*_a, **_kw):
        raise AssertionError("auto must not fall through to HF when Ollama answered")

    monkeypatch.setattr(ma, "_try_hf", _fail_if_called)
    result = ma.get_model_architecture("qwen3.5:9b", source="auto")
    assert result["source"] == "ollama_show"


def test_auto_routes_an_org_slash_name_to_hf(monkeypatch):
    monkeypatch.setattr(ma, "_try_hf", lambda repo, **kw: {**ma._unknown(repo, "hf_config", None), "kind": "moe", "source": "hf_config"})

    def _fail_if_called(*_a, **_kw):
        raise AssertionError("a repo with a slash is never treated as an Ollama tag")

    monkeypatch.setattr(ma, "_try_ollama", _fail_if_called)
    result = ma.get_model_architecture("org/some-model", source="auto")
    assert result["source"] == "hf_config"


# ── llamacpp source: honestly unresolved, never faked ────────────────────────

def test_llamacpp_source_is_unknown_never_a_guess():
    result = ma.get_model_architecture("some-model.gguf", source="llamacpp")
    assert result["kind"] == "unknown"
    assert result["source"] == "none"
    assert result["note"]


# ── caching ───────────────────────────────────────────────────────────────

def test_a_second_lookup_within_ttl_does_not_hit_the_network_again(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"model_type": "x", "architectures": ["X"]})

    monkeypatch.setattr(ma.httpx, "Client", _mock_client(handler))
    first = ma.get_model_architecture("org/cached-repo", source="hf")
    second = ma.get_model_architecture("org/cached-repo", source="hf")
    assert calls["n"] == 1
    assert first == second


def test_a_failed_lookup_is_not_cached_so_the_next_call_retries(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503)

    monkeypatch.setattr(ma.httpx, "Client", _mock_client(handler))
    ma.get_model_architecture("org/flaky-repo", source="hf")
    ma.get_model_architecture("org/flaky-repo", source="hf")
    assert calls["n"] == 2


# ── shape ─────────────────────────────────────────────────────────────────

def test_response_has_every_contract_field_even_when_unknown():
    result = ma.get_model_architecture("", source="hf")
    assert set(result) == {"repo", "kind", "total_params", "active_params", "num_experts", "mtp", "architectures", "source", "observed_at", "note"}
