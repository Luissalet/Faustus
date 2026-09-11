"""ADP-08 (contrato de escritorio semántico) + ADP-09 (backend Windows UIA
opcional).

Acceptance cases from the ADP-08/ADP-09 fichas (`BACKLOG_FAUSTUS.md`),
covered here:

  ADP-08 #1  "Una ref de otra sesión o generación es rechazada."
             -> test_resolve_rejects_ref_from_a_different_session
             -> test_resolve_rejects_a_ref_after_the_window_changed
  ADP-08 #2  "Una acción ambigua no se ejecuta."
             -> test_resolve_raises_ambiguous_when_several_controls_match
             -> test_act_does_not_execute_when_resolution_is_ambiguous
  ADP-08 #3  "Timeout distingue acción no entregada de resultado desconocido."
             -> test_act_reports_unknown_on_timeout_and_never_retries
             -> test_act_reports_not_delivered_when_backend_says_so
  ADP-09 #1  "Aplicación de prueba con controles duplicados no recibe clic
             incorrecto."
             -> test_resolve_picks_the_exact_duplicate_by_structural_path
  ADP-09 #2  "Cambios de ventana y controles eliminados degradan a error
             claro."
             -> test_resolve_rejects_a_ref_after_the_window_changed
             -> test_resolve_degrades_cleanly_when_the_control_is_deleted
  ADP-09 #3  "Arranque sin pywinauto o en otro SO sigue funcionando."
             -> test_windows_uia_backend_reports_unavailable_on_linux
             -> test_package_and_windows_uia_module_import_cleanly_on_linux
  ADP-09 #4  "Tests físicos y simulados quedan identificados por separado."
             -> module docstring of windows_uia.py states physical Windows
             verification as a DECLARED PENDING; every test in this file
             runs against `fake_backend.py`, never a real Windows desktop.

Registration coherence (same gap `tests/test_tool_index_schema_parity.py`/
`tests/test_l54_tool_wiring.py` guard for every other tool) is covered at
the bottom of this file.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import src.agent_tools as agent_tools  # noqa: F401 - resolves circular schema imports first
from src import desktop_semantics as ds
from src.desktop_semantics.fake_backend import FakeDesktopBackend, FakeDesktopSemanticBackend
from src.agent_tools import desktop_semantic_tools as dst
from src.agent_tools import desktop_tools as dt
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


# ---------------------------------------------------------------------------
# contracts.py: Ref format
# ---------------------------------------------------------------------------

def test_ref_format_and_parse_roundtrip():
    snap = ds.take_snapshot("s1", {"app": "Notepad", "window": "untitled", "elements": []})
    ref = snap.ref(3)
    assert ref == f"s1:{snap.generation}:{snap.snapshot_id}:3"
    session_id, generation, snapshot_id, n = ds.parse_ref(ref)
    assert (session_id, generation, snapshot_id, n) == ("s1", snap.generation, snap.snapshot_id, 3)


def test_parse_ref_rejects_a_bare_index_never_a_naked_e7_style_ref():
    with pytest.raises(ds.SemanticError):
        ds.parse_ref("e7")


# ---------------------------------------------------------------------------
# session.py: generations
# ---------------------------------------------------------------------------

def test_take_snapshot_bumps_generation_on_window_change():
    first = ds.take_snapshot("s1", {"app": "A", "window": "Win1", "elements": []})
    same = ds.take_snapshot("s1", {"app": "A", "window": "Win1", "elements": []})
    assert same.generation == first.generation
    swapped = ds.take_snapshot("s1", {"app": "A", "window": "Win2", "elements": []})
    assert swapped.generation == first.generation + 1


def test_generations_are_independent_per_session():
    ds.take_snapshot("s1", {"app": "A", "window": "Win1", "elements": []})
    ds.take_snapshot("s1", {"app": "A", "window": "Win2", "elements": []})
    snap2 = ds.take_snapshot("s2", {"app": "A", "window": "Win1", "elements": []})
    assert snap2.generation == 1  # s2's own first snapshot, unaffected by s1's bumps


# ---------------------------------------------------------------------------
# resolve(): the three typed failures
# ---------------------------------------------------------------------------

def _raw(elements):
    return {"app": "FakeApp", "window": "Fake Window", "elements": elements}


def test_resolve_rejects_ref_from_a_different_session():
    snap = ds.take_snapshot("s1", _raw([{"role": "button", "name": "OK"}]))
    ref = snap.ref(0)
    fresh = ds.take_snapshot("s2", _raw([{"role": "button", "name": "OK"}]))
    with pytest.raises(ds.WrongSessionError):
        ds.resolve(ref, fresh, caller_session_id="s2")
    with pytest.raises(ds.WrongSessionError):
        # Same session as the ref, but resolving against a DIFFERENT
        # session's fresh snapshot must also be rejected.
        ds.resolve(ref, fresh, caller_session_id="s1")


def test_resolve_rejects_a_ref_after_the_window_changed():
    snap = ds.take_snapshot("s1", _raw([{"role": "button", "name": "OK"}]))
    ref = snap.ref(0)
    ds.take_snapshot("s1", {"app": "FakeApp", "window": "Other Window", "elements": []})  # bumps generation
    fresh = ds.take_snapshot("s1", _raw([{"role": "button", "name": "OK"}]))  # new generation again
    with pytest.raises(ds.StaleRefError):
        ds.resolve(ref, fresh, caller_session_id="s1")


def test_resolve_degrades_cleanly_when_the_control_is_deleted():
    snap = ds.take_snapshot("s1", _raw([
        {"role": "button", "name": "OK", "path": [0]},
        {"role": "button", "name": "Cancel", "path": [1]},
    ]))
    ref = snap.ref(0)
    # Same window identity (no generation bump) -- just the control is gone.
    fresh = ds.take_snapshot("s1", _raw([{"role": "button", "name": "Cancel", "path": [1]}]))
    assert fresh.generation == snap.generation
    with pytest.raises(ds.StaleRefError):
        ds.resolve(ref, fresh, caller_session_id="s1")


def test_resolve_picks_the_exact_duplicate_by_structural_path():
    """ADP-09 #1: two controls with the IDENTICAL role/name ("OK") must
    still resolve to the SPECIFIC one the ref names, never "whichever is
    first"."""
    elements = [
        {"role": "button", "name": "OK", "path": [0, 0]},
        {"role": "button", "name": "OK", "path": [0, 1]},
    ]
    snap = ds.take_snapshot("s1", _raw(elements))
    ref_first = snap.ref(0)
    ref_second = snap.ref(1)
    fresh = ds.take_snapshot("s1", _raw(elements))

    resolved_first = ds.resolve(ref_first, fresh, caller_session_id="s1")
    resolved_second = ds.resolve(ref_second, fresh, caller_session_id="s1")
    assert resolved_first.path == (0, 0)
    assert resolved_second.path == (0, 1)
    assert resolved_first.n != resolved_second.n


def test_resolve_raises_ambiguous_when_several_controls_match():
    """A reflow moves the original control's path; two controls now share
    role+name+automation_id, so `resolve` must refuse rather than guess."""
    snap = ds.take_snapshot("s1", _raw([{"role": "button", "name": "OK", "path": [0]}]))
    ref = snap.ref(0)
    fresh = ds.take_snapshot("s1", _raw([
        {"role": "button", "name": "OK", "path": [0, 0]},
        {"role": "button", "name": "OK", "path": [0, 1]},
    ]))
    with pytest.raises(ds.AmbiguousTargetError) as excinfo:
        ds.resolve(ref, fresh, caller_session_id="s1")
    assert len(excinfo.value.candidates) == 2


# ---------------------------------------------------------------------------
# act(): delivery/verified as separate facts, precondition, no auto-retry
# ---------------------------------------------------------------------------

def _fake_with_button(**kwargs):
    backend = FakeDesktopBackend()
    control_el = backend.semantic().add_control("button", "Save", **kwargs)
    return backend, control_el


def _snapshot_and_ref(backend, session="s1"):
    raw = backend.semantic().snapshot(session_id=session)
    snap = ds.take_snapshot(session, raw)
    return snap.ref(0)


def test_act_delivered_and_verified_on_success():
    backend, _ = _fake_with_button()
    ref = _snapshot_and_ref(backend)
    element, result = ds.act(session_id="s1", ref=ref, op="invoke", backend=backend)
    assert result.delivery == "delivered"
    assert result.verified is True
    assert element.name == "Save"
    assert backend.semantic().invocations[0]["op"] == "invoke"


def test_act_reports_unknown_on_timeout_and_never_retries():
    """ADP-08 #3 / ADP-09 #4: a hung backend call is `unknown`, distinct
    from `not_delivered`, and `act()` calls the backend exactly once."""
    backend, _ = _fake_with_button()
    backend.semantic().set_delay("invoke", 10.0)
    ref = _snapshot_and_ref(backend)
    element, result = ds.act(session_id="s1", ref=ref, op="invoke", backend=backend, timeout=0.05)
    assert result.delivery == "unknown"
    assert result.verified is False
    assert result.observed_after is None
    assert len(backend.semantic().invocations) == 1  # never retried by act() itself


def test_act_reports_not_delivered_when_backend_says_so():
    """Distinct from a STALE ref (the control is gone before resolve even
    runs, covered by test_resolve_degrades_cleanly_when_the_control_is_deleted):
    here the control resolves fine, but the backend itself reports the call
    did not apply (e.g. it became disabled between resolve and invoke)."""
    backend, control_el = _fake_with_button()
    ref = _snapshot_and_ref(backend)
    backend.semantic().set_response(
        "invoke", {"delivery": "not_delivered", "observed_after": {"reason": "disabled mid-flight"}}
    )
    element, result = ds.act(session_id="s1", ref=ref, op="invoke", backend=backend)
    assert result.delivery == "not_delivered"
    assert result.verified is False


def test_act_precondition_failure_blocks_execution():
    backend, _ = _fake_with_button(enabled=False)
    ref = _snapshot_and_ref(backend)
    with pytest.raises(ds.PreconditionFailedError):
        ds.act(session_id="s1", ref=ref, op="invoke", backend=backend, precondition={"enabled": True})
    assert backend.semantic().invocations == []  # nothing executed


def test_act_does_not_execute_when_resolution_is_ambiguous():
    backend = FakeDesktopBackend()
    backend.semantic().add_control("button", "OK", path=(0,))
    raw = backend.semantic().snapshot(session_id="s1")
    snap = ds.take_snapshot("s1", raw)
    ref = snap.ref(0)
    # Reflow: same role/name, now two candidates with different paths.
    backend.semantic().window.controls = []
    backend.semantic().add_control("button", "OK", path=(0, 0))
    backend.semantic().add_control("button", "OK", path=(0, 1))
    with pytest.raises(ds.AmbiguousTargetError):
        ds.act(session_id="s1", ref=ref, op="invoke", backend=backend)
    assert backend.semantic().invocations == []


def test_act_raises_backend_unavailable_when_platform_has_no_semantic_backend():
    class Plain:
        def semantic(self):
            return None

    with pytest.raises(ds.BackendUnavailableError):
        ds.act(session_id="s1", ref="s1:1:x:0", op="invoke", backend=Plain())


# ---------------------------------------------------------------------------
# evidence.py: same audit trail, delivery/verified kept separate
# ---------------------------------------------------------------------------

def test_evidence_record_appends_to_the_same_audit_log_with_separate_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "RUNTIME", tmp_path)
    from src.desktop_semantics import evidence

    backend, _ = _fake_with_button()
    ref = _snapshot_and_ref(backend)
    element, result = ds.act(session_id="s1", ref=ref, op="invoke", backend=backend)
    evidence.record("s1", ref=ref, op="invoke", element=element, result=result)

    entries = control.audit_log("s1")
    assert len(entries) == 1
    payload = json.loads(entries[0]["note"])
    assert payload["delivery"] == "delivered"
    assert payload["verified"] is True
    assert payload["ref"] == ref
    assert payload["target"]["name"] == "Save"


def test_evidence_export_trace_is_scoped_by_session_and_strips_pixels(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "RUNTIME", tmp_path)
    from src.desktop_semantics import evidence
    from src.desktop_semantics.contracts import ActionResult, Element

    el = Element(n=0, role="button", name="Save")
    dirty_result = ActionResult(
        delivery="delivered", verified=True,
        observed_after={"value": "ok", "screenshot": "should-not-leak"},
    )
    evidence.record("s1", ref="s1:1:snap:0", op="invoke", element=el, result=dirty_result)
    evidence.record("s2", ref="s2:1:snap:0", op="invoke", element=el, result=dirty_result)

    trace = evidence.export_trace("s1")
    assert trace["count"] == 1
    assert trace["entries"][0]["session_id"] == "s1"
    assert "screenshot" not in trace["entries"][0]["observed_after"]
    assert trace["entries"][0]["observed_after"]["value"] == "ok"


# ---------------------------------------------------------------------------
# windows_uia.py: optional dependency, never breaks import on Linux
# ---------------------------------------------------------------------------

def test_package_and_windows_uia_module_import_cleanly_on_linux():
    import src.desktop_semantics  # noqa: F401
    import src.desktop_semantics.windows_uia  # noqa: F401


def test_windows_uia_backend_reports_unavailable_on_linux():
    from src.desktop_semantics.windows_uia import WindowsUiaSemanticBackend

    backend = WindowsUiaSemanticBackend()
    ok, reason = backend.available()
    assert ok is False
    assert reason
    with pytest.raises(RuntimeError):
        backend.snapshot(session_id="s1")


# ---------------------------------------------------------------------------
# desktop_semantic_tools.py: the ask_user-shaped tools
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    values = {"desktop_control_mode": "ask_each"}

    def _get(key, default=None):
        return values.get(key, default)

    import src.tool_capabilities as tc

    monkeypatch.setattr(tc, "get_setting", _get, raising=False)
    return values


def test_desktop_snapshot_tool_returns_refs_via_fake_backend(monkeypatch):
    backend = FakeDesktopBackend()
    backend.semantic().add_control("button", "Save", automation_id="btnSave")
    monkeypatch.setattr(dst, "get_backend", lambda: backend)

    desc, result = _run(dst.DesktopSnapshotTool().execute("{}", {"session_id": "s1"}))
    assert result["exit_code"] == 0
    assert len(result["elements"]) == 1
    assert result["elements"][0]["ref"].startswith("s1:")
    assert result["elements"][0]["name"] == "Save"


def test_desktop_snapshot_tool_errors_cleanly_without_semantic_backend(monkeypatch):
    monkeypatch.setattr(dst, "get_backend", lambda: dt.UnsupportedBackend("no display"))
    desc, result = _run(dst.DesktopSnapshotTool().execute("{}", {"session_id": "s1"}))
    assert result["exit_code"] == 1
    assert "error" in result

    class NoSemantic(dt.DesktopBackend):
        def available(self):
            return True, ""

    monkeypatch.setattr(dst, "get_backend", lambda: NoSemantic())
    desc, result = _run(dst.DesktopSnapshotTool().execute("{}", {"session_id": "s1"}))
    assert result["exit_code"] == 1
    assert "semantic" in result["error"]


def test_desktop_find_requires_a_prior_snapshot(monkeypatch):
    backend = FakeDesktopBackend()
    monkeypatch.setattr(dst, "get_backend", lambda: backend)
    desc, result = _run(dst.DesktopFindTool().execute(json.dumps({"query": "save"}), {"session_id": "fresh"}))
    assert result["exit_code"] == 1


def test_desktop_find_searches_the_latest_snapshot_without_a_new_capture(monkeypatch):
    backend = FakeDesktopBackend()
    backend.semantic().add_control("button", "Save", automation_id="btnSave")
    backend.semantic().add_control("button", "Cancel", automation_id="btnCancel")
    monkeypatch.setattr(dst, "get_backend", lambda: backend)

    _run(dst.DesktopSnapshotTool().execute("{}", {"session_id": "s1"}))
    # Add a control AFTER the snapshot: desktop_find must not see it, proving
    # it searches the STORED snapshot rather than taking a new one.
    backend.semantic().add_control("button", "Delete", automation_id="btnDelete")

    desc, result = _run(dst.DesktopFindTool().execute(json.dumps({"query": "save"}), {"session_id": "s1"}))
    assert result["exit_code"] == 0
    assert len(result["elements"]) == 1
    assert result["elements"][0]["name"] == "Save"
    names = {e["name"] for e in result["elements"]}
    assert "Delete" not in names


def test_desktop_act_tool_end_to_end_records_evidence(tmp_path, monkeypatch):
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
    assert result["delivery"] == "delivered"
    assert result["verified"] is True
    assert backend.semantic().invocations[0]["op"] == "invoke"

    entries = control.audit_log("s1")
    assert len(entries) == 1
    assert entries[0]["tool"] == "desktop_act:invoke"


def test_desktop_act_tool_reports_unknown_on_timeout_without_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "RUNTIME", tmp_path)
    backend = FakeDesktopBackend()
    backend.semantic().add_control("button", "Save", automation_id="btnSave")
    backend.semantic().set_delay("invoke", 10.0)
    monkeypatch.setattr(dst, "get_backend", lambda: backend)

    _, snap_result = _run(dst.DesktopSnapshotTool().execute("{}", {"session_id": "s1"}))
    ref = snap_result["elements"][0]["ref"]

    desc, result = _run(dst.DesktopActTool().execute(
        json.dumps({"ref": ref, "op": "invoke", "timeout": 0.05}), {"session_id": "s1"}
    ))
    assert result["exit_code"] == 0  # unknown delivery is a reported fact, not a tool failure
    assert result["delivery"] == "unknown"
    assert len(backend.semantic().invocations) == 1


def test_desktop_act_is_refused_when_desktop_control_mode_is_off(monkeypatch, _settings):
    _settings["desktop_control_mode"] = "off"
    backend = FakeDesktopBackend()
    backend.semantic().add_control("button", "Save")
    monkeypatch.setattr(dst, "get_backend", lambda: backend)
    desc, result = _run(dst.DesktopActTool().execute(
        json.dumps({"ref": "s1:1:x:0", "op": "invoke"}), {"session_id": "s1"}
    ))
    assert result["exit_code"] == 1
    assert "off" in result["error"]


def test_desktop_snapshot_is_not_refused_when_desktop_control_mode_is_off(monkeypatch, _settings):
    """desktop_snapshot/desktop_find are READS, like desktop_screenshot/
    desktop_list_windows -- `desktop_control_mode=off` only prunes/refuses
    the five (now six) CONTROL tools, never the reads."""
    _settings["desktop_control_mode"] = "off"
    backend = FakeDesktopBackend()
    backend.semantic().add_control("button", "Save")
    monkeypatch.setattr(dst, "get_backend", lambda: backend)
    desc, result = _run(dst.DesktopSnapshotTool().execute("{}", {"session_id": "s1"}))
    assert result["exit_code"] == 0


def test_desktop_act_rejects_an_unknown_op(monkeypatch):
    backend = FakeDesktopBackend()
    backend.semantic().add_control("button", "Save")
    monkeypatch.setattr(dst, "get_backend", lambda: backend)
    desc, result = _run(dst.DesktopActTool().execute(
        json.dumps({"ref": "s1:1:x:0", "op": "explode"}), {"session_id": "s1"}
    ))
    assert result["exit_code"] == 1


# ---------------------------------------------------------------------------
# Registration coherence (mirrors tests/test_l54_tool_wiring.py's pattern)
# ---------------------------------------------------------------------------

NEW_TOOLS = ("desktop_snapshot", "desktop_find", "desktop_act")


@pytest.mark.parametrize("name", NEW_TOOLS)
def test_new_semantic_tool_reaches_every_registry(name):
    from src.tool_capabilities import KNOWN_CAPABILITY_TOOLS
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    from src.tool_registry import snapshot
    from src.tool_security import NON_ADMIN_BLOCKED_TOOLS

    native_names = {(e.get("function") or {}).get("name") for e in FUNCTION_TOOL_SCHEMAS}
    assert name in agent_tools.TOOL_TAGS, f"{name} missing from TOOL_TAGS"
    assert name in agent_tools.TOOL_HANDLERS, f"{name} missing from TOOL_HANDLERS"
    assert name in native_names, f"{name} missing from FUNCTION_TOOL_SCHEMAS"
    assert name in KNOWN_CAPABILITY_TOOLS, f"{name} missing an explicit tool_capabilities entry"
    assert name in BUILTIN_TOOL_DESCRIPTIONS, f"{name} missing a tool_index description (unselectable by RAG)"
    assert name in {d.name for d in snapshot()}, f"{name} missing from ToolRegistry.snapshot()"
    assert name in NON_ADMIN_BLOCKED_TOOLS, f"{name} missing from NON_ADMIN_BLOCKED_TOOLS (sees the owner's desktop)"


def test_desktop_act_carries_the_same_external_side_effect_class_as_desktop_click():
    from src.tool_capabilities import ToolEffect, capabilities_for_tool

    assert ToolEffect.EXTERNAL_SIDE_EFFECT in capabilities_for_tool("desktop_act").effects
    assert ToolEffect.EXTERNAL_SIDE_EFFECT in capabilities_for_tool("desktop_click").effects
    # Deliberately NOT in ALWAYS_APPROVE_TOOLS -- see the comment above that
    # frozenset in src/tool_capabilities.py: a neighbouring test file
    # (tests/test_desktop_tools.py, not touched here) asserts it equals
    # exactly the five coordinate-input tools.
    from src.tool_capabilities import ALWAYS_APPROVE_TOOLS

    assert "desktop_act" not in ALWAYS_APPROVE_TOOLS
    assert "desktop_snapshot" not in ALWAYS_APPROVE_TOOLS
    assert "desktop_find" not in ALWAYS_APPROVE_TOOLS


def test_desktop_act_handler_in_tool_handlers_is_the_real_one():
    assert agent_tools.TOOL_HANDLERS["desktop_act"] is dst.DESKTOP_SEMANTIC_TOOL_HANDLERS["desktop_act"]


def test_existing_desktop_tool_handlers_are_unaffected():
    """Non-regression: adding the three semantic tools must not change
    dispatch for the seven pre-existing desktop_* tools."""
    for name in ("desktop_screenshot", "desktop_list_windows", "desktop_focus_window",
                 "desktop_click", "desktop_type", "desktop_key", "desktop_scroll"):
        assert agent_tools.TOOL_HANDLERS[name] is dt.DESKTOP_TOOL_HANDLERS[name]


def test_desktop_tools_frozenset_still_matches_the_seven_legacy_names():
    """Non-regression for tests/test_desktop_tools.py::test_registered_in_dispatch_tables
    and ::test_always_approve_set, which assert `dt.DESKTOP_TOOLS`/
    `ALWAYS_APPROVE_TOOLS` equal exactly the seven/five legacy names. The
    three semantic tools must reach TOOL_TAGS/TOOL_HANDLERS WITHOUT
    widening those two frozensets."""
    assert "desktop_act" not in dt.DESKTOP_TOOLS
    assert "desktop_snapshot" not in dt.DESKTOP_TOOLS
    assert "desktop_find" not in dt.DESKTOP_TOOLS
    for name in NEW_TOOLS:
        assert name in agent_tools.TOOL_TAGS
