"""Parameters as a contract, not a universal form — WP07 (MOD-06).

04_MODELOS_Y_RECURSOS.md, "Parámetros como contrato, no como formulario
universal": `negative_prompt`, CFG, seed, duration, fps... are not
interchangeable between engines, and a control can exist on a model without
being exposed by a given provider. This module is the single typed schema
per `(engine, task)` pair that a form, an API body and an agent tool all
validate against — "no hay tres listas de defaults divergentes."

Each `ParamField` carries: type, range/enum, default, unit, whether it
affects cost or time, whether it is deterministic (MOD-06's "obligatoriedad
y condiciones"), and an optional `depends_on` — a condition on ANOTHER field
in the same schema (e.g. `cfg` only applies when `guidance` is on; `strength`
requires an `image` to have been given at all). `validate()` rejects an
impossible combination before anything is submitted (WP07's closing
criterion), rather than sending it downstream and hoping the engine errors
usefully.

`describe_for_ui()` returns the same schema as plain, JSON-serializable
data — the mechanism by which Studio paints controls per (engine, task)
without a universal form: it walks the field list and renders whatever each
field says it is, never a fixed set of inputs.

Purely a schema/validation layer: no network, no model load, no dependency
on `src/creator/capabilities.py` (a param schema is a property of
`(engine, task)`; whether that combination is actually installed and
evidenced is `capabilities.py`'s question, asked separately, typically
before a route accepts a request that would use this schema).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from src import model_capabilities as mc

# ── field types ───────────────────────────────────────────────────────────

TYPE_NUMBER = "number"
TYPE_INTEGER = "integer"
TYPE_STRING = "string"
TYPE_BOOLEAN = "boolean"
TYPE_ENUM = "enum"
TYPE_ASSET_REF = "asset_ref"  # an image/audio/video/mask reference (artifact or upload id)

TYPES = frozenset({TYPE_NUMBER, TYPE_INTEGER, TYPE_STRING, TYPE_BOOLEAN, TYPE_ENUM, TYPE_ASSET_REF})

_MISSING = object()

# ── error codes returned by validate() ───────────────────────────────────

ERR_UNKNOWN_ENGINE_TASK = "unknown_engine_task"
ERR_MISSING_REQUIRED = "missing_required"
ERR_UNKNOWN_PARAM = "unknown_param"
ERR_WRONG_TYPE = "wrong_type"
ERR_OUT_OF_RANGE = "out_of_range"
ERR_NOT_IN_ENUM = "not_in_enum"
ERR_DEPENDENCY_UNMET = "dependency_unmet"


@dataclass(frozen=True)
class ParamDependency:
    """A condition on another field of the SAME schema. `param` names that
    field; the dependency is met when:

    * `equals` is set: `params[param] == equals` (e.g. `cfg` needs
      `guidance == True`);
    * `equals` is `None` (default) and `require_presence` is `True`: `param`
      was given at all, any truthy-or-not value (e.g. `strength` needs an
      `image` to have been supplied — not any particular image);
    * `equals` is `None` and `require_presence` is `False`: `param` is
      simply truthy (falls back to this when neither of the above is set).
    """

    param: str
    equals: Any = None
    require_presence: bool = False

    def is_met(self, normalized_so_far: Mapping[str, Any], raw_params: Mapping[str, Any]) -> bool:
        if self.require_presence:
            return self.param in raw_params
        if self.equals is not None or self.equals is False:
            return normalized_so_far.get(self.param, raw_params.get(self.param)) == self.equals
        return bool(normalized_so_far.get(self.param, raw_params.get(self.param)))

    def describe(self) -> str:
        if self.require_presence:
            return f"requires '{self.param}' to be provided"
        if self.equals is not None or self.equals is False:
            return f"requires '{self.param}' == {self.equals!r}"
        return f"requires '{self.param}' to be set"

    def to_dict(self) -> Dict[str, Any]:
        return {"param": self.param, "equals": self.equals, "require_presence": self.require_presence}


@dataclass(frozen=True)
class ParamField:
    """One control's full contract. `deterministic=False` marks a field
    whose value does not guarantee the same output twice even when held
    constant (e.g. a provider's own internal sampler noise) — MOD-20's
    "misma seed se describe como intento reproducible bajo condiciones, no
    garantía bit a bit universal"; the UI/receipt layer uses this to avoid
    promising reproducibility a field cannot back up."""

    key: str
    label: str
    type: str
    unit: str = ""
    default: Any = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    enum: Tuple[Any, ...] = ()
    required: bool = False
    deterministic: bool = True
    affects_cost: bool = False
    affects_time: bool = False
    depends_on: Optional[ParamDependency] = None
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "type": self.type,
            "unit": self.unit,
            "default": self.default,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "enum": list(self.enum),
            "required": self.required,
            "deterministic": self.deterministic,
            "affects_cost": self.affects_cost,
            "affects_time": self.affects_time,
            "depends_on": self.depends_on.to_dict() if self.depends_on else None,
            "description": self.description,
        }


@dataclass(frozen=True)
class ParamSchema:
    """The full contract for one `(engine, task)` pair."""

    engine: str
    task: str
    fields: Tuple[ParamField, ...] = ()

    def field_by_key(self, key: str) -> Optional[ParamField]:
        for f in self.fields:
            if f.key == key:
                return f
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "engine": self.engine,
            "task": self.task,
            "fields": [f.to_dict() for f in self.fields],
        }


@dataclass(frozen=True)
class ParamError:
    field: str
    code: str
    message: str

    def to_dict(self) -> Dict[str, Any]:
        return {"field": self.field, "code": self.code, "message": self.message}


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    errors: Tuple[ParamError, ...] = ()
    normalized: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": [e.to_dict() for e in self.errors],
            "normalized": dict(self.normalized),
        }


def _registry_key(engine: str, task: str) -> Tuple[str, str]:
    return str(engine or "").strip().lower(), str(task or "").strip().lower()


# ── seed registry: concrete (engine, task) schemas ───────────────────────
#
# Grounded in 04_MODELOS_Y_RECURSOS.md's own worked examples (ACE-Step
# variants, Chatterbox languages, InvokeAI-style region edit/control-net,
# Wan video) — not exhaustive, but each entry demonstrates one of the
# dependency shapes MOD-06 asks for: `cfg` needs `guidance=True`; `strength`
# needs `image` present at all; `mask` and `image` both required together
# for a region edit; an enum with a real, bounded vocabulary for language.
# Additional (engine, task) pairs register the same way — this dict is the
# whole extension point, nothing else needs to change to add one.

_REGISTRY: Dict[Tuple[str, str], ParamSchema] = {}


def register_schema(schema: ParamSchema) -> None:
    """Add or replace one `(engine, task)` schema. Exposed so a future WP
    (or a test) can register an engine-specific schema without editing this
    module's seed data in place."""
    _REGISTRY[_registry_key(schema.engine, schema.task)] = schema


def get_schema(engine: str, task: str) -> Optional[ParamSchema]:
    return _REGISTRY.get(_registry_key(engine, task))


def list_schemas() -> Tuple[ParamSchema, ...]:
    return tuple(_REGISTRY.values())


register_schema(ParamSchema(
    engine="ace_step",
    task=mc.TASK_MUSIC_GENERATE,
    fields=(
        ParamField(key="duration_s", label="Duración", type=TYPE_NUMBER, unit="s",
                   default=60, minimum=10, maximum=240, affects_time=True, affects_cost=True,
                   description="Longitud del clip generado."),
        ParamField(key="lyrics", label="Letra", type=TYPE_STRING, required=False,
                   description="Letra opcional; vacío genera instrumental."),
        ParamField(key="guidance", label="Guiado por CFG", type=TYPE_BOOLEAN, default=True,
                   description="Si el motor aplica classifier-free guidance."),
        ParamField(key="cfg", label="CFG scale", type=TYPE_NUMBER, default=7.5, minimum=1, maximum=20,
                   depends_on=ParamDependency(param="guidance", equals=True),
                   description="Sólo aplicable cuando 'guidance' está activo (variantes turbo pueden no soportarlo)."),
        ParamField(key="seed", label="Seed", type=TYPE_INTEGER, minimum=0,
                   description="Intento reproducible bajo las mismas condiciones; no garantía bit a bit."),
    ),
))

register_schema(ParamSchema(
    engine="chatterbox",
    task=mc.TASK_AUDIO_SYNTHESIZE,
    fields=(
        ParamField(key="text", label="Texto", type=TYPE_STRING, required=True),
        ParamField(key="language", label="Idioma", type=TYPE_ENUM, required=True,
                   enum=("en", "es", "fr", "de", "it", "pt"),
                   description="Chatterbox Turbo/Nano son centrados en inglés; validar variante instalada aparte."),
        ParamField(key="voice_ref", label="Referencia de voz", type=TYPE_ASSET_REF, required=False),
        ParamField(key="exaggeration", label="Exageración", type=TYPE_NUMBER, default=0.5, minimum=0, maximum=1),
        ParamField(key="cfg_weight", label="Peso CFG", type=TYPE_NUMBER, default=0.5, minimum=0, maximum=1),
    ),
))

register_schema(ParamSchema(
    engine="invoke",
    task=mc.TASK_IMAGE_EDIT,
    fields=(
        ParamField(key="image", label="Imagen base", type=TYPE_ASSET_REF, required=True),
        ParamField(key="prompt", label="Prompt", type=TYPE_STRING, required=True),
        ParamField(key="negative_prompt", label="Prompt negativo", type=TYPE_STRING, required=False),
        ParamField(key="strength", label="Fuerza", type=TYPE_NUMBER, default=0.6, minimum=0, maximum=1,
                   depends_on=ParamDependency(param="image", require_presence=True),
                   description="Requiere imagen base (MOD-06: 'strength necesita imagen')."),
    ),
))

register_schema(ParamSchema(
    engine="invoke",
    task=mc.TASK_IMAGE_INPAINT,
    fields=(
        ParamField(key="image", label="Imagen base", type=TYPE_ASSET_REF, required=True),
        ParamField(key="mask", label="Máscara", type=TYPE_ASSET_REF, required=True,
                   depends_on=ParamDependency(param="image", require_presence=True),
                   description="Un control regional exige máscara y dimensiones concordantes."),
        ParamField(key="prompt", label="Prompt", type=TYPE_STRING, required=True),
    ),
))

register_schema(ParamSchema(
    engine="invoke",
    task=mc.TASK_IMAGE_CONTROLNET,
    fields=(
        ParamField(key="image", label="Imagen base", type=TYPE_ASSET_REF, required=True),
        ParamField(key="control_type", label="Tipo de control", type=TYPE_ENUM, required=True,
                   enum=("canny", "depth", "pose", "lineart")),
        ParamField(key="conditioning_scale", label="Escala de conditioning", type=TYPE_NUMBER,
                   default=1.0, minimum=0, maximum=2,
                   depends_on=ParamDependency(param="control_type", require_presence=True)),
        ParamField(key="prompt", label="Prompt", type=TYPE_STRING, required=True),
    ),
))

register_schema(ParamSchema(
    engine="realesrgan",
    task=mc.TASK_IMAGE_UPSCALE,
    fields=(
        ParamField(key="image", label="Imagen", type=TYPE_ASSET_REF, required=True),
        ParamField(key="scale", label="Factor de escala", type=TYPE_ENUM, default=2, enum=(2, 4)),
        ParamField(key="face_enhance", label="Mejora de rostro", type=TYPE_BOOLEAN, default=False,
                   affects_time=True),
    ),
))

register_schema(ParamSchema(
    engine="wan",
    task=mc.TASK_VIDEO_GENERATE,
    fields=(
        ParamField(key="prompt", label="Prompt", type=TYPE_STRING, required=True),
        ParamField(key="reference_image", label="Imagen de referencia", type=TYPE_ASSET_REF, required=False,
                   description="Presente => pipeline I2V; ausente => T2V (no son el mismo componente)."),
        ParamField(key="duration_s", label="Duración", type=TYPE_NUMBER, unit="s", default=4, minimum=1,
                   maximum=10, affects_time=True, affects_cost=True),
        ParamField(key="fps", label="FPS", type=TYPE_ENUM, default=24, enum=(16, 24, 30)),
        ParamField(key="seed", label="Seed", type=TYPE_INTEGER, minimum=0),
    ),
))


# ── validation ─────────────────────────────────────────────────────────────

def _check_type(f: ParamField, value: Any) -> Optional[str]:
    if f.type == TYPE_NUMBER:
        return None if isinstance(value, (int, float)) and not isinstance(value, bool) else ERR_WRONG_TYPE
    if f.type == TYPE_INTEGER:
        return None if isinstance(value, int) and not isinstance(value, bool) else ERR_WRONG_TYPE
    if f.type == TYPE_BOOLEAN:
        return None if isinstance(value, bool) else ERR_WRONG_TYPE
    if f.type in (TYPE_STRING, TYPE_ASSET_REF):
        return None if isinstance(value, str) else ERR_WRONG_TYPE
    if f.type == TYPE_ENUM:
        return None  # range/enum membership checked separately (value may be non-str, e.g. scale=2)
    return None


def validate(engine: str, task: str, params: Mapping[str, Any] | None = None) -> ValidationResult:
    """Validate `params` against the `(engine, task)` schema. Never partial:
    every field the schema defines is checked, and every problem found is
    reported (not just the first) so a form or a tool call gets the whole
    picture in one round trip, per MOD-06's acceptance ("el mismo schema
    valida formulario, API y tool"). `ok` is False on ANY error, including an
    unmet dependency — WP07's closing criterion that an impossible
    combination is rejected before submit, not sent downstream and hoped
    for."""
    schema = get_schema(engine, task)
    raw = dict(params or {})
    if schema is None:
        return ValidationResult(
            ok=False,
            errors=(ParamError(field="_schema", code=ERR_UNKNOWN_ENGINE_TASK,
                                message=f"no ParamSchema registered for engine={engine!r} task={task!r}"),),
            normalized={},
        )

    known_keys = {f.key for f in schema.fields}
    errors: List[ParamError] = []
    for key in raw:
        if key not in known_keys:
            errors.append(ParamError(field=key, code=ERR_UNKNOWN_PARAM,
                                      message=f"'{key}' is not a parameter of {engine}/{task}"))

    normalized: Dict[str, Any] = {}
    for f in schema.fields:
        present = f.key in raw
        value = raw.get(f.key, _MISSING)

        if not present:
            if f.required:
                errors.append(ParamError(field=f.key, code=ERR_MISSING_REQUIRED,
                                          message=f"'{f.key}' is required"))
                continue
            if f.default is not None:
                normalized[f.key] = f.default
            continue

        type_err = _check_type(f, value)
        if type_err:
            errors.append(ParamError(field=f.key, code=type_err,
                                      message=f"'{f.key}' must be of type {f.type}"))
            continue

        if f.type == TYPE_ENUM and f.enum and value not in f.enum:
            errors.append(ParamError(field=f.key, code=ERR_NOT_IN_ENUM,
                                      message=f"'{f.key}' must be one of {list(f.enum)}"))
            continue

        if f.type in (TYPE_NUMBER, TYPE_INTEGER):
            if f.minimum is not None and value < f.minimum:
                errors.append(ParamError(field=f.key, code=ERR_OUT_OF_RANGE,
                                          message=f"'{f.key}' must be >= {f.minimum}"))
                continue
            if f.maximum is not None and value > f.maximum:
                errors.append(ParamError(field=f.key, code=ERR_OUT_OF_RANGE,
                                          message=f"'{f.key}' must be <= {f.maximum}"))
                continue

        normalized[f.key] = value

    # Dependencies are checked once every present/defaulted value is known,
    # so `cfg` depending on `guidance`'s (possibly defaulted) value reads the
    # default too — not just an explicitly-supplied one.
    for f in schema.fields:
        if f.key not in raw or f.depends_on is None:
            continue
        if not f.depends_on.is_met(normalized, raw):
            errors.append(ParamError(
                field=f.key, code=ERR_DEPENDENCY_UNMET,
                message=f"'{f.key}' {f.depends_on.describe()}",
            ))
            normalized.pop(f.key, None)

    return ValidationResult(ok=not errors, errors=tuple(errors), normalized=normalized)


def describe_for_ui(engine: str, task: str) -> Optional[Dict[str, Any]]:
    """The same schema `validate()` checks against, as plain JSON-
    serializable data — MOD-06's "describe controles visibles ... para que
    el Studio pinte controles sin un formulario universal". `None` when no
    schema is registered for this `(engine, task)`, distinct from an empty
    field list (a real schema with zero fields) — a caller (the route) turns
    `None` into 404, not an empty form."""
    schema = get_schema(engine, task)
    return schema.to_dict() if schema else None


__all__ = [
    "TYPE_NUMBER", "TYPE_INTEGER", "TYPE_STRING", "TYPE_BOOLEAN", "TYPE_ENUM", "TYPE_ASSET_REF", "TYPES",
    "ERR_UNKNOWN_ENGINE_TASK", "ERR_MISSING_REQUIRED", "ERR_UNKNOWN_PARAM", "ERR_WRONG_TYPE",
    "ERR_OUT_OF_RANGE", "ERR_NOT_IN_ENUM", "ERR_DEPENDENCY_UNMET",
    "ParamDependency", "ParamField", "ParamSchema", "ParamError", "ValidationResult",
    "register_schema", "get_schema", "list_schemas", "validate", "describe_for_ui",
]
