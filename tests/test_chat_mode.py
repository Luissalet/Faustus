"""Lean mode, pinned per chat (src/chat_mode.py and what hangs off it).

Real `SessionManager` over a temp SQLite file, as in test_behavior_mode_routes.
No model anywhere: the prompt tests build the system prompt through
`_build_system_prompt` and compare the exact bytes with the prompt-audit
helpers, because the property that matters is "the prefix a local server has
cached did not change", not "a helper returned the right dict".
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.testclient import TestClient

from core.database import Base
from src import chat_mode
from src.context_engine.prompt_audit import diff_prompts, fingerprint_prompt


# -- fixtures ---------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path, monkeypatch):
    import core.database as db_mod
    import core.session_manager as sm_mod
    import routes.session_routes as sr_mod

    url = "sqlite:///" + (tmp_path / "chat_mode.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", Session)
    monkeypatch.setattr(sm_mod, "SessionLocal", Session)
    monkeypatch.setattr(sr_mod, "SessionLocal", Session)
    yield Session
    engine.dispose()


@pytest.fixture()
def sm(db):
    from core.session_manager import SessionManager
    return SessionManager()


@pytest.fixture()
def settings(monkeypatch):
    """Override a few settings; every other key keeps its real value."""
    import src.settings as settings_mod
    values: dict = {}
    real = settings_mod.get_setting

    def patched(key, default=None):
        if key in values:
            return values[key]
        return real(key, default)

    monkeypatch.setattr(settings_mod, "get_setting", patched)
    return values


def _mk(sm, sid, owner="alice"):
    sm.create_session(session_id=sid, name=sid, endpoint_url="http://ep", model="m1", owner=owner)


# -- vocabulary ---------------------------------------------------------------------

@pytest.mark.parametrize("word,expected", [
    ("lean", "lean"), ("LEAN", "lean"), (" ligero ", "lean"), ("light", "lean"),
    ("normal", "normal"), ("Full", "normal"), ("completo", "normal"),
    ("terse", None), ("", None), (None, None), (3, None),
])
def test_normalize_accepts_both_languages(word, expected):
    assert chat_mode.normalize(word) == expected


def test_default_mode_reads_the_setting_and_falls_back_to_normal(settings):
    assert chat_mode.default_mode() == "normal"
    settings["agent_default_chat_mode"] = "lean"
    assert chat_mode.default_mode() == "lean"
    settings["agent_default_chat_mode"] = "nonsense"
    assert chat_mode.default_mode() == "normal"


def test_authorization_settings_are_never_pinned():
    pinned = set(chat_mode.PINNED_SETTINGS)
    for key in ("disabled_tools", "builtin_tool_overrides", "autonomy_preset", "agent_workspace_trust"):
        assert key not in pinned
    assert not any("approval" in k or "autonomy" in k or "permission" in k for k in pinned)


# -- persistence --------------------------------------------------------------------

def test_set_mode_persists_with_the_session(db, sm, settings):
    _mk(sm, "s1")
    view = chat_mode.set_mode("s1", "lean")
    assert view["mode"] == "lean" and view["persisted"]
    # A fresh read, as after a restart: only the database remembers it.
    stored = chat_mode.stored_profile("s1")
    assert stored["mode"] == "lean" and stored["v"] == chat_mode.PROFILE_VERSION
    assert chat_mode.describe("s1")["mode"] == "lean"
    assert chat_mode.describe("s1")["stored"] is True


def test_describe_never_pins(db, sm, settings):
    _mk(sm, "s1")
    out = chat_mode.describe("s1")
    assert out["stored"] is False and out["mode"] == "normal" and out["pinned"] is False
    assert chat_mode.stored_profile("s1") is None


def test_set_mode_rejects_an_unknown_mode(db, sm):
    _mk(sm, "s1")
    with pytest.raises(ValueError):
        chat_mode.set_mode("s1", "turbo")


def test_a_session_with_no_row_is_not_persisted(db, settings):
    view = chat_mode.turn_profile("ghost-id")
    assert view["persisted"] is False and view["mode"] == "normal"
    assert chat_mode.stored_profile("ghost-id") is None
    assert chat_mode.turn_profile("")["persisted"] is False


# -- pinning ------------------------------------------------------------------------

def test_first_turn_pins_the_default_and_a_later_default_change_does_not_reach_it(db, sm, settings):
    _mk(sm, "old")
    _mk(sm, "new")
    settings["agent_default_chat_mode"] = "lean"
    first = chat_mode.turn_profile("old")
    assert first["mode"] == "lean" and first["persisted"]
    settings["agent_default_chat_mode"] = "normal"
    assert chat_mode.turn_profile("old")["mode"] == "lean"      # pinned
    assert chat_mode.turn_profile("new")["mode"] == "normal"    # a new chat takes the new default


def test_pinned_values_do_not_follow_a_global_change_but_a_new_chat_does(db, sm, settings):
    _mk(sm, "a")
    _mk(sm, "b")
    settings["agent_harness_checks"] = True
    settings["agent_repo_map_tokens"] = 1500
    pa = chat_mode.turn_profile("a")
    assert pa["pinned"]["agent_harness_checks"] is True
    settings["agent_harness_checks"] = False
    settings["agent_repo_map_tokens"] = 300
    # still the old values for the pinned chat, whenever it is read again
    pa2 = chat_mode.turn_profile("a")
    assert pa2["pinned"]["agent_harness_checks"] is True and pa2["pinned_sha256"] == pa["pinned_sha256"]
    token = chat_mode.activate(pa2)
    try:
        assert chat_mode.get_setting("agent_harness_checks", None) is True
        assert chat_mode.get_setting("agent_repo_map_tokens", None) == 1500
        # a key that is not pinned keeps following the live settings
        settings["some_unpinned_key"] = "live"
        assert chat_mode.get_setting("some_unpinned_key", None) == "live"
    finally:
        chat_mode.deactivate(token)
    # outside a turn the same call reads the live value
    assert chat_mode.get_setting("agent_harness_checks", None) is False
    # a new chat pins the new values
    pb = chat_mode.turn_profile("b")
    assert pb["pinned"]["agent_harness_checks"] is False and pb["pinned"]["agent_repo_map_tokens"] == 300
    assert pb["pinned_sha256"] != pa["pinned_sha256"]


def test_set_mode_re_resolves_the_pin_from_the_live_settings(db, sm, settings):
    _mk(sm, "a")
    settings["agent_harness_checks"] = True
    chat_mode.turn_profile("a")
    settings["agent_harness_checks"] = False
    assert chat_mode.turn_profile("a")["pinned"]["agent_harness_checks"] is True
    view = chat_mode.set_mode("a", "lean")
    assert view["pinned"]["agent_harness_checks"] is False
    assert chat_mode.turn_profile("a")["pinned"]["agent_harness_checks"] is False


def test_profile_setting_reads_a_profile_without_a_turn(settings):
    settings["behavior_mode_default"] = "terse"
    assert chat_mode.profile_setting({"pinned": {"behavior_mode_default": "mentor"}}, "behavior_mode_default") == "mentor"
    assert chat_mode.profile_setting(None, "behavior_mode_default") == "terse"
    assert chat_mode.profile_setting({"pinned": {}}, "behavior_mode_default") == "terse"


def test_activation_resets_and_a_run_with_no_profile_sees_none():
    lean = {"mode": "lean", "pinned": {"agent_harness_checks": False}}
    assert not chat_mode.is_lean()
    outer = chat_mode.activate(lean)
    assert chat_mode.is_lean()
    inner = chat_mode.activate(None)      # a sub-agent / scheduled run inside a lean chat
    assert not chat_mode.is_lean() and chat_mode.active_profile() is None
    chat_mode.deactivate(inner)
    assert chat_mode.is_lean()
    chat_mode.deactivate(outer)
    assert not chat_mode.is_lean() and chat_mode.metrics_block() is None


def test_metrics_block_reports_the_active_profile():
    token = chat_mode.activate({"mode": "lean", "pinned": {"a": 1}, "pinned_at": 5})
    try:
        block = chat_mode.metrics_block()
    finally:
        chat_mode.deactivate(token)
    assert block["mode"] == "lean" and block["pinned_keys"] == 1
    assert block["pinned_sha256"] == chat_mode.pinned_sha256({"a": 1})


# -- the tool set -------------------------------------------------------------------

def test_lean_tools_never_add_authority_and_keep_what_was_forced():
    offered = {"read_file", "bash", "manage_skills", "send_email", "mcp__x__y", "update_plan"}
    assert chat_mode.lean_tool_names(offered, disabled=()) == {"read_file", "bash"}
    # a denied core tool stays denied
    assert chat_mode.lean_tool_names(offered, disabled={"bash"}) == {"read_file"}
    # selection offered no shell: lean does not invent one
    assert "bash" not in chat_mode.lean_tool_names({"read_file"}, disabled=())
    # a delegation the user typed survives
    assert chat_mode.lean_tool_names(offered, disabled=(), keep={"delegate_agents"}) == {"read_file", "bash", "delegate_agents"}
    # but a forced tool that is denied does not
    assert "delegate_agents" not in chat_mode.lean_tool_names(offered, disabled={"delegate_agents"}, keep={"delegate_agents"})
    # selection that offered everything (None) falls to the core set
    assert chat_mode.lean_tool_names(None, disabled={"python"}) == set(chat_mode.LEAN_CORE_TOOLS) - {"python"}


def test_the_core_set_has_files_shell_python_and_web():
    core = chat_mode.LEAN_CORE_TOOLS
    assert {"read_file", "write_file", "edit_file", "bash", "python", "web_search"} <= core
    assert not ({"manage_skills", "lookup_tools", "delegate_agents", "update_plan", "inspect_image"} & core)

# -- the prompt ---------------------------------------------------------------------

def _build(workspace, tools=("read_file", "edit_file", "bash", "manage_skills", "update_plan", "lookup_tools"),
           user="hello"):
    from src import agent_loop as al
    built = al._build_system_prompt(
        [{"role": "user", "content": user}], "qwen3:8b", None, None,
        relevant_tools=set(tools), workspace=str(workspace), owner="alice",
        session_id="s1", suppress_skills=True,
    )
    return built[0] if isinstance(built, tuple) else built


def _system_text(messages):
    return "\n".join(m["content"] for m in messages
                     if m.get("role") == "system" and isinstance(m.get("content"), str))


def _turn_fingerprint(profile, workspace, **kw):
    token = chat_mode.activate(profile)
    try:
        return fingerprint_prompt(_build(workspace, **kw))
    finally:
        chat_mode.deactivate(token)


def test_a_pinned_chat_keeps_its_prefix_when_a_global_setting_changes(db, sm, settings, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _mk(sm, "pinned")
    _mk(sm, "fresh")
    settings["agent_harness_checks"] = True
    first = _turn_fingerprint(chat_mode.turn_profile("pinned"), ws)       # pins on the first turn
    settings["agent_harness_checks"] = False                               # a change made elsewhere
    second = _turn_fingerprint(chat_mode.turn_profile("pinned"), ws)
    assert second["prompt_sha256"] == first["prompt_sha256"]
    assert [b["prefix_sha256"] for b in second["blocks"]] == [b["prefix_sha256"] for b in first["blocks"]]
    assert diff_prompts(first, second)["prefix_intact"] is True
    # a chat that starts now pins the new value, and its prefix differs from the first chat's
    fresh = _turn_fingerprint(chat_mode.turn_profile("fresh"), ws)
    assert fresh["prompt_sha256"] != first["prompt_sha256"]
    assert diff_prompts(first, fresh)["prefix_intact"] is False
    # without a profile the live setting rules, so the change is exactly what the pin held back
    unpinned = _turn_fingerprint(None, ws)
    assert unpinned["prompt_sha256"] == fresh["prompt_sha256"]


def test_lean_chat_system_prompt_drops_the_optional_blocks(db, sm, settings, tmp_path):
    from src import agent_loop as al
    ws = tmp_path / "ws"
    ws.mkdir()
    settings["agent_harness_checks"] = True
    core = ("read_file", "edit_file", "bash")      # what the loop leaves a lean turn
    normal = _system_text(_build(ws, tools=core))
    token = chat_mode.activate({"mode": "lean", "pinned": {}})
    try:
        lean = _system_text(_build(ws, tools=core))
    finally:
        chat_mode.deactivate(token)
    big_task = al._big_task_strategy_block()
    strategy = al._strategy_block("alice", "s1", "hello")
    assert big_task and big_task in normal and big_task not in lean
    assert strategy and strategy in normal and strategy not in lean
    # the catalog helper and the plan tracker are not part of a plain chat
    assert "```lookup_tools```" in normal and "```lookup_tools```" not in lean
    assert "```update_plan```" in normal and "```update_plan```" not in lean
    # ask_user stays, and the core rules are still there
    assert "```ask_user```" in lean
    assert len(lean) < len(normal)


def test_lean_and_normal_do_not_share_a_cached_base_prompt(db, sm, settings, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    normal_first = _system_text(_build(ws))
    token = chat_mode.activate({"mode": "lean", "pinned": {}})
    try:
        _build(ws)
    finally:
        chat_mode.deactivate(token)
    assert _system_text(_build(ws)) == normal_first


# -- the loop's entry point -----------------------------------------------------------

def test_stream_agent_loop_activates_the_chat_profile_for_the_body_and_resets(monkeypatch, settings):
    import asyncio
    from src import agent_loop as al

    seen = []

    async def stub(endpoint_url, model, messages, harness_options=None, session_id=None, owner=None):
        seen.append((chat_mode.is_lean(), chat_mode.get_setting("agent_harness_checks", None)))
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "_stream_agent_loop_body", stub)
    settings["agent_harness_checks"] = False
    lean = {"mode": "lean", "pinned": {"agent_harness_checks": True}}

    async def run(**kw):
        return [c async for c in al.stream_agent_loop("http://ep", "m", [], **kw)]

    asyncio.run(run(harness_options={"chat_profile": lean}, session_id="s"))
    assert seen[-1] == (True, True)               # lean, and the pinned value, not the live False
    assert not chat_mode.is_lean()                # reset once the turn is over
    asyncio.run(run(session_id="s"))              # a run with no profile
    assert seen[-1] == (False, False)

    async def nested():
        token = chat_mode.activate(lean)
        try:
            await run(session_id="sub")           # e.g. a sub-agent inside a lean chat
        finally:
            chat_mode.deactivate(token)
    asyncio.run(nested())
    assert seen[-1] == (False, False)             # never inherits its parent's lean mode


# -- the API ------------------------------------------------------------------------

def _fake_user(request):
    return request.headers.get("x-test-user", "alice")


@pytest.fixture()
def client(db, sm, settings, monkeypatch):
    import routes.behavior_mode_routes as bmr_mod
    import routes.session_routes as sr_mod

    monkeypatch.setattr(sr_mod, "effective_user", _fake_user)
    monkeypatch.setattr(bmr_mod, "effective_user", _fake_user)
    app = FastAPI()
    app.include_router(bmr_mod.setup_behavior_mode_routes(sm))
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_get_reports_the_default_without_pinning(client, sm, settings):
    _mk(sm, "s1")
    settings["agent_default_chat_mode"] = "lean"
    body = client.get("/api/sessions/s1/mode", headers={"x-test-user": "alice"}).json()
    assert body["mode"] == "lean" and body["stored"] is False and body["default"] == "lean"
    assert body["pinned"] is False
    assert {d["id"] for d in body["drops"]} >= {"skills_index", "repo_map", "instincts", "mcp_tools"}
    assert chat_mode.stored_profile("s1") is None


def test_post_stores_the_mode_and_get_reads_it_back(client, sm, settings):
    _mk(sm, "s1")
    r = client.post("/api/sessions/s1/mode", json={"mode": "lean"}, headers={"x-test-user": "alice"})
    assert r.status_code == 200 and r.json()["mode"] == "lean" and r.json()["pinned"] is True
    settings["agent_default_chat_mode"] = "normal"
    got = client.get("/api/sessions/s1/mode", headers={"x-test-user": "alice"}).json()
    assert got["mode"] == "lean" and got["stored"] is True and got["pinned_keys"]
    assert client.post("/api/sessions/s1/mode", json={"mode": "ligero"}, headers={"x-test-user": "alice"}).json()["mode"] == "lean"
    assert client.post("/api/sessions/s1/mode", json={"mode": "normal"}, headers={"x-test-user": "alice"}).json()["mode"] == "normal"


def test_post_rejects_an_unknown_mode_with_a_flat_error(client, sm):
    _mk(sm, "s1")
    r = client.post("/api/sessions/s1/mode", json={"mode": "turbo"}, headers={"x-test-user": "alice"})
    assert r.status_code == 400
    assert r.json() == {"error": "mode must be 'lean' or 'normal'.", "error_class": "chat_mode.invalid"}


def test_another_users_session_answers_like_a_missing_one(client, sm):
    _mk(sm, "s1", owner="alice")
    assert client.get("/api/sessions/s1/mode", headers={"x-test-user": "bob"}).status_code == 404
    assert client.post("/api/sessions/s1/mode", json={"mode": "lean"}, headers={"x-test-user": "bob"}).status_code == 404
    assert client.get("/api/sessions/ghost/mode", headers={"x-test-user": "alice"}).status_code == 404
    assert chat_mode.stored_profile("s1") is None


def test_audit_records_a_system_prefix_hash_that_ignores_the_user_turn():
    from src.context_engine.prompt_audit import PromptAuditRecorder

    def record(system, user):
        rec = PromptAuditRecorder(turn_id="t", session_id="s")
        return rec.observe_round(1, [{"role": "system", "content": system}, {"role": "user", "content": user}], [])

    a = record("You are Faustus. Rules: x", "capital of Italy?")
    b = record("You are Faustus. Rules: x", "capital of Spain?")
    c = record("You are Faustus. Rules: y", "capital of Italy?")
    assert a["system_prefix_sha256"] and len(a["system_prefix_sha256"]) == 16
    assert a["system_prefix_sha256"] == b["system_prefix_sha256"]
    assert a["system_prefix_sha256"] != c["system_prefix_sha256"]
    assert a["prompt_sha256"] != b["prompt_sha256"]
