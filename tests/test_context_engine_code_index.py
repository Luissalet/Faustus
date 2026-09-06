"""Tests for `src/context_engine/code_index.py` (plan §10).

Four things are being defended here, and they are the four §10 keeps repeating:
the refresh is incremental and notices additions, renames and deletions; the
walk never touches generated, vendored or secret files; every edge says how it
was established and an inference is never dressed up as an exact one; and what
the index hands to the compiler is a pointer — signature, range, summary — not
the contents of the file.
"""

import os

import pytest

from src.context_engine import code_index as ci
from src.context_engine import store


@pytest.fixture()
def ce_store(tmp_path):
    store.use_path(str(tmp_path / "ce.db"))
    try:
        yield
    finally:
        store.use_path(None)


def write(root, rel, text):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


AUTH_PY = '''"""Auth helpers."""
import os
from pkg import util

TOKEN_TTL = 3600


class Session:
    """A login session."""

    def refresh_oauth_token(self, code: str) -> str:
        """Exchange the code for a token."""
        return util.normalise(code)


def exchange(code: str, *, retries: int = 2) -> str:
    """Exchange an auth code for a token."""
    return Session().refresh_oauth_token(code)
'''

API_PY = '''from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def health():
    """Liveness probe."""
    return {"ok": True}
'''


@pytest.fixture()
def workspace(tmp_path):
    """A small, believable repository: a package, a route, a test, some JS,
    and the three kinds of thing that must never be indexed."""
    root = str(tmp_path / "ws")
    os.makedirs(root)
    write(root, "pkg/__init__.py", "")
    write(root, "pkg/auth.py", AUTH_PY)
    write(root, "pkg/util.py", "def normalise(value):\n    return value.strip()\n")
    write(root, "pkg/api.py", API_PY)
    write(root, "tests/test_auth.py", "from pkg import auth\n\n\n"
                                      "def test_exchange():\n    assert auth.exchange('x')\n")
    write(root, "static/app.js", "export function boot() { return 1; }\n")
    # The three that must not appear in the index, ever.
    write(root, "node_modules/left-pad/index.js", "function leftPad(x) { return x; }\n")
    write(root, "id_rsa", "-----BEGIN PRIVATE KEY-----\n")
    write(root, ".env", "SECRET=1\n")
    return root


# ── extraction ─────────────────────────────────────────────────────────────

def test_python_symbols_carry_kind_signature_and_range(ce_store, workspace):
    ci.refresh(workspace, project_id="p1")
    found = {symbol.qualname: symbol
             for symbol in ci.symbols_in("pkg/auth.py", workspace=workspace)}

    assert found["pkg.auth"].kind == "module"
    assert found["pkg.auth"].summary == "Auth helpers."
    assert found["TOKEN_TTL"].kind == "constant"
    assert found["Session"].kind == "class"

    method = found["Session.refresh_oauth_token"]
    assert method.kind == "method"
    assert method.signature == "def refresh_oauth_token(self, code: str) -> str"
    assert method.summary == "Exchange the code for a token."
    assert method.start_line < method.end_line
    assert method.language == "py"
    assert method.source_ref() == (
        f"symbol:pkg/auth.py#L{method.start_line}-L{method.end_line}")


def test_a_route_is_its_own_kind(ce_store, workspace):
    ci.refresh(workspace, project_id="p1")
    route = {s.qualname: s for s in ci.symbols_in("pkg/api.py", workspace=workspace)}["health"]
    assert route.kind == "route"
    assert "/health" in route.signature


def test_non_python_goes_through_repo_maps_extractors(ce_store, workspace):
    ci.refresh(workspace, project_id="p1")
    names = {s.qualname: s for s in ci.symbols_in("static/app.js", workspace=workspace)}
    assert "boot" in names
    assert names["boot"].language == "js"


def test_a_python_file_that_does_not_parse_falls_back_to_the_lexical_pass(
        ce_store, tmp_path):
    root = str(tmp_path / "broken")
    os.makedirs(root)
    write(root, "half.py", "def alpha():\n    return 1\n\ndef beta(:\n")
    ci.refresh(root)
    names = {s.qualname for s in ci.symbols_in("half.py", workspace=root)}
    # The AST refused it; repo_map's regex path still found the definitions.
    assert "alpha" in names and "beta" in names


# ── the walk (§10.4: nothing generated, vendored, binary or secret) ────────

def test_a_fake_node_modules_never_enters_the_index(ce_store, workspace):
    report = ci.refresh(workspace, project_id="p1")
    indexed = {symbol["path"] for symbol in ci.search("", workspace=workspace, k=500)}
    with store.db() as conn:
        files = {row["path"] for row in conn.execute(
            "SELECT path FROM code_files WHERE workspace = ?",
            (os.path.normpath(os.path.realpath(workspace)),))}

    assert not any(path.startswith("node_modules/") for path in files)
    assert not any(path.startswith("node_modules/") for path in indexed)
    assert "id_rsa" not in files                    # a secret by name
    assert ".env" not in files                      # hidden, per index_walk
    assert files == {"pkg/__init__.py", "pkg/auth.py", "pkg/util.py",
                     "pkg/api.py", "tests/test_auth.py", "static/app.js"}
    assert report["scanned"] == 6


def test_the_paths_form_applies_the_same_policy(ce_store, workspace):
    """A caller that names a file directly does not get to bypass the prune."""
    report = ci.refresh(workspace, project_id="p1",
                        paths=["node_modules/left-pad/index.js", "id_rsa",
                               "pkg/util.py"])
    assert report["scanned"] == 1
    assert report["reindexed"] == 1
    assert ci.symbols_in("node_modules/left-pad/index.js", workspace=workspace) == []


def test_a_binary_wearing_a_source_extension_is_skipped(ce_store, tmp_path):
    root = str(tmp_path / "bin")
    os.makedirs(root)
    with open(os.path.join(root, "blob.py"), "wb") as handle:
        handle.write(b"def a():\x00\x00 pass\n")
    assert ci.refresh(root)["reindexed"] == 0


# ── incremental refresh (§10.4) ───────────────────────────────────────────

def test_refresh_is_incremental(ce_store, workspace):
    first = ci.refresh(workspace, project_id="p1")
    assert first["reindexed"] == 6

    again = ci.refresh(workspace, project_id="p1")
    assert again["scanned"] == 6
    assert again["reindexed"] == 0          # nothing changed, nothing reparsed
    assert again["removed"] == 0
    assert again["symbols"] == first["symbols"]

    forced = ci.refresh(workspace, project_id="p1", full=True)
    assert forced["reindexed"] == 6
    assert forced["symbols"] == first["symbols"]


def test_adding_renaming_and_deleting_a_symbol_updates_the_index(ce_store, tmp_path):
    root = str(tmp_path / "live")
    os.makedirs(root)
    write(root, "mod.py", "def alpha():\n    return 1\n")
    ci.refresh(root, project_id="p1")
    assert {s.qualname for s in ci.symbols_in("mod.py", workspace=root)} == {"mod", "alpha"}

    # Added.
    write(root, "mod.py", "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n")
    report = ci.refresh(root, project_id="p1")
    assert report["reindexed"] == 1
    assert {s.qualname for s in ci.symbols_in("mod.py", workspace=root)} == {
        "mod", "alpha", "beta"}

    # Renamed: the old symbol is gone, not merely shadowed.
    write(root, "mod.py", "def gamma():\n    return 1\n\n\ndef beta():\n    return 2\n")
    ci.refresh(root, project_id="p1")
    names = {s.qualname for s in ci.symbols_in("mod.py", workspace=root)}
    assert names == {"mod", "gamma", "beta"}
    assert ci.search("alpha", workspace=root) == []

    # Deleted.
    os.remove(os.path.join(root, "mod.py"))
    report = ci.refresh(root, project_id="p1")
    assert report["removed"] == 1
    assert ci.symbols_in("mod.py", workspace=root) == []
    assert ci.status(root)["symbols"] == 0


def test_a_truncated_refresh_never_concludes_a_file_is_gone(ce_store, workspace):
    full = ci.refresh(workspace, project_id="p1")
    assert full["truncated"] is False

    report = ci.refresh(workspace, project_id="p1", budget_files=2, full=True)
    assert report["truncated"] is True
    assert report["scanned"] == 2
    assert report["removed"] == 0           # the rest of the tree was never seen
    assert report["symbols"] == full["symbols"]


def test_refresh_on_a_workspace_that_is_not_there_is_a_no_op(ce_store, tmp_path):
    report = ci.refresh(str(tmp_path / "missing"))
    assert report["scanned"] == 0 and report["symbols"] == 0
    assert ci.refresh("")["reindexed"] == 0


# ── §10.3: every edge says how it was established ─────────────────────────

def _edges(symbol_id, **kwargs):
    return {(row["edge_kind"], row["certainty"], row["qualname"], row["direction"])
            for row in ci.neighbors(symbol_id, **kwargs)}


def _module(path, workspace):
    return [s for s in ci.symbols_in(path, workspace=workspace) if s.kind == "module"][0]


def test_every_stored_edge_has_a_known_certainty(ce_store, workspace):
    ci.refresh(workspace, project_id="p1")
    with store.db() as conn:
        seen = {row["certainty"] for row in conn.execute("SELECT certainty FROM code_edges")}
    assert seen and seen <= set(ci.CERTAINTY)


def test_an_import_is_exact_only_when_the_target_exists_here(ce_store, workspace):
    ci.refresh(workspace, project_id="p1")
    auth = _module("pkg/auth.py", workspace)
    edges = _edges(auth.id)

    # Resolved to a file in this workspace: exact.
    assert ("imports", "exact", "pkg", "out") in edges
    assert ("imports", "exact", "pkg.util", "out") in edges
    # Not resolvable here — kept, and honest about being an inference.
    assert ("imports", "static_inferred", "module:os", "out") in edges
    assert not any(kind == "imports" and certainty == "exact" and name == "module:os"
                   for kind, certainty, name, _ in edges)


def test_a_definition_from_the_ast_is_exact_and_one_from_a_regex_is_lexical(
        ce_store, workspace):
    ci.refresh(workspace, project_id="p1")
    assert ("defines", "exact", "Session", "out") in _edges(_module("pkg/auth.py", workspace).id)
    assert ("defines", "lexical", "boot", "out") in _edges(_module("static/app.js", workspace).id)


def test_a_call_resolved_by_name_is_never_exact(ce_store, workspace):
    ci.refresh(workspace, project_id="p1")
    exchange = [s for s in ci.symbols_in("pkg/auth.py", workspace=workspace)
                if s.qualname == "exchange"][0]
    calls = {(row["certainty"], row["qualname"]) for row in ci.neighbors(exchange.id, kinds=["calls"])}
    assert ("static_inferred", "Session.refresh_oauth_token") in calls
    assert all(certainty == "static_inferred" for certainty, _ in calls)


def test_an_ambiguous_call_is_dropped_rather_than_guessed(ce_store, tmp_path):
    root = str(tmp_path / "ambiguous")
    os.makedirs(root)
    write(root, "a.py", "def save():\n    return 'a'\n")
    write(root, "b.py", "def save():\n    return 'b'\n")
    write(root, "caller.py", "def run():\n    return save()\n")
    ci.refresh(root, project_id="p1")

    run = [s for s in ci.symbols_in("caller.py", workspace=root) if s.qualname == "run"][0]
    assert ci.neighbors(run.id, kinds=["calls"]) == []


def test_a_test_module_gets_a_tests_edge_alongside_its_import(ce_store, workspace):
    ci.refresh(workspace, project_id="p1")
    edges = _edges(_module("tests/test_auth.py", workspace).id)
    assert ("imports", "exact", "pkg.auth", "out") in edges
    assert ("tests", "exact", "pkg.auth", "out") in edges


def test_a_route_is_registered_by_an_inferred_edge(ce_store, workspace):
    """The decorator is in the source; the router object it belongs to is a
    bare name nobody resolved, so the relation is inferred and says so."""
    ci.refresh(workspace, project_id="p1")
    edges = _edges(_module("pkg/api.py", workspace).id)
    assert ("registers", "static_inferred", "health", "out") in edges


def test_neighbors_reports_unresolved_targets_instead_of_hiding_them(ce_store, workspace):
    ci.refresh(workspace, project_id="p1")
    rows = [row for row in ci.neighbors(_module("pkg/auth.py", workspace).id)
            if row["symbol_id"] == "module:os"]
    assert rows and rows[0]["resolved"] is False
    assert rows[0]["path"] == "" and rows[0]["start_line"] == 0


def test_an_import_is_promoted_to_exact_when_its_target_appears_later(ce_store, tmp_path):
    root = str(tmp_path / "later")
    os.makedirs(root)
    write(root, "consumer.py", "import provider\n\n\ndef use():\n    return 1\n")
    ci.refresh(root, project_id="p1")
    consumer = _module("consumer.py", root)
    assert ("imports", "static_inferred", "module:provider", "out") in _edges(consumer.id)

    write(root, "provider.py", "def give():\n    return 2\n")
    ci.refresh(root, project_id="p1")
    edges = _edges(consumer.id)
    assert ("imports", "exact", "provider", "out") in edges
    assert not any(name == "module:provider" for _, _, name, _ in edges)


# ── §10.5: the index guides, the file is still read ───────────────────────

def test_candidates_are_pointers_not_file_bodies(ce_store, workspace):
    ci.refresh(workspace, project_id="p1")
    hits = ci.search("refresh oauth token", workspace=workspace, project_id="p1", k=3)
    assert hits and hits[0]["qualname"] == "Session.refresh_oauth_token"

    candidate = ci.as_candidates(hits, workspace=workspace)[0]
    assert candidate.source_type == "symbol"
    assert candidate.section == "code_map"
    assert candidate.source_ref == (
        f"symbol:pkg/auth.py#L{hits[0]['start_line']}-L{hits[0]['end_line']}")
    assert candidate.body == ("def refresh_oauth_token(self, code: str) -> str\n"
                              "Exchange the code for a token.")
    assert "return util.normalise" not in candidate.body      # not the body
    assert candidate.meta["retrieval"] == "signature_only"
    assert candidate.meta["symbol_id"] == hits[0]["id"]
    # A static reading of the repository never outranks the file as it is now.
    assert candidate.authority == "inference"
    assert candidate.trust_class == "observed"
    assert candidate.source_revision == hits[0]["file_hash"]
    assert candidate.scores["total"] == pytest.approx(hits[0]["score"])


def test_search_can_be_narrowed_to_kinds(ce_store, workspace):
    ci.refresh(workspace, project_id="p1")
    hits = ci.search("session", workspace=workspace, kinds=["class"], k=5)
    assert hits and {hit["kind"] for hit in hits} == {"class"}


def test_as_candidates_survives_a_malformed_hit(ce_store):
    assert ci.as_candidates([None, 42, {"path": ""}]) == []


# ── isolation (rule 7) ─────────────────────────────────────────────────────

def test_two_workspaces_never_see_each_other(ce_store, tmp_path):
    one = str(tmp_path / "one")
    two = str(tmp_path / "two")
    os.makedirs(one)
    os.makedirs(two)
    write(one, "alpha.py", "def only_in_one():\n    return 1\n")
    write(two, "beta.py", "def only_in_two():\n    return 2\n")
    ci.refresh(one, project_id="p1")
    ci.refresh(two, project_id="p2")

    mine = [hit["qualname"] for hit in ci.search("only_in_one", workspace=one)]
    assert mine[:1] == ["only_in_one"]
    # The other workspace answers about itself and never about this one, even
    # for a query its own symbols half-match.
    theirs = ci.search("only_in_one", workspace=two)
    assert {hit["path"] for hit in theirs} == {"beta.py"}
    assert "only_in_one" not in {hit["qualname"] for hit in theirs}
    assert ci.symbols_in("alpha.py", workspace=two) == []
    assert ci.status(one)["files"] == 1 and ci.status(two)["files"] == 1


def test_project_id_narrows_within_a_workspace(ce_store, workspace):
    ci.refresh(workspace, project_id="p1")
    assert ci.status(workspace, project_id="p1")["files"] == 6
    assert ci.status(workspace, project_id="p2")["files"] == 0
    assert ci.search("exchange", workspace=workspace, project_id="p2") == []


# ── housekeeping ───────────────────────────────────────────────────────────

def test_status_reports_kinds_certainties_and_languages(ce_store, workspace):
    ci.refresh(workspace, project_id="p1")
    report = ci.status(workspace, project_id="p1")
    assert report["files"] == 6
    assert report["by_kind"]["route"] == 1
    assert report["by_kind"]["module"] == 6
    assert set(report["by_certainty"]) <= set(ci.CERTAINTY)
    assert report["languages"] == {"py": 5, "js": 1}
    assert report["last_indexed_at"]


def test_invalidate_forces_a_reindex_without_losing_the_symbols(ce_store, workspace):
    ci.refresh(workspace, project_id="p1")
    assert ci.refresh(workspace, project_id="p1")["reindexed"] == 0

    assert ci.invalidate(workspace, ["pkg/auth.py"]) == 1
    # Still answerable in the meantime — an old range beats no range at all.
    assert ci.symbols_in("pkg/auth.py", workspace=workspace)
    assert ci.refresh(workspace, project_id="p1")["reindexed"] == 1

    assert ci.invalidate(workspace) == 6
    assert ci.refresh(workspace, project_id="p1")["reindexed"] == 6


def test_drop_removes_one_workspace_and_leaves_the_other(ce_store, tmp_path):
    one = str(tmp_path / "one")
    two = str(tmp_path / "two")
    os.makedirs(one)
    os.makedirs(two)
    write(one, "alpha.py", "def a():\n    return 1\n")
    write(two, "beta.py", "def b():\n    return 2\n")
    ci.refresh(one)
    ci.refresh(two)

    assert ci.drop(one) > 0
    assert ci.status(one)["symbols"] == 0
    assert ci.status(two)["symbols"] > 0
    assert ci.drop("") == 0


def test_reads_degrade_instead_of_raising_when_the_store_is_gone(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    store.use_path(str(blocker / "ce.db"))
    try:
        assert ci.search("anything", workspace=str(tmp_path)) == []
        assert ci.symbols_in("a.py", workspace=str(tmp_path)) == []
        assert ci.neighbors("sym_1") == []
        assert ci.status(str(tmp_path))["symbols"] == 0
        assert ci.invalidate(str(tmp_path)) == 0
        assert ci.drop(str(tmp_path)) == 0
        assert ci.refresh(str(tmp_path))["reindexed"] == 0
    finally:
        store.use_path(None)
