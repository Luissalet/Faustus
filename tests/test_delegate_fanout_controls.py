"""Wide fan-outs: tiers, an enforced model list, read-only reviewers,
per-task bounds and an owner-set width."""
import json

import pytest

from src.agent_tools import subagent_tools as st


@pytest.fixture
def settings(monkeypatch):
    values = {}
    monkeypatch.setattr(st, "_setting", lambda k, d=None: values.get(k, d))
    return values


def _parse(tasks, **extra):
    return st.parse_delegation_args(json.dumps({"tasks": tasks, **extra}), workspace=None)


def test_width_follows_the_setting(settings):
    settings["agent_subagent_max_tasks"] = 30
    out = _parse([{"instruction": f"review page {i}", "read_only": True} for i in range(40)])
    assert len(out["tasks"]) == 30 and out["dropped_tasks"] == 10


def test_default_width_is_unchanged(settings):
    out = _parse([f"task {i}" for i in range(6)])
    assert len(out["tasks"]) == st.MAX_SUBAGENTS and out["dropped_tasks"] == 2


def test_tier_maps_to_the_owners_model_and_endpoint(settings):
    settings["agent_subagent_tier_fast"] = "cloud-1|cheap-model"
    row = _parse([{"instruction": "x", "tier": "fast"}, "y"])["tasks"][0]
    assert row["model"] == "cheap-model" and row["endpoint_id"] == "cloud-1" and row["tier"] == "fast"


def test_model_list_is_enforced_before_start(settings):
    settings["agent_subagent_allowed_models"] = "cheap-model, big/model-b"
    out = _parse([{"instruction": "ok", "model": "cheap-model"},
                  {"instruction": "no", "model": "expensive-model"},
                  {"instruction": "inherits"}])
    assert [t["instruction"] for t in out["tasks"]] == ["ok", "inherits"]
    assert out["refused_tasks"][0]["model"] == "expensive-model"
    with pytest.raises(ValueError, match="refused"):
        _parse([{"instruction": "no", "model": "expensive-model"}])


def test_read_only_reviewer_contract(settings):
    row = _parse([{"instruction": "Check page A against the source", "read_only": True, "max_findings": 3}])["tasks"][0]
    assert row["read_only"] is True and row["max_rounds"] == st.REVIEWER_MAX_ROUNDS
    assert row.get("agent") == "explorer" or row.get("agent_def")
    assert "at most 3 findings" in row["instruction"] and "do not edit" in row["instruction"]


def test_per_task_bounds(settings):
    row = _parse([{"instruction": "x", "max_rounds": 99, "timeout_s": 5}])["tasks"][0]
    assert row["max_rounds"] == 40 and row["timeout_s"] == st.MIN_WORKER_TIMEOUT_S
