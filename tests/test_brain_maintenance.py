"""
tests/test_brain_maintenance.py — the three brain tasks in the background
scheduler: gated on `brain_enabled`, fanned out per owner, never raising.

Isolation mirrors `tests/test_l92_board_routes.py` / `test_brain_routes.py`:
a fresh brain store (`src.brain.db.use_dir`), a fresh context-engine store
(`src.context_engine.store.use_path`) so the maintenance run-history table
does not leak between tests, and a fresh `memory_engine.DATA_DIR`.
"""

from __future__ import annotations

import importlib

import pytest

from src import memory_engine as engine
from src.brain import db as brain_db
from src.context_engine import maintenance, store as ce_store

OWNER = "ada"
OTHER = "bruno"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    monkeypatch.setattr(engine, "DATA_DIR", str(memory_dir))
    engine.set_vector_store(None)
    engine.clear_injected()

    brain_db.use_dir(str(tmp_path / "brain"))
    ce_store.use_path(str(tmp_path / "context_engine.db"))

    yield

    brain_db.use_dir(None)
    ce_store.use_path(None)
    engine.clear_injected()


def _enable_brain(monkeypatch, enabled: bool = True) -> None:
    from src import settings as settings_mod

    monkeypatch.setattr(
        settings_mod, "get_setting",
        lambda key, default=None: enabled if key == "brain_enabled"
        else default,
    )


# ── registration ─────────────────────────────────────────────────────────

def test_brain_tasks_are_registered():
    for name in ("brain_vault_sync", "brain_extract", "brain_wiki"):
        assert name in maintenance.TASK_NAMES
        assert name in maintenance.TASKS
        assert name in maintenance.TASK_INTERVALS_S
        assert name in maintenance.OWNER_TASK_NAMES


# ── due() gating ─────────────────────────────────────────────────────────

def test_due_excludes_brain_tasks_when_disabled(monkeypatch):
    _enable_brain(monkeypatch, False)
    ready = maintenance.due()
    for name in ("brain_vault_sync", "brain_extract", "brain_wiki"):
        assert name not in ready


def test_due_includes_brain_tasks_when_enabled(monkeypatch):
    _enable_brain(monkeypatch, True)
    ready = maintenance.due()
    for name in ("brain_vault_sync", "brain_extract", "brain_wiki"):
        assert name in ready


# ── individual tasks fail closed and respect settings ───────────────────

def test_brain_vault_sync_noop_when_disabled(monkeypatch):
    _enable_brain(monkeypatch, False)
    called = []
    monkeypatch.setattr("src.brain.vault.sync",
                        lambda owner, **kw: called.append(owner) or {})
    changed, detail = maintenance._brain_vault_sync(owner=OWNER)
    assert changed == 0
    assert not called
    assert "disabled" in detail


def test_brain_vault_sync_calls_vault(monkeypatch):
    _enable_brain(monkeypatch, True)
    seen = {}

    def fake_sync(owner, **kw):
        seen["owner"] = owner
        return {"exported": 3, "imported": 1, "suppressed": 0,
                "guard_tripped": False, "errors": []}

    monkeypatch.setattr("src.brain.vault.sync", fake_sync)
    changed, detail = maintenance._brain_vault_sync(owner=OWNER)
    assert seen["owner"] == OWNER
    assert changed == 4
    assert "exported 3" in detail


def test_brain_extract_noop_when_disabled(monkeypatch):
    _enable_brain(monkeypatch, False)
    called = []
    monkeypatch.setattr(
        "src.brain.extract.extract_pending",
        lambda owner, **kw: called.append(owner),
    )
    changed, detail = maintenance._brain_extract(owner=OWNER)
    assert changed == 0
    assert not called


def test_brain_extract_calls_extract_pending(monkeypatch):
    _enable_brain(monkeypatch, True)

    async def fake_extract(owner, **kw):
        return {"processed": 2, "entities": 1, "relations": 1, "errors": 0}

    monkeypatch.setattr("src.brain.extract.extract_pending", fake_extract)
    changed, detail = maintenance._brain_extract(owner=OWNER)
    assert changed == 2
    assert "1 entit" in detail


def test_brain_wiki_off_when_summaries_disabled(monkeypatch):
    from src import settings as settings_mod

    monkeypatch.setattr(
        settings_mod, "get_setting",
        lambda key, default=None: True if key == "brain_enabled"
        else (False if key == "brain_wiki_summaries" else default),
    )
    called = []
    monkeypatch.setattr(
        "src.brain.wiki.refresh_stale",
        lambda owner, **kw: called.append(owner),
    )
    changed, detail = maintenance._brain_wiki(owner=OWNER)
    assert changed == 0
    assert not called
    assert "off" in detail


def test_brain_wiki_calls_refresh_stale(monkeypatch):
    from src import settings as settings_mod

    monkeypatch.setattr(
        settings_mod, "get_setting",
        lambda key, default=None: True if key in
        ("brain_enabled", "brain_wiki_summaries") else default,
    )

    async def fake_refresh(owner, **kw):
        return {"checked": 2, "updated": 1, "skipped": 1, "errors": 0}

    monkeypatch.setattr("src.brain.wiki.refresh_stale", fake_refresh)
    changed, detail = maintenance._brain_wiki(owner=OWNER)
    assert changed == 1
    assert "1 page(s) refreshed" in detail


# ── owner enumeration ────────────────────────────────────────────────────

def test_brain_owners_unions_notes_entities_and_memory(monkeypatch):
    with brain_db.db() as conn:
        conn.execute(
            "INSERT INTO notes (owner, path, title) VALUES (?, ?, ?)",
            (OWNER, "Notes/x.md", "x"),
        )
    from src.brain import entities

    entities.upsert_entity(OTHER, "Cordera Labs", type="organization")
    engine.add_item("remembers something", owner="cordelia")

    owners = maintenance._brain_owners()
    assert owners == sorted({OWNER, OTHER, "cordelia"})


def test_brain_owners_empty_store_is_empty_list():
    assert maintenance._brain_owners() == []


# ── scheduled fan-out ────────────────────────────────────────────────────

def test_run_scheduled_fans_brain_tasks_over_owners(monkeypatch):
    _enable_brain(monkeypatch, True)
    with brain_db.db() as conn:
        conn.execute(
            "INSERT INTO notes (owner, path, title) VALUES (?, ?, ?)",
            (OWNER, "Notes/a.md", "a"),
        )
        conn.execute(
            "INSERT INTO notes (owner, path, title) VALUES (?, ?, ?)",
            (OTHER, "Notes/b.md", "b"),
        )

    calls = []

    def fake_sync(owner, **kw):
        calls.append(owner)
        return {"exported": 0, "imported": 0, "suppressed": 0,
                "guard_tripped": False, "errors": []}

    async def fake_extract(owner, **kw):
        calls.append(("extract", owner))
        return {"processed": 0, "entities": 0, "relations": 0, "errors": 0}

    async def fake_wiki(owner, **kw):
        calls.append(("wiki", owner))
        return {"checked": 0, "updated": 0, "skipped": 0, "errors": 0}

    monkeypatch.setattr("src.brain.vault.sync", fake_sync)
    monkeypatch.setattr("src.brain.extract.extract_pending", fake_extract)
    monkeypatch.setattr("src.brain.wiki.refresh_stale", fake_wiki)

    results = maintenance.run_scheduled(projects=[], budget_s=10.0)
    by_name = {r.name: r for r in results}

    assert OWNER in calls and OTHER in calls
    assert by_name["brain_vault_sync"].ok
    assert "2/2 owner(s)" in by_name["brain_vault_sync"].detail
    assert ("extract", OWNER) in calls and ("extract", OTHER) in calls
    assert ("wiki", OWNER) in calls and ("wiki", OTHER) in calls


def test_run_scheduled_skips_brain_when_disabled(monkeypatch):
    _enable_brain(monkeypatch, False)
    results = maintenance.run_scheduled(projects=[], budget_s=10.0)
    names = {r.name for r in results}
    assert "brain_vault_sync" not in names
    assert "brain_extract" not in names
    assert "brain_wiki" not in names
