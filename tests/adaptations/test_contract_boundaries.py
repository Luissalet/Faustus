"""ADP-03 — regression tests for the cross-cutting guarantees no adaptation
(ADP-04..32) is allowed to break, no matter which module it touches.

Every test below is a FAKE: no network, no GPU, no real model endpoint, no
real filesystem outside `tmp_path`. Each docstring says exactly which
guarantee it protects and whether that guarantee holds TODAY:

  a) a session under `local_only` cannot end up on a remote endpoint by
     FALLBACK                                         -> holds (passing;
     closed by the integration lote -- see the section below)
  b) a late/cancelled run's result never reopens or overwrites the run
     that replaced it                                  -> holds (passing)
  c) a sealed exact-action approval for parameters A does not authorize
     parameters B                                       -> holds (passing)
  d) a subscription-backed endpoint (chatgpt_subscription/copilot) never
     silently falls back to a paid API on 401           -> holds (passing;
     the background/task half closed by the integration lote -- see that
     section below)

Where a guarantee does NOT hold today, the test is marked
`xfail(strict=True)` with the reason inline, per this lote's contract: a
test must never be quietly weakened to pass over a real gap. (As of the
integration lote, both gaps this file originally documented as xfail are
closed -- none remain in this file; a future adaptation that reopens one
should go back to that discipline rather than weaken the assertion.)
"""
from __future__ import annotations

import asyncio

import pytest


# ---------------------------------------------------------------------------
# a) local_only must never let a remote endpoint reach the fallback chain
# ---------------------------------------------------------------------------
#
# CLOSED (integration lote): `src.privacy_policy.assert_outbound` is now
# wired into the two places an ALTERNATE (fallback) endpoint gets chosen for
# the main model turn -- `src/foreground_model_routing.py::
# resolve_foreground_model_policy` (chat/agent foreground fallback) and
# `src/task_endpoint.py::resolve_task_candidates` (background task
# fallback), each dropping a non-local candidate under the `local_only`
# profile with a logged reason, never calling it. `routes/email_routes.py`'s
# utility fallback and the endpoint a session explicitly SELECTS (not
# reached by fallback) remain out of this test's scope -- the latter is
# `src/provider_policy.py::resolve_route`'s own invariant 1, already covered
# by `tests/test_adp22_provider_policy.py`.

def test_local_only_profile_drops_remote_fallback_from_foreground_policy(monkeypatch):
    """Guarantee (a), foreground half: a `local_only` session's Chat/Agent
    fallback chain (`src.foreground_model_routing.resolve_foreground_model_policy`)
    must never offer a remote candidate reached by FALLBACK (ADP-03
    criterion 1), even though the entry itself resolves fine.
    """
    import src.foreground_model_routing as foreground_model_routing
    from src import privacy_policy

    monkeypatch.setattr(
        foreground_model_routing, "_load_policy_preferences",
        lambda owner=None: {
            "foreground_fallback_enabled": True,
            "foreground_model_fallbacks": [{"endpoint_id": "remote-cloud", "model": "gpt-5"}],
        },
    )
    # The entry resolves fine at the endpoint-lookup layer -- the gate this
    # test proves lives ABOVE that layer, not inside it.
    monkeypatch.setattr(
        foreground_model_routing, "resolve_fallback_entries",
        lambda entries, owner=None, **kw: [
            ("https://api.remote-cloud.example/v1", "gpt-5", {"Authorization": "Bearer key"}),
        ],
    )
    monkeypatch.setattr(privacy_policy, "get_privacy_profile",
                        lambda *a, **kw: privacy_policy.PROFILE_LOCAL_ONLY)

    policy = foreground_model_routing.resolve_foreground_model_policy("alice")

    assert policy.fallback_candidates == (), (
        "a remote fallback candidate reached the policy under the "
        "local_only privacy profile"
    )


def test_local_only_profile_drops_remote_fallback_from_task_endpoint_chain(monkeypatch):
    """Guarantee (a), background-task half:
    `src.task_endpoint.resolve_task_candidates` must never offer a remote
    ALTERNATE endpoint under `local_only` (slot 1, the operator's own
    explicit Background Tasks endpoint choice, is intentionally out of this
    gate's scope -- see the module's own docstring).
    """
    import src.task_endpoint as task_endpoint
    from src import privacy_policy

    monkeypatch.setattr(task_endpoint, "resolve_task_endpoint",
                        lambda *a, **kw: (None, None, None))
    monkeypatch.setattr(
        task_endpoint, "resolve_endpoint",
        lambda kind, *a, **kw: {
            "utility": ("https://utility.remote.example/v1", "utility-model", {}),
            "default": ("https://default.remote.example/v1", "default-model", {}),
        }.get(kind, (None, None, None)),
    )
    monkeypatch.setattr(
        task_endpoint, "resolve_utility_fallback_candidates",
        lambda owner=None: [("https://chain.remote.example/v1", "chain-model", {})],
    )
    monkeypatch.setattr(privacy_policy, "get_privacy_profile",
                        lambda *a, **kw: privacy_policy.PROFILE_LOCAL_ONLY)

    candidates = task_endpoint.resolve_task_candidates(owner="alice")

    assert candidates == [], (
        "a remote alternate candidate reached resolve_task_candidates under "
        "the local_only privacy profile"
    )


# ---------------------------------------------------------------------------
# b) a late/cancelled run must never reopen or overwrite the run that
#    replaced it
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_agent_runs(tmp_path, monkeypatch):
    """Isolate src.agent_runs's module-level state (same pattern as
    tests/test_agent_run_log_replacement.py) so this file's runs never touch
    real DATA_DIR or bleed into another test's session ids."""
    from src import agent_runs
    import src.constants as consts

    monkeypatch.setattr(consts, "DATA_DIR", str(tmp_path / "data"), raising=False)
    agent_runs._RUNS.clear()
    agent_runs._LANES.clear()
    yield
    agent_runs._RUNS.clear()
    agent_runs._LANES.clear()


async def _blocking_gen(gate: asyncio.Event):
    await gate.wait()
    yield "data: [DONE]\n\n"


async def _quiesce(*runs):
    for r in runs:
        for t in (getattr(r, "task", None), getattr(r, "evict_task", None)):
            if t is not None and not t.done():
                t.cancel()
    await asyncio.sleep(0.01)


async def test_late_action_with_a_superseded_run_id_never_touches_the_replacement_run():
    """Guarantee (b): a late cancel/pause/steer carrying a SUPERSEDED run's
    id must never reopen or overwrite whatever run currently occupies that
    session (ADP-03 criterion 2). `src/agent_runs.py::start()` replaces
    `_RUNS[session_id]` synchronously and every mutation `stop`/
    `request_pause`/`queue_steer`/`queue_send_after` makes is gated on
    `expected_run_id` matching the CURRENT run -- a stale browser tab (or a
    delayed callback) holding the OLD run's id can act on nothing.
    """
    from src import agent_runs

    gate1, gate2 = asyncio.Event(), asyncio.Event()
    session_id = "adp03-late-action-test"

    run1 = agent_runs.start(session_id, _blocking_gen(gate1))
    await asyncio.sleep(0.02)
    old_run_id = run1.run_id
    assert agent_runs._RUNS[session_id] is run1

    # A second turn starts (e.g. the user sent another message) and replaces
    # run1 before it finished -- run1.task gets cancelled by start() itself.
    run2 = agent_runs.start(session_id, _blocking_gen(gate2))
    await asyncio.sleep(0.02)
    assert agent_runs._RUNS[session_id] is run2
    assert run2.run_id != old_run_id
    assert run2.status == "running"

    # Every late action still carrying the OLD run's id must be refused...
    assert agent_runs.stop(session_id, expected_run_id=old_run_id) is False
    assert agent_runs.request_pause(session_id, expected_run_id=old_run_id) is False
    assert agent_runs.queue_steer(session_id, "late text", expected_run_id=old_run_id) is False
    assert agent_runs.queue_send_after(session_id, "late text", expected_run_id=old_run_id) is False

    # ...and none of them touched run2's live state.
    assert agent_runs._RUNS[session_id] is run2
    assert run2.status == "running"
    assert run2.pause_requested is False
    assert run2.steer_queue == []
    assert run2.send_after_queue == []

    # Sanity: the CURRENT run's own id still works normally through the same
    # gate -- this is a targeted refusal of the stale id, not a broken gate.
    assert agent_runs.request_pause(session_id, expected_run_id=run2.run_id) is True
    agent_runs.take_pause_request(session_id)  # drain it back to False, tidy

    gate1.set()
    gate2.set()
    await asyncio.sleep(0.02)
    await _quiesce(run1, run2)


# ---------------------------------------------------------------------------
# c) an approval sealed for parameters A must not authorize parameters B
# ---------------------------------------------------------------------------

def test_exact_approval_sealed_for_one_action_does_not_authorize_different_content():
    """Guarantee (c): an approval the server sealed for one EXACT action
    (tool + content + workspace + ...) must not validate a different set of
    parameters for the same tool/session/owner (ADP-03 criterion 3).
    `src/tool_approvals.py::ExactToolApproval.matches`/`claim` recompute the
    canonical digest over the CANDIDATE content and compare it to the
    digest sealed at creation time -- a changed `content` changes the
    digest, so a different action can never ride on an old approval.
    """
    from src.tool_approvals import ToolApprovalStore
    from src.tool_approval_scopes import TASK_APPROVAL_DECISION
    from src.tool_capabilities import capabilities_for_action

    store = ToolApprovalStore()
    content_a = "echo A"
    content_b = "echo B"
    capabilities = capabilities_for_action("bash", content_a)

    pending = store.create(
        owner="alice",
        session_id="s1",
        origin_run_id="r1",
        tool_name="bash",
        content=content_a,
        workspace="/tmp/ws",
        external_untrusted_context_seen=True,
        capabilities=capabilities,
    )
    exact = store.consume(
        pending.approval_id,
        decision=TASK_APPROVAL_DECISION,
        owner="alice",
        session_id="s1",
    )
    assert exact is not None

    # The exact params it was sealed for still match (sanity: the gate works
    # at all before we test that it refuses a mismatch).
    assert exact.matches(
        owner="alice", session_id="s1", tool_name="bash",
        content=content_a, workspace="/tmp/ws",
    )

    # Different content (parameters B) for the SAME tool/owner/session must
    # NOT match -- the approval was sealed for A, not for "this tool, ever".
    assert exact.matches(
        owner="alice", session_id="s1", tool_name="bash",
        content=content_b, workspace="/tmp/ws",
    ) is False
    assert exact.claim(
        owner="alice", session_id="s1", tool_name="bash",
        content=content_b, workspace="/tmp/ws",
    ) is False

    # Claiming B must not have consumed the approval either -- A can still
    # claim it exactly once.
    assert exact.claim(
        owner="alice", session_id="s1", tool_name="bash",
        content=content_a, workspace="/tmp/ws",
    ) is True
    # And now it is consumed: even the ORIGINAL content can't claim twice.
    assert exact.claim(
        owner="alice", session_id="s1", tool_name="bash",
        content=content_a, workspace="/tmp/ws",
    ) is False


# ---------------------------------------------------------------------------
# d) a subscription endpoint (chatgpt_subscription/copilot) must never fall
#    back to a paid API on a 401
# ---------------------------------------------------------------------------

def _fake_401(message="unauthorized"):
    err = RuntimeError(message)
    err.status_code = 401
    return err


async def test_foreground_route_fallback_never_advances_past_a_401(monkeypatch):
    """Guarantee (d), the half that HOLDS today: the foreground chat/agent
    fallback path (`src/llm_core.py::llm_call_async_with_route_fallback`,
    driven by `src/foreground_model_routing.py`'s
    `FOREGROUND_AVAILABILITY_STATUSES`) deliberately excludes 401/403 from
    its eligible-for-fallback status set, so a subscription-backed
    candidate's auth failure is never silently retried against the next
    (potentially paid) candidate -- it propagates instead.
    """
    from src import llm_core
    from src.foreground_model_routing import FOREGROUND_AVAILABILITY_STATUSES

    assert 401 not in FOREGROUND_AVAILABILITY_STATUSES
    assert 403 not in FOREGROUND_AVAILABILITY_STATUSES

    calls = []

    async def fake_llm_call_async(url, model, messages, **kwargs):
        calls.append(url)
        raise _fake_401()

    monkeypatch.setattr(llm_core, "llm_call_async", fake_llm_call_async)
    candidates = [
        ("https://subscription.example/v1", "gpt-5-subscription", {}),
        ("https://paid.example/v1", "gpt-5-paid", {}),
    ]
    with pytest.raises(RuntimeError):
        await llm_core.llm_call_async_with_route_fallback(
            candidates,
            messages=[],
            fallback_statuses=FOREGROUND_AVAILABILITY_STATUSES,
        )
    assert calls == ["https://subscription.example/v1"], (
        "must never have reached the paid candidate after a 401"
    )


# CLOSED (integration lote): `src.llm_core.llm_call_async_with_fallback` --
# the function `src.task_endpoint.task_llm_call_async` uses for EVERY
# background-task LLM call, including the utility fallback chain, and
# `routes/email_routes.py` also uses directly -- now re-raises immediately
# on a 401/403 (`llm_core._NEVER_FALLBACK_STATUSES`) instead of advancing to
# the next candidate, the same status pair the FOREGROUND chat path already
# excludes via `FOREGROUND_AVAILABILITY_STATUSES` (see the passing sibling
# test above).

async def test_background_task_fallback_does_not_silently_move_from_subscription_to_paid_api_on_401(monkeypatch):
    """Guarantee (d), background/task half: the background/task fallback
    chain must not treat a subscription endpoint's 401 the same as a
    transient 503 and silently continue to a paid candidate -- calling the
    chain with a 401 on the first (subscription) candidate must raise, not
    return the paid candidate's answer.
    """
    from src import llm_core

    calls = []

    async def fake_llm_call_async(url, model, messages, **kwargs):
        calls.append(url)
        if url == "https://subscription.example/v1":
            raise _fake_401()
        return "paid api answered"

    monkeypatch.setattr(llm_core, "llm_call_async", fake_llm_call_async)
    candidates = [
        ("https://subscription.example/v1", "gpt-5-subscription", {}),
        ("https://paid.example/v1", "gpt-5-paid", {}),
    ]
    with pytest.raises(RuntimeError):
        await llm_core.llm_call_async_with_fallback(candidates, messages=[])
    assert calls == ["https://subscription.example/v1"], (
        "must never have reached the paid candidate after a 401"
    )


async def test_background_task_fallback_still_advances_past_a_transient_503(monkeypatch):
    """Sanity companion to the 401 test above: this is a targeted refusal of
    auth failures specifically, not a broken fallback chain in general -- a
    transient 503 on the first candidate must still fall through to the
    second, exactly as before this lote.
    """
    from src import llm_core

    calls = []

    async def fake_llm_call_async(url, model, messages, **kwargs):
        calls.append(url)
        if url == "https://flaky.example/v1":
            err = RuntimeError("service unavailable")
            err.status_code = 503
            raise err
        return "second candidate answered"

    monkeypatch.setattr(llm_core, "llm_call_async", fake_llm_call_async)
    candidates = [
        ("https://flaky.example/v1", "model-a", {}),
        ("https://backup.example/v1", "model-b", {}),
    ]
    result = await llm_core.llm_call_async_with_fallback(candidates, messages=[])
    assert result == "second candidate answered"
    assert calls == ["https://flaky.example/v1", "https://backup.example/v1"]
