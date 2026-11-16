"""The galaxy view of the brain: layout, links and degraded sources."""

from __future__ import annotations

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.brain import db as brain_db  # noqa: E402
from src.brain import entities, galaxy, ratified  # noqa: E402


@pytest.fixture()
def world(tmp_path, monkeypatch):
    brain_db.use_dir(str(tmp_path / "brain"))
    monkeypatch.setattr(galaxy, "_projects", lambda owner: [
        {"id": "p1", "name": "Alpha", "workspace": "w1", "folder": "A"},
        {"id": "p2", "name": "Beta", "workspace": "", "folder": ""}])
    monkeypatch.setattr(galaxy, "_agents", lambda owner, limit: [{"id": "a1", "name": "Scout", "model": "m"}])
    monkeypatch.setattr(galaxy, "_sessions", lambda owner, limit: [
        {"id": "s1", "name": "Chat one", "project_id": "p1", "crew_member_id": "a1", "mode": "agent",
         "messages": 12, "at": "2026-11-01"},
        {"id": "s2", "name": "Loose chat", "project_id": "", "crew_member_id": "", "mode": "",
         "messages": 0, "at": ""}])
    monkeypatch.setattr(galaxy, "_outputs", lambda owner, limit: [
        {"id": "o1", "name": "report.md", "kind": "document", "project_id": "p1", "session_id": "s1", "at": ""},
        {"id": "o2", "name": "orphan.png", "kind": "image", "project_id": "", "session_id": "", "at": ""}])
    yield
    brain_db.use_dir(None)


def _by_id(result):
    return {n["id"]: n for n in result["nodes"]}


def test_galaxy_has_the_five_clouds_and_the_links(world):
    ada = entities.upsert_entity("alice", "Ada", type="person")
    bluehaven = entities.upsert_entity("alice", "Bluehaven", type="organization")
    entities.add_relation("alice", ada["id"], "works_at", dst_id=bluehaven["id"])
    ratified.ratify("alice", f"ent:{ada['id']}")

    result = galaxy.build("alice")
    nodes = _by_id(result)
    assert result["counts"] == {"project": 2, "agent": 1, "session": 2, "output": 2, "entity": 2}
    assert result["warnings"] == []
    links = {tuple(link) for link in result["links"]}
    assert ("session:s1", "project:p1") in links and ("session:s1", "agent:a1") in links
    assert ("output:o1", "session:s1") in links
    assert any(a.startswith("entity:") and b.startswith("entity:") for a, b in links)
    # loose items have no project/session link but are still placed
    assert not [l for l in links if "session:s2" in l] and "session:s2" in nodes
    assert not [l for l in links if "output:o2" in l]
    assert nodes[f"entity:{ada['id']}"]["ratified"] is True
    assert nodes[f"entity:{bluehaven['id']}"]["ratified"] is False


def test_children_sit_near_their_parent_and_layout_is_deterministic(world):
    first = galaxy.build("alice")
    second = galaxy.build("alice")
    assert first["nodes"] == second["nodes"]
    nodes = _by_id(first)
    dist = lambda a, b: math.dist(nodes[a]["pos"], nodes[b]["pos"])  # noqa: E731
    assert dist("session:s1", "project:p1") <= 0.25
    assert dist("output:o1", "session:s1") <= 0.1
    assert dist("session:s1", "project:p1") < dist("session:s1", "project:p2")
    assert all(len(n["pos"]) == 3 for n in first["nodes"])


def test_kinds_filter_and_limit(world):
    only = galaxy.build("alice", kinds=["project", "agent"])
    assert {n["kind"] for n in only["nodes"]} == {"project", "agent"}
    assert galaxy.build("alice", kinds=["nonsense"])["counts"]["project"] == 2  # falls back to all


def test_a_broken_source_only_costs_its_own_cloud(world, monkeypatch):
    def boom(owner, limit):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(galaxy, "_sessions", boom)
    result = galaxy.build("alice")
    assert result["counts"]["session"] == 0 and result["counts"]["project"] == 2
    assert result["warnings"] == ["session: database is locked"]
    # outputs still attach to their projects
    assert ("output:o1", "project:p1") in {tuple(l) for l in result["links"]}
