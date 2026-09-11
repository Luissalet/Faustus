"""Lote A1 (OBJ-8) -- OpenRouter usage accounting from REAL provider cost.

OpenRouter reports `usage.cost` (actual USD spent), `usage.cost_details.
upstream_inference_cost`, `usage.prompt_tokens_details.cached_tokens` and
`usage.completion_tokens_details.reasoning_tokens` when a request opts in
with `payload["usage"] = {"include": True}` (added by Lote A2's
`src/openrouter_options.py` -- this lote does not touch the payload, only
the parsing side). Before this lote, `autonomy_budget.remote_spend_units()`
only ever ESTIMATED spend from token counts; this wires the real number
through when the provider gives it, end to end:

    src/llm_core.py (_extract_usage_extras, applied at every
    normalized_usage/_usage_data site)
        -> src/agent_loop.py (_usage_bucket persists cost_usd/cached_tokens/
           reasoning_tokens; _usage_bucket_summary sums cost_usd_total;
           TASK-06 passes provider_cost_usd into remote_spend_units)
        -> src/autonomy_budget.py (remote_spend_units prefers the real cost
           over the token estimate; spend_units_from_usd/USD_PER_UNIT is the
           documented USD<->unit bridge)

No network calls: `_drive`/`_FakeClient` below fake `stream_llm`'s HTTP
client exactly like `tests/test_llm_core_usage_finish_delta.py` does.
"""
import asyncio
import json
import sys
from unittest.mock import MagicMock

import pytest

from src import llm_core
from src import autonomy_budget as ab


# ── src/llm_core.py::_extract_usage_extras — unit tests ──


def test_extras_absent_when_usage_has_no_openrouter_fields():
    # A plain OpenAI-compatible usage dict (no OpenRouter usage.include) must
    # yield an EMPTY extras dict -- absent keys, not a None-filled one.
    assert llm_core._extract_usage_extras({"prompt_tokens": 9, "completion_tokens": 1}) == {}


def test_extras_absent_for_non_dict_usage():
    assert llm_core._extract_usage_extras(None) == {}
    assert llm_core._extract_usage_extras("not a dict") == {}


def test_extras_propagates_real_cost_and_marks_source():
    extras = llm_core._extract_usage_extras({
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "cost": 0.00123,
    })
    assert extras == {"cost_usd": 0.00123, "cost_source": "provider"}


def test_extras_propagates_upstream_cost_cached_and_reasoning_tokens():
    extras = llm_core._extract_usage_extras({
        "cost": 0.002,
        "cost_details": {"upstream_inference_cost": 0.0015},
        "prompt_tokens_details": {"cached_tokens": 512},
        "completion_tokens_details": {"reasoning_tokens": 64},
    })
    assert extras == {
        "cost_usd": 0.002,
        "cost_source": "provider",
        "upstream_cost_usd": 0.0015,
        "cached_tokens": 512,
        "reasoning_tokens": 64,
    }


@pytest.mark.parametrize("bad_cost", [None, "free", True, float("nan"), float("inf"), -0.5])
def test_extras_rejects_malformed_or_negative_cost(bad_cost):
    extras = llm_core._extract_usage_extras({"cost": bad_cost})
    assert "cost_usd" not in extras
    assert "cost_source" not in extras


def test_extras_ignores_non_dict_nested_details():
    extras = llm_core._extract_usage_extras({
        "cost": 0.001,
        "cost_details": "not a dict",
        "prompt_tokens_details": None,
        "completion_tokens_details": 42,
    })
    assert extras == {"cost_usd": 0.001, "cost_source": "provider"}


# ── src/llm_core.py::stream_llm end-to-end -- the OpenAI-compatible chat
#    path (~4487) is the one OpenRouter's usage.cost actually rides on ──


class _FakeResp:
    def __init__(self, lines):
        self._lines = lines
        self.status_code = 200

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln

    async def aread(self):
        return b""


class _FakeStreamCtx:
    def __init__(self, lines):
        self._lines = lines

    async def __aenter__(self):
        return _FakeResp(self._lines)

    async def __aexit__(self, *a):
        return False


class _FakeClient:
    def __init__(self, lines):
        self._lines = lines

    def stream(self, method, url, **kw):
        return _FakeStreamCtx(self._lines)


def _drive(monkeypatch, lines, model="openrouter-test"):
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: _FakeClient(lines))
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_mark_host_dead", lambda *a, **k: False, raising=False)

    async def run():
        out = []
        async for chunk in llm_core.stream_llm(
            "https://openrouter.ai/api/v1/chat/completions",
            model, [{"role": "user", "content": "hi"}],
            headers={"Authorization": "Bearer k"},
        ):
            out.append(chunk)
        return "".join(out)

    return asyncio.run(run())


def _usage_events(blob):
    events = []
    for ln in blob.split("\n"):
        ln = ln.strip()
        if ln.startswith("data: ") and ln[6:] != "[DONE]":
            try:
                j = json.loads(ln[6:])
            except ValueError:
                continue
            if j.get("type") == "usage":
                events.append(j["data"])
    return events


def test_stream_llm_surfaces_openrouter_real_cost_on_the_usage_event(monkeypatch):
    lines = [
        'data: ' + json.dumps({"choices": [{"delta": {"content": "Hi"}}]}),
        'data: ' + json.dumps({
            "choices": [],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "cost": 0.00123,
                "cost_details": {"upstream_inference_cost": 0.0009},
                "prompt_tokens_details": {"cached_tokens": 40},
                "completion_tokens_details": {"reasoning_tokens": 8},
            },
        }),
        'data: [DONE]',
    ]
    usage = _usage_events(_drive(monkeypatch, lines))
    assert usage, "no usage event emitted"
    data = usage[-1]
    assert data["input_tokens"] == 100
    assert data["output_tokens"] == 20
    assert data["cost_usd"] == 0.00123
    assert data["cost_source"] == "provider"
    assert data["upstream_cost_usd"] == 0.0009
    assert data["cached_tokens"] == 40
    assert data["reasoning_tokens"] == 8


def test_stream_llm_plain_usage_never_gains_cost_fields(monkeypatch):
    # Regression: a non-OpenRouter OpenAI-compatible server (no usage.include
    # support) must keep emitting exactly the pre-existing shape -- the exact
    # assertion tests/test_llm_core_usage_finish_delta.py already makes.
    lines = [
        'data: ' + json.dumps({"choices": [{"delta": {"content": "Hi"}}]}),
        'data: ' + json.dumps({
            "choices": [],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        }),
        'data: [DONE]',
    ]
    usage = _usage_events(_drive(monkeypatch, lines))
    assert usage == [{"input_tokens": 0, "output_tokens": 0}]


# ── src/agent_loop.py::_usage_bucket / _usage_bucket_summary ──
# Imported with the same heavy-dependency mocking `tests/test_agent_loop.py`
# uses, and dropped afterwards so the stubs never leak into later tests.

_MOCKED_IMPORTS = [
    'sqlalchemy', 'sqlalchemy.orm', 'sqlalchemy.ext', 'sqlalchemy.ext.declarative',
    'sqlalchemy.ext.hybrid', 'sqlalchemy.sql', 'sqlalchemy.sql.expression',
    'src.database',
    'src.agent_tools',
    'core.models', 'core.database',
]
_INJECTED_IMPORT_STUBS = {}
_PREEXISTING_AGENT_LOOP = sys.modules.get("src.agent_loop")


def _drop_module_if_same(name, expected):
    if sys.modules.get(name) is expected:
        sys.modules.pop(name, None)
    parent_name, _, attr = name.rpartition(".")
    parent = sys.modules.get(parent_name)
    if parent is not None and getattr(parent, "__dict__", {}).get(attr) is expected:
        delattr(parent, attr)


for _mod in _MOCKED_IMPORTS:
    if _mod not in sys.modules:
        _stub = MagicMock()
        sys.modules[_mod] = _stub
        _INJECTED_IMPORT_STUBS[_mod] = _stub

_IMPORTED_AGENT_LOOP = None
try:
    from src.agent_loop import _usage_bucket, _usage_bucket_summary
    _IMPORTED_AGENT_LOOP = sys.modules.get("src.agent_loop")
finally:
    if _PREEXISTING_AGENT_LOOP is None and _IMPORTED_AGENT_LOOP is not None:
        _drop_module_if_same("src.agent_loop", _IMPORTED_AGENT_LOOP)
    for _mod, _stub in _INJECTED_IMPORT_STUBS.items():
        _drop_module_if_same(_mod, _stub)


def _bucket(**overrides):
    kwargs = dict(
        round_num=1, model="anthropic/claude-3.7", endpoint_id="ep1",
        endpoint_label="OpenRouter", endpoint_cost_tracked=True,
        input_tokens=100, output_tokens=20, usage_source="real",
    )
    kwargs.update(overrides)
    return _usage_bucket(**kwargs)


def test_usage_bucket_without_extras_is_unchanged():
    bucket = _bucket()
    assert "cost_usd" not in bucket
    assert "cached_tokens" not in bucket
    assert "reasoning_tokens" not in bucket
    assert bucket["input_tokens"] == 100 and bucket["output_tokens"] == 20


def test_usage_bucket_persists_extras_when_given():
    bucket = _bucket(cost_usd=0.00123, cached_tokens=40, reasoning_tokens=8)
    assert bucket["cost_usd"] == 0.00123
    assert bucket["cached_tokens"] == 40
    assert bucket["reasoning_tokens"] == 8


@pytest.mark.parametrize("bad", [None, "free", True, -1.0])
def test_usage_bucket_ignores_malformed_cost(bad):
    bucket = _bucket(cost_usd=bad)
    assert "cost_usd" not in bucket


# ADP-22 integration lote: `billing`/`network`/`fallback_scope`/
# `route_reason` are the `src.provider_policy.RouteDecision` fields
# `docs/api/model_router.md` asks `_usage_bucket` to accept -- persisted only
# when a caller actually passes them, same "absent, not a default" rule as
# `cost_usd`/`cached_tokens`/`reasoning_tokens` above. No caller wires a real
# `RouteDecision` into `_usage_bucket` yet (see that function's own
# docstring for why there is no short path today); this only proves the
# kwargs exist and behave.

def test_usage_bucket_without_route_fields_is_unchanged():
    bucket = _bucket()
    assert "billing" not in bucket
    assert "network" not in bucket
    assert "fallback_scope" not in bucket
    assert "route_reason" not in bucket


def test_usage_bucket_persists_route_fields_when_given():
    bucket = _bucket(billing="subscription", network="local",
                     fallback_scope="none", route_reason="local-only profile")
    assert bucket["billing"] == "subscription"
    assert bucket["network"] == "local"
    assert bucket["fallback_scope"] == "none"
    assert bucket["route_reason"] == "local-only profile"


@pytest.mark.parametrize("field", ["billing", "network", "fallback_scope", "route_reason"])
@pytest.mark.parametrize("bad", [None, "", "   ", True, 42])
def test_usage_bucket_ignores_malformed_route_fields(field, bad):
    bucket = _bucket(**{field: bad})
    assert field not in bucket


def test_usage_bucket_summary_omits_cost_total_when_no_bucket_has_one():
    summary = _usage_bucket_summary([_bucket(round_num=1), _bucket(round_num=2)])
    assert "cost_usd_total" not in summary


def test_usage_bucket_summary_sums_cost_across_buckets_with_one():
    buckets = [
        _bucket(round_num=1, cost_usd=0.001),
        _bucket(round_num=2),  # local/no-cost round -- contributes nothing
        _bucket(round_num=3, cost_usd=0.002),
    ]
    summary = _usage_bucket_summary(buckets)
    assert summary["cost_usd_total"] == pytest.approx(0.003)
    # Aggregate token fields keep working exactly as before this lote.
    assert summary["input_tokens"] == 300
    assert summary["output_tokens"] == 60


# ── src/autonomy_budget.py::remote_spend_units / spend_units_from_usd ──


def test_remote_spend_units_still_estimates_from_tokens_without_provider_cost():
    # Pre-existing behaviour (tests/test_autonomy_budget.py) must be intact.
    assert ab.remote_spend_units(
        endpoint_cost_tracked=True, input_tokens=100, output_tokens=50,
    ) == 150


def test_remote_spend_units_prefers_real_provider_cost_over_estimate():
    units_from_cost = ab.remote_spend_units(
        endpoint_cost_tracked=True, input_tokens=100, output_tokens=50,
        provider_cost_usd=0.002,
    )
    assert units_from_cost == pytest.approx(ab.spend_units_from_usd(0.002))
    # The real cost REPLACES the estimate -- it is not summed with it.
    assert units_from_cost != 150


def test_remote_spend_units_ignores_cost_on_a_non_cost_tracked_endpoint():
    assert ab.remote_spend_units(
        endpoint_cost_tracked=False, input_tokens=100, output_tokens=50,
        provider_cost_usd=5.0,
    ) == 0.0
    assert ab.remote_spend_units(
        endpoint_cost_tracked=None, input_tokens=100, output_tokens=50,
        provider_cost_usd=5.0,
    ) == 0.0


@pytest.mark.parametrize("bad_cost", [None, "free", True, -1.0, float("nan"), float("inf")])
def test_remote_spend_units_falls_back_to_estimate_on_malformed_cost(bad_cost):
    assert ab.remote_spend_units(
        endpoint_cost_tracked=True, input_tokens=10, output_tokens=5,
        provider_cost_usd=bad_cost,
    ) == 15


def test_spend_units_from_usd_uses_documented_usd_per_unit():
    assert ab.USD_PER_UNIT > 0
    assert ab.spend_units_from_usd(ab.USD_PER_UNIT) == pytest.approx(1.0)
    assert ab.spend_units_from_usd(0) == 0.0


@pytest.mark.parametrize("bad", [None, "free", True, -1.0, float("nan")])
def test_spend_units_from_usd_rejects_malformed_input(bad):
    assert ab.spend_units_from_usd(bad) == 0.0
