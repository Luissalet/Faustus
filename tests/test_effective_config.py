"""Tests for src/effective_config.py and routes/config_routes.py (ARCH-03).

Covers: the precedence walk (turn beats model beats role/preset beats project
beats global), same-level conflict detection (a duplicate instructions file,
and the `_AGENT_RULES`/`_AGENT_PREAMBLE`/`_API_AGENT_RULES`-shaped duplicate
assignments, all three fixed as of lot 36), secret masking, hash stability
under reordering and sensitivity to a real change, the `/prompt` route
matching the real prompt assembly in `src/agent_loop.py`, and the two guards
(named-list + fully generalised AST scan) that keep every module-level `_*`
name in `src/agent_loop.py` defined exactly once.
"""
from __future__ import annotations

import os
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import effective_config as ec


# ── 1. precedence: turn > model > role/preset > project > global ──────────

def test_precedence_ladder_turn_beats_all():
    """Direct test of the ladder walker with an opinion at every one of the
    five levels: the winner must be `turn`, and `overridden` must list the
    other four, weakest last, none of them dropped."""
    levels = {
        "global": {"x": ("g", "settings:x", None)},
        "project": {"x": ("p", "project:1", None)},
        "role_preset": {"x": ("r", "agent_defs:worker", None)},
        "model": {"x": ("m", "model_load_options:k", None)},
        "turn": {"x": ("t", "turn_overrides", None)},
    }
    value = ec._pick("x", levels)
    assert value.value == "t"
    assert value.source["layer"] == "turn"
    assert [row["layer"] for row in value.overridden] == ["model", "role_preset", "project", "global"]


def test_precedence_ladder_falls_through_absent_levels():
    """A field only project and global have an opinion about: project wins,
    and the levels that said nothing (turn/model/role_preset) are absent from
    `overridden`, not silently counted as agreeing."""
    levels = {
        "global": {"y": ("g", "settings:y", None)},
        "project": {"y": ("p", "project:1", None)},
        "role_preset": {},
        "model": {},
        "turn": {},
    }
    value = ec._pick("y", levels)
    assert value.value == "p"
    assert value.source["layer"] == "project"
    assert [row["layer"] for row in value.overridden] == ["global"]


def test_compile_effective_checkpoints_project_beats_global_and_turn_beats_project(monkeypatch):
    """End-to-end (not just the internal walker): a project's own `checkpoints`
    knob overrides the global default, and an explicit turn override beats
    both — using the real `services.projects.agent_options` shape."""
    project = {"id": "proj1", "checkpoints": False, "workspace": ""}
    monkeypatch.setattr(ec, "_project_and_workspace", lambda owner, sid, pid: (project, ""))
    monkeypatch.setattr(ec, "_session_field", lambda sid, col: "")

    cfg = ec.compile_effective(owner="u", session_id="s1", project_id="proj1")
    row = cfg.values["checkpoints"]
    assert row.value is False
    assert row.source["layer"] == "project"
    assert any(o["layer"] == "global" for o in row.overridden)

    cfg2 = ec.compile_effective(owner="u", session_id="s1", project_id="proj1",
                                 turn_overrides={"checkpoints": True})
    row2 = cfg2.values["checkpoints"]
    assert row2.value is True
    assert row2.source["layer"] == "turn"
    assert any(o["layer"] == "project" and o["value"] is False for o in row2.overridden)


# ── 2. conflicts ────────────────────────────────────────────────────────

def test_conflict_detected_for_duplicate_instructions_files(tmp_path, monkeypatch):
    """Two candidate instruction files in the same workspace (AGENTS.md AND
    CLAUDE.md) is a same-level conflict: `project_instructions.find_file`
    picks the first silently, so it must show up in `conflicts`, not just in
    the winning value."""
    (tmp_path / "AGENTS.md").write_text("# repo rules\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("# other rules\n", encoding="utf-8")
    monkeypatch.setattr(ec, "_project_and_workspace", lambda owner, sid, pid: (None, str(tmp_path)))
    monkeypatch.setattr(ec, "_session_field", lambda sid, col: "")

    cfg = ec.compile_effective(owner="u", session_id="s1")
    hits = [c for c in cfg.conflicts if c["field"] == "project_instructions_text"]
    assert len(hits) == 1
    assert hits[0]["winner"].endswith("AGENTS.md")
    assert len(hits[0]["candidates"]) == 2
    # And the value that DID win is still the AGENTS.md text — silence is
    # replaced by a visible conflict, not by refusing to resolve at all.
    assert cfg.values["project_instructions_text"].value.startswith("# repo rules")


def test_agent_rules_duplication_conflicts_reports_none_now_fixed():
    """All three named rule blocks are fixed as of the lot-36 integration
    (see that lot's final report): `_AGENT_RULES` was already fixed before;
    `_AGENT_PREAMBLE` and `_API_AGENT_RULES` are now each assigned exactly
    once at module scope in src/agent_loop.py too, so the named-list
    detector has nothing left to report."""
    conflicts = ec.agent_rules_duplication_conflicts()
    names = {c["field"] for c in conflicts}
    assert not names & {"_AGENT_RULES", "_AGENT_PREAMBLE", "_API_AGENT_RULES"}
    for c in conflicts:
        assert len(c["candidates"]) >= 2


def test_no_module_level_name_is_assigned_twice_in_agent_loop():
    """Generalised guard (lot 36): `agent_rules_duplication_conflicts()`
    only ever watches the fixed list of names in `ec._DUP_RULE_NAMES` — it
    would stay silent about a FOURTH `_something = ...` assigned twice
    tomorrow, exactly the blind spot that let `_AGENT_PREAMBLE`/
    `_API_AGENT_RULES` go unnoticed for a while after `_AGENT_RULES` alone
    was fixed. This scans every direct module-level assignment target in
    src/agent_loop.py itself (not routed through `ec`'s curated list) and
    fails for ANY `_`-prefixed name assigned more than once at that level —
    so a future duplicate of any shape trips a test even if nobody
    remembers to add its name to `_DUP_RULE_NAMES`.

    Deliberately module-scope-only (`tree.body`, not a full walk): a name
    reassigned inside a function, or inside a top-level `try/except`
    fallback (e.g. `try: X = a\nexcept ImportError: X = b`), is ordinary
    Python control flow, not the silent same-level precedence bug this
    guards against."""
    import ast
    import inspect
    from src import agent_loop

    source = inspect.getsource(agent_loop)
    tree = ast.parse(source)
    lines_by_name: dict[str, list[int]] = {}
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = [t for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target]
        for target in targets:
            if target.id.startswith("_"):
                lines_by_name.setdefault(target.id, []).append(node.lineno)

    duplicated = {name: lines for name, lines in lines_by_name.items() if len(lines) > 1}
    assert duplicated == {}, (
        "module-level name(s) assigned more than once in src/agent_loop.py "
        f"(the last assignment wins in silence): {duplicated}"
    )


# ── 3. secrets masked ──────────────────────────────────────────────────

def test_secret_masked_in_resolved_value(tmp_path, monkeypatch):
    (tmp_path / "AGENTS.md").write_text(
        "# rules\napi_key: sk-THISVALUEMUSTNEVERAPPEAR\n", encoding="utf-8",
    )
    monkeypatch.setattr(ec, "_project_and_workspace", lambda owner, sid, pid: (None, str(tmp_path)))
    monkeypatch.setattr(ec, "_session_field", lambda sid, col: "")

    cfg = ec.compile_effective(owner="u", session_id="s1")
    text = cfg.values["project_instructions_text"].value
    assert "THISVALUEMUSTNEVERAPPEAR" not in text
    assert "***" in text
    # The whole payload (what a route would actually return) must be clean too.
    import json
    assert "THISVALUEMUSTNEVERAPPEAR" not in json.dumps(cfg.to_dict())


def test_secret_masked_in_turn_override_value():
    cfg = ec.compile_effective(
        owner="u", turn_overrides={"review_model": "client_secret: sk-turnleak000"},
    )
    # review_model isn't a resolved field, so exercise masking directly on a
    # field this module DOES resolve via a turn override carrying a secret shape.
    masked = ec._mask_value("client_secret: sk-turnleak000")
    assert "sk-turnleak000" not in masked
    assert "***" in masked


# ── 4. hash stability ───────────────────────────────────────────────────

def test_hash_stable_under_reorder_and_sensitive_to_change():
    cfg1 = ec.compile_effective(owner="u", model="m1",
                                 turn_overrides={"max_rounds": 5, "checkpoints": True})
    cfg2 = ec.compile_effective(owner="u", model="m1",
                                 turn_overrides={"checkpoints": True, "max_rounds": 5})
    cfg3 = ec.compile_effective(owner="u", model="m1",
                                 turn_overrides={"max_rounds": 6, "checkpoints": True})
    assert cfg1.hash == cfg2.hash
    assert cfg1.hash != cfg3.hash
    assert len(cfg1.hash) == 64  # sha256 hex, like src/contracts/base.fingerprint


# ── 5. /prompt route == the real agent_loop assembly ───────────────────

@pytest.fixture()
def config_client(monkeypatch):
    from routes.config_routes import setup_config_routes
    monkeypatch.setattr("routes.config_routes.require_admin", lambda request: None)
    app = FastAPI()
    app.include_router(setup_config_routes())
    return TestClient(app)


def test_prompt_route_matches_real_agent_loop_assembly(config_client, monkeypatch):
    from src import agent_loop
    from src.tool_security import blocked_tools_for_owner

    disabled = set(blocked_tools_for_owner(""))
    expected_prompt, expected_blocks = agent_loop.base_prompt_with_origin(
        disabled_tools=disabled, needs_admin=False, relevant_tools=None,
        compact=False, owner=None,
    )

    resp = config_client.get("/api/config/effective/prompt")
    assert resp.status_code == 200
    body = resp.json()
    assert body["prompt_chars"] == len(expected_prompt)
    assert [b["name"] for b in body["blocks"]] == [b["name"] for b in expected_blocks]
    assert [b["kind"] for b in body["blocks"]] == [b["kind"] for b in expected_blocks]


def test_prompt_route_reacts_to_disabled_tools(config_client, monkeypatch):
    """Changing what the owner's tools policy disables changes the block set
    the route reports — proving it is reading the real, live selection and
    not a frozen copy."""
    from src import tool_security

    def _blocked(owner):
        return {"web_search", "web_fetch"}
    monkeypatch.setattr("src.effective_config.blocked_tools_for_owner", _blocked, raising=False)
    monkeypatch.setattr(tool_security, "blocked_tools_for_owner", _blocked)

    resp = config_client.get("/api/config/effective/prompt")
    names = {b["name"] for b in resp.json()["blocks"]}
    assert "web_search" not in names


def test_effective_route_returns_no_secret(tmp_path, monkeypatch):
    from routes.config_routes import setup_config_routes
    from src import effective_config as ec_mod

    (tmp_path / "AGENTS.md").write_text("api_key: sk-ROUTELEAKCHECK\n", encoding="utf-8")
    monkeypatch.setattr(ec_mod, "_project_and_workspace", lambda owner, sid, pid: (None, str(tmp_path)))
    monkeypatch.setattr(ec_mod, "_session_field", lambda sid, col: "")
    monkeypatch.setattr("routes.config_routes.require_admin", lambda request: None)
    monkeypatch.setattr("routes.config_routes._owner_for", lambda request: "u")
    monkeypatch.setattr("routes.config_routes._verify_session", lambda request, sid: None)

    app = FastAPI()
    app.include_router(setup_config_routes())
    client = TestClient(app)
    resp = client.get("/api/config/effective", params={"session": "s1"})
    assert resp.status_code == 200
    assert "sk-ROUTELEAKCHECK" not in resp.text


# ── 6. the guard: _AGENT_RULES defined exactly once ────────────────────

_AGENT_LOOP_PATH = os.path.join(os.path.dirname(__file__), "..", "src", "agent_loop.py")
_AGENT_RULES_ASSIGN_RE = re.compile(r"(?m)^_AGENT_RULES\s*=")


def test_agent_rules_defined_exactly_once():
    """ARCH-03's own bug: `_AGENT_RULES` was assigned twice at module scope in
    src/agent_loop.py, the second silently shadowing the first. This is the
    regression guard the lote asked for.

    Verified to fail without the fix: temporarily re-inserting a second
    `_AGENT_RULES = "..."` assignment makes this assertion fail (checked by
    hand while writing this test — copy of the file, second assignment
    re-added, `pytest` run, restored; see the final report)."""
    with open(os.path.abspath(_AGENT_LOOP_PATH), encoding="utf-8") as f:
        source = f.read()
    hits = _AGENT_RULES_ASSIGN_RE.findall(source)
    assert len(hits) == 1, (
        f"_AGENT_RULES is assigned {len(hits)} times at module scope in src/agent_loop.py; "
        "expected exactly 1 (the second assignment used to silently shadow the first)"
    )


def test_agent_loop_live_agent_rules_text_preserved():
    """The FIX kept the text of the LIVE (second, previously-winning) block,
    per the lote's own instruction ("unifica en UNA definición conservando el
    texto de la viva") — not the dead first one. The live block is the one
    carrying the "ask before a new system" rule
    (tests/test_agent_asks_before_a_new_system.py already pins its behaviour;
    this pins that its TEXT specifically is what survived)."""
    from src import agent_loop
    assert "A NEW SYSTEM IS DECIDED WITH THE USER FIRST" in agent_loop._AGENT_RULES
    # The dead first block's distinctive UI-conventions section must be gone.
    assert "click to switch" not in agent_loop._AGENT_RULES
