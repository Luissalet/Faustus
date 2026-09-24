"""src/handoff_lanes.py -- permissions-as-topology for delegation."""
from __future__ import annotations

import json
import types

import pytest

from src import handoff_lanes as hl


# ── validate() ────────────────────────────────────────────────────────────

def test_validate_empty_is_empty():
    assert hl.validate(None) == []
    assert hl.validate([]) == []


def test_validate_normalises_a_minimal_lane():
    out = hl.validate([{"from": "main", "to": "coder"}])
    assert out == [{"id": "lane1", "from": "main", "to": "coder"}]


def test_validate_keeps_every_optional_field():
    raw = [{
        "id": "lane-a", "from": "main", "to": "coder",
        "tools_allow": ["read_file", " ", "bash"], "tools_deny": ["git_push"],
        "max_depth": 2, "note": "  hello  ",
    }]
    out = hl.validate(raw)
    assert out == [{
        "id": "lane-a", "from": "main", "to": "coder",
        "tools_allow": ["read_file", "bash"], "tools_deny": ["git_push"],
        "max_depth": 2, "note": "hello",
    }]


@pytest.mark.parametrize("raw", [
    "not-a-list",
    [1],
    [{"to": "coder"}],
    [{"from": "main", "to": "not a slug!"}],
    [{"from": "main", "to": "coder", "max_depth": 0}],
    [{"from": "main", "to": "coder", "max_depth": "x"}],
    [{"from": "main", "to": "coder", "tools_allow": "bash"}],
])
def test_validate_rejects_malformed_input(raw):
    with pytest.raises(ValueError):
        hl.validate(raw)


def test_validate_rejects_duplicate_ids():
    with pytest.raises(ValueError):
        hl.validate([
            {"id": "x", "from": "main", "to": "a"},
            {"id": "x", "from": "main", "to": "b"},
        ])


def test_validate_accepts_wildcard_and_main():
    out = hl.validate([{"from": "*", "to": "*"}])
    assert out[0]["from"] == "*" and out[0]["to"] == "*"


# ── normalize_mode() ─────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    ("off", "off"), ("SHADOW", "shadow"), ("enforce", "enforce"),
    ("", "off"), (None, "off"), ("bogus", "off"),
])
def test_normalize_mode(value, expected):
    assert hl.normalize_mode(value) == expected


# ── evaluate() ───────────────────────────────────────────────────────────

def test_evaluate_no_lanes_refuses():
    d = hl.evaluate("main", "coder", [], 1, lanes=[])
    assert d.allowed is False and d.lane_id is None
    assert "no handoff lane covers" in d.reason


def test_evaluate_exact_match_allows():
    lanes = hl.validate([{"id": "l1", "from": "main", "to": "coder"}])
    d = hl.evaluate("main", "coder", ["bash"], 1, lanes=lanes)
    assert d.allowed is True and d.lane_id == "l1"
    assert d.disabled_tools == frozenset()


def test_evaluate_wildcard_from_and_to():
    lanes = hl.validate([{"id": "any", "from": "*", "to": "*"}])
    assert hl.evaluate("reviewer", "coder", [], 1, lanes=lanes).allowed
    assert hl.evaluate("main", "anything", [], 1, lanes=lanes).allowed


def test_evaluate_tools_deny_intersects_requested():
    lanes = hl.validate([{"id": "l1", "from": "main", "to": "coder", "tools_deny": ["git_push"]}])
    d = hl.evaluate("main", "coder", ["bash", "git_push"], 1, lanes=lanes)
    assert d.allowed is True
    assert d.disabled_tools == frozenset({"git_push"})


def test_evaluate_tools_allow_denies_anything_else_requested():
    lanes = hl.validate([{"id": "l1", "from": "main", "to": "coder", "tools_allow": ["bash"]}])
    d = hl.evaluate("main", "coder", ["bash", "write_file"], 1, lanes=lanes)
    assert d.allowed is True
    assert d.disabled_tools == frozenset({"write_file"})


def test_evaluate_max_depth_exceeded_refuses_with_the_lane_named():
    lanes = hl.validate([{"id": "shallow", "from": "main", "to": "coder", "max_depth": 1}])
    d = hl.evaluate("main", "coder", [], 2, lanes=lanes)
    assert d.allowed is False
    assert "shallow" in d.reason


def test_evaluate_max_depth_within_bounds_allows():
    lanes = hl.validate([{"id": "shallow", "from": "main", "to": "coder", "max_depth": 2}])
    d = hl.evaluate("main", "coder", [], 2, lanes=lanes)
    assert d.allowed is True


def test_evaluate_refusal_names_related_lanes():
    lanes = hl.validate([{"id": "l1", "from": "main", "to": "reviewer"}])
    d = hl.evaluate("main", "coder", [], 1, lanes=lanes)
    assert d.allowed is False
    assert "l1" in d.reason


def test_evaluate_first_matching_lane_wins():
    lanes = hl.validate([
        {"id": "first", "from": "main", "to": "coder", "tools_deny": ["bash"]},
        {"id": "second", "from": "*", "to": "*"},
    ])
    d = hl.evaluate("main", "coder", ["bash"], 1, lanes=lanes)
    assert d.lane_id == "first"
    assert d.disabled_tools == frozenset({"bash"})


# ── apply() -- the delegation-path entry point ──────────────────────────

class _FakePermissions:
    def __init__(self, allowed=None):
        self.allowed_tools = allowed


class _FakeRun:
    def __init__(self, agent="", agent_def=None, permissions=None):
        self.agent = agent
        self.agent_def = agent_def
        self.permissions = permissions
        self.lane_id = None
        self.lane_disabled_tools = set()


def _set_setting(monkeypatch, mode, lanes=()):
    def fake_get_setting(key, default=None):
        if key == "agent_handoff_lanes_mode":
            return mode
        if key == "agent_handoff_lanes":
            return list(lanes)
        return default
    monkeypatch.setattr("src.settings.get_setting", fake_get_setting)


def test_apply_off_is_a_pure_noop(monkeypatch):
    _set_setting(monkeypatch, "off", lanes=[{"from": "main", "to": "coder"}])
    runs = [_FakeRun(agent="coder")]
    assert hl.apply(runs, "main", 0) == ""
    assert runs[0].lane_id is None
    assert runs[0].lane_disabled_tools == set()


def test_apply_shadow_never_blocks_but_evaluates(monkeypatch, tmp_path):
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    _set_setting(monkeypatch, "shadow", lanes=[])
    runs = [_FakeRun(agent="coder")]
    assert hl.apply(runs, "main", 0) == ""
    assert runs[0].lane_id is None  # shadow never mutates the run
    log = tmp_path / "handoff_lanes" / "log.jsonl"
    assert log.exists()
    row = json.loads(log.read_text().strip().splitlines()[-1])
    assert row["mode"] == "shadow" and row["allowed"] is False


def test_apply_enforce_refuses_an_uncovered_delegation(monkeypatch, tmp_path):
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    _set_setting(monkeypatch, "enforce", lanes=[{"from": "main", "to": "reviewer"}])
    runs = [_FakeRun(agent="coder")]
    error = hl.apply(runs, "main", 0)
    assert error.startswith("handoff_lanes:")
    assert "coder" in error


def test_apply_enforce_narrows_tools_on_a_covered_run(monkeypatch, tmp_path):
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    _set_setting(monkeypatch, "enforce",
                 lanes=[{"from": "main", "to": "coder", "tools_deny": ["git_push"]}])
    runs = [_FakeRun(agent="coder", agent_def={"tools": ["bash", "git_push"]})]
    error = hl.apply(runs, "main", 0)
    assert error == ""
    assert runs[0].lane_id == "lane1"
    assert runs[0].lane_disabled_tools == {"git_push"}


def test_apply_never_raises_on_a_broken_setting(monkeypatch):
    def boom(key, default=None):
        raise RuntimeError("boom")
    monkeypatch.setattr("src.settings.get_setting", boom)
    runs = [_FakeRun(agent="coder")]
    assert hl.apply(runs, "main", 0) == ""


# ── mermaid() / graph_json() ─────────────────────────────────────────────

def test_mermaid_with_no_lanes_still_renders_a_flowchart(monkeypatch):
    monkeypatch.setattr(hl, "_agent_slugs", lambda: ["main"])
    out = hl.mermaid(lanes=[])
    assert out.startswith("flowchart LR")
    assert "main" in out


def test_mermaid_draws_one_edge_per_lane(monkeypatch):
    monkeypatch.setattr(hl, "_agent_slugs", lambda: ["main", "coder"])
    lanes = hl.validate([{"id": "l1", "from": "main", "to": "coder", "tools_deny": ["git_push"]}])
    out = hl.mermaid(lanes=lanes)
    assert "-->" in out
    assert "l1" in out


def test_graph_json_shape(monkeypatch):
    monkeypatch.setattr(hl, "_agent_slugs", lambda: ["main", "coder"])
    _set_setting_direct = None
    lanes = hl.validate([{"id": "l1", "from": "main", "to": "coder"}])
    g = hl.graph_json(lanes=lanes)
    assert {"nodes", "edges", "mode"} <= set(g.keys())
    ids = {n["id"] for n in g["nodes"]}
    assert "main" in ids and "coder" in ids
    assert g["edges"] == lanes


# ── routes smoke ─────────────────────────────────────────────────────────

@pytest.fixture()
def client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import handoff_lanes_routes

    app = FastAPI()
    app.include_router(handoff_lanes_routes.setup_handoff_lanes_routes())
    monkeypatch.setattr(handoff_lanes_routes, "require_user", lambda request: "admin")
    monkeypatch.setattr(handoff_lanes_routes, "_is_admin", lambda owner: True)
    return TestClient(app)


def test_route_get_defaults_to_off_and_empty(client, monkeypatch):
    monkeypatch.setattr("src.settings.get_setting",
                        lambda key, default=None: default)
    body = client.get("/api/handoff-lanes").json()
    assert body == {"lanes": [], "mode": "off"}


def test_route_put_validates_before_saving(client):
    resp = client.put("/api/handoff-lanes", json={"lanes": [{"to": "coder"}]})
    assert resp.status_code == 400


def test_route_put_then_get_roundtrips(client, monkeypatch, tmp_path):
    saved = {}

    def fake_load():
        return dict(saved)

    def fake_save(settings):
        saved.clear()
        saved.update(settings)

    monkeypatch.setattr("src.settings.load_settings", fake_load)
    monkeypatch.setattr("src.settings.save_settings", fake_save)
    resp = client.put("/api/handoff-lanes", json={
        "lanes": [{"from": "main", "to": "coder"}], "mode": "shadow"})
    assert resp.status_code == 200
    assert resp.json()["mode"] == "shadow"
    assert saved["agent_handoff_lanes_mode"] == "shadow"


def test_route_graph_returns_mermaid(client, monkeypatch):
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: default)
    body = client.get("/api/handoff-lanes/graph").json()
    assert "mermaid" in body and body["mermaid"].startswith("flowchart")


def test_route_test_is_a_dry_run(client, monkeypatch):
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: default)
    body = client.post("/api/handoff-lanes/test",
                       json={"from": "main", "to": "coder", "tools": ["bash"]}).json()
    assert body["allowed"] is False
    assert "reason" in body


# ── integration with subagent_tools ──────────────────────────────────────

def test_off_leaves_parse_delegation_args_and_permissions_unchanged(monkeypatch):
    """mode=off: the delegation-path hook must not alter behaviour at all."""
    from src.agent_tools import subagent_tools as sat

    _set_setting(monkeypatch, "off")
    args = sat.parse_delegation_args(json.dumps({"tasks": [{"name": "t1", "instruction": "do x"}]}))
    runs = [sat.SubagentRun(0, args["tasks"][0])]
    depth_error = sat._attach_permissions(runs, {}, None, None)
    assert depth_error == ""
    assert runs[0].permissions is None
    error = hl.apply(runs, "main", 0)
    assert error == ""
    assert runs[0].lane_id is None and runs[0].lane_disabled_tools == set()
    # worker_disabled_tools sees no lane contribution at all
    disabled = sat.worker_disabled_tools(runs[0].instruction, runs[0].permissions) | runs[0].lane_disabled_tools
    assert disabled == sat.worker_disabled_tools(runs[0].instruction, runs[0].permissions)
