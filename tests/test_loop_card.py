"""Loop Card (src/loop_card.py): baseline statistics, threshold, win/loss/noise verdicts, the
three ways of reading the metric, and the chat / HTTP / MCP surfaces over one implementation."""
from __future__ import annotations

import asyncio
import json
import random
import statistics
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import constants as constants_mod
from src import loop_card as lc
from src import tool_index, tool_schemas
from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path / "data"))
    yield


def scripted(values, seconds=0.01):
    """A runner that prints `value ms` for each successive run, then keeps repeating the last."""
    state = {"i": 0}

    def runner(command, cwd, timeout_s):
        v = values[min(state["i"], len(values) - 1)]
        state["i"] += 1
        return 0, f"warmup done\nelapsed {v} ms\n", seconds
    return runner


REGEX = {"kind": "regex", "pattern": r"elapsed ([\d.]+) ms"}


def make(runner, **over):
    args = {"goal": "faster parse", "command": "bench", "extract": REGEX, "direction": "down", "repeats": 5, "project": "p1", **over}
    return lc.create("alice", args, runner)


def test_statistics_and_threshold():
    vals = [100.0, 102.0, 98.0, 101.0, 99.0]
    card = make(scripted(vals))
    b = card["baseline"]
    assert b["n"] == 5 and b["mean"] == pytest.approx(100.0) and b["median"] == 100.0
    assert b["stdev"] == pytest.approx(statistics.stdev(vals))
    assert b["min"] == 98.0 and b["max"] == 102.0
    assert b["cv"] == pytest.approx(statistics.stdev(vals) / 100.0)
    # larger of 2 x sd (~3.16) and 1 % of the mean (1.0)
    assert b["threshold"] == pytest.approx(2 * statistics.stdev(vals))
    quiet = make(scripted([200.0] * 5))
    assert quiet["baseline"]["stdev"] == 0 and quiet["baseline"]["threshold"] == pytest.approx(2.0)   # 1 % of 200
    assert any("never varied" in w for w in quiet["warnings"])


def test_check_win_loss_and_noise_for_lower_is_better():
    card = make(scripted([100.0, 102.0, 98.0, 101.0, 99.0]))
    cid = card["id"]
    win = lc.check("alice", cid, runner=scripted([90.0, 91.0, 89.0, 90.5, 89.5]))
    assert win["verdict"] == "win" and win["delta"] == pytest.approx(-10.0) and win["better_by"] == pytest.approx(10.0)
    assert win["noise_multiple"] > 3
    loss = lc.check("alice", cid, runner=scripted([110.0, 111.0, 109.0, 110.0, 110.0]))
    assert loss["verdict"] == "loss"
    noise = lc.check("alice", cid, runner=scripted([100.5, 101.0, 99.5, 100.0, 100.0]))
    assert noise["verdict"] == "noise"
    assert lc.get("alice", cid)["checks"][-1]["verdict"] == "noise" and len(lc.get("alice", cid)["checks"]) == 3


def test_direction_up_flips_the_verdict():
    card = make(scripted([0.80, 0.81, 0.79, 0.80, 0.80]), direction="up", goal="accuracy")
    assert lc.check("alice", card["id"], runner=scripted([0.90] * 5))["verdict"] == "win"
    assert lc.check("alice", card["id"], runner=scripted([0.70] * 5))["verdict"] == "loss"
    assert lc.normalise_direction("subir") == "up" and lc.normalise_direction("bajar") == "down"
    with pytest.raises(lc.LoopError):
        lc.normalise_direction("sideways")


def test_seeded_noise_is_measured_not_guessed():
    rng = random.Random(7)
    base = [100 + rng.gauss(0, 5) for _ in range(5)]
    card = make(scripted(base))
    sd = statistics.stdev(base)
    assert card["baseline"]["threshold"] == pytest.approx(max(2 * sd, 0.01 * statistics.fmean(base)))
    assert card["baseline"]["cv"] > 0.01
    # a shift smaller than the threshold is noise even though the mean moved
    shifted = [v - 0.5 * card["baseline"]["threshold"] for v in base]
    assert lc.check("alice", card["id"], runner=scripted(shifted))["verdict"] == "noise"


def test_rebase_adopts_the_new_measurement():
    card = make(scripted([100.0] * 5))
    res = lc.check("alice", card["id"], runner=scripted([50.0] * 5), rebase=True)
    assert res["verdict"] == "win" and res["rebased"] is True
    assert lc.get("alice", card["id"])["baseline"]["mean"] == 50.0


def test_real_commands_regex_json_and_time(tmp_path):
    py = f'"{sys.executable}"'
    c = lc.create("alice", {"goal": "g", "command": f'{py} -c "print(\'elapsed 12.5 ms\'); print(\'elapsed 7.5 ms\')"',
                            "extract": REGEX, "repeats": 3, "project": "p1"})
    assert c["baseline"]["mean"] == 7.5 and c["baseline"]["stdev"] == 0          # the last match counts
    j = lc.create("alice", {"goal": "g", "command": f'{py} -c "import json; print(json.dumps({{\'stats\': {{\'p95\': 42}}}}))"',
                            "extract": {"kind": "json", "path": "stats.p95"}, "repeats": 2, "project": "p1"})
    assert j["baseline"]["mean"] == 42
    t = lc.create("alice", {"goal": "g", "command": f'{py} -c "import time; time.sleep(0.05)"', "extract": "time", "repeats": 2,
                            "project": "p1", "cwd": str(tmp_path)})
    assert 0.04 < t["baseline"]["mean"] < 5


def test_errors_are_specific():
    with pytest.raises(lc.LoopError, match="matched nothing"):
        make(scripted([1]), extract={"kind": "regex", "pattern": r"nothing here (\d+)"})
    with pytest.raises(lc.LoopError, match="capture group"):
        make(scripted([1]), extract={"kind": "regex", "pattern": r"elapsed \d+"})
    with pytest.raises(lc.LoopError, match="exited with code 3"):
        make(lambda c, d, t: (3, "boom", 0.1))
    with pytest.raises(lc.LoopError, match="not JSON"):
        make(scripted([1]), extract={"kind": "json", "path": "a.b"})
    with pytest.raises(lc.LoopError, match="`goal` is required"):
        lc.create("alice", {"command": "x", "extract": "time"})
    assert lc.run("alice", {"action": "get", "id": "20260101-000000-abcdef"})["error_class"] == "loop.not_found"
    assert lc.run("alice", {"action": "nope"})["error_class"] == "loop.bad_args"


def test_list_get_delete_and_project_scoping():
    a = make(scripted([10.0] * 5), project="p1")
    b = make(scripted([20.0] * 5), project="p2")
    assert {r["id"] for r in lc.list_cards("alice")} == {a["id"], b["id"]}
    assert [r["id"] for r in lc.list_cards("alice", "p2")] == [b["id"]]
    assert lc.list_cards("bob") == []
    row = lc.list_cards("alice", "p1")[0]
    assert row["baseline"]["mean"] == 10.0 and row["last_check"] is None
    lc.check("alice", a["id"], runner=scripted([10.0] * 5))
    assert lc.list_cards("alice", "p1")[0]["last_check"]["verdict"] == "noise"
    lc.delete("alice", a["id"])
    assert [r["id"] for r in lc.list_cards("alice")] == [b["id"]]


def test_tools_are_registered_everywhere_the_catalogue_looks():
    names = ["loop_card_create", "loop_card_check", "loop_card_list", "loop_card_get", "loop_card_delete"]
    schemas = {s["function"]["name"] for s in tool_schemas.FUNCTION_TOOL_SCHEMAS}
    from src.tool_index_examples import EXAMPLES
    from src.tool_capabilities import TOOL_CAPABILITIES
    for n in names:
        assert n in TOOL_HANDLERS and n in TOOL_TAGS and n in schemas
        assert n in tool_index.BUILTIN_TOOL_DESCRIPTIONS and EXAMPLES[n] and TOOL_CAPABILITIES[n].effects


def test_chat_tools_round_trip():
    cmd = f'"{sys.executable}" -c "print(\'elapsed 5 ms\')"'
    out = asyncio.run(TOOL_HANDLERS["loop_card_create"](json.dumps(
        {"goal": "g", "command": cmd, "extract": {"kind": "regex", "pattern": "elapsed (\\d+) ms"}, "repeats": 2}), {"owner": "alice", "project_id": "proj"}))
    assert out["exit_code"] == 0 and "improvement threshold" in out["output"]
    cid = out["result"]["id"]
    listed = asyncio.run(TOOL_HANDLERS["loop_card_list"]("{}", {"owner": "alice", "project_id": "proj"}))
    assert cid in listed["output"]
    chk = asyncio.run(TOOL_HANDLERS["loop_card_check"](json.dumps({"id": cid}), {"owner": "alice", "project_id": "proj"}))
    assert chk["exit_code"] == 0 and "within the noise" in chk["output"]
    bad = asyncio.run(TOOL_HANDLERS["loop_card_check"](json.dumps({"id": "nope"}), {"owner": "alice"}))
    assert bad["exit_code"] == 1
    gone = asyncio.run(TOOL_HANDLERS["loop_card_delete"](json.dumps({"id": cid}), {"owner": "alice", "project_id": "proj"}))
    assert gone["exit_code"] == 0


@pytest.fixture()
def client(monkeypatch):
    import routes.loop_card_routes as lr
    monkeypatch.setattr(lr, "effective_user", lambda request: "alice")
    monkeypatch.setattr(lr, "require_admin", lambda request: None)
    app = FastAPI()
    app.include_router(lr.setup_loop_card_routes())
    return TestClient(app)


def test_http_routes(client):
    cmd = f'"{sys.executable}" -c "print(\'elapsed 9 ms\')"'
    r = client.post("/api/loop-cards", json={"goal": "g", "command": cmd, "extract": {"kind": "regex", "pattern": "elapsed (\\d+) ms"},
                                             "repeats": 2, "project": "p"})
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    assert client.get("/api/loop-cards", params={"project": "p"}).json()["cards"][0]["id"] == cid
    assert client.get(f"/api/loop-cards/{cid}").json()["baseline"]["mean"] == 9
    chk = client.post(f"/api/loop-cards/{cid}/check", json={})
    assert chk.status_code == 200 and chk.json()["verdict"] == "noise"
    assert client.post("/api/loop-cards", json={"goal": "g"}).status_code == 400
    assert client.get("/api/loop-cards/20260101-000000-abcdef").status_code == 404
    assert client.delete(f"/api/loop-cards/{cid}").json()["deleted"] == cid


def test_mcp_server_and_builtin_registration(monkeypatch):
    pytest.importorskip("mcp")
    import mcp_servers.loop_cards_server as srv
    tools = asyncio.run(srv.list_tools())
    assert [t.name for t in tools] == ["loop_card_create", "loop_card_check", "loop_card_list", "loop_card_get", "loop_card_delete",
                                       "loop_card_plan", "loop_arms", "loop_improve", "prompt_mirror"]
    monkeypatch.delenv("ODYSSEUS_MCP_LOOP_CARDS_OWNER", raising=False)
    monkeypatch.delenv("ODYSSEUS_LOOP_CARDS_OWNER", raising=False)
    assert "no owner configured" in asyncio.run(srv.call_tool("loop_card_list", {}))[0].text
    monkeypatch.setenv("ODYSSEUS_MCP_LOOP_CARDS_OWNER", "alice")
    cmd = f'"{sys.executable}" -c "print(\'elapsed 3 ms\')"'
    res = asyncio.run(srv.call_tool("loop_card_create", {"goal": "g", "command": cmd, "extract": "elapsed (\\d+) ms", "repeats": 2}))
    assert json.loads(res[0].text)["baseline"]["mean"] == 3
    from src import builtin_mcp
    assert "loop_cards" in builtin_mcp._BUILTIN_SERVERS and "loop_cards" in builtin_mcp.NATIVE_TWIN_SERVERS
