"""The contract a model step was made under (H06).

A model step announces a set of tool schemas, to one owner, in one
environment, under one policy. The calls it returns are executed later, after
the model has finished streaming and possibly after another tool in the same
round has run. A `StepSnapshot` freezes what was announced so that each call
can be judged against the step it came from, and only against that step:

* a late call uses the contract that was announced, not whatever the catalogue
  reads at execution time (an MCP server that changes its schema mid-run does
  not change the contract of a call already made under the old one);
* a security revocation is always read live. The snapshot records what was
  announced and allowed at capture; it is never evidence that a call is still
  allowed. A tool disabled, blocked by policy or removed after the capture is
  refused under every spelling of its name, whatever the snapshot says;
* a snapshot made for another session or owner is ignored, never applied.

The functions here are pure. `execute_tool_block` passes the live values
(`disabled_tools`, `tool_policy`, the live definition of an MCP tool) and gets
back a `CallAuthorization`; nothing here grants a permission.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional, Tuple

from src.tool_schema_receipts import definition_hashes

MODES = ("off", "shadow", "enforce")
UNANNOUNCED_MODES = ("allow", "shadow", "refuse")

logger = logging.getLogger(__name__)


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _owner_ref(owner: Any) -> str:
    """A stable reference to the owner that carries no identity."""
    text = str(owner or "").strip().lower()
    return _digest(["owner", text])[:16] if text else ""


@dataclass(frozen=True)
class AnnouncedTool:
    """One tool as the step announced it. Scalars only: the definition is a
    JSON string so nothing mutable is shared with the request."""

    name: str
    canonical_id: str
    origin: str
    exposure: str
    exact_sha256: str
    semantic_sha256: str
    definition_json: str

    def definition(self) -> dict:
        return json.loads(self.definition_json)


@dataclass(frozen=True)
class StepSnapshot:
    snapshot_id: str
    session_id: str
    run_id: str
    round_num: int
    candidate_index: int
    owner_ref: str
    is_admin: bool
    environment: Tuple[Tuple[str, str], ...]
    #: names that were disabled or blocked by policy when the step was made.
    policy_denied: Tuple[str, ...]
    tools: Tuple[AnnouncedTool, ...]
    catalog_version: str
    authority_fingerprint: str
    text_only: bool = False

    # -- reading ------------------------------------------------------------
    def announced(self, name: Any) -> Optional[AnnouncedTool]:
        """The announced tool a name stands for: the wire name, its canonical
        id or an alias. A name announced twice is ambiguous and reads as None."""
        if not isinstance(name, str):
            return None
        spellings = _spellings(name)
        found = [tool for tool in self.tools if tool.name in spellings]
        return found[0] if len(found) == 1 else None

    def contract_for(self, name: Any) -> Optional[dict]:
        tool = self.announced(name)
        return tool.definition() if tool else None

    def receipt(self) -> dict:
        """What is safe to put on a result or event: identities, no schema."""
        return {"snapshot_id": self.snapshot_id, "run_id": self.run_id, "round": self.round_num,
                "candidate_index": self.candidate_index, "catalog_version": self.catalog_version,
                "authority_fingerprint": self.authority_fingerprint, "announced": len(self.tools),
                "text_only": self.text_only}


def _spellings(name: str) -> frozenset:
    try:
        from src.tool_authority import AUTHORITY
        return AUTHORITY.spellings(name)
    except Exception:  # noqa: BLE001 - an unavailable registry reads as the name alone
        return frozenset({name})


def capture_step(candidate_tools: Any, *, session_id: Any, run_id: Any, round_num: int, candidate_index: int,
                 owner: Any = None, is_admin: bool = False,
                 disabled_tools: Optional[Iterable[str]] = None, tool_policy: Any = None,
                 environment: Optional[Mapping[str, Any]] = None, text_only: bool = False) -> StepSnapshot:
    """Freeze the step. Never raises on a malformed entry: it is skipped, and
    a call to it then reads as not announced."""
    from src.tool_authority import AUTHORITY

    tools = []
    seen = set()
    for entry in candidate_tools or ():
        definition = entry.get("function") if isinstance(entry, dict) else None
        if not isinstance(definition, dict) or not isinstance(definition.get("name"), str):
            continue
        name = definition["name"]
        if name in seen:
            continue
        seen.add(name)
        exact, semantic = definition_hashes(definition)
        registered = AUTHORITY.get(name)
        tools.append(AnnouncedTool(
            name=name, canonical_id=registered.canonical_id if registered else "",
            origin=registered.origin if registered else ("mcp" if name.startswith("mcp__") else "unregistered"),
            exposure=registered.exposure.value if registered else "direct",
            exact_sha256=exact, semantic_sha256=semantic,
            definition_json=json.dumps(definition, sort_keys=True, separators=(",", ":"), ensure_ascii=False)))
    disabled = {str(n) for n in (disabled_tools or ())}
    denied = sorted(t.name for t in tools
                    if t.name in disabled or (tool_policy is not None and _policy_blocks(tool_policy, t.name)))
    env = tuple(sorted((str(k), str(v)) for k, v in (environment or {}).items()))
    return StepSnapshot(
        snapshot_id=uuid.uuid4().hex, session_id=str(session_id or ""), run_id=str(run_id or ""),
        round_num=int(round_num), candidate_index=int(candidate_index), owner_ref=_owner_ref(owner),
        is_admin=bool(is_admin), environment=env, policy_denied=tuple(denied), tools=tuple(tools),
        catalog_version=_digest([[t.name, t.exact_sha256] for t in tools]),
        authority_fingerprint=AUTHORITY.fingerprint(), text_only=bool(text_only))


def _is_code_only(name: Any) -> bool:
    try:
        from src.tool_authority import AUTHORITY, Exposure
        return AUTHORITY.exposure(str(name)) is Exposure.CODE_ONLY
    except Exception:  # noqa: BLE001 - an unreadable registry never blocks a call
        return False


def _policy_blocks(tool_policy: Any, name: str) -> bool:
    try:
        return bool(tool_policy.blocks(name))
    except Exception:  # noqa: BLE001 - an unreadable policy denies
        return True


@dataclass(frozen=True)
class CallAuthorization:
    """The verdict for one call. `allowed` False means the call must not run."""

    allowed: bool
    status: str  # ok | revoked | code_only | contract_changed | not_announced | no_snapshot | snapshot_ignored | off
    reason: str = ""
    contract_sha256: str = ""
    live_contract_sha256: str = ""

    def observation(self, snapshot: Optional[StepSnapshot] = None) -> dict:
        out = {"stage": "step_snapshot", "status": self.status, "allowed": self.allowed}
        if self.reason:
            out["reason"] = self.reason
        if self.contract_sha256:
            out["contract_sha256"] = self.contract_sha256
        if self.live_contract_sha256:
            out["live_contract_sha256"] = self.live_contract_sha256
        if snapshot is not None:
            out["snapshot"] = snapshot.receipt()
        return out


def _revoked(name: str, disabled_tools: Optional[Iterable[str]], tool_policy: Any) -> Optional[str]:
    """Read the CURRENT disable list and policy under every spelling."""
    spellings = set(_spellings(name))
    try:
        from src.tool_security import email_tool_policy_names
        spellings |= set(email_tool_policy_names(name))
    except Exception:  # noqa: BLE001
        pass
    disabled = {str(n) for n in (disabled_tools or ())}
    if spellings & disabled:
        return "the tool is disabled for this run"
    if tool_policy is not None and any(_policy_blocks(tool_policy, s) for s in spellings):
        return "the tool is blocked by the current tool policy"
    return None


def authorize_call(snapshot: Optional[StepSnapshot], name: Any, *, session_id: Any = None, owner: Any = None,
                   disabled_tools: Optional[Iterable[str]] = None, tool_policy: Any = None,
                   live_definition: Optional[Callable[[str], Optional[Mapping[str, Any]]]] = None,
                   mode: str = "enforce", unannounced: str = "shadow") -> CallAuthorization:
    """Judge one call against its step and the live state.

    Order (the first rule that applies decides):
      1. mode off, or no snapshot: nothing to judge.
      2. a snapshot made for another session or owner is ignored.
      3. LIVE revocation: a disabled or policy-blocked tool is refused. This
         precedes every rule below, so nothing captured can override it.
      4. a CODE_ONLY tool is refused: a step's call is a model call, and such
         a tool is only reachable from a Code Mode program (no snapshot).
         a tool the step did not announce follows `unannounced`.
      5. `live_definition(name)` is supplied for tools whose contract can
         change while a run is in flight (MCP): a tool that vanished is
         revoked, one whose structure differs from the announced contract is
         `contract_changed`; the call keeps the ANNOUNCED contract.
    `mode` "shadow" reports what enforce would refuse without refusing it.
    """
    if mode not in MODES:
        mode = "enforce"
    if mode == "off":
        return CallAuthorization(True, "off")
    if snapshot is None:
        return CallAuthorization(True, "no_snapshot")
    if (session_id and snapshot.session_id and str(session_id) != snapshot.session_id) or (
            owner and snapshot.owner_ref and _owner_ref(owner) != snapshot.owner_ref):
        return CallAuthorization(True, "snapshot_ignored", "the snapshot belongs to another session or owner")
    enforce = mode == "enforce"

    why = _revoked(str(name), disabled_tools, tool_policy)
    if why:
        # Revocation is not softened by shadow mode: the run's own disable
        # list and policy already refuse this call; this only names the cause.
        return CallAuthorization(False, "revoked", why)

    if _is_code_only(name):
        # A step's call is a model call. A CODE_ONLY tool (H17) is reachable
        # from inside a Code Mode program, whose bridge carries no snapshot.
        return CallAuthorization(not enforce, "code_only",
                                 "the tool can only be called from inside a Code Mode program")

    announced = snapshot.announced(name)
    if announced is None:
        if unannounced == "refuse" and enforce:
            return CallAuthorization(False, "not_announced", "the step did not announce this tool")
        return CallAuthorization(True, "not_announced", "the step did not announce this tool")

    if live_definition is not None:
        live = live_definition(announced.name)
        if live is None:
            return CallAuthorization(False, "revoked", "the tool is no longer offered by its server",
                                     announced.semantic_sha256)
        _, live_semantic = definition_hashes(dict(live))
        if live_semantic != announced.semantic_sha256:
            logger.warning("step snapshot: contract of %s changed: %s", announced.name,
                           "; ".join(contract_diff(announced.definition(), dict(live))) or "no visible difference")
            return CallAuthorization(
                not enforce, "contract_changed",
                "the tool's schema changed after this step announced it; the announced contract applies",
                announced.semantic_sha256, live_semantic)
    return CallAuthorization(True, "ok", "", announced.semantic_sha256)


def contract_diff(announced: Any, live: Any, path: str = "", limit: int = 8) -> list:
    """Where two definitions differ structurally (descriptions apart), as short
    `path: announced -> live` lines, at most `limit`. For the log only."""
    out: list = []

    def walk(a: Any, b: Any, where: str) -> None:
        if len(out) >= limit:
            return
        if isinstance(a, dict) and isinstance(b, dict):
            for key in sorted(set(a) | set(b)):
                if key == "description":
                    continue
                if key not in a or key not in b:
                    out.append(f"{where}/{key}: {'missing' if key not in a else type(a[key]).__name__} -> "
                               f"{'missing' if key not in b else type(b[key]).__name__}")
                else:
                    walk(a[key], b[key], f"{where}/{key}")
        elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
            for i, (x, y) in enumerate(zip(a, b)):
                walk(x, y, f"{where}[{i}]")
        elif a != b or type(a) is not type(b):
            out.append(f"{where}: {json.dumps(a, default=str)[:60]} -> {json.dumps(b, default=str)[:60]}")

    walk(announced, live, path)
    return out


def live_mcp_definition(name: str, manager: Any) -> Optional[dict]:
    """The definition a connected MCP manager reports for `name` right now, in
    the same shape the step announced (the manager's native schema listing), or None."""
    getter = getattr(manager, "get_all_tools", None)
    if getter is None:
        return None
    try:
        rows = getter()
    except Exception:  # noqa: BLE001 - an unreadable manager reads as "gone"
        return None
    for row in rows or ():
        if isinstance(row, dict) and row.get("qualified_name") == name and not row.get("is_disabled"):
            return {"name": name, "description": str(row.get("description") or ""),
                    "parameters": row.get("input_schema") or {"type": "object", "properties": {}}}
    return None


def mcp_descriptors(tools: Iterable[Mapping[str, Any]], *, readonly_of: Optional[Callable[[Mapping], bool]] = None) -> dict:
    """Descriptors of every discoverable MCP tool, by wire name.

    A tool the descriptor rules refuse is skipped, never fatal: a server with
    a malformed tool must not hide its other tools.
    """
    from src.tool_authority import ToolAuthorityError, descriptor_from_mcp

    out = {}
    for tool in tools or ():
        try:
            descriptor = descriptor_from_mcp(tool, readonly=readonly_of(tool) if readonly_of else None)
        except ToolAuthorityError:
            continue
        out[descriptor.name] = descriptor
    return out


def authorize_for_execution(snapshot: Optional[StepSnapshot], block: Any, *, session_id: Any = None, owner: Any = None,
                            disabled_tools: Optional[Iterable[str]] = None, tool_policy: Any = None,
                            mode: Optional[str] = None, unannounced: Optional[str] = None,
                            mcp_manager: Any = None) -> CallAuthorization:
    """`authorize_call` with the settings and the live MCP manager filled in.

    This is the single entry `execute_tool_block` uses. A failure to read a
    setting or the manager never widens a denial: the defaults apply.
    """
    if snapshot is None:
        return CallAuthorization(True, "no_snapshot")
    if mode is None or unannounced is None:
        try:
            from src.settings import get_setting
            mode = mode or str(get_setting("agent_step_snapshot_mode", "enforce"))
            unannounced = unannounced or str(get_setting("agent_step_snapshot_unannounced", "shadow"))
        except Exception:  # noqa: BLE001
            mode, unannounced = mode or "enforce", unannounced or "shadow"
    if unannounced not in UNANNOUNCED_MODES:
        unannounced = "shadow"
    name = str(getattr(block, "tool_type", "") or "")
    live = None
    if name.startswith("mcp__"):
        manager = mcp_manager
        if manager is None:
            try:
                from src.tool_utils import get_mcp_manager
                manager = get_mcp_manager()
            except Exception:  # noqa: BLE001
                manager = None
        if manager is not None:
            live = lambda tool_name: live_mcp_definition(tool_name, manager)  # noqa: E731
    return authorize_call(snapshot, name, session_id=session_id, owner=owner, disabled_tools=disabled_tools,
                          tool_policy=tool_policy, live_definition=live, mode=mode, unannounced=unannounced)


def step_snapshot_for_answer(states: Mapping[int, Mapping[str, Any]], candidate_index: Any, *, round_num: int
                             ) -> Optional[StepSnapshot]:
    """The snapshot of the candidate that answered, for this round only.

    Never substitutes candidate zero or an earlier round: a call that cannot
    be tied to its own step is judged without a snapshot (status
    `no_snapshot`), not against a neighbour's.
    """
    if not isinstance(candidate_index, int) or isinstance(candidate_index, bool):
        return None
    snapshot = (states.get(candidate_index) or {}).get("step_snapshot")
    if not isinstance(snapshot, StepSnapshot):
        return None
    if snapshot.candidate_index != candidate_index or snapshot.round_num != round_num:
        return None
    return snapshot
