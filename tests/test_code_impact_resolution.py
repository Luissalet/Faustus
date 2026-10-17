"""Import identity and incremental edits must agree with a fresh code graph."""
import pytest

from src.context_engine import code_index as ci, store


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    store.use_path(str(tmp_path / "index.db"))
    yield root
    store.use_path(None)


def write(root, path, text):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def calls(root, name="run"):
    symbol = next(s for s in ci.symbols_in("caller.py", workspace=str(root)) if s.qualname == name)
    return {r["path"] for r in ci.neighbors(symbol.id, kinds=["calls"])
            if r["direction"] == "out" and r["resolved"]}


def test_explicit_import_never_falls_back_to_an_unrelated_unique_name(repo):
    write(repo, "other.py", "def helper(): return 1\n")
    write(repo, "caller.py", "from unavailable import helper as h\ndef run(): return h()\n")
    ci.refresh(str(repo))
    assert calls(repo) == set()


def test_two_module_calls_with_the_same_tail_are_both_preserved(repo):
    for module in ("a", "b"):
        write(repo, module + ".py", "def helper(): return 1\n")
    write(repo, "caller.py", "import a as left\nimport b as right\ndef run(): return left.helper() + right.helper()\n")
    ci.refresh(str(repo))
    assert calls(repo) == {"a.py", "b.py"}


def test_function_local_aliases_do_not_escape_their_scope(repo):
    for module in ("a", "b"):
        write(repo, module + ".py", "def helper(): return 1\n")
    write(repo, "caller.py", "def first():\n from a import helper as h\n return h()\ndef second():\n from b import helper as h\n return h()\n")
    ci.refresh(str(repo))
    assert calls(repo, "first") == {"a.py"}
    assert calls(repo, "second") == {"b.py"}


def test_unaliased_from_import_has_a_specific_binding(repo):
    for module in ("a", "b"):
        write(repo, module + ".py", "def helper(): return 1\n")
    write(repo, "caller.py", "import b\nfrom a import helper\ndef run(): return helper()\n")
    ci.refresh(str(repo))
    assert calls(repo) == {"a.py"}


def test_parameter_shadowing_an_import_does_not_produce_its_call(repo):
    write(repo, "a.py", "def helper(): return 1\n")
    write(repo, "caller.py", "from a import helper as h\ndef run(h): return h()\n")
    ci.refresh(str(repo))
    assert calls(repo) == set()


def test_adding_a_definition_resolves_an_unchanged_caller(repo):
    write(repo, "caller.py", "from a import helper as h\ndef run(): return h()\n")
    ci.refresh(str(repo))
    assert calls(repo) == set()
    write(repo, "a.py", "def helper(): return 1\n")
    result = ci.refresh(str(repo), paths=["a.py"])
    assert result["reindexed"] == 1
    assert calls(repo) == {"a.py"}


def test_added_homonym_removes_a_stale_inferred_call(repo):
    write(repo, "a.py", "def helper(): return 1\n")
    write(repo, "caller.py", "def run(): return helper()\n")
    ci.refresh(str(repo))
    assert calls(repo) == {"a.py"}
    write(repo, "b.py", "def helper(): return 2\n")
    ci.refresh(str(repo), paths=["b.py"])
    incremental = calls(repo)
    ci.refresh(str(repo), full=True)
    assert incremental == calls(repo) == set()


def test_removing_a_homonym_restores_the_remaining_inferred_call(repo):
    for module in ("a", "b"):
        write(repo, module + ".py", "def helper(): return 1\n")
    write(repo, "caller.py", "def run(): return helper()\n")
    ci.refresh(str(repo))
    assert calls(repo) == set()
    (repo / "b.py").unlink()
    ci.refresh(str(repo), paths=["b.py"])
    assert calls(repo) == {"a.py"}


def test_rebuilding_legacy_resolution_records_preserves_symbols(repo):
    write(repo, "a.py", "def helper(): return 1\n")
    write(repo, "caller.py", "from a import helper\ndef run(): return helper()\n")
    ci.refresh(str(repo))
    before = {s.id for s in ci.symbols_in("caller.py", workspace=str(repo))}
    with store.db() as conn:
        conn.execute("DELETE FROM code_reference_files")
        conn.execute("DELETE FROM code_references")
    ci.refresh(str(repo))
    assert calls(repo) == {"a.py"}
    assert before == {s.id for s in ci.symbols_in("caller.py", workspace=str(repo))}


def test_targeted_legacy_backfill_preserves_unvisited_callers(repo):
    write(repo, "a.py", "def helper(): return 1\n")
    write(repo, "b.py", "def unrelated(): return 2\n")
    write(repo, "caller.py", "from a import helper\ndef run(): return helper()\n")
    ci.refresh(str(repo))
    with store.db() as conn:
        conn.execute("DELETE FROM code_reference_files")
        conn.execute("DELETE FROM code_references")
    ci.refresh(str(repo), paths=["b.py"])
    assert calls(repo) == {"a.py"}


def test_http_impact_uses_the_import_resolved_graph(repo, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core import middleware
    from routes.code_graph_routes import setup_code_graph_routes
    write(repo, "a.py", "def helper(): return 1\n")
    write(repo, "other.py", "def helper(): return 2\n")
    write(repo, "caller.py", "from a import helper\ndef run(): return helper()\n")
    monkeypatch.setattr(middleware, "auth_disabled", lambda: True)
    app = FastAPI()
    app.include_router(setup_code_graph_routes())
    response = TestClient(app).get("/api/code-graph/impact", params={"root": str(repo), "symbol": "a.helper"})
    assert response.status_code == 200
    result = response.json()
    assert result["exit_code"] == 0, result
    assert {row["path"] for row in result["reached"]} == {"caller.py"}


def test_drop_clears_resolution_records_for_only_that_workspace(repo, tmp_path):
    write(repo, "caller.py", "def run(): return helper()\n")
    other = tmp_path / "other"
    other.mkdir()
    write(other, "caller.py", "def run(): return another()\n")
    ci.refresh(str(repo))
    ci.refresh(str(other))
    ci.drop(str(repo))
    with store.db() as conn:
        for table in ("code_references", "code_reference_files"):
            roots = {r[0] for r in conn.execute(f"SELECT DISTINCT workspace FROM {table}")}
            assert str(repo.resolve()).replace("\\", "/") not in {r.replace("\\", "/") for r in roots}
            assert roots
