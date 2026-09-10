"""Lifecycle and deterministic compiler for Modo Enséñame."""
from __future__ import annotations

import copy
import json
import logging
import re
import uuid
from typing import Any, Dict, List, Mapping, Optional

from src import skill_governance
from src.contracts.base import fingerprint, now_iso
from src.teach_mode import contracts
from src.teach_mode import persistence

logger = logging.getLogger(__name__)

_SECRET = re.compile(r"(?:password|passwd|secret|token|api[_-]?key|authorization|cookie|credential)", re.I)
_INLINE_SECRET = re.compile(
    r"(?i)(?:(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}|"
    r"(?:sk|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{8,}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"
)
_QUERY_SECRET = re.compile(r"(?i)([?&](?:token|key|api_key|access_token|signature)=)[^&#\s]+")
_service: Optional["TeachService"] = None


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _scrub(value: Any, depth: int = 0) -> Any:
    if depth > 12:
        return "<redacted>"
    if isinstance(value, Mapping):
        return {str(k): ("<redacted>" if _SECRET.search(str(k)) else _scrub(v, depth + 1))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v, depth + 1) for v in value]
    if isinstance(value, str):
        # Common bearer/private-key forms; do not attempt to preserve them.
        if "BEGIN PRIVATE KEY" in value or "BEGIN OPENSSH PRIVATE KEY" in value:
            return "<redacted>"
        cleaned = _INLINE_SECRET.sub("<redacted>", value)
        cleaned = _QUERY_SECRET.sub(r"\1<redacted>", cleaned)
        return cleaned[:20000]
    return value


class TeachService:
    def __init__(self, store: Any = None) -> None:
        self._store = store

    def _db(self):
        return self._store or persistence.store()

    def _emit(self, owner: str, name: str, kind: str, ident: str, payload: Mapping[str, Any]) -> None:
        self._db().emit(owner=owner, name=name, entity_kind=kind, entity_id=ident, payload=payload)

    def start(self, *, owner: str, request: Any, project_id: str = "",
              session_id: str = "") -> Dict[str, Any]:
        data = contracts.demonstration_request(request)
        data["project_id"] = project_id or data["project_id"]
        data["session_id"] = session_id or data["session_id"]
        # POST/retry and an overeager model must not create two recorders for
        # the same chat.  There can be at most one live capture in a scope.
        current = self.active(owner=owner, session_id=data["session_id"],
                              project_id=data["project_id"])
        if current is not None:
            return current
        demo = {**data, "id": _id("demo"), "status": "recording",
                "observation_count": 0, "procedure_id": "", "started_at": now_iso(),
                "stopped_at": "", "known_limitations": []}
        saved = self._db().put("demonstration", demo, owner=owner,
                              project_id=data["project_id"], session_id=data["session_id"],
                              status="recording")
        self._emit(owner, "teach_recording_started", "demonstration", saved["id"],
                   {"project_id": data["project_id"], "session_id": data["session_id"],
                    "title": data["title"]})
        return saved

    def get(self, *, owner: str, demonstration_id: str) -> Optional[Dict[str, Any]]:
        demo = self._db().get("demonstration", demonstration_id, owner=owner)
        if demo is not None:
            demo["observations"] = self.observations(owner=owner, demonstration_id=demonstration_id)
        return demo

    def list(self, *, owner: str, project_id: str = "", session_id: str = "",
             status: str = "", limit: int = 100) -> List[Dict[str, Any]]:
        return self._db().list("demonstration", owner=owner, project_id=project_id,
                               session_id=session_id, status=status, limit=limit)

    def active(self, *, owner: str, session_id: str = "", project_id: str = "") -> Optional[Dict[str, Any]]:
        for status in ("recording", "paused"):
            rows = self._db().list("demonstration", owner=owner, session_id=session_id,
                                   project_id=project_id, status=status, limit=1)
            if rows:
                return rows[0]
        return None

    def pause(self, *, owner: str, demonstration_id: str) -> Dict[str, Any]:
        return self._recording_transition(owner=owner, demonstration_id=demonstration_id,
                                          source="recording", target="paused")

    def resume(self, *, owner: str, demonstration_id: str) -> Dict[str, Any]:
        return self._recording_transition(owner=owner, demonstration_id=demonstration_id,
                                          source="paused", target="recording")

    def _recording_transition(self, *, owner: str, demonstration_id: str,
                              source: str, target: str) -> Dict[str, Any]:
        demo = self._db().get("demonstration", demonstration_id, owner=owner)
        if demo is None:
            raise contracts.TeachError("demonstration_id", "not found")
        # Idempotent commands are important for voice and retried requests.
        if demo.get("status") == target:
            return demo
        if demo.get("status") != source:
            raise contracts.TeachError("demonstration.status", f"cannot move to {target}",
                                       got=demo.get("status"))
        demo["status"] = target
        saved = self._db().put("demonstration", demo, owner=owner,
                              project_id=str(demo.get("project_id") or ""),
                              session_id=str(demo.get("session_id") or ""), status=target,
                              expected_revision=int(demo["revision"]))
        self._emit(owner, f"teach_recording_{target}", "demonstration", demonstration_id, {})
        return saved

    def observe(self, *, owner: str, demonstration_id: str, observation: Any) -> Dict[str, Any]:
        demo = self._db().get("demonstration", demonstration_id, owner=owner)
        if demo is None:
            raise contracts.TeachError("demonstration_id", "not found")
        if demo.get("status") != "recording":
            raise contracts.TeachError("demonstration.status", "only a recording demonstration accepts observations",
                                       got=demo.get("status"))
        data = _scrub(contracts.observation_request(observation))
        sequence = int(demo.get("observation_count") or 0) + 1
        row = {**data, "id": _id("obs"), "demonstration_id": demonstration_id,
               "sequence": sequence, "observed_at": now_iso()}
        saved = self._db().put("observation", row, owner=owner,
                               project_id=str(demo.get("project_id") or ""),
                               session_id=str(demo.get("session_id") or ""), status="captured")
        demo["observation_count"] = sequence
        self._db().put("demonstration", demo, owner=owner,
                       project_id=str(demo.get("project_id") or ""),
                       session_id=str(demo.get("session_id") or ""), status="recording",
                       expected_revision=int(demo["revision"]))
        self._emit(owner, "teach_observation_captured", "demonstration", demonstration_id,
                   {"observation_id": saved["id"], "sequence": sequence,
                    "kind": saved["kind"], "tool": saved["tool"], "success": saved["success"]})
        return saved

    def observations(self, *, owner: str, demonstration_id: str) -> List[Dict[str, Any]]:
        rows = self._db().list("observation", owner=owner, limit=1000)
        return sorted((row for row in rows if row.get("demonstration_id") == demonstration_id),
                      key=lambda row: int(row.get("sequence") or 0))

    def stop(self, *, owner: str, demonstration_id: str, cancelled: bool = False) -> Dict[str, Any]:
        demo = self._db().get("demonstration", demonstration_id, owner=owner)
        if demo is None:
            raise contracts.TeachError("demonstration_id", "not found")
        requested = "cancelled" if cancelled else "recorded"
        if demo.get("status") == requested:
            return demo
        if demo.get("status") not in ("recording", "paused", "interrupted"):
            raise contracts.TeachError("demonstration.status", "is not recording", got=demo.get("status"))
        status = requested
        demo.update({"status": status, "stopped_at": now_iso()})
        saved = self._db().put("demonstration", demo, owner=owner,
                              project_id=str(demo.get("project_id") or ""),
                              session_id=str(demo.get("session_id") or ""), status=status,
                              expected_revision=int(demo["revision"]))
        self._emit(owner, "teach_recording_cancelled" if cancelled else "teach_recording_stopped",
                   "demonstration", demonstration_id,
                   {"observation_count": saved.get("observation_count", 0)})
        return saved

    def trace(self, *, owner: str, demonstration_id: str) -> Dict[str, Any]:
        demo = self.get(owner=owner, demonstration_id=demonstration_id)
        if demo is None:
            raise contracts.TeachError("demonstration_id", "not found")
        observations = demo.pop("observations", [])
        return {
            "demonstration": demo,
            "summary": {
                "total": len(observations),
                "successful": sum(1 for row in observations if row.get("success")),
                "failed": sum(1 for row in observations if not row.get("success")),
                "tools": list(dict.fromkeys(str(row.get("tool") or "") for row in observations
                                              if row.get("tool"))),
                "discardable": sum(1 for row in observations
                                     if row.get("classification") in ("incidental", "exploratory")),
            },
            "observations": observations,
        }

    def recover_interrupted_recordings(self) -> int:
        """Conservatively close captures left live by a prior process."""
        count = 0
        # This maintenance scan is owner-agnostic internally, so the generic
        # store intentionally exposes no route for it.  SQLite is queried only
        # for identities, then every write remains owner-scoped.
        store = self._db()
        try:
            rows = store.list_all("demonstration", status="recording", limit=1000)
        except AttributeError:
            return 0
        for demo in rows:
            owner = str(demo.get("owner") or "")
            if not owner:
                continue
            demo.update({"status": "interrupted", "stopped_at": now_iso()})
            try:
                store.put("demonstration", demo, owner=owner,
                          project_id=str(demo.get("project_id") or ""),
                          session_id=str(demo.get("session_id") or ""), status="interrupted",
                          expected_revision=int(demo["revision"]))
                self._emit(owner, "teach_recording_interrupted", "demonstration", demo["id"], {})
                count += 1
            except Exception:
                logger.exception("teach: could not recover recording %s", demo.get("id"))
        return count

    def compile(self, *, owner: str, demonstration_id: str) -> Dict[str, Any]:
        demo = self._db().get("demonstration", demonstration_id, owner=owner)
        if demo is None:
            raise contracts.TeachError("demonstration_id", "not found")
        if demo.get("status") not in ("recorded", "candidate", "needs_clarification"):
            raise contracts.TeachError("demonstration.status", "stop recording before compiling",
                                       got=demo.get("status"))
        observations = self.observations(owner=owner, demonstration_id=demonstration_id)
        usable = [row for row in observations if row.get("success") and row.get("tool")
                  and row.get("classification") not in ("incidental", "exploratory")]
        if not usable:
            demo["status"] = "needs_clarification"
            self._db().put("demonstration", demo, owner=owner,
                           project_id=str(demo.get("project_id") or ""),
                           session_id=str(demo.get("session_id") or ""), status="needs_clarification",
                           expected_revision=int(demo["revision"]))
            raise contracts.TeachError("demonstration.observations",
                                       "no successful semantic tool actions were captured")
        steps = []
        effects, permissions = [], []
        for index, row in enumerate(usable, 1):
            classification = str(row.get("classification") or "unknown")
            if classification == "unknown":
                classification = "required_for_core"
            tool = str(row.get("tool") or "")
            effects.append(f"tool:{tool}")
            steps.append({"id": f"step_{index}", "tool": tool,
                          "arguments_template": copy.deepcopy(row.get("arguments", {})),
                          "classification": classification,
                          "required": classification in ("required_for_core", "professional_safeguard"),
                          "success_signal": "tool_result.success == true",
                          "source_observation_id": row["id"]})
        canonical = {"intent": demo.get("intent"), "steps": steps,
                     "environment_fingerprint": demo.get("environment_fingerprint", "")}
        revision_hash = fingerprint((("kind", "learned_procedure"), ("canonical", canonical)))
        procedure = {
            "id": _id("procedure"), "title": demo.get("title"),
            "intent": demo.get("intent"), "status": "candidate",
            "project_id": demo.get("project_id", ""), "demonstration_refs": [demonstration_id],
            "steps": steps, "inputs": [], "preconditions": [],
            "effects": sorted(set(effects)), "permissions": permissions,
            "postconditions": ["all required steps report success"],
            "proof_refs": [], "known_limitations": [], "revision_hash": revision_hash,
            "capability_contract": {"version": 1, "preconditions": [], "inputs": [],
                                    "effects": sorted(set(effects)), "permissions": permissions,
                                    "checks": ["all required steps report success"]},
        }
        saved = self._db().put("procedure", procedure, owner=owner,
                              project_id=str(demo.get("project_id") or ""), status="candidate")
        demo.update({"status": "candidate", "procedure_id": saved["id"]})
        self._db().put("demonstration", demo, owner=owner,
                       project_id=str(demo.get("project_id") or ""),
                       session_id=str(demo.get("session_id") or ""), status="candidate",
                       expected_revision=int(demo["revision"]))
        self._emit(owner, "teach_procedure_compiled", "procedure", saved["id"],
                   {"demonstration_id": demonstration_id, "revision_hash": revision_hash,
                    "step_count": len(steps)})
        return saved

    def procedure(self, *, owner: str, procedure_id: str) -> Optional[Dict[str, Any]]:
        return self._db().get("procedure", procedure_id, owner=owner)

    def procedures(self, *, owner: str, project_id: str = "", status: str = "",
                   limit: int = 100) -> List[Dict[str, Any]]:
        return self._db().list("procedure", owner=owner, project_id=project_id,
                               status=status, limit=limit)

    def transition(self, *, owner: str, procedure_id: str, action: str,
                   evidence: Any = None) -> Dict[str, Any]:
        procedure = self._db().get("procedure", procedure_id, owner=owner)
        if procedure is None:
            raise contracts.TeachError("procedure_id", "not found")
        allowed = {
            "simulate": ({"candidate", "needs_correction"}, "ready_for_replay"),
            "validate": ({"ready_for_replay", "needs_correction", "validated"}, "validated"),
            "approve": ({"validated"}, "approved"),
            "install": ({"approved"}, "installed"),
            "establish": ({"installed"}, "established"),
            "deprecate": ({"installed", "established"}, "deprecated"),
            "revoke": ({"approved", "installed", "established", "quarantined"}, "revoked"),
            "quarantine": ({"installed", "established"}, "quarantined"),
        }
        if action not in allowed:
            raise contracts.TeachError("action", f"unknown transition {action!r}")
        sources, target = allowed[action]
        if procedure.get("status") not in sources:
            raise contracts.TeachError("procedure.status", f"cannot {action} from this status",
                                       got=procedure.get("status"))
        proof = contracts.replay_evidence(evidence or {}) if action == "validate" else None
        if proof is not None and not proof["passed"]:
            target = "needs_correction"
        if proof is not None:
            procedure["proof_refs"] = list(dict.fromkeys(
                list(procedure.get("proof_refs") or []) + proof["proof_refs"]))
            procedure["known_limitations"] = proof["limitations"]
            procedure["last_evidence"] = proof
        installed_skill = None
        if target == "installed":
            # TOOL-06 (Lote 50 wiring): the promotion this transition performs
            # — a demonstrated procedure becoming a published SKILL.md other
            # projects can pick up — is exactly what skill_governance's review
            # gate exists for. `reviewed=True` is not a rubber stamp: the FSM
            # above already refused this call unless `procedure.status ==
            # "approved"`, i.e. an operator already ran the "approve"
            # transition, so the review this asserts already happened.
            promotion = skill_governance.validate_promotion(
                {"source": "teach_mode"}, target_status="published", reviewed=True)
            if not promotion["ok"]:
                raise contracts.TeachError("procedure", promotion["reason"])
            # Do the real registry write first.  The procedure is not allowed
            # to claim installation when no published SKILL.md exists.
            installed_skill = self._install_skill(owner, procedure)
            procedure["installed_skill_ref"] = f"skill://{installed_skill['name']}"
        procedure["status"] = target
        try:
            saved = self._db().put("procedure", procedure, owner=owner,
                                  project_id=str(procedure.get("project_id") or ""), status=target,
                                  expected_revision=int(procedure["revision"]))
        except Exception:
            if installed_skill and not installed_skill.get("_deduped"):
                try:
                    from services.memory.skills import SkillsManager
                    from src.constants import DATA_DIR
                    SkillsManager(DATA_DIR).delete_skill(installed_skill["name"], owner=owner)
                except Exception:
                    logger.exception("teach: could not roll back skill installation")
            raise
        self._emit(owner, f"teach_procedure_{target}", "procedure", procedure_id,
                   {"action": action, "proof_refs": saved.get("proof_refs", [])})
        if target == "installed":
            self._register_with_immune(owner, saved)
        elif target in ("deprecated", "revoked", "quarantined"):
            self._hide_installed_skill(owner, saved)
        return saved

    @staticmethod
    def _install_skill(owner: str, procedure: Mapping[str, Any]) -> Dict[str, Any]:
        from services.memory.skills import SkillsManager
        from src.constants import DATA_DIR

        rendered_steps = []
        required_tools = []
        for step in procedure.get("steps", []) or []:
            tool = str(step.get("tool") or "tool")
            required_tools.append(tool)
            args = json.dumps(step.get("arguments_template", {}), ensure_ascii=False,
                              sort_keys=True, separators=(",", ":"))
            rendered_steps.append(f"Use `{tool}` with validated inputs shaped like `{args}`; "
                                  "stop if the tool does not report success.")
        manager = SkillsManager(DATA_DIR)
        return manager.add_skill(
            name=str(procedure.get("title") or "taught-procedure"),
            description=str(procedure.get("intent") or procedure.get("title") or "")[:500],
            category="taught",
            tags=["taught", "verified"],
            requires_toolsets=list(dict.fromkeys(required_tools)),
            when_to_use=str(procedure.get("intent") or ""),
            procedure=rendered_steps,
            pitfalls=list(procedure.get("known_limitations") or []),
            verification=list(procedure.get("postconditions") or []),
            status="published",
            version="1.0.0",
            confidence=0.9,
            source="taught",
            owner=owner,
            dedupe=False,
            solution=f"Generated from {', '.join(procedure.get('demonstration_refs') or [])}; "
                     f"procedure revision {procedure.get('revision_hash') or ''}.",
        )

    @staticmethod
    def _hide_installed_skill(owner: str, procedure: Mapping[str, Any]) -> None:
        ref = str(procedure.get("installed_skill_ref") or "")
        if not ref.startswith("skill://"):
            return
        try:
            from services.memory.skills import SkillsManager
            from src.constants import DATA_DIR
            SkillsManager(DATA_DIR).update_skill(ref[8:], {"status": "draft"}, owner=owner)
        except Exception:
            logger.exception("teach: could not hide %s", ref)

    @staticmethod
    def _register_with_immune(owner: str, procedure: Mapping[str, Any]) -> None:
        try:
            from src.immune_system.service import service as immune_service
            immune_service().register_asset(
                owner=owner,
                request={"asset_id": f"procedure://{procedure['id']}",
                         "asset_version": str(procedure.get("revision_hash") or procedure.get("revision")),
                         "kind": "learned_procedure", "project_id": procedure.get("project_id", ""),
                         "dependencies": [], "fallback_refs": [],
                         "health_contract": {"checks": ["procedure replay succeeds"],
                                             "ttl_seconds": 86400}},
            )
        except Exception:
            # Installation is durable even if health registration is briefly
            # unavailable. The event is enough for a later reconciliation.
            logger.exception("teach: installed procedure could not be registered with Immune")

    def events(self, *, owner: str, since: int = 0, limit: int = 200) -> List[Dict[str, Any]]:
        return self._db().events(owner=owner, since=since, limit=limit)


def service() -> TeachService:
    global _service
    if _service is None:
        _service = TeachService()
        _service.recover_interrupted_recordings()
    return _service


def reset_service() -> None:
    global _service
    _service = None


def capture_tool_observation(*, owner: str, session_id: str, project_id: str,
                             tool: str, arguments: Any, result: Any,
                             success: bool = True, duration_ms: int = 0) -> Optional[Dict[str, Any]]:
    """Best-effort turn hook. Recording can never make the user's tool fail."""
    try:
        current = service().active(owner=owner, session_id=session_id, project_id=project_id)
        if current is None or current.get("status") != "recording":
            return None
        return service().observe(owner=owner, demonstration_id=current["id"], observation={
            "kind": "tool_call", "tool": tool, "arguments": arguments,
            "result": result, "success": bool(success), "classification": "unknown",
            "duration_ms": max(0, int(duration_ms or 0)),
        })
    except Exception:
        logger.exception("teach: observation capture failed without affecting the tool")
        return None
