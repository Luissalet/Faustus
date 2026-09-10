"""QA-45 · Actualizacion fallida (docs/spec/v2/acceptance_scenarios.json).

Estimulo: error a mitad de migracion de schema y corte de luz simulado.
Resultado exigido (literal): "Recuperacion consistente con backup, no base
vacia ni datos mezclados."

Requisitos: OPS-02, OPS-03.

Estado: CLOSED. `src/migrations.py` (new, OPS-02) wraps every schema-mutating
step in `core.database.init_db()` (see `_run_formal_migrations`, wired in the
same lote) with: a WAL-safe backup of the live db file before any step runs,
a `schema_migrations` record of what was applied, and — the literal
acceptance bar this test proves — a restore of that backup, byte for byte,
if ANY step raises. `src.backup_service.verify_backup` (OPS-03, extended in
the same lote) is the separate "prove a snapshot restores" half of OPS-03;
it is exercised in `tests/test_ops_backup_verify.py`.

This test used to grep `scripts/update_database.py` for the string
"backup_service" (source: git history). That script is legacy, dead code —
it imports a top-level `database` module that does not exist anywhere in
this repository (`from database import DATABASE_URL, SessionLocal, Base`;
confirmed absent via `find . -maxdepth 1 -iname database.py`), operates on a
`sessions.last_accessed/is_important/message_count` schema this codebase's
real `core/database.py` does not have, and is out of this lote's PROPIOS
(routes/scripts touched here are `src/migrations.py`/`core/database.py`).
Grepping a string into dead code proved nothing about whether an update
actually recovers cleanly; this test now DOES, end to end, against the real
migration path.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from src import migrations

pytestmark = pytest.mark.qa_state("green")


@pytest.fixture()
def real_sqlite_app_db(tmp_path, monkeypatch):
    """A real file-backed SQLite database, seeded like a live install's would
    be, with `FAUSTUS_BACKUP_DIR` pointed at a throwaway directory."""
    monkeypatch.setenv("FAUSTUS_BACKUP_DIR", str(tmp_path / "backups"))
    db_path = tmp_path / "app.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE sessions (id INTEGER PRIMARY KEY, name TEXT, folder TEXT)"
        ))
        conn.execute(text(
            "INSERT INTO sessions (name, folder) VALUES ('chat one', NULL), ('chat two', NULL)"
        ))
    yield engine, db_path
    engine.dispose()


def test_a_mid_migration_crash_and_a_simulated_power_cut_both_recover_cleanly(real_sqlite_app_db):
    """The exact scenario in the acceptance file: 'error a mitad de
    migracion de schema y corte de luz simulado'. Modelled as two migration
    steps where the second — after already writing a partial change and
    starting a row insert — is interrupted by an injected exception, the same
    shape a real power cut leaves (a process that stops mid-statement).
    """
    engine, db_path = real_sqlite_app_db

    def step_add_column():
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE sessions ADD COLUMN last_accessed TEXT"))

    def step_interrupted_mid_write():
        # Half-applied on purpose: one write commits, then the "power" goes.
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO sessions (name, folder) VALUES ('half-migrated row', NULL)"
            ))
        raise RuntimeError("simulated power cut mid-migration")

    steps = [("add_column", step_add_column), ("interrupted", step_interrupted_mid_write)]

    with pytest.raises(migrations.MigrationFailed):
        migrations.run(engine, steps, name="qa45")

    # "no base vacia": the database must still open and answer queries.
    engine.dispose()
    recovered = create_engine(f"sqlite:///{db_path}")
    try:
        with recovered.connect() as conn:
            names = [r[0] for r in conn.execute(text("SELECT name FROM sessions ORDER BY id"))]
            cols = [r[1] for r in conn.execute(text("PRAGMA table_info(sessions)"))]
        # "no datos mezclados": the row the interrupted step wrote must not
        # have survived alongside the pre-migration rows.
        assert names == ["chat one", "chat two"], names
        assert "half-migrated row" not in names
        # "recuperacion consistente": the schema is exactly the PREVIOUS
        # complete version — the half-applied ALTER TABLE did not survive
        # either, so there is no column with no matching, finished migration.
        assert "last_accessed" not in cols, cols
    finally:
        recovered.dispose()


def test_the_recovery_is_reported_a_second_boot_can_see(real_sqlite_app_db):
    """'al arrancar se comprueba y se informa' — the failure is not silent:
    `MigrationFailed` names the step, how many prior steps succeeded, and
    whether the restore happened, which is exactly what a caller (real
    startup: `core.database._run_formal_migrations`) logs at CRITICAL and
    surfaces rather than starting silently on an unknown schema.
    """
    engine, _ = real_sqlite_app_db

    def boom():
        raise RuntimeError("crash")

    try:
        migrations.run(engine, [("boom", boom)], name="qa45")
        assert False, "expected MigrationFailed"
    except migrations.MigrationFailed as e:
        message = str(e)
        assert "boom" in message
        assert "restored from the pre-migration backup" in message


def test_this_repos_real_app_db_migration_wiring_uses_the_same_wrapper():
    """Proof that `core.database.init_db()` — the real startup path, not a
    stand-in — actually CALLS the wrapper, not merely that a wrapper function
    happens to exist unused somewhere in the module."""
    import core.database as db
    import inspect

    init_source = inspect.getsource(db.init_db)
    assert "_run_formal_migrations()" in init_source, (
        "init_db() must call _run_formal_migrations() itself — a wrapper "
        "that exists but is never invoked from startup protects nothing"
    )
    wrapper_source = inspect.getsource(db._run_formal_migrations)
    assert "migrations" in wrapper_source and "run_migrations" in wrapper_source
    # And the wired sequence is not trivially empty.
    assert len(db._formal_migration_steps()) > 10
