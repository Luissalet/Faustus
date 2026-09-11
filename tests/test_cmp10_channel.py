"""tests/test_cmp10_channel.py — CMP-10 (INFORME_COMPARATIVO_V2.md §3.9):
`src/desktop_semantics/channel.py::choose_channel` and its wiring into
`desktop_act` (`src/agent_tools/desktop_semantic_tools.py`).

Acceptance cases covered:
  1. A target whose semantic channel is available gets it, no risk change.
  2. A target with no semantic channel falls back to `pixels` — visible
     (`risk_change`/`requires_visible_fallback` True), never silent.
  3. `choose_channel` never invents a channel nothing offered as usable —
     `BackendUnavailableError` when the candidate list is empty.
  4. `desktop_act` refuses (rather than acting blind) when its only channel
     is `native_a11y` and that is unavailable, and makes the fallback
     visible: one audit-trail entry with `channel_fallback: true`, and the
     session's ref generation is invalidated.
  5. `desktop_act` reports the channel it used on a normal, successful call.
  6. `session.invalidate_generation` bumps the generation and drops
     snapshot memory even with nothing recorded yet (idempotent).

Everything here runs against `fake_backend.py` / in-memory state — no real
desktop, no network, per the lote contract.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import src.agent_tools as agent_tools  # noqa: F401 - resolves circular schema imports first
from src import desktop_semantics as ds
from src.desktop_semantics import channel as ds_channel
from src.desktop_semantics.fake_backend import FakeDesktopBackend
from src.agent_tools import desktop_semantic_tools as dst
from src import desktop_control_session as control


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_state():
    ds.reset_state()
    control.reset_desk01_state()
    yield
    ds.reset_state()
    control.reset_desk01_state()


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    values = {"desktop_control_mode": "ask_each"}

    def _get(key, default=None):
        return values.get(key, default)

    import src.tool_capabilities as tc

    monkeypatch.setattr(tc, "get_setting", _get, raising=False)
    return values


# ---------------------------------------------------------------------------
# choose_channel: pure decision logic
# ---------------------------------------------------------------------------

def test_desktop_target_uses_native_a11y_when_available_no_risk_change():
    decision = ds_channel.choose_channel(
        {"kind": "desktop"},
        capabilities={"native_a11y": True, "pixels": True},
        policy={},
    )
    assert decision.channel == "native_a11y"
    assert decision.risk_change is False
    assert decision.requires_visible_fallback is False


def test_desktop_target_falls_back_to_pixels_visibly_when_native_a11y_unavailable():
    decision = ds_channel.choose_channel(
        {"kind": "desktop"},
        capabilities={"native_a11y": False, "pixels": True},
        policy={},
    )
    assert decision.channel == "pixels"
    assert decision.risk_change is True
    assert decision.requires_visible_fallback is True
    assert "risk change" in decision.reason


def test_api_target_prefers_app_api_over_native_a11y():
    decision = ds_channel.choose_channel(
        {"kind": "api"},
        capabilities={"app_api": True, "native_a11y": True, "pixels": True},
        policy={},
    )
    assert decision.channel == "app_api"
    assert decision.risk_change is False


def test_browser_target_never_offered_native_a11y_ahead_of_dom_cdp():
    decision = ds_channel.choose_channel(
        {"kind": "browser"},
        capabilities={"dom_cdp": True, "native_a11y": True, "pixels": True},
        policy={},
    )
    assert decision.channel == "dom_cdp"


def test_no_usable_channel_raises_rather_than_inventing_one():
    with pytest.raises(ds.BackendUnavailableError):
        ds_channel.choose_channel(
            {"kind": "desktop"},
            capabilities={"native_a11y": False, "pixels": False},
            policy={},
        )


def test_policy_can_forbid_pixels_entirely():
    with pytest.raises(ds.BackendUnavailableError):
        ds_channel.choose_channel(
            {"kind": "desktop"},
            capabilities={"native_a11y": False, "pixels": True},
            policy={"allow_pixels": False},
        )


def test_preferred_channel_only_wins_when_it_is_a_candidate_for_this_kind():
    # dom_cdp is not a candidate for an "api" target at all -- `preferred`
    # cannot smuggle it in.
    decision = ds_channel.choose_channel(
        {"kind": "api"},
        capabilities={"app_api": True, "dom_cdp": True, "pixels": True},
        policy={},
        preferred="dom_cdp",
    )
    assert decision.channel == "app_api"


# ---------------------------------------------------------------------------
# session.invalidate_generation
# ---------------------------------------------------------------------------

def test_invalidate_generation_bumps_and_drops_snapshots():
    snap = ds.take_snapshot("s1", {"app": "A", "window": "W", "elements": []})
    gen_before = snap.generation
    new_gen = ds.invalidate_generation("s1")
    assert new_gen == gen_before + 1
    assert ds.current_generation("s1") == new_gen
    assert ds.get_snapshot("s1", snap.snapshot_id) is None
    assert ds.latest_snapshot("s1") is None


def test_invalidate_generation_is_idempotent_on_a_fresh_session():
    first = ds.invalidate_generation("never-seen")
    second = ds.invalidate_generation("never-seen")
    assert second == first + 1


# ---------------------------------------------------------------------------
# desktop_act wiring: channel returned, fallback made visible, refs invalidated
# ---------------------------------------------------------------------------

def test_desktop_act_reports_the_channel_it_used_on_success(monkeypatch, tmp_path):
    monkeypatch.setattr(control, "RUNTIME", tmp_path)
    backend = FakeDesktopBackend()
    backend.semantic().add_control("button", "Save", automation_id="btnSave")
    monkeypatch.setattr(dst, "get_backend", lambda: backend)

    _, snap_result = _run(dst.DesktopSnapshotTool().execute("{}", {"session_id": "s1"}))
    ref = snap_result["elements"][0]["ref"]

    desc, result = _run(dst.DesktopActTool().execute(
        json.dumps({"ref": ref, "op": "invoke"}), {"session_id": "s1"}
    ))
    assert result["exit_code"] == 0
    assert result["channel"] == "native_a11y"


def test_desktop_act_refuses_and_invalidates_refs_when_native_a11y_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(control, "RUNTIME", tmp_path)
    backend = FakeDesktopBackend()
    backend.semantic().add_control("button", "Save", automation_id="btnSave")
    monkeypatch.setattr(dst, "get_backend", lambda: backend)

    # A ref taken while semantic support was up...
    _, snap_result = _run(dst.DesktopSnapshotTool().execute("{}", {"session_id": "s1"}))
    ref = snap_result["elements"][0]["ref"]
    gen_before = ds.current_generation("s1")

    # ...then the backend loses semantic support (e.g. UIA became unavailable
    # mid-session) before the act call.
    monkeypatch.setattr(backend.semantic(), "available", lambda: (False, "UIA unavailable"))

    desc, result = _run(dst.DesktopActTool().execute(
        json.dumps({"ref": ref, "op": "invoke"}), {"session_id": "s1"}
    ))
    assert result["exit_code"] == 1
    assert "pixels" in result["error"]
    assert result["channel"] == "pixels"  # the channel it landed on, even in refusal
    assert backend.semantic().invocations == []  # never actually acted

    # The fallback (native_a11y -> pixels) was made visible in the SAME
    # audit trail desktop_act evidence already uses.
    entries = control.audit_log("s1")
    fallback_entries = [e for e in entries if e["tool"] == "desktop_channel:decision"]
    assert len(fallback_entries) == 1
    payload = json.loads(fallback_entries[0]["note"])
    assert payload["channel_fallback"] is True
    assert payload["channel"] == "pixels"

    # And the ref's generation was invalidated -- a fresh desktop_snapshot
    # is required before anything can act on this session again.
    assert ds.current_generation("s1") == gen_before + 1
    assert ds.latest_snapshot("s1") is None
