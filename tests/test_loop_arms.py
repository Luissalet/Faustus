"""Experiment arms, the Loop Card plan and ledger, and the improvement loop (src/loop_arms.py,
src/loop_card.py): paired statistics and their gates, arms against the card's control, the
improvement loop end to end over real isolated alternatives, and the chat / HTTP / MCP surfaces."""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import constants as constants_mod
from src import loop_arms as la
from src import loop_card as lc
from src import tool_index, tool_schemas
from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS

REGEX = {"kind": "regex", "pattern": r"elapsed ([\d.]+) ms"}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path / "data"))
    yield


def by_command(table):
    """Runner whose metric depends on the command (and, for folders, on a file `speed.txt` in cwd)."""
    counts = {}

    def runner(command, cwd, timeout_s):
        key = command
        if cwd and os.path.isfile(os.path.join(cwd, "speed.txt")):
            key = open(os.path.join(cwd, "speed.txt"), encoding="utf-8").read().strip()
        seq = table[key]
        i = counts.get(key, 0)
        counts[key] = i + 1
        v = seq[i % len(seq)]
        if v is None:
            return 1, "boom", 0.01
        return 0, f"elapsed {v} ms\n", 0.01
    return runner


def samples(rows):
    return [{"round": r, "arm": a, "value": v, "valid": v is not None} for r, a, v in rows]


def test_arm_stats_keep_drop_inconclusive_and_minor():
    rows = []
    for r in range(1, 9):
        rows += [(r, "control", 100 + (r % 3)), (r, "aa", 100 + ((r + 1) % 3)), (r, "fast", 80 + (r % 2)),
                 (r, "slow", 110 + (r % 2)), (r, "same", 100 + ((r + 2) % 3))]
    s = la.arm_stats(samples(rows), "control", aa="aa", bar_pct=5, min_pairs=6)
    v = {a["arm"]: a["verdict"] for a in s["arms"]}
    assert v["fast"] == "KEEP" and v["slow"] == "DROP" and v["same"] in ("INCONCLUSIVE", "DROP")
    assert s["aa"]["noise_floor"] <= 2
    fast = next(a for a in s["arms"] if a["arm"] == "fast")
    assert fast["wins"] == 8 and fast["ci_low"] > 0 and fast["delta_pct"] > 15
    # a small but real gain that misses a 30 % bar is MINOR, not KEEP
    s2 = la.arm_stats(samples(rows), "control", bar_pct=30, min_pairs=6)
    assert {a["arm"]: a["verdict"] for a in s2["arms"]}["fast"] == "MINOR"
    # too few pairs: no verdict either way
    s3 = la.arm_stats(samples([r for r in rows if r[0] <= 3]), "control", min_pairs=6)
    assert all(a["verdict"] == "INCONCLUSIVE" for a in s3["arms"])


def test_arm_stats_reliability_gate_and_higher_is_better():
    rows = []
    for r in range(1, 8):
        rows += [(r, "control", 50), (r, "flaky", None if r <= 3 else 70), (r, "better", 60 + r % 2)]
    s = la.arm_stats(samples(rows), "control", higher_is_better=True, min_pairs=4)
    v = {a["arm"]: a for a in s["arms"]}
    assert v["flaky"]["verdict"] == "DROP" and "failed 3" in v["flaky"]["why"]
    assert v["better"]["verdict"] == "KEEP"
    assert la.arm_stats(samples([(1, "x", 1)]), "control").get("error")


def make_card(runner, **over):
    return lc.create("alice", {"goal": "faster", "command": "base", "extract": REGEX, "direction": "down", "repeats": 4,
                               "project": "p1", **over}, runner)


def test_plan_keep_bar_check_and_ledger():
    runner = by_command({"base": [100, 101, 99, 100], "faster": [90, 91, 89, 90], "bit": [98, 98.5, 97.5, 98]})
    card = make_card(runner, keep_bar_pct=5, min_pairs=4, guard_command="tests", stop_rule="stop after 3 tries")
    assert card["keep_bar"] == {"pct": 5.0, "abs": 0.0} and card["min_pairs"] == 4 and card["ledger"][0]["verdict"] == "baseline"
    chk = lc.check("alice", card["id"], command="faster", runner=runner, change="cache the parser")
    assert chk["verdict"] == "win" and chk["keep_bar_met"] is True
    small = lc.check("alice", card["id"], command="bit", runner=runner)
    assert small["keep_bar_met"] is False or small["verdict"] != "win"
    full = lc.get("alice", card["id"])
    assert [r["source"] for r in full["ledger"]] == ["baseline", "check", "check"]
    assert full["ledger"][1]["change"] == "cache the parser"
    moved = lc.update_plan("alice", card["id"], {"keep_bar_pct": 2, "reason": "the bar was too strict"})
    assert moved["keep_bar"]["pct"] == 2 and moved["ledger"][-1]["source"] == "plan"
    assert "too strict" in moved["ledger"][-1]["why"]
    text = lc.render(lc.get("alice", card["id"]))
    assert "keep bar (pre-registered)" in text and "guard: tests" in text
    assert lc.run("alice", {"action": "plan", "id": card["id"]}).get("error")


def test_run_arms_against_the_control_records_every_arm():
    runner = by_command({"base": [100, 102, 98, 101, 99, 100], "fast": [80, 81, 79, 80, 82, 80],
                         "slow": [120, 121, 119, 120, 122, 120], "broken": [None]})
    card = make_card(runner, keep_bar_pct=5)
    out = la.run_arms("alice", {"id": card["id"], "rounds": 6, "arms": [
        {"name": "fast", "command": "fast", "label": "precompile"}, {"name": "slow", "command": "slow"},
        {"name": "broken", "command": "broken"}]}, runner)
    v = {a["arm"]: a["verdict"] for a in out["stats"]["arms"]}
    assert v == {"broken": "DROP", "fast": "KEEP", "slow": "DROP"} and out["keep"] == ["fast"]
    assert out["stats"]["aa"]["pairs"] == 6
    ledger = lc.get("alice", card["id"])["ledger"]
    assert {r["arm"] for r in ledger if r["source"] == "arms"} == {"fast", "slow", "broken"}
    assert next(r for r in ledger if r["arm"] == "fast")["change"] == "precompile"
    assert "KEEP" in la.render_arms(out)
    with pytest.raises(lc.LoopError):
        la.run_arms("alice", {"id": card["id"], "arms": [{"name": "same"}]}, runner)
    with pytest.raises(lc.LoopError):
        la.run_arms("alice", {"id": card["id"], "arms": []}, runner)


def test_guard_command_drops_an_arm(tmp_path):
    good, bad = tmp_path / "good", tmp_path / "bad"
    good.mkdir(); bad.mkdir()
    (good / "speed.txt").write_text("quick", encoding="utf-8")
    (bad / "speed.txt").write_text("quick", encoding="utf-8")
    table = {"base": [100, 101, 99], "quick": [70, 71, 69]}
    inner = by_command(table)

    def runner(command, cwd, timeout_s):
        if command == "guard":
            return (1, "test failed", 0.01) if cwd == str(bad) else (0, "ok", 0.01)
        return inner(command, cwd, timeout_s)
    card = make_card(runner)
    out = la.run_arms("alice", {"id": card["id"], "rounds": 4, "min_pairs": 3, "guard_command": "guard",
                                "arms": [{"name": "good", "cwd": str(good)}, {"name": "bad", "cwd": str(bad)}]}, runner)
    v = {a["arm"]: a for a in out["stats"]["arms"]}
    assert v["good"]["verdict"] == "KEEP" and v["bad"]["verdict"] == "DROP" and "guard" in v["bad"]["why"]


def test_parsers_for_model_replies():
    hyps = la.parse_hypotheses('```json\n["Cache the compiled regex", "Use a set for lookups", "x"]\n```', 3)
    assert hyps == ["Cache the compiled regex", "Use a set for lookups"]
    assert la.parse_hypotheses("1. Precompute the table once\n2. Avoid repeated string concatenation", 5)[1].startswith("Avoid")
    hyp, files = la.parse_proposal("HYPOTHESIS: cache it\n=== FILE: pkg/slow.py ===\n```python\nprint(1)\n```\n=== END ===\n"
                                   "=== FILE: other.py ===\nx\n=== END ===", ["pkg/slow.py"])
    assert hyp == "cache it" and files == {"pkg/slow.py": "print(1)\n"}


def test_improvement_loop_end_to_end(tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()
    (ws / "speed.txt").write_text("v0", encoding="utf-8")
    (ws / "slow.py").write_text("# original\n", encoding="utf-8")
    table = {"v0": [100, 101, 99, 100], "v1": [60, 61, 59, 60], "v2": [130, 131, 129, 130]}
    runner = by_command(table)
    card = make_card(runner, cwd=str(ws), keep_bar_pct=10)

    async def ask(owner, job, messages, run_id):
        system = messages[0]["content"]
        if "plan performance" in system:
            assert "slow.py" in messages[1]["content"]
            return '["Precompute the lookup table", "Unroll the inner loop"]'
        target = "v1" if "Precompute" in messages[1]["content"] else "v2"
        # a proposal rewrites slow.py; the fake runner reads speed.txt, so the proposal changes both
        return f"HYPOTHESIS: x\n=== FILE: slow.py ===\n# {target}\n=== END ===\n=== FILE: speed.txt ===\n{target}\n=== END ==="

    async def main():
        job = la.start_improve("alice", {"id": card["id"], "files": ["slow.py", "speed.txt"], "proposals": 2, "rounds": 4,
                                         "min_pairs": 4}, ask=ask, runner=runner)
        await la.wait(job["id"], 60)
        return la.get_job("alice", job["id"])
    job = asyncio.run(main())
    assert job["status"] == "done", job.get("error")
    entry = job["history"][0]
    verdicts = {a["arm"]: a["verdict"] for a in entry["stats"]["arms"]}
    assert verdicts == {"h1": "KEEP", "h2": "DROP"}
    assert job["winner"]["hypothesis"].startswith("Precompute") and job["applied"] is True
    assert (ws / "speed.txt").read_text(encoding="utf-8").strip() == "v1"
    assert (ws / "slow.py").read_text(encoding="utf-8") == "# v1\n"
    assert entry["verify"]["verdict"] == "win" and entry.get("experiment_deleted") is True
    card_now = lc.get("alice", card["id"])
    assert card_now["baseline"]["mean"] == pytest.approx(60, abs=1)   # rebased on the new code
    assert {r["verdict"] for r in card_now["ledger"] if r["source"].startswith("improve")} == {"KEEP", "DROP"}
    assert la.list_jobs("alice", card["id"])[0]["id"] == job["id"]
    assert "KEEP" in la.render_job(job)
    assert la.delete_job("alice", job["id"]) == {"deleted": job["id"]}


def test_improvement_loop_refuses_bad_input(tmp_path):
    runner = by_command({"base": [1, 1, 1]})
    card = make_card(runner)
    with pytest.raises(lc.LoopError):
        la.start_improve("alice", {"id": card["id"], "files": ["x.py"]}, background=False)   # no folder on the card
    ws = tmp_path / "w"
    ws.mkdir()
    card2 = make_card(runner, cwd=str(ws))
    for files in ([], ["missing.py"], ["../outside.py"]):
        with pytest.raises(lc.LoopError):
            la.start_improve("alice", {"id": card2["id"], "files": files}, background=False)


def test_tools_registered_everywhere():
    names = ["loop_card_plan", "loop_arms", "loop_improve", "prompt_mirror"]
    schemas = {s["function"]["name"]: s["function"] for s in tool_schemas.FUNCTION_TOOL_SCHEMAS}
    from src.tool_capabilities import TOOL_CAPABILITIES
    from src.tool_index_examples import EXAMPLES
    from src.tool_security import NON_ADMIN_BLOCKED_TOOLS
    for n in names:
        assert n in TOOL_HANDLERS and n in TOOL_TAGS and n in schemas and n in NON_ADMIN_BLOCKED_TOOLS
        assert n in tool_index.BUILTIN_TOOL_DESCRIPTIONS and EXAMPLES[n] and TOOL_CAPABILITIES[n].effects
    assert "keep_bar_pct" in schemas["loop_card_create"]["parameters"]["properties"]


def test_chat_tools(tmp_path):
    cmd = f'"{sys.executable}" -c "print(\'elapsed 5 ms\')"'
    ctx = {"owner": "alice", "project_id": "proj"}
    out = asyncio.run(TOOL_HANDLERS["loop_card_create"](json.dumps(
        {"goal": "g", "command": cmd, "extract": REGEX, "repeats": 2, "cwd": str(tmp_path)}), ctx))
    cid = out["result"]["id"]
    plan = asyncio.run(TOOL_HANDLERS["loop_card_plan"](json.dumps({"id": cid, "keep_bar_pct": 3}), ctx))
    assert plan["exit_code"] == 0 and plan["result"]["keep_bar"]["pct"] == 3
    other = f'"{sys.executable}" -c "print(\'elapsed 9 ms\')"'
    arms = asyncio.run(TOOL_HANDLERS["loop_arms"](json.dumps({"id": cid, "rounds": 2, "min_pairs": 2,
                                                              "arms": [{"name": "nine", "command": other}]}), ctx))
    assert arms["exit_code"] == 0 and "nine: DROP" in arms["output"]
    listed = asyncio.run(TOOL_HANDLERS["loop_improve"](json.dumps({"action": "list", "id": cid}), ctx))
    assert listed["exit_code"] == 0
    bad = asyncio.run(TOOL_HANDLERS["loop_improve"](json.dumps({"action": "status", "job": "nope"}), ctx))
    assert bad["exit_code"] == 1


@pytest.fixture()
def client(monkeypatch):
    import routes.loop_card_routes as lr
    monkeypatch.setattr(lr, "effective_user", lambda request: "alice")
    monkeypatch.setattr(lr, "require_admin", lambda request: None)
    app = FastAPI()
    app.include_router(lr.setup_loop_card_routes())
    return TestClient(app)


def test_http_routes(client, tmp_path):
    cmd = f'"{sys.executable}" -c "print(\'elapsed 9 ms\')"'
    cid = client.post("/api/loop-cards", json={"goal": "g", "command": cmd, "extract": REGEX, "repeats": 2}).json()["id"]
    assert client.post(f"/api/loop-cards/{cid}/plan", json={"keep_bar_pct": 4}).json()["keep_bar"]["pct"] == 4
    other = f'"{sys.executable}" -c "print(\'elapsed 3 ms\')"'
    r = client.post(f"/api/loop-cards/{cid}/arms", json={"rounds": 2, "min_pairs": 2, "arms": [{"name": "three", "command": other}]})
    assert r.status_code == 200 and r.json()["stats"]["arms"][0]["verdict"] == "KEEP"
    assert client.get(f"/api/loop-cards/{cid}").json()["ledger"][-1]["arm"] == "three"
    assert client.get("/api/loop-improve").json() == {"runs": []}
    assert client.post("/api/loop-improve", json={"id": cid, "files": ["a.py"]}).status_code == 400
    assert client.get("/api/loop-improve/imp-20260101-000000-abcdef").status_code == 404


def test_mcp_server_lists_and_runs_the_new_tools(monkeypatch):
    pytest.importorskip("mcp")
    import mcp_servers.loop_cards_server as srv
    names = [t.name for t in asyncio.run(srv.list_tools())]
    assert names[-4:] == ["loop_card_plan", "loop_arms", "loop_improve", "prompt_mirror"]
    monkeypatch.setenv("ODYSSEUS_MCP_LOOP_CARDS_OWNER", "alice")
    cmd = f'"{sys.executable}" -c "print(\'elapsed 3 ms\')"'
    cid = json.loads(asyncio.run(srv.call_tool("loop_card_create", {"goal": "g", "command": cmd, "extract": "elapsed (\\d+) ms",
                                                                    "repeats": 2}))[0].text)["id"]
    res = json.loads(asyncio.run(srv.call_tool("loop_card_plan", {"id": cid, "keep_bar_abs": 1}))[0].text)
    assert res["keep_bar"]["abs"] == 1
    listed = json.loads(asyncio.run(srv.call_tool("loop_improve", {"action": "list"}))[0].text)
    assert listed == {"runs": []}
