"""One account for every model call a turn makes (src/turn_spend.py)."""

import asyncio
import json
import sqlite3

import pytest

from src import budget_account as ba
from src import llm_core, llm_trace, turn_spend


@pytest.fixture(autouse=True)
def account_db(tmp_path, monkeypatch):
    monkeypatch.setattr(ba, "default_path", lambda: tmp_path / "budget.sqlite3")
    monkeypatch.setattr(turn_spend, "_run_ceiling", lambda: 0)
    return tmp_path / "budget.sqlite3"


def _fake_impl(monkeypatch, *, attempts=(), text="answer", local=False, raises=None,
               outcome_unknown=0, hang=False, calls=None):
    """Replace the provider call; each item of ``attempts`` is one reported usage."""

    async def impl(url, model, messages, **kw):
        if calls is not None:
            calls.append(kw)
        for _ in range(outcome_unknown):
            kw["on_outcome_unknown"](1, 0.5)
        if hang:
            await asyncio.sleep(3600)
        for usage in attempts:
            kw["_on_observed_usage"]({**usage, "usage_source": "reported_engine"},
                                     endpoint_local=local)
        if raises:
            raise raises
        return text

    monkeypatch.setattr(llm_core, "_llm_call_async_impl", impl)


def _call(**kw):
    return llm_core.llm_call_async(
        "http://x/v1", "m", [{"role": "user", "content": "hello there"}], max_tokens=100, **kw)


def _run(coro):
    return asyncio.run(coro)


def _turn(mode="record", run_id="run-1"):
    return turn_spend.install(run_id, mode=mode)


# --- booking ---------------------------------------------------------------------


def test_a_reported_call_is_booked_under_its_purpose_with_unknown_remote_cost(monkeypatch):
    _fake_impl(monkeypatch, attempts=[{"input_tokens": 100, "output_tokens": 50}])
    token = _turn()
    _run(_call(_spend_purpose="advisor"))
    turn_spend.uninstall(token)
    snap = ba.snapshot("run-1")
    assert snap["consumed_tokens"] == 150
    assert snap["purposes"]["advisor"]["calls"] == 1
    assert snap["purposes"]["advisor"]["consumed_tokens"] == 150
    # a remote provider that gave no price is not free
    assert snap["consumed_cost"] == "unknown"
    assert snap["purposes"]["advisor"]["cost"] == "unknown"


def test_a_local_endpoint_is_a_known_zero_and_a_priced_call_sums(monkeypatch):
    _fake_impl(monkeypatch, attempts=[{"input_tokens": 10, "output_tokens": 5}], local=True)
    token = _turn(run_id="local")
    _run(_call(_spend_purpose="a"))
    turn_spend.uninstall(token)
    assert ba.snapshot("local")["consumed_cost"] == 0.0
    _fake_impl(monkeypatch, attempts=[{"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.01},
                                      {"input_tokens": 20, "output_tokens": 5, "cost_usd": 0.02}])
    token = _turn(run_id="priced")
    _run(_call(_spend_purpose="a"))
    turn_spend.uninstall(token)
    snap = ba.snapshot("priced")
    assert snap["consumed_tokens"] == 40
    assert snap["consumed_cost"] == pytest.approx(0.03)


def test_a_retry_adds_to_the_first_attempt_instead_of_replacing_it(monkeypatch):
    _fake_impl(monkeypatch, attempts=[{"input_tokens": 100, "output_tokens": 0},
                                      {"input_tokens": 100, "output_tokens": 30}])
    token = _turn()
    _run(_call(_spend_purpose="grounding"))
    turn_spend.uninstall(token)
    snap = ba.snapshot("run-1")
    assert snap["consumed_tokens"] == 230
    assert snap["purposes"]["grounding"]["attempts"] == 2


def test_usage_the_provider_never_reported_stays_unknown_not_zero(monkeypatch):
    _fake_impl(monkeypatch, attempts=[])
    token = _turn()
    _run(_call(_spend_purpose="typed_decision"))
    turn_spend.uninstall(token)
    snap = ba.snapshot("run-1")
    assert snap["unknown_usage_calls"] == 1
    assert snap["consumed_cost"] == "unknown"
    assert snap["purposes"]["typed_decision"]["unknown_usage_calls"] == 1


def test_an_uncertain_outcome_is_recorded_but_a_clean_failure_is_not(monkeypatch):
    _fake_impl(monkeypatch, outcome_unknown=1, raises=RuntimeError("read timeout"))
    token = _turn()
    with pytest.raises(RuntimeError):
        _run(_call(_spend_purpose="maybe_spent"))
    _fake_impl(monkeypatch, raises=RuntimeError("401 unauthorized"))
    with pytest.raises(RuntimeError):
        _run(_call(_spend_purpose="never_sent"))
    turn_spend.uninstall(token)
    purposes = ba.snapshot("run-1")["purposes"]
    assert purposes["maybe_spent"]["unknown_usage_calls"] == 1
    assert "never_sent" not in purposes


def test_the_callers_own_outcome_hook_still_runs(monkeypatch):
    _fake_impl(monkeypatch, outcome_unknown=1)
    seen = []
    token = _turn()
    _run(_call(_spend_purpose="x", on_outcome_unknown=lambda a, e: seen.append(a)))
    turn_spend.uninstall(token)
    assert seen == [1]


# --- charging the turn's ledger ------------------------------------------------------


def test_other_calls_are_charged_to_the_ledger_but_compaction_and_recovery_are_not_twice(monkeypatch):
    _fake_impl(monkeypatch, attempts=[{"input_tokens": 100, "output_tokens": 20}])
    charged = []
    token = _turn()
    turn_spend.current().bind(charge=lambda usage, *, purpose, endpoint_local=False:
                              charged.append((purpose, usage["input_tokens"])))
    _run(_call(_spend_purpose="advisor"))
    with llm_trace.call_phase("compaction"):
        _run(_call())
    with llm_trace.call_phase("recovery", step=2):
        _run(_call())
    turn_spend.uninstall(token)
    assert charged == [("advisor", 100)]  # the loop charges the other two itself
    purposes = ba.snapshot("run-1")["purposes"]
    assert purposes["compaction"]["calls"] == 1 and purposes["recovery"]["calls"] == 1


def test_the_purpose_comes_from_the_label_then_the_context_then_the_calling_module(monkeypatch):
    _fake_impl(monkeypatch, attempts=[{"input_tokens": 1, "output_tokens": 1}])
    token = _turn()
    _run(_call(_spend_purpose="explicit"))

    async def scoped():
        with turn_spend.spend_purpose("scoped"):
            await _call()
    _run(scoped())

    async def direct():  # awaited straight from this module, as real callers do
        await _call()
    _run(direct())
    turn_spend.uninstall(token)
    purposes = set(ba.snapshot("run-1")["purposes"])
    assert {"explicit", "scoped"} <= purposes, purposes
    assert any(p.endswith("test_turn_spend") for p in purposes), purposes


# --- reservations: admission and cancel ------------------------------------------------


def test_enforce_refuses_before_the_call_when_the_ceiling_would_be_passed(monkeypatch):
    monkeypatch.setattr(turn_spend, "_run_ceiling", lambda: 50)
    calls = []
    _fake_impl(monkeypatch, attempts=[{"input_tokens": 1, "output_tokens": 1}], calls=calls)
    token = _turn("enforce")
    with pytest.raises(turn_spend.TurnSpendExceeded) as err:
        _run(_call(_spend_purpose="advisor"))
    turn_spend.uninstall(token)
    assert "budget exceeded" in err.value.reason and err.value.purpose == "advisor"
    assert calls == []  # never reached the provider
    snap = ba.snapshot("run-1")
    assert snap["reserved_tokens"] == 0 and snap["purposes"] == {}


def test_enforce_refuses_when_the_loop_says_the_turn_is_over_budget(monkeypatch):
    calls = []
    _fake_impl(monkeypatch, attempts=[], calls=calls)
    token = _turn("enforce")
    turn_spend.current().bind(admit=lambda url: "turn budget reached: tokens 10/10")
    with pytest.raises(turn_spend.TurnSpendExceeded, match="tokens 10/10"):
        _run(_call(_spend_purpose="advisor"))
    turn_spend.uninstall(token)
    assert calls == []


def test_record_mode_never_refuses(monkeypatch):
    monkeypatch.setattr(turn_spend, "_run_ceiling", lambda: 50)
    _fake_impl(monkeypatch, attempts=[{"input_tokens": 500, "output_tokens": 500}])
    token = _turn("record")
    turn_spend.current().bind(admit=lambda url: "over budget")
    assert _run(_call(_spend_purpose="advisor")) == "answer"
    turn_spend.uninstall(token)
    assert ba.snapshot("run-1")["consumed_tokens"] == 1000


def test_a_reserved_call_reconciles_its_reservation_into_real_usage(monkeypatch):
    monkeypatch.setattr(turn_spend, "_run_ceiling", lambda: 100_000)
    _fake_impl(monkeypatch, attempts=[{"input_tokens": 40, "output_tokens": 10}])
    token = _turn("enforce")
    _run(_call(_spend_purpose="advisor"))
    turn_spend.uninstall(token)
    snap = ba.snapshot("run-1")
    assert snap["reserved_tokens"] == 0 and snap["consumed_tokens"] == 50
    assert snap["remaining_tokens"] == 100_000 - 50


def test_cancel_releases_the_reservation_once_and_records_unknown_usage(monkeypatch):
    monkeypatch.setattr(turn_spend, "_run_ceiling", lambda: 100_000)
    _fake_impl(monkeypatch, hang=True)
    token = _turn("enforce")

    async def scenario():
        task = asyncio.ensure_future(_call(_spend_purpose="advisor"))
        await asyncio.sleep(0.05)
        assert ba.snapshot("run-1")["reserved_tokens"] > 0  # held while in flight
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    _run(scenario())
    snap = ba.snapshot("run-1")
    assert snap["reserved_tokens"] == 0
    assert snap["remaining_tokens"] == 100_000
    assert snap["unknown_usage_calls"] == 1
    assert [c["status"] for c in snap["children"]] == ["reconciled"]
    # closing the turn afterwards finds nothing left to settle
    turn_spend.uninstall(token)
    after = ba.snapshot("run-1")
    assert after["unknown_usage_calls"] == 1 and len(after["children"]) == 1


def test_settling_twice_and_releasing_twice_frees_only_once(monkeypatch):
    monkeypatch.setattr(turn_spend, "_run_ceiling", lambda: 1000)
    token = _turn("enforce")
    account = turn_spend.current()
    call = account.begin(url="http://x", estimate_tokens=300, observer_charged=False,
                         purpose="advisor")
    assert ba.snapshot("run-1")["reserved_tokens"] == 300
    assert call.settle("error") == {"released": True}
    assert call.settle("error") is None
    assert ba.snapshot("run-1")["reserved_tokens"] == 0
    assert ba.release("run-1", call.child_id) is False  # nothing left to free
    turn_spend.uninstall(token)


def test_ending_the_turn_settles_what_is_still_open_exactly_once(monkeypatch):
    monkeypatch.setattr(turn_spend, "_run_ceiling", lambda: 1000)
    token = _turn("enforce")
    account = turn_spend.current()
    account.begin(url="http://x", estimate_tokens=300, observer_charged=False, purpose="advisor")
    turn_spend.uninstall(token)
    snap = ba.snapshot("run-1")
    assert snap["reserved_tokens"] == 0 and snap["unknown_usage_calls"] == 1
    account.close()  # a second close changes nothing
    assert ba.snapshot("run-1")["unknown_usage_calls"] == 1


# --- safety ---------------------------------------------------------------------------


def test_off_mode_books_nothing_and_a_broken_store_never_breaks_the_call(monkeypatch):
    _fake_impl(monkeypatch, attempts=[{"input_tokens": 5, "output_tokens": 5}])
    assert turn_spend.install("run-off", mode="off") is None
    assert _run(_call()) == "answer"
    assert ba.snapshot("run-off")["children"] == []

    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(ba, "reconcile", boom)
    token = _turn(run_id="run-broken")
    assert _run(_call(_spend_purpose="advisor")) == "answer"
    turn_spend.uninstall(token)


def test_without_an_installed_account_the_call_is_untouched(monkeypatch):
    _fake_impl(monkeypatch, attempts=[{"input_tokens": 5, "output_tokens": 5}])
    assert turn_spend.current() is None
    assert _run(_call()) == "answer"
    assert ba.snapshot("run-1")["children"] == []


def test_main_rounds_are_mirrored_for_the_breakdown(monkeypatch):
    token = _turn()
    account = turn_spend.current()
    account.record_main(round_num=1, input_tokens=300, output_tokens=40,
                        cost_usd=None, endpoint_local=False)
    account.record_main(round_num=2, input_tokens=10, output_tokens=10,
                        cost_usd=0.5, endpoint_local=False)
    turn_spend.uninstall(token)
    main = ba.snapshot("run-1")["purposes"]["main"]
    assert main["calls"] == 2 and main["consumed_tokens"] == 360
    assert main["cost"] == "unknown"  # one round had no price


def test_explain_orders_purposes_by_spend(monkeypatch):
    _fake_impl(monkeypatch, attempts=[{"input_tokens": 10, "output_tokens": 0}])
    token = _turn()
    _run(_call(_spend_purpose="small"))
    _fake_impl(monkeypatch, attempts=[{"input_tokens": 900, "output_tokens": 0}])
    _run(_call(_spend_purpose="big"))
    turn_spend.uninstall(token)
    report = turn_spend.explain("run-1")
    assert [r["purpose"] for r in report["purposes"]] == ["big", "small"]
    assert report["consumed_tokens"] == 910 and report["unknown_usage_calls"] == 0


# --- the account file ---------------------------------------------------------------------


def test_a_version_one_account_file_is_read_as_is_and_upgraded_on_write(tmp_path, monkeypatch):
    path = tmp_path / "old.sqlite3"
    monkeypatch.setattr(ba, "default_path", lambda: path)
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE runs (run_id TEXT PRIMARY KEY, project_id TEXT NOT NULL DEFAULT '',
        ceiling_tokens INTEGER NOT NULL DEFAULT 0, ceiling_cost REAL,
        created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
    conn.execute("""CREATE TABLE reservations (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
        child_id TEXT NOT NULL, status TEXT NOT NULL, reserved_tokens INTEGER NOT NULL DEFAULT 0,
        reserved_cost REAL, consumed_tokens INTEGER NOT NULL DEFAULT 0, consumed_cost REAL,
        unpriced INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
    conn.execute("INSERT INTO runs VALUES ('old', '', 0, NULL, 1, 1)")
    conn.execute("INSERT INTO reservations (run_id, child_id, status, reserved_tokens, consumed_tokens,"
                 " consumed_cost, unpriced, created_at, updated_at)"
                 " VALUES ('old', 'w1', 'reconciled', 10, 10, 0.5, 0, 1, 1)")
    conn.execute("PRAGMA user_version=1")
    conn.commit()
    conn.close()
    snap = ba.snapshot("old")  # read only, no upgrade
    assert snap["consumed_tokens"] == 10 and snap["consumed_cost"] == 0.5
    assert snap["purposes"]["unlabelled"]["calls"] == 1
    ba.reconcile("old", "w2", 5, None, purpose="advisor", usage_unknown=True)
    check = sqlite3.connect(path)
    assert check.execute("PRAGMA user_version").fetchone()[0] == ba.VERSION
    check.close()
    after = ba.snapshot("old")
    assert after["unknown_usage_calls"] == 1 and after["consumed_cost"] == "unknown"


# --- wired into the loop ----------------------------------------------------------------------


def test_the_loop_installs_books_and_closes_the_account(monkeypatch):
    import src.agent_loop as al

    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    _fake_impl(monkeypatch, attempts=[{"input_tokens": 70, "output_tokens": 30}])
    seen = {}

    async def fake_stream(_candidates, messages, **kwargs):
        seen["account"] = turn_spend.current()
        await _call(_spend_purpose="advisor")  # an auxiliary call inside the turn
        yield f'data: {json.dumps({"delta": "ok"})}\n\n'
        yield f'data: {json.dumps({"type": "finish", "finish_reason": "stop"})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", fake_stream, raising=False)

    async def go():
        return [c async for c in al.stream_agent_loop(
            "http://x/v1", "m", [{"role": "user", "content": "hi"}],
            session_id="sess-spend", owner="ada", max_rounds=1, relevant_tools={"bash"})]

    _run(go())
    assert seen["account"] is not None and seen["account"].mode == "record"
    assert turn_spend.current() is None  # uninstalled at the end of the turn
    snap = ba.snapshot("sess-spend")
    assert snap["purposes"]["advisor"]["consumed_tokens"] == 100


# --- read surfaces ---------------------------------------------------------------------------


def test_the_spend_route_and_the_mcp_tool_show_the_same_breakdown(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.budget_routes import setup_budget_routes
    import src.auth_helpers as auth_helpers
    monkeypatch.setattr(auth_helpers, "require_user", lambda request: "ada", raising=False)
    import routes.budget_routes as br
    monkeypatch.setattr(br, "require_user", lambda request: "ada")

    _fake_impl(monkeypatch, attempts=[{"input_tokens": 70, "output_tokens": 30}])
    token = _turn(run_id="r-route")
    _run(_call(_spend_purpose="advisor"))
    _fake_impl(monkeypatch, attempts=[])
    _run(_call(_spend_purpose="verifier"))
    turn_spend.uninstall(token)

    app = FastAPI()
    app.include_router(setup_budget_routes())
    body = TestClient(app).get("/api/runs/r-route/spend").json()
    assert body["consumed_tokens"] == 100 and body["unknown_usage_calls"] == 1
    assert body["consumed_cost"] == "unknown"
    assert [r["purpose"] for r in body["purposes"]] == ["advisor", "verifier"]

    from tests.test_dispatch import _load_workers_server
    ws = _load_workers_server(monkeypatch)
    assert "run_spend" in {t.name for t in ws.TOOLS}
    monkeypatch.setattr(ws, "_request", lambda method, path, body=None, **kw: turn_spend.explain("r-route"))
    text = _run(ws.call_tool("run_spend", {"run_id": "r-route"}))[0].text
    assert "advisor: 100 tokens" in text and "never reported usage" in text
    assert "cost unknown" in text
    assert "Error" in _run(ws.call_tool("run_spend", {}))[0].text
