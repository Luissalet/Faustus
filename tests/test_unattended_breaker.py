"""src/unattended_breaker.py -- the failure breaker and the provider cooldown.

Adapted in spirit from an MIT-licensed agent farm that pauses a worker for 15
minutes after a provider 429 and stops launching after a run of failures.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

import src.llm_core as llm_core
from src import unattended_breaker as ub
from tests.test_llm_core_retries import _Response, _call, _fast_sleep, _wire_sequence

HOSTED = "https://api.example-provider.test/v1/chat/completions"
LOCAL = "http://127.0.0.1:8081/v1/chat/completions"


@pytest.fixture
def cfg(monkeypatch):
    values = {}
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: values.get(key, default))

    def setter(**kw):
        values.update(kw)
    return setter


@pytest.fixture
def notices(monkeypatch):
    sent = []
    from src import notifications
    monkeypatch.setattr(notifications, "emit", lambda kind, **kw: sent.append((kind, kw)))
    return sent


# -- the breaker ---------------------------------------------------------------------------

def test_the_breaker_opens_after_the_configured_run_of_failures(cfg, notices):
    cfg(unattended_failure_breaker=3)
    now = 1000.0
    for i in range(2):
        ub.record("dispatch", False, f"fail {i}", now=now)
    assert ub.check(now=now) is None and not ub.is_open(now=now)
    st = ub.record("night_shift", False, "third", now=now)
    assert st["open"] is True and st["consecutive_failures"] == 3 and st["opened_count"] == 1
    blocked = ub.check(now=now)
    assert blocked["status"] == "breaker_open" and blocked["breaker_open"] is True
    assert blocked["pause_until"] == pytest.approx(now + 30 * 60) and blocked["retry_after_s"] > 0
    assert "third" in blocked["reason"] and "3 unattended runs failed in a row" in blocked["reason"]


def test_a_success_resets_the_run_of_failures(cfg):
    cfg(unattended_failure_breaker=3)
    ub.record("dispatch", False, "a")
    ub.record("dispatch", False, "b")
    ub.record("scheduled_task", True)
    ub.record("dispatch", False, "c")
    ub.record("dispatch", False, "d")
    assert not ub.is_open() and ub.status()["consecutive_failures"] == 2


def test_the_owner_is_told_once_through_the_notification_bus(cfg, notices):
    cfg(unattended_failure_breaker=2)
    for _ in range(5):
        ub.record("dispatch", False, "same problem")
    opened = [n for n in notices if n[0] == "unattended_breaker_open"]
    assert len(opened) == 1
    body = opened[0][1]["body"]
    assert "2 unattended runs failed in a row" in body and "dispatch" in body


def test_the_breaker_closes_by_itself_after_its_cooldown(cfg, notices):
    cfg(unattended_failure_breaker=1, unattended_breaker_cooldown_min=10)
    now = 5000.0
    ub.record("dispatch", False, "boom", now=now)
    assert ub.is_open(now=now + 599)
    assert ub.check(now=now + 601) is None
    st = ub.status(now=now + 601)
    assert st["open"] is False and st["consecutive_failures"] == 0 and st["auto_closed_at"] == pytest.approx(now + 601)
    # and it can open (and notify) again later
    ub.record("dispatch", False, "again", now=now + 700)
    assert ub.is_open(now=now + 700) and len([n for n in notices if n[0] == "unattended_breaker_open"]) == 2


def test_a_manual_reset_closes_it_now(cfg, notices):
    cfg(unattended_failure_breaker=1)
    ub.record("dispatch", False, "boom")
    assert ub.is_open()
    st = ub.reset()
    assert st["open"] is False and st["reset_at"] and st["consecutive_failures"] == 0
    assert ub.check() is None


def test_zero_turns_the_breaker_off(cfg):
    cfg(unattended_failure_breaker=0)
    for _ in range(50):
        ub.record("dispatch", False, "boom")
    assert ub.check() is None and ub.status()["enabled"] is False


def test_the_default_threshold_is_five(cfg):
    assert ub.threshold() == 5 and ub.cooldown_minutes() == 30
    for _ in range(4):
        ub.record("dispatch", False, "x")
    assert not ub.is_open()
    ub.record("dispatch", False, "x")
    assert ub.is_open()


def test_the_breaker_state_survives_a_restart(cfg):
    cfg(unattended_failure_breaker=1)
    ub.record("dispatch", False, "boom")
    ub._LOADED = False                       # a new process: nothing in memory, the file on disk
    ub._BREAKER.update(consecutive=0, open_since=None, open_until=None)
    assert ub.is_open()


def test_a_finished_unattended_dispatch_job_feeds_the_breaker(cfg):
    from src import dispatch
    cfg(unattended_failure_breaker=2)

    class Job:
        def __init__(self, status, unattended=True, result=None, error=None):
            self.status, self.unattended, self.result, self.error, self.verdict = status, unattended, result, error, ""
            self.id = "j"
    dispatch._note_breaker(Job("error", error="model crashed"))
    assert ub.status()["consecutive_failures"] == 1
    dispatch._note_breaker(Job("cancelled"))
    dispatch._note_breaker(Job("interrupted"))
    assert ub.status()["consecutive_failures"] == 1               # neither a failure nor a success
    dispatch._note_breaker(Job("done"))
    assert ub.status()["consecutive_failures"] == 0
    dispatch._note_breaker(Job("error", unattended=False, error="mine"))
    assert ub.status()["consecutive_failures"] == 0               # a job the owner started never counts


def test_a_local_server_answering_garbage_counts_as_a_failure(cfg, monkeypatch, tmp_path):
    from src import engine_swap, model_server_heal as heal
    cfg(unattended_failure_breaker=1)
    heal._INFLIGHT.clear()
    heal._LAST_RESTART.clear()
    monkeypatch.setattr(heal.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(heal, "_settings", lambda: {"enabled": True, "commands": [], "timeout_s": 5.0})
    monkeypatch.setattr(engine_swap, "restartable_engine_for_url", lambda url: None)

    async def insane(url, model, timeout_s=30.0):
        return False                                    # "////"
    monkeypatch.setattr(engine_swap, "generates_sanely", insane)
    monkeypatch.setattr(heal, "listener_pid", lambda port: 4242)
    monkeypatch.setattr(heal, "supervisor_of", lambda pid: {"pid": 7, "name": "powershell.exe", "cmdline": "x"})

    async def end(pid):
        return True

    async def back(url, model, port, timeout_s):
        return True
    monkeypatch.setattr(heal, "_end_process", end)
    monkeypatch.setattr(heal, "_wait_back", back)
    out = asyncio.run(heal.heal("http://127.0.0.1:8081/v1", "m"))
    assert out["action"] == "restarted"
    st = ub.status()
    assert st["open"] is True and st["last_failure"]["kind"] == "local_garbage"
    heal._INFLIGHT.clear()
    heal._LAST_RESTART.clear()

# -- the cooldown ---------------------------------------------------------------------------

def test_a_429_cools_the_endpoint_for_its_retry_after():
    now = 100.0
    entry = ub.note_status(HOSTED, 429, {"Retry-After": "90"}, now=now)
    assert entry["endpoint"] == "api.example-provider.test" and entry["until"] == pytest.approx(190.0)
    assert entry["honoured_retry_after"] is True
    assert ub.cooldown_for(HOSTED, now=150.0) is not None
    assert ub.cooldown_for(HOSTED, now=191.0) is None                 # and it expires on its own


def test_without_a_retry_after_the_cooldown_is_fifteen_minutes(cfg):
    entry = ub.note_status(HOSTED, 429, {}, now=0.0)
    assert entry["until"] == pytest.approx(15 * 60.0) and entry["honoured_retry_after"] is False
    cfg(provider_cooldown_default_min=5)
    assert ub.note_status("https://other.example-provider.test/v1", 429, {}, now=0.0)["until"] == pytest.approx(300.0)


def test_with_the_default_off_only_an_explicit_retry_after_cools(cfg):
    cfg(provider_cooldown_default_min=0)
    assert ub.note_status(HOSTED, 429, {}, now=0.0) is None
    assert ub.note_status(HOSTED, 429, {"Retry-After": "30"}, now=0.0)["until"] == pytest.approx(30.0)


def test_overload_statuses_cool_but_ordinary_errors_do_not():
    assert ub.note_status(HOSTED, 529, {}, now=0.0) is not None
    ub.reset_for_tests()
    for status in (400, 401, 404, 500, 502, None):
        assert ub.note_status(HOSTED, status, {}, now=0.0) is None
    # a bare 503 is how a connect failure is reported inside llm_core; with Retry-After it is an overload
    assert ub.note_status(HOSTED, 503, {}, now=0.0) is None
    assert ub.note_status(HOSTED, 503, {"Retry-After": "10"}, now=0.0) is not None


def test_a_server_on_this_machine_is_never_cooled_down():
    for url in (LOCAL, "http://localhost:11434/v1", "http://[::1]:8080/v1"):
        assert ub.note_status(url, 429, {"Retry-After": "600"}, now=0.0) is None
    assert ub.cooldowns(0.0) == []


def test_another_credential_at_the_same_host_has_its_own_limit():
    ub.note_status(HOSTED, 429, {}, request_headers={"Authorization": "Bearer key-A"}, now=0.0)
    assert ub.cooldown_for(HOSTED, {"Authorization": "Bearer key-A"}, now=10.0) is not None
    assert ub.cooldown_for(HOSTED, {"Authorization": "Bearer key-B"}, now=10.0) is None
    assert ub.cooldown_for(HOSTED, None, now=10.0) is not None
    row = ub.cooldowns(10.0)[0]
    assert "credential" not in row and "key-A" not in json.dumps(row)    # the secret never shows


def test_repeated_429s_extend_the_cooldown_and_count_the_hits():
    ub.note_status(HOSTED, 429, {"Retry-After": "60"}, now=0.0)
    again = ub.note_status(HOSTED, 429, {"Retry-After": "120"}, now=30.0)
    assert again["hits"] == 2 and again["until"] == pytest.approx(150.0) and again["since"] == 0.0


def test_the_cooldown_list_and_clear():
    ub.note_status(HOSTED, 429, {"Retry-After": "60"}, now=0.0)
    ub.note_status("https://b.example-provider.test/v1", 429, {"Retry-After": "90"}, now=0.0)
    rows = ub.cooldowns(10.0)
    assert [r["endpoint"] for r in rows] == ["api.example-provider.test", "b.example-provider.test"]
    assert rows[0]["remaining_s"] == pytest.approx(50.0) and rows[0]["pause_until"] == pytest.approx(60.0)
    assert ub.clear_cooldown("https://b.example-provider.test/v1") == 1
    assert ub.clear_cooldown() == 1 and ub.cooldowns(10.0) == []


def test_a_fallback_chain_skips_a_cooled_endpoint_while_another_exists():
    ub.note_status(HOSTED, 429, {"Retry-After": "600"})
    chain = [(HOSTED, "m1", {}), ("https://backup.example-provider.test/v1/chat/completions", "m2", {}),
             (LOCAL, "m3", {})]
    kept = ub.filter_candidates(chain)
    assert [c[1] for c in kept] == ["m2", "m3"]
    kept, descs = ub.filter_candidates(chain, [{"id": 1}, {"id": 2}, {"id": 3}])
    assert [c[1] for c in kept] == ["m2", "m3"] and descs == [{"id": 2}, {"id": 3}]


def test_when_every_endpoint_is_cooled_the_chain_is_left_alone():
    ub.note_status(HOSTED, 429, {"Retry-After": "600"})
    assert ub.filter_candidates([(HOSTED, "m1", {})]) == [(HOSTED, "m1", {})]


def test_the_model_chain_builder_drops_a_cooled_endpoint():
    ub.note_status(HOSTED, 429, {"Retry-After": "600"})
    chain = [(HOSTED, "m1", {"Authorization": "Bearer k"}),
             ("https://backup.example-provider.test/v1/chat/completions", "m2", {"Authorization": "Bearer k2"})]
    kept, descs = llm_core._dedupe_model_candidates_with_descriptors(chain, [{}, {}])
    assert [c[1] for c in kept] == ["m2"] and len(descs) == 1


# -- a fake endpoint that always answers 429 / garbage -------------------------------------------

def test_a_provider_that_always_429s_ends_up_in_cooldown(monkeypatch):
    sleeps = _fast_sleep(monkeypatch)
    url = "https://api.always-429.test/v1/chat/completions"
    calls = _wire_sequence(monkeypatch, [_Response(429, headers={"Retry-After": "45"})] * 3)
    with pytest.raises(llm_core.HTTPException) as err:
        _call(monkeypatch, url, max_retries=3)
    assert err.value.status_code == 429 and len(calls) == 3
    cd = ub.cooldown_for(url)
    assert cd is not None and cd["status"] == 429
    assert cd["until"] - time.time() == pytest.approx(45.0, abs=3.0)
    from src import period_budget
    assert period_budget.gate("dispatch", url)["kind"] == "cooldown"
    assert period_budget.state()["cooldowns"][0]["endpoint"] == "api.always-429.test"


def test_a_429_that_recovers_inside_the_retries_is_not_a_cooldown(monkeypatch):
    _fast_sleep(monkeypatch)
    url = "https://api.recovers.test/v1/chat/completions"
    _wire_sequence(monkeypatch, [_Response(429), _Response(200)])
    assert _call(monkeypatch, url, max_retries=3) == "ok"
    assert ub.cooldown_for(url) is None


def test_a_streaming_429_is_remembered_by_the_trace_record_of_the_same_call():
    from src import llm_trace
    url = "https://api.stream-429.test/v1/chat/completions"
    ub.note_pending_status(429, {"Retry-After": "20"})
    llm_trace.record_call(session_id=None, endpoint_url=url, model="m", error="HTTP 429")
    cd = ub.cooldown_for(url)
    assert cd is not None and cd["status"] == 429
    assert ub.take_pending_status() is None                  # consumed: it does not leak into the next call


def test_a_streaming_status_that_is_not_a_rate_limit_is_not_kept():
    ub.note_pending_status(500, {})
    assert ub.take_pending_status() is None


def test_a_streaming_call_that_always_429s_cools_its_endpoint():
    """The real streaming path: the retry decision sees the final 429 and the
    trace record of the same call, which knows the URL, books the cooldown."""
    from src import llm_trace
    from src.retry_policy import RetryBudget
    url = "https://api.stream-always-429.test/v1/chat/completions"
    should_retry, _, _, _ = llm_core._stream_retry_decision(
        status=429, headers={"Retry-After": "40"}, attempt=3, max_retries=3,
        budget=RetryBudget(30.0), delta_emitted=False)
    assert should_retry is False                                   # attempts used up: this is the final answer
    llm_trace.record_call(session_id=None, endpoint_url=url, model="m", error="HTTP 429")
    cd = ub.cooldown_for(url)
    assert cd is not None and cd["until"] - time.time() == pytest.approx(40.0, abs=3.0)


def test_a_streaming_429_that_will_be_retried_is_not_a_cooldown_yet():
    from src.retry_policy import RetryBudget
    should_retry, _, _, _ = llm_core._stream_retry_decision(
        status=429, headers={"Retry-After": "1"}, attempt=1, max_retries=3,
        budget=RetryBudget(30.0), delta_emitted=False)
    assert should_retry is True and ub.take_pending_status() is None
