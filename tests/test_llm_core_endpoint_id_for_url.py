"""`src/llm_core.py::_endpoint_id_for_url` — real `endpoint_id` for
`apply_openrouter_payload` from the three call sites (`llm_call`,
`_llm_call_async_impl`, `_stream_llm_inner`) that only ever have a bare
`url`/`model`, not a saved endpoint config.

Isolated on-disk sqlite engine per test (same idiom as
`tests/test_cli_model_routes.py`), never the shared in-memory test DB, so
this file's rows can never leak into another test's assertions.
"""
from __future__ import annotations

import pytest

from src import llm_core


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    import core.database as core_database
    import src.database as src_database
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(
        f'sqlite:///{(tmp_path / "endpoints.db").as_posix()}',
        connect_args={"check_same_thread": False},
    )
    core_database.ModelEndpoint.__table__.create(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    # `_endpoint_id_for_url` does `from src.database import SessionLocal`,
    # not `core.database` directly, so that is the binding that must move.
    monkeypatch.setattr(core_database, "SessionLocal", sessions)
    monkeypatch.setattr(src_database, "SessionLocal", sessions)
    yield sessions
    engine.dispose()


def _add_endpoint(sessions, **kwargs):
    from core.database import ModelEndpoint
    db = sessions()
    try:
        db.add(ModelEndpoint(**kwargs))
        db.commit()
    finally:
        db.close()


def test_matches_a_configured_endpoint_by_base_url(isolated_db):
    _add_endpoint(
        isolated_db, id="ep-or", name="OpenRouter",
        base_url="https://openrouter.ai/api/v1", is_enabled=True,
    )
    assert llm_core._endpoint_id_for_url("https://openrouter.ai/api/v1") == "ep-or"


def test_matches_through_the_same_normalization_as_the_cached_model_lookup(isolated_db):
    """`_model_list_base` strips a trailing `/chat/completions`, so a caller
    handing the full chat URL still resolves to the endpoint saved with the
    bare base URL — the exact normalization `_configured_cached_model_ids`
    already relies on for the same kind of match."""
    _add_endpoint(
        isolated_db, id="ep-or", name="OpenRouter",
        base_url="https://openrouter.ai/api/v1", is_enabled=True,
    )
    url = "https://openrouter.ai/api/v1/chat/completions"
    assert llm_core._endpoint_id_for_url(url) == "ep-or"


def test_no_match_returns_none(isolated_db):
    _add_endpoint(
        isolated_db, id="ep-or", name="OpenRouter",
        base_url="https://openrouter.ai/api/v1", is_enabled=True,
    )
    assert llm_core._endpoint_id_for_url("https://api.example.test/v1") is None


def test_disabled_endpoint_is_never_matched(isolated_db):
    _add_endpoint(
        isolated_db, id="ep-or", name="OpenRouter",
        base_url="https://openrouter.ai/api/v1", is_enabled=False,
    )
    assert llm_core._endpoint_id_for_url("https://openrouter.ai/api/v1") is None


def test_empty_url_returns_none_without_touching_the_db(isolated_db):
    assert llm_core._endpoint_id_for_url("") is None


def test_openrouter_payload_receives_the_real_endpoint_id_end_to_end(isolated_db, monkeypatch, tmp_path):
    """The exact bug this closes: a real saved endpoint's per-endpoint
    OpenRouter preferences (e.g. `sort`) now reach the payload from a plain
    url/model call, instead of always being skipped because `endpoint_id`
    was hardcoded to `None`."""
    import src.openrouter_options as oo

    _add_endpoint(
        isolated_db, id="ep-or", name="OpenRouter",
        base_url="https://openrouter.ai/api/v1", is_enabled=True,
    )
    monkeypatch.setattr(oo, "STORE_PATH", str(tmp_path / "openrouter_endpoints.json"))
    oo.set_prefs("ep-or", {"sort": "price"})

    resolved = llm_core._endpoint_id_for_url("https://openrouter.ai/api/v1/chat/completions")
    payload = {"model": "openai/gpt-4o"}
    oo.apply_openrouter_payload(payload, provider="openrouter", endpoint_id=resolved, model="openai/gpt-4o")
    assert payload["provider"]["sort"] == "price"
