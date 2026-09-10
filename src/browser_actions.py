"""Precondition checks and readback for browser actions (WEB-04).

``docs/spec/v2/MAPA_REUTILIZACION.md`` (WEB-04, "ausente"): Faustus's own
code never verifies a click/type/navigate's target before running it or
what actually happened after — that is left entirely to
``@playwright/mcp``. ``src/browser_view.py`` already grabs a screenshot
after the fact for the UI panel; this module is the seam the model-facing
tool RESULT goes through, so a browser action can refuse to run when its
precondition does not hold ("a shifted layout must not produce a blind
click on Delete" — WEB-04's own acceptance criterion) and always carries a
readback of what state the page was actually in afterward, instead of the
model taking its own request for granted.

Both functions take an injected snapshot/act callable rather than reaching
into ``McpManager`` themselves, so they are testable against a fake page
abstraction with no real browser or network (per the lote's own
instruction) and reusable from whichever caller ends up wiring them in —
see the final report for where that wiring belongs (outside this lote's
file ownership).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional


@dataclass(frozen=True)
class ActionPrecondition:
    """What must hold before a browser action is allowed to run.

    Both checks are optional (empty string = "not checked") because not
    every action has an expected URL (a `browser_type` into a field on the
    current page does not) or a specific element to confirm (a bare
    `browser_navigate` does not).
    """

    expected_url: str = ""
    require_element: str = ""


@dataclass(frozen=True)
class ActionReadback:
    """What the page actually showed, captured fresh — never assumed."""

    url: str
    title: str
    dom_hash: str
    element_text: str = ""

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "dom_hash": self.dom_hash,
            "element_text": self.element_text,
        }


def _hash_snapshot(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()


def check_precondition(
    precondition: ActionPrecondition,
    *,
    current_url: str,
    snapshot_text: str,
) -> Optional[str]:
    """``None`` when `precondition` holds against the current page state,
    otherwise a human-readable reason the action must not run.

    `expected_url` is a substring match, not equality — a caller usually
    knows "still on the checkout page", not the exact query string a
    redirect might have appended. `require_element` is checked against the
    freshly-taken snapshot text (an accessibility-tree dump or similar,
    whatever `src/browser_view.py`'s callers already produce), not a
    coordinate, so a scrolled or resized page does not falsely fail this.
    """
    if precondition.expected_url and precondition.expected_url not in (current_url or ""):
        return (
            f"expected the current URL to contain {precondition.expected_url!r}, "
            f"but the page is at {current_url or '(unknown)'!r}"
        )
    if precondition.require_element and precondition.require_element not in (snapshot_text or ""):
        return f"expected element {precondition.require_element!r} was not found in the current page"
    return None


def build_readback(*, url: str, title: str, snapshot_text: str, element_text: str = "") -> ActionReadback:
    return ActionReadback(
        url=url or "",
        title=title or "",
        dom_hash=_hash_snapshot(snapshot_text),
        element_text=element_text or "",
    )


SnapshotFn = Callable[[], Awaitable[Mapping[str, Any]]]
ActFn = Callable[[], Awaitable[Any]]


async def run_with_precondition(
    action: str,
    *,
    precondition: ActionPrecondition,
    snapshot_fn: SnapshotFn,
    act_fn: ActFn,
) -> Dict[str, Any]:
    """Check `precondition` against a FRESH snapshot, run `act_fn` only if it
    holds, then attach a fresh post-action readback either way.

    `snapshot_fn`/`act_fn` are injected async callables returning
    ``{"url", "title", "text"}`` and a raw tool-result respectively — this
    function never calls Playwright/MCP directly, so a test supplies fakes
    and no network or browser is involved (mirrors how
    `src/browser_view.after_browser_action` takes an already-abstracted
    `mcp_manager.call_tool` rather than a browser handle).

    A precondition failure returns ``{"blocked": True, "error": ..., "readback": ...}``
    and never calls `act_fn` — the model must not be told an action happened
    when it was refused before it ran. A successful run always re-snapshots
    AFTER `act_fn` returns, so the readback is what the page shows now, not
    what it showed when the precondition was checked (`browser_navigate`
    changes exactly that).
    """
    before = await snapshot_fn()
    reason = check_precondition(
        precondition,
        current_url=str((before or {}).get("url") or ""),
        snapshot_text=str((before or {}).get("text") or ""),
    )
    if reason:
        readback = build_readback(
            url=str((before or {}).get("url") or ""),
            title=str((before or {}).get("title") or ""),
            snapshot_text=str((before or {}).get("text") or ""),
        )
        return {
            "blocked": True,
            "error": f"precondition failed for {action}: {reason}",
            "readback": readback.to_mapping(),
        }

    result = await act_fn()
    after = await snapshot_fn()
    readback = build_readback(
        url=str((after or {}).get("url") or ""),
        title=str((after or {}).get("title") or ""),
        snapshot_text=str((after or {}).get("text") or ""),
    )

    out: Dict[str, Any] = dict(result) if isinstance(result, dict) else {"output": result}
    out["readback"] = readback.to_mapping()
    return out
