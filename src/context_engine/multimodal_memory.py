"""
context_engine/multimodal_memory.py — how the good one was made, and whether it
can be made again on this machine.

§12.  Faustus already records what it produced: `artifact_store` keeps the file,
`contracts/artifact.Provenance` keeps the model, the workflow id, its version
and fingerprint, the seed and the licence, and `media_runs` keeps the engine job
that made it.  What none of them answers is the question a user actually asks a
week later — *"do that again, with this other reference"* — because a
provenance record is written to be audited, not to be replayed.

The failure this exists for was cheap and infuriating: a render everybody liked,
made through `image.product 1.0.0` with a seed and two LoRAs, was reproduced
three weeks later against a checkpoint that had been replaced.  The graph still
rendered.  The parameters were still accepted.  The picture was different, and
nothing in the system had said so.  Handing a model a recipe whose parameters
the installed engine will not honour is worse than handing it none: it produces
confident, silent drift.

So the order here is not negotiable — **hard compatibility first, ranking
second**.  `search()` throws away every recipe this machine cannot run before
it scores anything, and `compatible()` explains the refusal field by field
instead of returning a bare False.  An input asset that has gone missing is
named rather than skipped: the recipe is still worth reusing with a new
reference, and the caller has to know which reference it lost.

Three more rules, each of them a thing that went wrong somewhere else:

* **A derivative keeps its parent and not its parent's outputs.**  `derive()`
  copies what is reproducible and never `artifact_ids`: a variation that claims
  its parent's images is a lie the gallery will happily render.
* **Signals are strong or weak, and they are not the same number.**  An
  explicit `rate()` raises a recipe's standing; `note_rejection()` records that
  one *part* was redone and does not sink the rest.  Downloading or reusing a
  file is modelled as nothing at all — §12.4 calls it a weak signal and this
  module would rather have no opinion than a wrong one.
* **A project's taste is not the person's taste.**  A recipe carrying a
  `project_id` contributes no rating to a search that did not name that
  project.  §22 requires it, and the reason is that one heavily-rated campaign
  otherwise becomes the house style of everything the user ever renders again.

Nothing here infers anything about the person.  What is stored is what they
did with a recipe: a rating they gave, a part they had redone.  There is no
taste profile, and adding one is not a small change.

`params` is a free map on purpose (§12.2 lists sampler/steps/CFG/LoRA/ControlNet
for image, fps/duration/motion for video, voice/prosody for audio).  It is
validated for types and not for keys, because the next model will bring a knob
this file has never heard of, and a closed schema would answer that by dropping
it.  The keys each media type is expected to carry are documented on
`GenerationRecipe` so a reader does not have to guess.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from src.contracts.base import (
    ContractError,
    as_mapping,
    one_of,
    reject_unknown,
    text,
    text_list,
    whole,
)
from src.image_model_ids import model_id_leaf

from . import store
from .contracts import ContextCandidate, new_id

logger = logging.getLogger(__name__)

#: What a recipe can have produced.  `document` is here because a generated PDF
#: or slide deck has a template, a model and a seed exactly like a picture does.
MEDIA_TYPES: Tuple[str, ...] = ("image", "video", "audio", "3d", "document")

#: `unreproducible` is not a failure: it is a recipe whose engine, checkpoint or
#: workflow is gone, kept because its prompt and its rating are still worth
#: reading.  It is never returned by `search()`.
RECIPE_STATUSES: Tuple[str, ...] = ("active", "unreproducible", "superseded")

#: The highest `rate()` accepts.  0 means nobody has rated it, which is not the
#: same as a bad rating and is not scored as one.
MAX_RATING = 5

#: How many rejection notes one recipe keeps.  A recipe with thirty is telling
#: you something the thirty-first note will not add.
MAX_ISSUES = 32

#: Ranking weights.  Similarity leads because the caller asked for something;
#: rating is a strong but scarce signal; reproducibility is what separates a
#: recipe from a screenshot; freshness only breaks ties.
WEIGHTS: Mapping[str, float] = {
    "similarity": 0.45, "rating": 0.25, "reproducibility": 0.20, "freshness": 0.10,
}

#: Freshness half-life.  Thirty days: long enough that last month's good render
#: still wins, short enough that a checkpoint upgrade eventually shows.
FRESHNESS_HALFLIFE_S = 30 * 24 * 3600

_SEARCH_SCAN_LIMIT = 2000
_WORD = re.compile(r"[a-z0-9_]+")

SCHEMA: Tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS recipes (
        id TEXT PRIMARY KEY,
        owner TEXT NOT NULL DEFAULT '',
        project_id TEXT NOT NULL DEFAULT '',
        media_type TEXT NOT NULL DEFAULT 'image',
        request TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        model_version TEXT NOT NULL DEFAULT '',
        provider TEXT NOT NULL DEFAULT '',
        prompt TEXT NOT NULL DEFAULT '',
        negative_prompt TEXT NOT NULL DEFAULT '',
        transformations TEXT NOT NULL DEFAULT '[]',
        input_refs TEXT NOT NULL DEFAULT '[]',
        asset_hashes TEXT NOT NULL DEFAULT '[]',
        params TEXT NOT NULL DEFAULT '{}',
        seed TEXT NOT NULL DEFAULT '',
        workflow_ref TEXT NOT NULL DEFAULT '',
        tools TEXT NOT NULL DEFAULT '[]',
        artifact_ids TEXT NOT NULL DEFAULT '[]',
        cost TEXT NOT NULL DEFAULT '{}',
        rating INTEGER NOT NULL DEFAULT 0,
        rejections INTEGER NOT NULL DEFAULT 0,
        issues TEXT NOT NULL DEFAULT '[]',
        license_note TEXT NOT NULL DEFAULT '',
        parent_id TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT ''
    )""",
    "CREATE INDEX IF NOT EXISTS idx_recipes_owner ON recipes(owner, project_id)",
    "CREATE INDEX IF NOT EXISTS idx_recipes_media ON recipes(media_type, status)",
    "CREATE INDEX IF NOT EXISTS idx_recipes_model ON recipes(model)",
    "CREATE INDEX IF NOT EXISTS idx_recipes_parent ON recipes(parent_id)",
)

store.register_schema("recipes", SCHEMA)


def _moment(now: Any = None) -> str:
    if now is None:
        return store.now_iso()
    if isinstance(now, datetime):
        moment = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        return (moment.astimezone(timezone.utc).replace(microsecond=0)
                .isoformat().replace("+00:00", "Z"))
    parsed = store.parse_iso(now)
    return _moment(parsed) if parsed is not None else store.now_iso()


def _free_map(data: Mapping[str, Any], key: str, path: str) -> Dict[str, Any]:
    """A `{name: value}` bag validated for types and not for keys.

    JSON-representable values only, because this round-trips through SQLite as
    JSON and a value that cannot survive that is a value the recipe would
    silently lose.  Keys are free: the next sampler will bring its own."""
    raw = data.get(key, None)
    if raw is None:
        return {}
    bag = as_mapping(raw, f"{path}.{key}")
    out: Dict[str, Any] = {}
    for name, value in bag.items():
        if not isinstance(value, (str, int, float, bool, list, dict)) and value is not None:
            raise ContractError(f"{path}.{key}.{name}",
                                "expected a JSON value (string, number, bool, list, "
                                "object or null)", got=value)
        out[str(name)] = value
    return out


def _tokens(value: str) -> frozenset:
    return frozenset(_WORD.findall(str(value or "").lower()))


def _overlap(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


def _names(value: Any, path: str) -> List[str]:
    """A list of model or workflow names, refusing a bare string.

    `text_list` refuses one for the same reason: `available_models="flux"` read
    as a sequence is four one-character names, every recipe fails its
    compatibility check, and the user is told the machine has nothing
    installed."""
    if isinstance(value, str):
        raise ContractError(path, "expected a list of names; a bare string would be "
                                  "read one character at a time", got=value)
    return [str(v).strip() for v in (value or ()) if str(v).strip()]


def _same_model(wanted: str, available: Iterable[str]) -> bool:
    """Is this recipe's model one of the ones installed?

    Compared on the provider-stripped leaf, reusing `src.image_model_ids`, so
    that a recipe recorded against `flux.1-dev` still matches an endpoint that
    advertises it as `local/flux.1-dev`.  Namespacing an id is a deployment
    detail; refusing a working recipe over it is a bug the user experiences as
    "it forgot"."""
    target = str(wanted or "").strip().lower()
    if not target:
        return False
    leaf = model_id_leaf(target)
    for candidate in available:
        name = str(candidate or "").strip().lower()
        if not name:
            continue
        if name == target or model_id_leaf(name) == leaf:
            return True
    return False


@dataclass(frozen=True)
class GenerationRecipe:
    """Everything needed to make that result again, and nothing about the person.

    `params` carries the knobs the media type actually has (§12.2).  Expected
    keys, documented rather than enforced:

    * **image** — `sampler`, `scheduler`, `steps`, `cfg`, `width`, `height`,
      `aspect_ratio`, `loras` (name → weight), `controlnets`, `preprocessors`,
      `ip_adapter`, `mask`, `denoise`, `upscale`, `face_restore`, `vae`,
      `clip_skip`;
    * **video** — `pipeline`, `duration_s`, `fps`, `width`, `height`,
      `shot_prompts`, `init_image`, `init_video`, `motion`, `camera`,
      `interpolation`, `upscale`, `audio`, `character_seeds`;
    * **audio** — `voice`, `voice_version`, `language`, `style`, `speed`,
      `prosody`, `segmentation`, `postprocess`;
    * **3d / document** — whatever the pipeline names; nothing here refuses a
      key it has not seen.

    `seed` is a string, not an int, and that is on purpose: `media_workflows`
    caps a ComfyUI seed at 2**53-1 precisely because a bigger one stops
    round-tripping through JavaScript's number type, and some providers return
    a seed that was never a number at all.  A seed that cannot be written down
    exactly is a seed that does not reproduce anything.

    `workflow_ref` follows the shape `Provenance` already uses:
    `image.product@1.0.0` or `image.product@1.0.0#<fingerprint>` — id, version,
    and optionally the fingerprint that proves the template on disk had not been
    edited under that name."""

    id: str = ""
    owner: str = ""
    project_id: str = ""
    media_type: str = "image"
    request: str = ""
    model: str = ""
    model_version: str = ""
    provider: str = ""
    prompt: str = ""
    negative_prompt: str = ""
    transformations: Tuple[str, ...] = ()
    input_refs: Tuple[str, ...] = ()
    asset_hashes: Tuple[str, ...] = ()
    params: Mapping[str, Any] = field(default_factory=dict)
    seed: str = ""
    workflow_ref: str = ""
    tools: Tuple[str, ...] = ()
    artifact_ids: Tuple[str, ...] = ()
    cost: Mapping[str, Any] = field(default_factory=dict)
    rating: int = 0
    rejections: int = 0
    issues: Tuple[str, ...] = ()
    license_note: str = ""
    parent_id: str = ""
    status: str = "active"
    created_at: str = ""
    updated_at: str = ""
    _KEYS = ("id", "owner", "project_id", "media_type", "request", "model",
             "model_version", "provider", "prompt", "negative_prompt",
             "transformations", "input_refs", "asset_hashes", "params", "seed",
             "workflow_ref", "tools", "artifact_ids", "cost", "rating",
             "rejections", "issues", "license_note", "parent_id", "status",
             "created_at", "updated_at")

    @classmethod
    def parse(cls, raw: Any, path: str = "recipe") -> "GenerationRecipe":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            id=text(data, "id", path, required=False,
                    default=new_id("recipe"), max_len=128),
            owner=text(data, "owner", path, required=False, max_len=256),
            project_id=text(data, "project_id", path, required=False, max_len=128),
            media_type=one_of(data, "media_type", path, choices=MEDIA_TYPES,
                              required=False, default="image") or "image",
            request=text(data, "request", path, required=False,
                         max_len=8192, allow_blank=True),
            model=text(data, "model", path, max_len=256),
            model_version=text(data, "model_version", path, required=False, max_len=128),
            provider=text(data, "provider", path, required=False, max_len=128),
            prompt=text(data, "prompt", path, required=False,
                        max_len=32768, allow_blank=True),
            negative_prompt=text(data, "negative_prompt", path, required=False,
                                 max_len=32768, allow_blank=True),
            transformations=text_list(data, "transformations", path,
                                      max_items=64, max_len=256),
            input_refs=text_list(data, "input_refs", path, max_items=128, max_len=2048),
            asset_hashes=text_list(data, "asset_hashes", path, max_items=128, max_len=128),
            params=_free_map(data, "params", path),
            seed=text(data, "seed", path, required=False, max_len=128),
            workflow_ref=text(data, "workflow_ref", path, required=False, max_len=512),
            tools=text_list(data, "tools", path, max_items=64, max_len=256),
            artifact_ids=text_list(data, "artifact_ids", path, max_items=200, max_len=64),
            cost=_free_map(data, "cost", path),
            rating=whole(data, "rating", path, default=0,
                         minimum=0, maximum=MAX_RATING) or 0,
            rejections=whole(data, "rejections", path, default=0, minimum=0) or 0,
            issues=text_list(data, "issues", path, max_items=MAX_ISSUES,
                             max_len=512, unique=False),
            license_note=text(data, "license_note", path, required=False, max_len=512),
            parent_id=text(data, "parent_id", path, required=False, max_len=128),
            status=one_of(data, "status", path, choices=RECIPE_STATUSES,
                          required=False, default="active") or "active",
            created_at=text(data, "created_at", path, required=False, max_len=64),
            updated_at=text(data, "updated_at", path, required=False, max_len=64),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "owner": self.owner, "project_id": self.project_id,
            "media_type": self.media_type, "request": self.request,
            "model": self.model, "model_version": self.model_version,
            "provider": self.provider, "prompt": self.prompt,
            "negative_prompt": self.negative_prompt,
            "transformations": list(self.transformations),
            "input_refs": list(self.input_refs),
            "asset_hashes": list(self.asset_hashes),
            "params": dict(self.params), "seed": self.seed,
            "workflow_ref": self.workflow_ref, "tools": list(self.tools),
            "artifact_ids": list(self.artifact_ids), "cost": dict(self.cost),
            "rating": self.rating, "rejections": self.rejections,
            "issues": list(self.issues), "license_note": self.license_note,
            "parent_id": self.parent_id, "status": self.status,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    def workflow_id(self) -> str:
        """`image.product` out of `image.product@1.0.0#abc…`, for matching
        against what the engine has installed."""
        return self.workflow_ref.split("#", 1)[0].split("@", 1)[0].strip()

    def reproducibility(self) -> float:
        """How much of "make that again" this recipe can actually deliver.

        A seed and a pinned model version are what separate a recipe from a
        nice prompt; the workflow and the input hashes are what make the rest of
        it checkable.  Rejections pull it down because a recipe somebody had to
        redo three times did not reproduce anything the first time either."""
        have = [bool(self.seed), bool(self.model_version), bool(self.workflow_ref),
                bool(self.asset_hashes or not self.input_refs), bool(self.params)]
        score = sum(1.0 for v in have if v) / float(len(have))
        return max(0.0, score - 0.1 * min(3, int(self.rejections)))

    def compact(self) -> str:
        """§12.3: the model gets a recipe and references, never the ComfyUI log."""
        lines = [f"{self.media_type} via {self.model}"
                 + (f" {self.model_version}" if self.model_version else "")
                 + (f" ({self.provider})" if self.provider else "")]
        if self.workflow_ref:
            lines.append(f"workflow: {self.workflow_ref}")
        if self.prompt:
            lines.append(f"prompt: {self.prompt}")
        if self.negative_prompt:
            lines.append(f"negative: {self.negative_prompt}")
        if self.seed:
            lines.append(f"seed: {self.seed}")
        if self.params:
            lines.append("params: " + ", ".join(
                f"{k}={self.params[k]}" for k in sorted(self.params)))
        if self.input_refs:
            lines.append("inputs: " + ", ".join(self.input_refs))
        if self.issues:
            lines.append("known issues: " + "; ".join(self.issues))
        if self.license_note:
            lines.append(f"licence: {self.license_note}")
        return "\n".join(lines)


def _from_row(row: Mapping[str, Any]) -> GenerationRecipe:
    """A stored row → a recipe, without re-validating it.  A recipe already on
    disk comes back even if a later rule would refuse it: the alternative is a
    schema change that quietly eats the user's best render."""
    return GenerationRecipe(
        id=str(row.get("id") or ""),
        owner=str(row.get("owner") or ""),
        project_id=str(row.get("project_id") or ""),
        media_type=str(row.get("media_type") or "image"),
        request=str(row.get("request") or ""),
        model=str(row.get("model") or ""),
        model_version=str(row.get("model_version") or ""),
        provider=str(row.get("provider") or ""),
        prompt=str(row.get("prompt") or ""),
        negative_prompt=str(row.get("negative_prompt") or ""),
        transformations=tuple(str(v) for v in store.loads_list(row.get("transformations"))),
        input_refs=tuple(str(v) for v in store.loads_list(row.get("input_refs"))),
        asset_hashes=tuple(str(v) for v in store.loads_list(row.get("asset_hashes"))),
        params=store.loads_dict(row.get("params")),
        seed=str(row.get("seed") or ""),
        workflow_ref=str(row.get("workflow_ref") or ""),
        tools=tuple(str(v) for v in store.loads_list(row.get("tools"))),
        artifact_ids=tuple(str(v) for v in store.loads_list(row.get("artifact_ids"))),
        cost=store.loads_dict(row.get("cost")),
        rating=int(row.get("rating") or 0),
        rejections=int(row.get("rejections") or 0),
        issues=tuple(str(v) for v in store.loads_list(row.get("issues"))),
        license_note=str(row.get("license_note") or ""),
        parent_id=str(row.get("parent_id") or ""),
        status=str(row.get("status") or "active"),
        created_at=str(row.get("created_at") or ""),
        updated_at=str(row.get("updated_at") or ""),
    )


_COLUMNS = ("id", "owner", "project_id", "media_type", "request", "model",
            "model_version", "provider", "prompt", "negative_prompt",
            "transformations", "input_refs", "asset_hashes", "params", "seed",
            "workflow_ref", "tools", "artifact_ids", "cost", "rating",
            "rejections", "issues", "license_note", "parent_id", "status",
            "created_at", "updated_at")

_INSERT = ("INSERT INTO recipes (" + ", ".join(_COLUMNS) + ") VALUES ("
           + ",".join("?" for _ in _COLUMNS) + ")")

_JSON_COLUMNS = frozenset({"transformations", "input_refs", "asset_hashes",
                           "params", "tools", "artifact_ids", "cost", "issues"})


def _insert_params(recipe: GenerationRecipe) -> Tuple[Any, ...]:
    data = recipe.to_dict()
    return tuple(store.dumps(data[name]) if name in _JSON_COLUMNS else data[name]
                 for name in _COLUMNS)


def _replace(recipe: GenerationRecipe, **changes: Any) -> GenerationRecipe:
    """A new frozen recipe with a few fields changed, keeping the tuple/mapping
    types `to_dict()` flattens into lists."""
    data = recipe.to_dict()
    data.update({
        "transformations": recipe.transformations, "input_refs": recipe.input_refs,
        "asset_hashes": recipe.asset_hashes, "tools": recipe.tools,
        "artifact_ids": recipe.artifact_ids, "issues": recipe.issues,
        "params": dict(recipe.params), "cost": dict(recipe.cost),
    })
    data.update(changes)
    return GenerationRecipe(**data)


# ── writing ────────────────────────────────────────────────────────────────

def record(**fields: Any) -> GenerationRecipe:
    """Write down how something was made.

    Raises `ContractError` on a malformed payload and writes nothing; lets a
    `ContextStoreError` propagate, because a caller that believes the recipe of
    a render it just liked was saved needs to know when it was not."""
    recipe = GenerationRecipe.parse(fields)
    moment = store.now_iso()
    recipe = _replace(recipe,
                      created_at=recipe.created_at or moment,
                      updated_at=recipe.updated_at or recipe.created_at or moment)
    with store.db() as conn:
        conn.execute(_INSERT, _insert_params(recipe))
    logger.debug("recipe %s recorded (%s via %s)", recipe.id, recipe.media_type,
                 recipe.model)
    return recipe


def get(recipe_id: str) -> Optional[GenerationRecipe]:
    if not str(recipe_id or "").strip():
        return None
    try:
        with store.db() as conn:
            row = conn.execute("SELECT * FROM recipes WHERE id = ?",
                               (str(recipe_id),)).fetchone()
    except store.ContextStoreError:
        logger.warning("recipe store unavailable for get(%s)", recipe_id)
        return None
    return _from_row(dict(row)) if row else None


def rate(recipe_id: str, *, rating: int, note: str = "") -> Optional[GenerationRecipe]:
    """The strong signal: somebody said this one was good (or was not).

    Explicit and scarce, which is why it moves the ranking at all.  `note` is
    logged rather than stored — a rating is a number the ranker uses, and a
    sentence attached to it would be a review nothing reads."""
    if isinstance(rating, bool) or not isinstance(rating, int):
        raise ContractError("rate.rating", "expected a whole number", got=rating)
    if rating < 0 or rating > MAX_RATING:
        raise ContractError("rate.rating", f"must be between 0 and {MAX_RATING}",
                            got=rating)
    recipe = get(recipe_id)
    if recipe is None:
        return None
    moment = store.now_iso()
    with store.db() as conn:
        conn.execute("UPDATE recipes SET rating = ?, updated_at = ? WHERE id = ?",
                     (int(rating), moment, recipe.id))
    logger.info("recipe %s rated %d (%s)", recipe.id, rating, note or "no note")
    return _replace(recipe, rating=int(rating), updated_at=moment)


def note_rejection(recipe_id: str, *, part: str,
                   note: str = "") -> Optional[GenerationRecipe]:
    """The weak, LOCAL signal: one part of the result had to be redone.

    §12.4 — "rehacer inmediatamente una parte es señal negativa localizada".
    So the part is recorded by name in `issues` and the counter goes up; the
    rating is not touched.  A recipe whose hands need fixing is still the right
    recipe for that lighting, and burying it would cost the user the whole
    result to fix a corner of it."""
    label = str(part or "").strip()
    if not label:
        raise ContractError("note_rejection.part",
                            "is required — a rejection with no part is a bad "
                            "rating wearing a disguise, and this is not that")
    recipe = get(recipe_id)
    if recipe is None:
        return None
    entry = f"{label}: {note.strip()}" if str(note or "").strip() else label
    issues = tuple([*recipe.issues, entry][-MAX_ISSUES:])
    moment = store.now_iso()
    with store.db() as conn:
        conn.execute(
            "UPDATE recipes SET issues = ?, rejections = ?, updated_at = ? WHERE id = ?",
            (store.dumps(list(issues)), int(recipe.rejections) + 1, moment, recipe.id))
    logger.info("recipe %s rejection on %r (%d total)", recipe.id, label,
                recipe.rejections + 1)
    return _replace(recipe, issues=issues, rejections=recipe.rejections + 1,
                    updated_at=moment)


def derive(parent_id: str, **overrides: Any) -> GenerationRecipe:
    """A variation that remembers where it came from.

    Copies only what is reproducible — model, prompt, params, seed, workflow,
    tools, licence.  Never `artifact_ids`: those are the parent's outputs, and a
    derivative that claims them is a record the gallery will render as a lie.
    Never `rating`, `rejections` or `issues` either: a derivative has not earned
    its parent's reception, and inheriting a rating is how one liked render
    becomes a family of unrated ones that all look popular.

    Overriding `input_refs` without also giving `asset_hashes` clears the
    hashes.  A recipe pointing at new references while carrying the old
    references' hashes is worse than one with no hashes at all: it would pass a
    reproducibility check it should fail."""
    parent = get(parent_id)
    if parent is None:
        raise ContractError("derive.parent_id", "names no recipe", got=parent_id)
    inherited = parent.to_dict()
    for name in ("id", "artifact_ids", "rating", "rejections", "issues",
                 "created_at", "updated_at", "parent_id", "status"):
        inherited.pop(name, None)
    if "input_refs" in overrides and "asset_hashes" not in overrides:
        inherited["asset_hashes"] = []
        logger.debug("derive(%s): input_refs replaced, parent asset hashes dropped",
                     parent_id)
    for name in ("artifact_ids", "rating", "rejections", "issues"):
        if name in overrides:
            raise ContractError(
                f"derive.{name}",
                "belongs to a result that has not happened yet — a derivative "
                "records how it will be made, and earns its reception afterwards",
                got=overrides[name],
            )
    payload = {**inherited, **overrides, "parent_id": parent.id}
    child = GenerationRecipe.parse(payload)
    moment = store.now_iso()
    child = _replace(child, created_at=moment, updated_at=moment)
    with store.db() as conn:
        conn.execute(_INSERT, _insert_params(child))
    logger.debug("recipe %s derived from %s", child.id, parent.id)
    return child


# ── compatibility, which happens before anything is ranked ────────────────

def compatible(recipe: GenerationRecipe, *,
               available_models: Sequence[str] = (),
               available_workflows: Sequence[str] = (),
               asset_exists: Optional[Callable[[str], bool]] = None) -> Dict[str, Any]:
    """Can this machine run it, and will it come out the same?

    Two different questions, answered separately on purpose:

    * `ok` is "can it run at all" — the status, the model and the workflow.  A
      recipe that fails here is not offered, because a recipe whose sampler the
      installed engine has never heard of is worse than no recipe: it renders
      something plausible and nobody notices it is not what was asked for.
    * `degraded` is "will it reproduce" — a missing input asset, a missing seed,
      a model whose version was never pinned.  Those do not stop a render, so
      they do not remove the recipe; they are named in `missing` and travel with
      it, because §12.3 wants the gap visible rather than tidied away.

    An empty `available_models` or `available_workflows` means "the caller could
    not tell us", never "nothing is installed": absence of evidence does not get
    to refuse a recipe.  `asset_exists=None` means the same about assets."""
    missing: List[Dict[str, str]] = []
    ok = True
    degraded = False

    if recipe.status != "active":
        ok = False
        missing.append({"what": "status",
                        "detail": f"recipe is marked {recipe.status!r}"})

    models = _names(available_models, "compatible.available_models")
    if models and not _same_model(recipe.model, models):
        ok = False
        missing.append({"what": "model",
                        "detail": f"{recipe.model or '(unnamed)'} is not among the "
                                  f"{len(models)} available model(s)"})

    workflows = _names(available_workflows, "compatible.available_workflows")
    wanted_workflow = recipe.workflow_id()
    if workflows and wanted_workflow:
        known = {w.split("#", 1)[0].split("@", 1)[0].strip() for w in workflows}
        if wanted_workflow not in known and recipe.workflow_ref not in workflows:
            ok = False
            missing.append({"what": "workflow",
                            "detail": f"{recipe.workflow_ref} is not installed"})

    if asset_exists is not None:
        refs = list(recipe.input_refs) or list(recipe.asset_hashes)
        for ref in refs:
            try:
                present = bool(asset_exists(ref))
            except Exception:  # a resolver that throws is a resolver that failed
                logger.debug("asset resolver failed for %s", ref, exc_info=True)
                present = False
            if not present:
                degraded = True
                missing.append({"what": "asset", "detail": ref})

    if not recipe.seed:
        degraded = True
        missing.append({"what": "seed",
                        "detail": "no seed was recorded; the same prompt will not "
                                  "give the same result"})
    if not recipe.model_version:
        degraded = True
        missing.append({"what": "model_version",
                        "detail": f"{recipe.model or 'the model'} was never pinned to "
                                  "a version; a checkpoint upgrade changes the output "
                                  "silently"})
    return {"ok": ok, "missing": missing, "degraded": degraded}


def search(query: str = "", *, owner: str = "", project_id: str = "",
           media_type: str = "", available_models: Sequence[str] = (),
           k: int = 5, now: Any = None) -> List[Dict[str, Any]]:
    """Recipes worth reusing, hard-filtered first and ranked second.

    Order matters and is the whole point of §12.3.  Media type, status and
    model availability decide what is even a candidate; only the survivors are
    scored on similarity, the user's rating, reproducibility and freshness.

    Assets are not checked here — `search()` has no way to resolve one.  A
    caller that can should pass its resolver to `compatible()` on the hits it
    keeps; every hit already carries the `compatible` verdict computed with what
    was available, so the gap is visible rather than assumed away.

    Rule that §22 pins: a recipe carrying a `project_id` contributes **no
    rating** to a search that did not name that project.  It can still be
    returned — it is the same owner's work — but one campaign's taste does not
    become the ranking of everything else the person ever renders."""
    models = _names(available_models, "search.available_models")
    where: List[str] = ["status = 'active'"]
    params: List[Any] = []
    if owner:
        clause, owner_params = store.scope_clause(str(owner), str(project_id))
        where.append(clause)
        params.extend(owner_params)
    elif project_id:
        where.append("(project_id = ? OR project_id = '')")
        params.append(str(project_id))
    if media_type:
        if media_type not in MEDIA_TYPES:
            return []
        where.append("media_type = ?")
        params.append(str(media_type))
    sql = ("SELECT * FROM recipes WHERE " + " AND ".join(where)
           + " ORDER BY updated_at DESC, id DESC LIMIT ?")
    params.append(_SEARCH_SCAN_LIMIT)
    try:
        with store.db() as conn:
            rows = store.rows(conn.execute(sql, params))
    except store.ContextStoreError:
        logger.warning("recipe store unavailable for search(%r)", query)
        return []

    moment = store.parse_iso(_moment(now)) or datetime.now(timezone.utc)
    wanted = _tokens(query)
    hits: List[Dict[str, Any]] = []
    for row in rows:
        recipe = _from_row(row)
        verdict = compatible(recipe, available_models=models)
        if not verdict["ok"]:
            continue
        haystack = _tokens(" ".join((recipe.request, recipe.prompt,
                                     " ".join(recipe.transformations),
                                     recipe.media_type)))
        similarity = _overlap(wanted, haystack) if wanted else 0.0
        counts_here = (not recipe.project_id) or recipe.project_id == str(project_id or "")
        rating = (recipe.rating / float(MAX_RATING)) if counts_here else 0.0
        age = store.age_seconds(recipe.updated_at or recipe.created_at, now=moment)
        freshness = 0.5 if age is None else math.exp(-age / FRESHNESS_HALFLIFE_S)
        signals = {
            "similarity": round(similarity, 6),
            "rating": round(rating, 6),
            "reproducibility": round(recipe.reproducibility(), 6),
            "freshness": round(freshness, 6),
        }
        score = sum(WEIGHTS[name] * value for name, value in signals.items())
        hits.append({"recipe": recipe, "score": round(score, 6),
                     "signals": signals, "compatible": verdict})
    # Score first, then the most recently touched, then the id: a stable order
    # matters more here than it looks, because two identical recipes flipping
    # places between calls reads to a user as the system changing its mind.
    hits.sort(key=lambda h: (h["score"], h["recipe"].updated_at, h["recipe"].id),
              reverse=True)
    return hits[:max(1, int(k or 1))]


def as_candidates(hits: Iterable[Any]) -> List[ContextCandidate]:
    """Search hits → what the compiler ranks.

    The body is `GenerationRecipe.compact()` and never the engine log: §12.3 is
    explicit that the model gets a recipe plus references, and a ComfyUI history
    dump would spend the whole `multimodal_recipes` budget proving it.

    A hit whose `compatible` verdict is degraded arrives with `degraded=True`
    and the reasons in `meta["missing"]`, so a packet that offered a recipe with
    a vanished reference says so instead of letting the model discover it."""
    out: List[ContextCandidate] = []
    for hit in hits:
        if isinstance(hit, GenerationRecipe):
            recipe, score, signals = hit, 0.0, {}
            verdict: Dict[str, Any] = {"ok": True, "missing": [], "degraded": False}
        elif isinstance(hit, Mapping) and isinstance(hit.get("recipe"), GenerationRecipe):
            recipe = hit["recipe"]
            score = float(hit.get("score") or 0.0)
            signals = dict(hit.get("signals") or {})
            verdict = dict(hit.get("compatible") or
                           {"ok": True, "missing": [], "degraded": False})
        else:
            continue
        title = recipe.request or recipe.prompt or f"{recipe.media_type} recipe"
        out.append(ContextCandidate.parse({
            "source_type": "recipe",
            "source_ref": f"recipe:{recipe.id}",
            "title": title[:512],
            "body": recipe.compact(),
            "section": "multimodal_recipes",
            "lanes": ["lexical"],
            "scores": {**{str(k): float(v) for k, v in signals.items()},
                       "total": round(score, 6)},
            # A recipe is a record of a render that actually happened, not an
            # assertion about one -- but its authority stays low until somebody
            # kept the result, which is what `artifact_ids` proves.
            "trust_class": "observed",
            "authority": "proved_result" if recipe.artifact_ids else "agent_claim",
            "observed_at": recipe.updated_at or recipe.created_at,
            "owner": recipe.owner,
            "project_id": recipe.project_id,
            "degraded": bool(verdict.get("degraded")),
            "meta": {
                "recipe_id": recipe.id, "media_type": recipe.media_type,
                "model": recipe.model, "model_version": recipe.model_version,
                "provider": recipe.provider, "seed": recipe.seed,
                "workflow_ref": recipe.workflow_ref,
                "params": dict(recipe.params),
                "input_refs": list(recipe.input_refs),
                "artifact_ids": list(recipe.artifact_ids),
                "parent_id": recipe.parent_id,
                "rating": recipe.rating, "rejections": recipe.rejections,
                "issues": list(recipe.issues),
                "license_note": recipe.license_note,
                "missing": list(verdict.get("missing") or []),
                "reproducibility": round(recipe.reproducibility(), 6),
            },
        }))
    return out


def stats(*, owner: str = "", project_id: str = "") -> Dict[str, Any]:
    """What the recipe book holds.  Diagnostics only; never raises."""
    out: Dict[str, Any] = {
        "owner": owner, "project_id": project_id, "total": 0,
        "by_media_type": {}, "by_status": {}, "rated": 0, "with_seed": 0,
        "derived": 0, "rejections": 0, "models": [],
    }
    where: List[str] = []
    params: List[Any] = []
    if owner:
        clause, owner_params = store.scope_clause(str(owner), str(project_id))
        where.append(clause)
        params.extend(owner_params)
    elif project_id:
        where.append("(project_id = ? OR project_id = '')")
        params.append(str(project_id))
    sql = "SELECT * FROM recipes"
    if where:
        sql += " WHERE " + " AND ".join(where)
    try:
        with store.db() as conn:
            rows = store.rows(conn.execute(sql, params))
    except store.ContextStoreError:
        logger.warning("recipe store unavailable for stats(owner=%s)", owner)
        return out
    models: Dict[str, int] = {}
    for row in rows:
        recipe = _from_row(row)
        out["total"] += 1
        out["by_media_type"][recipe.media_type] = \
            out["by_media_type"].get(recipe.media_type, 0) + 1
        out["by_status"][recipe.status] = out["by_status"].get(recipe.status, 0) + 1
        if recipe.rating:
            out["rated"] += 1
        if recipe.seed:
            out["with_seed"] += 1
        if recipe.parent_id:
            out["derived"] += 1
        out["rejections"] += int(recipe.rejections)
        if recipe.model:
            models[recipe.model] = models.get(recipe.model, 0) + 1
    out["models"] = sorted(models)
    return out


__all__ = [
    "MEDIA_TYPES", "RECIPE_STATUSES", "MAX_RATING", "MAX_ISSUES", "WEIGHTS",
    "FRESHNESS_HALFLIFE_S", "SCHEMA",
    "GenerationRecipe",
    "record", "get", "search", "compatible", "as_candidates", "rate",
    "note_rejection", "derive", "stats",
]
