"""src/inference_capabilities.py — INF-02 A3.

`assess_options` is pure (no probe, no process) and must keep support/
effective/benefit apart: this file only exercises `support` and `reasons`,
because `effective` and `benefit` are fixed at `unconfirmed`/`not_evaluated`
here by design — only `launch_receipts.verify` (and, later, a benchmark run)
ever moves those.
"""
from __future__ import annotations

import pytest

from src import inference_capabilities as ic


DENSE_ARCH = {"kind": "dense", "mtp": None}
MOE_ARCH = {"kind": "moe", "mtp": True}
UNKNOWN_ARCH = {"kind": "unknown", "mtp": None}


# ── T01/T02: dense vs. unknown architecture ─────────────────────────────────

def test_dense_model_does_not_get_moe_option_flagged_supported():
    # T01: a dense model requesting an MoE-only vLLM option is unsupported,
    # with the failed requirement named in reasons — never silently dropped.
    assessments = ic.assess_options("vllm", {"expert_parallel": True}, arch=DENSE_ARCH)
    assert len(assessments) == 1
    a = assessments[0]
    assert a.support == "unsupported"
    assert any("arch.kind==moe" in r for r in a.reasons)


def test_unknown_architecture_is_unknown_not_unsupported():
    # T02: not knowing the architecture must never read as a confident "no".
    assessments = ic.assess_options("vllm", {"expert_parallel": True}, arch=UNKNOWN_ARCH)
    assert assessments[0].support == "unknown"
    assessments_none = ic.assess_options("vllm", {"expert_parallel": True}, arch=None)
    assert assessments_none[0].support == "unknown"


def test_moe_model_gets_expert_parallel_supported():
    assessments = ic.assess_options("vllm", {"expert_parallel": True}, arch=MOE_ARCH)
    assert assessments[0].support == "supported"


# ── KV cache quantization requires flash attention (llama.cpp) ──────────────

def test_kv_cache_quant_without_flash_attn_is_unsupported_with_requirement_named():
    assessments = ic.assess_options(
        "llama-server", {"cache_type_k": "q8_0"}, arch=DENSE_ARCH,
    )
    a = assessments[0]
    assert a.support == "unsupported"
    assert any("option:flash_attn==on" in r for r in a.reasons)


def test_kv_cache_quant_with_flash_attn_on_is_supported():
    assessments = ic.assess_options(
        "llama-server", {"flash_attn": True, "cache_type_k": "q8_0"}, arch=DENSE_ARCH,
    )
    by_option = {a.option: a for a in assessments}
    assert by_option["cache_type_k"].support == "supported"
    assert by_option["flash_attn"].support == "supported"


def test_ollama_kv_cache_type_also_requires_flash_attn_global():
    assessments = ic.assess_options("ollama", {"kv_cache_type": "q8_0"})
    a = assessments[0]
    assert a.support == "unsupported"
    assert a.scope == "global"


# ── unknown option ───────────────────────────────────────────────────────────

def test_option_not_in_manifest_is_unknown_with_reason():
    assessments = ic.assess_options("llama-server", {"totally_made_up_option": 1})
    a = assessments[0]
    assert a.support == "unknown"
    assert "not in the versioned manifest for llama-server" in a.reasons[0]
    assert a.evidence.kind == "none"


def test_unknown_implementation_treats_every_option_as_unknown():
    assessments = ic.assess_options("some_future_engine", {"ctx": 4096})
    assert assessments[0].support == "unknown"


# ── since: null never presents as a confirmed minimum version ──────────────

def test_since_null_is_never_presented_as_a_confirmed_version():
    assessments = ic.assess_options("llama-server", {"ctx": 8192}, arch=DENSE_ARCH)
    a = assessments[0]
    manifest = ic.load_manifest()
    assert manifest["llama-server"]["options"]["ctx"]["since"] is None
    assert a.support == "supported"
    # The uncertainty must be visible somewhere in reasons, not hidden.
    assert a.reasons, "a since:null option must say so in reasons"


# ── llama_cpp.server: options with no equivalent flag ───────────────────────

def test_llama_cpp_python_omitted_option_is_unsupported_with_specific_reason():
    assessments = ic.assess_options("llama_cpp.server", {"flash_attn": True})
    a = assessments[0]
    assert a.support == "unsupported"
    assert a.reasons == ("no equivalent flag in python -m llama_cpp.server",)


@pytest.mark.parametrize("option", sorted(ic.LLAMA_CPP_PY_OMITTED))
def test_every_omitted_option_is_flagged_for_llama_cpp_python(option):
    assessments = ic.assess_options("llama_cpp.server", {option: True})
    assert assessments[0].support == "unsupported"


def test_llama_cpp_python_supports_cache_type_without_flash_attn_gate():
    # Unlike llama-server, the python wrapper has no --flash-attn flag to
    # gate cache_type_k/v behind — the manifest's `requires` for this option
    # under this implementation must be empty.
    assessments = ic.assess_options("llama_cpp.server", {"cache_type_k": "q8_0"})
    assert assessments[0].support == "supported"


# ── gpu-count requirement ────────────────────────────────────────────────────

def test_tensor_split_needs_two_gpus_and_is_unknown_without_gpu_info():
    assessments = ic.assess_options("llama-server", {"tensor_split": "1,1"}, gpus=None)
    assert assessments[0].support == "unknown"

    assessments_one_gpu = ic.assess_options("llama-server", {"tensor_split": "1,1"}, gpus=[{"index": 0}])
    assert assessments_one_gpu[0].support == "unsupported"

    assessments_two_gpus = ic.assess_options(
        "llama-server", {"tensor_split": "1,1"}, gpus=[{"index": 0}, {"index": 1}],
    )
    assert assessments_two_gpus[0].support == "supported"


# ── hard_blockers ─────────────────────────────────────────────────────────

def test_hard_blockers_returns_only_unsupported_and_nothing_else():
    assessments = ic.assess_options(
        "vllm",
        {"expert_parallel": True, "ctx": 8192, "totally_unknown": 1},
        arch=DENSE_ARCH,
    )
    blockers = ic.hard_blockers(assessments)
    assert [b.option for b in blockers] == ["expert_parallel"]


def test_hard_blockers_empty_when_nothing_unsupported():
    assessments = ic.assess_options("vllm", {"ctx": 8192, "dtype": "bfloat16"})
    assert ic.hard_blockers(assessments) == []


# ── requested value survives untouched ──────────────────────────────────────

def test_requested_value_is_returned_verbatim_never_coerced():
    assessments = ic.assess_options("llama-server", {"ngl": 0})
    assert assessments[0].requested == 0
    assert assessments[0].requested is not False
