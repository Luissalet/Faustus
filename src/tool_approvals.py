"""Opaque exact-action approvals with explicit task and chat scopes.

The server still seals and claims the first displayed action exactly once. The
selected scope then bypasses only the automatic post-external-context approval
gate for the rest of the resumed task or chat session. Browser-visible fields
are display copies, never authority.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from src.tool_approval_scopes import (
    CHAT_SESSION_APPROVAL_DECISION,
    DENY_APPROVAL_DECISION,
    TASK_APPROVAL_DECISION,
    WORKSPACE_APPROVAL_DECISION,
    ToolApprovalScope,
    scope_for_decision,
)
from src.tool_capabilities import ToolCapabilities, capabilities_for_action

logger = logging.getLogger(__name__)


def _ttl_from_env(default_seconds: int) -> int:
    """Operator override for the approval deadline (seconds)."""
    raw = os.environ.get("TOOL_APPROVAL_TTL_SECONDS", "").strip()
    if not raw:
        return default_seconds
    try:
        parsed = int(float(raw))
    except (TypeError, ValueError):
        return default_seconds
    # A zero/negative override would make every card expire on arrival.
    return parsed if parsed > 0 else default_seconds


# How long a parked approval card stays answerable.
#
# This is a *human review deadline*, not a session timeout: the card shows a
# diff and the user is supposed to read it. Ten minutes was measured against a
# fast model and broke the moment a real one was used — a local model at
# ~1 tok/s leaves the card on screen for half an hour, and the click that
# finally arrived was refused with a 409, dropping an approved patch.
#
# The window is ABSOLUTE from creation and nothing extends it. A sliding TTL
# would have to be refreshed by the browser, which is the wrong direction of
# trust: an open tab (or a page influenced by untrusted content) would keep a
# privileged sealed action alive indefinitely, and "expires 30 minutes after
# you last twitched" is not a bound anyone can audit. A fixed deadline the
# server picked is. Users who exceed it are not stranded — the gate reports
# ``tool_approval_expired`` and the UI offers to rerun the turn.
DEFAULT_APPROVAL_TTL_SECONDS = _ttl_from_env(30 * 60)
DEFAULT_MAX_PENDING_APPROVALS = 2048
# Bounded memory of approvals dropped by the TTL, so the gate can answer
# "expired" instead of the misleading "invalid, or belongs to another thread".
DEFAULT_MAX_EXPIRED_MEMORY = 512


def _normalized_owner(owner: Any) -> str:
    return str(owner or "").strip().casefold()


def _normalized_workspace(workspace: Any) -> str:
    if not isinstance(workspace, str) or not workspace.strip():
        return ""
    return os.path.realpath(os.path.expanduser(workspace))


_MAX_APPROVAL_SELECTED_TOOLS = 512
_MAX_APPROVAL_TOOL_NAME_CHARS = 512
_MAX_APPROVAL_CONTINUATION_QUERY_CHARS = 4000


def _normalized_selected_tools(
    selected_tools: Any,
    *,
    required_tool: Any = None,
) -> tuple[str, ...]:
    if isinstance(selected_tools, str):
        selected_tools = (selected_tools,)
    try:
        values = selected_tools or ()
        names = {
            name.strip()
            for name in values
            if (
                isinstance(name, str)
                and name.strip()
                and len(name.strip()) <= _MAX_APPROVAL_TOOL_NAME_CHARS
            )
        }
        required_name = str(required_tool or "").strip()
        if required_name and len(required_name) <= _MAX_APPROVAL_TOOL_NAME_CHARS:
            names.add(required_name)
        ordered = sorted(names)
        if len(ordered) <= _MAX_APPROVAL_SELECTED_TOOLS:
            return tuple(ordered)
        kept = ordered[:_MAX_APPROVAL_SELECTED_TOOLS]
        if required_name and required_name in names and required_name not in kept:
            kept[-1] = required_name
            kept.sort()
        return tuple(kept)
    except TypeError:
        return ()


def _normalized_continuation_query(value: Any) -> str:
    # The query is server-derived from the interrupted run and already lives in
    # session history. Keep the pending copy bounded because approvals are held
    # in memory until consumed or expired.
    return str(value or "").strip()[:_MAX_APPROVAL_CONTINUATION_QUERY_CHARS]


def _canonical_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def document_content_digest(content: Any) -> str:
    """Return the stable server-side fingerprint used to seal a document."""
    return hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()


def _binding_payload(
    *,
    owner: Any,
    session_id: Any,
    origin_run_id: Any,
    tool_name: Any,
    content: Any,
    workspace: Any,
    document_id: Any,
    document_version: Any,
    document_digest: Any,
    external_untrusted_context_seen: bool,
    selected_tools: Any,
    continuation_query: Any,
    effects: tuple[str, ...],
    result_integrity: str,
) -> dict[str, Any]:
    return {
        "owner": _normalized_owner(owner),
        "session_id": str(session_id or ""),
        "origin_run_id": str(origin_run_id or ""),
        "tool_name": str(tool_name or ""),
        "content": str(content or ""),
        "workspace": _normalized_workspace(workspace),
        "document_id": str(document_id or ""),
        "document_version": (
            int(document_version) if document_version is not None else None
        ),
        "document_digest": str(document_digest or "").strip().lower(),
        "external_untrusted_context_seen": bool(external_untrusted_context_seen),
        "selected_tools": list(
            _normalized_selected_tools(selected_tools, required_tool=tool_name)
        ),
        "continuation_query": _normalized_continuation_query(continuation_query),
        "effects": list(effects),
        "result_integrity": str(result_integrity),
    }


# Both denial shapes the destructive-command guard produces
# (`command_guard.gate_check`'s DANGEROUS-tier denial and its
# `_degraded_denial` for an unclassified command, plus
# `tool_capabilities.GUARD_FAILURE_DENIAL` for a broken guard) end by
# pointing the user at the allowlist endpoint — nothing else that denies
# through `decision_for` does, so it is the cheapest reliable way to tell
# "a specific dangerous command was stopped" apart from the generic
# post-external-context gate (whose card is meant to stay the same
# "Allow this task to continue?" question it always was).
_DESTRUCTIVE_GUARD_MARKER = "/api/command-guard/allowlist"
_APPROVAL_QUESTION_COMMAND_CHARS = 120


def _approval_card_question(reason: str | None, content: Any) -> str:
    """The approval card's `question` — specific for a destructive-command
    guard denial (names the command and why it was stopped), the long-
    standing generic wording for every other gate (external context, a
    desktop-input confirmation, a teacher-generated skill, ...)."""
    reason_text = (reason or "").strip()
    if not reason_text or _DESTRUCTIVE_GUARD_MARKER not in reason_text:
        return "Allow this task to continue?"
    try:
        from src import command_guard
        preview = command_guard.command_preview(content, max_len=4000)
    except Exception:  # noqa: BLE001 - never block an approval card on this
        preview = str(content or "")
    preview = preview.strip()
    if len(preview) > _APPROVAL_QUESTION_COMMAND_CHARS:
        preview = preview[:_APPROVAL_QUESTION_COMMAND_CHARS].rstrip() + "…"
    return f"Run this destructive command? {preview} — {reason_text}"


@dataclass(frozen=True)
class PendingToolApproval:
    approval_id: str
    owner: str
    session_id: str
    origin_run_id: str
    tool_name: str
    content: str
    workspace: str
    document_id: str
    document_version: int | None
    document_digest: str
    external_untrusted_context_seen: bool
    effects: tuple[str, ...]
    result_integrity: str
    digest: str
    created_at: float
    expires_at: float
    # Server-only continuation state. Both fields are digest-bound and never
    # exposed in the browser payload.
    selected_tools: tuple[str, ...] = ()
    continuation_query: str = ""
    # Shadow-mode / active-mode autonomy (src.approval_autonomy): a short,
    # human-readable confidence note. Empty in "off" and "shadow" modes (both
    # leave the card byte-identical to before this field existed); set only
    # in "active" mode for a call that was NOT auto-approved outright, so the
    # person reviewing it sees why the check leaned the way it did.
    autonomy_note: str = ""

    def public_payload(self, *, reason: str | None = None) -> dict[str, Any]:
        question = _approval_card_question(reason, self.content)
        payload = {
            "kind": "tool_approval",
            "approval_id": self.approval_id,
            # The browser already owns this chat id. Persisting it with the
            # resolved card lets history-derived session grants remain bound to
            # this exact chat and prevents inheritance by a forked session.
            "session_id": self.session_id,
            "question": question,
            "description": reason or (
                "Untrusted context influenced this run, so continuing with "
                "otherwise-gated actions needs your explicit approval."
            ),
            "options": [
                {
                    "label": "Allow for this task",
                    "value": TASK_APPROVAL_DECISION,
                    "description": (
                        "Execute the sealed action and allow every otherwise-gated "
                        "action needed to finish this request. Current tool, account, "
                        "workspace, and sandbox restrictions still apply."
                    ),
                },
                {
                    "label": "Allow for this chat session",
                    "value": CHAT_SESSION_APPROVAL_DECISION,
                    "description": (
                        "Execute the sealed action and stop asking at this gate for "
                        "later requests in this chat. Current tool, account, workspace, "
                        "and sandbox restrictions still apply."
                    ),
                },
                {
                    "label": "Always for this workspace folder",
                    "value": WORKSPACE_APPROVAL_DECISION,
                    "description": (
                        "Execute the sealed action and remember the answer for this "
                        "workspace folder: later chats in it stop asking at this gate. "
                        "Destructive-command and desktop-input confirmations, tool, "
                        "account and sandbox restrictions still apply."
                    ),
                },
                {
                    "label": "Deny",
                    "value": DENY_APPROVAL_DECISION,
                    "description": "Do not execute the proposed action.",
                },
            ],
            "action": self._action_payload(),
        }
        if self.autonomy_note:
            payload["autonomy_note"] = self.autonomy_note
        return payload

    def _action_payload(self) -> dict[str, Any]:
        action: dict[str, Any] = {
            "tool": self.tool_name,
            # Show the complete sealed input so approval never hides
            # trailing lines.  This is not read back as authority.
            "content": self.content,
            "digest": self.digest[:16],
            "effects": list(self.effects),
            "workspace": self.workspace or None,
            "document_id": self.document_id or None,
            "document_version": self.document_version,
        }
        # EXEC-02: the same secret-masked preview a tool result carries — an
        # approval card is the FIRST look the user gets at a shell command,
        # so it must never be the one place a raw secret shows up verbatim.
        try:
            from src import command_guard
            action["command_preview"] = command_guard.command_preview(self.content)
        except Exception:  # noqa: BLE001 - never block an approval card on this
            pass
        # EXEC-01: where this would run, for the tools that actually reach a
        # shell — best-effort (the sandbox decision itself is made at
        # execution time), but "sandboxed container vs. this host" is known
        # before that, from the same setting subprocess_tools reads.
        if self.tool_name in ("bash", "python", "powershell"):
            try:
                from src.agent_tools.subprocess_tools import _execution_target
                from src import sandbox_exec
                action["execution_target"] = _execution_target(
                    sandboxed=sandbox_exec.enabled(), cwd=self.workspace or "", shell="",
                )
            except Exception:  # noqa: BLE001 - never block an approval card on this
                pass
        return action


@dataclass
class ExactToolApproval:
    """A consumed exact first action plus an explicit continuation scope."""

    pending: PendingToolApproval
    scope: ToolApprovalScope = ToolApprovalScope.TASK
    # The seam consumed by agent_loop. Both chat-card allow choices cover the
    # complete resumed task, because one-action scope there immediately
    # re-entered the same gate on the next round. Callers with no resumable
    # chat still get SINGLE_ACTION, which leaves the gate armed behind the
    # sealed action.
    allow_remaining_actions: bool = True
    _claimed: bool = field(default=False, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @property
    def grants_chat_session(self) -> bool:
        # A workspace grant covers this chat too — it is the chat grant plus
        # a record on disk (src/tool_approval_grants.py).
        return self.scope in (ToolApprovalScope.CHAT_SESSION, ToolApprovalScope.WORKSPACE)

    @property
    def grants_workspace(self) -> bool:
        return self.scope is ToolApprovalScope.WORKSPACE

    def _matches_unlocked(
        self,
        *,
        owner: Any,
        session_id: Any,
        tool_name: Any,
        content: Any,
        workspace: Any,
    ) -> bool:
        if self._claimed:
            return False
        capabilities = capabilities_for_action(tool_name, content)
        effects = tuple(sorted(effect.value for effect in capabilities.effects))
        result_integrity = capabilities.result_integrity.value
        if (
            effects != self.pending.effects
            or result_integrity != self.pending.result_integrity
        ):
            return False
        expected = _binding_payload(
            owner=owner,
            session_id=session_id,
            origin_run_id=self.pending.origin_run_id,
            tool_name=tool_name,
            content=content,
            workspace=workspace,
            document_id=self.pending.document_id,
            document_version=self.pending.document_version,
            document_digest=self.pending.document_digest,
            external_untrusted_context_seen=(
                self.pending.external_untrusted_context_seen
            ),
            selected_tools=self.pending.selected_tools,
            continuation_query=self.pending.continuation_query,
            effects=effects,
            result_integrity=result_integrity,
        )
        return _canonical_digest(expected) == self.pending.digest

    def matches(
        self,
        *,
        owner: Any,
        session_id: Any,
        tool_name: Any,
        content: Any,
        workspace: Any,
    ) -> bool:
        with self._lock:
            return self._matches_unlocked(
                owner=owner,
                session_id=session_id,
                tool_name=tool_name,
                content=content,
                workspace=workspace,
            )

    def claim(
        self,
        *,
        owner: Any,
        session_id: Any,
        tool_name: Any,
        content: Any,
        workspace: Any,
    ) -> bool:
        with self._lock:
            if not self._matches_unlocked(
                owner=owner,
                session_id=session_id,
                tool_name=tool_name,
                content=content,
                workspace=workspace,
            ):
                return False
            self._claimed = True
            return True


def _autonomy_shadow_log(*, approval_id: str, owner: Any, tool_name: Any, content: Any,
                          workspace: str, user_text: str, capabilities: ToolCapabilities) -> str:
    """Best-effort bridge to `src.approval_autonomy`, called once per card
    created here. Mode "off" (the default) is a single settings read and
    nothing else -- no import, no score, no row, no note: the card this
    function returns for "off" is exactly the empty string it always
    returned before this function existed. Never raises: a failure in the
    autonomy module must never block an approval card."""
    try:
        from src import approval_autonomy as autonomy
    except Exception:  # noqa: BLE001
        return ""
    try:
        mode = autonomy.autonomy_mode()
        if mode == "off":
            return ""
        blocked = capabilities.effects & set(autonomy.PROMOTABLE_EFFECTS)
        if not blocked or blocked != capabilities.effects:
            # Only log/annotate the class of action `active` mode could ever
            # auto-approve (see `PROMOTABLE_EFFECTS`) -- a `bash` or
            # `send_email` card is untouched, in every mode.
            return ""
        result = autonomy.compute_confidence(
            tool_name, content, workspace=workspace, owner=owner,
            user_text=user_text, capabilities=capabilities,
        )
        from src.tool_capabilities import ToolEffect
        autonomy.record_shadow_decision(
            owner=owner, family=autonomy.family_for(tool_name, content),
            tool_name=str(tool_name or ""), workspace=workspace,
            score=result.score, tier=result.tier, source="shadow",
            destructive=(ToolEffect.DESTRUCTIVE in capabilities.effects),
            approval_id=approval_id,
        )
        if mode != "active":
            return ""
        return (
            f"Autonomy check: {result.tier} ({result.score:.0%} confidence) — "
            + "; ".join(result.reasons[:3]) + "."
        )
    except Exception:  # noqa: BLE001 - see docstring
        logger.warning("approval_autonomy shadow log failed", exc_info=True)
        return ""


def _finalize_autonomy_shadow(*, approval_id: str, scope: Any, normalized_decision: str) -> None:
    """Fill in what the user actually clicked for the shadow row `create()`
    logged (a no-op if that row does not exist -- mode "off", or a card
    whose action class `_autonomy_shadow_log` never logs). A resolved scope
    is an approval of some breadth; `scope is None` together with the
    literal deny wire value is a denial. Any other unmapped decision string
    is neither and is left unresolved rather than guessed at."""
    try:
        from src import approval_autonomy as autonomy
        if autonomy.autonomy_mode() == "off":
            return
        if scope is not None:
            actual = "approved"
        elif normalized_decision == DENY_APPROVAL_DECISION:
            actual = "denied"
        else:
            return
        autonomy.finalize_shadow_decision(approval_id=approval_id, actual_decision=actual)
    except Exception:  # noqa: BLE001 - finalizing a log must never break consumption
        logger.warning("approval_autonomy finalize failed", exc_info=True)


class ToolApprovalStore:
    """Thread-safe pending approval registry with destructive consumption."""

    def __init__(
        self,
        *,
        ttl_seconds: int = DEFAULT_APPROVAL_TTL_SECONDS,
        max_pending: int = DEFAULT_MAX_PENDING_APPROVALS,
        max_expired_memory: int = DEFAULT_MAX_EXPIRED_MEMORY,
        max_consumed_memory: int = DEFAULT_MAX_EXPIRED_MEMORY,
    ):
        self._ttl_seconds = max(1, int(ttl_seconds))
        self._max_pending = max(1, int(max_pending))
        self._max_expired_memory = max(1, int(max_expired_memory))
        self._max_consumed_memory = max(1, int(max_consumed_memory))
        self._pending: dict[str, PendingToolApproval] = {}
        # approval_id -> normalized owner, for approvals the TTL dropped. Only
        # the owner is kept: enough to tell that user "this expired, rerun the
        # turn", and nothing a stranger holding a leaked id could learn from.
        # Insertion-ordered and capped, so it can never grow without bound.
        self._expired: dict[str, str] = {}
        # A02: approval_id -> {owner, tool_name, consumed_at}, for approvals
        # `consume` already popped. Recorded under the SAME lock that pops
        # `_pending`, so a second concurrent `consume` racing the first one
        # sees either the pending card (and wins) or this tombstone (and can
        # answer "already_consumed" instead of the indistinguishable-from-
        # "never existed" `None` a bare pop-and-return gave before — see
        # `consume_with_reason`. Bounded the same way `_expired` is.
        self._consumed: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        # Optional file the pending cards are mirrored to, so a server
        # restart inside the TTL does not strand a paused turn (live,
        # 24-09-2026: a restart turned every open card into a 409 and the
        # turn could only be started over). Off unless `enable_persistence`.
        self._persist_path: str | None = None

    # -- persistence ------------------------------------------------------------
    def enable_persistence(self, path: str) -> int:
        """Mirror pending approvals to ``path`` and load the ones already
        there that have not expired. Returns how many were loaded. The TTL
        stays absolute from creation: a restart never extends a card."""
        now = time.time()
        loaded = 0
        try:
            with open(path, encoding="utf-8") as fh:
                rows = json.load(fh)
        except FileNotFoundError:
            rows = []
        except Exception as exc:  # noqa: BLE001 - a corrupt file starts empty
            logger.warning("[approvals] could not read %s: %s", path, exc)
            rows = []
        with self._lock:
            self._persist_path = path
            for row in rows if isinstance(rows, list) else []:
                try:
                    row = dict(row)
                    row["effects"] = tuple(row.get("effects") or ())
                    row["selected_tools"] = tuple(row.get("selected_tools") or ())
                    pending = PendingToolApproval(**row)
                except Exception:  # noqa: BLE001 - skip a row that no longer fits
                    continue
                if pending.expires_at <= now or pending.approval_id in self._pending:
                    continue
                self._pending[pending.approval_id] = pending
                loaded += 1
            self._persist_locked()
        if loaded:
            logger.info("[approvals] restored %d pending approval card(s) from %s", loaded, path)
        return loaded

    def _persist_locked(self) -> None:
        path = self._persist_path
        if not path:
            return
        try:
            from dataclasses import asdict
            rows = [asdict(p) for p in self._pending.values()]
            tmp = f"{path}.tmp"
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(rows, fh)
            os.replace(tmp, path)
        except Exception as exc:  # noqa: BLE001 - persistence is best effort
            logger.warning("[approvals] could not write %s: %s", path, exc)

    def _remember_expired_locked(self, pending: PendingToolApproval) -> None:
        self._expired.pop(pending.approval_id, None)
        self._expired[pending.approval_id] = pending.owner
        while len(self._expired) > self._max_expired_memory:
            self._expired.pop(next(iter(self._expired)), None)

    def _remember_consumed_locked(self, pending: PendingToolApproval) -> None:
        self._consumed.pop(pending.approval_id, None)
        self._consumed[pending.approval_id] = {
            "owner": pending.owner,
            "tool_name": pending.tool_name,
            "consumed_at": time.time(),
        }
        while len(self._consumed) > self._max_consumed_memory:
            self._consumed.pop(next(iter(self._consumed)), None)

    def _purge_expired_locked(self, now: float) -> None:
        expired = [
            approval_id
            for approval_id, pending in self._pending.items()
            if pending.expires_at <= now
        ]
        for approval_id in expired:
            dropped = self._pending.pop(approval_id, None)
            if dropped is not None:
                self._remember_expired_locked(dropped)
        if expired:
            self._persist_locked()

    def create(
        self,
        *,
        owner: Any,
        session_id: Any,
        origin_run_id: Any,
        tool_name: Any,
        content: Any,
        workspace: Any,
        document_id: Any = None,
        document_version: Any = None,
        document_digest: Any = None,
        selected_tools: Any = None,
        continuation_query: Any = None,
        external_untrusted_context_seen: bool,
        capabilities: ToolCapabilities,
    ) -> PendingToolApproval:
        now = time.time()
        effects = tuple(sorted(effect.value for effect in capabilities.effects))
        result_integrity = capabilities.result_integrity.value
        payload = _binding_payload(
            owner=owner,
            session_id=session_id,
            origin_run_id=origin_run_id,
            tool_name=tool_name,
            content=content,
            workspace=workspace,
            document_id=document_id,
            document_version=document_version,
            document_digest=document_digest,
            external_untrusted_context_seen=external_untrusted_context_seen,
            selected_tools=selected_tools,
            continuation_query=continuation_query,
            effects=effects,
            result_integrity=result_integrity,
        )
        approval_id = secrets.token_urlsafe(32)
        autonomy_note = _autonomy_shadow_log(
            approval_id=approval_id, owner=payload["owner"], tool_name=tool_name,
            content=content, workspace=payload["workspace"],
            user_text=payload["continuation_query"], capabilities=capabilities,
        )
        pending = PendingToolApproval(
            approval_id=approval_id,
            owner=payload["owner"],
            session_id=payload["session_id"],
            origin_run_id=payload["origin_run_id"],
            tool_name=payload["tool_name"],
            content=payload["content"],
            workspace=payload["workspace"],
            document_id=payload["document_id"],
            document_version=payload["document_version"],
            document_digest=payload["document_digest"],
            external_untrusted_context_seen=payload[
                "external_untrusted_context_seen"
            ],
            effects=effects,
            result_integrity=result_integrity,
            digest=_canonical_digest(payload),
            created_at=now,
            expires_at=now + self._ttl_seconds,
            selected_tools=tuple(payload["selected_tools"]),
            continuation_query=payload["continuation_query"],
            autonomy_note=autonomy_note,
        )
        with self._lock:
            self._purge_expired_locked(now)
            # The chat UI exposes one pending card per session, so supersede an
            # older action there. Headless/manual-test callers use an empty
            # session id; keep independent origin runs separate so two skill
            # tests owned by the same user cannot invalidate each other.
            superseded = [
                approval_id
                for approval_id, existing in self._pending.items()
                if (
                    existing.owner == pending.owner
                    and existing.session_id == pending.session_id
                    and (
                        bool(pending.session_id)
                        or existing.origin_run_id == pending.origin_run_id
                    )
                )
            ]
            for approval_id in superseded:
                self._pending.pop(approval_id, None)
            while len(self._pending) >= self._max_pending:
                oldest_id = min(
                    self._pending,
                    key=lambda approval_id: self._pending[approval_id].created_at,
                )
                self._pending.pop(oldest_id, None)
            self._pending[pending.approval_id] = pending
            self._persist_locked()
        return pending

    def consume(
        self,
        approval_id: Any,
        *,
        decision: Any,
        owner: Any,
        session_id: Any,
        allow_continuation: bool = True,
    ) -> ExactToolApproval | None:
        """Consume a pending approval.

        ``allow_continuation`` is the caller's assertion that it owns a
        resumable conversation the granted scope can apply to. Callers without
        one (the skill tester, unattended audits) pass ``False`` and get the
        original one-use grant, so a button labelled "Allow once" cannot widen
        into a run-long bypass just because the chat card reuses the same wire
        value.

        A02: a bare wrapper over `consume_with_reason` that keeps this exact
        signature and return shape for existing callers (routes/chat_routes.py,
        routes/skills_routes.py, src/task_scheduler.py) — see that method for
        why a plain ``None`` here is ambiguous between five different reasons
        a caller that only needs the ``ExactToolApproval`` never had to care
        about.
        """
        _reason, approval = self.consume_with_reason(
            approval_id, decision=decision, owner=owner, session_id=session_id,
            allow_continuation=allow_continuation,
        )
        return approval

    def consume_with_reason(
        self,
        approval_id: Any,
        *,
        decision: Any,
        owner: Any,
        session_id: Any,
        allow_continuation: bool = True,
    ) -> tuple[str, ExactToolApproval | None]:
        """Consume a pending approval, typed: `consume`'s ambiguous ``None``
        collapsed five different situations into one — the same trap A02's
        HTTP approval races were built to catch. Returns
        ``(reason, approval_or_None)``:

        * ``"consumed"`` — this call won it; `approval` is set.
        * ``"already_consumed"`` — a concurrent `consume`/`consume_with_reason`
          on the SAME `approval_id` already popped it (this store's bounded
          `_consumed` tombstone still remembers who).
        * ``"expired"`` — the TTL dropped it before this call arrived, and
          `owner` matches who it belonged to (see `was_expired`; a mismatched
          owner gets "not_found" instead — a leaked id must not reveal that
          an approval for someone else ever existed).
        * ``"owner_mismatch"`` — a pending card exists for this exact
          `approval_id` but a different owner/session; NOT popped (a stranger
          holding a leaked/guessed id cannot invalidate another owner's
          pending action just by trying).
        * ``"not_found"`` — never existed, or existed and expired long enough
          ago that even the bounded expiry memory no longer has it.
        * ``"invalid_decision"`` — the card WAS this caller's to consume and
          IS now consumed (no re-consuming it), but `decision` did not map to
          a known scope (`scope_for_decision`), so there is no `approval` to
          return. Not one of the four acceptance-parity reasons the contract
          names, but real and distinct from all of them.
        """
        now = time.time()
        with self._lock:
            self._purge_expired_locked(now)
            approval_key = str(approval_id or "")
            pending = self._pending.get(approval_key)
            if pending is None:
                if approval_key in self._consumed:
                    return "already_consumed", None
                if self._expired.get(approval_key) == _normalized_owner(owner):
                    return "expired", None
                return "not_found", None
            if (
                pending.owner != _normalized_owner(owner)
                or pending.session_id != str(session_id or "")
            ):
                # Authentication is checked before destructive consumption so
                # a leaked/guessed opaque id cannot be used to invalidate
                # another owner's pending action.
                return "owner_mismatch", None
            self._pending.pop(approval_key, None)
            self._remember_consumed_locked(pending)
            self._persist_locked()
        normalized_decision = str(decision or "").strip().lower()
        scope = scope_for_decision(normalized_decision)
        _finalize_autonomy_shadow(
            approval_id=approval_key, scope=scope, normalized_decision=normalized_decision,
        )
        if scope is None:
            return "invalid_decision", None
        if not allow_continuation:
            return "consumed", ExactToolApproval(
                pending,
                scope=ToolApprovalScope.SINGLE_ACTION,
                allow_remaining_actions=False,
            )
        return "consumed", ExactToolApproval(
            pending,
            scope=scope,
            allow_remaining_actions=True,
        )

    def pending_session_ids(self, *, owner: Any) -> list[str]:
        """Sessions of this owner that are parked on an approval card."""
        now = time.time()
        normalized_owner = _normalized_owner(owner)
        with self._lock:
            self._purge_expired_locked(now)
            return sorted({
                pending.session_id
                for pending in self._pending.values()
                if pending.owner == normalized_owner and pending.session_id
            })

    def peek(self, approval_id: Any) -> PendingToolApproval | None:
        now = time.time()
        with self._lock:
            self._purge_expired_locked(now)
            return self._pending.get(str(approval_id or ""))

    def was_expired(self, approval_id: Any, *, owner: Any) -> bool:
        """Whether this owner's approval was dropped by the TTL.

        Lets the gate answer "your approval expired, rerun the turn" instead of
        the catch-all "invalid, expired, or belongs to another thread" — the
        difference between a dead end and a way forward. Owner-scoped so a
        leaked opaque id still tells a stranger nothing.
        """
        now = time.time()
        normalized_owner = _normalized_owner(owner)
        with self._lock:
            self._purge_expired_locked(now)
            return self._expired.get(str(approval_id or "")) == normalized_owner

    def expire_now(self, approval_id: Any) -> bool:
        """Expire a pending approval immediately (tests, admin revocation)."""
        with self._lock:
            pending = self._pending.pop(str(approval_id or ""), None)
            if pending is None:
                return False
            self._remember_expired_locked(pending)
            self._persist_locked()
            return True

    def retire_for_session(self, *, owner: Any, session_id: Any) -> bool:
        """Discard pending actions superseded by an ordinary user turn.

        Returns whether any retired action carried external provenance, so the
        caller can preserve that security state without treating the new user
        message as an approval continuation.

        Thin wrapper over `retire_for_session_ids` for every caller that only
        ever needed the taint bool -- same signature, same return value, byte
        for byte, as before that method existed.
        """
        _ids, carried_taint = self.retire_for_session_ids(owner=owner, session_id=session_id)
        return carried_taint

    def retire_for_session_ids(self, *, owner: Any, session_id: Any) -> tuple[list[str], bool]:
        """A07 (docs/spec/paridad/): same retirement as `retire_for_session`,
        plus the approval_ids actually retired -- `chat_stop`'s cleanup report
        needs to say WHICH approvals it retired for the cancelled turn and its
        stopped workers, not just whether any carried external provenance."""
        now = time.time()
        normalized_owner = _normalized_owner(owner)
        normalized_session = str(session_id or "")
        if not normalized_session:
            return [], False
        with self._lock:
            self._purge_expired_locked(now)
            retired_ids = [
                approval_id
                for approval_id, pending in self._pending.items()
                if (
                    pending.owner == normalized_owner
                    and pending.session_id == normalized_session
                )
            ]
            carried_taint = any(
                self._pending[approval_id].external_untrusted_context_seen
                for approval_id in retired_ids
            )
            for approval_id in retired_ids:
                self._pending.pop(approval_id, None)
            if retired_ids:
                self._persist_locked()
        return retired_ids, carried_taint


tool_approval_store = ToolApprovalStore()
