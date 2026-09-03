"""The provenance graph (src/provenance_graph.py) and its API
(routes/provenance_routes.py).

The claim under test is the one the feature is sold on: **every edge traces to
a record something already stored**, and no edge exists that a model asserted.
So the suite pins the EXACT node and edge sets a fixture of declared records
produces — a fixture where every edge is traceable by hand to the line of
JSONL, the evidence span, the checkpoint diff or the literally verified text
overlap it came from — and then asserts that nothing else appears.

The rest follows the report's value order: orphans, multi-hop neighbours and
``impact`` ("what breaks if I touch this"), ``explain`` (the ordered evidence
chain that answers "why does the agent believe this"), and the ranking signal
capped at 0.10. Plus the two non-negotiables: a missing or broken source yields
a SMALLER graph rather than an error, and two builds of the same records are
byte-identical.
"""

import json
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import memory_engine as engine  # noqa: E402
from src import provenance_graph as pg  # noqa: E402

NOW = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)

RULE = "Always run the project tests before claiming the work is done"
RULE_DUP = "Always run the project tests before claiming the work is done!"
ORIGINAL = "Skip the tests when the change looks small"
ANTI = f"AVOID: {ORIGINAL}"
LONE = "Descale the espresso machine every March, the water here is very hard"


# ── the fixture: nothing but declared records ───────────────────────────


def _write_objectives(workspace):
    """Two live objectives with one declared dependency edge, plus a dropped
    one that must NOT reach the graph (services/objectives.py leaves dropped
    objectives out of the prompt block and the impact scores too)."""
    path = os.path.join(workspace, ".odysseus", "objectives.jsonl")
    rows = [
        {"t": "obj", "id": "OBJ-1", "title": "Ship the provenance graph",
         "status": "in_progress", "priority": 1, "owner": "user",
         "created_at": "2026-09-01T09:00:00Z", "updated_at": "2026-09-01T10:00:00Z",
         "last_actor": "user", "notes": ""},
        {"t": "obj", "id": "OBJ-2", "title": "Write the overlap detector",
         "status": "done", "priority": 2, "owner": "user",
         "created_at": "2026-09-01T08:00:00Z", "updated_at": "2026-09-01T09:00:00Z",
         "last_actor": "user", "notes": ""},
        {"t": "obj", "id": "OBJ-9", "title": "Abandoned idea", "status": "dropped",
         "priority": 3, "owner": "user", "created_at": "2026-08-01T08:00:00Z",
         "updated_at": "2026-08-01T08:00:00Z", "last_actor": "user", "notes": ""},
        {"t": "dep", "from": "OBJ-1", "to": "OBJ-2"},
    ]
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _write_objective_log(workspace):
    path = os.path.join(workspace, ".odysseus", "objectives_log.jsonl")
    rows = [
        {"ts": "2026-09-02T09:00:00Z", "kind": "delta", "actor": "agent", "op": "EDIT",
         "id": "OBJ-2", "fields": {"status": "done"}, "rationale": "tests green",
         "session": "sess-abc"},
        {"ts": "2026-09-02T10:00:00Z", "kind": "evidence", "id": "OBJ-1",
         "source": "dispatch", "ref": "job0001", "confidence": 0.6,
         "note": "2 file(s) changed: src/provenance_graph.py, app.py"},
        # An evidence record for an objective that does not exist draws nothing.
        {"ts": "2026-09-02T11:00:00Z", "kind": "evidence", "id": "OBJ-404",
         "source": "dispatch", "ref": "job9999", "confidence": 0.4, "note": ""},
    ]
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _write_mirror(data_dir, workspace):
    """A dispatch job mirror: what Faustus SAW change between its checkpoints."""
    path = os.path.join(data_dir, "dispatch", "job0001.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({
            "id": "job0001", "title": "Build the provenance graph", "status": "done",
            "workspace": workspace, "session_id": "sess-abc", "checkpoint": "deadbeefcafe",
            "verdict": "done · 2 files changed",
            "changes": {"source": "checkpoint", "count": 2,
                        "added": ["src/provenance_graph.py"], "modified": ["app.py"],
                        "deleted": [], "truncated": False},
            "result": {"files_changed": ["src/provenance_graph.py", "app.py"]},
        }, fh)


def _add_memory(workspace):
    """Five items, each one a record a human or the Curator wrote."""
    ids = {}
    ids["rule"] = engine.add_item(
        RULE, owner="luis", project=workspace, level="procedural",
        trust_class="human_explicit", now=NOW,
        evidence=[{"kind": "chat", "session_id": "sess-abc", "excerpt": "the user said so"},
                  {"kind": "file", "ref": "src/provenance_graph.py:42",
                   "excerpt": "the builder refuses a node past the budget"}],
    )["id"]
    ids["dup"] = engine.add_item(
        RULE_DUP, owner="luis", project=workspace, level="semantic",
        trust_class="agent_assertion", now=NOW,
    )["id"]
    ids["original"] = engine.add_item(
        ORIGINAL, owner="luis", project=workspace, level="procedural",
        trust_class="agent_assertion", now=NOW,
    )["id"]
    anti = engine.add_item(
        ANTI, owner="luis", project=workspace, level="procedural",
        trust_class="agent_validated", status="anti_pattern", now=NOW,
    )
    anti["inverted_from"] = ORIGINAL
    engine.save_item(anti)
    ids["anti"] = anti["id"]
    ids["lone"] = engine.add_item(
        LONE, owner="luis", project=workspace, level="episodic",
        trust_class="human_explicit", now=NOW,
    )["id"]
    return ids


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A disposable workspace + data dir holding every source the graph reads."""
    workspace = tmp_path / "ws"
    (workspace / ".odysseus").mkdir(parents=True)
    data = tmp_path / "data"
    (data / "dispatch").mkdir(parents=True)

    monkeypatch.setattr(engine, "DATA_DIR", str(data))
    monkeypatch.setattr(pg, "DATA_DIR", str(data))
    engine.set_vector_store(None)

    _write_objectives(str(workspace))
    _write_objective_log(str(workspace))
    _write_mirror(str(data), str(workspace))
    ids = _add_memory(str(workspace))

    yield SimpleNamespace(workspace=str(workspace), data=str(data), ids=ids,
                          project={"id": "p1", "name": "Faustus",
                                   "workspace": str(workspace)})
    engine.reset_vector_store()


def _build(store, **kw):
    kw.setdefault("project", store.project)
    kw.setdefault("workspace", store.workspace)
    kw.setdefault("now", NOW)
    return pg.build("luis", **kw)


def _n(store, key):
    return f"memory:{store.ids[key]}"


def _edge_set(graph):
    return {(e["kind"], e["from"], e["to"]) for e in graph["edges"]}


def _pairs(graph, kind):
    """Undirected pairs of one edge kind. ``duplicate_of`` is symmetric and the
    direction it is stored in is the id order — which for memory items is a
    uuid, so the PAIR is what a fixture can pin, not the arrow."""
    return {frozenset((e["from"], e["to"])) for e in graph["edges"] if e["kind"] == kind}


# ── the exact node and edge sets ────────────────────────────────────────


def test_the_node_set_is_exactly_what_the_records_name(store):
    graph = _build(store)
    assert {n["id"] for n in graph["nodes"]} == {
        # objectives.jsonl — the dropped OBJ-9 is deliberately absent
        "objective:OBJ-1", "objective:OBJ-2",
        # the five memory items
        _n(store, "rule"), _n(store, "dup"), _n(store, "original"),
        _n(store, "anti"), _n(store, "lone"),
        # named by a memory evidence span, a delta record and the job mirror
        "chat:sess-abc",
        # named by a memory evidence span and by the checkpoint diff
        "file:src/provenance_graph.py", "file:app.py",
        # named by the objectives log's evidence record and by its mirror
        "checkpoint:job0001",
    }
    assert graph["truncated"] is False


def test_the_edge_set_is_exactly_what_the_records_declare(store):
    graph = _build(store)
    directed = {e for e in _edge_set(graph) if e[0] != "duplicate_of"}
    assert directed == {
        # objectives.jsonl: one dependency edge record
        ("depends_on", "objective:OBJ-1", "objective:OBJ-2"),
        # objectives_log.jsonl: one evidence record, one delta record
        ("evidence_of", "objective:OBJ-1", "checkpoint:job0001"),
        ("evidence_of", "objective:OBJ-2", "chat:sess-abc"),
        # the job mirror: its session, and its two observed file changes
        ("evidence_of", "checkpoint:job0001", "chat:sess-abc"),
        ("changed", "checkpoint:job0001", "file:app.py"),
        ("changed", "checkpoint:job0001", "file:src/provenance_graph.py"),
        # the rule's two stored evidence spans
        ("evidence_of", _n(store, "rule"), "chat:sess-abc"),
        ("evidence_of", _n(store, "rule"), "file:src/provenance_graph.py"),
        # the Curator's stored inverted_from
        ("contradicts", _n(store, "anti"), _n(store, "original")),
    }
    # ...plus exactly two literally verified near-duplicate pairs.
    assert _pairs(graph, "duplicate_of") == {
        frozenset((_n(store, "dup"), _n(store, "rule"))),
        frozenset((_n(store, "anti"), _n(store, "original"))),
    }


def test_no_edge_exists_without_a_declared_source(store):
    graph = _build(store)
    assert graph["edges"], "the fixture must produce edges to make this meaningful"
    known = {n["id"] for n in graph["nodes"]}
    for edge in graph["edges"]:
        assert edge["kind"] in pg.EDGE_KINDS
        # Declared, and filterable: nothing in this module ever emits inferred.
        assert edge["trust"] == pg.TRUST_DECLARED
        # A sentence naming the record it came from — the whole point of the view.
        assert edge["why"].strip(), edge
        assert 0.0 <= edge["confidence"] <= 1.0
        assert edge["from"] in known and edge["to"] in known
    assert pg.filter_trust(graph, pg.TRUST_DECLARED)["edges"] == graph["edges"]
    assert pg.filter_trust(graph, pg.TRUST_INFERRED)["edges"] == graph["edges"]


def test_a_duplicate_edge_carries_the_verified_ratio_and_its_spans(store):
    graph = _build(store)
    pair = {_n(store, "rule"), _n(store, "dup")}
    edge = next(e for e in graph["edges"]
                if e["kind"] == "duplicate_of" and {e["from"], e["to"]} == pair)
    from src import text_overlap
    measured = text_overlap.overlap(RULE_DUP, RULE)["ratio"]
    assert edge["confidence"] == pytest.approx(measured, abs=1e-4)
    assert edge["confidence"] >= pg.DUPLICATE_THRESHOLD
    assert "verified by exact substring comparison" in edge["why"]
    assert edge["meta"]["spans"], "the verified spans travel with the edge"


def test_a_file_evidence_span_keeps_the_line_it_named(store):
    graph = _build(store)
    node = next(n for n in graph["nodes"] if n["id"] == "file:src/provenance_graph.py")
    assert node["meta"]["lines"] == [42]
    edge = next(e for e in graph["edges"]
                if e["kind"] == "evidence_of" and e["to"] == node["id"]
                and e["from"] == _n(store, "rule"))
    assert "src/provenance_graph.py:42" in edge["why"]
    assert edge["meta"]["line"] == 42


def test_an_absolute_path_lands_on_the_same_file_node_as_the_checkpoint_diff(store):
    """A memory span naming the absolute path and a checkpoint naming the
    workspace-relative one are the same file, so they are one node."""
    engine.add_item("The app entrypoint registers every router", owner="luis",
                    project=store.workspace, level="semantic", now=NOW,
                    evidence=[{"kind": "file",
                               "ref": os.path.join(store.workspace, "app.py")}])
    graph = _build(store)
    assert len([n for n in graph["nodes"] if n["kind"] == "file"
                and n["id"] == "file:app.py"]) == 1
    kinds = {e["kind"] for e in graph["edges"] if e["to"] == "file:app.py"}
    assert kinds == {"changed", "evidence_of"}


# ── value #2: orphans and duplicates ────────────────────────────────────


def test_orphans_are_the_nodes_no_edge_touches_grouped_by_kind(store):
    graph = _build(store)
    loose = pg.orphans(graph)
    assert loose["ids"] == [_n(store, "lone")]
    assert list(loose["by_kind"]) == ["memory"]
    assert loose["by_kind"]["memory"][0]["label"] == LONE
    assert loose["count"] == 1
    assert pg.stats(graph)["orphans"] == 1


def test_orphans_answers_identically_twice(store):
    graph = _build(store)
    assert pg.orphans(graph) == pg.orphans(graph)


# ── value #3: neighbours and impact ─────────────────────────────────────


def test_neighbors_at_one_and_two_hops(store):
    graph = _build(store)
    one = pg.neighbors(graph, "checkpoint:job0001", hops=1)
    assert {n["id"] for n in one["nodes"]} == {
        "checkpoint:job0001", "chat:sess-abc", "file:app.py",
        "file:src/provenance_graph.py", "objective:OBJ-1",
    }
    two = pg.neighbors(graph, "checkpoint:job0001", hops=2)
    assert {n["id"] for n in one["nodes"]} < {n["id"] for n in two["nodes"]}
    # Two hops reaches what the one-hop neighbours themselves touch.
    assert {"objective:OBJ-2", _n(store, "rule")} <= {n["id"] for n in two["nodes"]}
    assert len(two["edges"]) >= len(one["edges"])
    assert two["edges"] == sorted(two["edges"], key=lambda e: (e["kind"], e["from"], e["to"]))


def test_neighbors_of_an_unknown_node_is_empty_not_an_error(store):
    graph = _build(store)
    answer = pg.neighbors(graph, "memory:does-not-exist")
    assert answer["missing"] is True and answer["nodes"] == [] and answer["edges"] == []


def test_impact_is_reachability_along_reversed_depends_on_and_changed(store):
    graph = _build(store)
    # Touch OBJ-2 and OBJ-1 (which declares the dependency) is threatened.
    assert pg.impact(graph, "objective:OBJ-2") == ["objective:OBJ-1"]
    # Touch a file and the checkpoint whose recorded diff names it is affected.
    assert pg.impact(graph, "file:app.py") == ["checkpoint:job0001"]
    # A leaf of both relations breaks nothing, and the root is never in its own set.
    assert pg.impact(graph, "objective:OBJ-1") == []
    assert pg.impact(graph, _n(store, "lone")) == []
    assert pg.impact(graph, "nonsense") == []


def test_impact_is_transitive(store):
    """OBJ-3 → OBJ-1 → OBJ-2: touching OBJ-2 threatens both."""
    path = os.path.join(store.workspace, ".odysseus", "objectives.jsonl")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": "obj", "id": "OBJ-3", "title": "Draw the page",
                             "status": "open", "priority": 3, "owner": "user",
                             "updated_at": "2026-09-01T11:00:00Z"}) + "\n")
        fh.write(json.dumps({"t": "dep", "from": "OBJ-3", "to": "OBJ-1"}) + "\n")
    graph = _build(store)
    assert pg.impact(graph, "objective:OBJ-2") == ["objective:OBJ-1", "objective:OBJ-3"]


# ── value #1: explain ───────────────────────────────────────────────────


def test_explain_returns_an_ordered_chain_whose_steps_carry_why(store):
    graph = _build(store)
    answer = pg.explain(graph, _n(store, "rule"))

    assert answer["missing"] is False
    assert answer["node"]["label"] == RULE
    steps = answer["steps"]
    assert steps, "the rule has two stored evidence spans"
    assert [s["order"] for s in steps] == list(range(1, len(steps) + 1))
    assert [s["hop"] for s in steps] == sorted(s["hop"] for s in steps)
    for step in steps:
        assert step["why"].strip(), step
        assert step["direction"] in ("rests_on", "vouches_for")
        assert step["trust"] == pg.TRUST_DECLARED
        assert step["node"] is not None

    # The chain is memory → evidence span → chat / file: the stored evidence
    # comes FIRST, before the "also stored over there" duplicate.
    first_two = steps[:2]
    assert all(s["kind"] == "evidence_of" and s["hop"] == 1 for s in first_two)
    assert {s["to"] for s in first_two} == {"chat:sess-abc", "file:src/provenance_graph.py"}
    assert "src/provenance_graph.py:42" in next(
        s["why"] for s in first_two if s["to"] == "file:src/provenance_graph.py")
    assert any(s["kind"] == "duplicate_of" for s in steps)


def test_explain_reaches_the_checkpoint_that_touched_the_file_the_memory_names(store):
    """The multi-hop audit answer: the rule cites a file, and a job's recorded
    diff says that file changed. Both are stored records."""
    answer = pg.explain(_build(store), _n(store, "rule"))
    changed = [s for s in answer["steps"] if s["kind"] == "changed"]
    assert changed and changed[0]["direction"] == "vouches_for"
    assert changed[0]["from"] == "checkpoint:job0001"
    assert "observed on disk, not claimed by a worker" in changed[0]["why"]


def test_explain_tells_a_rule_it_was_inverted(store):
    answer = pg.explain(_build(store), _n(store, "original"))
    step = next(s for s in answer["steps"] if s["kind"] == "contradicts")
    assert step["direction"] == "vouches_for"
    assert step["from"] == _n(store, "anti")
    assert "inverted_from" in step["why"]


def test_explain_of_a_node_nothing_points_at_says_so(store):
    answer = pg.explain(_build(store), _n(store, "lone"))
    assert answer["steps"] == []
    assert "no evidence chain" in answer["summary"]


def test_explain_of_an_unknown_node_is_missing_not_an_error(store):
    answer = pg.explain(_build(store), "memory:nope")
    assert answer == {"node": None, "steps": [], "summary": "", "missing": True}


# ── the ranking signal ──────────────────────────────────────────────────


def test_ranking_signal_never_exceeds_the_cap(store):
    graph = _build(store)
    scores = pg.ranking_signal(graph)
    assert set(scores) == {n["id"] for n in graph["nodes"]}
    assert all(0.0 <= v <= pg.RANKING_CAP for v in scores.values())
    assert pg.RANKING_CAP == 0.10
    assert max(scores.values()) == pytest.approx(pg.RANKING_CAP)
    # The busiest node takes the cap and nothing beats it: the job whose two
    # file changes, its session and its objective evidence all touch it.
    assert scores["checkpoint:job0001"] == pytest.approx(pg.RANKING_CAP)
    # Everything else scores strictly below, in proportion to its degree.
    assert scores["chat:sess-abc"] == pytest.approx(pg.RANKING_CAP * 3 / 4)
    assert scores[_n(store, "lone")] == 0.0


def test_ranking_signal_answers_for_the_ids_it_was_given(store):
    graph = _build(store)
    scores = pg.ranking_signal(graph, ["chat:sess-abc", "not-a-node"])
    assert set(scores) == {"chat:sess-abc", "not-a-node"}
    assert scores["not-a-node"] == 0.0
    assert pg.ranking_signal({"nodes": [], "edges": []}, ["x"]) == {"x": 0.0}


# ── budget, determinism, filtering ──────────────────────────────────────


def test_the_node_budget_truncates_and_says_so(store):
    small = _build(store, limit_nodes=3)
    assert len(small["nodes"]) == 3
    assert small["truncated"] is True
    known = {n["id"] for n in small["nodes"]}
    # Never an edge pointing into a node the budget cut.
    assert all(e["from"] in known and e["to"] in known for e in small["edges"])
    assert pg.stats(small)["truncated"] is True
    assert _build(store, limit_nodes=3) == small          # deterministic cut


def test_two_builds_of_the_same_records_are_identical(store):
    assert _build(store) == _build(store)
    assert json.dumps(_build(store), sort_keys=True) == json.dumps(_build(store), sort_keys=True)


def test_nodes_and_edges_come_back_in_a_stable_order(store):
    graph = _build(store)
    assert graph["nodes"] == sorted(graph["nodes"], key=lambda n: (n["kind"], n["id"]))
    assert graph["edges"] == sorted(graph["edges"],
                                    key=lambda e: (e["kind"], e["from"], e["to"]))


def test_filter_kinds_drops_the_edges_of_dropped_nodes(store):
    graph = _build(store)
    only = pg.filter_kinds(graph, ["objective"])
    assert {n["kind"] for n in only["nodes"]} == {"objective"}
    assert _edge_set(only) == {("depends_on", "objective:OBJ-1", "objective:OBJ-2")}
    assert pg.filter_kinds(graph, [])["nodes"] == graph["nodes"]
    assert pg.filter_kinds(graph, None)["nodes"] == graph["nodes"]
    assert pg.filter_kinds(graph, ["nope"])["nodes"] == []


def test_stats_counts_by_kind(store):
    numbers = pg.stats(_build(store))
    assert numbers["nodes"] == 11 and numbers["edges"] == 11
    assert numbers["node_kinds"] == {"chat": 1, "checkpoint": 1, "file": 2,
                                     "memory": 5, "objective": 2}
    assert numbers["edge_kinds"] == {"changed": 2, "contradicts": 1, "depends_on": 1,
                                     "duplicate_of": 2, "evidence_of": 5}


# ── every source is optional ────────────────────────────────────────────


def test_no_project_means_no_objectives_and_no_error(store):
    graph = _build(store, project=None)
    assert [n for n in graph["nodes"] if n["kind"] == "objective"] == []
    assert graph["sources"]["objectives"]["available"] is False
    assert graph["sources"]["memory"]["available"] is True
    assert [n for n in graph["nodes"] if n["kind"] == "memory"]


def test_a_missing_memory_database_yields_a_smaller_graph(tmp_path, monkeypatch, store):
    """The store module raising is a degradation, not a failure: the objectives
    half of the graph still builds."""
    def boom(*a, **kw):
        raise engine.MemoryEngineError("store unusable")

    monkeypatch.setattr(engine, "scoped_items", boom)
    graph = _build(store)
    assert [n for n in graph["nodes"] if n["kind"] == "memory"] == []
    assert graph["sources"]["memory"]["available"] is False
    assert "unusable" in graph["sources"]["memory"]["note"]
    assert [n["id"] for n in graph["nodes"] if n["kind"] == "objective"] == [
        "objective:OBJ-1", "objective:OBJ-2"]


def test_a_missing_objectives_file_yields_a_smaller_graph(store):
    os.remove(os.path.join(store.workspace, ".odysseus", "objectives.jsonl"))
    graph = _build(store)
    assert [n for n in graph["nodes"] if n["kind"] == "objective"] == []
    assert graph["sources"]["objectives"]["available"] is True
    assert graph["sources"]["objectives"]["count"] == 0
    # The log records now name objectives that are not there: no phantom nodes.
    assert "checkpoint:job0001" in {n["id"] for n in graph["nodes"]}   # from the mirror
    assert ("evidence_of", "objective:OBJ-1", "checkpoint:job0001") not in _edge_set(graph)


def test_a_corrupt_objectives_file_does_not_raise(store):
    path = os.path.join(store.workspace, ".odysseus", "objectives.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{not json at all\n")
    graph = _build(store)
    assert [n for n in graph["nodes"] if n["kind"] == "objective"] == []
    assert graph["sources"]["objectives"]["available"] is True


def test_missing_dispatch_mirrors_yield_a_smaller_graph(store):
    os.remove(os.path.join(store.data, "dispatch", "job0001.json"))
    graph = _build(store)
    assert graph["sources"]["checkpoints"]["available"] is False
    # The objectives log still names the job, so the node stands — with no
    # changed-file edges, because nothing on disk says what it changed.
    assert "checkpoint:job0001" in {n["id"] for n in graph["nodes"]}
    assert [e for e in graph["edges"] if e["kind"] == "changed"] == []


def test_a_half_written_mirror_is_skipped(store):
    path = os.path.join(store.data, "dispatch", "job0002.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('{"id": "job0002", "chan')
    graph = _build(store)
    assert "checkpoint:job0002" not in {n["id"] for n in graph["nodes"]}
    assert "checkpoint:job0001" in {n["id"] for n in graph["nodes"]}


def test_every_source_absent_still_answers_with_a_valid_graph(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "DATA_DIR", str(tmp_path / "empty-data"))
    monkeypatch.setattr(pg, "DATA_DIR", str(tmp_path / "empty-data"))
    engine.set_vector_store(None)
    try:
        graph = pg.build("nobody", project=None, workspace=str(tmp_path / "nowhere"),
                         now=NOW)
    finally:
        engine.reset_vector_store()
    assert graph["nodes"] == [] and graph["edges"] == []
    assert graph["truncated"] is False
    assert set(graph["sources"]) >= {"objectives", "memory", "checkpoints", "duplicates"}
    # An empty store is a WORKING store, so memory reports available with a
    # count of zero; everything genuinely absent says so instead.
    assert graph["sources"]["memory"]["available"] is True
    assert graph["sources"]["memory"]["count"] == 0
    assert all(graph["sources"][name]["available"] is False
               for name in ("objectives", "objective_log", "checkpoints", "experts",
                            "duplicates"))
    assert all(graph["sources"][name]["note"] for name in graph["sources"])
    assert pg.orphans(graph)["count"] == 0
    assert pg.explain(graph, "memory:x")["missing"] is True
    assert pg.impact(graph, "memory:x") == []


def test_build_never_raises_on_nonsense_arguments(store):
    for kwargs in ({"project": "not a dict"}, {"limit_nodes": "many"},
                   {"limit_nodes": 0}, {"limit_nodes": -5}, {"workspace": 12345}):
        graph = pg.build("luis", now=NOW, **kwargs)
        assert isinstance(graph["nodes"], list) and isinstance(graph["edges"], list)


def test_reader_helpers_never_raise_on_a_malformed_graph():
    for graph in (None, {}, {"nodes": None, "edges": "x"}, {"nodes": [1, 2], "edges": [3]},
                  "not a graph"):
        assert isinstance(pg.stats(graph), dict)
        assert isinstance(pg.orphans(graph), dict)
        assert isinstance(pg.neighbors(graph, "x"), dict)
        assert pg.impact(graph, "x") == []
        assert pg.explain(graph, "x")["missing"] is True
        assert isinstance(pg.ranking_signal(graph), dict)
        assert isinstance(pg.filter_kinds(graph, ["memory"]), dict)


# ── experts: a source that is skipped cleanly when nothing cites ────────


def test_the_expert_source_is_skipped_when_nothing_cites_a_chunk(store):
    graph = _build(store)
    assert graph["sources"]["experts"]["available"] is False
    assert "expert chunk" in graph["sources"]["experts"]["note"]
    assert [n for n in graph["nodes"] if n["kind"] in ("expert", "corpus")] == []


def test_a_stored_evidence_ref_naming_an_indexed_chunk_becomes_a_cites_edge(
        store, tmp_path, monkeypatch):
    """The only stored citation record Faustus has: an evidence ref naming a
    chunk that an expert's own index knows. The page comes out of that index —
    services/experts.py copies it and never invents one."""
    from services import experts as experts_svc
    monkeypatch.setattr(experts_svc, "DATA_DIR", str(tmp_path / "experts-data"))
    slug = "brenner-bot"
    os.makedirs(experts_svc.expert_dir(slug), exist_ok=True)
    chunk_id = "c" + "a1b2c3d4e5f60718"
    experts_svc.save_index(slug, [{
        "id": chunk_id, "source": "Brenner.pdf", "page": 118,
        "page_confidence": "exact", "start_line": 3, "end_line": 9,
        "text": "Cut every sentence that only restates the last one.",
    }])
    engine.add_item("Cut sentences that restate the previous one", owner="luis",
                    project=store.workspace, level="procedural", now=NOW,
                    evidence=[{"kind": "chat", "ref": f"expert:{slug}#{chunk_id}"}])

    graph = _build(store)
    assert graph["sources"]["experts"]["available"] is True
    assert "expert:brenner-bot" in {n["id"] for n in graph["nodes"]}
    corpus = next(n for n in graph["nodes"] if n["kind"] == "corpus")
    assert corpus["id"] == "corpus:brenner-bot/Brenner.pdf"
    cites = next(e for e in graph["edges"] if e["kind"] == "cites")
    assert cites["to"] == corpus["id"]
    assert cites["confidence"] == 1.0 and "page 118" in cites["why"]
    assert ("contains", "expert:brenner-bot", corpus["id"]) in _edge_set(graph)


def test_a_chunk_no_index_knows_cites_nothing(store, tmp_path, monkeypatch):
    from services import experts as experts_svc
    monkeypatch.setattr(experts_svc, "DATA_DIR", str(tmp_path / "experts-data"))
    os.makedirs(experts_svc.expert_dir("brenner-bot"), exist_ok=True)
    experts_svc.save_index("brenner-bot", [])
    engine.add_item("A rule citing a chunk that does not exist", owner="luis",
                    project=store.workspace, level="procedural", now=NOW,
                    evidence=[{"kind": "chat", "ref": "expert:brenner-bot#c0000000000000ff"}])
    graph = _build(store)
    assert [n for n in graph["nodes"] if n["kind"] in ("expert", "corpus")] == []
    assert graph["sources"]["experts"]["available"] is False


# ── settings ────────────────────────────────────────────────────────────


def test_settings_helpers_read_the_defaults(monkeypatch):
    from src.settings import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["agent_provenance_graph"] is True
    assert DEFAULT_SETTINGS["agent_provenance_max_nodes"] == 2000
    assert pg.enabled() in (True, False)
    assert 50 <= pg.max_nodes() <= pg.MAX_LIMIT_NODES

    import src.settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: {"agent_provenance_graph": False,
                                                   "agent_provenance_max_nodes": 10 ** 9}[key])
    assert pg.enabled() is False
    assert pg.max_nodes() == pg.MAX_LIMIT_NODES


def test_settings_helpers_never_raise(monkeypatch):
    import src.settings as settings_mod

    def boom(*a, **kw):
        raise RuntimeError("settings are gone")

    monkeypatch.setattr(settings_mod, "get_setting", boom)
    assert pg.enabled() is True
    assert pg.max_nodes() == pg.DEFAULT_LIMIT_NODES


# ── the API ─────────────────────────────────────────────────────────────

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("starlette.testclient")


def _client(monkeypatch):
    """The real router + core.middleware.require_admin, with an auth manager
    stub and the X-User header standing in for AuthMiddleware."""
    from fastapi import FastAPI
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.testclient import TestClient
    import core.middleware as mw
    from routes.provenance_routes import setup_provenance_routes

    monkeypatch.setattr(mw, "auth_disabled", lambda: False)
    app = FastAPI()
    app.include_router(setup_provenance_routes())
    app.state.auth_manager = SimpleNamespace(is_configured=True, is_admin=lambda u: u == "luis")

    class _Stamp(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            user = request.headers.get("x-user")
            if user:
                request.state.current_user = user
            return await call_next(request)

    app.add_middleware(_Stamp)
    return TestClient(app, raise_server_exceptions=False)


def _get(client, path, workspace, **params):
    params["workspace"] = workspace
    return client.get(path, params=params, headers={"x-user": "luis"})


def test_graph_endpoint_answers_with_nodes_edges_sources_and_stats(store, monkeypatch):
    client = _client(monkeypatch)
    response = _get(client, "/api/provenance/graph", store.workspace)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert set(body) == {"status", "nodes", "edges", "sources", "truncated", "stats",
                         "node_kinds", "edge_kinds", "enabled", "limit"}
    assert body["stats"]["nodes"] == len(body["nodes"])
    assert body["node_kinds"] == list(pg.NODE_KINDS)
    assert {n["id"] for n in body["nodes"]} >= {_n(store, "rule"), "chat:sess-abc"}
    for edge in body["edges"]:
        assert set(edge) >= {"from", "to", "kind", "confidence", "why", "trust"}


def test_graph_endpoint_filters_by_kind_and_limit(store, monkeypatch):
    client = _client(monkeypatch)
    body = _get(client, "/api/provenance/graph", store.workspace, kinds="memory").json()
    assert {n["kind"] for n in body["nodes"]} == {"memory"}
    assert {e["kind"] for e in body["edges"]} <= {"duplicate_of", "contradicts"}

    small = _get(client, "/api/provenance/graph", store.workspace, limit=2).json()
    assert len(small["nodes"]) == 2 and small["truncated"] is True and small["limit"] == 2


def test_explain_endpoint_returns_the_ordered_chain(store, monkeypatch):
    client = _client(monkeypatch)
    node_id = _n(store, "rule")
    body = _get(client, f"/api/provenance/node/{node_id}/explain", store.workspace).json()
    assert body["status"] == "success"
    assert body["node"]["id"] == node_id
    assert [s["order"] for s in body["steps"]] == list(range(1, len(body["steps"]) + 1))
    assert all(s["why"] for s in body["steps"])
    assert body["summary"] and body["enabled"] is True


def test_explain_endpoint_handles_a_file_node_id_with_slashes(store, monkeypatch):
    client = _client(monkeypatch)
    body = _get(client, "/api/provenance/node/file:src/provenance_graph.py/explain",
                store.workspace).json()
    assert body["node"]["id"] == "file:src/provenance_graph.py"
    assert body["steps"]


def test_explain_endpoint_404s_on_an_unknown_node(store, monkeypatch):
    client = _client(monkeypatch)
    response = _get(client, "/api/provenance/node/memory:nope/explain", store.workspace)
    assert response.status_code == 404


def test_neighbors_endpoint_carries_the_impact_set(store, monkeypatch):
    client = _client(monkeypatch)
    body = _get(client, "/api/provenance/node/objective:OBJ-2/neighbors",
                store.workspace, hops=1).json()
    assert body["root"] == "objective:OBJ-2" and body["hops"] == 1
    assert body["impact_ids"] == ["objective:OBJ-1"]
    assert [n["id"] for n in body["impact"]] == ["objective:OBJ-1"]
    assert "objective:OBJ-1" in {n["id"] for n in body["nodes"]}


def test_orphans_endpoint_returns_orphans_and_duplicate_pairs(store, monkeypatch):
    client = _client(monkeypatch)
    body = _get(client, "/api/provenance/orphans", store.workspace).json()
    assert body["count"] == 1
    assert body["orphan_ids"] == [_n(store, "lone")]
    assert list(body["orphans"]) == ["memory"]
    pairs = {frozenset((p["a"], p["b"])) for p in body["duplicates"]}
    assert pairs == {frozenset((_n(store, "dup"), _n(store, "rule"))),
                     frozenset((_n(store, "anti"), _n(store, "original")))}
    for pair in body["duplicates"]:
        assert pair["ratio"] >= pg.DUPLICATE_THRESHOLD
        assert pair["a_label"] and pair["b_label"] and pair["why"]


def test_turning_the_setting_off_reads_nothing_and_says_so(store, monkeypatch):
    """The toggle does something real: no source is read, and the answer names
    the setting instead of looking like an empty workspace."""
    import src.settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: False if key == "agent_provenance_graph"
                        else default)
    client = _client(monkeypatch)

    graph = _get(client, "/api/provenance/graph", store.workspace).json()
    assert graph["enabled"] is False
    assert graph["nodes"] == [] and graph["edges"] == []
    assert "turned off" in graph["sources"]["settings"]["note"]

    loose = _get(client, "/api/provenance/orphans", store.workspace).json()
    assert loose["enabled"] is False and loose["count"] == 0 and loose["duplicates"] == []

    response = _get(client, "/api/provenance/node/objective:OBJ-1/explain", store.workspace)
    assert response.status_code == 404
    assert "turned off" in response.json()["detail"]


def test_every_endpoint_is_admin_only(store, monkeypatch):
    client = _client(monkeypatch)
    for path in ("/api/provenance/graph", "/api/provenance/orphans",
                 "/api/provenance/node/objective:OBJ-1/explain",
                 "/api/provenance/node/objective:OBJ-1/neighbors"):
        assert client.get(path).status_code == 403
        assert client.get(path, headers={"x-user": "someone-else"}).status_code == 403


# ── robot mode ──────────────────────────────────────────────────────────


def test_robot_mode_wraps_the_graph_in_the_envelope_with_flat_rows(store, monkeypatch):
    client = _client(monkeypatch)
    body = _get(client, "/api/provenance/graph", store.workspace, robot=1).json()
    assert set(body) == {"ok", "data", "error_code", "error", "elapsed_ms", "schema_version"}
    assert body["ok"] is True and body["error_code"] is None
    data = body["data"]
    assert set(data) == {"nodes", "edges", "sources", "truncated", "nodes_total",
                         "edges_total", "orphans_total"}
    # Every row is the same all-scalar column tuple — TOON's tabular condition.
    for row in data["nodes"]:
        assert list(row) == ["id", "kind", "label", "detail", "status", "path", "score"]
        assert all(v is None or isinstance(v, (str, int, float, bool)) for v in row.values())
    for row in data["edges"]:
        assert list(row) == ["from", "to", "kind", "confidence", "trust", "why"]
    # meta is dropped: it is what stopped the rows from tabularising.
    assert all("meta" not in row for row in data["nodes"] + data["edges"])
    assert data["nodes_total"] == len(data["nodes"])


def test_robot_mode_projects_explain_neighbors_and_orphans(store, monkeypatch):
    client = _client(monkeypatch)
    explain = _get(client, f"/api/provenance/node/{_n(store, 'rule')}/explain",
                   store.workspace, robot=1).json()
    assert explain["ok"] is True
    assert set(explain["data"]) == {"node", "kind", "label", "summary", "steps"}
    assert all(list(s) == ["order", "hop", "direction", "kind", "from", "to",
                           "confidence", "trust", "why"] for s in explain["data"]["steps"])

    hood = _get(client, "/api/provenance/node/objective:OBJ-2/neighbors",
                store.workspace, robot=1).json()
    assert set(hood["data"]) == {"root", "hops", "nodes", "edges", "impact"}
    assert hood["data"]["impact"] == ["objective:OBJ-1"]

    loose = _get(client, "/api/provenance/orphans", store.workspace, robot=1).json()
    assert set(loose["data"]) == {"orphans", "count", "duplicates"}
    assert [row["id"] for row in loose["data"]["orphans"]] == [_n(store, "lone")]
    assert all(list(d) == ["a", "b", "ratio", "a_label", "b_label", "why"]
               for d in loose["data"]["duplicates"])


def test_robot_mode_toon_renders_the_tables(store, monkeypatch):
    client = _client(monkeypatch)
    response = _get(client, "/api/provenance/graph", store.workspace, format="toon")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    from src import toon
    decoded = toon.decode(response.text)
    assert decoded["ok"] is True
    assert len(decoded["data"]["nodes"]) == 11


def test_robot_mode_turns_a_404_into_an_envelope(store, monkeypatch):
    client = _client(monkeypatch)
    response = _get(client, "/api/provenance/node/memory:nope/explain",
                    store.workspace, robot=1)
    assert response.status_code == 404
    body = response.json()
    assert body["ok"] is False and body["error_code"] == "http_404"
    assert "memory:nope" in body["error"]


def test_a_call_without_robot_parameters_is_unchanged(store, monkeypatch):
    """The contract robot mode is built on: no query parameter, no change."""
    client = _client(monkeypatch)
    plain = _get(client, "/api/provenance/graph", store.workspace)
    assert plain.json()["status"] == "success"
    assert "ok" not in plain.json()


# ── registration ────────────────────────────────────────────────────────


def test_app_registers_the_provenance_router():
    from pathlib import Path
    source = (Path(__file__).resolve().parent.parent / "app.py").read_text(encoding="utf-8")
    assert "from routes.provenance_routes import setup_provenance_routes" in source
    assert "app.include_router(setup_provenance_routes())" in source
