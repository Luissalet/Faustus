"""L96 · OBJ-8 Lote A2 — per-endpoint OpenRouter options.

Covers `src/openrouter_options.py` (schema validation, atomic persistence,
`apply_openrouter_payload`), the two `src/llm_core.py` cache_control helpers
that extend Anthropic prompt-cache breakpoints to an OpenRouter
`anthropic/*` call, and `routes/openrouter_routes.py` through a real
`TestClient`. No network calls anywhere in this file.
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import src.openrouter_options as oo
import routes.openrouter_routes as openrouter_routes
from src import llm_core


# ---------------------------------------------------------------------------
# Isolation: every test gets its own on-disk store.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(oo, "STORE_PATH", str(tmp_path / "openrouter_endpoints.json"))
    yield


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------
def test_defaults_are_a_fresh_copy_each_time():
    a = oo._default_prefs()
    b = oo._default_prefs()
    a["order"].append("mutated")
    assert b["order"] == []


def test_get_prefs_none_or_empty_returns_defaults_without_touching_disk():
    assert oo.get_prefs(None) == oo._default_prefs()
    assert oo.get_prefs("") == oo._default_prefs()
    assert not (oo.STORE_PATH and __import__("os").path.exists(oo.STORE_PATH))


@pytest.mark.parametrize("patch,message_substr", [
    ({"sort": "fastest"}, "sort"),
    ({"allow_fallbacks": "yes"}, "allow_fallbacks"),
    ({"require_parameters": 1}, "require_parameters"),
    ({"zdr": "true"}, "zdr"),
    ({"order": "openai"}, "order"),
    ({"order": [f"p{i}" for i in range(21)]}, "order"),
    ({"order": [123]}, "order"),
    ({"data_collection": "maybe"}, "data_collection"),
    ({"native_fallback": "yes"}, "native_fallback"),
    ({"web_search": "yes"}, "web_search"),
    ({"web_search": {"enabled": "yes"}}, "web_search"),
    ({"web_search": {"max_results": 0}}, "web_search"),
    ({"web_search": {"max_results": 11}}, "web_search"),
    ({"max_price": "cheap"}, "max_price"),
    ({"max_price": {"prompt": "cheap"}}, "max_price"),
    ({"max_price": {"prompt": -1}}, "max_price"),
    ({"bogus_field": True}, "unknown field"),
])
def test_set_prefs_rejects_invalid_fields_with_a_clear_message(patch, message_substr):
    with pytest.raises(ValueError, match=message_substr):
        oo.set_prefs("ep1", patch)


def test_set_prefs_rejects_non_object_patch():
    with pytest.raises(ValueError):
        oo.set_prefs("ep1", ["not", "a", "dict"])  # type: ignore[arg-type]


def test_set_prefs_requires_a_non_empty_endpoint_id():
    with pytest.raises(ValueError):
        oo.set_prefs("", {"sort": "price"})
    with pytest.raises(ValueError):
        oo.set_prefs("   ", {"zdr": True})


def test_set_prefs_validates_and_normalizes_a_full_patch():
    result = oo.set_prefs("ep1", {
        "sort": "price",
        "allow_fallbacks": False,
        "require_parameters": True,
        "max_price": {"prompt": 1.5, "completion": 3},
        "zdr": True,
        "order": ["anthropic", "openai"],
        "ignore": ["mistral"],
        "data_collection": "deny",
        "web_search": {"enabled": True, "max_results": 3},
        "native_fallback": True,
    })
    assert result == {
        "sort": "price",
        "allow_fallbacks": False,
        "require_parameters": True,
        "max_price": {"prompt": 1.5, "completion": 3.0},
        "zdr": True,
        "order": ["anthropic", "openai"],
        "ignore": ["mistral"],
        "data_collection": "deny",
        "web_search": {"enabled": True, "max_results": 3},
        "native_fallback": True,
    }


def test_max_price_drops_none_fields_and_empty_dict_becomes_none():
    result = oo.set_prefs("ep1", {"max_price": {"prompt": 2.0, "completion": None}})
    assert result["max_price"] == {"prompt": 2.0}
    result2 = oo.set_prefs("ep1", {"max_price": {}})
    assert result2["max_price"] is None


def test_partial_web_search_patch_keeps_the_other_field():
    oo.set_prefs("ep1", {"web_search": {"enabled": True, "max_results": 8}})
    result = oo.set_prefs("ep1", {"web_search": {"enabled": False}})
    assert result["web_search"] == {"enabled": False, "max_results": 8}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def test_set_then_get_round_trips():
    oo.set_prefs("ep1", {"sort": "throughput"})
    assert oo.get_prefs("ep1")["sort"] == "throughput"


def test_get_prefs_for_unknown_endpoint_is_defaults():
    assert oo.get_prefs("never-saved") == oo._default_prefs()


def test_set_prefs_merges_onto_the_previous_patch_not_defaults():
    oo.set_prefs("ep1", {"sort": "price"})
    oo.set_prefs("ep1", {"zdr": True})
    result = oo.get_prefs("ep1")
    assert result["sort"] == "price"
    assert result["zdr"] is True


def test_delete_prefs_removes_the_entry_and_is_idempotent():
    oo.set_prefs("ep1", {"sort": "latency"})
    oo.delete_prefs("ep1")
    assert oo.get_prefs("ep1") == oo._default_prefs()
    oo.delete_prefs("ep1")  # no error deleting again
    oo.delete_prefs("never-existed")  # no error deleting a fresh id


def test_all_prefs_lists_every_saved_endpoint():
    oo.set_prefs("ep1", {"sort": "price"})
    oo.set_prefs("ep2", {"zdr": True})
    result = oo.all_prefs()
    assert set(result) == {"ep1", "ep2"}
    assert result["ep1"]["sort"] == "price"
    assert result["ep2"]["zdr"] is True


def test_store_is_written_atomically_and_survives_a_reload():
    oo.set_prefs("ep1", {"sort": "price"})
    with open(oo.STORE_PATH, "r", encoding="utf-8") as fh:
        on_disk = json.load(fh)
    assert on_disk["ep1"]["sort"] == "price"
    # A brand-new in-process read (no cached state) sees the same thing.
    assert oo.get_prefs("ep1")["sort"] == "price"


def test_corrupt_store_file_degrades_to_empty_instead_of_raising(monkeypatch):
    with open(oo.STORE_PATH, "w", encoding="utf-8") as fh:
        fh.write("{not valid json")
    assert oo.get_prefs("ep1") == oo._default_prefs()
    # get_prefs's degrade-to-defaults path quarantines the corrupt file
    # rather than looping forever on the same bad bytes.
    import os
    assert os.path.exists(oo.STORE_PATH + ".corrupt")


# ---------------------------------------------------------------------------
# apply_openrouter_payload
# ---------------------------------------------------------------------------
def test_apply_openrouter_payload_is_a_no_op_outside_openrouter():
    payload = {"model": "gpt-4", "messages": []}
    before = dict(payload)
    result = oo.apply_openrouter_payload(payload, provider="openai", endpoint_id=None, model="gpt-4")
    assert result == before
    assert "usage" not in payload


def test_apply_openrouter_payload_always_sets_usage_include():
    payload = {"model": "openai/gpt-4o", "messages": []}
    oo.apply_openrouter_payload(payload, provider="openrouter", endpoint_id=None, model="openai/gpt-4o")
    assert payload["usage"] == {"include": True}


def test_apply_openrouter_payload_omits_provider_key_with_no_configured_prefs():
    payload = {"model": "openai/gpt-4o", "messages": []}
    oo.apply_openrouter_payload(payload, provider="openrouter", endpoint_id=None, model="openai/gpt-4o")
    assert "provider" not in payload


def test_apply_openrouter_payload_builds_provider_block_from_configured_prefs():
    oo.set_prefs("ep1", {
        "sort": "price",
        "require_parameters": True,
        "max_price": {"prompt": 2.0},
        "zdr": True,
        "order": ["anthropic"],
        "ignore": ["mistral"],
        "allow_fallbacks": False,
    })
    payload = {"model": "openai/gpt-4o", "messages": []}
    oo.apply_openrouter_payload(payload, provider="openrouter", endpoint_id="ep1", model="openai/gpt-4o")
    assert payload["provider"] == {
        "sort": "price",
        "allow_fallbacks": False,
        "require_parameters": True,
        "max_price": {"prompt": 2.0},
        "zdr": True,
        "order": ["anthropic"],
        "ignore": ["mistral"],
    }


def test_data_collection_auto_denies_under_local_only_privacy_profile(monkeypatch):
    from src import privacy_policy
    monkeypatch.setattr(privacy_policy, "get_privacy_profile", lambda project=None, owner=None: privacy_policy.PROFILE_LOCAL_ONLY)
    payload = {"model": "openai/gpt-4o", "messages": []}
    oo.apply_openrouter_payload(payload, provider="openrouter", endpoint_id=None, model="openai/gpt-4o")
    assert payload["provider"]["data_collection"] == "deny"


def test_data_collection_auto_stays_unset_when_privacy_profile_is_not_local_only(monkeypatch):
    from src import privacy_policy
    monkeypatch.setattr(privacy_policy, "get_privacy_profile", lambda project=None, owner=None: privacy_policy.PROFILE_LOCAL_PREFERRED)
    payload = {"model": "openai/gpt-4o", "messages": []}
    oo.apply_openrouter_payload(payload, provider="openrouter", endpoint_id=None, model="openai/gpt-4o")
    assert "provider" not in payload


def test_data_collection_explicit_choice_always_wins_over_privacy_profile(monkeypatch):
    from src import privacy_policy
    monkeypatch.setattr(privacy_policy, "get_privacy_profile", lambda project=None, owner=None: privacy_policy.PROFILE_CLOUD_ALLOWED)
    oo.set_prefs("ep1", {"data_collection": "deny"})
    payload = {"model": "openai/gpt-4o", "messages": []}
    oo.apply_openrouter_payload(payload, provider="openrouter", endpoint_id="ep1", model="openai/gpt-4o")
    assert payload["provider"]["data_collection"] == "deny"


def test_web_search_plugin_never_added_by_default():
    payload = {"model": "openai/gpt-4o", "messages": []}
    oo.apply_openrouter_payload(payload, provider="openrouter", endpoint_id=None, model="openai/gpt-4o")
    assert "plugins" not in payload


def test_web_search_plugin_added_when_turn_requests_it_explicitly():
    payload = {"model": "openai/gpt-4o", "messages": []}
    oo.apply_openrouter_payload(
        payload, provider="openrouter", endpoint_id=None, model="openai/gpt-4o",
        web_search_requested=True,
    )
    assert payload["plugins"] == [{"id": "web", "max_results": 5}]


def test_web_search_plugin_added_when_endpoint_opted_in_ahead_of_time():
    oo.set_prefs("ep1", {"web_search": {"enabled": True, "max_results": 7}})
    payload = {"model": "openai/gpt-4o", "messages": []}
    oo.apply_openrouter_payload(payload, provider="openrouter", endpoint_id="ep1", model="openai/gpt-4o")
    assert payload["plugins"] == [{"id": "web", "max_results": 7}]


def test_web_search_requested_false_does_not_suppress_an_endpoint_opt_in():
    oo.set_prefs("ep1", {"web_search": {"enabled": True}})
    payload = {"model": "openai/gpt-4o", "messages": []}
    oo.apply_openrouter_payload(
        payload, provider="openrouter", endpoint_id="ep1", model="openai/gpt-4o",
        web_search_requested=False,
    )
    assert "plugins" in payload


def test_native_fallback_builds_models_list_deduped_and_capped():
    oo.set_prefs("ep1", {"native_fallback": True})
    payload = {"model": "openai/gpt-4o", "messages": []}
    fallback = ["openai/gpt-4o", "anthropic/claude-3.7-sonnet"] + [f"vendor/model-{i}" for i in range(10)]
    oo.apply_openrouter_payload(
        payload, provider="openrouter", endpoint_id="ep1", model="openai/gpt-4o",
        fallback_models=fallback,
    )
    assert payload["models"][0] == "openai/gpt-4o"
    assert len(payload["models"]) == 8
    assert len(set(payload["models"])) == len(payload["models"])


def test_native_fallback_not_applied_when_prefs_disabled_even_with_candidates():
    payload = {"model": "openai/gpt-4o", "messages": []}
    oo.apply_openrouter_payload(
        payload, provider="openrouter", endpoint_id=None, model="openai/gpt-4o",
        fallback_models=["anthropic/claude-3.7-sonnet"],
    )
    assert "models" not in payload


def test_native_fallback_not_applied_when_no_candidates_even_if_enabled():
    oo.set_prefs("ep1", {"native_fallback": True})
    payload = {"model": "openai/gpt-4o", "messages": []}
    oo.apply_openrouter_payload(
        payload, provider="openrouter", endpoint_id="ep1", model="openai/gpt-4o",
        fallback_models=None,
    )
    assert "models" not in payload


def test_apply_openrouter_payload_returns_the_same_mutated_dict():
    payload = {"model": "openai/gpt-4o", "messages": []}
    result = oo.apply_openrouter_payload(payload, provider="openrouter", endpoint_id=None, model="openai/gpt-4o")
    assert result is payload


# ---------------------------------------------------------------------------
# llm_core.py cache_control extension (OBJ-8 Lote A2 point (b))
# ---------------------------------------------------------------------------
def test_cache_hints_applicable_only_for_openrouter_anthropic_models():
    assert llm_core._openrouter_anthropic_cache_hints_applicable("openrouter", "anthropic/claude-3.7-sonnet") is True
    assert llm_core._openrouter_anthropic_cache_hints_applicable("openrouter", "openai/gpt-4o") is False
    assert llm_core._openrouter_anthropic_cache_hints_applicable("anthropic", "anthropic/claude-3.7-sonnet") is False
    assert llm_core._openrouter_anthropic_cache_hints_applicable("openrouter", "") is False
    assert llm_core._openrouter_anthropic_cache_hints_applicable("openrouter", None) is False


def test_cache_breakpoint_shared_threshold_used_by_both_paths():
    assert llm_core._anthropic_cache_breakpoint_applies(None, "short") is False
    assert llm_core._anthropic_cache_breakpoint_applies(None, "x" * 4001) is True
    assert llm_core._anthropic_cache_breakpoint_applies([{"type": "function"}], "short") is True


def test_apply_openrouter_anthropic_cache_hints_rewrites_long_system_message():
    payload = {
        "model": "anthropic/claude-3.7-sonnet",
        "messages": [
            {"role": "system", "content": "x" * 4001},
            {"role": "user", "content": "hi"},
        ],
    }
    llm_core._apply_openrouter_anthropic_cache_hints(payload, tools=None)
    assert payload["messages"][0]["content"] == [{
        "type": "text",
        "text": "x" * 4001,
        "cache_control": {"type": "ephemeral"},
    }]
    assert payload["messages"][1] == {"role": "user", "content": "hi"}


def test_apply_openrouter_anthropic_cache_hints_skips_short_system_without_tools():
    payload = {
        "model": "anthropic/claude-3.7-sonnet",
        "messages": [{"role": "system", "content": "short"}],
    }
    llm_core._apply_openrouter_anthropic_cache_hints(payload, tools=None)
    assert payload["messages"][0]["content"] == "short"


def test_apply_openrouter_anthropic_cache_hints_applies_for_short_system_with_tools():
    payload = {
        "model": "anthropic/claude-3.7-sonnet",
        "messages": [{"role": "system", "content": "short"}],
    }
    llm_core._apply_openrouter_anthropic_cache_hints(payload, tools=[{"type": "function", "function": {"name": "x"}}])
    assert isinstance(payload["messages"][0]["content"], list)
    assert payload["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_apply_openrouter_anthropic_cache_hints_noop_without_a_system_message():
    payload = {
        "model": "anthropic/claude-3.7-sonnet",
        "messages": [{"role": "user", "content": "hi"}],
    }
    llm_core._apply_openrouter_anthropic_cache_hints(payload, tools=None)
    assert payload["messages"] == [{"role": "user", "content": "hi"}]


def test_apply_openrouter_anthropic_cache_hints_noop_on_empty_messages():
    payload = {"model": "anthropic/claude-3.7-sonnet", "messages": []}
    llm_core._apply_openrouter_anthropic_cache_hints(payload, tools=None)
    assert payload["messages"] == []


def test_native_anthropic_payload_still_caches_the_same_way_as_before():
    """Non-regression: `_build_anthropic_payload`'s own condition now goes
    through `_anthropic_cache_breakpoint_applies`, but the native Anthropic
    path's behaviour is unchanged."""
    payload = llm_core._build_anthropic_payload(
        "claude-3.7-sonnet",
        [{"role": "system", "content": "x" * 4001}, {"role": "user", "content": "hi"}],
        temperature=0.5, max_tokens=1024,
    )
    assert payload["system"][0]["cache_control"] == {"type": "ephemeral"}

    payload_short = llm_core._build_anthropic_payload(
        "claude-3.7-sonnet",
        [{"role": "system", "content": "short"}, {"role": "user", "content": "hi"}],
        temperature=0.5, max_tokens=1024,
    )
    assert "cache_control" not in payload_short["system"][0]


# ---------------------------------------------------------------------------
# Routes (TestClient)
# ---------------------------------------------------------------------------
def _app(monkeypatch, *, admin_raises=None):
    def _require_admin(request):
        if admin_raises is not None:
            raise admin_raises

    monkeypatch.setattr(openrouter_routes, "require_admin", _require_admin)
    app = FastAPI()
    app.include_router(openrouter_routes.setup_openrouter_routes())
    return app


def _client(monkeypatch, **kw):
    return TestClient(_app(monkeypatch, **kw))


def test_routes_require_admin(monkeypatch):
    client = _client(monkeypatch, admin_raises=HTTPException(403, "Admin only"))
    assert client.get("/api/openrouter/prefs").status_code == 403
    assert client.get("/api/openrouter/endpoints/ep1/prefs").status_code == 403
    assert client.put("/api/openrouter/endpoints/ep1/prefs", json={"sort": "price"}).status_code == 403
    assert client.delete("/api/openrouter/endpoints/ep1/prefs").status_code == 403


def test_get_endpoint_prefs_defaults_for_an_admin(monkeypatch):
    client = _client(monkeypatch)
    response = client.get("/api/openrouter/endpoints/ep1/prefs")
    assert response.status_code == 200
    assert response.json() == oo._default_prefs()


def test_put_then_get_round_trips_through_the_route(monkeypatch):
    client = _client(monkeypatch)
    put_response = client.put("/api/openrouter/endpoints/ep1/prefs", json={"sort": "throughput", "zdr": True})
    assert put_response.status_code == 200
    assert put_response.json()["sort"] == "throughput"
    get_response = client.get("/api/openrouter/endpoints/ep1/prefs")
    assert get_response.json()["zdr"] is True


def test_put_invalid_patch_returns_flat_400_body(monkeypatch):
    client = _client(monkeypatch)
    response = client.put("/api/openrouter/endpoints/ep1/prefs", json={"sort": "fastest"})
    assert response.status_code == 400
    body = response.json()
    assert body["error_class"] == "openrouter.invalid_prefs"
    assert "sort" in body["detail"]


def test_delete_then_get_returns_defaults(monkeypatch):
    client = _client(monkeypatch)
    client.put("/api/openrouter/endpoints/ep1/prefs", json={"sort": "price"})
    delete_response = client.delete("/api/openrouter/endpoints/ep1/prefs")
    assert delete_response.status_code == 200
    assert delete_response.json() == {"deleted": True, "endpoint_id": "ep1"}
    assert client.get("/api/openrouter/endpoints/ep1/prefs").json() == oo._default_prefs()


def test_list_all_prefs_route(monkeypatch):
    client = _client(monkeypatch)
    client.put("/api/openrouter/endpoints/ep1/prefs", json={"sort": "price"})
    client.put("/api/openrouter/endpoints/ep2/prefs", json={"zdr": True})
    response = client.get("/api/openrouter/prefs")
    assert response.status_code == 200
    endpoints = response.json()["endpoints"]
    assert set(endpoints) == {"ep1", "ep2"}
