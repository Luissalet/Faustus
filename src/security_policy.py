"""security_policy.py — SEC-08: proportional, auditable permission policy.

Four risk profiles, one decision function, one audit trail, and one consent
ledger keyed so a plugin update never has to be all-or-nothing:

* **Profiles are risk tiers, not a new taxonomy.** Each profile is a set of
  `src.tool_capabilities.ToolEffect` values — the SAME closed vocabulary
  `src.autonomy_budget` already uses for `read_only`. `PROFILES["read"]` is
  literally `autonomy_budget.READ_ONLY_ALLOWED_EFFECTS`, reused rather than
  redefined, so the two modules can never quietly disagree about what "read"
  means. `local_edit`, `supervised_agent` and `bounded_automation` extend it
  effect by effect — never the other direction: nothing here can make an
  effect READABLE under a narrower profile than the one below it.

* **A decision always names why.** `evaluate()` never returns a bare
  True/False; every `PolicyDecision` carries the effect(s) that decided it
  and is appended to a bounded, in-process audit trail (`audit_log`,
  `export_audit`) — "proportional AND auditable" is the requirement, and a
  policy that could not say why it denied something would only be the first
  half.

* **Consent is keyed per (plugin, tool), never per plugin.** That is the
  entire mechanism behind this module's acceptance case: a plugin update
  that adds a permission to ONE tool invalidates consent for exactly that
  tool — `ConsentStore.check` compares THAT tool's newly declared effects
  against what THAT tool was last granted — and every sibling tool under the
  same plugin, whose declared effects did not change, is untouched because
  its own record was never read, let alone written. There is no
  plugin-wide consent flag to invalidate in the first place.

* **Confirmation fatigue is a named failure, not an oversight.** Effects a
  profile already permits outright (a `read` profile's own reads, a
  `bounded_automation` profile's already-allowed writes) never carry
  `requires_approval`; only `ALWAYS_APPROVAL_EFFECTS` (admin changes,
  destructive effects) do, in every profile that permits them at all — a
  user who authorised a profile is not asked to re-authorise the same
  low-risk action on every call.

Nothing here executes a tool, denies a tool call on its own, or replaces
`src.subagent_permissions` / `src.tool_execution`'s own sandbox — this module
answers "would this be allowed, and why", the same read-only relationship
`src.completion_engine.scope` has with the run it advises.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from src import autonomy_budget
from src.tool_capabilities import ToolEffect

logger = logging.getLogger(__name__)

__all__ = [
    "PROFILES", "PROFILE_CONSEQUENCE", "ALWAYS_APPROVAL_EFFECTS",
    "DEFAULT_PROFILE", "normalize_profile", "PolicyDecision", "evaluate",
    "audit_log", "export_audit", "clear_audit_log", "audit_log_path",
    "ConsentRecord", "ConsentStore", "consent_store", "permission_fingerprint",
    "effects_for_declared_permissions",
]

# ── profiles: risk tiers built from the existing effect vocabulary ─────────

#: "lectura" — read-only, byte-for-byte the same set `autonomy_budget` already
#: gates the `read_only` preset with. Reused, not copied: a change to that
#: set changes this profile too, on purpose.
_READ: FrozenSet[ToolEffect] = autonomy_budget.READ_ONLY_ALLOWED_EFFECTS

#: "edición local" — reads plus changing files and running code IN the
#: workspace. No network egress, no external or UI side effect, no admin or
#: destructive change: this profile cannot reach past the local sandbox.
_LOCAL_EDIT: FrozenSet[ToolEffect] = _READ | frozenset({
    ToolEffect.WRITE_WORKSPACE, ToolEffect.EXECUTE_CODE,
})

#: "agente supervisado" — local edit plus reaching outward (network egress,
#: an external or UI side effect, writing somewhere private) AND, unlike
#: `read`/`local_edit`, admin/destructive effects — the profile a person is
#: watching, so it may go further than `local_edit`, but never quietly: every
#: admin/destructive effect it permits still asks every time
#: (`ALWAYS_APPROVAL_EFFECTS` below governs THAT, not membership here).
_SUPERVISED_AGENT: FrozenSet[ToolEffect] = _LOCAL_EDIT | frozenset({
    ToolEffect.NETWORK_EGRESS, ToolEffect.EXTERNAL_SIDE_EFFECT,
    ToolEffect.UI_SIDE_EFFECT, ToolEffect.WRITE_PRIVATE,
    ToolEffect.ADMIN_CHANGE, ToolEffect.DESTRUCTIVE,
})

#: "automatización acotada" — the same reach as `supervised_agent` (nothing
#: here is allowed to see more of the world than a person could already
#: watch it touch); what changes between the two is `evaluate()`'s
#: `requires_approval` calculus, not this set. "Acotada" (bounded) means
#: admin/destructive effects still always need approval — see
#: `ALWAYS_APPROVAL_EFFECTS` — never that the profile itself grows unchecked.
_BOUNDED_AUTOMATION: FrozenSet[ToolEffect] = _SUPERVISED_AGENT

PROFILES: Dict[str, FrozenSet[ToolEffect]] = {
    "read": _READ,
    "local_edit": _LOCAL_EDIT,
    "supervised_agent": _SUPERVISED_AGENT,
    "bounded_automation": _BOUNDED_AUTOMATION,
}

#: One line of consequence per profile, mirroring
#: `autonomy_budget.PRESET_CONSEQUENCE`'s shape for a settings UI.
PROFILE_CONSEQUENCE: Mapping[str, str] = {
    "read": "Only reads anything — nothing changes on disk or anywhere else.",
    "local_edit": "Changes files and runs code in the workspace; nothing leaves it.",
    "supervised_agent": "Reaches the network and outside services too, with a person watching.",
    "bounded_automation": "Runs the same reach as a supervised agent without asking each time — except admin or destructive effects, which always ask.",
}

DEFAULT_PROFILE = "supervised_agent"

#: Effects that need a per-call approval in EVERY profile that permits them
#: at all — no profile raises this away, because "bounded" is precisely the
#: promise that automation never quietly grants itself an irreversible or
#: administrative effect.
ALWAYS_APPROVAL_EFFECTS: FrozenSet[ToolEffect] = frozenset({
    ToolEffect.ADMIN_CHANGE, ToolEffect.DESTRUCTIVE,
})


def normalize_profile(value: Any) -> str:
    """A valid profile name, or `DEFAULT_PROFILE` for anything else — the
    same "unset/misspelled/pre-existing client" fallback
    `autonomy_budget.normalize_preset` uses, for the same reason: a caller
    that never heard of this module must not crash it."""
    name = str(value or "").strip().lower()
    return name if name in PROFILES else DEFAULT_PROFILE


# ── decisions, always with a reason, always logged ──────────────────────────

@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    requires_approval: bool
    profile: str
    effects: Tuple[str, ...]
    reason: str
    plugin_id: str = ""
    tool_name: str = ""
    decided_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed, "requires_approval": self.requires_approval,
            "profile": self.profile, "effects": list(self.effects),
            "reason": self.reason, "plugin_id": self.plugin_id,
            "tool_name": self.tool_name, "decided_at": self.decided_at,
        }


#: Bounded so a chatty caller cannot grow this without limit — the same
#: shape `src.mcp_manager._call_outcomes` already uses for a per-server
#: outcome trail. Still the fast, in-process read path for `audit_log()`;
#: SEC-08 asked for the trail to survive a restart, so every append below
#: also goes to disk (`_persist_audit_record`) and the deque is reseeded from
#: that file at import time (`_load_persisted_audit`) — a process crash or
#: restart does not start the trail over at empty.
_AUDIT_MAX = 2000
_audit: Deque[PolicyDecision] = deque(maxlen=_AUDIT_MAX)

try:  # pragma: no cover - constants always import in the app
    from src.constants import DATA_DIR as _DEFAULT_AUDIT_DIR
except Exception:  # noqa: BLE001 - standalone use (tests, tooling)
    _DEFAULT_AUDIT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")

#: Module-level so tests can point it at a disposable directory (the pattern
#: `src/mcp_manager.py::DATA_DIR` already uses for its own per-server logs).
AUDIT_DATA_DIR = _DEFAULT_AUDIT_DIR
_AUDIT_LOG_FILENAME = "sec08_policy_audit.jsonl"


def audit_log_path() -> str:
    """`AUDIT_DATA_DIR/security_audit/sec08_policy_audit.jsonl` — one JSON
    record per line, append-only. Reassembled from `AUDIT_DATA_DIR` on every
    call (not cached) so a test's `monkeypatch.setattr(sp, "AUDIT_DATA_DIR",
    ...)` takes effect on the next write/read, same as `mcp_log_dir()` does
    for `src.mcp_manager.DATA_DIR`."""
    return os.path.join(AUDIT_DATA_DIR, "security_audit", _AUDIT_LOG_FILENAME)


def _persist_audit_record(record: Mapping[str, Any]) -> None:
    """Append one decision to disk. Best-effort: a logging failure must never
    turn a policy decision into an exception — the same "bookkeeping is never
    worth breaking the caller" rule `mcp_manager._record_call_outcome` follows."""
    try:
        path = audit_log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(dict(record), separators=(",", ":")) + "\n")
    except Exception as e:  # noqa: BLE001
        logger.debug("security_policy: could not persist audit record: %s", e)


def _decision_from_record(record: Mapping[str, Any]) -> Optional[PolicyDecision]:
    try:
        return PolicyDecision(
            allowed=bool(record["allowed"]),
            requires_approval=bool(record["requires_approval"]),
            profile=str(record["profile"]),
            effects=tuple(record.get("effects") or ()),
            reason=str(record.get("reason") or ""),
            plugin_id=str(record.get("plugin_id") or ""),
            tool_name=str(record.get("tool_name") or ""),
            decided_at=float(record.get("decided_at") or time.time()),
        )
    except Exception as e:  # noqa: BLE001 - a malformed line is skipped, not fatal
        logger.debug("security_policy: could not parse a persisted audit record: %s", e)
        return None


def _load_persisted_audit() -> None:
    """Seed the in-process deque from disk at import time — a restarted
    process should not read as having no audit history when one was already
    recorded. Best-effort, bounded to `_AUDIT_MAX` like everything else the
    deque holds; never raises."""
    try:
        path = audit_log_path()
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()[-_AUDIT_MAX:]
    except Exception as e:  # noqa: BLE001
        logger.debug("security_policy: could not read persisted audit log: %s", e)
        return
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            decision = _decision_from_record(json.loads(line))
        except Exception:
            decision = None
        if decision is not None:
            _audit.append(decision)


def audit_log(*, limit: int = 200) -> List[Dict[str, Any]]:
    """The most recent decisions, newest first."""
    rows = list(_audit)[-max(0, int(limit)):]
    rows.reverse()
    return [d.to_dict() for d in rows]


def clear_audit_log() -> None:
    """Test/ops-only reset — never called from `evaluate()` itself. Clears
    both the in-process deque and the file it is backed by, so a test that
    resets the trail sees an empty one on both the next read AND the next
    process restart, not a deque that is empty while the file it reseeds from
    still holds the old records."""
    _audit.clear()
    try:
        path = audit_log_path()
        if os.path.exists(path):
            os.remove(path)
    except Exception as e:  # noqa: BLE001
        logger.debug("security_policy: could not clear persisted audit log: %s", e)


def _signing_key() -> Optional[bytes]:
    """`eventos firmados cuando el despliegue lo requiera` — signing is
    conditional on the deployment actually configuring a key; a deployment
    that never set one gets unsigned exports (`signature: None`), exactly as
    before this function existed, rather than a startup failure over a key
    nobody asked this module to hold."""
    raw = os.environ.get("FAUSTUS_AUDIT_SIGNING_KEY", "").strip()
    return raw.encode("utf-8") if raw else None


def _sign(record: Mapping[str, Any], key: bytes) -> str:
    # Sorted, separator-stable serialisation so the same record always signs
    # to the same digest regardless of dict insertion order.
    blob = "|".join(f"{k}={record[k]}" for k in sorted(record))
    return hmac.new(key, blob.encode("utf-8"), hashlib.sha256).hexdigest()


def export_audit(*, limit: int = 200, signing_key: Optional[bytes] = None) -> Dict[str, Any]:
    """Audit export for SEC-08: every record from `audit_log`, each with a
    `signature` — HMAC-SHA256 over its own fields — when a signing key is
    available (`signing_key`, else `FAUSTUS_AUDIT_SIGNING_KEY`), or `None`
    when the deployment configured none. `signed` at the top level says
    which happened, so a consumer never has to guess from a null field
    whether signing was skipped or the record was tampered with."""
    key = signing_key if signing_key is not None else _signing_key()
    records = audit_log(limit=limit)
    if key:
        for record in records:
            record["signature"] = _sign(record, key)
    return {"records": records, "signed": bool(key), "count": len(records)}


def evaluate(effects: Sequence[ToolEffect], *, profile: Any = DEFAULT_PROFILE,
             plugin_id: str = "", tool_name: str = "",
             consents: Optional["ConsentStore"] = None) -> PolicyDecision:
    """The one place a profile decision is made. Order matters (mirrors
    `src.completion_engine.scope`'s own "permission before value" rule):
    profile membership is checked BEFORE consent, so an effect a profile
    never permits is refused for that reason and never gets the chance to
    look like a mere missing-consent case.
    """
    profile_name = normalize_profile(profile)
    allowed_effects = PROFILES[profile_name]
    effect_set = frozenset(effects or ())
    effect_names = tuple(sorted(e.value for e in effect_set))
    out_of_profile = effect_set - allowed_effects
    if out_of_profile:
        decision = PolicyDecision(
            allowed=False, requires_approval=False, profile=profile_name,
            effects=effect_names, plugin_id=plugin_id, tool_name=tool_name,
            reason=f"profile {profile_name!r} does not permit "
                   f"{sorted(e.value for e in out_of_profile)}",
        )
        _audit.append(decision)
        _persist_audit_record(decision.to_dict())
        return decision

    store = consents if consents is not None else consent_store
    if plugin_id and tool_name and store is not None:
        check = store.check(plugin_id, tool_name, effect_set)
        if not check["consented"]:
            decision = PolicyDecision(
                allowed=False, requires_approval=True, profile=profile_name,
                effects=effect_names, plugin_id=plugin_id, tool_name=tool_name,
                reason=check["reason"],
            )
            _audit.append(decision)
            _persist_audit_record(decision.to_dict())
            return decision

    needs_approval = bool(effect_set & ALWAYS_APPROVAL_EFFECTS)
    decision = PolicyDecision(
        allowed=True, requires_approval=needs_approval, profile=profile_name,
        effects=effect_names, plugin_id=plugin_id, tool_name=tool_name,
        reason=(
            f"effect(s) always require approval: {sorted(e.value for e in effect_set & ALWAYS_APPROVAL_EFFECTS)}"
            if needs_approval else f"permitted under profile {profile_name!r}"
        ),
    )
    _audit.append(decision)
    _persist_audit_record(decision.to_dict())
    return decision


# ── consent, keyed per (plugin, tool) so an update never blocks a sibling ──

def permission_fingerprint(effects: Sequence[ToolEffect]) -> str:
    """A short, order-independent digest of an effect set, for a quick
    equality check without re-comparing frozensets everywhere."""
    joined = ",".join(sorted(e.value for e in frozenset(effects or ())))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class ConsentRecord:
    plugin_id: str
    tool_name: str
    effects: FrozenSet[ToolEffect]
    fingerprint: str
    granted_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plugin_id": self.plugin_id, "tool_name": self.tool_name,
            "effects": sorted(e.value for e in self.effects),
            "fingerprint": self.fingerprint, "granted_at": self.granted_at,
        }


class ConsentStore:
    """SEC-08 acceptance: consent lives at `(plugin_id, tool_name)`, never at
    `plugin_id` alone. `check()` for one tool reads and, on a mismatch,
    would only ever be asked to re-grant that SAME key — a sibling tool's
    record under the same plugin is a different dict key, never touched by
    another tool's grant, check or revoke. That per-key isolation is the
    entire mechanism; there is no separate "invalidate the plugin" step to
    get right or forget.
    """

    def __init__(self) -> None:
        self._records: Dict[Tuple[str, str], ConsentRecord] = {}

    def grant(self, plugin_id: str, tool_name: str, effects: Sequence[ToolEffect]) -> ConsentRecord:
        effect_set = frozenset(effects or ())
        record = ConsentRecord(
            plugin_id=plugin_id, tool_name=tool_name, effects=effect_set,
            fingerprint=permission_fingerprint(effect_set),
        )
        self._records[(plugin_id, tool_name)] = record
        return record

    def get(self, plugin_id: str, tool_name: str) -> Optional[ConsentRecord]:
        return self._records.get((plugin_id, tool_name))

    def check(self, plugin_id: str, tool_name: str, declared_effects: Sequence[ToolEffect]) -> Dict[str, Any]:
        """Consented iff `declared_effects` is a SUBSET of what was last
        granted for this exact `(plugin_id, tool_name)`. A plugin update that
        only REMOVES an effect from a tool stays consented (nothing new to
        agree to); one that ADDS even a single effect is not — the added
        effect(s) are named in `new_effects` so a caller can show exactly
        what changed rather than an opaque "permissions changed"."""
        declared = frozenset(declared_effects or ())
        record = self._records.get((plugin_id, tool_name))
        if record is None:
            return {"consented": False, "reason": f"no consent on file for {tool_name!r}",
                    "new_effects": sorted(e.value for e in declared)}
        new_effects = declared - record.effects
        if new_effects:
            return {
                "consented": False,
                "reason": f"{tool_name!r} now declares {sorted(e.value for e in new_effects)}, "
                          f"beyond what was consented to — prior consent is invalidated",
                "new_effects": sorted(e.value for e in new_effects),
            }
        return {"consented": True, "reason": "declared effects are covered by prior consent",
                "new_effects": []}

    def revoke(self, plugin_id: str, tool_name: Optional[str] = None) -> int:
        """Remove consent for one tool, or (``tool_name=None``) every tool
        recorded under `plugin_id`. Returns how many records were removed."""
        if tool_name is not None:
            return 1 if self._records.pop((plugin_id, tool_name), None) is not None else 0
        keys = [k for k in self._records if k[0] == plugin_id]
        for k in keys:
            del self._records[k]
        return len(keys)

    def tools_for_plugin(self, plugin_id: str) -> List[str]:
        return sorted(tool for (pid, tool) in self._records if pid == plugin_id)


#: Process-wide default store — callers that don't need an isolated one (most
#: of them: this mirrors `src.mcp_manager._sampling_quota`'s "one table per
#: process" choice) use this; tests construct their own `ConsentStore()`.
consent_store = ConsentStore()


# ── bridging TOOL-04's declared-permission vocabulary onto effects ─────────

#: `src.extension_manifest.PERMISSION_KEYS` ("network", "files", "secrets")
#: is the vocabulary an MCP server's admin actually declares — coarser than
#: `ToolEffect`, and rightly so (nobody installing a third-party MCP server
#: states a `ToolEffect` list). This maps it onto the SAME effect vocabulary
#: `evaluate()`/`ConsentStore` already speak, so an MCP server's declared
#: permissions can be run through this module's one real decision function
#: instead of growing a second, parallel one (rule 4).
_DECLARED_PERMISSION_EFFECTS: Mapping[str, FrozenSet[ToolEffect]] = {
    "network": frozenset({ToolEffect.NETWORK_EGRESS}),
    "files": frozenset({ToolEffect.READ_WORKSPACE, ToolEffect.WRITE_WORKSPACE}),
    # A server that receives credential-bearing env vars (FAUSTUS's
    # `inherit_env`) can READ private material through them; it is not
    # thereby granted a WRITE_PRIVATE effect, which is reserved for a tool
    # that explicitly writes somewhere private (e.g. a password manager).
    "secrets": frozenset({ToolEffect.READ_PRIVATE}),
}


def effects_for_declared_permissions(permissions: Optional[Mapping[str, Any]]) -> FrozenSet[ToolEffect]:
    """The `ToolEffect` set implied by a `{"network", "files", "secrets"}`
    declared-permission dict — never raises; an unknown key or falsy value
    contributes nothing."""
    permissions = permissions or {}
    effects: set = set()
    for key, mapped in _DECLARED_PERMISSION_EFFECTS.items():
        if permissions.get(key):
            effects |= mapped
    return frozenset(effects)


# Seed the in-process audit trail from disk once, at import time, so a
# restarted process does not read as having zero history (SEC-08: persist
# the audit log to disk instead of an in-memory-only deque).
_load_persisted_audit()
