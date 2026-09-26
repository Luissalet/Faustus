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
import struct

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
    assert set(result) == {"repo", "kind", "total_params", "active_params", "num_experts", "mtp",
                           "architectures", "native_context", "context_note", "source", "observed_at", "note"}


# ── INF-05 A4: native_context / context_note (§14 "tres límites de contexto") ─

def test_classify_native_context_from_max_position_embeddings():
    cfg = {"model_type": "qwen3", "architectures": ["Qwen3ForCausalLM"], "max_position_embeddings": 32768}
    info = ma.classify_hf_config(cfg)
    assert info["native_context"] == 32768
    assert info["context_note"] is None


def test_classify_native_context_is_the_rope_original_not_the_extended_value():
    # The Qwen-shaped case §14 cites: config states BOTH the extended window
    # and the original one under rope_scaling — native must be the ORIGINAL.
    cfg = {
        "model_type": "qwen3", "architectures": ["Qwen3ForCausalLM"],
        "max_position_embeddings": 131072,
        "rope_scaling": {"type": "yarn", "factor": 4.0, "original_max_position_embeddings": 32768},
    }
    info = ma.classify_hf_config(cfg)
    assert info["native_context"] == 32768  # the original, never the extended 131072
    assert "extended by rope_scaling (yarn) to 131072" in info["context_note"]
    assert "not a quality guarantee" in info["context_note"]


def test_classify_no_context_fields_leaves_native_context_absent():
    cfg = {"model_type": "x", "architectures": ["X"]}
    info = ma.classify_hf_config(cfg)
    assert info["native_context"] is None
    assert info["context_note"] is None


def test_ollama_show_native_context_from_model_info_context_length(monkeypatch):
    payload = {
        "model_info": {"general.architecture": "qwen3", "qwen3.context_length": 40960},
        "details": {"parameter_size": "8B"},
    }
    monkeypatch.setattr(ma.httpx, "Client", _mock_client(lambda req: httpx.Response(200, json=payload)))
    result = ma.get_model_architecture("qwen3:8b", source="ollama")
    assert result["native_context"] == 40960
    assert result["context_note"] is None


# ── gguf_header: local .gguf file, no HF config.json, no network ────────────
# Reuses tests/test_gguf_meta.py's synthetic-GGUF byte builders rather than
# a real model file.

def _kv_string(key: str, value: str) -> bytes:
    kb = key.encode("utf-8")
    vb = value.encode("utf-8")
    return struct.pack("<Q", len(kb)) + kb + struct.pack("<I", 8) + struct.pack("<Q", len(vb)) + vb


def _kv_u32(key: str, value: int) -> bytes:
    kb = key.encode("utf-8")
    return struct.pack("<Q", len(kb)) + kb + struct.pack("<I", 4) + struct.pack("<I", value)


def _write_gguf(path, kvs: list) -> None:
    with open(path, "wb") as f:
        f.write(b"GGUF")
        f.write(struct.pack("<I", 3))  # version
        f.write(struct.pack("<Q", 0))  # tensor_count
        f.write(struct.pack("<Q", len(kvs)))  # kv_count
        for kv in kvs:
            f.write(kv)


def test_llamacpp_source_reads_dense_architecture_from_local_gguf_header(tmp_path):
    path = tmp_path / "model.gguf"
    _write_gguf(path, [_kv_string("general.architecture", "llama")])
    result = ma.get_model_architecture(str(path), source="llamacpp")
    assert result["kind"] == "dense"
    assert result["source"] == "gguf_header"
    assert result["architectures"] == ["llama"]
    assert result["num_experts"] is None
    assert result["mtp"] is None  # no nextn_predict_layers key present -> absence, not False


def test_llamacpp_source_reads_moe_architecture_from_expert_count(tmp_path):
    path = tmp_path / "model.gguf"
    _write_gguf(path, [
        _kv_string("general.architecture", "qwen3moe"),
        _kv_u32("qwen3moe.expert_count", 128),
    ])
    result = ma.get_model_architecture(str(path), source="llamacpp")
    assert result["kind"] == "moe"
    assert result["num_experts"] == 128


def test_llamacpp_source_reports_mtp_true_when_nextn_layers_positive(tmp_path):
    path = tmp_path / "model.gguf"
    _write_gguf(path, [
        _kv_string("general.architecture", "qwen3"),
        _kv_u32("qwen3.nextn_predict_layers", 1),
    ])
    result = ma.get_model_architecture(str(path), source="llamacpp")
    assert result["mtp"] is True


def test_llamacpp_source_missing_gguf_file_is_unknown_not_an_error(tmp_path):
    result = ma.get_model_architecture(str(tmp_path / "nope.gguf"), source="llamacpp")
    assert result["kind"] == "unknown"
    assert result["source"] == "none"


def test_llamacpp_source_non_gguf_repo_string_is_unknown():
    # Not a local file at all (an HF-style repo id) -- llamacpp cannot serve
    # this without a running server, and must say so rather than guess.
    result = ma.get_model_architecture("org/some-repo", source="llamacpp")
    assert result["kind"] == "unknown"
    assert result["source"] == "none"


def test_auto_source_prefers_local_gguf_header_over_hf_config_when_path_is_a_gguf_file(tmp_path, monkeypatch):
    path = tmp_path / "model.gguf"
    _write_gguf(path, [_kv_string("general.architecture", "llama")])

    def handler(request):
        raise AssertionError("auto must not hit the network for a local .gguf path")

    monkeypatch.setattr(ma.httpx, "Client", _mock_client(handler))
    result = ma.get_model_architecture(str(path), source="auto")
    assert result["kind"] == "dense"
    assert result["source"] == "gguf_header"


def test_corrupt_gguf_header_is_unknown_not_an_exception(tmp_path):
    path = tmp_path / "truncated.gguf"
    with open(path, "wb") as f:
        f.write(b"GGUF")
        f.write(struct.pack("<I", 3))
        f.write(struct.pack("<Q", 0))
        f.write(struct.pack("<Q", 5))  # claims 5 kv entries, file has none
    result = ma.get_model_architecture(str(path), source="llamacpp")
    assert result["kind"] == "unknown"
    assert result["source"] == "none"
