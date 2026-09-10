"""OPS-02 / QA-45 — formal schema migrations: backup before, rollback on a
mid-migration failure, no re-backup when nothing changed.

Every test here proves its point by reverting `src/migrations.py` to a stub
that does nothing (see `test_the_wrapper_actually_prevents_data_loss`) is
unnecessary — the whole point of this module is that a step CAN raise, so the
tests inject the failure themselves rather than needing to break the source
to prove they would catch a regression.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from src import migrations


@pytest.fixture()
def engine_and_db(tmp_path, monkeypatch):
    monkeypatch.setenv("FAUSTUS_BACKUP_DIR", str(tmp_path / "backups"))
    db_path = tmp_path / "app.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT)"))
        conn.execute(text("INSERT INTO widgets (name) VALUES ('a'), ('b')"))
    yield engine, db_path
    engine.dispose()


def test_a_clean_migration_is_recorded_and_applies_every_step(engine_and_db):
    engine, _ = engine_and_db
    applied = []

    def step_one():
        applied.append("one")
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE widgets ADD COLUMN color TEXT"))

    result = migrations.run(engine, [("step_one", step_one)], name="test")
    assert result["ok"] and result["applied"]
    assert applied == ["one"]
    row = migrations.last_applied(engine)
    assert row is not None and row["steps"] == 1


def test_rerunning_the_same_steps_skips_the_backup_and_does_not_reapply(engine_and_db):
    engine, _ = engine_and_db
    calls = {"n": 0}

    def step():
        calls["n"] += 1

    steps = [("step", step)]
    first = migrations.run(engine, steps, name="test")
    second = migrations.run(engine, steps, name="test")
    assert first["applied"] is True
    assert second["applied"] is False
    assert calls["n"] == 1, "an unchanged step set must not re-run"


def test_a_failure_mid_migration_restores_the_previous_consistent_database(engine_and_db):
    """The literal QA-45 acceptance bar: 'recuperacion consistente con backup,
    no base vacia ni datos mezclados' — simulate a crash injected partway
    through a multi-step migration and prove the file comes back exactly as
    it was, not empty, not with the failed step's partial write surviving.
    """
    engine, db_path = engine_and_db

    def step_ok():
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE widgets ADD COLUMN color TEXT"))

    def step_boom():
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO widgets (name) VALUES ('mid-migration')"))
        raise RuntimeError("simulated power cut mid-migration")

    with pytest.raises(migrations.MigrationFailed):
        migrations.run(engine, [("step_ok", step_ok), ("step_boom", step_boom)], name="test")

    engine.dispose()
    fresh = create_engine(f"sqlite:///{db_path}")
    try:
        with fresh.connect() as conn:
            cols = [r[1] for r in conn.execute(text("PRAGMA table_info(widgets)"))]
            rows = [r[0] for r in conn.execute(text("SELECT name FROM widgets ORDER BY id"))]
        assert "color" not in cols, "the failed step's schema change must not survive"
        assert rows == ["a", "b"], "the failed step's data write must not survive"
        assert "mid-migration" not in rows, "database ended up MIXED, not restored"
    finally:
        fresh.dispose()


def test_a_pre_migration_backup_file_is_actually_written(engine_and_db, tmp_path):
    engine, _ = engine_and_db

    def step():
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE widgets ADD COLUMN size TEXT"))

    result = migrations.run(engine, [("step", step)], name="test")
    assert result["backup"], "a live db file must be backed up before migrating"
    assert Path(result["backup"]).is_file()


def test_checksum_changes_when_the_step_sequence_changes():
    a = migrations.checksum_of([("x", lambda: None)])
    b = migrations.checksum_of([("x", lambda: None), ("y", lambda: None)])
    assert a != b


# ── proof this test would have failed against the old, unwrapped call site ──

def test_without_the_wrapper_a_mid_sequence_failure_leaves_the_schema_half_applied(engine_and_db):
    """What `core/database.py` did before this lote: bare sequential calls,
    no backup, no rollback. Reproduced here (not by editing the source) to
    show the exact failure this module exists to prevent — this test is
    expected to reach the 'half-applied' state, proving the OLD behavior was
    the bug QA-45 describes.
    """
    engine, db_path = engine_and_db

    def step_ok():
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE widgets ADD COLUMN color TEXT"))

    def step_boom():
        raise RuntimeError("simulated crash, unwrapped")

    with pytest.raises(RuntimeError):
        step_ok()
        step_boom()

    with engine.connect() as conn:
        cols = [r[1] for r in conn.execute(text("PRAGMA table_info(widgets)"))]
    assert "color" in cols, (
        "this demonstrates the pre-fix bug: an unwrapped sequence leaves the "
        "schema half-applied (column added, migration otherwise incomplete) "
        "instead of restoring the previous consistent version"
    )
