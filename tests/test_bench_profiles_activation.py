"""src/bench/profiles.py — INF-05 B3: `activate_profile`/`deactivate_profile`
(§13 "política de activación", applied to a saved `InferenceProfile`).

Ollama: `activate_profile` changes `model_load_options`, which `llm_core`
resolves fresh on every request — no process is touched, no server is
restarted, and this module never claims otherwise. Every other engine stays
entirely `deferred`, carrying a `plan` for a person to relaunch with.
"""
from __future__ import annotations

import pytest

from src.bench import profiles
from src.contracts.base import now_iso
from src.contracts.inference import InferenceProfile
from src import model_load_options as mlo
from src import settings as settings_mod


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    import src.constants as constants
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    settings_mod._invalidate_caches()
    mlo.reset_endpoint_cache()
    yield
    settings_mod._invalidate_caches()
    mlo.reset_endpoint_cache()


@pytest.fixture
def declared_endpoint(monkeypatch):
    """The endpoint `activate_profile` resolves an Ollama profile's
    host:port against — `_resolve_ollama_endpoint_id` reuses
    `routes.local_models_routes.list_ollama_endpoints`, so a test only
    needs to fake that one call, never a real DB."""
    import routes.local_models_routes as lmr
    monkeypatch.setattr(lmr, "list_ollama_endpoints", lambda **kw: [
        {"id": "local-ollama", "name": "Ollama (this machine)", "root": "http://127.0.0.1:11434",
         "base_url": "http://127.0.0.1:11434/v1", "same_machine": True},
    ])


def _ollama_profile(*, label, model="qwen3.5:9b", options=None):
    raw = {
        "id": f"profile-{label}", "label": label, "model": {"artifact_id": model},
        "engine": {"implementation": "ollama", "host": "127.0.0.1", "port": 11434},
        "hardware_id": None, "options": options or {}, "objective": "interactive",
        "created_at": now_iso(),
    }
    return InferenceProfile.parse(raw)


def _serve_profile(*, label, implementation="llama-server", model="org/model", options=None):
    raw = {
        "id": f"profile-{label}", "label": label, "model": {"artifact_id": model},
        "engine": {"implementation": implementation, "host": "127.0.0.1", "port": 8080},
        "hardware_id": None, "options": options or {}, "objective": "interactive",
        "created_at": now_iso(),
    }
    return InferenceProfile.parse(raw)


# ── Ollama: per-request vs deferred (T05) ───────────────────────────────────

def test_activate_ollama_applies_per_request_options_and_defers_server_scoped(declared_endpoint):
    profile = _ollama_profile(label="a", options={
        "num_ctx": 16384, "keep_alive": "30m", "flash_attn": True, "kv_cache_type": "q8_0",
    })
    profiles.save_profile(profile)

    result = profiles.activate_profile(profile.id, owner="luis")

    assert result["profile_id"] == profile.id
    assert result["scope"] == "next_request"
    assert result["applied"] == {
        "endpoint_id": "local-ollama", "model": "qwen3.5:9b",
        "options": {"num_ctx": 16384, "keep_alive": "30m"},
    }
    assert result["deferred"]["options"] == {"flash_attn": True, "kv_cache_type": "q8_0"}
    assert result["deferred"]["scope"] == "requires_restart"
    assert "server-scoped" in result["deferred"]["note"].lower()
    assert result["previous_profile_id"] is None

    # llm_core reads this on EVERY request — "next request" is literal.
    saved = mlo.get_options("local-ollama", "qwen3.5:9b")
    assert saved == {"num_ctx": 16384, "keep_alive": "30m"}
    assert profiles.active_profile_for("http://127.0.0.1:11434", "qwen3.5:9b") == profile.id


def test_activate_ollama_merges_onto_existing_options_never_replaces_wholesale(declared_endpoint):
    mlo.set_options("local-ollama", "qwen3.5:9b", {"main_gpu": 1})
    profile = _ollama_profile(label="b", options={"num_ctx": 8192})
    profiles.save_profile(profile)

    profiles.activate_profile(profile.id)

    saved = mlo.get_options("local-ollama", "qwen3.5:9b")
    assert saved == {"main_gpu": 1, "num_ctx": 8192}  # main_gpu untouched


def test_activate_ollama_without_a_declared_endpoint_is_refused(monkeypatch):
    import routes.local_models_routes as lmr
    monkeypatch.setattr(lmr, "list_ollama_endpoints", lambda **kw: [])
    profile = _ollama_profile(label="c")
    profiles.save_profile(profile)

    with pytest.raises(profiles.ActivationRefused):
        profiles.activate_profile(profile.id)
    # Nothing was written, nothing marked active.
    assert profiles.active_profile_for("http://127.0.0.1:11434", "qwen3.5:9b") is None


def test_activate_unknown_profile_raises_profile_not_found():
    with pytest.raises(profiles.ProfileNotFound):
        profiles.activate_profile("does-not-exist")


# ── serve engines: entirely deferred, with a relaunch plan ─────────────────

def test_activate_serve_engine_is_entirely_deferred_with_a_relaunch_plan():
    profile = _serve_profile(label="d", options={"ctx": 8192, "flash_attn": True})
    profiles.save_profile(profile)

    result = profiles.activate_profile(profile.id)

    assert result["scope"] == "requires_restart"
    assert result["applied"] == {}
    assert result["deferred"]["plan"] == {
        "implementation": "llama-server", "model": "org/model",
        "options": {"ctx": 8192, "flash_attn": True},
    }
    assert result["deferred"]["scope"] == "requires_restart"
    assert "relaunch" in result["note"].lower()
    # Never launches or restarts anything on its own.
    assert profiles.active_profile_for("http://127.0.0.1:8080", "org/model") == profile.id


# ── deactivate: restore, or remove only what was added ──────────────────────

def test_deactivate_restores_the_previous_options(declared_endpoint):
    mlo.set_options("local-ollama", "qwen3.5:9b", {"num_ctx": 4096, "keep_alive": "5m"})
    profile = _ollama_profile(label="e", options={"num_ctx": 32768})
    profiles.save_profile(profile)
    profiles.activate_profile(profile.id)
    assert mlo.get_options("local-ollama", "qwen3.5:9b")["num_ctx"] == 32768

    profiles.deactivate_profile(profile.id)

    assert mlo.get_options("local-ollama", "qwen3.5:9b") == {"num_ctx": 4096, "keep_alive": "5m"}
    assert profiles.active_profile_for("http://127.0.0.1:11434", "qwen3.5:9b") is None


def test_deactivate_without_prior_options_removes_only_what_it_added(declared_endpoint):
    profile = _ollama_profile(label="f", options={"num_ctx": 32768})
    profiles.save_profile(profile)
    profiles.activate_profile(profile.id)
    assert mlo.get_options("local-ollama", "qwen3.5:9b") == {"num_ctx": 32768}

    profiles.deactivate_profile(profile.id)

    assert mlo.get_options("local-ollama", "qwen3.5:9b") == {}


def test_deactivate_unactivated_profile_raises_profile_not_found():
    profile = _ollama_profile(label="g")
    profiles.save_profile(profile)
    with pytest.raises(profiles.ProfileNotFound):
        profiles.deactivate_profile(profile.id)


def test_deactivate_never_touches_a_different_models_options(declared_endpoint):
    mlo.set_options("local-ollama", "other-model", {"num_ctx": 4096})
    profile = _ollama_profile(label="h", options={"num_ctx": 32768})
    profiles.save_profile(profile)
    profiles.activate_profile(profile.id)

    profiles.deactivate_profile(profile.id)

    assert mlo.get_options("local-ollama", "other-model") == {"num_ctx": 4096}


# ── regression rollback: a proposal, never an automatic revert (§13) ───────

def test_rollback_proposal_names_the_previous_profile_and_never_reverts(declared_endpoint):
    a = _ollama_profile(label="i1", options={"num_ctx": 8192})
    b = _ollama_profile(label="i2", options={"num_ctx": 16384})
    profiles.save_profile(a)
    profiles.save_profile(b)
    profiles.activate_profile(a.id)
    result_b = profiles.activate_profile(b.id)
    assert result_b["previous_profile_id"] == a.id

    proposal = profiles.rollback_proposal(b.id)

    assert proposal["previous_profile_id"] == a.id
    assert proposal["reason"]
    # Still active: a proposal is not an action.
    assert profiles.active_profile_for("http://127.0.0.1:11434", b.model.artifact_id) == b.id
    assert mlo.get_options("local-ollama", b.model.artifact_id)["num_ctx"] == 16384


def test_rollback_proposal_for_a_never_activated_profile_names_nothing():
    profile = _ollama_profile(label="j")
    profiles.save_profile(profile)
    proposal = profiles.rollback_proposal(profile.id)
    assert proposal["previous_profile_id"] is None


def test_rollback_proposal_unknown_profile_raises_profile_not_found():
    with pytest.raises(profiles.ProfileNotFound):
        profiles.rollback_proposal("does-not-exist")


# ── the activation bookkeeping row never masquerades as a saved profile ────

def test_activations_bookkeeping_never_appears_in_list_profiles(declared_endpoint):
    profile = _ollama_profile(label="k")
    profiles.save_profile(profile)
    profiles.activate_profile(profile.id)

    listed_ids = {p.id for p in profiles.list_profiles()}
    assert listed_ids == {profile.id}
    assert profiles.get_profile(profiles._ACTIVATIONS_KEY) is None


def test_current_profile_names_an_external_ollama_by_its_url(monkeypatch):
    """First real run (12-09-2026): the resident Ollama at 127.0.0.1:11434 came
    out `implementation: unknown, managed: faustus` — nothing Faustus did not
    launch is managed by it, and an Ollama URL is recognisable on its own."""
    from src import launch_receipts
    from src.bench import profiles
    monkeypatch.setattr(launch_receipts, "identity_for_endpoint", lambda url, **k: None)
    monkeypatch.setattr(launch_receipts, "find_by_endpoint", lambda host, port: None)
    p = profiles.current_profile("http://127.0.0.1:11434/v1", "qwen:27b", "alice")
    assert p.engine.implementation == "ollama"
    assert p.engine.managed == "external"
    assert (p.engine.host, p.engine.port) == ("127.0.0.1", 11434)
    q = profiles.current_profile("http://10.0.0.5:8080/v1", "x", "alice")
    assert q.engine.implementation == "unknown" and q.engine.managed == "external"
