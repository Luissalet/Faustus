"""L43 · MOD-04 — per-model load options with scope (global/project/session)
and precedence (docs/spec/v2/backlog.json MOD-04).

Before this lote ``src/model_load_options.py`` only had ONE scope: a flat
"endpoint|model" -> options table, always global. This adds a project and a
session scope on top of it — the global table itself is untouched (every
existing key/shape/behaviour), so this is additive per COMUN.md rule 3.

``resolve_with_origin`` is the "quien manda" answer the Local models UI shows:
for each field, which scope supplied the winning value and what every WEAKER
scope that also had an opinion offered instead — the same overridden/source
shape ``src/effective_config.py`` uses for its own (bigger) ladder, echoed
here without importing that module (it has no endpoint URL to resolve the
global layer against — see the docstring on ``resolve_with_origin``).
"""
from __future__ import annotations

import pytest

from src import model_load_options as mlo
from src import settings as settings_mod


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    settings_mod._invalidate_caches()
    mlo.reset_endpoint_cache()
    monkeypatch.setattr(mlo, "_endpoint_bases", lambda: {
        "local-ollama": "http://localhost:11434/v1",
    })
    yield
    settings_mod._invalidate_caches()
    mlo.reset_endpoint_cache()


URL = "http://127.0.0.1:11434/api/chat"


def test_scoped_set_get_and_clear_do_not_touch_the_global_table(store):
    mlo.set_options("local-ollama", "qwen3.5:9b", {"num_ctx": 8192})
    mlo.set_scoped_options("local-ollama", "qwen3.5:9b", mlo.SCOPE_PROJECT, "proj1", {"num_ctx": 16384})
    # Global table (the pre-existing store) is exactly what it was.
    assert mlo.get_options("local-ollama", "qwen3.5:9b") == {"num_ctx": 8192}
    assert mlo.get_scoped_options("local-ollama", "qwen3.5:9b", mlo.SCOPE_PROJECT, "proj1") == {"num_ctx": 16384}
    assert mlo.get_scoped_options("local-ollama", "qwen3.5:9b", mlo.SCOPE_SESSION, "s1") == {}
    # Clearing the project override leaves the global entry alone.
    mlo.set_scoped_options("local-ollama", "qwen3.5:9b", mlo.SCOPE_PROJECT, "proj1", {})
    assert mlo.get_scoped_options("local-ollama", "qwen3.5:9b", mlo.SCOPE_PROJECT, "proj1") == {}
    assert mlo.get_options("local-ollama", "qwen3.5:9b") == {"num_ctx": 8192}


def test_precedence_session_beats_project_beats_global(store):
    mlo.set_options("local-ollama", "qwen3.5:9b", {"num_ctx": 8192, "keep_alive": "5m"})
    mlo.set_scoped_options("local-ollama", "qwen3.5:9b", mlo.SCOPE_PROJECT, "proj1", {"num_ctx": 16384})
    mlo.set_scoped_options("local-ollama", "qwen3.5:9b", mlo.SCOPE_SESSION, "sess1", {"num_ctx": 32768})

    resolved = mlo.resolve_with_origin(URL, "qwen3.5:9b", session_id="sess1", project_id="proj1")
    assert resolved["options"]["num_ctx"] == 32768
    assert resolved["origin"]["num_ctx"]["scope"] == "session"
    # keep_alive only has a global opinion — it still resolves, from the
    # weakest rung, and is not shadowed by the other two scopes existing.
    assert resolved["options"]["keep_alive"] == "5m"
    assert resolved["origin"]["keep_alive"]["scope"] == "global"
    # num_ctx: project and global both had an opinion and both lost to
    # session — "outranked, not refused" is recorded for each.
    lost_scopes = {o["scope"] for o in resolved["overridden"]["num_ctx"]}
    assert lost_scopes == {"project", "global"}
    lost_values = {o["scope"]: o["value"] for o in resolved["overridden"]["num_ctx"]}
    assert lost_values == {"project": 16384, "global": 8192}


def test_project_beats_global_when_no_session_override_exists(store):
    mlo.set_options("local-ollama", "qwen3.5:9b", {"num_ctx": 8192})
    mlo.set_scoped_options("local-ollama", "qwen3.5:9b", mlo.SCOPE_PROJECT, "proj1", {"num_ctx": 16384})
    resolved = mlo.resolve_with_origin(URL, "qwen3.5:9b", session_id="sess-with-nothing-saved", project_id="proj1")
    assert resolved["options"]["num_ctx"] == 16384
    assert resolved["origin"]["num_ctx"]["scope"] == "project"


def test_resolve_for_request_stays_two_argument_compatible(store):
    """llm_core.py calls resolve_for_request(url, model) with NO session/
    project — the exact call this lote must not break (rule 3)."""
    mlo.set_options("local-ollama", "qwen3.5:9b", {"num_ctx": 8192})
    assert mlo.resolve_for_request(URL, "qwen3.5:9b") == {"num_ctx": 8192}


def test_resolve_for_request_prefers_the_scoped_override_when_given(store):
    mlo.set_options("local-ollama", "qwen3.5:9b", {"num_ctx": 8192})
    mlo.set_scoped_options("local-ollama", "qwen3.5:9b", mlo.SCOPE_SESSION, "sess1", {"num_ctx": 32768})
    assert mlo.resolve_for_request(URL, "qwen3.5:9b", session_id="sess1") == {"num_ctx": 32768}


def test_scope_only_override_resolves_with_no_global_entry_ever_saved(store):
    """A project override can exist with NOTHING saved globally for that
    model — the endpoint is still resolved from the configured endpoint
    table (`_endpoint_bases`), not only from an existing global key."""
    mlo.set_scoped_options("local-ollama", "qwen3.5:9b", mlo.SCOPE_PROJECT, "proj1", {"num_gpu": 20})
    resolved = mlo.resolve_with_origin(URL, "qwen3.5:9b", project_id="proj1")
    assert resolved["options"] == {"num_gpu": 20}
    assert resolved["origin"]["num_gpu"]["scope"] == "project"
    assert resolved["endpoint_id"] == "local-ollama"


def test_explicit_endpoint_id_skips_netloc_guessing(store):
    """A caller that already resolved its endpoint (every route does) can
    say so directly instead of relying on this module re-deriving it from
    `url` through a possibly different endpoint table."""
    mlo.set_scoped_options("some-other-id", "qwen3.5:9b", mlo.SCOPE_PROJECT, "proj1", {"num_ctx": 4096})
    # No endpoint at this netloc is configured at all (`_endpoint_bases` is
    # empty and the URL is not the machine default) — without an explicit
    # endpoint_id this would resolve nothing.
    resolved = mlo.resolve_with_origin(
        "http://unrelated-host:9999", "qwen3.5:9b", project_id="proj1", endpoint_id="some-other-id",
    )
    assert resolved["options"] == {"num_ctx": 4096}
    assert resolved["endpoint_id"] == "some-other-id"


def test_scope_id_is_required_and_empty_value_clears(store):
    with pytest.raises(ValueError):
        mlo.set_scoped_options("local-ollama", "qwen3.5:9b", mlo.SCOPE_PROJECT, "", {"num_ctx": 1024})
    with pytest.raises(ValueError):
        mlo.set_scoped_options("local-ollama", "qwen3.5:9b", "bogus-scope", "p1", {"num_ctx": 1024})


def test_all_scoped_options_drops_malformed_entries_without_raising(store):
    settings_mod.save_settings({mlo.SCOPED_SETTING_KEY: {
        "local-ollama|qwen3.5:9b::project:p1": {"num_ctx": 4096},
        "not-a-scoped-key": {"num_ctx": 1},
        "local-ollama|qwen3.5:9b::project:p2": "not a dict",
        "local-ollama|qwen3.5:9b::project:p3": {"num_ctx": "not-an-int"},
    }})
    out = mlo.all_scoped_options()
    assert out == {"local-ollama|qwen3.5:9b::project:p1": {"num_ctx": 4096}}
