"""src/fanout/plan.py — the request one fan-out run answers: one prompt, a
workspace, and the N candidates to race inside it.

A candidate names WHAT to run it on, never HOW to reach it beyond that: an
explicit ``endpoint_url`` wins, an ``endpoint_id`` is resolved the same way
``src.agent_tools.subagent_tools._route_for`` resolves an agent definition's
endpoint (falls back to the coordinator's route, never silently), and with
neither the candidate runs on the coordinator's own endpoint under its own
model. ``profile`` is free-form (e.g. an agent definition name from
``src.agent_defs``) and only read by ``runner.py`` when present.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

DEFAULT_MAX_ROUNDS = 8
#: 0 = no ceiling (src.budget_account's own convention: accounting only).
DEFAULT_BUDGET_TOKENS = 0


@dataclass
class FanoutCandidate:
    label: str
    model: str = ""
    endpoint_url: str = ""
    endpoint_id: str = ""
    profile: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label, "model": self.model,
            "endpoint_url": self.endpoint_url, "endpoint_id": self.endpoint_id,
            "profile": self.profile,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FanoutCandidate":
        return cls(
            label=str(data.get("label") or "").strip() or "candidate",
            model=str(data.get("model") or ""),
            endpoint_url=str(data.get("endpoint_url") or ""),
            endpoint_id=str(data.get("endpoint_id") or ""),
            profile=str(data.get("profile") or ""),
        )


@dataclass
class FanoutPlan:
    prompt: str
    workspace: str
    candidates: List[FanoutCandidate] = field(default_factory=list)
    max_rounds: int = DEFAULT_MAX_ROUNDS
    budget_tokens: int = DEFAULT_BUDGET_TOKENS
    project_id: str = ""
    goal: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "prompt": self.prompt, "workspace": self.workspace,
            "candidates": [c.to_dict() for c in self.candidates],
            "max_rounds": self.max_rounds, "budget_tokens": self.budget_tokens,
            "project_id": self.project_id, "goal": self.goal or self.prompt,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FanoutPlan":
        return cls(
            prompt=str(data.get("prompt") or ""),
            workspace=str(data.get("workspace") or ""),
            candidates=[FanoutCandidate.from_dict(c) for c in (data.get("candidates") or [])],
            max_rounds=int(data.get("max_rounds") or DEFAULT_MAX_ROUNDS),
            budget_tokens=int(data.get("budget_tokens") or DEFAULT_BUDGET_TOKENS),
            project_id=str(data.get("project_id") or ""),
            goal=str(data.get("goal") or ""),
        )

    def _dedupe_labels(self) -> None:
        seen: Dict[str, int] = {}
        for c in self.candidates:
            base = c.label
            n = seen.get(base, 0)
            seen[base] = n + 1
            if n:
                c.label = f"{base} ({n + 1})"


def from_settings_default_candidates(*, coordinator_model: str,
                                      coordinator_endpoint_url: str = "",
                                      max_candidates: int = 3) -> List[FanoutCandidate]:
    """The default N candidates when the caller named none: the
    coordinator's own model/endpoint, plus the settings-configured worker
    model (``agent_subagent_worker_model``) and dispatch model
    (``dispatch_model``) when they are set and actually different from it —
    "el modelo del chat + dispatch_model + los del model_pool si existen"
    (contract), read as: everything this install already names as a second
    model gets a seat, never invented models that were never configured.
    Always at least one candidate (the coordinator's own), capped at
    ``max_candidates``.
    """
    from src.settings import get_setting

    out: List[FanoutCandidate] = []
    seen_keys: set = set()

    def _add(label: str, model: str, endpoint_url: str = "", endpoint_id: str = "") -> None:
        if len(out) >= max_candidates:
            return
        key = (model or "", endpoint_url or "", endpoint_id or "")
        if key in seen_keys:
            return
        seen_keys.add(key)
        out.append(FanoutCandidate(label=label, model=model, endpoint_url=endpoint_url, endpoint_id=endpoint_id))

    _add("coordinator", coordinator_model or "", coordinator_endpoint_url or "")

    worker_model = str(get_setting("agent_subagent_worker_model", "") or "").strip()
    if worker_model and worker_model.lower() != "auto":
        _add("worker-model", worker_model)

    dispatch_model = str(get_setting("dispatch_model", "") or "").strip()
    dispatch_endpoint_id = str(get_setting("dispatch_endpoint_id", "") or "").strip()
    if dispatch_model:
        _add("dispatch-model", dispatch_model, endpoint_id=dispatch_endpoint_id)

    pool = get_setting("agent_fanout_model_pool", "") or ""
    if isinstance(pool, str):
        pool_models = [m.strip() for m in pool.split(",") if m.strip()]
    elif isinstance(pool, list):
        pool_models = [str(m).strip() for m in pool if str(m).strip()]
    else:
        pool_models = []
    for i, m in enumerate(pool_models):
        _add(f"pool-{i + 1}", m)

    if len(out) < 2:
        # Never a fan-out of exactly one: a second run of the SAME model
        # (higher temperature sampling still makes them diverge) beats
        # silently degrading to "compare one thing to itself and always win".
        # Deliberately bypasses the `_add` dedupe above -- this IS meant to
        # repeat the coordinator's own (model, endpoint) pair.
        out.append(FanoutCandidate(label="coordinator-retry", model=coordinator_model or "",
                                    endpoint_url=coordinator_endpoint_url or ""))
    return out
