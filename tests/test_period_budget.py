"""src/period_budget.py -- the period budget governor.

The pace-line cases are the ones the MIT-licensed agent farm this was adapted
from tests in its governor (full speed behind the line, a partial throttle
inside the band, a pause that says when it ends, a hard stop at the target, a
window past its reset counting as empty, a floor that slows but never freezes);
the rest covers what Faustus adds: the ledger, GPU seconds, the interactive
back-off and each place the budget is enforced.
"""
from __future__ import annotations

import asyncio
import json
import time
import types
from contextlib import asynccontextmanager

import pytest

from src import period_budget as pb
from src import unattended_breaker as ub

HOSTED = "https://openrouter.ai/api/v1/chat/completions"
LOCAL = "http://127.0.0.1:8081/v1/chat/completions"


@pytest.fixture
def cfg(monkeypatch):
    """Set the budget settings for one test: cfg(budget_gpu_daily_seconds=20)."""
    values = {}

    def fake_get_setting(key, default=None):
        return values.get(key, default)

    monkeypatch.setattr("src.settings.get_setting", fake_get_setting)

    def setter(**kw):
        values.update(kw)
        return values
    return setter


def _noon_today():
    start, end = pb.window_bounds("day", time.time())
    return start + 12 * 3600.0


# -- decide(): the pace line ----------------------------------------------------

W0, WLEN = 0.0, 1000.0


def _d(usage, elapsed, *, target=1.0, band=0.1, floor=1, interactive=False, n=4, now=500.0):
    return pb.decide(usage, target, band, elapsed, floor, interactive, max_workers=n,
                     window_start=W0, window_length=WLEN, now=now, label="t")


def test_behind_the_pace_line_runs_at_full_speed():
    d = _d(0.2, 0.5)
    assert d.workers == 4 and d.kind in ("ok", "pace") and d.pause_until is None


def test_inside_the_band_throttles_partially():
    # 55 % used against 50 % elapsed + 10 % band: half of the band's headroom is left
    d = _d(0.55, 0.5, floor=0)
    assert d.kind == "pace" and d.workers == 2


def test_ahead_of_the_line_pauses_until_the_line_catches_up():
    d = _d(0.7, 0.5, floor=0)
    assert d.workers == 0 and d.kind == "pace"
    # elapsed + band reaches 0.7 at 60 % of the window
    assert d.pause_until == pytest.approx(W0 + 0.6 * WLEN)
    assert d.retry_after_s == pytest.approx(100.0)


def test_the_target_reached_is_a_hard_stop_until_the_reset():
    d = _d(1.0, 0.2)
    assert d.workers == 0 and d.kind == "limit"
    assert d.pause_until == pytest.approx(W0 + WLEN)
    assert d.retry_after_s == pytest.approx(500.0)


def test_over_the_target_is_still_a_hard_stop():
    assert _d(3.0, 0.9).workers == 0


def test_a_floor_slows_pacing_but_only_the_target_stops_work():
    d = _d(0.7, 0.5, floor=1)
    assert d.workers == 1 and d.kind == "pace"
    assert _d(1.0, 0.5, floor=1).workers == 0          # the floor never overrides the hard stop


def test_a_floor_is_capped_by_the_workers_asked_for():
    assert _d(0.7, 0.5, floor=9, n=2).workers == 2


def test_the_band_gives_an_early_window_some_room():
    # nothing of the window has passed, but the band lets 10 % through
    assert _d(0.05, 0.0, floor=0).workers > 0
    assert _d(0.2, 0.0, floor=0).workers == 0


def test_a_zero_band_is_a_straight_line():
    assert _d(0.4, 0.5, band=0.0, floor=0).workers == 4
    assert _d(0.6, 0.5, band=0.0, floor=0).workers == 0


def test_no_target_never_limits_and_zero_workers_stays_zero():
    d = pb.decide(99.0, 0.0, 0.1, 0.1, 0, False, max_workers=3)
    assert d.workers == 3 and "no target" in d.reason
    assert pb.decide(0.0, 1.0, 0.1, 0.5, 1, False, max_workers=0).workers == 0


def test_an_interactive_turn_backs_off_whatever_the_allowance():
    d = _d(0.1, 0.5, interactive=True)
    assert d.workers == 0 and d.kind == "interactive" and d.retry_after_s
    # with no target at all the back-off still applies
    assert pb.decide(0, 0, 0.1, 0.5, 1, True, max_workers=2).kind == "interactive"


def test_the_hard_stop_outranks_the_interactive_back_off():
    assert _d(1.2, 0.5, interactive=True).kind == "limit"


def test_a_window_past_its_reset_counts_as_empty():
    assert pb.live_usage(0.9, 100.0, 200.0) == 0.0
    assert pb.live_usage(0.9, 300.0, 200.0) == pytest.approx(0.9)
    assert pb.live_usage(0.9, None, 200.0) == pytest.approx(0.9)
    assert pb.live_usage(-5, None, 0.0) == 0.0


def test_the_pause_is_never_shorter_than_the_minimum():
    d = pb.decide(0.61, 1.0, 0.1, 0.5, 0, False, max_workers=1,
                  window_start=W0, window_length=WLEN, now=599.0)
    assert d.workers == 0 and d.pause_until == pytest.approx(599.0 + 60.0)


def test_the_decision_serialises_with_an_iso_time():
    out = _d(0.7, 0.5, floor=0).to_dict()
    assert out["workers"] == 0 and out["pause_until"] and out["pause_until_iso"]
    json.dumps(out)


# -- windows ----------------------------------------------------------------------

def test_day_and_week_windows_are_local_and_contiguous():
    now = time.time()
    ds, de = pb.window_bounds("day", now)
    ws, we = pb.window_bounds("week", now)
    assert ds <= now < de and ws <= now < we
    assert 23 * 3600 <= de - ds <= 25 * 3600           # a DST day can be 23 or 25 hours
    assert 6.9 * 86400 <= we - ws <= 7.1 * 86400
    assert ws <= ds and de <= we
    assert pb.elapsed_fraction(ds, de, ds) == 0.0 and pb.elapsed_fraction(ds, de, de) == 1.0


# -- settings -----------------------------------------------------------------------

def test_targets_parse_the_json_setting_and_a_bare_number_is_dollars(cfg):
    cfg(budget_period_targets={"OpenRouter.ai": {"usd": 5, "tokens": 1000}, "*": 20, "bad": {}, "": 3})
    assert pb.targets() == {"openrouter.ai": {"usd": 5.0, "tokens": 1000.0}, "*": {"usd": 20.0}}
    cfg(budget_period_targets='{"anthropic": {"tokens": 5000}}')
    assert pb.targets() == {"anthropic": {"tokens": 5000.0}}
    cfg(budget_period_targets="not json")
    assert pb.targets() == {}


def test_defaults_when_nothing_is_set(cfg):
    assert pb.targets() == {} and pb.period_window() == "week" and pb.band_fraction() == pytest.approx(0.10)
    assert pb.gpu_daily_seconds() == 0.0 and pb.backoff_when_interactive() is True and pb.min_workers() == 0
    cfg(budget_period_window="fortnight", budget_period_band_pct=250, budget_backoff_when_interactive="off")
    assert pb.period_window() == "week" and pb.band_fraction() == 1.0 and pb.backoff_when_interactive() is False

# -- the ledger -----------------------------------------------------------------------

def test_hosted_spend_is_booked_and_local_calls_book_nothing():
    pb.on_model_call(HOSTED, "some/model", {"prompt_tokens": 80, "completion_tokens": 20, "cost_usd": 0.5})
    pb.on_model_call(LOCAL, "qwen", {"prompt_tokens": 80, "completion_tokens": 20})
    used = pb.usage_since("*", 0)
    assert used["usd"] == pytest.approx(0.5) and used["calls"] == 1 and used["tokens"] >= 100
    assert pb.usage_since("local", 0)["calls"] == 0


def test_a_call_with_no_reported_price_counts_its_tokens_as_unpriced():
    pb.on_model_call(HOSTED, "m", {"total_tokens": 300})
    used = pb.usage_since("*", 0)
    assert used["usd"] == 0.0 and used["tokens"] > 0 and used["unpriced_tokens"] == pytest.approx(used["tokens"])


def test_usage_is_sliced_by_provider_window_and_key():
    now = time.time()
    pb.record_usage("openrouter.ai", kind="openrouter", usd=1.0, tokens=10, ts=now)
    pb.record_usage("api.anthropic.com", kind="anthropic", usd=2.0, tokens=20, ts=now)
    pb.record_usage("openrouter.ai", kind="openrouter", usd=4.0, tokens=40, ts=now - 30 * 86400)
    assert pb.usage_since("*", now - 86400)["usd"] == pytest.approx(3.0)
    assert pb.usage_since("openrouter.ai", now - 86400)["usd"] == pytest.approx(1.0)
    assert pb.usage_since("anthropic", now - 86400)["usd"] == pytest.approx(2.0)
    assert pb.usage_since("openrouter", 0)["usd"] == pytest.approx(5.0)
    assert pb.usage_since("*", now - 86400, now - 3600)["calls"] == 0
    assert {r["provider"] for r in pb.providers_seen(now - 86400)} == {"openrouter.ai", "api.anthropic.com"}


def test_gpu_seconds_are_booked_for_the_local_model_only():
    pb.record_gpu_seconds(LOCAL, 12.5, "qwen")
    pb.record_gpu_seconds(LOCAL, 0, "qwen")
    pb.record_gpu_seconds(LOCAL, -3, "qwen")
    assert pb.usage_since("local", 0)["gpu_seconds"] == pytest.approx(12.5)
    assert pb.usage_since("*", 0)["calls"] == 0


def test_the_local_generation_slot_books_the_seconds_it_was_held(monkeypatch):
    import src.llm_core as llm_core

    @asynccontextmanager
    async def no_gate(*a, **k):                 # the wait for the slot is not what is timed
        yield
    monkeypatch.setattr(llm_core, "_local_model_slot_gate", no_gate)

    async def run():
        async with llm_core._local_model_slot(LOCAL, "qwen"):
            await asyncio.sleep(0.15)
        async with llm_core._local_model_slot(HOSTED, "m"):          # hosted: nothing booked
            await asyncio.sleep(0.05)
    asyncio.run(run())
    used = pb.usage_since("local", 0)
    assert used["gpu_seconds"] >= 0.1 and used["calls"] == 1
    assert pb.usage_since("*", 0)["calls"] == 0


def test_the_trace_hook_feeds_the_ledger():
    from src import llm_trace
    llm_trace.record_call(session_id=None, endpoint_url=HOSTED, model="m",
                          usage={"cost_usd": 0.25, "total_tokens": 50})
    assert pb.usage_since("*", 0)["usd"] == pytest.approx(0.25)


# -- evaluate / gate: the enforcement answer ---------------------------------------------

def test_nothing_set_means_nothing_is_ever_paused():
    assert pb.gate("dispatch", HOSTED, unattended=True) is None
    assert pb.gate("dispatch", LOCAL, unattended=True) is None
    assert pb.evaluate(None, unattended=True, max_workers=3).workers == 3


def test_a_spent_hosted_target_pauses_with_a_clear_answer(cfg):
    cfg(budget_period_targets={"*": {"usd": 10}})
    now = time.time()
    pb.record_usage("openrouter.ai", kind="openrouter", usd=10.0, tokens=1, ts=now)
    answer = pb.gate("dispatch", HOSTED, unattended=False, now=now)
    assert answer["status"] == "budget_paused" and answer["budget_paused"] is True
    assert answer["point"] == "dispatch" and answer["kind"] == "limit"
    assert answer["pause_until"] == pytest.approx(pb.window_bounds("week", now)[1])
    assert answer["pause_until_iso"] and answer["reason"]
    # a local model is not covered by a hosted target
    assert pb.gate("dispatch", LOCAL, unattended=False, now=now) is None


def test_a_target_for_one_provider_leaves_the_others_alone(cfg):
    cfg(budget_period_targets={"api.anthropic.com": {"usd": 1}})
    now = time.time()
    pb.record_usage("api.anthropic.com", kind="anthropic", usd=1.5, tokens=1, ts=now)
    assert pb.gate("dispatch", "https://api.anthropic.com/v1/messages", now=now)["budget_paused"]
    assert pb.gate("dispatch", HOSTED, now=now) is None


def test_a_token_target_is_enforced_too(cfg):
    cfg(budget_period_targets={"*": {"tokens": 1000}})
    now = time.time()
    pb.record_usage("openrouter.ai", kind="openrouter", usd=0.0, tokens=1200, ts=now)
    assert pb.gate("dispatch", HOSTED, now=now)["budget_paused"] is True


def test_the_gpu_seconds_window_pauses_the_local_model(cfg):
    cfg(budget_gpu_daily_seconds=20)
    now = _noon_today()
    pb.record_usage("local", kind="local", gpu_seconds=25.0, ts=now)
    answer = pb.gate("dispatch", LOCAL, unattended=True, now=now)
    assert answer["budget_paused"] is True and answer["scope"] == "local gpu_seconds/day"
    assert answer["pause_until"] == pytest.approx(pb.window_bounds("day", now)[1])
    assert answer["retry_after_s"] > 0


def test_gpu_seconds_inside_the_allowance_let_work_through(cfg):
    cfg(budget_gpu_daily_seconds=20)
    now = _noon_today()
    pb.record_usage("local", kind="local", gpu_seconds=5.0, ts=now)
    assert pb.gate("dispatch", LOCAL, now=now) is None


def test_the_gpu_pace_line_throttles_an_early_burst(cfg):
    cfg(budget_gpu_daily_seconds=100)
    start, _ = pb.window_bounds("day", time.time())
    now = start + 3600.0                                   # 1 am: ~4 % of the day gone
    pb.record_usage("local", kind="local", gpu_seconds=40.0, ts=now - 60)
    answer = pb.gate("dispatch", LOCAL, now=now)
    assert answer and answer["kind"] == "pace" and answer["pause_until"] > now


def test_a_window_that_reset_counts_as_empty_in_the_ledger(cfg):
    cfg(budget_period_targets={"*": {"usd": 1}})
    now = time.time()
    pb.record_usage("openrouter.ai", kind="openrouter", usd=5.0, tokens=1, ts=now - 20 * 86400)
    assert pb.gate("dispatch", HOSTED, now=now) is None


def test_an_interactive_turn_holds_unattended_work_only(cfg, monkeypatch):
    monkeypatch.setattr("src.interactive_gate._has_active_chat_stream", lambda: True)
    held = pb.gate("dispatch", LOCAL, unattended=True)
    assert held["kind"] == "interactive" and held["budget_paused"] and held["retry_after_s"]
    assert pb.gate("dispatch", LOCAL, unattended=False) is None        # the owner's own turn is never held
    cfg(budget_backoff_when_interactive=False)
    assert pb.gate("dispatch", LOCAL, unattended=True) is None


def test_an_open_breaker_stops_unattended_starts_with_breaker_open(cfg):
    cfg(unattended_failure_breaker=2)
    ub.record("dispatch", False, "boom")
    ub.record("dispatch", False, "boom")
    held = pb.gate("dispatch", LOCAL, unattended=True)
    assert held["status"] == "breaker_open" and held["breaker_open"] is True and held["pause_until"]
    assert pb.gate("dispatch", LOCAL, unattended=False) is None


def test_a_cooled_down_provider_is_not_started_against():
    now = time.time()
    ub.note_status(HOSTED, 429, {"Retry-After": "120"}, now=now)
    answer = pb.gate("dispatch", HOSTED, now=now)
    assert answer["kind"] == "cooldown" and answer["pause_until"] == pytest.approx(now + 120.0, abs=1.0)
    assert pb.gate("dispatch", "https://api.other-provider.test/v1", now=now) is None


def test_a_broken_ledger_lets_work_through(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db gone")
    monkeypatch.setattr(pb, "evaluate", boom)
    assert pb.gate("dispatch", HOSTED) is None
    assert pb.allowed_workers("x", HOSTED, 3) == (3, None)

# -- allowed_workers --------------------------------------------------------------------

def test_the_worker_count_is_capped_by_the_pace_line(cfg):
    cfg(budget_period_targets={"*": {"usd": 10}})
    start, end = pb.window_bounds("week", time.time())
    now = start + 0.5 * (end - start)
    pb.record_usage("openrouter.ai", kind="openrouter", usd=5.5, tokens=1, ts=now)
    n, answer = pb.allowed_workers("delegate_agents", HOSTED, 4, now=now)
    assert n == 2 and answer["point"] == "delegate_agents"
    pb.reset_for_tests()
    pb.record_usage("openrouter.ai", kind="openrouter", usd=1.0, tokens=1, ts=now)
    assert pb.allowed_workers("delegate_agents", HOSTED, 4, now=now) == (4, None)
    pb.record_usage("openrouter.ai", kind="openrouter", usd=7.0, tokens=1, ts=now)
    n, answer = pb.allowed_workers("delegate_agents", HOSTED, 4, now=now)
    assert n == 0 and answer["budget_paused"] is True


def test_the_floor_setting_keeps_a_paced_window_moving(cfg):
    cfg(budget_period_targets={"*": {"usd": 10}}, budget_period_min_workers=1)
    start, end = pb.window_bounds("week", time.time())
    now = start + 0.5 * (end - start)
    pb.record_usage("openrouter.ai", kind="openrouter", usd=7.0, tokens=1, ts=now)
    n, _ = pb.allowed_workers("delegate_agents", HOSTED, 4, now=now)
    assert n == 1
    pb.record_usage("openrouter.ai", kind="openrouter", usd=3.0, tokens=1, ts=now)
    assert pb.allowed_workers("delegate_agents", HOSTED, 4, now=now)[0] == 0      # the target still stops it


# -- enforcement points --------------------------------------------------------------------

def test_the_router_holds_a_paid_escalation_while_the_budget_is_spent(cfg, monkeypatch):
    from src import model_router, privacy_policy
    monkeypatch.setattr(privacy_policy, "get_privacy_profile", lambda **kw: privacy_policy.PROFILE_CLOUD_ALLOWED)
    config = model_router.RouterConfig(allow_paid_escalation=True)
    req = model_router.Requirements(capabilities=("vision",))

    free = model_router.choose(req, installed=["unqualified"], config=config, log=False)
    assert free.escalated is True

    cfg(budget_period_targets={"*": {"usd": 1}})
    pb.record_usage("openrouter.ai", kind="openrouter", usd=2.0, tokens=1)
    held = model_router.choose(req, installed=["unqualified"], config=config, log=False)
    assert held.escalated is False and held.model is None
    assert held.escalation["budget_paused"]["status"] == "budget_paused"
    assert held.escalation["budget_paused"]["point"] == "router_escalation"
    assert "budget_paused" in held.reason


def test_the_escalation_gate_looks_at_every_provider_target(cfg):
    cfg(budget_period_targets={"api.anthropic.com": {"usd": 1}, "openrouter.ai": {"usd": 1}})
    now = time.time()
    pb.record_usage("api.anthropic.com", kind="anthropic", usd=2.0, tokens=1, ts=now)
    assert pb.gate_paid_escalation(now) is None            # one provider is still free
    pb.record_usage("openrouter.ai", kind="openrouter", usd=2.0, tokens=1, ts=now)
    assert pb.gate_paid_escalation(now)["budget_paused"] is True


def _delegate_env(monkeypatch, tmp_path, url):
    import src.ai_interaction as ai
    from src import tool_execution as te

    class SM:
        def __init__(self):
            self.sessions = {"parent": types.SimpleNamespace(endpoint_url=url, model="m", headers=None, name="p")}

        def get_session(self, sid):
            return self.sessions.get(sid)

        def save_sessions(self):
            pass

    monkeypatch.setattr(ai, "get_session_manager", lambda: SM())
    monkeypatch.setattr(te, "get_active_workspace", lambda: str(tmp_path))
    monkeypatch.setattr(te, "get_active_workspace_roots", lambda: ())


async def test_delegate_agents_refuses_when_the_budget_is_spent(cfg, monkeypatch, tmp_path):
    from src.agent_tools import subagent_tools as st
    cfg(budget_gpu_daily_seconds=20)
    pb.record_usage("local", kind="local", gpu_seconds=30.0)
    _delegate_env(monkeypatch, tmp_path, "http://127.0.0.1:11434/v1")
    result = await st.DelegateAgentsTool().execute(
        json.dumps({"tasks": ["task one", "task two"]}), {"session_id": "parent", "owner": None})
    assert result["exit_code"] == 1 and "budget_paused" in result["error"]
    assert result["budget_paused"]["status"] == "budget_paused" and result["budget_paused"]["pause_until"]


async def test_delegate_agents_runs_fewer_workers_when_the_budget_only_allows_some(monkeypatch, tmp_path):
    import src.agent_loop as al
    from src.agent_tools import subagent_tools as st
    seen = []

    async def _loop(endpoint_url, model, messages, **kwargs):
        seen.append(messages[0]["content"])
        yield "data: " + json.dumps({"type": "harness_summary", "data": {"mutations": [], "stop_reason": "complete"}}) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_agent_loop", _loop)
    _delegate_env(monkeypatch, tmp_path, "http://127.0.0.1:11434/v1")
    answer = {"status": "budget_paused", "budget_paused": True, "reason": "pacing", "point": "delegate_agents"}
    monkeypatch.setattr(pb, "allowed_workers", lambda point, url, wanted, **k: (1, answer))

    async def cb(payload):
        pass
    result = await st.DelegateAgentsTool().execute(
        json.dumps({"tasks": [{"name": "A", "instruction": "task A"}, {"name": "B", "instruction": "task B"}]}),
        {"session_id": "parent", "owner": None, "progress_cb": cb})
    assert len(result["subagents"]) == 1 and result["dropped_tasks"] == 1
    assert len(seen) == 1 and "task A" in seen[0]


async def test_a_dispatched_job_is_not_gated_a_second_time_inside_the_tool(cfg, monkeypatch, tmp_path):
    import src.agent_loop as al
    from src.agent_tools import subagent_tools as st

    async def _loop(endpoint_url, model, messages, **kwargs):
        yield "data: " + json.dumps({"type": "harness_summary", "data": {"mutations": [], "stop_reason": "complete"}}) + "\n\n"
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_agent_loop", _loop)
    _delegate_env(monkeypatch, tmp_path, "http://127.0.0.1:11434/v1")
    calls = []
    monkeypatch.setattr(pb, "allowed_workers", lambda *a, **k: calls.append(a) or (0, {"reason": "x"}))

    async def cb(payload):
        pass
    result = await st.DelegateAgentsTool().execute(
        json.dumps({"tasks": ["t"]}), {"session_id": "parent", "owner": None, "period_budget_checked": True,
                                       "progress_cb": cb})
    assert calls == [] and "budget_paused" not in str(result.get("error", ""))
    assert len(result["subagents"]) == 1


# -- state, text and the route ---------------------------------------------------------------

def test_the_state_shows_each_window_its_pace_line_and_when_it_unpauses(cfg):
    cfg(budget_period_targets={"*": {"usd": 10}}, budget_gpu_daily_seconds=20)
    now = time.time()
    pb.record_usage("openrouter.ai", kind="openrouter", usd=10.5, tokens=7, ts=now)
    pb.record_usage("local", kind="local", gpu_seconds=3.0, ts=now)
    ub.note_status(HOSTED, 429, {"Retry-After": "300"}, now=now)
    s = pb.state(now)
    assert s["enabled"] is True and s["window"] == "week" and s["settings"]["budget_gpu_daily_seconds"] == 20
    row = s["providers"][0]
    assert row["provider"] == "*" and row["metric"] == "usd" and row["paused"] is True
    assert row["paused_until"] == pytest.approx(s["window_resets_at"]) and row["pace"]["allowed"] > 0
    assert s["gpu"]["used"] == pytest.approx(3.0) and s["gpu"]["target"] == 20
    assert s["hosted_seen"][0]["provider"] == "openrouter.ai"
    assert s["cooldowns"][0]["endpoint"] == "openrouter.ai" and s["breaker"]["open"] is False
    json.dumps(s)
    text = pb.render_text(s)
    assert "PAUSED" in text and "openrouter.ai" in text and "failure breaker closed" in text


def test_with_nothing_configured_the_state_says_so():
    s = pb.state()
    assert s["enabled"] is False and s["providers"] == [] and s["gpu"] is None
    assert "no target set" in pb.render_text(s)


def _client(monkeypatch, *, admin=True):
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    import routes.period_budget_routes as pr

    def who(request):
        user = getattr(request.state, "current_user", None)
        if not user:
            raise HTTPException(401, "not signed in")
        return user

    app = FastAPI()

    @app.middleware("http")
    async def stamp(request, call_next):
        request.state.current_user = request.headers.get("x-test-user") or ""
        return await call_next(request)

    monkeypatch.setattr(pr, "require_user", who)
    monkeypatch.setattr(pr, "effective_user", lambda request: getattr(request.state, "current_user", None) or None)
    monkeypatch.setattr(pr, "_auth_disabled", lambda: False)
    monkeypatch.setattr("src.tool_security.owner_is_admin_or_single_user", lambda owner: admin)
    app.include_router(pr.setup_period_budget_routes())
    return TestClient(app)


def test_the_period_route_needs_a_user_and_returns_the_state(cfg, monkeypatch):
    cfg(budget_gpu_daily_seconds=20)
    c = _client(monkeypatch)
    assert c.get("/api/budget/period").status_code == 401
    r = c.get("/api/budget/period", headers={"x-test-user": "luis"})
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True and body["gpu"]["target"] == 20 and "breaker" in body and "cooldowns" in body


def test_the_breaker_and_the_cooldowns_are_reset_by_an_admin_only(cfg, monkeypatch):
    cfg(unattended_failure_breaker=1)
    ub.record("dispatch", False, "boom")
    ub.note_status(HOSTED, 429, {"Retry-After": "300"})
    assert ub.is_open() and ub.cooldowns()
    plain = _client(monkeypatch, admin=False)
    assert plain.post("/api/budget/period/breaker/reset", headers={"x-test-user": "eve"}).status_code == 403
    assert ub.is_open()
    c = _client(monkeypatch)
    r = c.post("/api/budget/period/breaker/reset", headers={"x-test-user": "luis"})
    assert r.status_code == 200 and r.json()["breaker"]["open"] is False and not ub.is_open()
    r = c.post("/api/budget/period/cooldowns/clear", json={"endpoint": "openrouter.ai"}, headers={"x-test-user": "luis"})
    assert r.json()["cleared"] == 1 and r.json()["cooldowns"] == []


def test_the_period_route_is_readable_by_a_session_or_dispatch_token_but_reset_is_not():
    from core import authz
    assert authz.api_token_allowed("GET", "/api/budget/period", ["sessions"])[0]
    assert authz.api_token_allowed("GET", "/api/budget/period", ["agents:dispatch"])[0]
    assert not authz.api_token_allowed("GET", "/api/budget/period", ["chat"])[0]
    assert not authz.api_token_allowed("POST", "/api/budget/period/breaker/reset", ["sessions", "agents:dispatch"])[0]
# -- dispatch: the gate at job start ------------------------------------------------------------

from tests.test_dispatch import box, _client as _dispatch_client, _close_clients  # noqa: E402,F401
from src import dispatch as _dispatch_mod  # noqa: E402


@pytest.fixture(autouse=True)
def no_real_verification(monkeypatch):
    """A job's verify step would run the real command in the workspace (a whole
    pytest run for a spec that names one): these tests are about what happens
    BEFORE the workers start, so the verification is a stub."""
    monkeypatch.setattr(_dispatch_mod, "run_verification",
                        lambda *a, **k: {"ran": False, "ok": True, "summary": "skipped in this test"})


def test_dispatch_refuses_a_job_when_the_local_gpu_window_is_spent(box, cfg):
    from src import dispatch
    cfg(budget_gpu_daily_seconds=20)
    pb.record_usage("local", kind="local", gpu_seconds=40.0)
    with pytest.raises(pb.BudgetPaused) as err:
        asyncio.run(dispatch.start("luis", {"tasks": ["add apply_tax"], "workspace": box["ws"]}))
    info = err.value.info
    assert info["status"] == "budget_paused" and info["point"] == "dispatch" and info["pause_until"]
    assert not box["executed"]                       # nothing started, no worker was spent


def test_the_dispatch_route_answers_429_with_the_reason_and_a_retry_after(box, cfg, monkeypatch):
    cfg(budget_gpu_daily_seconds=20)
    pb.record_usage("local", kind="local", gpu_seconds=40.0)
    c = _dispatch_client(monkeypatch, token_scopes=["agents:dispatch"])
    r = c.post("/api/dispatch", json={"tasks": ["add apply_tax"], "workspace": box["ws"]})
    assert r.status_code == 429
    body = r.json()
    assert body["status"] == "budget_paused" and body["pause_until"] and body["reason"]
    assert int(r.headers["retry-after"]) >= 1


def test_an_interactive_turn_holds_an_unattended_job_but_never_a_manual_one(box, monkeypatch):
    from src import dispatch
    monkeypatch.setattr("src.interactive_gate._has_active_chat_stream", lambda: True)
    with pytest.raises(pb.BudgetPaused) as err:
        asyncio.run(dispatch.start("luis", {"tasks": ["x"], "workspace": box["ws"], "unattended": True}))
    assert err.value.info["kind"] == "interactive"

    async def manual():
        job = await dispatch.start("luis", {"tasks": ["x"], "workspace": box["ws"]})
        await dispatch.wait(job, 5)
        return job
    assert asyncio.run(manual()).status in ("done", "partial")


def test_an_open_breaker_refuses_unattended_jobs_with_breaker_open(box, cfg):
    from src import dispatch
    cfg(unattended_failure_breaker=1)
    ub.record("night_shift", False, "boom")
    with pytest.raises(pb.BudgetPaused) as err:
        asyncio.run(dispatch.start("luis", {"tasks": ["x"], "workspace": box["ws"], "unattended": True}))
    assert err.value.info["status"] == "breaker_open"


# -- night shift: between tasks ------------------------------------------------------------------

from tests.test_night_shift import (_FakeJob, _run_and_wait, data_dir, fake_dispatch)  # noqa: E402,F401
from src import night_shift as ns  # noqa: E402


async def test_a_night_shift_stops_cleanly_between_tasks_when_the_gpu_window_is_spent(data_dir, fake_dispatch, cfg):
    cfg(budget_gpu_daily_seconds=20)
    pb.record_usage("local", kind="local", gpu_seconds=30.0)
    fake_dispatch({"t1": _FakeJob("j1"), "t2": _FakeJob("j2")})
    shift = ns.start("u1", {"tasks": ["t1", "t2"], "workspace": "/tmp"})
    await _run_and_wait(shift["id"], "u1")
    final = ns.get("u1", shift["id"])
    assert final["state"] == "budget_paused" and final["results"] == []
    assert final["skipped"] == ["t1", "t2"]
    assert final["budget_paused"]["pause_until"] and final["budget_paused"]["reason"]
    assert "Paused by the period budget" in ns.report("u1", shift["id"])


async def test_a_night_shift_stops_when_the_pace_line_is_hit_mid_way(data_dir, monkeypatch, fake_dispatch):
    jobs = {"t1": _FakeJob("j1"), "t2": _FakeJob("j2"), "t3": _FakeJob("j3")}
    fake_dispatch(jobs)
    calls = {"n": 0}

    def gate(point, url=None, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return {"status": "budget_paused", "budget_paused": True, "kind": "pace", "reason": "pacing",
                "pause_until": time.time() + 600, "retry_after_s": 600.0, "point": point}
    monkeypatch.setattr(pb, "gate", gate)
    shift = ns.start("u1", {"tasks": ["t1", "t2", "t3"], "workspace": "/tmp"})
    await _run_and_wait(shift["id"], "u1")
    final = ns.get("u1", shift["id"])
    assert final["state"] == "budget_paused" and len(final["results"]) == 1 and final["skipped"] == ["t2", "t3"]


async def test_a_night_shift_waits_out_an_interactive_turn_instead_of_stopping(data_dir, monkeypatch, fake_dispatch):
    fake_dispatch({"t1": _FakeJob("j1")})
    answers = iter([{"status": "budget_paused", "budget_paused": True, "kind": "interactive", "reason": "chat",
                     "retry_after_s": 0.01}, None])
    monkeypatch.setattr(pb, "gate", lambda point, url=None, **kw: next(answers))
    real_sleep = asyncio.sleep
    waited = []

    async def quick_sleep(s):
        waited.append(s)
        await real_sleep(0)
    monkeypatch.setattr(ns.asyncio, "sleep", quick_sleep)
    shift = ns.start("u1", {"tasks": ["t1"], "workspace": "/tmp"})
    await _run_and_wait(shift["id"], "u1")
    final = ns.get("u1", shift["id"])
    assert final["state"] == "done" and len(final["results"]) == 1 and "waiting_for" not in final
    assert waited


async def test_the_shift_bodies_are_marked_unattended(data_dir, monkeypatch):
    import types as _t
    seen = []

    async def start(owner, body):
        seen.append(body)
        return _FakeJob("j1")
    from tests.test_night_shift import _patch_dispatch_module
    _patch_dispatch_module(monkeypatch, _t.SimpleNamespace(start=start, wait=lambda job, t: asyncio.sleep(0),
                                                           compact=lambda job: job.compact_payload()))
    shift = ns.start("u1", {"tasks": ["t1"], "workspace": "/tmp"})
    await _run_and_wait(shift["id"], "u1")
    assert seen and seen[0]["unattended"] is True


async def test_a_failing_night_shift_task_counts_toward_the_breaker(data_dir, monkeypatch, cfg):
    cfg(unattended_failure_breaker=2)

    async def boom(owner, body):
        raise ValueError("no route")
    from tests.test_night_shift import _patch_dispatch_module
    _patch_dispatch_module(monkeypatch, types.SimpleNamespace(start=boom, wait=lambda job, t: None, compact=lambda job: {}))
    shift = ns.start("u1", {"tasks": ["t1", "t2"], "workspace": "/tmp"})
    await _run_and_wait(shift["id"], "u1")
    assert ub.is_open()
    final = ns.get("u1", shift["id"])
    assert len(final["results"]) == 2 and all(r["status"] == "error" for r in final["results"])


async def test_the_night_shift_tool_reports_the_budget(cfg):
    from src.agent_tools import night_shift_tools as nst
    cfg(budget_gpu_daily_seconds=20)
    out = await nst.NightShiftTool().execute(json.dumps({"action": "budget"}), {"owner": "u1"})
    assert "### Period budget" in out["output"] and "local GPU/day" in out["output"]
    assert out["budget"]["gpu"]["target"] == 20


# -- the MCP tools (budget_period, and a refused dispatch_workers) --------------------------------------

def _ws(monkeypatch):
    from tests.test_dispatch import _load_workers_server
    return _load_workers_server(monkeypatch)


def test_the_mcp_server_lists_budget_period(monkeypatch):
    ws = _ws(monkeypatch)
    names = [t.name for t in ws.TOOLS]
    assert "budget_period" in names and len(names) == len(set(names))


def test_budget_period_shows_spend_against_the_pace_line_the_breaker_and_cooldowns(cfg, monkeypatch):
    ws = _ws(monkeypatch)
    cfg(budget_period_targets={"*": {"usd": 10}}, budget_gpu_daily_seconds=20, unattended_failure_breaker=2)
    now = time.time()
    pb.record_usage("openrouter.ai", kind="openrouter", usd=10.5, tokens=7, ts=now)
    pb.record_usage("local", kind="local", gpu_seconds=3.0, ts=now)
    ub.note_status(HOSTED, 429, {"Retry-After": "300"}, now=now)
    ub.record("dispatch", False, "x")
    ub.record("dispatch", False, "y")
    state = pb.state()
    monkeypatch.setattr(ws, "_request", lambda method, path, *a, **k: state)
    out = asyncio.run(ws.call_tool("budget_period", {}))[0].text
    assert out.startswith("period budget (week")
    assert "* usd/week: $10.50 of $10.00" in out and "PAUSED until" in out
    assert "local gpu_seconds/day: 3 s of 20 s" in out
    assert "failure breaker OPEN after 2 failures" in out
    assert "cooldown: openrouter.ai after HTTP 429" in out
    assert "spent on openrouter.ai: $10.50" in out


def test_budget_period_with_nothing_set_says_so(monkeypatch):
    ws = _ws(monkeypatch)
    state = pb.state()
    monkeypatch.setattr(ws, "_request", lambda *a, **k: state)
    assert "no target set" in asyncio.run(ws.call_tool("budget_period", {}))[0].text


def test_budget_period_is_read_only_through_the_mcp_server(monkeypatch):
    ws = _ws(monkeypatch)
    calls = []

    def fake(method, path, body=None, *a, **k):
        calls.append((method, path))
        return pb.state()
    monkeypatch.setattr(ws, "_request", fake)
    asyncio.run(ws.call_tool("budget_period", {"reset": "breaker"}))       # an unknown argument changes nothing
    assert calls == [("GET", "/api/budget/period")]
    tool = next(t for t in ws.TOOLS if t.name == "budget_period")
    assert tool.inputSchema["properties"] == {}


def test_a_dispatch_refused_with_429_says_when_to_come_back(monkeypatch):
    ws = _ws(monkeypatch)
    body = {"status": "budget_paused", "budget_paused": True, "reason": "local gpu_seconds/day: 200% of the target used",
            "pause_until_iso": "2026-10-03T00:00:00", "retry_after_s": 3600.0}

    def refuse(*a, **k):
        raise ws.FaustusHTTPError("Faustus answered HTTP 429", 429, body)
    monkeypatch.setattr(ws, "_request", refuse)
    out = asyncio.run(ws.call_tool("dispatch_workers", {"tasks": ["x"], "workspace": "D:/x"}))[0].text
    assert out.startswith("budget_paused: local gpu_seconds/day") and "pause_until: 2026-10-03T00:00:00" in out
    assert "nothing was started" in out
    opened = {"status": "breaker_open", "breaker_open": True, "reason": "5 unattended runs failed in a row"}

    def refuse_breaker(*a, **k):
        raise ws.FaustusHTTPError("Faustus answered HTTP 429", 429, opened)
    monkeypatch.setattr(ws, "_request", refuse_breaker)
    out = asyncio.run(ws.call_tool("dispatch_workers", {"tasks": ["x"], "workspace": "D:/x"}))[0].text
    assert out.startswith("breaker_open: 5 unattended runs failed in a row")