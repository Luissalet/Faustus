"""CMP-10 (INFORME_COMPARATIVO_V2.md §3.9) — which CHANNEL a desktop action
actually goes through, decided per target instead of a fixed global order.

Vocabulary — the four channels an action can travel through, ordered here
from "acts on a named, stable identity" to "acts on raw pixels with no
identity at all":

  app_api      a named Faustus endpoint/tool acts on the user's behalf
               (`app_api`/a dedicated tool) — no window, no coordinates.
  dom_cdp      the integrated browser's accessibility/DOM tree over CDP
               (`src/browser_view.py`'s snapshot/ref world) — semantic, but
               scoped to a browser tab.
  native_a11y  this package's own semantic desktop layer (`contracts.py`/
               `session.py`, UIA today) — semantic, scoped to a native app.
  pixels       coordinate clicks/keys with no identity at all
               (`src/agent_tools/desktop_tools.py::DesktopBackend`) — the
               ORIGINAL, always-available fallback; every other channel is
               something Faustus reaches for INSTEAD of this when it can.

`choose_channel` never returns a fixed order: which channels are even
candidates depends on `target["kind"]` (an API-shaped goal has no business
trying `dom_cdp`), and which of those candidates is actually usable comes
from the caller-supplied `capabilities` (what is available THIS call, not
what exists in the abstract) filtered by `policy`. A decision that lands on
`pixels` while a more semantic channel was the natural fit for this kind of
target is a RISK CHANGE — `risk_change=True`, `requires_visible_fallback=
True` — and the caller MUST make it visible (an event, or an evidence entry
with `channel_fallback: true`; see `record_fallback` below) rather than
silently substitute coordinates for identity.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence

from .contracts import BackendUnavailableError

CHANNELS = ("app_api", "dom_cdp", "native_a11y", "pixels")

# The channels this package considers "semantic" — the action names WHAT it
# targets (role/name/identity), never WHERE on screen it happens to sit.
# `pixels` is deliberately the only non-semantic member.
SEMANTIC_CHANNELS = frozenset({"app_api", "dom_cdp", "native_a11y"})

# Candidate order PER TARGET KIND — not one global ranking. A target this
# module does not recognise falls back to `_DEFAULT_ORDER`, which still puts
# `pixels` last: the point of this module is that reaching pixels is a
# decision, never a default.
_ORDER_BY_KIND: Dict[str, Sequence[str]] = {
    "api": ("app_api", "native_a11y", "pixels"),
    "browser": ("dom_cdp", "native_a11y", "pixels"),
    "desktop": ("native_a11y", "app_api", "pixels"),
}
_DEFAULT_ORDER: Sequence[str] = ("native_a11y", "dom_cdp", "app_api", "pixels")


@dataclass(frozen=True)
class ChannelDecision:
    """One `choose_channel` outcome. Every field is a separate fact — never
    collapsed into a single "ok" boolean, same discipline as
    `contracts.ActionResult`."""

    channel: str
    reason: str
    risk_change: bool
    requires_visible_fallback: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "channel": self.channel,
            "reason": self.reason,
            "risk_change": self.risk_change,
            "requires_visible_fallback": self.requires_visible_fallback,
        }


def _order_for(target: Optional[Dict[str, Any]]) -> Sequence[str]:
    kind = str((target or {}).get("kind") or "").strip().lower()
    return _ORDER_BY_KIND.get(kind, _DEFAULT_ORDER)


def choose_channel(
    target: Optional[Dict[str, Any]],
    *,
    capabilities: Optional[Dict[str, bool]] = None,
    policy: Optional[Dict[str, Any]] = None,
    preferred: Optional[str] = None,
) -> ChannelDecision:
    """Pick the channel for `target`.

    `target`: `{"kind": "api" | "browser" | "desktop", ...}` — anything
    else falls back to `_DEFAULT_ORDER` (still `pixels`-last).
    `capabilities`: `{channel: bool}` — what is ACTUALLY usable for this
    call right now (e.g. `native_a11y=False` when the UIA backend is
    unavailable on this platform, not "does UIA exist in the abstract").
    Missing keys are treated as unavailable.
    `policy`: `{"desktop_control_mode": "...", "allow_pixels": bool}` —
    `allow_pixels=False` removes `pixels` from consideration entirely
    (a caller with no other option then gets `BackendUnavailableError`
    rather than a silent coordinate action the policy forbade).
    `preferred`: an explicit channel to try first, ahead of the per-kind
    order, IF it is a candidate for this kind and available — never
    overrides "not a candidate for this kind" (an `api` target never gets
    `dom_cdp` just because a caller asked).

    Raises `BackendUnavailableError` when nothing in the candidate order is
    usable — including `pixels`, when `policy` allows it: `choose_channel`
    never invents a channel that was not offered as a capability.
    """
    caps = {c: bool((capabilities or {}).get(c)) for c in CHANNELS}
    pol = dict(policy or {})
    order = list(_order_for(target))
    if preferred and preferred in order:
        order.remove(preferred)
        order.insert(0, preferred)

    allow_pixels = bool(pol.get("allow_pixels", True))
    if not allow_pixels:
        caps["pixels"] = False

    usable = [c for c in order if caps.get(c)]
    if not usable:
        raise BackendUnavailableError(
            f"no channel is usable for target kind {str((target or {}).get('kind') or '(unspecified)')!r} "
            f"under the current capabilities/policy (tried {', '.join(order)})"
        )

    chosen = usable[0]
    # The most-semantic channel this target's kind would naturally reach for
    # — used only to decide whether landing on `chosen` is a fallback, never
    # to override `chosen` itself.
    natural = order[0]
    risk_change = chosen not in SEMANTIC_CHANNELS and natural in SEMANTIC_CHANNELS
    requires_visible_fallback = risk_change or bool(pol.get("require_fallback_visibility"))

    if chosen == natural:
        reason = f"{chosen} is the natural channel for this target and is available"
    elif risk_change:
        reason = (
            f"{natural} (semantic) is not available right now, so this call is falling back to "
            f"{chosen} — a real risk change that must stay visible"
        )
    else:
        reason = f"{natural} is not available right now; falling back to {chosen}, still semantic"

    return ChannelDecision(
        channel=chosen,
        reason=reason,
        risk_change=risk_change,
        requires_visible_fallback=requires_visible_fallback,
    )


def record_fallback(
    session_id: str,
    decision: ChannelDecision,
    *,
    target: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Append ONE visible evidence entry for a channel decision — reuses the
    exact audit trail `src.desktop_semantics.evidence`/`desktop_act` already
    write (`src.desktop_control_session.record_action`; rule 4: no second
    store for something that already has one). Call this whenever
    `decision.requires_visible_fallback` is True; harmless (but pointless)
    to call otherwise, since the entry always carries the true
    `channel_fallback` value rather than assuming the caller checked."""
    import json

    from src import desktop_control_session as policy

    note = json.dumps(
        {
            "kind": "desktop_channel_decision",
            "channel": decision.channel,
            "reason": decision.reason,
            "risk_change": decision.risk_change,
            "channel_fallback": bool(decision.requires_visible_fallback),
            "target": target or {},
        },
        sort_keys=True,
    )
    return policy.record_action(session_id, "desktop_channel:decision", note=note)
