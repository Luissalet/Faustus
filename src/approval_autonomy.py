"""src/approval_autonomy.py — shadow mode and confidence-scored autonomy for
the post-external-context approval gate.

Faustus already gates certain tool calls behind a human approval card once
untrusted content has entered a run (`src.tool_capabilities.
ToolRunSecurityContext.decision_for`, the ``blocked_effects`` branch). This
module adds an OPT-IN, three-mode autonomy system on top of that ONE gate —
it never touches the desktop-input confirmation or the destructive shell
command guard, both of which return their own denial earlier in
`decision_for` and are never reached by anything here.

Setting ``approval_autonomy``:

* ``"off"`` (default) — nothing in this module runs. `decision_for` behaves
  exactly as it did before this module existed, and no row is ever written.
* ``"shadow"`` — behaviour is unchanged (a card is still shown for every
  gated action); every time a card is CREATED (`src.tool_approvals.
  ToolApprovalStore.create`) this module computes a confidence score and
  records what it WOULD have decided, then finalizes that row with what the
  user actually clicked once the card is consumed
  (`ToolApprovalStore.consume_with_reason`). This is how a tool family earns
  the history needed to be promoted.
* ``"active"`` — for a call whose family has been PROMOTED (see below), a
  confidence >= ``ACT_THRESHOLD`` auto-approves it: `decision_for` returns
  an allow and no card is ever shown. Anything else — a family not yet
  promoted, or a lower-confidence call in a promoted family — still shows a
  card, now carrying a short recommendation (`PendingToolApproval.
  autonomy_note`) for the human to read.

Confidence tiers, from the 0..1 score (`tier_for_score`):

* ``act``      (>= 0.80) — the kind of call `active` mode may auto-approve.
* ``advise``   (0.50..0.79) — still asks; the card explains why it leans
  toward approving.
* ``escalate`` (< 0.50) — still asks, no different from today's card.

Confidence signals (`compute_confidence`), each already available to the
gate with no new instrumentation:

* ``readonly``       — 1.0 for a read-only private-store action, 0.5 for a
  write confirmed inside the bound workspace, 0.0 for anything else
  (never reached in practice: `PROMOTABLE_EFFECTS` already excludes it).
* ``named``           — 1.0 when the user's own words this turn named the
  tool or the exact target path/file, 0.0 otherwise.
* ``workspace``       — 1.0 when every write target resolves inside the
  bound workspace folder (or the action has no write target), 0.0 when a
  target exists but could not be confirmed inside it.
* ``prior_approvals`` — how many times THIS owner has approved THIS family
  before, capped at 5 for a 0..1 contribution.
* ``reversible``      — 1.0 for a read, 1.0 for a write to a file that
  already exists (a `git diff`/undo can restore it), 0.5 for a new file.

    score = 0.30*readonly + 0.20*named + 0.15*workspace
          + 0.25*prior_approvals + 0.10*reversible

Weights are a deliberate design choice, not a fit to data: reversibility and
"the user actually named this" both carry real signal but on their own are
weak evidence of intent, so the largest weights go to the two hardest-to-
fake ones — this being a demonstrably safe class of effect at all
(``readonly``) and a genuine track record with this exact user
(``prior_approvals``).

Hard blocks (`is_hard_blocked`) are checked FIRST and can never be
overridden by a high score or a long promoted history: destructive effects,
a write whose target cannot be confirmed inside the bound workspace, a
message/notification send, anything that looks like a payment, and any
action whose capability class is not one of `PROMOTABLE_EFFECTS`
(``read_private`` and an in-workspace ``write_workspace`` only — never
``execute_code``, ``network_egress``, ``external_side_effect``,
``admin_change`` or ``destructive``). This is enforced twice: once here as
its own function (tested directly), and once structurally, because
`ToolRunSecurityContext._autonomy_decision` only ever calls this override
site from the ONE gate branch these effect classes can reach.

Promotion (`is_family_promoted`): a family (one tool name, see `family_for`)
is auto-promoted once its logged ``act``-tier shadow rows for one owner
number at least `DEFAULT_PROMOTE_MIN_DECISIONS` (20), agree with what the
user actually did at least `DEFAULT_PROMOTE_MIN_AGREEMENT` (95%) of the
time, and never disagreed on a row flagged destructive. A manual override
(`set_family_override`) forces "promoted" or "demoted" regardless of that
math; clearing it (``status=""``) returns to the computed value.

Storage: two tiny tables in the shared context-engine store (`ce_store`),
registered the same way every other `src.code_graph`/`src.context_engine`
feature owns its schema — see that module's own docstring for why a
separate, rebuildable database rather than a migration in `app.db`.
"""
from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from src.context_engine import store as ce_store

logger = logging.getLogger(__name__)

MODES: Tuple[str, ...] = ("off", "shadow", "active")
TIERS: Tuple[str, ...] = ("act", "advise", "escalate")

#: >= this score is "act" tier.
ACT_THRESHOLD = 0.80
#: >= this (and below ACT_THRESHOLD) is "advise" tier; below this is "escalate".
ADVISE_THRESHOLD = 0.50

#: A family needs at least this many "act"-tier shadow decisions...
DEFAULT_PROMOTE_MIN_DECISIONS = 20
#: ...agreeing with the user at least this often...
DEFAULT_PROMOTE_MIN_AGREEMENT = 0.95
#: ...to be auto-promoted. A single disagreement on a row flagged destructive
#: blocks promotion regardless of the aggregate rate (see `_promoted_from_stats`).

#: Tool names that send something outside Faustus or move money: NEVER
#: eligible for auto-approval, whatever the score or family history says.
_NEVER_AUTO_TOOLS = frozenset({
    "send_email", "reply_to_email", "bulk_email",
    "whatsapp_send", "whatsapp_react",
    "send_to_session",
})
_NEVER_AUTO_NAME_RE = re.compile(
    r"(?:^|_)(pay|payment|checkout|transfer|invoice_pay|refund)(?:_|$)", re.I
)

ce_store.register_schema("approval_autonomy", (
    """
    CREATE TABLE IF NOT EXISTS approval_shadow_log (
        id              TEXT NOT NULL PRIMARY KEY,
        owner           TEXT NOT NULL DEFAULT '',
        family          TEXT NOT NULL DEFAULT '',
        tool_name       TEXT NOT NULL DEFAULT '',
        workspace       TEXT NOT NULL DEFAULT '',
        score           REAL NOT NULL DEFAULT 0.0,
        tier            TEXT NOT NULL DEFAULT '',
        source          TEXT NOT NULL DEFAULT 'shadow',
        destructive     INTEGER NOT NULL DEFAULT 0,
        actual_decision TEXT NOT NULL DEFAULT '',
        agreed          INTEGER,
        approval_id     TEXT NOT NULL DEFAULT '',
        created_at      TEXT NOT NULL DEFAULT '',
        decided_at      TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_approval_shadow_owner_family "
    "ON approval_shadow_log(owner, family)",
    "CREATE INDEX IF NOT EXISTS ix_approval_shadow_approval_id "
    "ON approval_shadow_log(approval_id)",
    """
    CREATE TABLE IF NOT EXISTS approval_family_overrides (
        owner       TEXT NOT NULL DEFAULT '',
        family      TEXT NOT NULL DEFAULT '',
        status      TEXT NOT NULL DEFAULT '',
        updated_at  TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (owner, family)
    )
    """,
))


def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:  # noqa: BLE001
        return default


def autonomy_mode() -> str:
    mode = str(_setting("approval_autonomy", "off") or "off").strip().lower()
    return mode if mode in MODES else "off"


def tier_for_score(score: float) -> str:
    if score >= ACT_THRESHOLD:
        return "act"
    if score >= ADVISE_THRESHOLD:
        return "advise"
    return "escalate"


def family_for(tool_name: Any, content: Any = None) -> str:
    """Grouping key for shadow stats and promotion. One family per tool
    name today: coarse enough that a normal working session accumulates the
    20 decisions promotion needs, fine enough that a promoted `read_file`
    never promotes `bash` alongside it."""
    return str(tool_name or "").strip() or "unknown"


def dedup_key(tool_name: Any, content: Any) -> str:
    """Stable key for one exact call, so a gate asked twice for the same
    content (the loop, then `tool_execution.py`, per `decision_for`'s own
    docstring) logs an auto-approval once, not twice."""
    raw = f"{tool_name}\x00{content if isinstance(content, str) else str(content)}"
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:32]


def _owner_key(owner: Any) -> str:
    return str(owner or "").strip().casefold()


def _resolve_workspace(workspace: Any) -> str:
    import os
    if not isinstance(workspace, str) or not workspace.strip():
        return ""
    try:
        return os.path.realpath(os.path.expanduser(workspace))
    except (OSError, ValueError):
        return workspace


@dataclass(frozen=True)
class ConfidenceResult:
    score: float
    tier: str
    signals: Dict[str, float] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)


def _promotable_effects():
    from src.tool_capabilities import ToolEffect
    return frozenset({ToolEffect.READ_PRIVATE, ToolEffect.WRITE_WORKSPACE})


class _PromotableEffectsProxy:
    """Lazy `frozenset` of the two effect classes this module will ever
    consider promoting -- built on first use so importing this module never
    has to import `src.tool_capabilities` at module load time (that module
    imports this one lazily too; both stay import-cycle-free)."""

    _cached: Optional[frozenset] = None

    def _get(self) -> frozenset:
        if self._cached is None:
            self._cached = _promotable_effects()
        return self._cached

    def __iter__(self):
        return iter(self._get())

    def __contains__(self, item) -> bool:
        return item in self._get()

    def __le__(self, other) -> bool:
        return self._get() <= other

    def __ge__(self, other) -> bool:
        return self._get() >= other

    def __and__(self, other):
        return self._get() & other

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"PROMOTABLE_EFFECTS({sorted(e.value for e in self._get())})"


PROMOTABLE_EFFECTS = _PromotableEffectsProxy()


def is_hard_blocked(tool_name: Any, content: Any, *, workspace: str = "",
                     capabilities: Any = None) -> bool:
    """True when this action must NEVER be auto-approved, independent of
    confidence or family history: destructive, a write whose target cannot
    be confirmed inside the bound workspace, a message/notification send, a
    payment-shaped tool name, or an unrecognised/non-promotable capability
    class. Fails closed: anything this cannot positively clear is blocked."""
    name = str(tool_name or "")
    if name in _NEVER_AUTO_TOOLS or _NEVER_AUTO_NAME_RE.search(name):
        return True
    try:
        from src.tool_capabilities import (
            ToolEffect, capabilities_for_action, _write_targets, path_inside_trusted,
        )
    except Exception:  # noqa: BLE001
        return True
    caps = capabilities if capabilities is not None else capabilities_for_action(tool_name, content)
    if not getattr(caps, "known", False):
        return True
    effects = getattr(caps, "effects", frozenset())
    if not effects or not (effects <= _promotable_effects()):
        return True
    if ToolEffect.DESTRUCTIVE in effects:
        return True
    if ToolEffect.WRITE_WORKSPACE in effects:
        if not workspace:
            return True
        targets = _write_targets(name, content)
        if not targets or not all(path_inside_trusted(workspace, t) for t in targets):
            return True
    return False


def count_prior_approvals(owner: Any, family: str) -> int:
    try:
        with ce_store.db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM approval_shadow_log "
                "WHERE owner = ? AND family = ? AND actual_decision = 'approved'",
                (_owner_key(owner), family),
            ).fetchone()
            return int(row["n"]) if row else 0
    except (ce_store.ContextStoreError, sqlite3.Error) as exc:
        logger.debug("approval_autonomy: prior-approval count failed: %s", exc)
        return 0


def compute_confidence(tool_name: Any, content: Any, *, workspace: str = "",
                        owner: str = "", user_text: str = "",
                        capabilities: Any = None) -> ConfidenceResult:
    """0..1 confidence that this exact call is safe to fold into `active`
    mode's auto-approve, from signals already available to the gate. Never
    raises: an internal failure returns the lowest tier (`escalate`) with a
    reason explaining why, never a score that could accidentally promote
    anything."""
    try:
        from src.tool_capabilities import (
            ToolEffect, capabilities_for_action, _write_targets, path_inside_trusted,
        )
    except Exception:  # noqa: BLE001
        return ConfidenceResult(0.0, "escalate", {}, ["capability classifier unavailable"])
    try:
        caps = capabilities if capabilities is not None else capabilities_for_action(tool_name, content)
        effects = getattr(caps, "effects", frozenset())
        signals: Dict[str, float] = {}
        reasons: List[str] = []

        if ToolEffect.WRITE_WORKSPACE in effects:
            signals["readonly"] = 0.5
            reasons.append("writes inside the bound workspace")
        elif not effects or ToolEffect.READ_PRIVATE in effects:
            signals["readonly"] = 1.0
            reasons.append("a read-only action")
        else:
            signals["readonly"] = 0.0
            reasons.append("an effect class outside the promotable set")

        targets = _write_targets(str(tool_name or ""), content) or []

        named = 0.0
        text_low = str(user_text or "").lower()
        if text_low:
            for target in targets:
                base = target.replace("\\", "/").rsplit("/", 1)[-1]
                if base and base.lower() in text_low:
                    named = 1.0
                    break
            if not named and str(tool_name or "").strip().lower() in text_low:
                named = 1.0
        signals["named"] = named
        reasons.append(
            "the user's message named this tool or target" if named
            else "the user's message did not name a specific target"
        )

        if targets:
            root = workspace or ""
            inside = 1.0 if root and all(path_inside_trusted(root, t) for t in targets) else 0.0
        else:
            inside = 1.0  # nothing to write, "inside the workspace" is vacuously true
        signals["workspace"] = inside
        reasons.append(
            "confirmed inside the bound workspace" if inside >= 1.0
            else "a target could not be confirmed inside the workspace"
        )

        family = family_for(tool_name, content)
        prior_count = count_prior_approvals(owner, family) if owner else 0
        prior = min(1.0, prior_count / 5.0)
        signals["prior_approvals"] = prior
        reasons.append(
            f"{prior_count} prior approval(s) of this family by this user" if prior_count
            else "no prior approval history for this family"
        )

        reversible = 1.0
        if ToolEffect.WRITE_WORKSPACE in effects:
            try:
                import os
                reversible = 1.0 if (targets and os.path.exists(targets[0])) else 0.5
            except OSError:
                reversible = 0.5
        signals["reversible"] = reversible

        score = (
            0.30 * signals["readonly"] + 0.20 * signals["named"]
            + 0.15 * signals["workspace"] + 0.25 * signals["prior_approvals"]
            + 0.10 * signals["reversible"]
        )
        score = max(0.0, min(1.0, round(score, 4)))
        return ConfidenceResult(score=score, tier=tier_for_score(score),
                                signals=signals, reasons=reasons)
    except Exception as exc:  # noqa: BLE001 - never raise out of a gate check
        logger.debug("approval_autonomy: confidence computation failed: %s", exc)
        return ConfidenceResult(0.0, "escalate", {}, [f"confidence computation failed: {exc}"])


def record_shadow_decision(*, owner: Any, family: str, tool_name: str, workspace: str,
                            score: float, tier: str, source: str = "shadow",
                            destructive: bool = False, approval_id: str = "",
                            actual_decision: str = "", agreed: Optional[bool] = None) -> str:
    """Log one shadow (or already-resolved, for `active_auto`) decision.
    Never raises -- logging must never be able to block an approval card."""
    row_id = uuid.uuid4().hex
    now = ce_store.now_iso()
    try:
        with ce_store.db() as conn:
            conn.execute(
                "INSERT INTO approval_shadow_log "
                "(id, owner, family, tool_name, workspace, score, tier, source, destructive, "
                "actual_decision, agreed, approval_id, created_at, decided_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row_id, _owner_key(owner), str(family or ""), str(tool_name or ""),
                 _resolve_workspace(workspace), float(score), str(tier or ""), str(source or "shadow"),
                 int(bool(destructive)), str(actual_decision or ""),
                 (None if agreed is None else int(bool(agreed))),
                 str(approval_id or ""), now, now if actual_decision else ""),
            )
    except (ce_store.ContextStoreError, sqlite3.Error) as exc:
        logger.warning("approval_autonomy: record_shadow_decision failed: %s", exc)
    return row_id


def finalize_shadow_decision(*, approval_id: str, actual_decision: str) -> None:
    """Fill in what the user actually did for the shadow row `create()`
    logged for this `approval_id`, and compute agreement:

    * an ``act``-or-``advise``-tier row agrees with an approval, disagrees
      with a denial (both tiers leaned toward "let this through");
    * an ``escalate``-tier row agrees with a denial, disagrees with an
      approval (it leaned toward "make a human look at this").

    Only ``act``-tier rows count toward automatic promotion (`family_stats`)
    -- this still records agreement for `advise`/`escalate` rows because the
    per-family table is more informative with it, but a family is never
    promoted from anything except confident (`act`) history."""
    if not approval_id:
        return
    approved = str(actual_decision or "") == "approved"
    try:
        with ce_store.db() as conn:
            row = conn.execute(
                "SELECT id, tier FROM approval_shadow_log "
                "WHERE approval_id = ? AND actual_decision = '' "
                "ORDER BY created_at DESC LIMIT 1",
                (approval_id,),
            ).fetchone()
            if row is None:
                return
            tier = str(row["tier"])
            agreed = (not approved) if tier == "escalate" else approved
            conn.execute(
                "UPDATE approval_shadow_log SET actual_decision = ?, agreed = ?, decided_at = ? "
                "WHERE id = ?",
                (str(actual_decision or ""), int(agreed), ce_store.now_iso(), row["id"]),
            )
    except (ce_store.ContextStoreError, sqlite3.Error) as exc:
        logger.warning("approval_autonomy: finalize_shadow_decision failed: %s", exc)


def _load_overrides(owner: str = "") -> Dict[Tuple[str, str], str]:
    try:
        with ce_store.db() as conn:
            if owner:
                rows = conn.execute(
                    "SELECT owner, family, status FROM approval_family_overrides WHERE owner = ?",
                    (_owner_key(owner),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT owner, family, status FROM approval_family_overrides"
                ).fetchall()
    except (ce_store.ContextStoreError, sqlite3.Error) as exc:
        logger.debug("approval_autonomy: override read failed: %s", exc)
        return {}
    return {(str(r["owner"]), str(r["family"])): str(r["status"]) for r in rows}


def set_family_override(owner: Any, family: str, status: Optional[str]) -> None:
    """Manually promote/demote a family. `status` is ``"promoted"``,
    ``"demoted"``, or ``""``/``None`` to clear back to the computed value."""
    normalized = str(status or "").strip().lower()
    if normalized not in ("promoted", "demoted", ""):
        raise ValueError("status must be 'promoted', 'demoted', or '' to clear")
    owner_key = _owner_key(owner)
    family = str(family or "").strip()
    if not family:
        raise ValueError("family is required")
    try:
        with ce_store.db() as conn:
            if normalized == "":
                conn.execute(
                    "DELETE FROM approval_family_overrides WHERE owner = ? AND family = ?",
                    (owner_key, family),
                )
            else:
                conn.execute(
                    "INSERT INTO approval_family_overrides (owner, family, status, updated_at) "
                    "VALUES (?,?,?,?) "
                    "ON CONFLICT(owner, family) DO UPDATE SET "
                    "status = excluded.status, updated_at = excluded.updated_at",
                    (owner_key, family, normalized, ce_store.now_iso()),
                )
    except (ce_store.ContextStoreError, sqlite3.Error) as exc:
        logger.warning("approval_autonomy: set_family_override failed: %s", exc)
        raise


def _promoted_from_stats(agg: Dict[str, Any], override: Optional[str], *,
                          min_decisions: int = DEFAULT_PROMOTE_MIN_DECISIONS,
                          min_agreement: float = DEFAULT_PROMOTE_MIN_AGREEMENT) -> bool:
    if override == "demoted":
        return False
    if override == "promoted":
        return True
    if agg.get("destructive_disagree", 0) > 0:
        return False
    act_total = agg.get("act_total", 0)
    if act_total < min_decisions:
        return False
    return (agg.get("act_agree", 0) / act_total) >= min_agreement


def family_stats(owner: str = "") -> List[Dict[str, Any]]:
    """Per-(owner, family) shadow-log aggregate: totals, agreement rate and
    whether the family is (or would be) promoted. `owner=""` aggregates
    every owner -- for an admin-facing view across the whole install."""
    try:
        with ce_store.db() as conn:
            if owner:
                rows = ce_store.rows(conn.execute(
                    "SELECT * FROM approval_shadow_log WHERE owner = ? AND actual_decision != ''",
                    (_owner_key(owner),),
                ))
            else:
                rows = ce_store.rows(conn.execute(
                    "SELECT * FROM approval_shadow_log WHERE actual_decision != ''"
                ))
    except (ce_store.ContextStoreError, sqlite3.Error) as exc:
        logger.warning("approval_autonomy: family_stats read failed: %s", exc)
        return []

    by_family: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in rows:
        key = (str(row["owner"]), str(row["family"]))
        agg = by_family.setdefault(key, {
            "owner": key[0], "family": key[1], "total": 0,
            "act_total": 0, "act_agree": 0, "destructive_disagree": 0,
        })
        agg["total"] += 1
        if str(row["tier"]) == "act":
            agg["act_total"] += 1
            if row["agreed"]:
                agg["act_agree"] += 1
            elif row["destructive"]:
                agg["destructive_disagree"] += 1

    overrides = _load_overrides(owner)
    out: List[Dict[str, Any]] = []
    for key in sorted(by_family):
        agg = by_family[key]
        act_total = agg["act_total"]
        agg["agreement_rate"] = round(agg["act_agree"] / act_total, 4) if act_total else 0.0
        override = overrides.get(key)
        agg["override"] = override or None
        agg["promoted"] = _promoted_from_stats(agg, override)
        out.append(agg)
    return out


def recent_decisions(owner: str, family: str, limit: int = 20) -> List[Dict[str, Any]]:
    """The last `limit` shadow-log rows for one (owner, family), newest
    first -- the drilldown behind the per-family aggregate in `family_stats`.
    Each row already carries what shadow mode would have decided (`tier`,
    `score`) and, once resolved, what actually happened (`actual_decision`,
    `agreed`); a row still awaiting a human click has `actual_decision ==
    ""`. `owner=""` returns rows for every owner in this family (an admin-
    wide drilldown, matching `family_stats`'s own `owner=""` convention)."""
    limit = max(1, min(int(limit or 20), 200))
    try:
        with ce_store.db() as conn:
            if owner:
                rows = ce_store.rows(conn.execute(
                    "SELECT * FROM approval_shadow_log WHERE owner = ? AND family = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (_owner_key(owner), family, limit),
                ))
            else:
                rows = ce_store.rows(conn.execute(
                    "SELECT * FROM approval_shadow_log WHERE family = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (family, limit),
                ))
    except (ce_store.ContextStoreError, sqlite3.Error) as exc:
        logger.warning("approval_autonomy: recent_decisions read failed: %s", exc)
        return []
    out: List[Dict[str, Any]] = []
    for row in rows:
        out.append({
            "id": row["id"],
            "owner": row["owner"],
            "family": row["family"],
            "tool_name": row["tool_name"],
            "score": row["score"],
            "tier": row["tier"],
            "source": row["source"],
            "destructive": bool(row["destructive"]),
            "actual_decision": row["actual_decision"] or None,
            "agreed": (None if row["agreed"] is None else bool(row["agreed"])),
            "created_at": row["created_at"],
            "decided_at": row["decided_at"] or None,
        })
    return out


def is_family_promoted(owner: Any, family: str) -> bool:
    owner_key = _owner_key(owner)
    for row in family_stats(owner_key):
        if row["family"] == family:
            return bool(row["promoted"])
    # No shadow history at all for this family: a manual override can still
    # promote it outright.
    return _load_overrides(owner_key).get((owner_key, family)) == "promoted"


__all__ = [
    "MODES", "TIERS", "ACT_THRESHOLD", "ADVISE_THRESHOLD",
    "DEFAULT_PROMOTE_MIN_DECISIONS", "DEFAULT_PROMOTE_MIN_AGREEMENT",
    "PROMOTABLE_EFFECTS", "ConfidenceResult",
    "autonomy_mode", "tier_for_score", "family_for", "dedup_key",
    "is_hard_blocked", "compute_confidence", "count_prior_approvals",
    "record_shadow_decision", "finalize_shadow_decision",
    "set_family_override", "family_stats", "recent_decisions", "is_family_promoted",
]
