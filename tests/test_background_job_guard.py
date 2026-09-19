"""A scheduled/background job must never load a model on its own.

Real incident: a 02:00 scheduled skill audit resolved to a 32B model that
was not already loaded and made a non-streaming call anyway, loading it
(37.5 GB VRAM) while the owner's main model was serving a long task on
another engine. Two things are covered here:

1. `src.background_job_guard.should_run_with_model` — the choke point a
   background job calls before spending a model call: postpones when the
   model isn't resident and the job isn't user-initiated, unless the owner
   opted in via `background_jobs_may_load_models`.
2. `routes.skills_routes._resolve_audit_models` no longer guesses
   `_avail[0]` when the configured model isn't on the endpoint, and
   `run_scheduled_skill_audit` wires the guard in before it runs.
"""
import asyncio

import pytest

import src.background_job_guard as guard


def test_user_initiated_run_bypasses_the_guard(monkeypatch):
    # Even a model that would need loading, and the setting off, never blocks
    # a run the person explicitly asked for right now.
    monkeypatch.setattr(guard, "would_require_load", lambda url, model: True)
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: default)
    assert guard.should_run_with_model("skill_audit", "http://127.0.0.1:11434/v1", "big-model", user_initiated=True) is True


def test_background_job_postpones_a_non_resident_model(monkeypatch, caplog):
    monkeypatch.setattr(guard, "would_require_load", lambda url, model: True)
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: default)
    with caplog.at_level("INFO"):
        ok = guard.should_run_with_model("skill_audit", "http://127.0.0.1:11434/v1", "deepseek-r1:32b", user_initiated=False)
    assert ok is False
    assert any("scheduled skill_audit: postponed, would load deepseek-r1:32b" in r.message for r in caplog.records)


def test_background_job_proceeds_when_model_is_already_resident(monkeypatch):
    monkeypatch.setattr(guard, "would_require_load", lambda url, model: False)
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: default)
    assert guard.should_run_with_model("skill_audit", "http://127.0.0.1:11434/v1", "resident-model", user_initiated=False) is True


def test_setting_true_lets_a_background_job_load_a_model(monkeypatch):
    monkeypatch.setattr(guard, "would_require_load", lambda url, model: True)
    monkeypatch.setattr(
        "src.settings.get_setting",
        lambda key, default=None: True if key == "background_jobs_may_load_models" else default,
    )
    assert guard.should_run_with_model("skill_audit", "http://127.0.0.1:11434/v1", "big-model", user_initiated=False) is True


def test_would_require_load_true_when_residency_cannot_be_determined(monkeypatch):
    # Non-Ollama / unreachable endpoint: unknown residency must never be read
    # as "safe to load" by an unattended job.
    monkeypatch.setattr(guard, "_resident_model_names", lambda url: None)
    assert guard.would_require_load("http://remote.example/v1", "some-model") is True


def test_would_require_load_false_when_model_is_in_the_loaded_list(monkeypatch):
    monkeypatch.setattr(guard, "_resident_model_names", lambda url: ["qwen3:8b", "other-model"])
    assert guard.would_require_load("http://127.0.0.1:11434/v1", "qwen3:8b") is False
    assert guard.would_require_load("http://127.0.0.1:11434/v1", "deepseek-r1:32b") is True


# ---- routes.skills_routes wiring ----


def test_resolve_audit_models_never_guesses_first_available(monkeypatch):
    """The bug: a model that doesn't match the endpoint's list used to fall
    back to `_avail[0]` — whatever the endpoint happened to list first."""
    import routes.skills_routes as skills_routes

    monkeypatch.setattr(
        "src.endpoint_resolver.resolve_endpoint",
        lambda role, owner=None: ("http://127.0.0.1:11434/v1", "configured-model", {}),
    )
    monkeypatch.setattr(
        "src.llm_core.list_model_ids",
        lambda url, headers=None: ["deepseek-r1:32b", "some-other-model"],
    )
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: default)

    with pytest.raises(ValueError, match="configured-model"):
        skills_routes._resolve_audit_models(owner=None)


def test_resolve_audit_models_picks_the_utility_model_when_available(monkeypatch):
    import routes.skills_routes as skills_routes

    monkeypatch.setattr(
        "src.endpoint_resolver.resolve_endpoint",
        lambda role, owner=None: ("http://127.0.0.1:11434/v1", "utility-model", {}),
    )
    monkeypatch.setattr("src.llm_core.list_model_ids", lambda url, headers=None: ["utility-model"])
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: default)

    url, model, headers, teacher = skills_routes._resolve_audit_models(owner=None)
    assert (url, model) == ("http://127.0.0.1:11434/v1", "utility-model")
    assert teacher is None


def test_run_scheduled_skill_audit_postpones_instead_of_loading(monkeypatch):
    import routes.skills_routes as skills_routes

    monkeypatch.setattr(
        skills_routes,
        "_resolve_audit_models",
        lambda owner=None: ("http://127.0.0.1:11434/v1", "deepseek-r1:32b", {}, None),
    )
    monkeypatch.setattr(
        "src.background_job_guard.should_run_with_model",
        lambda job_name, url, model, user_initiated=False: False,
    )

    async def _boom(*args, **kwargs):
        raise AssertionError("must not run the audit job when the guard postpones it")

    monkeypatch.setattr(skills_routes, "_run_audit_all_job", _boom)

    class _FakeSkillsManager:
        def load(self, owner=None):
            raise AssertionError("must not even list skills once postponed")

    result = asyncio.run(skills_routes.run_scheduled_skill_audit(_FakeSkillsManager(), owner=None))
    assert result["status"] == "skipped"
    assert "deepseek-r1:32b" in result["reason"]
