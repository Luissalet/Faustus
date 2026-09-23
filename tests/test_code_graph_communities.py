"""Tests for `code_graph.communities` — deterministic module clustering
(code graph+).

Two fixtures:

* `clique_repo` — two dense, internally-clique-connected file groups joined
  by exactly one weak (lexical-certainty, single-direction) edge: the
  "two clear clusters + a bridge" shape the wave contract's acceptance test
  asks for, built dense enough that modularity has an unambiguous answer
  (a single shared edge between two 3-file cliques is far outweighed by
  each clique's ~6 internal edges) rather than leaving the boundary to
  chance the way a thin, uniform-weight chain of single edges would.
* `repo` — a smaller, richer fixture (routes, a dense services trio, a
  standalone utility file, a test file) used for the per-community detail
  fields (routes/key_symbols/test_files/coupling). Assertions there check
  those fields exist SOMEWHERE in the partition rather than pinning which
  exact community a borderline file (a single-route caller with only one
  edge into the rest of the graph) lands in — Louvain's own boundary choice
  there is legitimate and not the property under test.
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import time

import pytest

from src import code_graph
from src import tool_execution as te
from src.context_engine import store as ce_store

# `src.code_graph.__init__` does `from .communities import communities`, which
# rebinds the *package attribute* `code_graph.communities` to that function --
# so `import src.code_graph.communities as x` (sugar for an attribute lookup
# through the package) would silently hand back the function, not the module.
# `importlib.import_module` goes through `sys.modules` directly instead.
cg_communities = importlib.import_module("src.code_graph.communities")


def _write(root, rel, text):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def _commit(root):
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")


@pytest.fixture()
def ce_db(tmp_path):
    ce_store.use_path(str(tmp_path / "ce.db"))
    try:
        yield
    finally:
        ce_store.use_path(None)


def _bind(root):
    ws_token = te._active_workspace.set(root)
    roots_token = te._active_workspace_roots.set((root,))
    return ws_token, roots_token


def _unbind(ws_token, roots_token):
    te._active_workspace.reset(ws_token)
    te._active_workspace_roots.reset(roots_token)


# ── fixture 1: two dense cliques + one weak bridge ──────────────────────

def _clique_file(pkg, name, others, extra_call_target="", extra_pkg=""):
    calls = "\n".join(f"    {o}()" for o in others)
    body = f"def {name}():\n{calls}\n    return True\n" if others else f"def {name}():\n    return True\n"
    imports = "\n".join(f"from {pkg}.{o}_mod import {o}" for o in others)
    if extra_call_target:
        imports += f"\nfrom {extra_pkg}.{extra_call_target}_mod import {extra_call_target}"
        body += f"\n\ndef bridge_from_{name}():\n    {extra_call_target}()\n"
    return f'"""Module {name}."""\n{imports}\n\n\n{body}'


@pytest.fixture()
def clique_repo(tmp_path, ce_db):
    root = tmp_path / "clique_repo"
    root.mkdir()
    # Cluster A: a1, a2, a3 -- each calls the other two (a 6-edge clique).
    for me in ("a1", "a2", "a3"):
        others = [o for o in ("a1", "a2", "a3") if o != me]
        extra = "b1" if me == "a1" else ""
        _write(root, f"pkg_a/{me}_mod.py",
               _clique_file("pkg_a", me, others, extra, extra_pkg="pkg_b"))
    # Cluster B: b1, b2, b3 -- same clique shape, no edges back into A.
    for me in ("b1", "b2", "b3"):
        others = [o for o in ("b1", "b2", "b3") if o != me]
        _write(root, f"pkg_b/{me}_mod.py", _clique_file("pkg_b", me, others))
    _commit(str(root))
    return str(root)


def test_two_dense_clusters_and_one_bridge_yield_two_communities(clique_repo):
    ws_token, roots_token = _bind(clique_repo)
    try:
        code_graph.index(clique_repo)
        result = code_graph.communities(clique_repo, level=0)
    finally:
        _unbind(ws_token, roots_token)
    assert result["exit_code"] == 0
    assert result["method"] == "louvain"
    assert len(result["communities"]) == 2
    by_file = {f: c["id"] for c in result["communities"] for f in c["files"]}
    assert by_file["pkg_a/a1_mod.py"] == by_file["pkg_a/a2_mod.py"] == by_file["pkg_a/a3_mod.py"]
    assert by_file["pkg_b/b1_mod.py"] == by_file["pkg_b/b2_mod.py"] == by_file["pkg_b/b3_mod.py"]
    assert by_file["pkg_a/a1_mod.py"] != by_file["pkg_b/b1_mod.py"]


def test_community_ids_are_deterministic_across_calls(clique_repo):
    ws_token, roots_token = _bind(clique_repo)
    try:
        code_graph.index(clique_repo)
        first = code_graph.communities(clique_repo, level=0)
        second = code_graph.communities(clique_repo, level=0, refresh=True)
    finally:
        _unbind(ws_token, roots_token)
    assert sorted(c["id"] for c in first["communities"]) == \
        sorted(c["id"] for c in second["communities"])


def test_second_call_is_a_cache_hit(clique_repo, monkeypatch):
    ws_token, roots_token = _bind(clique_repo)
    try:
        code_graph.index(clique_repo)
        code_graph.communities(clique_repo, level=0)  # builds and persists

        calls = []
        real_build = cg_communities._build

        def _spy(*a, **kw):
            calls.append(1)
            return real_build(*a, **kw)

        monkeypatch.setattr(cg_communities, "_build", _spy)
        result = code_graph.communities(clique_repo, level=0)
    finally:
        _unbind(ws_token, roots_token)
    assert result["exit_code"] == 0
    assert calls == []  # never rebuilt


def test_changing_a_file_invalidates_the_cache(clique_repo, monkeypatch):
    ws_token, roots_token = _bind(clique_repo)
    try:
        code_graph.index(clique_repo)
        first = code_graph.communities(clique_repo, level=0)

        with open(os.path.join(clique_repo, "pkg_a", "a1_mod.py"), "a", encoding="utf-8") as handle:
            handle.write("\n\ndef another_function():\n    return 1\n")
        code_graph.index(clique_repo)

        calls = []
        real_build = cg_communities._build

        def _spy(*a, **kw):
            calls.append(1)
            return real_build(*a, **kw)

        monkeypatch.setattr(cg_communities, "_build", _spy)
        second = code_graph.communities(clique_repo, level=0)
    finally:
        _unbind(ws_token, roots_token)
    assert calls == [1]  # rebuilt because the fingerprint changed
    assert second["fingerprint"] != first["fingerprint"]


def test_level_1_is_coarser_or_equal_to_level_0(clique_repo):
    ws_token, roots_token = _bind(clique_repo)
    try:
        code_graph.index(clique_repo)
        lvl0 = code_graph.communities(clique_repo, level=0)
        lvl1 = code_graph.communities(clique_repo, level=1)
    finally:
        _unbind(ws_token, roots_token)
    assert len(lvl1["communities"]) <= len(lvl0["communities"])
    files0 = {f for c in lvl0["communities"] for f in c["files"]}
    files1 = {f for c in lvl1["communities"] for f in c["files"]}
    assert files0 == files1


def test_time_budget_exceeded_falls_back_to_directory_grouping(clique_repo, monkeypatch):
    monkeypatch.setattr(cg_communities, "_TIME_BUDGET_S", -1000.0)
    ws_token, roots_token = _bind(clique_repo)
    try:
        code_graph.index(clique_repo)
        result = code_graph.communities(clique_repo, level=0, refresh=True)
    finally:
        _unbind(ws_token, roots_token)
    assert result["exit_code"] == 0
    assert result["method"] == "directory"
    assert all(c["method"] == "directory" for c in result["communities"])


def test_node_cap_exceeded_falls_back_to_directory_grouping(clique_repo, monkeypatch):
    monkeypatch.setattr(cg_communities, "_NODE_CAP", 1)
    ws_token, roots_token = _bind(clique_repo)
    try:
        code_graph.index(clique_repo)
        result = code_graph.communities(clique_repo, level=0, refresh=True)
    finally:
        _unbind(ws_token, roots_token)
    assert result["method"] == "directory"


def test_community_lookup_by_id(clique_repo):
    ws_token, roots_token = _bind(clique_repo)
    try:
        code_graph.index(clique_repo)
        listing = code_graph.communities(clique_repo, level=0)
        target = listing["communities"][0]["id"]
        detail = code_graph.community(clique_repo, target)
    finally:
        _unbind(ws_token, roots_token)
    assert detail["exit_code"] == 0
    assert detail["community"]["id"] == target


def test_community_lookup_by_symbol(clique_repo):
    ws_token, roots_token = _bind(clique_repo)
    try:
        code_graph.index(clique_repo)
        detail = code_graph.community(clique_repo, "a2")
    finally:
        _unbind(ws_token, roots_token)
    assert detail["exit_code"] == 0
    assert "pkg_a/a2_mod.py" in detail["community"]["files"]


def test_community_of_resolves_a_symbols_file(clique_repo):
    ws_token, roots_token = _bind(clique_repo)
    try:
        code_graph.index(clique_repo)
        result = code_graph.community_of(clique_repo, "a2")
    finally:
        _unbind(ws_token, roots_token)
    assert result["exit_code"] == 0
    assert result["path"] == "pkg_a/a2_mod.py"
    assert result["community_id"]


def test_community_of_unknown_symbol(clique_repo):
    ws_token, roots_token = _bind(clique_repo)
    try:
        code_graph.index(clique_repo)
        result = code_graph.community_of(clique_repo, "totally_unknown_symbol_xyz")
    finally:
        _unbind(ws_token, roots_token)
    assert result["exit_code"] == 1


def test_root_outside_workspace_is_rejected(clique_repo, tmp_path):
    ws_token, roots_token = _bind(clique_repo)
    try:
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        result = code_graph.communities(str(outside), level=0)
    finally:
        _unbind(ws_token, roots_token)
    assert result["exit_code"] == 1
    assert "error" in result


# ── fixture 2: routes / key symbols / coupling / test coverage ──────────

ROUTES_PY = '''"""HTTP routes."""
from fastapi import APIRouter

from services.orders import place_order

router = APIRouter()


@router.post("/orders")
def create_order(payload):
    """Create an order."""
    return place_order(payload)
'''

ORDERS_PY = '''"""Order business logic -- a dense trio with db.py and invoices.py."""
from services.db import commit_order
from services.invoices import issue_invoice


def place_order(payload):
    issue_invoice(payload)
    return commit_order(payload)
'''

DB_PY = '''"""DB writes."""
from services.orders import place_order
from services.invoices import issue_invoice


def commit_order(payload):
    return {"ok": True}


def retry_via_orders(payload):
    return place_order(payload)


def retry_via_invoices(payload):
    return issue_invoice(payload)
'''

INVOICES_PY = '''"""Invoicing."""
from services.orders import place_order
from services.db import commit_order


def issue_invoice(order):
    place_order(order)
    return commit_order(order)
'''

SHARED_PY = '''"""A standalone helper nothing links to."""


def slugify(text):
    return text.lower()
'''

TEST_ORDERS_PY = '''"""Exercises the orders trio -- must never pull it into a test cluster."""
from services.orders import place_order


def test_place_order():
    assert place_order({}) is not None
'''

ORPHAN_TEST_PY = '''"""A test file with no resolved edge into any production file at all --
must land in the shared "Unattached tests" bucket, never its own community."""


def test_nothing_in_particular():
    assert 1 + 1 == 2
'''


@pytest.fixture()
def repo(tmp_path, ce_db):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "web/routes.py", ROUTES_PY)
    _write(root, "services/orders.py", ORDERS_PY)
    _write(root, "services/db.py", DB_PY)
    _write(root, "services/invoices.py", INVOICES_PY)
    _write(root, "shared/util.py", SHARED_PY)
    _write(root, "tests/test_orders.py", TEST_ORDERS_PY)
    _write(root, "tests/test_orphan.py", ORPHAN_TEST_PY)
    _commit(str(root))
    return str(root)


@pytest.fixture()
def ws(repo):
    ws_token, roots_token = _bind(repo)
    try:
        yield repo
    finally:
        _unbind(ws_token, roots_token)


def test_orders_trio_is_not_split_from_itself(ws):
    code_graph.index(ws)
    result = code_graph.communities(ws, level=0)
    by_file = {f: c["id"] for c in result["communities"] for f in c["files"]}
    assert by_file["services/orders.py"] == by_file["services/db.py"]
    assert by_file["services/orders.py"] == by_file["services/invoices.py"]


def test_test_file_never_merges_into_the_production_cluster(ws):
    """Test files are never clustering nodes at all -- they never show up in
    any community's `files` list, only (post attachment) in `test_files`."""
    code_graph.index(ws)
    result = code_graph.communities(ws, level=0)
    all_files = {f for c in result["communities"] for f in c["files"]}
    assert "tests/test_orders.py" not in all_files
    orders_comm = next(c for c in result["communities"] if "services/orders.py" in c["files"])
    assert "tests/test_orders.py" in orders_comm["test_files"]


def test_community_reports_test_coverage_via_unfiltered_edges(ws):
    code_graph.index(ws)
    result = code_graph.communities(ws, level=0)
    orders_comm = next(c for c in result["communities"] if "services/orders.py" in c["files"])
    assert "tests/test_orders.py" in orders_comm["test_files"]


def test_communities_report_routes_and_key_symbols_somewhere(ws):
    code_graph.index(ws)
    result = code_graph.communities(ws, level=0)
    all_routes = [r for c in result["communities"] for r in c["routes"]]
    all_keys = [s["symbol"] for c in result["communities"] for s in c["key_symbols"]]
    assert any("create_order" in r["route"] for r in all_routes)
    assert any("place_order" in n for n in all_keys)


def test_purpose_and_name_are_nonempty(ws):
    code_graph.index(ws)
    result = code_graph.communities(ws, level=0)
    for c in result["communities"]:
        assert c["name"]
        assert c["purpose"]


def test_coupling_is_symmetric_between_the_two_reported_communities(ws):
    code_graph.index(ws)
    result = code_graph.communities(ws, level=0)
    orders_comm = next(c for c in result["communities"] if "services/orders.py" in c["files"])
    if orders_comm["coupling"]:
        other_id = orders_comm["coupling"][0]["community_id"]
        other = next(c for c in result["communities"] if c["id"] == other_id)
        assert any(x["community_id"] == orders_comm["id"] for x in other["coupling"])


def test_names_are_path_based_not_symbol_based(ws):
    """Quality round: no more "dir — symbol" names -- see communities.py's
    `_name_for`. `key_symbols` remains a separate field."""
    code_graph.index(ws)
    result = code_graph.communities(ws, level=0, min_files=1)
    for c in result["communities"]:
        assert "—" not in c["name"]
        assert "key_symbols" in c


def test_min_files_param_controls_the_display_floor(ws):
    code_graph.index(ws)
    everything = code_graph.communities(ws, level=0, min_files=0)
    default = code_graph.communities(ws, level=0)
    assert default["min_files"] == cg_communities._DEFAULT_MIN_FILES
    assert len(everything["communities"]) >= len(default["communities"])
    assert all(len(c["files"]) >= default["min_files"] for c in default["communities"])


def test_orphan_test_file_lands_in_the_unattached_bucket_not_its_own_community(ws):
    code_graph.index(ws)
    default = code_graph.communities(ws, level=0)
    assert default["unattached_tests_count"] >= 1
    assert all(c.get("bucket") != "unattached_tests" for c in default["communities"])
    assert all("tests/test_orphan.py" not in c["files"] for c in default["communities"])

    with_bucket = code_graph.communities(ws, level=0, include_unattached=True, min_files=0)
    bucket = next(c for c in with_bucket["communities"] if c.get("bucket") == "unattached_tests")
    assert "tests/test_orphan.py" in bucket["files"]


# ── quality round: fold-tiny / catch-all-split / test-attach unit tests ──
#
# These exercise the pure post-processing functions directly (small, exact,
# deterministic inputs) rather than only through a full Louvain build --
# the acceptance scenarios (a repo-scale run) are covered separately by the
# real-repo numbers in REPORT_CG.md.

def test_fold_tiny_groups_prefers_the_strongest_real_edge():
    groups = {"big": ["a.py", "b.py", "c.py"], "tiny": ["d.py"]}
    pair_weight = {("c.py", "d.py"): 5.0}
    folded = cg_communities._fold_tiny_groups(groups, pair_weight, min_files=3)
    assert "tiny" not in folded
    assert sorted(folded["big"]) == ["a.py", "b.py", "c.py", "d.py"]


def test_fold_tiny_groups_falls_back_to_directory_majority_with_no_edges():
    groups = {"big": ["dir/a.py", "dir/b.py", "dir/c.py"], "tiny": ["dir/d.py"]}
    folded = cg_communities._fold_tiny_groups(groups, {}, min_files=3)
    assert sorted(folded["big"]) == ["dir/a.py", "dir/b.py", "dir/c.py", "dir/d.py"]


def test_fold_tiny_groups_uses_a_loose_bucket_with_no_edge_and_no_dir_neighbour():
    groups = {"big": ["dir_a/a.py", "dir_a/b.py", "dir_a/c.py"], "tiny": ["dir_b/z.py"]}
    folded = cg_communities._fold_tiny_groups(groups, {}, min_files=3)
    assert "dir_b/z.py" not in folded["big"]
    loose = [files for gid, files in folded.items() if gid.startswith("__loose__")]
    assert loose == [["dir_b/z.py"]]


def test_fold_tiny_groups_is_a_noop_when_nothing_reaches_min_files():
    groups = {"a": ["x.py"], "b": ["y.py"]}
    assert cg_communities._fold_tiny_groups(groups, {}, min_files=3) == groups


def test_split_catchalls_splits_a_big_zero_cohesion_blob_by_directory():
    files = [f"area_a/f{i}.py" for i in range(40)] + [f"area_b/g{i}.py" for i in range(40)]
    final, split_of = cg_communities._split_catchalls(
        {"blob": files}, {}, min_files=3, catchall_min_files=3, total_files=80,
        deadline=time.monotonic() + 5.0)
    assert len(final) == 2
    assert set(split_of.values()) == {"directory"}
    assert sum(len(v) for v in final.values()) == 80


def test_split_catchalls_leaves_a_cohesive_non_dominant_group_alone():
    # High cohesion AND a small slice of a (hypothetically) much bigger repo:
    # neither trigger fires, so the group is left as-is.
    blob = [f"area/f{i}.py" for i in range(70)]
    other = ["other/g0.py", "other/g1.py", "other/g2.py"]
    pair_weight = {(blob[i], blob[i + 1]): 10.0 for i in range(69)}
    pair_weight[(blob[0], other[0])] = 0.5  # one weak cross edge
    final, split_of = cg_communities._split_catchalls(
        {"blob": blob, "other": other}, pair_weight, min_files=3,
        catchall_min_files=3, total_files=1000, deadline=time.monotonic() + 5.0)
    assert final["blob"] == blob
    assert split_of == {}


def test_split_catchalls_splits_a_dominant_cohesive_blob_by_call_graph():
    # High cohesion but the group IS most of the repo -- this is exactly the
    # "one dense blob swallows the whole app" failure mode the dominance
    # trigger exists for. Two internally-dense clusters, weakly cross-linked,
    # should be told apart by the internal-subgraph Louvain pass.
    a = [f"area_a/f{i}.py" for i in range(20)]
    b = [f"area_b/g{i}.py" for i in range(20)]
    pair_weight = {(a[i], a[i + 1]): 10.0 for i in range(19)}
    pair_weight.update({(b[i], b[i + 1]): 10.0 for i in range(19)})
    pair_weight[(a[0], b[0])] = 0.1  # one very weak cross-cluster edge
    blob = a + b
    final, split_of = cg_communities._split_catchalls(
        {"blob": blob}, pair_weight, min_files=3, catchall_min_files=3,
        total_files=41, deadline=time.monotonic() + 5.0)
    assert len(final) >= 2
    assert set(split_of.values()) <= {"call_graph", "directory"}
    assert sum(len(v) for v in final.values()) == 40


def test_split_catchalls_leaves_a_small_group_alone_regardless_of_cohesion():
    files = [f"area/f{i}.py" for i in range(10)]  # under catchall_min_files
    final, split_of = cg_communities._split_catchalls(
        {"blob": files}, {}, min_files=3, catchall_min_files=15, total_files=10,
        deadline=time.monotonic() + 5.0)
    assert final == {"blob": files}
    assert split_of == {}


def test_assign_tests_attaches_to_the_strongest_production_community():
    test_weighted = {"tests/test_a.py": {"prod/a.py": 3.0, "prod/b.py": 1.0}}
    file_to_id0 = {"prod/a.py": "comm1", "prod/b.py": "comm2"}
    by_id0, unattached = cg_communities._assign_tests(
        test_weighted, file_to_id0, ["tests/test_a.py"])
    assert by_id0 == {"comm1": {"tests/test_a.py"}}
    assert unattached == []


def test_assign_tests_is_unattached_with_no_production_edge():
    by_id0, unattached = cg_communities._assign_tests({}, {}, ["tests/test_orphan.py"])
    assert unattached == ["tests/test_orphan.py"]
    assert by_id0 == {}


def test_name_for_is_a_directory_path_not_a_symbol():
    files = ["src/brain/a.py", "src/brain/b.py"]
    ctx = cg_communities._NameCtx({f: 10 for f in files}, files)
    assert cg_communities._name_for(files, ctx) == "src/brain"


def test_name_for_falls_back_to_a_file_stem_for_a_small_slice_of_a_flat_dir():
    # "src" holds 10 files repo-wide; this community owns exactly one of them.
    all_files = [f"src/x{i}.py" for i in range(10)] + ["other/y.py"]
    ctx = cg_communities._NameCtx({f: 5 for f in all_files}, all_files)
    assert cg_communities._name_for(["src/x0.py"], ctx) == "src/x0"


def test_disambiguate_names_tags_a_name_collision():
    files = ["src/foo_bar.py", "src/foo_baz.py", "other/foo_qux.py"]
    ctx = cg_communities._NameCtx({f: 5 for f in files}, files)
    records = [
        {"name": "src", "files": ["src/foo_bar.py"]},
        {"name": "src", "files": ["src/foo_baz.py"]},
    ]
    cg_communities._disambiguate_names(records, ctx)
    assert records[0]["name"] != records[1]["name"]
    assert records[0]["name"].startswith("src (") and records[1]["name"].startswith("src (")


# ── tool/schema registration ────────────────────────────────────────────

def test_tool_is_registered_everywhere():
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    from src.tool_capabilities import TOOL_CAPABILITIES
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_index_examples import EXAMPLES

    name = "code_graph_communities"
    schema_names = {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
    assert name in TOOL_HANDLERS
    assert name in TOOL_TAGS
    assert name in schema_names
    assert name in TOOL_CAPABILITIES
    assert name in BUILTIN_TOOL_DESCRIPTIONS
    assert len(EXAMPLES.get(name, [])) >= 2


def test_communities_tool_executor(ws):
    import asyncio
    from src.agent_tools.code_graph_tools import CodeGraphCommunitiesTool

    code_graph.index(ws)
    result = asyncio.run(CodeGraphCommunitiesTool().execute(
        json.dumps({"root": str(ws), "level": 0}), {}))
    assert result["exit_code"] == 0
    assert len(result["communities"]) >= 1


def test_communities_tool_executor_detail_by_id(ws):
    import asyncio
    from src.agent_tools.code_graph_tools import CodeGraphCommunitiesTool

    code_graph.index(ws)
    listing = code_graph.communities(ws, level=0)
    target = listing["communities"][0]["id"]
    result = asyncio.run(CodeGraphCommunitiesTool().execute(
        json.dumps({"root": str(ws), "id": target}), {}))
    assert result["exit_code"] == 0
    assert result["community"]["id"] == target


# ── small-repo regression: scaled thresholds + entry-kind ranking ───────
#
# Reproduces, in miniature, the failure mode a real ~60-file FastAPI+React
# app hit: fixed (big-repo-tuned) fold/catch-all thresholds either fold
# every small module away or never fire at all on a repo this size, and a
# vendored library's own internal entry point can outrank the app's real
# HTTP routes by raw call-graph criticality alone. Five disconnected-by-
# design Python "packages" (~37 files: a 10-file clique, an 8-file clique,
# another 8-file clique, a 5-file vendored clique + one un-called internal
# root inside it, 4 directory-loose utility files, and one HTTP route
# calling into the first clique) -- deliberately built so NO package alone
# would trigger the old fixed catch-all threshold (60 files) or need it to
# stay separate from the others, the exact shape a "handful of real
# modules" small app has.

def _clique_package(root, pkg, n):
    """`n` files, each defining and exporting one PACKAGE-PREFIXED function
    (`{pkg}_m1`, `{pkg}_m2`, ...) that calls every other sibling -- prefixed
    so names are globally unique across every package this fixture builds.
    `code_index`'s call resolver only disambiguates a same-named function
    across multiple files via an aliased import's module hint (see
    `_resolve`'s docstring in code_index.py); a plain, non-aliased
    `from pkg.x_mod import x` -- what `_clique_file` generates and what real
    code overwhelmingly looks like -- carries no such hint, so a bare name
    that collides across packages (four different `m1`s) has its call edges
    silently dropped as unresolvably ambiguous. Unique names sidestep that
    entirely and let every intra-package call resolve as a real `calls`
    edge, which this fixture's assertions on `flows()` entry points and
    fan-out depend on."""
    names = [f"{pkg}_m{i}" for i in range(1, n + 1)]
    for me in names:
        others = [o for o in names if o != me]
        _write(root, f"{pkg}/{me}_mod.py", _clique_file(pkg, me, others))
    return names


VENDOR_ROOT_PY = '''"""An internal orchestrator nothing else in the vendored package (or the
app) calls -- a plain "public root" candidate by call-graph shape alone,
which must be demoted to `vendor_root` because its own directory carries
the vendoring marker below."""
from vendor_hoard_link.vendor_hoard_link_m1_mod import vendor_hoard_link_m1
from vendor_hoard_link.vendor_hoard_link_m2_mod import vendor_hoard_link_m2
from vendor_hoard_link.vendor_hoard_link_m3_mod import vendor_hoard_link_m3
from vendor_hoard_link.vendor_hoard_link_m4_mod import vendor_hoard_link_m4


def internal_orchestrator():
    vendor_hoard_link_m1()
    vendor_hoard_link_m2()
    vendor_hoard_link_m3()
    vendor_hoard_link_m4()
'''

ROUTE_PY = '''"""The app's one real HTTP entry point -- must outrank every internal/
vendored root in `flows()` regardless of its own (small) call tree."""
from fastapi import APIRouter

from core.core_m1_mod import core_m1

router = APIRouter()


@router.get("/status")
def get_status():
    """The one entry point this fixture cares about."""
    return core_m1()
'''

UTIL_PY_TMPL = '''"""A standalone utility file, no calls in or out -- must still end up in
SOME community (the size-scaled fold's directory-loose bucket), never
folded away entirely and never a fatal error."""


def {name}():
    return {idx}
'''


@pytest.fixture()
def small_app_repo(tmp_path, ce_db):
    root = tmp_path / "small_app"
    root.mkdir()
    _clique_package(root, "core", 10)
    _clique_package(root, "workers", 8)
    _clique_package(root, "models", 8)
    _clique_package(root, "vendor_hoard_link", 5)
    _write(root, "vendor_hoard_link/VENDORED.txt",
           "Vendored third-party code -- do not edit directly.\n")
    _write(root, "vendor_hoard_link/root_mod.py", VENDOR_ROOT_PY)
    _write(root, "api/routes.py", ROUTE_PY)
    for i in range(1, 5):
        _write(root, f"utils/u{i}.py", UTIL_PY_TMPL.format(name=f"u{i}", idx=i))
    _commit(str(root))
    return str(root)


def test_small_app_yields_several_level0_communities_not_one_blob(small_app_repo):
    ws_token, roots_token = _bind(small_app_repo)
    try:
        code_graph.index(small_app_repo)
        result = code_graph.communities(small_app_repo, level=0)
    finally:
        _unbind(ws_token, roots_token)
    assert result["exit_code"] == 0
    # Five independent packages must not collapse into one catch-all, and
    # must not each be folded away into nothing either.
    assert len(result["communities"]) > 1
    total_shown = sum(c["size"] for c in result["communities"])
    assert total_shown > 0


def test_small_app_route_outranks_internal_and_vendored_roots(small_app_repo):
    cg_flows = importlib.import_module("src.code_graph.flows")
    ws_token, roots_token = _bind(small_app_repo)
    try:
        code_graph.index(small_app_repo)
        result = code_graph.flows(small_app_repo, limit=50)
    finally:
        _unbind(ws_token, roots_token)
    assert result["exit_code"] == 0
    flows_list = result["flows"]
    reasons = {f["entry_reason"] for f in flows_list}
    assert "route" in reasons
    assert "vendor_root" in reasons  # the vendoring marker must be detected

    route_pos = next(i for i, f in enumerate(flows_list) if f["entry_reason"] == "route")
    vendor_positions = [i for i, f in enumerate(flows_list) if f["entry_reason"] == "vendor_root"]
    assert route_pos < min(vendor_positions)

    route_flow = flows_list[route_pos]
    assert route_flow["name"] == "GET /status"

    if any(f["entry_reason"] == "root" for f in flows_list):
        root_positions = [i for i, f in enumerate(flows_list) if f["entry_reason"] == "root"]
        # non-vendored roots must also outrank vendored ones (the penalty).
        assert max(root_positions) < min(vendor_positions)
