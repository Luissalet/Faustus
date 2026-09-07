"""The derived index must release connections on successful and failed calls."""
import sqlite3

import pytest

from src.project_context import index
from src.project_context.models import ExtractedChunk, ExtractedCorpus


SCOPE = dict(owner="alice", project_id="project", link_id="document")


@pytest.fixture
def tracked_connections(tmp_path, monkeypatch):
    original = sqlite3.connect
    connections = []

    class Connection(sqlite3.Connection):
        closed = False
        fail_setup = False
        fail_write = False

        def close(self):
            self.closed = True
            return super().close()

        def execute(self, sql, *args, **kwargs):
            if self.fail_setup and sql.startswith("CREATE TABLE"):
                raise sqlite3.OperationalError("injected schema failure")
            return super().execute(sql, *args, **kwargs)

        def executemany(self, sql, *args, **kwargs):
            if self.fail_write:
                raise sqlite3.OperationalError("injected chunk failure")
            return super().executemany(sql, *args, **kwargs)

    def connect(*args, **kwargs):
        conn = original(*args, **kwargs, factory=Connection)
        connections.append(conn)
        return conn

    monkeypatch.setattr(index.sqlite3, "connect", connect)
    index.use_path(str(tmp_path / "context.db"))
    try:
        yield connections, Connection
    finally:
        index.use_path(None)
        for conn in connections:
            if not conn.closed:
                conn.close()


def corpus(revision="r1", text="aurora"):
    return ExtractedCorpus(revision=revision, chunks=(ExtractedChunk(index=0, text=text),))


@pytest.mark.parametrize("operation", ["replace", "revision", "search", "delete"])
def test_each_public_operation_closes_its_connection(tracked_connections, operation):
    connections, _ = tracked_connections
    index.replace(**SCOPE, corpus=corpus())
    if operation == "replace":
        assert index.replace(**SCOPE, corpus=corpus("r2")) == 1
    elif operation == "revision":
        assert index.indexed_revision(**SCOPE) == "r1"
    elif operation == "search":
        assert index.search(**SCOPE, query="aurora")[0].revision == "r1"
    else:
        assert index.delete(**SCOPE) == 2
    assert len(connections) == 2
    assert all(conn.closed for conn in connections)


def test_schema_failure_closes_the_partially_initialized_connection(tracked_connections):
    connections, cls = tracked_connections
    cls.fail_setup = True
    with pytest.raises(sqlite3.OperationalError, match="schema failure"):
        index.indexed_revision(**SCOPE)
    assert len(connections) == 1 and connections[0].closed


def test_failed_replacement_rolls_back_and_closes(tracked_connections):
    connections, cls = tracked_connections
    index.replace(**SCOPE, corpus=corpus())
    cls.fail_write = True
    with pytest.raises(sqlite3.OperationalError, match="chunk failure"):
        index.replace(**SCOPE, corpus=corpus("r2", "replacement"))
    cls.fail_write = False
    assert index.indexed_revision(**SCOPE) == "r1"
    assert index.search(**SCOPE, query="aurora")[0].revision == "r1"
    assert not index.search(**SCOPE, query="replacement")
    assert all(conn.closed for conn in connections)
