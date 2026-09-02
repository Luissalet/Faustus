"""Agent & automation settings schema (src/agent_settings_schema.py).

- parity with DEFAULT_SETTINGS: every agent_* / browser_* / desktop_* key (and
  the EXTRA_KEYS) has exactly one schema entry, and no entry names a key that
  does not exist — adding a setting without describing it fails here;
- field types match the defaults' Python types, numeric bounds contain the
  default, selects list their default, help texts are present;
- coerce_setting_value: the type/bounds contract POST /api/auth/settings
  applies to these keys;
- GET /api/agent/settings/schema: 200 + {groups, defaults} for an admin, 403
  for a non-admin and for an anonymous caller.
"""
from __future__ import annotations

import pytest

import src.agent_settings_schema as schema_mod
from src.agent_settings_schema import (
    EXTRA_KEYS,
    FIELD_TYPES,
    SCHEMA_KEY_RE,
    build_schema,
    coerce_setting_value,
    expected_keys,
    schema_fields,
    schema_keys,
    schema_problems,
)
from src.settings import DEFAULT_SETTINGS, RETIRED_SETTING_KEYS


# ── parity / consistency ────────────────────────────────────────────────────

def test_schema_has_no_problems():
    assert schema_problems() == []


def test_every_agent_browser_desktop_default_is_in_the_schema():
    keys = set(schema_keys())
    for key in DEFAULT_SETTINGS:
        if key in RETIRED_SETTING_KEYS:
            continue
        if SCHEMA_KEY_RE.match(key):
            assert key in keys, f"{key} was added to DEFAULT_SETTINGS without a schema entry"
    for key in EXTRA_KEYS:
        assert key in DEFAULT_SETTINGS
        assert key in keys


def test_schema_names_only_existing_keys_exactly_once():
    keys = schema_keys()
    assert len(keys) == len(set(keys)), "a key is listed twice"
    for key in keys:
        assert key in DEFAULT_SETTINGS, f"{key} is in the schema but not in DEFAULT_SETTINGS"
    assert set(keys) == set(expected_keys())


def test_schema_problems_detects_a_new_undeclared_key(monkeypatch):
    defaults = dict(DEFAULT_SETTINGS)
    defaults["agent_brand_new_knob"] = 3
    monkeypatch.setattr(schema_mod, "DEFAULT_SETTINGS", defaults)
    problems = schema_problems()
    assert any("agent_brand_new_knob" in p and "missing from the schema" in p for p in problems)


def test_schema_problems_detects_a_stale_schema_key(monkeypatch):
    defaults = {k: v for k, v in DEFAULT_SETTINGS.items() if k != "browser_headless"}
    monkeypatch.setattr(schema_mod, "DEFAULT_SETTINGS", defaults)
    problems = schema_problems()
    assert any(p.startswith("browser_headless:") and "not in DEFAULT_SETTINGS" in p for p in problems)


def test_field_types_match_default_python_types():
    for f in schema_fields():
        default = DEFAULT_SETTINGS[f["key"]]
        t = f["type"]
        assert t in FIELD_TYPES, f["key"]
        if t == "bool":
            assert isinstance(default, bool), f["key"]
        elif t == "int":
            assert isinstance(default, int) and not isinstance(default, bool), f["key"]
            assert f["min"] <= default <= f["max"], f["key"]
        elif t == "float":
            assert isinstance(default, (int, float)) and not isinstance(default, bool), f["key"]
            assert f["min"] <= default <= f["max"], f["key"]
        elif t == "select":
            assert isinstance(default, str)
            assert default in [o["value"] for o in f["options"]], f["key"]
        elif t == "list":
            assert isinstance(default, list), f["key"]
        else:
            assert isinstance(default, str), f["key"]


def test_every_field_has_label_help_key_and_restart_flag():
    for f in schema_fields():
        assert f["label"].strip() and f["help"].strip(), f["key"]
        assert isinstance(f["restart_hint"], bool), f["key"]
        # The raw key is shown under the label for slash-command users, so it
        # must be the literal DEFAULT_SETTINGS key.
        assert f["key"] in DEFAULT_SETTINGS


def test_groups_follow_the_requested_layout():
    groups = {g["id"]: g for g in schema_mod.GROUPS}
    by_group = {g["id"]: [f["key"] for f in g["fields"]] for g in schema_mod.GROUPS}
    assert [g["title"] for g in schema_mod.GROUPS][:8] == [
        "Agent loop", "Verification", "Context", "Sub-agents", "Runs & queue",
        "Browser", "Desktop control", "Vision",
    ]
    for g in groups.values():
        assert g["help"].strip() and g["fields"], g["id"]
    assert {"agent_max_rounds", "agent_max_tool_calls", "agent_harness_checks", "agent_tool_preflight",
            "agent_local_temperature_cap", "agent_auto_continue_cycles", "agent_local_stream_timeout_seconds",
            "agent_local_think_budget_seconds", "agent_subprocess_idle_timeout_seconds",
            "agent_workspace_no_memory"} <= set(by_group["loop"])
    assert all(k.startswith(("agent_project_test", "agent_static_analysis", "agent_auto_review", "agent_checkpoint"))
               for k in by_group["verification"])
    assert {"agent_subagent_reviewer", "agent_subagent_max_parallel", "agent_subagent_stall_seconds",
            "agent_subagent_tick_seconds", "agent_subagent_supervisor", "agent_subagent_lean_tools",
            "agent_subagent_worker_model"} == set(by_group["subagents"])
    assert {"agent_runs_persist", "agent_runs_keep_hours", "agent_queue_local_concurrency",
            "agent_queue_api_concurrency", "agent_scorecard"} == set(by_group["runs"])
    assert all(k.startswith("browser_") for k in by_group["browser"]) and len(by_group["browser"]) == 7
    assert by_group["desktop"] == ["desktop_control_mode"]
    assert by_group["vision"] == ["vision_enabled", "vision_model"]
    assert by_group["files"] == ["tool_path_extra_roots"]


def test_browser_fields_say_next_action_and_never_restart():
    for f in schema_fields():
        if f["key"].startswith("browser_"):
            assert f["restart_hint"] is False, f["key"]
    browser = next(g for g in schema_mod.GROUPS if g["id"] == "browser")
    assert "next browser action" in browser["help"]
    cdp = next(f for f in browser["fields"] if f["key"] == "browser_cdp_endpoint")
    assert "--remote-debugging-port=9222" in cdp["help"] and "--user-data-dir" in cdp["help"]


def test_selects_carry_the_documented_options():
    fields = {f["key"]: f for f in schema_fields()}
    assert [o["value"] for o in fields["browser_profile"]["options"]] == ["isolated", "persistent"]
    assert [o["value"] for o in fields["desktop_control_mode"]["options"]] == ["ask_each", "ask_task", "off"]
    assert [o["value"] for o in fields["agent_static_analysis"]["options"]] == ["off", "names", "types"]
    assert [o["value"] for o in fields["agent_project_tests_scope"]["options"]] == ["related", "all"]


def test_build_schema_payload_shape():
    payload = build_schema()
    assert set(payload) == {"groups", "defaults"}
    assert set(payload["defaults"]) == set(schema_keys())
    for key, value in payload["defaults"].items():
        assert value == DEFAULT_SETTINGS[key]
    # The payload is a copy: mutating it must not touch the module's GROUPS.
    payload["groups"][0]["fields"][0]["label"] = "mutated"
    assert schema_mod.GROUPS[0]["fields"][0]["label"] != "mutated"


# ── coercion (POST /api/auth/settings contract) ─────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    (True, True), ("true", True), ("on", True), (1, True),
    (False, False), ("false", False), ("off", False), (0, False), ("", False),
])
def test_coerce_bool(raw, expected):
    assert coerce_setting_value("browser_headless", raw) is expected


def test_coerce_bool_rejects_garbage():
    with pytest.raises(ValueError):
        coerce_setting_value("browser_headless", "maybe")


def test_coerce_int_and_clamp():
    assert coerce_setting_value("agent_subagent_max_parallel", "4") == 4
    assert coerce_setting_value("agent_subagent_max_parallel", 4.0) == 4
    assert coerce_setting_value("agent_subagent_max_parallel", 999) == 32     # max
    assert coerce_setting_value("agent_subagent_max_parallel", -5) == 1       # min
    assert coerce_setting_value("agent_keep_images", -1) == -1
    with pytest.raises(ValueError):
        coerce_setting_value("agent_subagent_max_parallel", "four")
    with pytest.raises(ValueError):
        coerce_setting_value("agent_subagent_max_parallel", 2.5)
    with pytest.raises(ValueError):
        coerce_setting_value("agent_subagent_max_parallel", True)


def test_coerce_float_and_clamp():
    assert coerce_setting_value("agent_local_temperature_cap", "0.7") == 0.7
    assert coerce_setting_value("agent_local_temperature_cap", 1) == 1.0
    assert coerce_setting_value("agent_local_temperature_cap", 9) == 2.0
    assert coerce_setting_value("agent_read_window_fraction", 0) == 0.01
    with pytest.raises(ValueError):
        coerce_setting_value("agent_local_temperature_cap", "hot")


def test_coerce_select_text_and_list():
    assert coerce_setting_value("desktop_control_mode", " off ") == "off"
    with pytest.raises(ValueError):
        coerce_setting_value("desktop_control_mode", "sometimes")
    assert coerce_setting_value("browser_cdp_endpoint", "  http://127.0.0.1:9222 ") == "http://127.0.0.1:9222"
    assert coerce_setting_value("browser_cdp_endpoint", None) == ""
    assert coerce_setting_value("tool_path_extra_roots", "/srv/a, /srv/b,,\n/srv/c ") == ["/srv/a", "/srv/b", "/srv/c"]
    assert coerce_setting_value("tool_path_extra_roots", ["/x", " ", "/y"]) == ["/x", "/y"]
    assert coerce_setting_value("tool_path_extra_roots", None) == []
    with pytest.raises(ValueError):
        coerce_setting_value("tool_path_extra_roots", [1, 2])
    with pytest.raises(ValueError):
        coerce_setting_value("tool_path_extra_roots", {"a": 1})


def test_coerce_leaves_unknown_keys_alone():
    assert coerce_setting_value("tts_speed", "1.5") == "1.5"
    assert coerce_setting_value("not_a_setting", {"x": 1}) == {"x": 1}


# ── POST /api/auth/settings applies the schema ──────────────────────────────

@pytest.mark.asyncio
async def test_settings_post_coerces_agent_keys(monkeypatch):
    from types import SimpleNamespace
    import routes.auth_routes as auth_routes

    store = dict(DEFAULT_SETTINGS)
    monkeypatch.setattr(auth_routes, "migrate_from_settings", lambda: None)
    monkeypatch.setattr(auth_routes, "_load_settings", lambda: dict(store))
    monkeypatch.setattr(auth_routes, "_save_settings", lambda updated: (store.clear(), store.update(updated)))

    class _Auth:
        def get_username_for_token(self, token):
            return "admin" if token == "s" else None

        def is_admin(self, username):
            return username == "admin"

    class _Req(SimpleNamespace):
        def __init__(self, body):
            super().__init__(cookies={auth_routes.SESSION_COOKIE: "s"}, _body=body)

        async def json(self):
            return self._body

    router = auth_routes.setup_auth_routes(_Auth())
    post = next(r.endpoint for r in router.routes if r.path == "/api/auth/settings" and "POST" in r.methods)

    await post(_Req({
        "browser_headless": "false",
        "agent_subagent_max_parallel": "3",
        "agent_local_temperature_cap": "0.6",
        "tool_path_extra_roots": "/srv/a, /srv/b",
        "agent_runs_keep_hours": 99999,
        "agent_max_rounds": "7",
    }))
    assert store["browser_headless"] is False
    assert store["agent_subagent_max_parallel"] == 3
    assert store["agent_local_temperature_cap"] == 0.6
    assert store["tool_path_extra_roots"] == ["/srv/a", "/srv/b"]
    assert store["agent_runs_keep_hours"] == 8760
    assert store["agent_max_rounds"] == 7

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await post(_Req({"desktop_control_mode": "sometimes"}))
    assert exc.value.status_code == 400
    assert "desktop_control_mode" in exc.value.detail


# ── GET /api/agent/settings/schema ──────────────────────────────────────────

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("starlette.testclient")


def _client(monkeypatch):
    """Real router + core.middleware.require_admin, an auth manager stub on
    app.state and a middleware that reads the caller from the X-User header
    the way the real AuthMiddleware stamps request.state.current_user."""
    from fastapi import FastAPI
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.testclient import TestClient
    from types import SimpleNamespace
    import core.middleware as mw
    from routes.agent_settings_routes import setup_agent_settings_routes

    monkeypatch.setattr(mw, "auth_disabled", lambda: False)
    app = FastAPI()
    app.include_router(setup_agent_settings_routes())
    app.state.auth_manager = SimpleNamespace(is_configured=True, is_admin=lambda u: u == "root")

    class _Stamp(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            user = request.headers.get("x-user")
            if user:
                request.state.current_user = user
            return await call_next(request)

    app.add_middleware(_Stamp)
    return TestClient(app, raise_server_exceptions=False)


def test_schema_route_admin_gets_groups_and_defaults(monkeypatch):
    client = _client(monkeypatch)
    r = client.get("/api/agent/settings/schema", headers={"x-user": "root"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"groups", "defaults"}
    assert [g["id"] for g in body["groups"]][:3] == ["loop", "verification", "context"]
    assert body["defaults"]["agent_max_rounds"] == DEFAULT_SETTINGS["agent_max_rounds"]
    assert body["defaults"]["browser_profile"] == "persistent"
    field = next(f for g in body["groups"] for f in g["fields"] if f["key"] == "agent_local_temperature_cap")
    assert field["type"] == "float" and field["min"] == 0 and field["max"] == 2


def test_schema_route_is_admin_only(monkeypatch):
    client = _client(monkeypatch)
    assert client.get("/api/agent/settings/schema", headers={"x-user": "alice"}).status_code == 403
    assert client.get("/api/agent/settings/schema").status_code == 403


def test_app_registers_the_schema_router():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "app.py").read_text(encoding="utf-8")
    assert "from routes.agent_settings_routes import setup_agent_settings_routes" in src
    assert "app.include_router(setup_agent_settings_routes())" in src
