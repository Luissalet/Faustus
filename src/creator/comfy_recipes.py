"""comfy_recipes.py — WP11: typed-slot ComfyUI recipes over `src.media_workflows`.

`src/media_workflows.py` already IS the authority for what a ComfyUI graph
is, how it fingerprints, and whether a human has reviewed the nodes it calls
(MEDIA-02). This module does not re-implement any of that. A `ComfyRecipe`
is a small, named translation layer: it maps a handful of TYPED SLOTS —
`prompt`, `negative`, `seed`, `steps`, `cfg`, `size`, `reference_image`,
`mask`, `controlnet`, `lora` (`SLOT_TYPES`) — onto the declared `inputs` of
one underlying `MediaWorkflow` template, so a Studio panel or an agent tool
can talk about "the prompt" and "the seed" without knowing that one template
calls its size control `size` and another calls it `aspect_ratio`.

Two kinds of recipe:

* **factory** — declared in `_FACTORY` below, each one driving a template
  that ships under `config/media_workflows/` (code review, same authority
  as every other template). `catalogue()`/`get_recipe()` return these to
  every caller regardless of owner.
* **user** — created through `create_user_recipe()` from a caller-supplied
  template body shaped exactly like a `config/media_workflows/*.json` file
  (`id`, `version`, `title`, `engine`, `inputs`, `computed`, `models`,
  `requires_nodes`, `outputs`, `graph`). It is parsed with
  `media_workflows.parse()` — the SAME parser, so the same placeholder and
  shape checks apply — and stored owner-scoped in
  `DATA_DIR/creator/comfy_recipes.sqlite3` (never under `config/`, which is
  code review's business). A user recipe's underlying workflow id is never
  registered in `config/media_workflows/reviews/approved_recipes.json`, so
  `workflow.review_status()` reports it unreviewed until a human edits that
  file — and because `src.media_runs.start()` only ever loads a template
  BY ID FROM `WORKFLOWS_DIR` on disk, a user recipe's ad hoc graph has no
  path to `media_runs` at all: it can be explained and compiled (both pure),
  never submitted. "A recipe not approved is not sent" holds by construction
  here, not by an extra gate this module would have to remember to check.

`compile()` is the one function that matters operationally: given a recipe
id and a params dict keyed by slot name, it (1) validates against the
`(engine="comfyui", task)` `ParamSchema` registered in `src/creator/params.py`
when one exists, (2) renames the validated values from slot names to the
underlying template's input names, and (3) calls `media_workflows.render()`
to get the actual graph plus its fingerprint. Same input -> same graph ->
same fingerprint, because every step in between is pure and the fingerprint
itself comes straight from `MediaWorkflow.fingerprint()` (WP01's v2, which
already folds `models`/`requires_nodes`/`outputs` in — a different model or
required node IS a different fingerprint, nothing here has to re-derive
that).

`plan()`/`submit()` themselves are WP10's job — see
`src/creator/adapters/comfyui.py`, which this module's `resolve_for_adapter()`
feeds a `(workflow_id, translated_inputs)` pair so a `recipe_id + params`
call reaches `media_runs.plan()`/`.start()` exactly like a raw `workflow_id`
call already did (additive: nothing about the existing call path changes).
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src import media_workflows as workflows
from src.contracts.base import ContractError
from src.creator import params as creator_params

# ── typed slots ──────────────────────────────────────────────────────────

SLOT_PROMPT = "prompt"
SLOT_NEGATIVE = "negative"
SLOT_SEED = "seed"
SLOT_STEPS = "steps"
SLOT_CFG = "cfg"
SLOT_SIZE = "size"
SLOT_REFERENCE_IMAGE = "reference_image"
SLOT_MASK = "mask"
SLOT_CONTROLNET = "controlnet"
SLOT_LORA = "lora"

#: The closed vocabulary of slot names a `SlotSpec` may use. Closed like
#: `media_workflows.INPUT_TYPES` — a slot nobody declared here is a naming
#: mistake in a recipe, not a new capability that quietly works anyway.
SLOT_TYPES = frozenset({
    SLOT_PROMPT, SLOT_NEGATIVE, SLOT_SEED, SLOT_STEPS, SLOT_CFG, SLOT_SIZE,
    SLOT_REFERENCE_IMAGE, SLOT_MASK, SLOT_CONTROLNET, SLOT_LORA,
})

TASK_TXT2IMG = "image.generate"
TASK_IMG2IMG = "image.edit"
TASK_INPAINT = "image.inpaint"
TASK_CONTROLNET = "image.controlnet"
TASK_UPSCALE = "image.upscale"


class RecipeError(ContractError):
    """Same shape as `media_workflows.TemplateError` — `path` names the
    field a caller has to change. Raised for anything this module itself
    catches before reaching `media_workflows` (unknown recipe id, params
    that fail the registered `ParamSchema`, a user template that will not
    parse) — never for a refusal `media_workflows`/`media_runs` already
    knows how to explain, which is returned as their own dict instead."""


@dataclass(frozen=True)
class SlotSpec:
    """One typed slot, and the name of the underlying template `InputSpec`
    it fills. `slot` is drawn from `SLOT_TYPES`; `workflow_input` is
    whatever that specific template happens to call the same idea."""

    slot: str
    workflow_input: str
    required: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"slot": self.slot, "workflow_input": self.workflow_input, "required": self.required}


@dataclass(frozen=True)
class ComfyRecipe:
    """A named, explainable way to drive one `MediaWorkflow` through typed
    slots. Never carries the graph itself — that always comes from
    `media_workflows.load()` (factory) or a fresh `media_workflows.parse()`
    of the stored body (user), so there is exactly one place a graph is
    ever assembled."""

    id: str
    title: str
    description: str
    task: str
    workflow_id: str
    workflow_version: str = ""
    slots: Tuple[SlotSpec, ...] = ()
    variantable: bool = False
    source: str = "factory"          # "factory" | "user"
    owner: str = ""
    project_id: str = ""
    created_at: float = 0.0
    #: Only set for source="user" — the template body this recipe was
    #: created from, so `compile()` can `media_workflows.parse()` it fresh
    #: every time rather than trust a cached `MediaWorkflow` object.
    user_template: Mapping[str, Any] = field(default_factory=dict)

    def slot_map(self) -> Dict[str, str]:
        return {s.slot: s.workflow_input for s in self.slots}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "description": self.description,
            "task": self.task, "engine": "comfyui",
            "workflow_id": self.workflow_id, "workflow_version": self.workflow_version,
            "slots": [s.to_dict() for s in self.slots],
            "variantable": self.variantable, "source": self.source,
        }


@dataclass(frozen=True)
class ComfyGraph:
    """What `compile()` returns — the ficha's `ComfyGraph`. `reviewed`
    reflects `media_workflows.review_status()` on the SAME workflow this
    graph was rendered from; a caller building a submit UI reads it to
    grey out the button rather than let a rejection surface only at
    `media_runs.start()` time."""

    recipe_id: str
    workflow_id: str
    workflow_version: str
    graph: Mapping[str, Any]
    values: Mapping[str, Any]
    fingerprint: str
    models: Tuple[Dict[str, Any], ...]
    reviewed: bool
    review_reason: str
    new_nodes: Tuple[str, ...]

    def to_dict(self, *, with_graph: bool = True) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "recipe_id": self.recipe_id, "workflow_id": self.workflow_id,
            "workflow_version": self.workflow_version, "fingerprint": self.fingerprint,
            "values": dict(self.values), "models": list(self.models),
            "reviewed": self.reviewed, "review_reason": self.review_reason,
            "new_nodes": list(self.new_nodes),
        }
        if with_graph:
            out["graph"] = dict(self.graph)
        return out


# ── factory catalogue ───────────────────────────────────────────────────
#
# Each entry drives one reviewed template under `config/media_workflows/`
# (see `config/media_workflows/reviews/approved_recipes.json` for the
# fingerprints this lot added). Slot names on the left are this module's
# vocabulary; `workflow_input` on the right is that template's own
# `InputSpec.name` — read the template if the mapping looks surprising.

RECIPE_TXT2IMG = ComfyRecipe(
    id="txt2img", title="Text to image",
    description="Prompt in, picture out. Steps/cfg/seed/size are direct controls.",
    task=TASK_TXT2IMG, workflow_id="image.txt2img",
    slots=(
        SlotSpec(SLOT_PROMPT, "prompt", required=True),
        SlotSpec(SLOT_NEGATIVE, "negative_prompt"),
        SlotSpec(SLOT_SIZE, "size"),
        SlotSpec(SLOT_STEPS, "steps"),
        SlotSpec(SLOT_CFG, "cfg"),
        SlotSpec(SLOT_SEED, "seed"),
    ),
    variantable=True,
)

RECIPE_TXT2IMG_LORA = ComfyRecipe(
    id="txt2img-lora", title="Text to image with a LoRA",
    description="Same as txt2img, with one LoRA applied to both the UNet and the CLIP conditioning.",
    task=TASK_TXT2IMG, workflow_id="image.txt2img-lora",
    slots=(
        SlotSpec(SLOT_PROMPT, "prompt", required=True),
        SlotSpec(SLOT_NEGATIVE, "negative_prompt"),
        SlotSpec(SLOT_LORA, "lora_strength"),
        SlotSpec(SLOT_SIZE, "size"),
        SlotSpec(SLOT_STEPS, "steps"),
        SlotSpec(SLOT_CFG, "cfg"),
        SlotSpec(SLOT_SEED, "seed"),
    ),
    variantable=True,
)

RECIPE_IMG2IMG = ComfyRecipe(
    id="img2img", title="Image to image",
    description="A variation on a picture you already have, with strength/steps/cfg exposed directly.",
    task=TASK_IMG2IMG, workflow_id="image.img2img",
    slots=(
        SlotSpec(SLOT_REFERENCE_IMAGE, "reference_image", required=True),
        SlotSpec(SLOT_PROMPT, "prompt", required=True),
        SlotSpec(SLOT_NEGATIVE, "negative_prompt"),
        SlotSpec(SLOT_STEPS, "steps"),
        SlotSpec(SLOT_CFG, "cfg"),
        SlotSpec(SLOT_SEED, "seed"),
    ),
    variantable=True,
)

RECIPE_INPAINT = ComfyRecipe(
    id="inpaint", title="Inpainting with a mask",
    description="IMG04: reference and mask are both resolved to engine uploads; the mask is a MASK-typed graph input, never a conditioning field.",
    task=TASK_INPAINT, workflow_id="image.inpaint",
    slots=(
        SlotSpec(SLOT_REFERENCE_IMAGE, "reference_image", required=True),
        SlotSpec(SLOT_MASK, "mask", required=True),
        SlotSpec(SLOT_PROMPT, "prompt", required=True),
        SlotSpec(SLOT_NEGATIVE, "negative_prompt"),
        SlotSpec(SLOT_STEPS, "steps"),
        SlotSpec(SLOT_CFG, "cfg"),
        SlotSpec(SLOT_SEED, "seed"),
    ),
    variantable=True,
)

RECIPE_CONTROLNET_POSE = ComfyRecipe(
    id="controlnet-pose", title="ControlNet — pose",
    description="Text to image constrained by a pose control map (a separate staged input from any photo reference).",
    task=TASK_CONTROLNET, workflow_id="image.controlnet-pose",
    slots=(
        SlotSpec(SLOT_CONTROLNET, "controlnet_image", required=True),
        SlotSpec(SLOT_PROMPT, "prompt", required=True),
        SlotSpec(SLOT_NEGATIVE, "negative_prompt"),
        SlotSpec(SLOT_SIZE, "size"),
        SlotSpec(SLOT_STEPS, "steps"),
        SlotSpec(SLOT_CFG, "cfg"),
        SlotSpec(SLOT_SEED, "seed"),
    ),
    variantable=True,
)

RECIPE_CONTROLNET_DEPTH = ComfyRecipe(
    id="controlnet-depth", title="ControlNet — depth",
    description="Text to image constrained by a depth control map.",
    task=TASK_CONTROLNET, workflow_id="image.controlnet-depth",
    slots=(
        SlotSpec(SLOT_CONTROLNET, "controlnet_image", required=True),
        SlotSpec(SLOT_PROMPT, "prompt", required=True),
        SlotSpec(SLOT_NEGATIVE, "negative_prompt"),
        SlotSpec(SLOT_SIZE, "size"),
        SlotSpec(SLOT_STEPS, "steps"),
        SlotSpec(SLOT_CFG, "cfg"),
        SlotSpec(SLOT_SEED, "seed"),
    ),
    variantable=True,
)

RECIPE_UPSCALE = ComfyRecipe(
    id="upscale", title="Upscale",
    description="IMG09: model-driven upscale; the receipt keeps the source resolution and the scaler used.",
    task=TASK_UPSCALE, workflow_id="image.upscale",
    slots=(
        SlotSpec(SLOT_REFERENCE_IMAGE, "reference_image", required=True),
        SlotSpec(SLOT_SIZE, "scale"),
    ),
    variantable=False,
)

_FACTORY: Dict[str, ComfyRecipe] = {
    r.id: r for r in (
        RECIPE_TXT2IMG, RECIPE_TXT2IMG_LORA, RECIPE_IMG2IMG, RECIPE_INPAINT,
        RECIPE_CONTROLNET_POSE, RECIPE_CONTROLNET_DEPTH, RECIPE_UPSCALE,
    )
}


# ── param schemas for the factory recipes ───────────────────────────────
#
# Registered against `src/creator/params.py` under engine="comfyui" so the
# same schema validates a Studio form, this module's `compile()` and an
# agent tool call (MOD-06) — `compile()` below is the one place that reads
# them back for a factory recipe.

def _register_factory_schemas() -> None:
    from src.creator.params import (
        ParamDependency, ParamField, ParamSchema, TYPE_ASSET_REF, TYPE_BOOLEAN,
        TYPE_ENUM, TYPE_INTEGER, TYPE_NUMBER, TYPE_STRING, register_schema,
    )

    common_prompt_fields = (
        ParamField(key=SLOT_PROMPT, label="Prompt", type=TYPE_STRING, required=True),
        ParamField(key=SLOT_NEGATIVE, label="Negative prompt", type=TYPE_STRING, required=False),
        ParamField(key=SLOT_STEPS, label="Steps", type=TYPE_INTEGER, default=24, minimum=4, maximum=80,
                   affects_time=True),
        ParamField(key=SLOT_CFG, label="CFG scale", type=TYPE_NUMBER, default=7.0, minimum=1, maximum=20),
        ParamField(key=SLOT_SEED, label="Seed", type=TYPE_INTEGER, minimum=0,
                   description="Intento reproducible bajo las mismas condiciones; no garantía bit a bit."),
    )

    register_schema(ParamSchema(engine="comfyui", task=TASK_TXT2IMG, fields=(
        *common_prompt_fields,
        ParamField(key=SLOT_SIZE, label="Size", type=TYPE_ENUM, default="square",
                   enum=("square", "portrait", "landscape", "widescreen")),
    )))

    register_schema(ParamSchema(engine="comfyui", task="image.generate.lora", fields=(
        *common_prompt_fields,
        ParamField(key=SLOT_SIZE, label="Size", type=TYPE_ENUM, default="square",
                   enum=("square", "portrait", "landscape")),
        ParamField(key=SLOT_LORA, label="LoRA strength", type=TYPE_NUMBER, default=0.8, minimum=0, maximum=2),
    )))

    register_schema(ParamSchema(engine="comfyui", task=TASK_IMG2IMG, fields=(
        ParamField(key=SLOT_REFERENCE_IMAGE, label="Reference image", type=TYPE_ASSET_REF, required=True),
        *common_prompt_fields,
        ParamField(key="strength", label="Strength", type=TYPE_NUMBER, default=0.55, minimum=0.05, maximum=0.95,
                   depends_on=ParamDependency(param=SLOT_REFERENCE_IMAGE, require_presence=True)),
    )))

    register_schema(ParamSchema(engine="comfyui", task=TASK_INPAINT, fields=(
        ParamField(key=SLOT_REFERENCE_IMAGE, label="Reference image", type=TYPE_ASSET_REF, required=True),
        ParamField(key=SLOT_MASK, label="Mask", type=TYPE_ASSET_REF, required=True,
                   depends_on=ParamDependency(param=SLOT_REFERENCE_IMAGE, require_presence=True)),
        *common_prompt_fields,
        ParamField(key="strength", label="Strength", type=TYPE_NUMBER, default=0.85, minimum=0.2, maximum=1.0),
    )))

    register_schema(ParamSchema(engine="comfyui", task=TASK_CONTROLNET, fields=(
        ParamField(key=SLOT_CONTROLNET, label="Control image", type=TYPE_ASSET_REF, required=True),
        *common_prompt_fields,
        ParamField(key=SLOT_SIZE, label="Size", type=TYPE_ENUM, default="portrait",
                   enum=("square", "portrait", "landscape")),
        ParamField(key="conditioning_scale", label="Conditioning scale", type=TYPE_NUMBER, default=1.0,
                   minimum=0, maximum=2,
                   depends_on=ParamDependency(param=SLOT_CONTROLNET, require_presence=True)),
    )))

    register_schema(ParamSchema(engine="comfyui", task=TASK_UPSCALE, fields=(
        ParamField(key=SLOT_REFERENCE_IMAGE, label="Image", type=TYPE_ASSET_REF, required=True),
        ParamField(key=SLOT_SIZE, label="Scale", type=TYPE_ENUM, default="4", enum=("2", "4")),
    )))


_register_factory_schemas()


# ── reading the catalogue ───────────────────────────────────────────────

def catalogue(*, owner: str = "", project_id: str = "", db_path: str = "") -> List[ComfyRecipe]:
    """Every factory recipe, plus this owner's own recipes when `owner` is
    given. `owner=""` (no session) returns factory recipes only — never
    another owner's user recipes, per CONTRATO rule 3. `db_path` overrides
    the store location (tests only; routes always use the default)."""
    out = list(_FACTORY.values())
    if owner:
        out.extend(_list_user_recipes(owner=owner, project_id=project_id, db_path=db_path))
    return out


def get_recipe(recipe_id: str, *, owner: str = "", db_path: str = "") -> Optional[ComfyRecipe]:
    """A factory recipe (any caller) or a user recipe scoped to `owner`.
    A user recipe that belongs to someone else is indistinguishable from
    one that does not exist — both return `None` here, so the route layer
    answers 404 either way (CONTRATO rule 3)."""
    recipe = _FACTORY.get(recipe_id)
    if recipe is not None:
        return recipe
    return _get_user_recipe(recipe_id, owner=owner, db_path=db_path)


def explain(recipe: ComfyRecipe) -> str:
    """A human-readable account of what this recipe does and what it
    needs — the ficha's `explain(recipe)`. Pulls in the underlying
    template's models/requires_nodes/review status rather than repeating
    only what the recipe object itself carries, so the answer is honest
    about what would actually have to be on the engine."""
    workflow, err = _load_workflow(recipe)
    lines = [f"{recipe.title} — {recipe.description}".strip(" —")]
    lines.append(f"Task: {recipe.task} (engine: comfyui). Source: {recipe.source}.")
    slot_lines = ", ".join(f"{s.slot}->{s.workflow_input}{'*' if s.required else ''}" for s in recipe.slots)
    lines.append(f"Slots: {slot_lines or '(none declared)'}")
    if err is not None:
        lines.append(f"Underlying template could not be read: {err}")
        return "\n".join(lines)
    review = workflows.review_status(workflow)
    models = ", ".join(f"{m.name} ({m.kind})" for m in workflow.models) or "(none declared)"
    nodes = ", ".join(workflow.requires_nodes) or "(none declared)"
    lines.append(f"Underlying template: {workflow.id} {workflow.version}, fingerprint {workflow.fingerprint()}")
    lines.append(f"Models required: {models}")
    lines.append(f"Nodes required: {nodes}")
    if review["reviewed"]:
        lines.append("Review status: reviewed — every node this graph calls has been looked at.")
    else:
        new_nodes = ", ".join(review["new_nodes"]) or "unknown"
        lines.append(
            f"Review status: NOT reviewed ({review['reason']}) — node type(s) {new_nodes} have not been "
            "signed off; this recipe cannot be submitted until a human reviews and approves it.")
    return "\n".join(lines)


# ── compiling ────────────────────────────────────────────────────────────

def _load_workflow(recipe: ComfyRecipe) -> Tuple[Optional[workflows.MediaWorkflow], Optional[str]]:
    """The `MediaWorkflow` a recipe drives, or `(None, reason)`.

    Factory recipes load from `WORKFLOWS_DIR` on disk, same as any other
    caller of `media_workflows.load()`. User recipes are parsed fresh from
    the stored body every time — pure, so two calls with the same stored
    body produce byte-identical `MediaWorkflow` objects and therefore the
    same fingerprint."""
    if recipe.source == "user":
        if not recipe.user_template:
            return None, "user recipe has no stored template body"
        try:
            return workflows.parse(dict(recipe.user_template), source=f"user:{recipe.id}"), None
        except workflows.TemplateError as e:
            return None, str(e)
    workflow = workflows.load(recipe.workflow_id, recipe.workflow_version)
    if workflow is None:
        return None, f"underlying template {recipe.workflow_id!r} is not installed"
    return workflow, None


def describe(recipe: ComfyRecipe) -> Dict[str, Any]:
    """`recipe.to_dict()` plus everything a route needs to render a detail
    view — `explain()`'s text, the underlying template's review status,
    models and required nodes — without a caller reaching past this
    module's public surface into `_load_workflow`."""
    out = recipe.to_dict()
    out["explain"] = explain(recipe)
    workflow, err = _load_workflow(recipe)
    if workflow is None:
        out["review_status"] = {"reviewed": False, "reason": "template_unavailable", "new_nodes": []}
        out["load_error"] = err
        return out
    review = workflows.review_status(workflow)
    out["review_status"] = review
    out["models"] = [m.to_dict() for m in workflow.models]
    out["requires_nodes"] = list(workflow.requires_nodes)
    out["workflow_fingerprint"] = workflow.fingerprint()
    return out


def compile(recipe_id: str, params: Optional[Mapping[str, Any]] = None, *,
           owner: str = "", seed_override: Optional[int] = None, db_path: str = "") -> ComfyGraph:
    """Params keyed by slot name -> a `ComfyGraph`, deterministically.

    Same `recipe_id`+`params` (seed included, once resolved) always
    produces the same graph and the same fingerprint — nothing here reads
    the clock or the filesystem beyond loading the template itself, and
    `media_workflows.render()` is documented pure. Raises `RecipeError`
    (unknown recipe, schema validation failure naming the field) or
    returns a `ComfyGraph` whose `reviewed=False` names exactly why a
    caller must not treat it as submittable."""
    recipe = get_recipe(recipe_id, owner=owner, db_path=db_path)
    if recipe is None:
        raise RecipeError("recipe_id", f"no recipe called {recipe_id!r}")

    raw = dict(params or {})
    if seed_override is not None:
        raw[SLOT_SEED] = seed_override

    schema = creator_params.get_schema("comfyui", recipe.task)
    if schema is not None:
        result = creator_params.validate("comfyui", recipe.task, raw)
        if not result.ok:
            first = result.errors[0]
            raise RecipeError(first.field, first.message,
                              got={"errors": [e.to_dict() for e in result.errors]})
        normalized = result.normalized
    else:
        # No registered schema (a fresh user recipe, typically) — pass
        # slot values through as given; `media_workflows.render()` below
        # still enforces the underlying template's own per-field checks
        # and reports a `TemplateError` naming the exact field.
        normalized = raw

    workflow, err = _load_workflow(recipe)
    if workflow is None:
        raise RecipeError("recipe.workflow_id", err or "template could not be loaded")

    slot_map = recipe.slot_map()
    template_values: Dict[str, Any] = {}
    for slot, value in normalized.items():
        if value is None:
            continue
        template_values[slot_map.get(slot, slot)] = value

    try:
        rendered = workflows.render(workflow, template_values)
    except workflows.TemplateError as e:
        raise RecipeError(e.path, e.message, got=e.got if e.has_got else None) from e

    review = workflows.review_status(workflow)
    return ComfyGraph(
        recipe_id=recipe.id, workflow_id=workflow.id, workflow_version=workflow.version,
        graph=rendered["graph"], values=rendered["values"], fingerprint=rendered["fingerprint"],
        models=tuple(rendered["models"]),
        reviewed=bool(review["reviewed"]), review_reason=str(review["reason"]),
        new_nodes=tuple(review["new_nodes"]),
    )


def compile_batch(recipe_id: str, params: Optional[Mapping[str, Any]] = None, *,
                  seeds: Sequence[int], owner: str = "", db_path: str = "") -> Tuple[ComfyGraph, ...]:
    """A batch of variants — same recipe and params, one `ComfyGraph` per
    seed in `seeds`. Each graph is independently deterministic (`compile()`
    with that one seed forced in); the batch itself adds nothing but the
    loop, so a caller who wants five reproducible variants of one prompt
    gets five fingerprints it can individually replay later."""
    if not seeds:
        raise RecipeError("seeds", "at least one seed is required for a batch")
    return tuple(compile(recipe_id, params, owner=owner, seed_override=int(s), db_path=db_path) for s in seeds)


def resolve_for_adapter(recipe_id: str, params: Optional[Mapping[str, Any]] = None, *,
                        owner: str = "") -> Optional[Tuple[str, Dict[str, Any]]]:
    """`(workflow_id, translated_inputs)` for `recipe_id`, or `None` if
    `recipe_id` names no recipe this owner can see — the hook
    `src/creator/adapters/comfyui.py` uses to accept `recipe_id + params`
    additively, falling back to treating `op` as a raw workflow id exactly
    as it already did when this returns `None`. Deliberately does NOT
    validate against the param schema again here — `media_runs.plan()`/
    `.start()` (called right after, by the adapter) validate the translated
    inputs against the template itself, which is the one authority that
    actually submits."""
    recipe = get_recipe(recipe_id, owner=owner)
    if recipe is None:
        return None
    slot_map = recipe.slot_map()
    translated = {slot_map.get(k, k): v for k, v in dict(params or {}).items() if v is not None}
    return recipe.workflow_id, translated


# ── user recipes: owner-scoped sqlite store ─────────────────────────────
#
# Same shape as `src/budget_account.py`: a private sqlite file under
# `DATA_DIR`, WAL, a real `BEGIN IMMEDIATE` for writes, connection closed
# on every call (not just committed — Windows needs the handle actually
# released). No process-memory cache: another worker's write is visible on
# the next read, which is the whole point of a durable store.

def default_db_path() -> str:
    from src.constants import DATA_DIR
    return str(Path(DATA_DIR) / "creator" / "comfy_recipes.sqlite3")


@contextmanager
def _conn(db_path: str, *, write: bool = False):
    path = Path(db_path)
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        if write:
            conn.execute("BEGIN IMMEDIATE")
        conn.execute("""CREATE TABLE IF NOT EXISTS user_recipes (
            recipe_id TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            task TEXT NOT NULL DEFAULT '',
            slots_json TEXT NOT NULL DEFAULT '[]',
            template_json TEXT NOT NULL,
            created_at REAL NOT NULL)""")
        conn.execute("CREATE INDEX IF NOT EXISTS user_recipes_owner ON user_recipes(owner, project_id)")
        yield conn
        if write:
            conn.commit()
    except Exception:
        if write:
            conn.rollback()
        raise
    finally:
        conn.close()


def _row_to_recipe(row: sqlite3.Row) -> ComfyRecipe:
    slots_raw = json.loads(row["slots_json"] or "[]")
    slots = tuple(SlotSpec(slot=s["slot"], workflow_input=s["workflow_input"],
                           required=bool(s.get("required", False))) for s in slots_raw)
    template = json.loads(row["template_json"])
    return ComfyRecipe(
        id=row["recipe_id"], title=row["title"], description=row["description"],
        task=row["task"], workflow_id=str(template.get("id") or row["recipe_id"]),
        workflow_version=str(template.get("version") or ""), slots=slots,
        variantable=False, source="user", owner=row["owner"], project_id=row["project_id"],
        created_at=float(row["created_at"]), user_template=template,
    )


def create_user_recipe(owner: str, project_id: str, *, title: str, description: str,
                       task: str, slots: Sequence[Mapping[str, Any]],
                       template: Mapping[str, Any], db_path: str = "") -> ComfyRecipe:
    """A user-defined recipe over a caller-supplied template body.

    The body is parsed with `media_workflows.parse()` before anything is
    stored — a `TemplateError` there is surfaced as-is (bad shape refused
    up front, same as an imported template anywhere else in this project).
    Parsing successfully is NOT review: the stored recipe's underlying
    workflow id is absent from `approved_recipes.json` by construction (a
    fresh, unregistered id), so `explain()`/`compile()` report it
    unreviewed until a human adds an entry — the quarantine
    `06_ADAPTADORES_MULTIMEDIA.md` asks for ("un JSON importado permanece
    en cuarentena... que se pueda parsear no implica que se deba
    ejecutar")."""
    if not owner:
        raise RecipeError("owner", "a user recipe needs an owner")
    try:
        parsed = workflows.parse(dict(template), source=f"user-submitted:{owner}")
    except workflows.TemplateError as e:
        raise RecipeError(e.path, e.message, got=e.got if e.has_got else None) from e

    unknown_slots = [s.get("slot") for s in slots if s.get("slot") not in SLOT_TYPES]
    if unknown_slots:
        raise RecipeError("slots", f"unknown slot type(s): {', '.join(map(str, unknown_slots))}; "
                                    f"known: {', '.join(sorted(SLOT_TYPES))}")
    known_inputs = {i.name for i in parsed.inputs}
    bad_targets = [s.get("workflow_input") for s in slots if s.get("workflow_input") not in known_inputs]
    if bad_targets:
        raise RecipeError("slots", f"slot target(s) not declared by the template: {', '.join(map(str, bad_targets))}")

    recipe_id = f"user_{uuid.uuid4().hex[:20]}"
    now = time.time()
    with _conn(db_path or default_db_path(), write=True) as conn:
        conn.execute(
            "INSERT INTO user_recipes (recipe_id, owner, project_id, title, description, task, "
            "slots_json, template_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (recipe_id, owner, project_id or "", title or parsed.title, description or parsed.description,
             task or "", json.dumps(list(slots)), json.dumps(dict(template)), now),
        )
    return ComfyRecipe(
        id=recipe_id, title=title or parsed.title, description=description or parsed.description,
        task=task or "", workflow_id=parsed.id, workflow_version=parsed.version,
        slots=tuple(SlotSpec(slot=s["slot"], workflow_input=s["workflow_input"],
                             required=bool(s.get("required", False))) for s in slots),
        variantable=False, source="user", owner=owner, project_id=project_id or "",
        created_at=now, user_template=dict(template),
    )


def _get_user_recipe(recipe_id: str, *, owner: str, db_path: str = "") -> Optional[ComfyRecipe]:
    if not owner or not recipe_id.startswith("user_"):
        return None
    with _conn(db_path or default_db_path(), write=False) as conn:
        row = conn.execute(
            "SELECT * FROM user_recipes WHERE recipe_id = ? AND owner = ?", (recipe_id, owner),
        ).fetchone()
    return _row_to_recipe(row) if row is not None else None


def _list_user_recipes(*, owner: str, project_id: str = "", db_path: str = "") -> List[ComfyRecipe]:
    if not owner:
        return []
    with _conn(db_path or default_db_path(), write=False) as conn:
        if project_id:
            rows = conn.execute(
                "SELECT * FROM user_recipes WHERE owner = ? AND project_id = ? ORDER BY created_at",
                (owner, project_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM user_recipes WHERE owner = ? ORDER BY created_at", (owner,),
            ).fetchall()
    return [_row_to_recipe(r) for r in rows]


__all__ = [
    "SLOT_PROMPT", "SLOT_NEGATIVE", "SLOT_SEED", "SLOT_STEPS", "SLOT_CFG", "SLOT_SIZE",
    "SLOT_REFERENCE_IMAGE", "SLOT_MASK", "SLOT_CONTROLNET", "SLOT_LORA", "SLOT_TYPES",
    "TASK_TXT2IMG", "TASK_IMG2IMG", "TASK_INPAINT", "TASK_CONTROLNET", "TASK_UPSCALE",
    "RecipeError", "SlotSpec", "ComfyRecipe", "ComfyGraph",
    "RECIPE_TXT2IMG", "RECIPE_TXT2IMG_LORA", "RECIPE_IMG2IMG", "RECIPE_INPAINT",
    "RECIPE_CONTROLNET_POSE", "RECIPE_CONTROLNET_DEPTH", "RECIPE_UPSCALE",
    "catalogue", "get_recipe", "explain", "describe", "compile", "compile_batch", "resolve_for_adapter",
    "default_db_path", "create_user_recipe",
]
