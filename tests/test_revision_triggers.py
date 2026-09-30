"""Connection identity is enforced by the database, not only by the ORM hooks.

A plain SQL write to an endpoint's address, key, credential binding or kind, or
to the credentials it uses, used to leave its revision (and every probe recorded
under it) looking current. The triggers close that path; ORM writes, which
already rotate the revision in the same statement, are unaffected.
"""
import sqlite3

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as d


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "app.db"
    engine = create_engine("sqlite:///" + str(path), connect_args={"check_same_thread": False})
    d.ModelEndpoint.__table__.create(engine)
    d.ProviderAuthSession.__table__.create(engine)
    sessions = sessionmaker(bind=engine)
    with sessions() as s:
        s.add(d.ProviderAuthSession(id="auth1", provider="p", base_url="http://x/v1",
                                    access_token="t1", refresh_token="r1", auth_mode="oauth"))
        s.add(d.ModelEndpoint(id="a", name="A", base_url="http://localhost:8081/v1", api_key="k",
                              endpoint_kind="local"))
        s.add(d.ModelEndpoint(id="b", name="B", base_url="http://localhost:8082/v1",
                              provider_auth_id="auth1"))
        s.add(d.ModelEndpoint(id="c", name="C", base_url="http://localhost:8083/v1", provider_auth_id="other"))
        s.commit()
    raw = sqlite3.connect(path)
    d.install_revision_triggers(raw)
    raw.commit()
    yield raw, sessions, path
    raw.close()
    engine.dispose()


def rev(raw, table, row_id, column):
    return raw.execute(f"SELECT {column} FROM {table} WHERE id=?", (row_id,)).fetchone()[0]


@pytest.mark.parametrize("column,value", [
    ("base_url", "http://localhost:9999/v1"),
    ("api_key", "a-different-key"),
    ("provider_auth_id", "auth1"),
    ("endpoint_kind", "api"),
])
def test_plain_sql_change_of_an_identity_field_rotates_the_revision(db, column, value):
    raw, _, _ = db
    before = rev(raw, "model_endpoints", "a", "connection_revision")
    raw.execute(f"UPDATE model_endpoints SET {column}=? WHERE id='a'", (value,))
    raw.commit()
    after = rev(raw, "model_endpoints", "a", "connection_revision")
    assert after != before and len(after) == 32


def test_a_bulk_update_rotates_only_the_rows_it_changed(db):
    raw, _, _ = db
    before = {i: rev(raw, "model_endpoints", i, "connection_revision") for i in "abc"}
    raw.execute("UPDATE model_endpoints SET base_url = replace(base_url, 'localhost', '127.0.0.1') "
                "WHERE id IN ('a','b')")
    raw.commit()
    after = {i: rev(raw, "model_endpoints", i, "connection_revision") for i in "abc"}
    assert after["a"] != before["a"] and after["b"] != before["b"] and after["c"] == before["c"]


@pytest.mark.parametrize("column,value", [
    ("name", "renamed"), ("is_enabled", 0), ("cached_models", '["m"]'), ("supports_tools", 1),
])
def test_label_and_cache_changes_leave_the_revision_alone(db, column, value):
    raw, _, _ = db
    before = rev(raw, "model_endpoints", "a", "connection_revision")
    raw.execute(f"UPDATE model_endpoints SET {column}=? WHERE id='a'", (value,))
    raw.commit()
    assert rev(raw, "model_endpoints", "a", "connection_revision") == before


def test_writing_the_same_value_does_not_rotate(db):
    raw, _, _ = db
    before = rev(raw, "model_endpoints", "a", "connection_revision")
    raw.execute("UPDATE model_endpoints SET base_url = base_url, endpoint_kind = 'local' WHERE id='a'")
    raw.commit()
    assert rev(raw, "model_endpoints", "a", "connection_revision") == before


def test_an_explicit_new_revision_from_the_writer_is_kept_not_overridden(db):
    raw, _, _ = db
    raw.execute("UPDATE model_endpoints SET base_url='http://n/v1', connection_revision='fixed' WHERE id='a'")
    raw.commit()
    assert rev(raw, "model_endpoints", "a", "connection_revision") == "fixed"


def test_rolled_back_change_keeps_the_revision(db):
    raw, _, _ = db
    before = rev(raw, "model_endpoints", "a", "connection_revision")
    raw.execute("UPDATE model_endpoints SET base_url='http://n/v1' WHERE id='a'")
    raw.rollback()
    assert rev(raw, "model_endpoints", "a", "connection_revision") == before


def test_a_row_inserted_without_identity_gets_one(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as raw:
        raw.execute("CREATE TABLE model_endpoints (id TEXT PRIMARY KEY, base_url TEXT, api_key TEXT, "
                    "provider_auth_id TEXT, endpoint_kind TEXT, connection_revision TEXT)")
        d.install_revision_triggers(raw)
        raw.execute("INSERT INTO model_endpoints (id, base_url) VALUES ('x', 'http://h/v1')")
        raw.execute("INSERT INTO model_endpoints (id, base_url, connection_revision) VALUES ('y', 'http://h/v1', 'given')")
        assert len(rev(raw, "model_endpoints", "x", "connection_revision")) == 32
        assert rev(raw, "model_endpoints", "y", "connection_revision") == "given"


def test_plain_sql_token_change_rotates_the_credential_and_every_linked_endpoint(db):
    raw, _, _ = db
    cred_before = rev(raw, "provider_auth_sessions", "auth1", "credential_revision")
    linked_before = rev(raw, "model_endpoints", "b", "connection_revision")
    other_before = rev(raw, "model_endpoints", "c", "connection_revision")
    raw.execute("UPDATE provider_auth_sessions SET access_token='t2' WHERE id='auth1'")
    raw.commit()
    assert rev(raw, "provider_auth_sessions", "auth1", "credential_revision") != cred_before
    assert rev(raw, "model_endpoints", "b", "connection_revision") != linked_before
    assert rev(raw, "model_endpoints", "c", "connection_revision") == other_before


def test_a_credential_timestamp_change_is_not_an_identity_change(db):
    raw, _, _ = db
    linked = rev(raw, "model_endpoints", "b", "connection_revision")
    cred = rev(raw, "provider_auth_sessions", "auth1", "credential_revision")
    raw.execute("UPDATE provider_auth_sessions SET label='work', last_refresh=CURRENT_TIMESTAMP WHERE id='auth1'")
    raw.commit()
    assert rev(raw, "model_endpoints", "b", "connection_revision") == linked
    assert rev(raw, "provider_auth_sessions", "auth1", "credential_revision") == cred


# ------------------------------------------------ the ORM path is unchanged --

def test_orm_change_rotates_exactly_once(db):
    raw, sessions, _ = db
    before = rev(raw, "model_endpoints", "a", "connection_revision")
    with sessions() as s:
        s.get(d.ModelEndpoint, "a").base_url = "http://localhost:7000/v1"
        s.commit()
    after = rev(raw, "model_endpoints", "a", "connection_revision")
    assert after != before
    # a second, unrelated ORM write does not rotate again
    with sessions() as s:
        s.get(d.ModelEndpoint, "a").name = "renamed"
        s.commit()
    assert rev(raw, "model_endpoints", "a", "connection_revision") == after


def test_orm_token_refresh_keeps_the_endpoint_revision_and_bumps_the_credential_version(db):
    raw, sessions, _ = db
    linked = rev(raw, "model_endpoints", "b", "connection_revision")
    cred = rev(raw, "provider_auth_sessions", "auth1", "credential_revision")
    with sessions() as s:
        auth = s.get(d.ProviderAuthSession, "auth1")
        auth.access_token = "refreshed"
        d._mark_provider_auth_oauth_refresh(auth)
        s.commit()
    assert rev(raw, "model_endpoints", "b", "connection_revision") == linked
    assert rev(raw, "provider_auth_sessions", "auth1", "credential_revision") != cred


def test_orm_reauth_rotates_linked_endpoints_once(db):
    raw, sessions, _ = db
    linked = rev(raw, "model_endpoints", "b", "connection_revision")
    with sessions() as s:
        auth = s.get(d.ProviderAuthSession, "auth1")
        auth.access_token = "new-login"
        s.commit()
    assert rev(raw, "model_endpoints", "b", "connection_revision") != linked


# ----------------------------------------------------------------- migration --

def test_install_is_idempotent_and_skips_what_does_not_exist(tmp_path):
    path = tmp_path / "empty.db"
    with sqlite3.connect(path) as raw:
        assert d.install_revision_triggers(raw) == []
        raw.execute("CREATE TABLE model_endpoints (id TEXT PRIMARY KEY, base_url TEXT, api_key TEXT, "
                    "provider_auth_id TEXT, endpoint_kind TEXT, connection_revision TEXT)")
        first = d.install_revision_triggers(raw)
        assert first == ["trg_model_endpoints_revision_insert", "trg_model_endpoints_revision_update"]
        assert d.install_revision_triggers(raw) == first


def test_the_migration_step_installs_them_and_keeps_existing_rows(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as raw:
        raw.execute("CREATE TABLE model_endpoints (id TEXT PRIMARY KEY, base_url TEXT, api_key TEXT, "
                    "provider_auth_id TEXT, endpoint_kind TEXT, connection_revision TEXT)")
        raw.execute("CREATE TABLE provider_auth_sessions (id TEXT PRIMARY KEY, provider TEXT, owner TEXT, "
                    "base_url TEXT, auth_mode TEXT, access_token TEXT, refresh_token TEXT, credential_revision TEXT)")
        raw.execute("INSERT INTO model_endpoints VALUES ('a','http://h/v1',NULL,NULL,'auto','keep')")
    monkeypatch.setattr(d, "DATABASE_URL", "sqlite:///" + str(path))
    d._migrate_add_revision_triggers()
    d._migrate_add_revision_triggers()
    with sqlite3.connect(path) as raw:
        assert raw.execute("SELECT connection_revision FROM model_endpoints").fetchall() == [("keep",)]
        assert len(raw.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()) == 3
        raw.execute("UPDATE model_endpoints SET base_url='http://other/v1'")
        assert raw.execute("SELECT connection_revision FROM model_endpoints").fetchone()[0] != "keep"


def test_the_step_is_registered_last_in_the_formal_sequence():
    names = [name for name, _ in d._formal_migration_steps()]
    assert names[-1] == "add_revision_triggers" and names.count("add_revision_triggers") == 1


def test_a_probe_recorded_under_the_old_revision_stops_applying_after_a_plain_sql_change(db, tmp_path, monkeypatch):
    from src import model_calibration as c
    raw, _, _ = db
    monkeypatch.setattr(c, "_default_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(c, "_record_deployment_evidence", lambda *a, **kw: None)
    old = rev(raw, "model_endpoints", "a", "connection_revision")
    ident = dict(vendor="openai_compatible", model_id="m", endpoint_id="a",
                 protocol="openai_chat_completions")
    c.save_scoped_tested(**ident, endpoint_revision=old, announced={},
                         tested={c.TEST_TOOL_CALLING: {"ok": True}})
    raw.execute("UPDATE model_endpoints SET base_url='http://elsewhere/v1' WHERE id='a'")
    raw.commit()
    new = rev(raw, "model_endpoints", "a", "connection_revision")
    assert c.get_effective_manifest(**ident, endpoint_revision=new)["tested"] == {}
    assert c.get_effective_manifest(**ident, endpoint_revision=old)["tested"]
