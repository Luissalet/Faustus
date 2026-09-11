"""strategy_policy.py — CMP-09: an observable, editable strategy per turn.

Comparative report §3.8: today Faustus picks how hard to work on a task
implicitly (buried in prompt heuristics scattered across
`src/agent_loop.py`) and never says so. This module makes that choice a
first-class, inspectable value — `Strategy` — with a `reasons[]` list a
person (or a test) can actually read, instead of a hidden score.

Two axes, deliberately kept separate:

* ``method`` — WHAT KIND of work this is (`direct_edit`,
  `plan_then_execute`, `research`, `specialised_review`,
  `explore_alternatives`). Chosen from the task text by small, legible
  patterns (`_classify_method`) — never a numeric "confidence" the model
  reports about itself. The ONLY other thing allowed to change it is an
  escalation from OBSERVABLE evidence already produced by a prior attempt
  on the same task (`context["failures_observed"]` /
  `context["uncovered_requirements"]`) — see `choose_strategy`'s docstring.
* ``profile`` — HOW CAREFULLY to do that same kind of work
  (`fast`/`balanced`/`deep_review`). Only scales steps/budget for whatever
  method was already picked; it never itself picks a different method.
  `profile_diff` makes "what changes" between two profiles inspectable
  without running anything.

``METHODS`` deliberately has no "council"/multi-agent-review entry at all —
`routes/council_routes.py` and `src/chat_team.py` exist elsewhere in this
codebase as an explicit, user-invoked feature, but nothing in this module
can ever choose it: there is no branch, default or escalation path here
that produces a value outside `METHODS` (see
`tests/test_cmp09_strategy.py::test_never_defaults_to_council`).

Recipes (`src/recipes.py`, CMP-12) plug into the SAME `Strategy` shape:
an active recipe's own declared steps replace the method's generic ones
(`choose_strategy(..., context={"recipe_id": ...})`) instead of adding a
second, parallel "what to do" concept.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.atomic_io import atomic_write_json
from src import constants as _constants

logger = logging.getLogger(__name__)

METHODS = (
    "direct_edit",
    "plan_then_execute",
    "research",
    "specialised_review",
    "explore_alternatives",
)
PROFILES = ("fast", "balanced", "deep_review")
DEFAULT_PROFILE = "balanced"


@dataclass
class Strategy:
    """Observable and editable — every field here is something a person can
    read in the compositor or override, not an internal score."""

    method: str
    steps: List[str] = field(default_factory=list)
    budget: Dict[str, Any] = field(default_factory=dict)
    models_hint: List[str] = field(default_factory=list)
    permissions_needed: List[str] = field(default_factory=list)
    close_criteria: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "method": self.method,
            "steps": list(self.steps),
            "budget": dict(self.budget),
            "models_hint": list(self.models_hint),
            "permissions_needed": list(self.permissions_needed),
            "close_criteria": list(self.close_criteria),
            "reasons": list(self.reasons),
        }


# ---------------------------------------------------------------------------
# Method defaults — steps/budget/close_criteria/permissions per method,
# BEFORE any profile scaling or recipe substitution.
# ---------------------------------------------------------------------------

_BASE_STEPS: Dict[str, List[str]] = {
    "direct_edit": [
        "locate the exact text/code to change",
        "apply the edit",
        "verify (tests, or a read-back of the changed spot)",
    ],
    "plan_then_execute": [
        "write a short plan",
        "confirm or adjust the plan against what the task actually asks",
        "execute the plan step by step",
        "verify the result against close_criteria",
    ],
    "research": [
        "gather candidate sources",
        "read and extract, keeping citations attached to each claim",
        "synthesise, dropping nothing that lost its citation",
        "verify claims against the sources actually read",
    ],
    "specialised_review": [
        "read the diff/change in full before judging any part of it",
        "check it against the stated review criteria",
        "report findings, most severe first, each with the concrete failure it causes",
    ],
    "explore_alternatives": [
        "isolate two or more alternatives so they cannot interfere with each other",
        "run each alternative to the same bar",
        "compare them explicitly",
        "pick one or combine, and say why",
    ],
}

# tokens/time_s/calls are ASSUMPTIONS the ficha requires to be listed, not a
# measurement — `forecast_with_assumptions` in CMP-08's vocabulary. They are
# small integers picked to be legible and orderable across profiles, not a
# claim about any real model's actual usage.
_BASE_BUDGET: Dict[str, Dict[str, int]] = {
    "direct_edit": {"tokens": 4000, "time_s": 60, "calls": 3},
    "plan_then_execute": {"tokens": 12000, "time_s": 240, "calls": 8},
    "research": {"tokens": 16000, "time_s": 300, "calls": 10},
    "specialised_review": {"tokens": 10000, "time_s": 180, "calls": 6},
    "explore_alternatives": {"tokens": 20000, "time_s": 420, "calls": 14},
}

_CLOSE_CRITERIA: Dict[str, List[str]] = {
    "direct_edit": [
        "the edit landed at the located occurrence(s)",
        "no unrelated file changed",
    ],
    "plan_then_execute": [
        "every plan step is checked off or explicitly dropped with a reason",
    ],
    "research": [
        "every claim in the answer traces to a source actually read",
    ],
    "specialised_review": [
        "every finding is verified against the real diff, not just described",
    ],
    "explore_alternatives": [
        "the comparison covers every alternative on the same criteria",
    ],
}

_PERMISSIONS: Dict[str, List[str]] = {
    "direct_edit": ["file_write"],
    "plan_then_execute": ["file_write", "plan_approval"],
    "research": ["web_search"],
    "specialised_review": ["read_only"],
    "explore_alternatives": ["file_write", "worktree_or_snapshot"],
}

# Profile multipliers scale the SAME method's budget/steps. `review_step`
# adds one extra, explicit review step for deep_review — the visible part of
# "revisión intensiva" the ficha asks for.
_PROFILE_MULTIPLIERS: Dict[str, Dict[str, Any]] = {
    "fast": {"tokens": 0.5, "time_s": 0.5, "calls": 0.6, "review_step": False},
    "balanced": {"tokens": 1.0, "time_s": 1.0, "calls": 1.0, "review_step": False},
    "deep_review": {"tokens": 2.2, "time_s": 2.5, "calls": 1.8, "review_step": True},
}

_REVIEW_STEP = "review the result against close_criteria before closing"

# Deliberately simple, legible keyword patterns — CMP-09 requires the
# REASON to be inspectable, not a black-box score. First match wins; order
# is the priority when a task text could plausibly match more than one.
_METHOD_PATTERNS: List[tuple] = [
    ("specialised_review", re.compile(
        r"\b(revisa|revisar|review|audita|audit)\b.{0,40}\b(cambios|changes|diff|pr\b|pull request)",
        re.I,
    )),
    ("research", re.compile(
        r"\b(convierte|convertir|resume|resumir|summari[sz]e|investiga|investigar|research)\b.{0,40}"
        r"\b(fuentes|sources|informe|report)\b",
        re.I,
    )),
    ("explore_alternatives", re.compile(
        r"\b(compara|comparar|compare)\b.{0,40}\b(enfoques|approaches|alternativ\w+|formas|ways|opciones|options)\b"
        r"|\bexplore\b.{0,20}\balternativ",
        re.I,
    )),
    ("plan_then_execute", re.compile(
        r"\b(dise[ñn]a|dise[ñn]ar|design)\b.{0,60}\b(funci[oó]n|function|feature|sistema|system)\b",
        re.I,
    )),
    ("direct_edit", re.compile(
        r"\b(edita|editar|edit|corrige|corregir|fix|arregla|arreglar)\b.{0,40}"
        r"\b(pasaje|texto|text|passage|typo|l[ií]nea|line|tono|tone)\b",
        re.I,
    )),
]

_SHORT_EDIT_VERB_RE = re.compile(r"\b(fix|corrige|arregla|add|a[ñn]ade|rename|renombra)\b", re.I)
_SHORT_TASK_MAX_CHARS = 240


def _classify_method(task_text: str) -> tuple:
    """(method, reason) — never a bare method with no reason attached."""
    text = task_text or ""
    for method, pattern in _METHOD_PATTERNS:
        if pattern.search(text):
            return method, f"task text matched the '{method}' pattern"
    if len(text.strip()) <= _SHORT_TASK_MAX_CHARS and _SHORT_EDIT_VERB_RE.search(text):
        return "direct_edit", f"short task text (<= {_SHORT_TASK_MAX_CHARS} chars) with a single edit verb"
    return "plan_then_execute", "no specific pattern matched; multi-step is the safe unopinionated default"


def _scale_budget(base: Dict[str, int], profile: str) -> Dict[str, Any]:
    mult = _PROFILE_MULTIPLIERS.get(profile, _PROFILE_MULTIPLIERS[DEFAULT_PROFILE])
    return {
        "tokens": int(round(base["tokens"] * mult["tokens"])),
        "time_s": int(round(base["time_s"] * mult["time_s"])),
        "calls": max(1, int(round(base["calls"] * mult["calls"]))),
    }


def choose_strategy(
    task_text: str,
    *,
    profile: str = DEFAULT_PROFILE,
    context: Optional[Dict[str, Any]] = None,
) -> Strategy:
    """Pick an observable `Strategy` for one turn. Pure except for two
    best-effort, read-only hardware checks gated by
    ``context.get("hardware_aware", True)`` (never required — a lookup
    failure only skips its `reasons[]` line, it never raises).

    ``context`` (all optional):
      - ``recipe_id``: an active recipe (`src/recipes.py`) — its declared
        steps REPLACE the method's generic ones.
      - ``failures_observed`` / ``uncovered_requirements``: lists of
        strings. The ONLY signals allowed to escalate the method (never a
        model's self-reported confidence) — see the escalation block below.
      - ``hardware_aware``: bool, default True.

    Never returns a method outside `METHODS` — there is no "council"/
    multi-agent branch here at all (see the module docstring).
    """
    context = context or {}
    profile = profile if profile in PROFILES else DEFAULT_PROFILE
    method, reason = _classify_method(task_text)
    reasons = [reason]

    failures = [str(x) for x in (context.get("failures_observed") or []) if str(x).strip()]
    uncovered = [str(x) for x in (context.get("uncovered_requirements") or []) if str(x).strip()]
    if (failures or uncovered) and method == "direct_edit":
        method = "plan_then_execute"
        if failures:
            reasons.append(f"escalated direct_edit -> plan_then_execute: {len(failures)} observed failure(s)")
        if uncovered:
            reasons.append(
                f"escalated direct_edit -> plan_then_execute: {len(uncovered)} uncovered requirement(s)"
            )

    steps = list(_BASE_STEPS[method])
    recipe_id = context.get("recipe_id")
    if recipe_id:
        try:
            from src import recipes as _recipes
            recipe = _recipes.get_recipe(str(recipe_id))
        except Exception:
            logger.debug("[strategy_policy] recipe lookup failed for %s", recipe_id, exc_info=True)
            recipe = None
        if recipe is not None and recipe.steps:
            steps = list(recipe.steps)
            reasons.append(f"recipe '{recipe.id}' active — steps replaced with its declared procedure")
        elif recipe is None:
            reasons.append(f"recipe '{recipe_id}' not found — falling back to the method's generic steps")

    mult = _PROFILE_MULTIPLIERS[profile]
    if mult["review_step"] and _REVIEW_STEP not in steps:
        steps = steps + [_REVIEW_STEP]
        reasons.append(f"profile '{profile}' adds an explicit review step")

    budget = _scale_budget(_BASE_BUDGET[method], profile)

    models_hint: List[str] = []
    if context.get("hardware_aware", True):
        try:
            from src import model_router
            cfg = model_router.get_router_config()
            if cfg.enabled:
                models_hint.append("model_router: enabled — routing follows its scored choice")
            else:
                reasons.append("model_router is inert (enabled=False) — model choice here is not auto-routed")
        except Exception:
            logger.debug("[strategy_policy] model_router lookup failed", exc_info=True)
        try:
            from src import vram_admission
            tickets = vram_admission.pending()
            if tickets:
                reasons.append(f"vram_admission has {len(tickets)} pending ticket(s) — a local model call may queue")
        except Exception:
            logger.debug("[strategy_policy] vram_admission lookup failed", exc_info=True)

    return Strategy(
        method=method,
        steps=steps,
        budget=budget,
        models_hint=models_hint,
        permissions_needed=list(_PERMISSIONS[method]),
        close_criteria=list(_CLOSE_CRITERIA[method]),
        reasons=reasons,
    )


def profile_diff(from_profile: str, to_profile: str) -> Dict[str, Any]:
    """What visibly changes switching profiles for the SAME method — pure,
    no I/O. This is the "diff visible de qué cambia" the ficha requires
    before a switch is applied."""
    a = _PROFILE_MULTIPLIERS.get(from_profile, _PROFILE_MULTIPLIERS[DEFAULT_PROFILE])
    b = _PROFILE_MULTIPLIERS.get(to_profile, _PROFILE_MULTIPLIERS[DEFAULT_PROFILE])
    return {
        "from": from_profile if from_profile in PROFILES else DEFAULT_PROFILE,
        "to": to_profile if to_profile in PROFILES else DEFAULT_PROFILE,
        "budget_multiplier": {"tokens": b["tokens"], "time_s": b["time_s"], "calls": b["calls"]},
        "adds_review_step": bool(b["review_step"]) and not bool(a["review_step"]),
        "drops_review_step": bool(a["review_step"]) and not bool(b["review_step"]),
    }


# ---------------------------------------------------------------------------
# Persistence — which profile/recipe is active, per owner (optionally scoped
# to one session). Same shape as src/attention.py's read marks: one JSON
# file under DATA_DIR, a process-local lock, atomic_write_json.
# ---------------------------------------------------------------------------

_LOCK = threading.Lock()
_DEFAULT_SCOPE = "_default"


def _store_path() -> str:
    # Read DATA_DIR at call time, not bound at import time, so a test's
    # monkeypatch of src.constants.DATA_DIR is honoured (src/attention.py's
    # own _reads_path() does the same for the same reason).
    return os.path.join(_constants.DATA_DIR, "strategy_profile.json")


def _load() -> Dict[str, Any]:
    path = _store_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _scope_key(session_id: Optional[str]) -> str:
    return str(session_id) if session_id else _DEFAULT_SCOPE


def get_active(owner: str, session_id: Optional[str] = None) -> Dict[str, Any]:
    """Active `{profile, recipe_id}` for this owner: the session's own
    override if one was ever set, else the owner's default, else the
    module default. Never raises."""
    data = _load()
    owner_data = data.get(str(owner)) if isinstance(data, dict) else None
    if not isinstance(owner_data, dict):
        return {"profile": DEFAULT_PROFILE, "recipe_id": None}
    scoped = owner_data.get(_scope_key(session_id))
    if isinstance(scoped, dict) and scoped:
        return {"profile": scoped.get("profile", DEFAULT_PROFILE), "recipe_id": scoped.get("recipe_id")}
    default_scope = owner_data.get(_DEFAULT_SCOPE)
    if isinstance(default_scope, dict):
        return {"profile": default_scope.get("profile", DEFAULT_PROFILE), "recipe_id": default_scope.get("recipe_id")}
    return {"profile": DEFAULT_PROFILE, "recipe_id": None}


def set_active(
    owner: str,
    *,
    session_id: Optional[str] = None,
    profile: Optional[str] = None,
    recipe_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist a profile and/or recipe choice. Passing ``recipe_id=""``
    clears the active recipe; omitting it (``None``) leaves it unchanged.
    Raises ``ValueError`` for an unknown profile — callers (the route)
    turn that into a 400, never silently fall back."""
    if profile is not None and profile not in PROFILES:
        raise ValueError(f"unknown profile: {profile!r}")
    with _LOCK:
        data = _load()
        owner_key = str(owner)
        owner_data = data.get(owner_key)
        if not isinstance(owner_data, dict):
            owner_data = {}
        scope_key = _scope_key(session_id)
        current = owner_data.get(scope_key)
        if not isinstance(current, dict):
            current = {"profile": DEFAULT_PROFILE, "recipe_id": None}
        if profile is not None:
            current["profile"] = profile
        if recipe_id is not None:
            current["recipe_id"] = recipe_id or None
        current["updated_at"] = time.time()
        owner_data[scope_key] = current
        data[owner_key] = owner_data
        atomic_write_json(_store_path(), data)
    return get_active(owner, session_id)
