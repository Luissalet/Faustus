"""Selective rebuild of derived results, resumption, parameter and version
changes, the decision map and Atlas registration (src/derivation_graph.py).
Temp directories only; no network."""
from __future__ import annotations

import os

import pytest

from src import constants as constants_mod
from src import derivation_graph as dg

OWNER = "alice"
G = "g"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path / "data"))
    yield


@pytest.fixture()
def work(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    return d


def write(path, text):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


class Runner:
    """Upper-cases inputs into outputs and counts what ran."""

    def __init__(self, work, fail_on=None):
        self.work = work
        self.calls = []
        self.fail_on = set(fail_on or [])
        self.keys = {}

    def __call__(self, view):
        nid = view["id"]
        self.calls.append(nid)
        self.keys.setdefault(nid, []).append(view["idempotency_key"])
        if nid in self.fail_on:
            raise RuntimeError("boom in " + nid)
        text = ""
        for spec in view["inputs"]:
            if spec["type"] == "file":
                text += read(spec["resolved_path"])
            elif spec["type"] == "node":
                for o in spec["outputs"]:
                    text += read(os.path.join(view["base_dir"], o["path"]))
        text += view["params"].get("suffix", "")
        out = view["declared_outputs"][0]["path"]
        write(os.path.join(view["base_dir"], out), text.upper())
        return {"outputs": [out]}


def chain(work, extra_input=False):
    write(work / "raw.txt", "abc")
    write(work / "side.txt", "xyz")
    dg.set_base_dir(OWNER, G, str(work))
    dg.define_node(OWNER, G, "a", op={"kind": "tool", "name": "clean", "version": "1"}, inputs=["raw.txt"],
                   outputs=["a.out"])
    dg.define_node(OWNER, G, "b", op={"kind": "tool", "name": "join", "version": "1"},
                   inputs=[{"node": "a"}, "side.txt"], outputs=["b.out"], params={"suffix": "-b"})
    dg.define_node(OWNER, G, "c", op={"kind": "tool", "name": "render", "version": "1"},
                   inputs=[{"node": "b"}], outputs=["c.out"])
    dg.define_node(OWNER, G, "d", op={"kind": "tool", "name": "other", "version": "1"},
                   inputs=["side.txt"], outputs=["d.out"])


def test_first_build_runs_everything_then_nothing(work):
    chain(work)
    r = Runner(work)
    out = dg.rebuild(OWNER, G, "c", runner=r, atlas=False)
    assert out["status"] == "rebuilt" and out["rebuilt"] == ["a", "b", "c"]
    assert read(work / "c.out") == "ABCXYZ-B"
    again = Runner(work)
    out = dg.rebuild(OWNER, G, "c", runner=again, atlas=False)
    assert out["status"] == "up_to_date" and out["reused"] == ["a", "b", "c"] and again.calls == []


def test_changed_input_reruns_only_downstream(work):
    chain(work)
    dg.rebuild(OWNER, G, None, runner=Runner(work), atlas=False)
    write(work / "side.txt", "XYZ2")                       # input of b and d, not of a
    plan = dg.plan(OWNER, G, "c")
    assert plan["stale"] == ["b", "c"] and plan["intact"] == ["a"]
    assert any(r.startswith("input_changed:file:side.txt") for r in plan["nodes"][1]["reasons"])
    assert plan["nodes"][2]["conditional"] is True            # c: only because b is stale
    r = Runner(work)
    out = dg.rebuild(OWNER, G, "c", runner=r, atlas=False)
    assert r.calls == ["b", "c"] and out["reused"] == ["a"]
    assert read(work / "c.out") == "ABCXYZ2-B"
    assert dg.plan(OWNER, G, "d")["stale"] == ["d"]           # d was not asked for and is still stale


def test_unchanged_output_stops_the_cascade(work):
    chain(work)
    dg.rebuild(OWNER, G, "c", runner=Runner(work), atlas=False)
    write(work / "raw.txt", "ABC")                           # upper-cased it comes out identical
    r = Runner(work)
    out = dg.rebuild(OWNER, G, "c", runner=r, atlas=False)
    assert r.calls == ["a"] and out["reused"] == ["b", "c"]


def test_failure_in_the_middle_then_resume_does_not_repeat_completed_nodes(work):
    chain(work)
    first = Runner(work, fail_on={"b"})
    out = dg.rebuild(OWNER, G, "c", runner=first, atlas=False)
    assert out["status"] == "failed" and out["failed"]["node"] == "b" and "boom" in out["failed"]["error"]
    assert first.calls == ["a", "b"] and out["skipped"] == ["c"]
    second = Runner(work)
    out = dg.rebuild(OWNER, G, "c", runner=second, atlas=False)
    assert second.calls == ["b", "c"]                         # a completed: not executed again
    assert out["reused"] == ["a"] and out["status"] == "rebuilt"
    # the retry of b carries the same idempotency key as the attempt that failed
    assert second.keys["b"] == first.keys["b"]


def test_interrupted_node_is_offered_again_with_the_same_key(work):
    chain(work)
    first = dg.rebuild(OWNER, G, "a")                         # no runner: opens the attempt and stops
    assert first["status"] == "awaiting_execution" and first["next"]["id"] == "a"
    key = first["next"]["idempotency_key"]
    again = dg.rebuild(OWNER, G, "a")
    assert again["next"]["interrupted"] is True and again["next"]["idempotency_key"] == key


def test_agent_in_the_loop_mode_completes_with_measured_hashes(work):
    chain(work)
    step = dg.rebuild(OWNER, G, "c")
    assert step["status"] == "awaiting_execution" and step["next"]["id"] == "a"
    write(work / "a.out", "ABC")
    done = dg.complete_node(OWNER, G, "a", outputs=["a.out"])
    assert done["status"] == "done" and len(done["outputs"][0]["hash"]) == 64
    step = dg.rebuild(OWNER, G, "c")
    assert step["next"]["id"] == "b" and step["reused"] == ["a"]
    miss = dg.complete_node(OWNER, G, "b", outputs=["nothing.out"])
    assert miss["status"] == "failed" and "not produced" in miss["error"]


def test_parameter_change_is_detected(work):
    chain(work)
    dg.rebuild(OWNER, G, "c", runner=Runner(work), atlas=False)
    dg.define_node(OWNER, G, "b", op={"kind": "tool", "name": "join", "version": "1"}, params={"suffix": "-B2"})
    plan = dg.plan(OWNER, G, "c")
    assert plan["stale"] == ["b", "c"] and "parameter_changed" in plan["nodes"][1]["reasons"]
    r = Runner(work)
    dg.rebuild(OWNER, G, "c", runner=r, atlas=False)
    assert r.calls == ["b", "c"] and read(work / "c.out") == "ABCXYZ-B2"


def test_tool_version_change_is_detected(work):
    chain(work)
    dg.rebuild(OWNER, G, "c", runner=Runner(work), atlas=False)
    dg.define_node(OWNER, G, "a", op={"kind": "tool", "name": "clean", "version": "2"})
    plan = dg.plan(OWNER, G, "c")
    assert plan["stale"] == ["a", "b", "c"]
    assert "tool_version_changed:1->2" in plan["nodes"][0]["reasons"]


def test_edited_or_missing_output_is_stale(work):
    chain(work)
    dg.rebuild(OWNER, G, "b", runner=Runner(work), atlas=False)
    write(work / "a.out", "hand edited")
    assert any(r.startswith("output_changed") for r in dg.plan(OWNER, G, "b")["nodes"][0]["reasons"])
    os.remove(work / "a.out")
    assert any(r.startswith("output_missing") for r in dg.plan(OWNER, G, "b")["nodes"][0]["reasons"])


def test_recipe_version_is_part_of_identity(work, monkeypatch):
    from src import recipes
    r = recipes.Recipe(id="rec-1", title="t", status="published", version="1.0.0")
    monkeypatch.setattr(recipes, "get_recipe", lambda rid, owner=None: r)
    write(work / "raw.txt", "q")
    dg.set_base_dir(OWNER, G, str(work))
    dg.define_node(OWNER, G, "x", op={"kind": "recipe", "name": "rec-1"}, inputs=["raw.txt"], outputs=["x.out"])
    dg.rebuild(OWNER, G, "x", runner=Runner(work), atlas=False)
    assert dg.plan(OWNER, G, "x")["stale"] == []
    r.version = "1.1.0"
    assert "tool_version_changed" in dg.plan(OWNER, G, "x")["nodes"][0]["reasons"][0]


def test_cycles_and_unknown_inputs_are_refused(work):
    chain(work)
    with pytest.raises(dg.DerivationError) as e:
        dg.define_node(OWNER, G, "a", op={"name": "clean"}, inputs=[{"node": "c"}])
    assert e.value.error_class == "derivations.cycle"
    assert dg.get_graph(OWNER, G)["nodes"]["a"]["inputs"] == [{"type": "file", "path": "raw.txt"}]
    with pytest.raises(dg.DerivationError):
        dg.define_node(OWNER, G, "z", op={"name": "t"}, inputs=[{"node": "nope"}])


def test_decision_map_affected_by_follows_only_explicit_links(work):
    chain(work)
    dg.map_add(OWNER, G, "req:currency", "requirement", "Report in euros")
    dg.map_add(OWNER, G, "dec:rate-source", "decision", "Use the ECB daily rate")
    dg.map_add(OWNER, G, "del:report", "deliverable", "Quarterly report", path="c.out")
    dg.map_add(OWNER, G, "del:other", "deliverable", "Unrelated deliverable")
    dg.map_add(OWNER, G, "unit:eur", "unit", "EUR")
    dg.map_link(OWNER, G, "req:currency", "dec:rate-source", "decided by")
    dg.map_link(OWNER, G, "dec:rate-source", "node:b", "implemented in")
    dg.map_link(OWNER, G, "node:c", "del:report", "delivers")
    out = dg.affected_by(OWNER, G, "req:currency")
    ids = [a["id"] for a in out["affected"]]
    assert ids == ["dec:rate-source", "node:b", "node:c", "del:report"]
    assert out["recalculate"] == ["b", "c"] and [d["id"] for d in out["deliverables"]] == ["del:report"]
    assert "del:other" not in ids and "node:d" not in ids and "node:a" not in ids
    assert out["affected"][-1]["via"][0] == "req:currency"
    by_title = dg.affected_by(OWNER, G, "report in euros")
    assert by_title["resolved"] == ["req:currency"]
    nothing = dg.affected_by(OWNER, G, "unit:eur")
    assert nothing["affected"] == [] and "no explicit link" in nothing["note"]
    with pytest.raises(dg.DerivationError):
        dg.affected_by(OWNER, G, "the currency, whatever it is")


def test_invalidate_forces_the_next_rebuild(work):
    chain(work)
    dg.rebuild(OWNER, G, "c", runner=Runner(work), atlas=False)
    dg.map_add(OWNER, G, "req:fmt", "format", "PDF")
    dg.map_link(OWNER, G, "req:fmt", "node:c")
    dg.affected_by(OWNER, G, "req:fmt", invalidate=True)
    plan = dg.plan(OWNER, G, "c")
    assert plan["stale"] == ["c"] and plan["nodes"][2]["reasons"] == ["decision_map:req:fmt changed"]
    r = Runner(work)
    dg.rebuild(OWNER, G, "c", runner=r, atlas=False)
    assert r.calls == ["c"] and dg.plan(OWNER, G, "c")["stale"] == []


class FakeAtlas:
    def __init__(self, root, ok=True):
        self.root, self.ok = root, ok
        self.files, self.published = {}, []

    def location(self, project_id, *, area="shared", app=None):
        if not self.ok:
            return {"ok": False, "error": "hub unreachable"}
        return {"ok": True, "path": str(self.root / "shared")}

    def register(self, project_id, rel, *, title="", request_id=None):
        fid = self.files.setdefault(rel, "f%d" % (len(self.files) + 1))
        return {"ok": True, "file": {"id": fid, "revision": "rev-" + fid}}

    def publish(self, project_id, source_ids, recipe, output_id, *, source_revisions, request_id=None):
        self.published.append((list(source_ids), recipe, output_id, dict(source_revisions)))
        return {"ok": True, "cache_key": "ck%d" % len(self.published)}


def test_atlas_registration_when_reachable_and_local_otherwise(tmp_path):
    project = tmp_path / "proj"
    shared = project / "shared"
    shared.mkdir(parents=True)
    write(shared / "in.txt", "hi")
    dg.set_base_dir(OWNER, G, str(shared))
    dg.define_node(OWNER, G, "n", op={"kind": "tool", "name": "up", "version": "3"}, inputs=["in.txt"],
                   outputs=["out.txt"], params={"mode": "x"})

    def run(view):
        write(shared / "out.txt", "HI")
        return {"outputs": ["out.txt"]}
    dg.set_atlas(OWNER, G, "p1")
    down = FakeAtlas(project, ok=False)
    out = dg.rebuild(OWNER, G, "n", runner=run, atlas_client=down)
    assert out["atlas"]["local_only"] is True and "not reachable" in out["atlas"]["reason"]
    assert dg.get_graph(OWNER, G)["nodes"]["n"].get("atlas") is None
    up = FakeAtlas(project)
    res = dg.sync_atlas(OWNER, G, client=up)
    assert res["registered"] == 1 and res["local_only"] is False
    sources, recipe, output, revs = up.published[0]
    assert recipe["operation"] == "tool:up" and recipe["version"] == "3" and recipe["parameters"] == {"mode": "x"}
    assert revs == {sources[0]: "rev-" + sources[0]} and output != sources[0]
    assert dg.sync_atlas(OWNER, G, client=up)["registered"] == 0     # already registered: not repeated


def test_graph_without_atlas_says_local_only(work):
    chain(work)
    out = dg.rebuild(OWNER, G, "a", runner=Runner(work))
    assert out["atlas"]["local_only"] is True and "no Atlas project" in out["atlas"]["reason"]
