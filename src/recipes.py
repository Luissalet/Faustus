"""recipes.py — CMP-12: reusable, structured work procedures ("recetas").

Comparative report §3.11: a recipe is
``{id, title, inputs[], steps[], tools[], success_conditions[],
optional_resources[], license?}`` — a short, structured procedure for one
kind of task, injected into a turn INSTEAD OF the full skill haystack
(`src/skills_runtime/`) when it is active. This module owns the shape and
storage; `src/strategy_policy.py::choose_strategy` is what actually injects
an active recipe's steps into a `Strategy` (``context={"recipe_id": ...}``),
and `src/agent_loop.py::_strategy_block` is where that lands in the prompt.

Two sources, never mixed on disk:

* Built-ins — ``docs/recipes/*.json``, read-only, shipped with the repo
  (four to start: review changes, sources-to-report, design-a-function,
  edit-without-changing-tone).
* Drafts — ``DATA_DIR/recipes/<owner>/<id>.json``, owner-scoped, created
  ONLY by `from_run`, always ``status="draft"``. Nothing in this module
  ever promotes a draft to a built-in or to another owner's list — that
  review step is deliberately left for a human, out of this lot's scope
  (see docs/adaptations/decisions/CMP-12.md's Límites section).
"""
from __future__ import annotations

import glob
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.atomic_io import atomic_write_json
from core.log_safety import redact_secrets
from src import constants as _constants

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BUILTIN_DIR = os.path.join(_REPO_ROOT, "docs", "recipes")
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]")


@dataclass
class Recipe:
    id: str
    title: str
    inputs: List[str] = field(default_factory=list)
    steps: List[str] = field(default_factory=list)
    tools: List[str] = field(default_factory=list)
    success_conditions: List[str] = field(default_factory=list)
    optional_resources: List[str] = field(default_factory=list)
    license: Optional[str] = None
    status: str = "published"
    source: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "inputs": list(self.inputs),
            "steps": list(self.steps),
            "tools": list(self.tools),
            "success_conditions": list(self.success_conditions),
            "optional_resources": list(self.optional_resources),
            "license": self.license,
            "status": self.status,
            "source": dict(self.source) if self.source else None,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Recipe":
        return cls(
            id=str(data.get("id") or ""),
            title=str(data.get("title") or ""),
            inputs=[str(x) for x in (data.get("inputs") or [])],
            steps=[str(x) for x in (data.get("steps") or [])],
            tools=[str(x) for x in (data.get("tools") or [])],
            success_conditions=[str(x) for x in (data.get("success_conditions") or [])],
            optional_resources=[str(x) for x in (data.get("optional_resources") or [])],
            license=data.get("license") if isinstance(data.get("license"), str) else None,
            status=str(data.get("status") or "published"),
            source=data.get("source") if isinstance(data.get("source"), dict) else None,
        )


def _builtin_recipes() -> List[Recipe]:
    out: List[Recipe] = []
    try:
        paths = sorted(glob.glob(os.path.join(_BUILTIN_DIR, "*.json")))
    except OSError:
        return out
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            recipe = Recipe.from_dict(data)
            if recipe.id and recipe.title:
                out.append(recipe)
        except (OSError, ValueError):
            logger.debug("[recipes] skipping unreadable built-in %s", path, exc_info=True)
    return out


def _drafts_dir(owner: str) -> str:
    safe_owner = _SAFE_ID_RE.sub("_", str(owner))[:120] or "_"
    return os.path.join(_constants.DATA_DIR, "recipes", safe_owner)


def _owner_drafts(owner: str) -> List[Recipe]:
    out: List[Recipe] = []
    d = _drafts_dir(owner)
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, name), "r", encoding="utf-8") as f:
                data = json.load(f)
            out.append(Recipe.from_dict(data))
        except (OSError, ValueError):
            logger.debug("[recipes] skipping unreadable draft %s", name, exc_info=True)
    return out


def list_recipes(owner: Optional[str] = None) -> List[Recipe]:
    """Built-ins first (stable file-name order), then this owner's own
    drafts, if any. ``owner=None`` returns only the built-ins — drafts are
    private to the owner who made them."""
    out = list(_builtin_recipes())
    if owner:
        out.extend(_owner_drafts(owner))
    return out


def get_recipe(recipe_id: str, owner: Optional[str] = None) -> Optional[Recipe]:
    for recipe in list_recipes(owner):
        if recipe.id == recipe_id:
            return recipe
    return None


def procedure_block(recipe: Recipe) -> str:
    """The short, structured procedure injected into the prompt when this
    recipe is active — steps and the done-condition, not the full metadata
    (license/optional_resources stay in the UI, not the prompt)."""
    lines = [f"Recipe: {recipe.title} ({recipe.id})"]
    if recipe.inputs:
        lines.append("Inputs: " + "; ".join(recipe.inputs))
    for i, step in enumerate(recipe.steps, 1):
        lines.append(f"{i}. {step}")
    if recipe.success_conditions:
        lines.append("Done when: " + "; ".join(recipe.success_conditions))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# from-run: draft a recipe from one successful task
# ---------------------------------------------------------------------------

class RecipeFromRunError(Exception):
    """Carries an ``error_class`` the route turns straight into the
    ``{"error_class": ...}`` shape every route in this repo uses."""

    def __init__(self, error_class: str, detail: str):
        super().__init__(detail)
        self.error_class = error_class
        self.detail = detail


def _run_log_path(run_id: str) -> str:
    # Same on-disk shape src/agent_runs.py documents in its own module
    # docstring (DATA_DIR/runs/<session>.jsonl) — reimplemented here rather
    # than importing that module's private `_log_path`, since this is a
    # READ of a stable, documented path, not a call into its runtime. If
    # that shape ever changes, this is the one place to update alongside it
    # (documented as a known coupling in docs/adaptations/decisions/CMP-12.md).
    safe = _SAFE_ID_RE.sub("_", str(run_id))[:120]
    return os.path.join(_constants.DATA_DIR, "runs", safe + ".jsonl")


# A run finishes successfully with run.status == "done" (see
# src/agent_runs.py — "waiting_user"/"stopped"/"error" are the other
# terminal values, none of them a success).
_SUCCESS_STATUS = "done"


def from_run(run_id: str, owner: str) -> Recipe:
    """Turn one successful run into a DRAFT recipe (``status="draft"``) —
    never published automatically; a human reviews and promotes it
    separately, which is deliberately out of this module's scope (the
    ``status`` field is the seam for that later step).

    Steps are the distinct tool names the run actually used, in first-seen
    order — a real, if coarse, procedure reconstructed from what happened,
    never invented. The run's own ``label`` (the same title Activity
    already shows for it) becomes the recipe title.

    Secrets: the WHOLE raw log is passed through
    ``core.log_safety.redact_secrets`` before anything is parsed out of
    it, so a secret that ended up in a tool argument or an assistant
    message is masked before it can reach the draft — never only the
    fields this function happens to read.

    Known limitation (see docs/adaptations/decisions/CMP-12.md): the run
    log only carries the ASSISTANT side of the turn (tool events + status),
    not the original user request, so ``inputs`` is a generic placeholder
    rather than reverse-engineered from the conversation.
    """
    path = _run_log_path(run_id)
    if not os.path.isfile(path):
        raise RecipeFromRunError("recipes.run_not_found", f"no run log for {run_id!r}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError as e:
        raise RecipeFromRunError("recipes.run_unreadable", str(e))

    raw = redact_secrets(raw)

    statuses: List[str] = []
    title = ""
    tools: List[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        status = obj.get("status")
        if isinstance(status, str) and status:
            statuses.append(status)
        label = obj.get("label")
        if not title and isinstance(label, str) and label.strip():
            title = label.strip()
        ev = obj.get("ev")
        if isinstance(ev, str) and ev.startswith("data: "):
            payload_text = ev[len("data: "):].strip()
            try:
                payload = json.loads(payload_text)
            except (ValueError, TypeError):
                payload = None
            if isinstance(payload, dict) and payload.get("type") == "tool_start":
                tool_name = payload.get("tool_type") or payload.get("tool") or payload.get("name")
                if isinstance(tool_name, str) and tool_name and tool_name not in tools:
                    tools.append(tool_name)

    if _SUCCESS_STATUS not in statuses:
        raise RecipeFromRunError(
            "recipes.run_not_finished",
            "the run has no recorded success status — only a finished, successful run becomes a recipe",
        )

    steps = [f"use `{tool}`" for tool in tools] or [
        "repeat the run's own steps (no tool events were recorded for it)"
    ]

    recipe = Recipe(
        id=f"draft-{uuid.uuid4().hex[:12]}",
        title=title or f"Recipe from run {run_id}",
        inputs=["task description"],
        steps=steps,
        tools=tools,
        success_conditions=["the result is verified the same way the source run was"],
        optional_resources=[],
        license=None,
        status="draft",
        source={"run_id": str(run_id), "owner": str(owner), "created_at": time.time()},
    )
    _save_draft(owner, recipe)
    return recipe


def _save_draft(owner: str, recipe: Recipe) -> None:
    d = _drafts_dir(owner)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{recipe.id}.json")
    atomic_write_json(path, recipe.to_dict())
