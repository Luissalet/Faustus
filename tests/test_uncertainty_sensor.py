"""Uncertainty sensor (src/uncertainty_sensor.py, src/uncertainty_case.py,
the `uncertainty_next_observation` chat tool and its MCP exposure)."""
import asyncio
import json

import pytest

from src import uncertainty_case, uncertainty_sensor as us

HYPS = ["pump", "valve", "sensor"]


def cands():
    return [
        {"id": "swap_sensor", "cost": 5, "predictions": {"pump": "fault", "valve": "fault", "sensor": "ok"}},
        {"id": "pressure_gauge", "cost": 20, "description": "read pressure with a gauge",
         "predictions": {"pump": "low", "valve": "high", "sensor": "normal"}},
        {"id": "teardown", "cost": 120, "predictions": {"pump": "worn", "valve": "stuck", "sensor": "clean"}},
        {"id": "listen", "cost": 1, "predictions": {"pump": "noise", "valve": "quiet", "sensor": "quiet"}},
    ]


def test_cheapest_test_that_separates_all_three():
    r = us.next_observation(HYPS, cands())
    rec = r["recommended"]
    assert rec["candidate"] == "pressure_gauge"            # cheapest of the two that separate all three
    assert rec["mode"] == "cheapest_test_that_separates_all_live_hypotheses"
    assert rec["separates_all_live"] and rec["left_together_pairs"] == []
    assert "tells all 3" in rec["why"] and "pump predicts 'low'" in rec["why"]
    assert {d["discriminating_test"] for d in r["doubts"]} <= {"pressure_gauge", "teardown", "swap_sensor", "listen"}
    assert all(d["discriminating_test"] for d in r["doubts"])
    assert abs(sum(h["posterior"] for h in r["hypotheses"]) - 1) < 1e-4


def test_best_gain_per_cost_when_none_separates_all():
    c = [x for x in cands() if x["id"] in ("swap_sensor", "listen")]
    r = us.next_observation(HYPS, c)
    rec = r["recommended"]
    assert rec["mode"] == "best_expected_gain_per_cost" and not rec["separates_all_live"]
    ranks = [x["gain_per_cost"] for x in r["ranking"]]
    assert ranks == sorted(ranks, reverse=True) and rec["candidate"] == r["ranking"][0]["candidate"]
    assert rec["left_together_pairs"]        # it says which it leaves together


def test_observation_concentrates_posterior_and_stop_fires():
    r = us.next_observation(HYPS, cands(), [{"candidate": "pressure_gauge", "result": "HIGH"}])
    top = r["hypotheses"][0]
    assert top["id"] == "valve" and top["posterior"] > 0.95
    assert r["stop"]["stop"] and r["stop"]["reason"] == "posterior_threshold" and r["recommended"] is None
    assert all(x["candidate"] != "pressure_gauge" for x in r["ranking"])


def test_stop_when_gain_per_cost_below_minimum():
    r = us.next_observation(HYPS, cands()[:1], min_gain_per_cost=5.0)
    assert r["stop"]["stop"] and r["stop"]["reason"] == "gain_per_cost_below_minimum"


def test_numeric_interval_predictions():
    hyps = [{"id": "worn", "description": "pad worn out"}, {"id": "ok"}, {"id": "swollen"}]
    c = [{"id": "thickness", "cost": 3, "predictions": {
        "worn": [0.5, 1.5], "ok": {"interval": [2.0, 3.0]}, "swollen": {"value": 4.0, "tolerance": 0.5}}}]
    r = us.next_observation(hyps, c)
    assert r["recommended"]["separates_all_live"] and "[0.5, 1.5]" in r["recommended"]["why"]
    r2 = us.next_observation(hyps, c, [{"candidate": "thickness", "result": 2.4}])
    assert r2["hypotheses"][0]["id"] == "ok" and r2["hypotheses"][0]["posterior"] > 0.95
    r3 = us.next_observation(hyps, c, [{"candidate": "thickness", "result": {"interval": [0.9, 1.1]}}])
    assert r3["hypotheses"][0]["id"] == "worn"
    # a reading nobody predicted does not zero everything and is flagged
    r4 = us.next_observation(hyps, c, [{"candidate": "thickness", "result": 9.0}])
    assert any("not predicted" in w for w in r4["warnings"])
    assert abs(sum(h["posterior"] for h in r4["hypotheses"]) - 1) < 1e-4


def test_indistinguishable_pair_is_reported():
    c = [{"id": "a", "cost": 1, "predictions": {"pump": "x", "valve": "y", "sensor": "y"}},
         {"id": "b", "cost": 2, "predictions": {"pump": "p", "valve": "q", "sensor": "q"}}]
    hyps = [{"id": "pump"}, {"id": "valve", "description": "stuck open"}, {"id": "sensor", "description": "drifting"}]
    r = us.next_observation(hyps, c)
    assert r["cannot_separate"]
    pair = r["unseparable_pairs"][0]
    assert sorted(pair["between"]) == ["sensor", "valve"] and "stuck open" in pair["needed_observation"]
    doubt = next(d for d in r["doubts"] if sorted(d["between"]) == ["sensor", "valve"])
    assert doubt["discriminating_test"] is None
    assert next(d for d in r["doubts"] if "pump" in d["between"])["discriminating_test"] == "a"


def test_prior_weights_respected():
    flat = us.next_observation(HYPS, cands())
    assert all(h["posterior"] == pytest.approx(1 / 3, abs=1e-4) for h in flat["hypotheses"])
    r = us.next_observation([{"id": "pump", "prior": 8}, {"id": "valve", "prior": 1}, {"id": "sensor", "prior": 1}], cands())
    by = {h["id"]: h for h in r["hypotheses"]}
    assert by["pump"]["prior"] == pytest.approx(0.8) and r["hypotheses"][0]["id"] == "pump"
    # the same ambiguous result favours the hypothesis with the larger prior
    amb = [{"id": "t", "predictions": {"pump": {"a": 0.5, "b": 0.5}, "valve": {"a": 0.5, "b": 0.5}, "sensor": "c"}}]
    p = us.next_observation([{"id": "pump", "prior": 3}, {"id": "valve", "prior": 1}, "sensor"], amb, [{"candidate": "t", "result": "a"}])
    pm = {h["id"]: h["posterior"] for h in p["hypotheses"]}
    assert pm["pump"] > pm["valve"] * 2.5 and pm["sensor"] < 0.05


def test_invalid_input():
    with pytest.raises(ValueError):
        us.next_observation(["only"], cands())
    with pytest.raises(ValueError):
        us.next_observation(HYPS, [{"id": "x", "cost": -1, "predictions": {"pump": "a"}}])
    with pytest.raises(ValueError):
        us.next_observation(HYPS, cands(), [{"candidate": "nope", "result": 1}])
    with pytest.raises(ValueError):
        us.next_observation(HYPS, [{"id": "x", "predictions": {"ghost": "a"}}])


def test_deterministic():
    assert us.next_observation(HYPS, cands()) == us.next_observation(HYPS, cands())


# ── session record + chat tool + MCP ───────────────────────────────────────

@pytest.fixture
def datadir(tmp_path, monkeypatch):
    import src.constants as const
    monkeypatch.setattr(const, "DATA_DIR", str(tmp_path))
    return tmp_path


def test_case_updates_across_calls(datadir):
    first = uncertainty_case.advance({"case_id": "press-1", "hypotheses": HYPS, "candidates": cands()}, "u1")
    assert first["case_id"] == "press-1" and first["case_created"] and not first["stop"]["stop"]
    second = uncertainty_case.advance({"case_id": "press-1", "observed": [{"candidate": "pressure_gauge", "result": "low"}]}, "u1")
    assert not second["case_created"] and second["hypotheses"][0]["id"] == "pump" and second["stop"]["stop"]
    assert len(second["observations_used"]) == 1
    assert uncertainty_case.advance({"action": "list"}, "u1")["cases"][0]["observations"] == 1
    assert uncertainty_case.advance({"action": "list"}, "other")["cases"] == []
    assert uncertainty_case.advance({"action": "delete", "case_id": "press-1"}, "u1")["deleted"]
    with pytest.raises(ValueError):
        uncertainty_case.advance({"case_id": "../x", "hypotheses": HYPS, "candidates": cands()}, "u1")


def test_chat_tool_and_catalogue(datadir):
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
    from src.tool_capabilities import ToolEffect, capabilities_for_tool
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_index_examples import EXAMPLES
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    name = "uncertainty_next_observation"
    assert name in TOOL_HANDLERS and name in TOOL_TAGS and name in BUILTIN_TOOL_DESCRIPTIONS and EXAMPLES[name]
    assert any(t["function"]["name"] == name for t in FUNCTION_TOOL_SCHEMAS)
    assert ToolEffect.WRITE_PRIVATE in capabilities_for_tool(name).effects
    payload = json.dumps({"hypotheses": HYPS, "candidates": cands(), "case_id": "chat-1"})
    out = asyncio.run(TOOL_HANDLERS[name](payload, {"owner": "u1"}))
    assert out["exit_code"] == 0 and "Next: pressure_gauge" in out["output"] and "Case: chat-1" in out["output"]
    bad = asyncio.run(TOOL_HANDLERS[name](json.dumps({"hypotheses": ["a"], "candidates": cands()}), {}))
    assert bad["exit_code"] == 1 and "two competing" in bad["error"]


def test_mcp_tool(datadir):
    pytest.importorskip("mcp")
    import mcp_servers.prior_art_server as srv
    srv._initialized = False
    srv._engine.clear()
    assert "uncertainty_next_observation" in {t.name for t in asyncio.run(srv.list_tools())}
    res = asyncio.run(srv.call_tool("uncertainty_next_observation", {"hypotheses": HYPS, "candidates": cands()}))
    data = json.loads(res[0].text)
    assert data["ok"] and data["recommended"]["candidate"] == "pressure_gauge"
    err = asyncio.run(srv.call_tool("uncertainty_next_observation", {"hypotheses": HYPS}))
    assert "required" in err[0].text
