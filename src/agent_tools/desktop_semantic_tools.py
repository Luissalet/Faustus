"""ADP-08/ADP-09 — agent tools over `src.desktop_semantics`.

Three tools, alongside the coordinate-based ones in `desktop_tools.py`:

    desktop_snapshot   read the control tree of the active window (READ)
    desktop_find        search an already-taken snapshot for a control (READ)
    desktop_act         invoke/select/set_value/scroll/focus a control by ref (CONTROL)

`desktop_snapshot`/`desktop_find` never touch the desktop — `desktop_find`
does not even take a new snapshot, it searches one already returned this
turn — and stay on the normal approval gate the read desktop_* tools use
(`READ_PRIVATE`, `src/tool_capabilities.py`). `desktop_act` executes a real
action and carries `EXTERNAL_SIDE_EFFECT`, the same class as
`desktop_click`/`desktop_type` — the normal human-approval gate applies to
every call. It is NOT folded into `tool_capabilities.ALWAYS_APPROVE_TOOLS`
(the "ask on literally every call, even within one already-approved task"
set the five coordinate tools use): a neighbouring test file locks that
frozenset down to exactly those five names (see the comment above
`ALWAYS_APPROVE_TOOLS` in `tool_capabilities.py`). `desktop_act` instead
refuses itself outright when `desktop_control_mode=off` (checked directly
below) — the same end state, minus the tool-preflight pruning and
ask-every-call behaviour those five get automatically.

Every handler has the same `ask_user` shape as `desktop_tools.DesktopTool`
(`async execute(content, ctx) -> (desc, result)`) and never raises.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Tuple

from src import desktop_semantics as ds
from src.tool_capabilities import desktop_control_mode

from .desktop_tools import (
    DesktopError,
    WindowsBackend,
    _error,
    _parse_args,
    _screen_hash,
    get_backend,
)

logger = logging.getLogger(__name__)

SEMANTIC_TOOLS = frozenset({"desktop_snapshot", "desktop_find", "desktop_act"})
_ACT_OPS = frozenset({"invoke", "select", "set_value", "scroll", "focus"})
_MAX_FIND_LIMIT = 100
_DEFAULT_FIND_LIMIT = 20
_MAX_TIMEOUT = 30.0


def _session_id(ctx: Any) -> str:
    return str((ctx or {}).get("session_id") or "")


def _semantic_error(tool: str, exc: ds.SemanticError) -> Tuple[str, Dict[str, Any]]:
    result = {"error": f"{tool}: {exc.message}", "exit_code": 1, "error_class": exc.code}
    if isinstance(exc, ds.AmbiguousTargetError):
        result["candidates"] = list(exc.candidates)
    return f"{tool}: failed", result


def _element_summary(el) -> Dict[str, Any]:
    return {
        "role": el.role,
        "name": el.name,
        "automation_id": el.automation_id,
        "enabled": el.enabled,
        "value": el.value,
    }


class DesktopSnapshotTool:
    """`desktop_snapshot`: a bounded, structured read of the active
    window's control tree — the ADP-08 alternative to guessing pixels from
    a screenshot. Returns one `ref` per control, scoped to this session's
    current generation (see `src/desktop_semantics/contracts.py`)."""

    async def execute(self, content: Any, ctx: dict) -> Tuple[str, Dict[str, Any]]:
        try:
            return await self._execute(content, ctx)
        except DesktopError as exc:
            return _error("desktop_snapshot", str(exc))
        except ds.SemanticError as exc:
            return _semantic_error("desktop_snapshot", exc)
        except Exception as exc:  # noqa: BLE001 - tools never raise
            logger.warning("desktop_snapshot failed: %s", exc, exc_info=True)
            return _error("desktop_snapshot", f"{type(exc).__name__}: {exc}")

    async def _execute(self, content: Any, ctx: dict) -> Tuple[str, Dict[str, Any]]:
        args = _parse_args(content)
        session = _session_id(ctx)
        backend = get_backend()
        ok, reason = backend.available()
        if not ok:
            raise DesktopError(reason)
        if isinstance(backend, WindowsBackend):
            from src.desktop_control_session import ensure_indicator

            await ensure_indicator()
        semantic = backend.semantic()
        if semantic is None:
            raise DesktopError(
                "no semantic desktop backend on this platform yet (Windows UIA only); "
                "use desktop_screenshot + desktop_click/desktop_type instead"
            )
        avail, why = semantic.available()
        if not avail:
            raise DesktopError(why)

        depth = args.get("depth")
        max_elements = args.get("max_elements")
        kwargs: Dict[str, Any] = {}
        if depth is not None:
            kwargs["depth"] = int(depth)
        if max_elements is not None:
            kwargs["max_elements"] = int(max_elements)
        raw = semantic.snapshot(session_id=session, **kwargs)
        snap = ds.take_snapshot(session, raw)

        elements = [
            {"ref": snap.ref(el.n), **_element_summary(el)} for el in snap.elements
        ]
        lines = [
            f"Snapshot of {snap.app or 'the active app'} / {snap.window or 'active window'}: "
            f"{len(elements)} element(s){' (truncated)' if snap.truncated else ''}."
        ]
        if snap.truncated:
            lines.append("The tree was cut by depth/size limits; narrow the window or ask for more.")
        return f"desktop_snapshot: {snap.window or snap.app or 'window'}", {
            "output": "\n".join(lines),
            "exit_code": 0,
            "snapshot_id": snap.snapshot_id,
            "generation": snap.generation,
            "app": snap.app,
            "window": snap.window,
            "truncated": snap.truncated,
            "elements": elements,
        }


class DesktopFindTool:
    """`desktop_find`: search an ALREADY-TAKEN `desktop_snapshot` for
    controls matching `query` (and optionally `role`) — no new capture, pure
    read over data this turn already has, the same shape as ADP-29's
    `browser_view.subtree`/`search` over a browser snapshot."""

    async def execute(self, content: Any, ctx: dict) -> Tuple[str, Dict[str, Any]]:
        try:
            return await self._execute(content, ctx)
        except DesktopError as exc:
            return _error("desktop_find", str(exc))
        except ds.SemanticError as exc:
            return _semantic_error("desktop_find", exc)
        except Exception as exc:  # noqa: BLE001 - tools never raise
            logger.warning("desktop_find failed: %s", exc, exc_info=True)
            return _error("desktop_find", f"{type(exc).__name__}: {exc}")

    async def _execute(self, content: Any, ctx: dict) -> Tuple[str, Dict[str, Any]]:
        args = _parse_args(content)
        session = _session_id(ctx)
        query = str(args.get("query") or args.get("_raw") or "").strip().lower()
        role_filter = str(args.get("role") or "").strip().lower()
        try:
            limit = int(args.get("limit") or _DEFAULT_FIND_LIMIT)
        except (TypeError, ValueError):
            raise DesktopError("`limit` must be a number") from None
        limit = max(1, min(limit, _MAX_FIND_LIMIT))

        snapshot_id = str(args.get("snapshot_id") or "").strip()
        if snapshot_id:
            snap = ds.get_snapshot(session, snapshot_id)
        else:
            snap = ds.latest_snapshot(session)
        if snap is None:
            raise DesktopError(
                "no desktop_snapshot on record for this session yet — call desktop_snapshot first"
            )

        def matches(el) -> bool:
            if role_filter and el.role.lower() != role_filter:
                return False
            if not query:
                return True
            haystack = f"{el.role} {el.name} {el.automation_id}".lower()
            return query in haystack

        found = [el for el in snap.elements if matches(el)]
        truncated_results = len(found) > limit
        found = found[:limit]
        elements = [{"ref": snap.ref(el.n), **_element_summary(el)} for el in found]
        return f"desktop_find: {len(elements)} match(es)", {
            "output": f"{len(elements)} matching control(s) in {snap.window or snap.app or 'the snapshot'}"
                       + (" (more matched than the limit — narrow the query)" if truncated_results else "") + ".",
            "exit_code": 0,
            "snapshot_id": snap.snapshot_id,
            "generation": snap.generation,
            "truncated_results": truncated_results,
            "elements": elements,
        }


class DesktopActTool:
    """`desktop_act`: re-resolve `ref` against a fresh snapshot, check an
    optional `precondition`, and run `op` — `invoke` (click/press),
    `select`, `set_value`, `scroll`, or `focus`. Returns an `ActionResult`
    whose `delivery`/`verified` are kept separate (see
    `src/desktop_semantics/contracts.py`); `delivery="unknown"` (a timeout)
    is reported as-is and NEVER retried by this tool."""

    async def execute(self, content: Any, ctx: dict) -> Tuple[str, Dict[str, Any]]:
        try:
            return await self._execute(content, ctx)
        except DesktopError as exc:
            return _error("desktop_act", str(exc))
        except ds.SemanticError as exc:
            return _semantic_error("desktop_act", exc)
        except Exception as exc:  # noqa: BLE001 - tools never raise
            logger.warning("desktop_act failed: %s", exc, exc_info=True)
            return _error("desktop_act", f"{type(exc).__name__}: {exc}")

    async def _execute(self, content: Any, ctx: dict) -> Tuple[str, Dict[str, Any]]:
        args = _parse_args(content)
        if desktop_control_mode() == "off":
            raise DesktopError(
                "desktop control is disabled (setting desktop_control_mode=off). "
                "Do not call desktop input tools again in this turn."
            )
        session = _session_id(ctx)
        ref = str(args.get("ref") or "").strip()
        if not ref:
            raise DesktopError("missing `ref` (from a prior desktop_snapshot/desktop_find)")
        op = str(args.get("op") or "").strip().lower()
        if op not in _ACT_OPS:
            raise DesktopError(f"`op` must be one of {sorted(_ACT_OPS)}; got {op!r}")
        precondition = args.get("precondition")
        if precondition is not None and not isinstance(precondition, dict):
            raise DesktopError("`precondition` must be an object of {attribute: expected_value}")
        try:
            timeout = float(args.get("timeout") or 5.0)
        except (TypeError, ValueError):
            raise DesktopError("`timeout` must be a number of seconds") from None
        timeout = max(0.1, min(timeout, _MAX_TIMEOUT))
        params: Dict[str, Any] = {}
        if "value" in args:
            params["value"] = args.get("value")
        if "direction" in args:
            params["direction"] = args.get("direction")
        if "amount" in args:
            params["amount"] = args.get("amount")

        backend = get_backend()
        ok, reason = backend.available()
        if not ok:
            raise DesktopError(reason)
        if isinstance(backend, WindowsBackend):
            from src.desktop_control_session import ensure_indicator

            await ensure_indicator()

        from src.desktop_control_session import audit_enabled

        audit_active = audit_enabled(session)
        before_hash = _screen_hash(backend) if audit_active else ""

        element, result = ds.act(
            session_id=session, ref=ref, op=op, backend=backend,
            params=params, precondition=precondition, timeout=timeout,
        )

        after_hash = _screen_hash(backend) if audit_active else ""
        from src.desktop_semantics import evidence

        evidence.record(
            session, ref=ref, op=op, element=element, result=result,
            before_hash=before_hash, after_hash=after_hash,
        )

        summary = (
            f"{op} on {element.role} {element.name!r}: delivery={result.delivery}"
            + (", verified" if result.verified else "")
        )
        return f"desktop_act: {summary}", {
            "output": summary + ". Take a new desktop_snapshot (or desktop_screenshot) to see the effect.",
            "exit_code": 0,
            "target": _element_summary(element),
            "delivery": result.delivery,
            "observed_after": result.observed_after,
            "verified": result.verified,
        }


DESKTOP_SEMANTIC_TOOL_HANDLERS = {
    "desktop_snapshot": DesktopSnapshotTool().execute,
    "desktop_find": DesktopFindTool().execute,
    "desktop_act": DesktopActTool().execute,
}
