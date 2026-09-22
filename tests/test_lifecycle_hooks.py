"""Tests for src/lifecycle_hooks.py — user-configurable lifecycle automation.

Covers: hook validation, match semantics (tool/path glob alternation,
command/text regex, scope=project without a workspace), template rendering
(including shell-quoting and unknown placeholders), real subprocess
execution (echo + a timeout via sleep), truncation, inject/warn levels,
`attach_to_result` never clobbering existing keys, `render_context` shape,
idempotent presets, the admin API routes, and log rotation.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

import src.lifecycle_hooks as lh
from src.lifecycle_hooks import (
    EVENTS,
    HookError,
    HookResult,
    apply_preset,
    attach_to_result,
    evaluate,
    preset,
    render_command,
    render_context,
    render_text,
    run,
    run_async,
    validate_hooks,
)

pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"), reason="subprocess tests use POSIX shell builtins",
)


def _set_hooks(monkeypatch, hooks):
    monkeypatch.setattr(lh, "_load_hooks", lambda: [h for h in hooks])


def _set_setting(monkeypatch, overrides):
    base = {
        "lifecycle_hooks_enabled": True,
        "lifecycle_hooks_command_timeout_seconds": lh.DEFAULT_COMMAND_TIMEOUT_S,
        "lifecycle_hooks_total_timeout_seconds": lh.DEFAULT_TOTAL_TIMEOUT_S,
    }
    base.update(overrides)
    monkeypatch.setattr(lh, "_get_setting", lambda key, default=None: base.get(key, default))


def _hook(**kw):
    base = {
        "id": "h1", "enabled": True, "event": "post_tool", "name": "H1",
        "match": {}, "action": "warn", "command": None, "text": "hi",
        "timeout_s": None, "max_output_chars": 4000, "scope": "global", "note": "",
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def test_validate_hooks_accepts_a_well_formed_list():
    hooks = [{"id": "h1", "event": "post_tool", "action": "warn", "text": "watch out",
              "match": {"path": "*.py"}}]
    checked = validate_hooks(hooks)
    assert checked[0]["id"] == "h1"
    assert checked[0]["enabled"] is True
    assert checked[0]["scope"] == "global"


@pytest.mark.parametrize("bad,msg", [
    ({"event": "post_tool", "action": "warn", "text": "x"}, "id"),
    ({"id": "H1", "event": "post_tool", "action": "warn", "text": "x"}, "slug"),
    ({"id": "h1", "event": "bogus", "action": "warn", "text": "x"}, "event"),
    ({"id": "h1", "event": "post_tool", "action": "bogus", "text": "x"}, "action"),
    ({"id": "h1", "event": "post_tool", "action": "command"}, "command"),
    ({"id": "h1", "event": "post_tool", "action": "warn"}, "text"),
    ({"id": "h1", "event": "post_tool", "action": "warn", "text": "x", "match": {"nope": "1"}}, "match key"),
    ({"id": "h1", "event": "post_tool", "action": "warn", "text": "x", "match": {"command": "("}}, "regex"),
    ({"id": "h1", "event": "post_tool", "action": "warn", "text": "x", "timeout_s": 999}, "timeout_s"),
    ({"id": "h1", "event": "post_tool", "action": "warn", "text": "x", "scope": "nope"}, "scope"),
])
def test_validate_hooks_rejects_bad_hooks(bad, msg):
    with pytest.raises(HookError) as exc:
        validate_hooks([bad])
    assert msg in str(exc.value)


def test_validate_hooks_rejects_duplicate_ids():
    hook = {"id": "h1", "event": "post_tool", "action": "warn", "text": "x"}
    with pytest.raises(HookError, match="duplicate"):
        validate_hooks([dict(hook), dict(hook)])


def test_validate_hooks_rejects_non_list():
    with pytest.raises(HookError, match="list"):
        validate_hooks({"not": "a list"})


def test_validate_hooks_leaves_timeout_none_when_unset():
    checked = validate_hooks([{"id": "h1", "event": "pre_tool", "action": "command",
                                "command": "echo hi"}])
    assert checked[0]["timeout_s"] is None


# ---------------------------------------------------------------------------
# match semantics
# ---------------------------------------------------------------------------

def test_tool_glob_alternation(monkeypatch):
    _set_hooks(monkeypatch, [_hook(match={"tool": "edit_file|write_file"})])
    assert [h["id"] for h in evaluate("post_tool", {"tool": "write_file"})] == ["h1"]
    assert [h["id"] for h in evaluate("post_tool", {"tool": "edit_file"})] == ["h1"]
    assert evaluate("post_tool", {"tool": "bash"}) == []


def test_path_glob_alternation(monkeypatch):
    _set_hooks(monkeypatch, [_hook(match={"path": "*.ts|*.tsx"})])
    assert [h["id"] for h in evaluate("post_tool", {"paths": ["src/App.tsx"]})] == ["h1"]
    assert evaluate("post_tool", {"paths": ["src/app.py"]}) == []
    assert evaluate("post_tool", {"paths": []}) == []


def test_command_regex_match_is_search_not_fullmatch(monkeypatch):
    _set_hooks(monkeypatch, [_hook(match={"command": r"--no-verify"})])
    assert [h["id"] for h in evaluate("post_tool", {"command": "git commit --no-verify -m x"})] == ["h1"]
    assert evaluate("post_tool", {"command": "git commit -m x"}) == []


def test_text_regex_matches_user_message(monkeypatch):
    _set_hooks(monkeypatch, [_hook(event="turn_start", match={"text": r"(?i)deploy"})])
    assert [h["id"] for h in evaluate("turn_start", {"user_message": "please deploy this"})] == ["h1"]
    assert evaluate("turn_start", {"user_message": "please review this"}) == []


def test_scope_project_requires_a_workspace(monkeypatch):
    _set_hooks(monkeypatch, [_hook(scope="project")])
    assert evaluate("post_tool", {}) == []
    assert evaluate("post_tool", {"workspace": "/tmp/ws"}) != []


def test_disabled_hook_never_matches(monkeypatch):
    _set_hooks(monkeypatch, [_hook(enabled=False)])
    assert evaluate("post_tool", {}) == []


def test_empty_match_matches_everything_for_its_event(monkeypatch):
    _set_hooks(monkeypatch, [_hook(match={})])
    assert [h["id"] for h in evaluate("post_tool", {"tool": "anything"})] == ["h1"]


def test_multiple_match_keys_are_anded(monkeypatch):
    _set_hooks(monkeypatch, [_hook(match={"tool": "bash", "command": "build"})])
    assert evaluate("post_tool", {"tool": "bash", "command": "npm run build"}) != []
    assert evaluate("post_tool", {"tool": "bash", "command": "npm run dev"}) == []
    assert evaluate("post_tool", {"tool": "python", "command": "build"}) == []


def test_evaluate_only_returns_the_requested_event(monkeypatch):
    _set_hooks(monkeypatch, [_hook(event="pre_tool"), _hook(id="h2", event="post_tool")])
    assert [h["id"] for h in evaluate("post_tool", {})] == ["h2"]
    assert [h["id"] for h in evaluate("pre_tool", {})] == ["h1"]


def test_evaluate_rejects_unknown_event(monkeypatch):
    _set_hooks(monkeypatch, [_hook()])
    assert evaluate("not_an_event", {}) == []


# ---------------------------------------------------------------------------
# template rendering
# ---------------------------------------------------------------------------

def test_render_text_substitutes_known_placeholders():
    ctx = {"tool": "bash", "command": "npm run build", "workspace": "/w",
           "event": "post_tool", "session_id": "s1", "user_message": "hello there"}
    text = render_text("[{event}] {tool} ran `{command}` (session {session_id}): {user_message}", ctx)
    assert text == "[post_tool] bash ran `npm run build` (session s1): hello there"


def test_render_text_leaves_unknown_placeholders_literal():
    assert render_text("keep {this_one} literal", {}) == "keep {this_one} literal"


def test_render_uses_first_path_relative_to_workspace():
    ctx = {"workspace": "/repo", "paths": ["/repo/src/app.py", "/repo/src/other.py"]}
    assert render_text("{file}", ctx) == "src/app.py"
    assert render_text("{files}", ctx) == "src/app.py src/other.py"


def test_render_command_shell_quotes_file_and_files():
    ctx = {"workspace": "/repo", "paths": ["/repo/needs quoting.py"]}
    rendered = render_command("ruff format {file}", ctx)
    assert rendered == "ruff format " + lh._quote_for_shell("needs quoting.py")
    assert "'" in rendered  # shlex.quote wraps a space in single quotes on POSIX


def test_render_text_does_not_quote_file():
    ctx = {"workspace": "/repo", "paths": ["/repo/needs quoting.py"]}
    assert render_text("{file}", ctx) == "needs quoting.py"


def test_render_truncates_user_message_to_500_chars():
    ctx = {"user_message": "x" * 600}
    assert len(render_text("{user_message}", ctx)) == 500


def test_render_command_missing_file_is_empty_string():
    assert render_command("echo {file}", {}) == "echo "


# ---------------------------------------------------------------------------
# command execution (real subprocess)
# ---------------------------------------------------------------------------

def test_command_execution_runs_a_real_echo(monkeypatch, tmp_path):
    _set_setting(monkeypatch, {})
    _set_hooks(monkeypatch, [_hook(id="echoer", event="pre_tool", action="command",
                                     command="echo hello-from-hook", text=None,
                                     match={})])
    results = asyncio.run(run_async("pre_tool", {"workspace": str(tmp_path)}))
    assert len(results) == 1
    r = results[0]
    assert r.ok is True
    assert "hello-from-hook" in r.output
    assert r.exit_code == 0
    assert r.level == "info"
    assert r.timed_out is False


def test_command_execution_runs_with_cwd_as_workspace(monkeypatch, tmp_path):
    _set_setting(monkeypatch, {})
    (tmp_path / "marker.txt").write_text("x", encoding="utf-8")
    _set_hooks(monkeypatch, [_hook(id="lister", event="pre_tool", action="command", command="ls",
                                     text=None, match={})])
    results = asyncio.run(run_async("pre_tool", {"workspace": str(tmp_path)}))
    assert "marker.txt" in results[0].output


def test_command_execution_nonzero_exit_is_ok_but_warn_level(monkeypatch):
    _set_setting(monkeypatch, {})
    _set_hooks(monkeypatch, [_hook(id="failer", event="pre_tool", action="command", command="exit 3",
                                     text=None, match={})])
    results = asyncio.run(run_async("pre_tool", {}))
    r = results[0]
    # A hook augments, it never blocks: a failing shell command is still a
    # completed, informative run — `ok=True`, just flagged `warn`.
    assert r.ok is True
    assert r.exit_code == 3
    assert r.level == "warn"


def test_command_timeout(monkeypatch):
    _set_setting(monkeypatch, {})
    _set_hooks(monkeypatch, [_hook(id="sleeper", event="pre_tool", action="command", command="sleep 5",
                                     text=None, match={}, timeout_s=1)])
    results = asyncio.run(run_async("pre_tool", {}))
    r = results[0]
    assert r.timed_out is True
    assert r.ok is False
    assert r.level == "warn"
    assert "timed out" in r.error


def test_command_uses_setting_default_timeout_when_hook_has_none(monkeypatch):
    _set_setting(monkeypatch, {"lifecycle_hooks_command_timeout_seconds": 1})
    _set_hooks(monkeypatch, [_hook(id="sleeper", event="pre_tool", action="command", command="sleep 5",
                                     text=None, match={}, timeout_s=None)])
    results = asyncio.run(run_async("pre_tool", {}))
    assert results[0].timed_out is True


def test_output_truncation_keeps_head_and_tail(monkeypatch):
    _set_setting(monkeypatch, {})
    # Print 200 numbered lines; with a tiny cap the middle must be dropped
    # but the very first and very last lines must survive.
    cmd = "for i in $(seq 1 200); do echo line-$i; done"
    _set_hooks(monkeypatch, [_hook(id="big", event="pre_tool", action="command", command=cmd, text=None,
                                     match={}, max_output_chars=200)])
    results = asyncio.run(run_async("pre_tool", {}))
    out = results[0].output
    assert len(out) <= 200 + len(lh._TRUNC_MARKER) + 10
    assert "line-1\n" in out
    assert "line-200" in out
    assert "...[truncated]..." in out
    assert "line-100" not in out


def test_command_spawn_error_is_ok_false(monkeypatch):
    _set_setting(monkeypatch, {})
    _set_hooks(monkeypatch, [_hook(id="badcwd", event="pre_tool", action="command", command="echo hi",
                                     text=None, match={})])
    results = asyncio.run(run_async("pre_tool", {"workspace": "/no/such/dir/at/all"}))
    r = results[0]
    assert r.ok is False
    assert r.error


# ---------------------------------------------------------------------------
# inject / warn
# ---------------------------------------------------------------------------

def test_inject_action_renders_text_and_is_info_level(monkeypatch):
    _set_setting(monkeypatch, {})
    _set_hooks(monkeypatch, [_hook(id="note", action="inject", text="Note for {tool}",
                                     match={})])
    results = asyncio.run(run_async("post_tool", {"tool": "bash"}))
    r = results[0]
    assert r.ok is True
    assert r.output == "Note for bash"
    assert r.level == "info"


def test_warn_action_renders_text_and_is_warn_level(monkeypatch):
    _set_setting(monkeypatch, {})
    _set_hooks(monkeypatch, [_hook(id="w", event="pre_tool", action="warn", text="careful", match={})])
    results = asyncio.run(run_async("pre_tool", {}))
    assert results[0].level == "warn"
    assert results[0].output == "careful"


# ---------------------------------------------------------------------------
# run() sync wrapper, enable flag, total timeout
# ---------------------------------------------------------------------------

def test_run_sync_wrapper_outside_a_loop(monkeypatch):
    _set_setting(monkeypatch, {})
    _set_hooks(monkeypatch, [_hook(id="note", action="inject", text="hi", match={})])
    results = run("post_tool", {})
    assert results[0].output == "hi"


def test_run_sync_wrapper_from_inside_a_running_loop(monkeypatch):
    _set_setting(monkeypatch, {})
    _set_hooks(monkeypatch, [_hook(id="note", action="inject", text="hi", match={})])

    async def _inner():
        return run("post_tool", {})

    results = asyncio.run(_inner())
    assert results[0].output == "hi"


def test_lifecycle_hooks_enabled_false_short_circuits(monkeypatch):
    _set_setting(monkeypatch, {"lifecycle_hooks_enabled": False})
    _set_hooks(monkeypatch, [_hook(id="note", action="inject", text="hi", match={})])
    assert asyncio.run(run_async("post_tool", {})) == []


def test_total_timeout_skips_remaining_hooks(monkeypatch):
    _set_setting(monkeypatch, {"lifecycle_hooks_total_timeout_seconds": 0.01})
    _set_hooks(monkeypatch, [
        _hook(id="slow", event="pre_tool", action="command", command="sleep 0.2", text=None, match={}, timeout_s=5),
        _hook(id="second", event="pre_tool", action="inject", text="never runs in time", match={}),
    ])
    results = asyncio.run(run_async("pre_tool", {}))
    assert len(results) == 2
    assert results[1].ok is False
    assert "timeout exceeded" in results[1].error


def test_a_raising_hook_never_breaks_the_others(monkeypatch):
    _set_setting(monkeypatch, {})

    def _boom(template, ctx):
        raise RuntimeError("boom")

    # Only the "command" action path calls render_command; "good" below is
    # an "inject" hook and uses render_text, so it is unaffected.
    monkeypatch.setattr(lh, "render_command", _boom)
    _set_hooks(monkeypatch, [_hook(id="bad", event="pre_tool", action="command", command="echo hi", match={}),
                              _hook(id="good", event="pre_tool", action="inject", text="still runs", match={})])
    results = asyncio.run(run_async("pre_tool", {}))
    assert results[0].ok is False and "boom" in results[0].error
    assert results[1].ok is True and results[1].output == "still runs"


# ---------------------------------------------------------------------------
# attach_to_result
# ---------------------------------------------------------------------------

def test_attach_to_result_adds_hook_notes_without_touching_other_keys():
    result = {"output": "original", "exit_code": 0}
    results = [HookResult("h1", "H1", "post_tool", "warn", ok=True, output="be careful", level="warn")]
    attach_to_result(result, results)
    assert result["output"] == "original"
    assert result["exit_code"] == 0
    assert result["hook_notes"] == [{"hook_id": "h1", "name": "H1", "level": "warn", "output": "be careful"}]


def test_attach_to_result_never_overwrites_existing_hook_notes():
    result = {"hook_notes": "sentinel"}
    results = [HookResult("h1", "H1", "post_tool", "warn", ok=True, output="x")]
    attach_to_result(result, results)
    assert result["hook_notes"] == "sentinel"


def test_attach_to_result_is_a_no_op_with_no_results():
    result = {"a": 1}
    attach_to_result(result, [])
    assert result == {"a": 1}


def test_attach_to_result_ignores_a_non_dict_result():
    assert attach_to_result("not-a-dict", [HookResult("h1", "H1", "e", "warn", ok=True)]) == "not-a-dict"


# ---------------------------------------------------------------------------
# render_context
# ---------------------------------------------------------------------------

def test_render_context_formats_one_section_per_hook_with_output():
    results = [
        HookResult("h1", "Git status", "session_start", "command", ok=True, output="clean"),
        HookResult("h2", "Silent", "session_start", "inject", ok=True, output=""),
    ]
    text = render_context(results)
    assert text == "### Git status (command)\nclean"


def test_render_context_empty_when_nothing_to_show():
    assert render_context([]) == ""
    assert render_context([HookResult("h1", "H1", "e", "inject", ok=True, output="")]) == ""


# ---------------------------------------------------------------------------
# presets
# ---------------------------------------------------------------------------

def test_preset_lookup():
    p = preset("compact_note")
    assert p["event"] == "pre_compact"
    assert p["action"] == "inject"


def test_preset_unknown_raises():
    with pytest.raises(HookError):
        preset("does-not-exist")


def test_every_preset_is_individually_valid():
    for p in lh.PRESETS:
        hook = {k: v for k, v in p.items() if k not in ("preset_id", "description")}
        validate_hooks([hook])  # must not raise


def test_apply_preset_adds_the_hook():
    updated = apply_preset([], "compact_note")
    assert [h["id"] for h in updated] == ["compact_note"]


def test_apply_preset_is_idempotent_by_id():
    once = apply_preset([], "compact_note")
    twice = apply_preset(once, "compact_note")
    assert [h["id"] for h in twice] == ["compact_note"]


def test_apply_preset_keeps_other_existing_hooks():
    existing = [_hook(id="custom")]
    updated = apply_preset(existing, "compact_note")
    assert {h["id"] for h in updated} == {"custom", "compact_note"}


# ---------------------------------------------------------------------------
# event log + rotation
# ---------------------------------------------------------------------------

@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(d))
    import src.constants as consts
    monkeypatch.setattr(consts, "DATA_DIR", str(d), raising=False)
    return d


def test_log_result_appends_and_recent_reads_it_back(data_dir, monkeypatch):
    _set_setting(monkeypatch, {})
    _set_hooks(monkeypatch, [_hook(id="note", action="inject", text="hi", match={})])
    asyncio.run(run_async("post_tool", {}))
    rows = lh.recent(limit=10)
    assert len(rows) == 1
    assert rows[0]["hook_id"] == "note"
    assert rows[0]["output"] == "hi"
    assert "ts" in rows[0]


def test_recent_returns_newest_first(data_dir, monkeypatch):
    _set_setting(monkeypatch, {})
    for i in range(3):
        _set_hooks(monkeypatch, [_hook(id=f"note{i}", action="inject", text=f"t{i}", match={})])
        asyncio.run(run_async("post_tool", {}))
    rows = lh.recent(limit=10)
    assert [r["hook_id"] for r in rows] == ["note2", "note1", "note0"]


def test_recent_with_no_log_file_is_empty(data_dir):
    assert lh.recent() == []


def test_log_rotates_past_the_byte_cap(data_dir, monkeypatch):
    monkeypatch.setattr(lh, "ROTATE_MAX_BYTES", 2048)
    monkeypatch.setattr(lh, "ROTATE_KEEP_LINES", 10)
    for r in (HookResult(f"h{i}", "H", "post_tool", "inject", ok=True, output="x" * 100) for i in range(200)):
        lh._log_result(r)
    path = lh._log_path()
    assert os.path.getsize(path) <= 2048 * 2
    rows = lh.recent(limit=200)
    assert rows[0]["hook_id"] == "h199", "rotation must keep the NEWEST entries"
    tmp_leftovers = [n for n in os.listdir(os.path.dirname(path)) if ".tmp" in n]
    assert tmp_leftovers == []


# ---------------------------------------------------------------------------
# API routes: GET/PUT /api/lifecycle-hooks, presets, /test, /log
# ---------------------------------------------------------------------------

@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    settings_mod._invalidate_caches()
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(tmp_path / "data"))
    import src.constants as consts
    monkeypatch.setattr(consts, "DATA_DIR", str(tmp_path / "data"), raising=False)

    from routes.lifecycle_hooks_routes import setup_lifecycle_hooks_routes

    app = FastAPI()
    app.include_router(setup_lifecycle_hooks_routes())
    with TestClient(app) as c:
        yield c
    settings_mod._invalidate_caches()


def test_api_get_defaults(api_client):
    resp = api_client.get("/api/lifecycle-hooks")
    assert resp.status_code == 200
    body = resp.json()
    assert body["hooks"] == []
    assert set(EVENTS) <= set(body["events"])
    assert "command" in body["actions"]
    assert any(p["preset_id"] == "compact_note" for p in body["presets"])


def test_api_put_then_get_round_trips(api_client):
    hooks = [{"id": "h1", "event": "post_tool", "action": "warn", "text": "careful",
              "match": {"path": "*.py"}}]
    put_resp = api_client.put("/api/lifecycle-hooks", json={"hooks": hooks})
    assert put_resp.status_code == 200
    assert put_resp.json()["hooks"][0]["id"] == "h1"
    get_resp = api_client.get("/api/lifecycle-hooks")
    assert get_resp.json()["hooks"][0]["id"] == "h1"


def test_api_put_rejects_bad_hook_and_does_not_partially_persist(api_client):
    bad = [{"id": "h1", "event": "bogus", "action": "warn", "text": "x"}]
    resp = api_client.put("/api/lifecycle-hooks", json={"hooks": bad})
    assert resp.status_code == 400
    assert "event" in resp.json()["error"]
    assert api_client.get("/api/lifecycle-hooks").json()["hooks"] == []


def test_api_preset_endpoint_adds_and_is_idempotent(api_client):
    r1 = api_client.post("/api/lifecycle-hooks/presets/compact_note")
    assert r1.status_code == 200
    assert [h["id"] for h in r1.json()["hooks"]] == ["compact_note"]
    r2 = api_client.post("/api/lifecycle-hooks/presets/compact_note")
    assert [h["id"] for h in r2.json()["hooks"]] == ["compact_note"]


def test_api_preset_endpoint_rejects_unknown_preset(api_client):
    resp = api_client.post("/api/lifecycle-hooks/presets/does-not-exist")
    assert resp.status_code == 400


def test_api_test_endpoint_dry_evaluation(api_client):
    hooks = [{"id": "h1", "event": "pre_tool", "action": "warn", "text": "x",
              "match": {"tool": "bash"}}]
    api_client.put("/api/lifecycle-hooks", json={"hooks": hooks})
    resp = api_client.post("/api/lifecycle-hooks/test",
                            json={"event": "pre_tool", "ctx": {"tool": "bash"}})
    assert resp.status_code == 200
    assert resp.json() == {"matched": ["h1"]}
    resp2 = api_client.post("/api/lifecycle-hooks/test",
                             json={"event": "pre_tool", "ctx": {"tool": "python"}})
    assert resp2.json() == {"matched": []}


def test_api_test_endpoint_can_actually_run(api_client):
    hooks = [{"id": "h1", "event": "pre_tool", "action": "inject", "text": "hello {tool}",
              "match": {"tool": "bash"}}]
    api_client.put("/api/lifecycle-hooks", json={"hooks": hooks})
    resp = api_client.post("/api/lifecycle-hooks/test",
                            json={"event": "pre_tool", "ctx": {"tool": "bash"}, "run": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"][0]["output"] == "hello bash"


def test_api_test_endpoint_rejects_unknown_event(api_client):
    resp = api_client.post("/api/lifecycle-hooks/test", json={"event": "bogus"})
    assert resp.status_code == 400


def test_api_log_endpoint(api_client):
    hooks = [{"id": "h1", "event": "pre_tool", "action": "inject", "text": "logged",
              "match": {}}]
    api_client.put("/api/lifecycle-hooks", json={"hooks": hooks})
    api_client.post("/api/lifecycle-hooks/test", json={"event": "pre_tool", "run": True})
    resp = api_client.get("/api/lifecycle-hooks/log?limit=5")
    assert resp.status_code == 200
    rows = resp.json()["results"]
    assert rows and rows[0]["hook_id"] == "h1"


def test_api_admin_gate(tmp_path, monkeypatch):
    """Without AUTH_ENABLED=false and no admin session, the routes 403."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    settings_mod._invalidate_caches()
    monkeypatch.delenv("AUTH_ENABLED", raising=False)

    from routes.lifecycle_hooks_routes import setup_lifecycle_hooks_routes

    app = FastAPI()
    app.include_router(setup_lifecycle_hooks_routes())
    with TestClient(app) as c:
        resp = c.get("/api/lifecycle-hooks")
    assert resp.status_code == 403
    settings_mod._invalidate_caches()
