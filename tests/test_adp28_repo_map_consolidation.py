"""Tests for ADP-28: `repo_map.symbols_for()` served from `code_index`.

`build()` (the per-turn map text) keeps its own fast, in-memory extraction —
see `src/repo_map.py`'s module docstring for why that hot path was left
alone — so its existing tests (`-k "repo_map"`) are untouched and still
green. What is new here is `symbols_for()`: a *client* of
`context_engine.code_index`'s incremental, hash-per-file, secret-excluding
index, not a second one. Three things are checked, matching the ficha's
acceptance criteria: changing a file invalidates only its own entry, the
symbol a caller actually wants survives a tight character budget (Aider's
"definitions + references" idea — call/import edges rank a definition, not
just its existence), and a file `code_index`'s own walk excludes never
produces a symbol here either.
"""

from __future__ import annotations

import os

import pytest

from src import repo_map
from src.context_engine import code_index as ci
from src.context_engine import store


@pytest.fixture()
def ce_store(tmp_path):
    store.use_path(str(tmp_path / "ce.db"))
    try:
        yield
    finally:
        store.use_path(None)


def write(root: str, rel: str, text: str) -> str:
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


AUTH_PY = '''"""Auth helpers."""


def normalise(code):
    return code.strip()


class Session:
    def refresh_oauth_token(self, code):
        return normalise(code)


def exchange(code):
    """Widely used: several other modules call this."""
    return Session().refresh_oauth_token(code)


def unused_helper():
    """Nobody calls this one."""
    return None
'''

CALLER_PY = '''from pkg.auth import exchange


def do_login(code):
    return exchange(code)
'''


@pytest.fixture()
def workspace(tmp_path):
    root = str(tmp_path / "ws")
    os.makedirs(root, exist_ok=True)
    write(root, "pkg/__init__.py", "")
    write(root, "pkg/auth.py", AUTH_PY)
    write(root, "pkg/caller.py", CALLER_PY)
    write(root, "static/app.js", "export function boot() { return 1; }\n")
    # Must never appear: excluded by code_index's own walk, reused (not
    # reimplemented) here — no extension repo_map/code_index recognise as
    # source, so it is never even a symbols_for candidate.
    write(root, "id_rsa", "-----BEGIN PRIVATE KEY-----\n")
    write(root, ".env", "SECRET=1\n")
    write(root, "node_modules/left-pad/index.js", "function leftPad(x) { return x; }\n")
    return root


# ── invalidation is per-file ────────────────────────────────────────────────

def test_changing_one_file_invalidates_only_its_own_entry(ce_store, workspace):
    first = repo_map.symbols_for(
        ["pkg/auth.py", "pkg/caller.py"], workspace=workspace, budget_chars=4000)
    auth_before = {s["qualname"]: s["line"] for f in first["files"] if f["path"] == "pkg/auth.py"
                   for s in f["symbols"]}
    caller_before = {s["qualname"] for f in first["files"] if f["path"] == "pkg/caller.py"
                      for s in f["symbols"]}
    assert "exchange" in auth_before
    assert "do_login" in caller_before

    # A second, unchanged refresh reindexes nothing at all.
    again = ci.refresh(workspace, paths=["pkg/auth.py", "pkg/caller.py"])
    assert again["reindexed"] == 0

    # Touch only pkg/auth.py (add a function, shift later lines).
    write(workspace, "pkg/auth.py", AUTH_PY + "\n\ndef added():\n    return 1\n")
    touched = ci.refresh(workspace, paths=["pkg/auth.py", "pkg/caller.py"])
    assert touched["reindexed"] == 1  # only the changed file was reopened/reparsed

    second = repo_map.symbols_for(
        ["pkg/auth.py", "pkg/caller.py"], workspace=workspace, budget_chars=4000)
    auth_after = {s["qualname"]: s["line"] for f in second["files"] if f["path"] == "pkg/auth.py"
                  for s in f["symbols"]}
    caller_after = {s["qualname"] for f in second["files"] if f["path"] == "pkg/caller.py"
                     for s in f["symbols"]}
    assert "added" in auth_after and "added" not in auth_before
    # The untouched file's symbols are byte-for-byte the same afterwards.
    assert caller_after == caller_before


# ── budgeted ranking: the referenced symbol survives a tight budget ────────

POPULAR_PY = '''def popular(x):
    """Called from three other places below and from another file."""
    return x


def lonely_a():
    return None


def lonely_b():
    return None


def caller_one():
    return popular(1)


def caller_two():
    return popular(2)
'''

EXTRA_CALLER_PY = '''from pkg.popular import popular


def caller_three():
    return popular(3)
'''


def test_referenced_symbol_survives_a_tight_budget(ce_store, workspace):
    # `popular` is called from three places (two in its own file, one in
    # another); `lonely_a`/`lonely_b` are called from nowhere. Both files
    # must be indexed for the cross-file call edge to resolve before the
    # budget is applied.
    write(workspace, "pkg/popular.py", POPULAR_PY)
    write(workspace, "pkg/extra_caller.py", EXTRA_CALLER_PY)
    ci.refresh(workspace, paths=["pkg/popular.py", "pkg/extra_caller.py"])

    tight = repo_map.symbols_for(["pkg/popular.py"], workspace=workspace, budget_chars=40)
    assert tight["truncated"] is True
    kept = {s["qualname"] for f in tight["files"] for s in f["symbols"]}
    assert "popular" in kept  # referenced from three call sites: ranks first
    assert "lonely_a" not in kept and "lonely_b" not in kept  # zero references: first to go
    assert tight["omitted"]  # never a silent drop: what did not fit is named
    assert any("lonely" in item for item in tight["omitted"])

    # Rank correctness, not just this one budget: nothing kept has fewer
    # references than the least-referenced kept symbol beats every omitted
    # one (the whole point of ranking before truncating).
    wide = repo_map.symbols_for(["pkg/popular.py"], workspace=workspace, budget_chars=10_000)
    by_name = {s["qualname"]: s["references"] for f in wide["files"] for s in f["symbols"]}
    assert by_name["popular"] >= 3
    assert by_name["lonely_a"] == 0 and by_name["lonely_b"] == 0


def test_unrequested_or_unknown_paths_are_explicit_not_silent(ce_store, workspace):
    out = repo_map.symbols_for(
        ["pkg/does_not_exist.py", "id_rsa"], workspace=workspace, budget_chars=4000)
    assert out["files"] == []
    assert set(out["unknown"]) == {"pkg/does_not_exist.py", "id_rsa"}


# ── secrets excluded, reused from code_index rather than reimplemented ─────

def test_secret_and_excluded_files_never_produce_symbols(ce_store, workspace):
    out = repo_map.symbols_for(
        ["id_rsa", ".env", "node_modules/left-pad/index.js"],
        workspace=workspace, budget_chars=4000)
    assert out["files"] == []
    for path in ("id_rsa", ".env", "node_modules/left-pad/index.js"):
        assert path in out["unknown"]
    # And the exclusion is code_index's, not a second list kept in repo_map:
    # nothing under node_modules/, id_rsa or .env was ever written into the
    # index at all (only pkg/auth.py and pkg/caller.py are real source here).
    status = ci.status(workspace)
    assert status["files"] == 0  # this fixture's refresh only ever touched excluded paths


# ── explicit degradation: no tree-sitter, and the field says so ────────────

def test_parser_field_is_explicit_ast_vs_regex(ce_store, workspace):
    out = repo_map.symbols_for(
        ["pkg/auth.py", "static/app.js"], workspace=workspace, budget_chars=4000)
    by_path = {f["path"]: f["parser"] for f in out["files"]}
    assert by_path["pkg/auth.py"] == "ast"
    assert by_path["static/app.js"] == "regex"
    assert repo_map.parser_for("py") == "ast"
    assert repo_map.parser_for("js") == "regex"
    assert repo_map.parser_for("none") == "none"


def test_symbols_for_outside_workspace_and_empty_paths_are_safe(ce_store, workspace):
    assert repo_map.symbols_for([], workspace=workspace) == {
        "files": [], "omitted": [], "unknown": [], "truncated": False,
    }
    out = repo_map.symbols_for(["../outside.py"], workspace=workspace)
    assert out["files"] == []
    out2 = repo_map.symbols_for(["pkg/auth.py"], workspace="/no/such/dir")
    assert out2["unknown"] == ["pkg/auth.py"]
