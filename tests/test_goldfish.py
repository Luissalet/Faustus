"""Elephant and goldfish (src/goldfish.py): every flow with a scripted model, the pieces they are
made of (ranges, links, consolidation, edits), the decision gate, and the chat / HTTP / MCP surfaces."""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import constants as constants_mod
from src import goldfish as G
from src import tool_index, tool_schemas
from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS

DOC = "# Cache\n\nGoal: faster reads.\nThe cache is TBD.\n\n## Implementation Plan\n\n- edit cache.py\n"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path / "data"))
    yield


class Model:
    """A scripted model: answers by role, records what each call was shown."""

    def __init__(self, fix_round=1):
        self.calls = []
        self.fix_round = fix_round

    async def __call__(self, owner, run, messages, role="goldfish", model="", tag=""):
        system, user = messages[0]["content"], messages[-1]["content"]
        self.calls.append({"role": role, "model": model, "tag": tag, "system": system, "user": messages[1]["content"]})
        if "elephant: the author's side" in system:
            ids = [line.split("]")[0][1:] for line in user.split("Findings:\n")[1].splitlines() if line.startswith("[")]
            out = []
            for i in ids:
                if i.startswith(f"R{self.fix_round}-") or i.startswith("C"):
                    out.append({"id": i, "category": "PROPOSE FIX", "justification": "valid",
                                "edits": [{"find": "The cache is TBD.", "replace": "The cache is rebuilt every night."}]})
                else:
                    out.append({"id": i, "category": "CONTEST", "justification": "line 3 already says it"})
            return json.dumps({"decisions": out})
        if "Group findings" in system:
            return json.dumps({"groups": [["M1.1", "M2.1"], ["M1.2"]], "contradicted": []})
        if "DISPROVE" in system:
            return json.dumps({"survives": False, "confidence": "medium"})
        if "independent reviewer" in system:
            extra = [{"location": "line 1", "mechanism": "title typo", "severity": "P3"}] if model == "m1" else []
            return json.dumps({"findings": [{"location": "line 4", "mechanism": "cache never invalidated",
                                             "failing_case": "edit then read", "severity": "P1", "confidence": "high"}] + extra})
        if "Compare a brief" in system:
            return "No divergence."
        if "elephant: you hold the project context" in system:
            return "## Section\n\nText for the section.\n"
        if "concepts brief" in system:
            return "# Cache\n\n## Overview\nA cache."
        if "Comprehension" in system:
            return json.dumps({"paraphrase_proposal": "A cache.", "paraphrase_system": "Reads files.",
                               "findings": [{"title": "Undefined TBD", "severity": "major", "lines": "4", "description": "TBD is not defined"}]
                               if "TBD" in user else []})
        if "TBD" in user:
            return json.dumps({"findings": [{"title": "Placeholder left", "severity": "major", "lines": "4", "description": "TBD placeholder"}]})
        return json.dumps({"findings": []})


def run_flow(args, model):
    async def main():
        r = G.start("alice", args, ask=model)
        await G.wait(r["id"], 30)
        return G.get_run("alice", r["id"])
    return asyncio.run(main())


def test_pieces():
    r = G.split_ranges(DOC)
    assert r["feature"] == (1, 5) and r["detailed"] == (6, 8)
    assert G.split_ranges("# A\n\nno plan")["detailed"] is None
    assert G.numbered("a\nb", 2).strip() == "2 | b"
    merged = G.consolidate([
        {"id": "a1", "reviewer": "x", "title": "Undefined cache term", "severity": "nit", "lines": "4", "description": "cache term TBD undefined"},
        {"id": "b1", "reviewer": "y", "title": "Cache term undefined", "severity": "major", "lines": "3-5", "description": "the cache term TBD is undefined"},
        {"id": "c1", "reviewer": "z", "title": "Missing tests", "severity": "nit", "lines": "8", "description": "no test plan"}])
    assert [m["severity"] for m in merged] == ["major", "nit"] and merged[0]["sources"] == ["b1", "a1"]
    doc, applied = G.apply_edits("one two two", {"F1": {"category": "PROPOSE FIX", "edits": [{"find": "one", "replace": "1"}]},
                                                  "F2": {"category": "PROPOSE FIX", "edits": [{"find": "two", "replace": "2"}]},
                                                  "F3": {"category": "CONTEST", "edits": [{"find": "1", "replace": "x"}]}})
    assert doc == "1 two two" and applied == ["F1"]


def test_linked_files_stay_inside_the_folder(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "api.md").write_text("x", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("x", encoding="utf-8")
    doc = tmp_path / "docs" / "design.md"
    doc.write_text("[api](api.md#x) [up](../secret.txt) [web](https://example.com) [none](missing.md)", encoding="utf-8")
    assert G.linked_files(doc.read_text(), str(doc)) == [str((tmp_path / "docs" / "api.md").resolve())]


def test_review_flow_runs_the_five_stages_with_fresh_reviewers(tmp_path):
    path = tmp_path / "design.md"
    path.write_text(DOC, encoding="utf-8")
    ctx = tmp_path / "cache.py"
    ctx.write_text("CACHE = {}\n", encoding="utf-8")
    m = Model()
    run = run_flow({"flow": "review", "path": str(path), "context_files": [str(ctx)], "context": "SECRET DECISION"}, m)
    assert run["status"] == "done", run.get("error")
    assert [s["stage"] for s in run["stages_done"]] == [5, 6, 7, 8, 9]
    assert run["counts"]["major"] >= 1 and run["consolidated_file"] == "consolidated_review_report.md"
    goldfish_calls = [c for c in m.calls if c["role"] == "goldfish"]
    assert goldfish_calls and all("SECRET DECISION" not in c["user"] for c in goldfish_calls)   # the elephant's context never leaks
    comprehension = [c for c in goldfish_calls if "Comprehension" in c["system"]]
    assert comprehension and all("CACHE = {}" not in c["user"] for c in comprehension)        # stage 6 reads the document alone
    assert any("CACHE = {}" in c["user"] for c in goldfish_calls if "Codebase fit" in c["system"])
    report = G.read_artifact("alice", run["id"], "consolidated_review_report.md")
    assert "Traceability" in report and "Reviewer status" in report
    assert path.read_text(encoding="utf-8") == DOC   # a review alone changes nothing


def test_loop_fixes_settles_and_writes_back(tmp_path):
    path = tmp_path / "design.md"
    path.write_text(DOC, encoding="utf-8")
    run = run_flow({"flow": "loop", "path": str(path), "reviewers": ["comprehension", "design_critic"], "max_rounds": 4}, Model())
    assert run["status"] == "done", run.get("error")
    assert run["rounds"][0]["applied"] and run["written_back"] is True
    assert "rebuilt every night" in path.read_text(encoding="utf-8")
    assert run["outcome"] == "converged" and run["final_file"] == "doc_v1.md"
    assert [r["clean"] for r in run["rounds"]][-2:] == [True, True]


def test_loop_waits_for_a_decision_when_not_automatic(tmp_path):
    path = tmp_path / "design.md"
    path.write_text(DOC, encoding="utf-8")

    async def main():
        r = G.start("alice", {"flow": "loop", "path": str(path), "reviewers": ["design_critic"], "auto_apply": False,
                              "max_rounds": 2}, ask=Model())
        for _ in range(200):
            await asyncio.sleep(0.02)
            if G.get_run("alice", r["id"])["status"] == "awaiting_decision":
                break
        waiting = G.get_run("alice", r["id"])
        fid = waiting["rounds"][0]["findings"][0]["cid"]
        G.resolve("alice", r["id"], {"mode": "decisions", "decisions": {fid: "accept"}})
        for _ in range(300):
            await asyncio.sleep(0.02)
            cur = G.get_run("alice", r["id"])
            if cur["status"] == "awaiting_decision":
                G.resolve("alice", r["id"], {"mode": "continue"})
            if cur["status"] in ("done", "failed"):
                break
        return waiting, G.get_run("alice", r["id"])
    waiting, done = asyncio.run(main())
    assert waiting["status"] == "awaiting_decision" and path.read_text(encoding="utf-8") != DOC
    assert done["status"] == "done"
    with pytest.raises(G.GoldfishError):
        G.resolve("alice", done["id"], {"mode": "continue"})


def test_draft_brainstorm_panel_and_drift(tmp_path):
    out = tmp_path / "new.md"
    draft = run_flow({"flow": "draft", "brief": "A cache for reads", "save_to": str(out), "max_rounds": 2}, Model())
    assert draft["status"] == "done" and out.is_file() and "## Section" in out.read_text(encoding="utf-8")
    assert draft["reconciliation"] == "No divergence." and len(draft["rounds"]) >= 1
    brain = run_flow({"flow": "brainstorm", "brief": "a cache"}, Model())
    assert brain["status"] == "done" and "Overview" in G.read_artifact("alice", brain["id"], "concepts_brief.md")
    panel = run_flow({"flow": "panel", "text": DOC, "models": ["m1", "m2"]}, Model())
    assert panel["status"] == "done" and panel["outcome"] == "ACTED"
    assert [f["severity"] for f in panel["findings"]] == ["blocker"] and panel["findings"][0]["reviewers"] == ["m1", "m2"]
    single = next(c for c in panel["panel"]["candidates"] if c["bucket"] == "single")
    assert single["survives"] is False
    src = tmp_path / "cache.py"
    src.write_text("X = 1\n", encoding="utf-8")
    drift = run_flow({"flow": "drift", "text": DOC, "context_files": [str(src)]}, Model())
    assert drift["status"] == "done" and drift["consolidated_file"] == "drift_report.md"
    nodrift = run_flow({"flow": "drift", "text": DOC}, Model())
    assert nodrift["status"] == "failed" and "source files" in nodrift["error"]


def test_panel_with_a_model_that_is_down():
    class OneDown(Model):
        async def __call__(self, owner, run, messages, role="goldfish", model="", tag=""):
            if model == "m2":
                raise RuntimeError("503: the engine is not answering")
            return await super().__call__(owner, run, messages, role=role, model=model, tag=tag)

    model = OneDown()
    panel = run_flow({"flow": "panel", "text": DOC, "models": ["m1", "m2"]}, model)
    assert panel["status"] == "done", panel.get("error")
    statuses = panel["panel"]["statuses"]
    assert statuses["m1"] == "completed" and statuses["m2"].startswith("not run")
    proofs = [c for c in model.calls if c["tag"].startswith("proof-")]
    assert proofs and all(c["model"] == "m1" for c in proofs)


def test_bad_arguments():
    with pytest.raises(G.GoldfishError):
        G.start("alice", {"flow": "nope", "text": "x"}, background=False)
    with pytest.raises(G.GoldfishError):
        G.start("alice", {"flow": "loop"}, background=False)
    with pytest.raises(G.GoldfishError):
        G.start("alice", {"flow": "loop", "text": "x", "reviewers": ["ghost"]}, background=False)
    with pytest.raises(G.GoldfishError):
        G.start("alice", {"flow": "draft"}, background=False)
    with pytest.raises(G.GoldfishError):
        G.get_run("alice", "gf-20260101-000000-abcdef")


def test_registered_and_surfaces(monkeypatch, tmp_path):
    schemas = {s["function"]["name"] for s in tool_schemas.FUNCTION_TOOL_SCHEMAS}
    from src.tool_capabilities import TOOL_CAPABILITIES
    from src.tool_index_examples import EXAMPLES
    assert "goldfish_review" in TOOL_HANDLERS and "goldfish_review" in TOOL_TAGS and "goldfish_review" in schemas
    assert tool_index.BUILTIN_TOOL_DESCRIPTIONS["goldfish_review"] and EXAMPLES["goldfish_review"]
    assert TOOL_CAPABILITIES["goldfish_review"].effects
    roles = asyncio.run(TOOL_HANDLERS["goldfish_review"](json.dumps({"action": "roles"}), {"owner": "alice"}))
    assert roles["exit_code"] == 0 and "comprehension" in roles["output"]
    listed = asyncio.run(TOOL_HANDLERS["goldfish_review"](json.dumps({"action": "list"}), {"owner": "alice"}))
    assert listed["output"] == "no goldfish runs"

    import routes.goldfish_routes as gr
    monkeypatch.setattr(gr, "effective_user", lambda request: "alice")
    monkeypatch.setattr(gr, "require_admin", lambda request: None)
    app = FastAPI()
    app.include_router(gr.setup_goldfish_routes())
    client = TestClient(app)
    assert client.get("/api/goldfish").json() == {"runs": []}
    meta = client.get("/api/goldfish/meta").json()
    assert "zoom_out" in meta["reviewers"] and meta["presets"]["full"] == [5, 6, 7, 8, 9] and "models" in meta
    assert client.post("/api/goldfish", json={"flow": "loop"}).status_code == 400
    assert client.get("/api/goldfish/gf-20260101-000000-abcdef").status_code == 404

    pytest.importorskip("mcp")
    import mcp_servers.goldfish_server as srv
    assert [t.name for t in asyncio.run(srv.list_tools())] == ["goldfish_review"]
    monkeypatch.delenv("ODYSSEUS_MCP_GOLDFISH_OWNER", raising=False)
    assert "no owner configured" in asyncio.run(srv.call_tool("goldfish_review", {"action": "list"}))[0].text
    monkeypatch.setenv("ODYSSEUS_MCP_GOLDFISH_OWNER", "alice")
    assert asyncio.run(srv.call_tool("goldfish_review", {"action": "list"}))[0].text == "no goldfish runs"
    from src import builtin_mcp
    assert "goldfish" in builtin_mcp._BUILTIN_SERVERS and "goldfish" in builtin_mcp.NATIVE_TWIN_SERVERS
