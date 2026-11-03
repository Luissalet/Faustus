"""Real execution of a learned recipe on a second case (src/recipe_execution.py).

Everything here runs for real through the normal tool dispatcher
(code_mode.bridge.dispatch_call -> execute_tool_block): write_file,
process_start (an asynchronous process with a handle that is waited on through
process_read) and read_file, in a temporary workspace with made-up data and the
isolated test data folder. No model is loaded.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from src import constants as constants_mod
from src import derivation_ops as ops
from src import recipe_execution as rex
from src import recipe_learning as rl
from src import recipes as recipes_store

OWNER = "admin"
REQUEST = "Genera el informe de ventas_norte.csv en informe_norte.md"
SCRIPT = (
    "import csv, os, sys\n"
    "src, dst = sys.argv[1], sys.argv[2]\n"
    "if not os.path.exists(src):\n"
    "    sys.stderr.write('missing ' + src + '\\n'); sys.exit(2)\n"
    "rows = list(csv.DictReader(open(src)))\n"
    "open('effects.log', 'a').write('ran ' + src + '\\n')\n"
    "out = ['| region | total |', '|---|---|'] + ['| %s | %s |' % (r['region'], r['total']) for r in rows]\n"
    "out += ['', 'Total: %s' % sum(float(r['total']) for r in rows)]\n"
    "open(dst, 'w').write('\\n'.join(out) + '\\n')\n"
)
STEPS_A = [
    ("write_file", {"path": "report_tool.py", "content": SCRIPT}),
    ("process_start", {"command": "sleep 1; python3 report_tool.py ventas_norte.csv informe_norte.md", "yield_ms": 100}),
    ("read_file", {"path": "informe_norte.md"}),
]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AUTH_ENABLED", "false")      # single-user profile: the owner may use file/process tools
    yield


@pytest.fixture()
def ws(tmp_path):
    w = tmp_path / "ws"
    w.mkdir()
    (w / "ventas_norte.csv").write_text("region,total\nnorte,10\nnoreste,5\n", encoding="utf-8")
    (w / "ventas_sur.csv").write_text("region,total\nsur,7\nsureste,3\nsuroeste,2.5\n", encoding="utf-8")
    return w


def _learn_from_case_a(ws) -> recipes_store.Recipe:
    """Case A done with normal tools through the real dispatcher, then learned
    from its events (the shape stored tool events have)."""
    executor = rex.make_dispatch_executor(OWNER, workspace=str(ws))
    events = []
    for i, (tool, args) in enumerate(STEPS_A):
        res = executor(tool, args, f"case-a-{i}")
        if tool == "process_start":
            status, _, wait = rex.classify(tool, res)
            assert status == "pending" and wait          # really asynchronous
            res, finished = rex._wait(executor, wait, "case-a", 30)
            assert finished
        assert rex.classify(tool, res)[0] == "ok", res
        events.append({"tool": tool, "args": args, "exit_code": res.get("exit_code", 0)})
    assert (ws / "informe_norte.md").read_text(encoding="utf-8").strip().endswith("Total: 15.0")
    trace = rl.trace_from_events(REQUEST, events, title="Informe de ventas")
    return rl.propose_from_trace(trace, OWNER, title="Informe de ventas")


def _names(recipe):
    by_example = {p["example"]: p["name"] for p in recipe.params}
    return by_example["ventas_norte.csv"], by_example["informe_norte.md"]


def _criteria(src, dst):
    return [
        {"kind": "file_exists", "path": "{{%s}}" % dst},
        {"kind": "row_count", "path": "{{%s}}" % dst, "equals": {"rows_of": "{{%s}}" % src}},
        {"kind": "columns", "path": "{{%s}}" % dst, "equals": {"columns_of": "{{%s}}" % src}},
        {"kind": "number", "path": "{{%s}}" % dst, "pattern": r"^Total: ([0-9.]+)$",
         "equals": {"csv_sum": {"path": "{{%s}}" % src, "column": "total"}}},
    ]


def _chat(args):
    from src.agent_tools import TOOL_HANDLERS
    return asyncio.run(TOOL_HANDLERS["recipe_learn"](json.dumps(args), {"owner": OWNER}))


def _execute(recipe, ws, params, **kw):
    args = {"action": "execute", "recipe_id": recipe.id, "params": params, "workspace": str(ws), **kw}
    return _chat(args)


# ---------------------------------------------------------------------------

def test_case_b_runs_for_real_and_the_delivered_report_is_checked(ws):
    recipe = _learn_from_case_a(ws)
    src, dst = _names(recipe)
    case_b = {src: "ventas_sur.csv", dst: "informe_sur.md"}

    dry = _chat({"action": "try", "recipe_id": recipe.id, "params": case_b})
    assert dry["executed"] is False and dry["mode"] == "dry_run" and dry["kind"] == "structural_dry_run"
    assert dry["verified"] is False and not (ws / "informe_sur.md").exists()

    out = _execute(recipe, ws, case_b, criteria=_criteria(src, dst))
    assert out["executed"] is True and out["mode"] == "execute"
    assert out["status"] == "verified" and out["verified"] is True, out.get("why_unverified")
    assert [s["status"] for s in out["steps"]] == ["ok", "ok", "ok"]
    proc = out["steps"][1]
    assert proc["tool"] == "process_start" and proc["waited_for"].startswith("ph_") and proc["ids"]["handle"]
    assert all(s["idempotency_key"] for s in out["steps"])
    # the independent check opened the report and compared it with the source table
    by_kind = {c["kind"]: c for c in out["criteria"]}
    assert by_kind["row_count"]["observed"] == by_kind["row_count"]["expected"] == 3
    assert by_kind["columns"]["observed"] == ["region", "total"]
    assert by_kind["number"]["observed"] == by_kind["number"]["expected"] == 12.5
    report = (ws / "informe_sur.md").read_text(encoding="utf-8")
    assert "| suroeste | 2.5 |" in report and "norte" not in report
    assert (ws / "effects.log").read_text(encoding="utf-8").splitlines() == ["ran ventas_norte.csv", "ran ventas_sur.csv"]
    stored = recipes_store.get_recipe(recipe.id, OWNER)
    assert stored.exit_criteria == _criteria(src, dst) and stored.executions[-1]["verified"] is True


def test_a_failing_step_is_detected_and_nothing_after_it_runs(ws):
    recipe = _learn_from_case_a(ws)
    src, dst = _names(recipe)
    out = _execute(recipe, ws, {src: "ventas_oeste.csv", dst: "informe_oeste.md"}, criteria=_criteria(src, dst))
    assert out["verified"] is False and out["status"] == "step_failed"
    assert [s["status"] for s in out["steps"]] == ["ok", "error", "skipped"]
    assert "exited with code 2" in out["steps"][1]["detail"]
    assert not (ws / "informe_oeste.md").exists()
    assert any("step 2" in w for w in out["why_unverified"])
    # the criteria are still evaluated and shown, and they fail too
    assert out["criteria"] and not any(c["passed"] for c in out["criteria"])


def test_a_wrong_deliverable_fails_its_criterion(ws):
    recipe = _learn_from_case_a(ws)
    src, dst = _names(recipe)
    bad = _criteria(src, dst) + [{"kind": "row_count", "path": "{{%s}}" % dst, "equals": 99}]
    out = _execute(recipe, ws, {src: "ventas_sur.csv", dst: "informe_sur.md"}, criteria=bad)
    assert all(s["status"] == "ok" for s in out["steps"])
    assert out["status"] == "criteria_failed" and out["verified"] is False
    assert any("expected 99, observed 3" in w for w in out["why_unverified"])


def test_no_criteria_means_executed_but_not_verified(ws):
    recipe = _learn_from_case_a(ws)
    src, dst = _names(recipe)
    out = _execute(recipe, ws, {src: "ventas_sur.csv", dst: "informe_sur.md"})
    assert out["executed"] is True and out["verified"] is False and out["status"] == "executed_unverified"


def test_a_job_still_running_is_not_counted_as_done(ws):
    steps = [{"tool": "process_start", "args": {"command": "sleep 3; echo hecho > salida_a.txt", "yield_ms": 50}}]
    recipe = rl.propose(OWNER, request="crea salida_a.txt", steps=steps, title="Lento")
    (name,) = [p["name"] for p in recipe.params]
    out = _execute(recipe, ws, {name: "salida_b.txt"}, job_timeout=1,
                   criteria=[{"kind": "contains", "path": "{{%s}}" % name, "text": "hecho"}])
    assert out["status"] == "incomplete" and out["verified"] is False
    assert out["steps"][0]["status"] == "queued" and "still running" in out["steps"][0]["detail"]


def test_resume_does_not_repeat_completed_effects(ws):
    recipe = _learn_from_case_a(ws)
    src, dst = _names(recipe)
    case = {src: "ventas_este.csv", dst: "informe_este.md"}
    first = _execute(recipe, ws, case, criteria=_criteria(src, dst))
    assert [s["status"] for s in first["steps"]] == ["ok", "error", "skipped"]

    (ws / "ventas_este.csv").write_text("region,total\neste,4\n", encoding="utf-8")
    second = _execute(recipe, ws, case)
    # the input file appeared, so every step of this case is stale; the run starts over
    assert second["status"] == "verified", second.get("why_unverified")
    log = (ws / "effects.log").read_text(encoding="utf-8").splitlines()
    assert log.count("ran ventas_este.csv") == 1

    third = _execute(recipe, ws, case)
    assert [s["status"] for s in third["steps"]] == ["reused", "reused", "reused"]
    assert third["ran_now"] == 0 and third["status"] == "verified" and third["executed"] is True
    assert (ws / "effects.log").read_text(encoding="utf-8").splitlines().count("ran ventas_este.csv") == 1
    mtime_after = os.stat(ws / "report_tool.py").st_mtime_ns
    _execute(recipe, ws, case)
    assert os.stat(ws / "report_tool.py").st_mtime_ns == mtime_after


def test_resume_after_a_failed_step_reuses_the_steps_before_it(ws):
    """Failure in the middle with the inputs unchanged: only the failed step
    and what follows run again."""
    steps = [
        {"tool": "process_start", "args": {"command": "echo uno >> efectos_a.log", "yield_ms": 2000}},
        {"tool": "process_start", "args": {"command": "test -f permiso.txt && echo dos >> efectos_a.log", "yield_ms": 2000}},
    ]
    recipe = rl.propose(OWNER, request="anota en efectos_a.log", steps=steps, title="Dos pasos")
    (name,) = [p["name"] for p in recipe.params]
    case = {name: "efectos_b.log"}
    crit = [{"kind": "regex", "path": "{{%s}}" % name, "pattern": "^dos$"}]
    first = _execute(recipe, ws, case, criteria=crit)
    assert [s["status"] for s in first["steps"]] == ["ok", "error"] and first["verified"] is False
    (ws / "permiso.txt").write_text("ok", encoding="utf-8")
    second = _execute(recipe, ws, case)
    assert [s["status"] for s in second["steps"]] == ["reused", "ok"] and second["verified"] is True
    assert (ws / "efectos_b.log").read_text(encoding="utf-8").splitlines() == ["uno", "dos"]


def test_changing_the_input_or_the_recipe_invalidates_the_earlier_verification(ws):
    recipe = _learn_from_case_a(ws)
    src, dst = _names(recipe)
    case_b = {src: "ventas_sur.csv", dst: "informe_sur.md"}
    assert _execute(recipe, ws, case_b, criteria=_criteria(src, dst))["verified"] is True
    status = _chat({"action": "verification", "recipe_id": recipe.id})
    (row,) = status["cases"]
    assert row["fresh"] is True and row["stale_reasons"] == []

    (ws / "ventas_sur.csv").write_text("region,total\nsur,1\n", encoding="utf-8")
    (row,) = _chat({"action": "verification", "recipe_id": recipe.id})["cases"]
    assert row["fresh"] is False and any(r.startswith("input_changed:") for r in row["stale_reasons"])

    again = _execute(recipe, ws, case_b)
    assert again["verified"] is True and again["ran_now"] == 3
    assert any(r.startswith("input_changed:") for r in again["steps"][0]["why_run"])
    assert {c["kind"]: c for c in again["criteria"]}["number"]["observed"] == 1.0
    assert _chat({"action": "verification", "recipe_id": recipe.id})["cases"][0]["fresh"] is True

    (ws / "informe_sur.md").write_text("editado a mano\n", encoding="utf-8")
    (row,) = _chat({"action": "verification", "recipe_id": recipe.id})["cases"]
    assert any(r.startswith("delivered_file_changed:") for r in row["stale_reasons"])

    stored = recipes_store.get_recipe(recipe.id, OWNER)
    stored.step_templates[2]["args"]["limit"] = 50
    recipes_store.save_recipe(OWNER, stored)
    (row,) = _chat({"action": "verification", "recipe_id": recipe.id})["cases"]
    assert "recipe_changed" in row["stale_reasons"] and row["fresh"] is False


def test_a_blocked_call_is_a_failed_step_not_a_new_permission(ws, monkeypatch):
    recipe = _learn_from_case_a(ws)
    src, dst = _names(recipe)
    import src.tool_execution as te
    monkeypatch.setattr(te, "_owner_is_admin", lambda owner: False)
    out = _execute(recipe, ws, {src: "ventas_sur.csv", dst: "informe_sur.md"}, criteria=_criteria(src, dst))
    assert out["verified"] is False and out["steps"][0]["status"] == "error"
    assert "admin" in out["steps"][0]["detail"] and out["steps"][1]["status"] == "skipped"


def test_classification_of_results():
    assert rex.classify("x", {"error": "nope", "exit_code": 1})[0] == "error"
    assert rex.classify("x", {"output": "fine", "exit_code": 0})[0] == "ok"
    assert rex.classify("x", {"result": 1, "ok": False})[0] == "error"
    assert rex.classify("image_job", {"job_id": "j1", "status": "queued", "exit_code": 0})[0] == "queued"
    assert rex.classify("x", {"blocked": True, "policy": "p"})[0] == "error"
    assert rex.classify("x", {"outcome_unknown": True})[0] == "error"
    assert rex.classify("process_read", {"handle": "h", "state": "exited", "process_exit_code": 3})[0] == "error"
    assert rex.classify("process_read", {"handle": "h", "state": "exited", "process_exit_code": 0})[0] == "ok"
    assert rex.classify("process_start", {"handle": "h", "state": "running"})[0] == "pending"


def test_an_executor_exception_is_reported_and_stops_the_chain(ws):
    recipe = _learn_from_case_a(ws)
    src, dst = _names(recipe)

    def boom(tool, args, call_id):
        raise RuntimeError("dispatcher down")

    out = ops.run_recipe(OWNER, {"action": "execute", "recipe_id": recipe.id, "workspace": str(ws),
                                 "params": {src: "ventas_sur.csv", dst: "informe_sur.md"}}, executor=boom)
    assert out["steps"][0]["status"] == "exception" and "dispatcher down" in out["steps"][0]["detail"]
    assert [s["status"] for s in out["steps"][1:]] == ["skipped", "skipped"] and out["verified"] is False


def test_unresolved_or_leaking_case_is_not_run(ws):
    recipe = _learn_from_case_a(ws)
    src, _dst = _names(recipe)
    out = _execute(recipe, ws, {src: "ventas_sur.csv"})
    assert out["status"] == "not_runnable" and out["executed"] is False and out["verified"] is False


def test_execute_needs_a_workspace_and_try_stays_dry_by_default(ws):
    recipe = _learn_from_case_a(ws)
    src, dst = _names(recipe)
    out = _chat({"action": "execute", "recipe_id": recipe.id, "params": {src: "a.csv", dst: "b.md"}})
    assert out.get("error_class") == "recipes.no_workspace"
    out = _chat({"action": "try", "recipe_id": recipe.id, "mode": "nonsense", "params": {src: "a.csv", dst: "b.md"}})
    assert out.get("error_class") == "recipes.bad_mode"


def test_http_and_mcp_offer_the_real_execution(ws, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routes.derivation_routes as dr
    recipe = _learn_from_case_a(ws)
    src, dst = _names(recipe)
    monkeypatch.setattr(dr, "effective_user", lambda request: OWNER)
    monkeypatch.setattr(dr, "require_admin", lambda request: None)
    app = FastAPI()
    app.include_router(dr.setup_derivation_routes())
    client = TestClient(app)
    case_b = {src: "ventas_sur.csv", dst: "informe_sur.md"}
    dry = client.post(f"/api/recipes/{recipe.id}/try", json={"params": case_b}).json()
    assert dry["executed"] is False and dry["kind"] == "structural_dry_run"
    r = client.post(f"/api/recipes/{recipe.id}/execute",
                    json={"params": case_b, "workspace": str(ws), "criteria": _criteria(src, dst)})
    assert r.status_code == 200 and r.json()["verified"] is True
    v = client.get(f"/api/recipes/{recipe.id}/verification").json()
    assert v["cases"][0]["fresh"] is True and v["exit_criteria"]

    pytest.importorskip("mcp")
    import mcp_servers.derivations_server as srv
    monkeypatch.setenv("ODYSSEUS_MCP_DERIVATIONS_OWNER", OWNER)
    case_c = {src: "ventas_norte.csv", dst: "informe_norte_2.md"}
    res = asyncio.run(srv.call_tool("recipe_learn", {"action": "try", "mode": "execute", "recipe_id": recipe.id,
                                                     "params": case_c, "workspace": str(ws)}))
    body = json.loads(res[0].text)
    assert body["mode"] == "execute" and body["verified"] is True and body["ran_now"] == 3


def test_promotion_says_whether_a_real_verification_backs_it(ws):
    recipe = _learn_from_case_a(ws)
    src, dst = _names(recipe)
    case_b = {src: "ventas_sur.csv", dst: "informe_sur.md"}
    assert _chat({"action": "try", "recipe_id": recipe.id, "params": case_b})["promotable"] is True
    assert _execute(recipe, ws, case_b, criteria=_criteria(src, dst))["verified"] is True
    out = rl.promote(recipe.id, OWNER)
    assert out["promoted_from"] == "verified_execution" and out["verified_execution"]["params"] == case_b
