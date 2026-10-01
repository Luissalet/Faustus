"""Lessons ("instincts") need approval before they are injected."""
from __future__ import annotations

import json

import pytest

from src import instincts


@pytest.fixture
def store(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(instincts, "DATA_DIR", str(d), raising=False)
    return d


def _settings(monkeypatch, **overrides):
    import src.settings as st
    monkeypatch.setattr(st, "get_setting", lambda k, d=None: overrides.get(k, d))


def _proposal(owner="alice", trigger="when a task spans many files", action="batch the edits in one script",
              project="/repo"):
    return instincts.upsert(owner, {
        "trigger": trigger, "action": action, "domain": "workflow", "scope": "project",
        "project": project, "source": "session-observation", "status": "proposed",
    })


def test_legacy_records_without_status_stay_active(store):
    path = store / "instincts" / "alice" / "instincts.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "proj-x-abc": {"id": "proj-x-abc", "trigger": "when a", "action": "do a", "confidence": 0.9,
                       "scope": "project", "project": "/repo", "domain": "other",
                       "last_observed": __import__("time").time()},
    }), encoding="utf-8")
    items = instincts.list_instincts("alice", project="/repo")
    assert [i["id"] for i in items] == ["proj-x-abc"]
    assert items[0]["status"] == "active"
    assert "when a" in instincts.render_block("alice", "/repo")


def test_proposed_is_never_injected_until_approved(store):
    rec = _proposal()
    assert rec["status"] == "proposed"
    assert instincts.render_block("alice", "/repo") == ""
    assert instincts.list_instincts("alice", project="/repo") == []
    assert [r["id"] for r in instincts.list_by_status("alice", ("proposed",), project="/repo")] == [rec["id"]]

    done = instincts.approve("alice", rec["id"])
    assert done["status"] == "active" and done["confidence"] >= instincts.APPROVED_CONFIDENCE
    block = instincts.render_block("alice", "/repo")
    assert "batch the edits in one script" in block


def test_approve_can_reword_and_edit_does_not_approve(store):
    rec = _proposal()
    edited = instincts.edit("alice", rec["id"], action="batch mechanical edits in a script")
    assert edited["status"] == "proposed" and edited["action"] == "batch mechanical edits in a script"
    assert instincts.render_block("alice", "/repo") == ""
    done = instincts.approve("alice", rec["id"], trigger="when many files need the same change")
    assert done["trigger"] == "when many files need the same change"
    assert "same change" in instincts.render_block("alice", "/repo")


def test_edit_rejects_blank_text_and_unknown_id(store):
    rec = _proposal()
    with pytest.raises(ValueError):
        instincts.edit("alice", rec["id"], action="   ")
    with pytest.raises(KeyError):
        instincts.approve("alice", "nope")
    with pytest.raises(KeyError):
        instincts.reject("alice", "nope")


def test_rejected_is_not_injected_and_not_revived_by_extraction(store):
    rec = _proposal()
    instincts.reject("alice", rec["id"])
    assert instincts.render_block("alice", "/repo") == ""
    again = _proposal()                      # same idea observed again, automatically
    assert again["status"] == "rejected"
    assert instincts.list_by_status("alice", ("proposed",)) == []
    # an explicit manual add is the user's own decision and does revive it
    manual = instincts.add("alice", trigger=rec["trigger"], action=rec["action"], project="/repo")
    assert manual["status"] == "active"


def test_reobserved_proposal_stays_proposed_and_active_stays_active(store):
    rec = _proposal()
    again = _proposal()
    assert again["id"] == rec["id"] and again["status"] == "proposed" and again["observations"] == 2
    instincts.approve("alice", rec["id"])
    third = _proposal()
    assert third["status"] == "active"


def test_status_reports_proposals(store):
    _proposal()
    st = instincts.status("alice")
    assert st["proposed"] == 1 and st["total"] == 0
    assert st["pending_proposals"][0]["trigger"].startswith("when a task")


def test_should_offer_needs_enough_tool_calls_only_with_approval(monkeypatch):
    _settings(monkeypatch)
    assert instincts.require_approval() is True
    assert instincts.should_offer(7) is False and instincts.should_offer(8) is True
    _settings(monkeypatch, instincts_offer_min_tool_calls=3)
    assert instincts.should_offer(3) is True and instincts.should_offer(2) is False
    _settings(monkeypatch, instincts_require_approval=False)
    assert instincts.should_offer(0) is True


def test_portability_filter():
    ok = instincts._is_portable
    assert ok("when a task spans many files", "batch the edits in one script")
    assert not ok("when x", "edit src/foo/bar.py first")
    assert not ok("when x", "use C:\\data\\x")
    assert not ok("when x", "ship it on 2026-10-01")
    assert not ok("when x", "do a. then do b")
    assert not ok("when x", "line one\nline two")


def test_prompt_carries_the_quality_bar_and_one_proposal(store):
    p = instincts._build_extract_prompt("[user] hi", [{"id": "i1", "trigger": "when x", "status": "rejected"}],
                                        max_items=1)
    assert "when in doubt, save nothing" in p.lower()
    assert "ONE portable lesson" in p and "ONE" in p
    assert "[rejected]" in p
    assert "at most one block" in p
    p3 = instincts._build_extract_prompt("[user] hi", [], max_items=3)
    assert "up to 3" in p3


def _fake_model(monkeypatch, reply):
    monkeypatch.setattr("src.endpoint_resolver.resolve_endpoint", lambda *a, **k: ("http://x/v1", "m", {}))

    async def _call(*a, **k):
        return reply

    monkeypatch.setattr("src.llm_core.llm_call_async", _call)


_TWO = (
    "<instinct>\ntrigger: when a task spans many files\naction: batch the edits in one script\n"
    "domain: workflow\nevidence: \"batch the edits\"\ncontradicts: none\n</instinct>\n"
    "<instinct>\ntrigger: when tests fail on import\naction: check the interpreter first\n"
    "domain: debugging\nevidence: \"check the interpreter\"\ncontradicts: none\n</instinct>"
)
_CONV = [{"role": "assistant", "content": "I will batch the edits and check the interpreter first."}]


@pytest.mark.asyncio
async def test_extraction_proposes_one_lesson_by_default(store, monkeypatch):
    _settings(monkeypatch)
    _fake_model(monkeypatch, _TWO)
    out = await instincts.extract_from_turn("alice", "s1", _CONV, project="/repo",
                                            project_name="repo", workspace="/repo")
    assert len(out) == 1 and out[0]["status"] == "proposed"
    assert instincts.render_block("alice", "/repo") == ""


@pytest.mark.asyncio
async def test_extraction_drops_non_portable_proposals(store, monkeypatch):
    _settings(monkeypatch)
    _fake_model(monkeypatch, _TWO.replace("batch the edits in one script", "edit src/app/main.py in one script"))
    out = await instincts.extract_from_turn("alice", "s1", _CONV, project="/repo",
                                            project_name="repo", workspace="/repo")
    assert out == []


@pytest.mark.asyncio
async def test_extraction_without_approval_keeps_old_behaviour(store, monkeypatch):
    _settings(monkeypatch, instincts_require_approval=False)
    _fake_model(monkeypatch, _TWO)
    out = await instincts.extract_from_turn("alice", "s1", _CONV, project="/repo",
                                            project_name="repo", workspace="/repo")
    assert len(out) == 2 and all(o["status"] == "active" for o in out)


@pytest.mark.asyncio
async def test_extraction_skips_ideas_the_user_rejected(store, monkeypatch):
    _settings(monkeypatch)
    rec = _proposal()
    instincts.reject("alice", rec["id"])
    _fake_model(monkeypatch, _TWO)
    out = await instincts.extract_from_turn("alice", "s1", _CONV, project="/repo",
                                            project_name="repo", workspace="/repo")
    assert out == []        # only one slot per turn and the first idea was rejected


@pytest.mark.asyncio
async def test_tool_actions(store):
    from src.agent_tools.instinct_tools import do_manage_instincts
    rec = _proposal()
    listed = await do_manage_instincts(json.dumps({"action": "proposed"}), owner="alice")
    assert listed["count"] == 1 and rec["id"] in listed["results"]
    done = await do_manage_instincts(json.dumps({"action": "approve", "id": rec["id"], "do": "batch edits"}),
                                     owner="alice")
    assert done["results"]["status"] == "active" and done["results"]["action"] == "batch edits"
    rej = await do_manage_instincts(json.dumps({"action": "reject", "id": rec["id"]}), owner="alice")
    assert rej["results"]["status"] == "rejected"
    missing = await do_manage_instincts(json.dumps({"action": "approve", "id": "nope"}), owner="alice")
    assert missing["exit_code"] == 1


def test_routes_list_and_decide(store):
    from fastapi import FastAPI
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.testclient import TestClient
    from routes.instincts_routes import setup_instincts_routes

    app = FastAPI()
    app.include_router(setup_instincts_routes())

    class _Stamp(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.current_user = "alice"
            return await call_next(request)

    app.add_middleware(_Stamp)
    c = TestClient(app, raise_server_exceptions=False)
    a = _proposal()
    b = _proposal(trigger="when tests fail on import", action="check the interpreter first")

    assert c.get("/api/instincts").json()["count"] == 0
    r = c.get("/api/instincts/proposed").json()
    assert r["count"] == 2
    assert c.get("/api/instincts?status=proposed").json()["count"] == 2
    assert c.get("/api/instincts?status=bogus").status_code == 400
    assert c.get("/api/instincts/status").json()["proposed"] == 2

    ok = c.post(f"/api/instincts/{a['id']}/approve", json={"action": "batch edits in one script"})
    assert ok.status_code == 200 and ok.json()["status"] == "active"
    assert c.get("/api/instincts").json()["count"] == 1
    assert c.post(f"/api/instincts/{b['id']}/reject").json()["status"] == "rejected"
    assert c.post(f"/api/instincts/{b['id']}/edit", json={"action": "x" * 2}).status_code == 200
    assert c.post("/api/instincts/nope/approve").status_code == 404
    assert c.get("/api/instincts?status=all").json()["count"] == 2