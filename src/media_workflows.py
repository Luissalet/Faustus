"""
media_workflows.py — approved recipes, not graphs the model wrote.

The whole design of Phase 3 turns on one refusal: **the agent never supplies a
ComfyUI graph.** A ComfyUI prompt is a node graph, and some nodes read files,
write files or run other people's Python. Letting a model assemble one is the
same class of mistake as letting it assemble a shell command — except the
blast radius includes every custom node the machine happens to have installed.

So a workflow is a **versioned template on disk** that declares:

* `inputs` — the only things anyone may fill in, each with a type and a range
  or a list of choices. An input that is not declared is a refusal that names
  it, never a value that gets quietly ignored;
* `computed` — values the template derives from those inputs through explicit
  lookup tables (aspect ratio → width and height). Tables, not expressions: a
  template file is data somebody pastes, and the moment it can express a
  computation it is code;
* `models` and `requires_nodes` — what the engine has to already have. Checked
  before submitting, so a missing checkpoint is an answer in a second rather
  than a job that dies twenty minutes in;
* `graph` — the ComfyUI prompt, with `{{name}}` placeholders that resolve ONLY
  against declared inputs and computed values.

`render()` is pure and does no I/O: it takes a template and a dict of what the
user asked for, and returns either a graph or a refusal naming the field. That
is what makes it testable without a GPU in the room.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.contracts.base import ContractError, fingerprint

logger = logging.getLogger(__name__)

#: Where the approved templates live. A directory, versioned in the repo,
#: deliberately NOT under `data/` — these are code review's business, not
#: something a running process edits.
WORKFLOWS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "config", "media_workflows")

#: Input types a template may declare. Closed, like everything else here.
INPUT_TYPES = ("text", "enum", "integer", "number", "boolean", "seed", "artifact")

#: `{{name}}` — the only substitution that exists. No filters, no expressions,
#: no nesting; a placeholder is a name or it is not a placeholder.
PLACEHOLDER = re.compile(r"^\{\{([a-z][a-z0-9_]*)\}\}$")

#: The largest seed ComfyUI accepts. Also the point past which a "random" seed
#: stops round-tripping through JavaScript's number type, which is how a
#: recorded seed quietly stops reproducing the image it came from.
MAX_SEED = 2 ** 53 - 1


class TemplateError(ContractError):
    """A template on disk is wrong, or the values given to it are.

    Deliberately the same shape as a contract error — `path` names the field —
    because the person reading it is doing the same thing either way: looking
    for the line they have to change."""


@dataclass(frozen=True)
class ModelRequirement:
    """One model the template cannot run without.

    `license` is not decoration: a media file handed to a client carries the
    licence of the model that made it, and by the time someone asks, nobody
    remembers. It travels into the artifact's provenance."""

    name: str
    kind: str = "checkpoint"          # checkpoint | lora | vae | controlnet | upscale
    license: str = ""
    node: str = ""                    # the node whose dropdown lists it
    field: str = ""                   # the field on that node

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "license": self.license,
                "node": self.node, "field": self.field}


@dataclass(frozen=True)
class InputSpec:
    """One thing a caller may fill in."""

    name: str
    type: str
    required: bool = False
    default: Any = None
    choices: Tuple[Any, ...] = ()
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    max_len: int = 2000
    title: str = ""

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"name": self.name, "type": self.type,
                               "required": self.required, "title": self.title}
        if self.default is not None:
            out["default"] = self.default
        if self.choices:
            out["choices"] = list(self.choices)
        if self.minimum is not None:
            out["minimum"] = self.minimum
        if self.maximum is not None:
            out["maximum"] = self.maximum
        if self.type == "text":
            out["max_len"] = self.max_len
        return out


@dataclass(frozen=True)
class MediaWorkflow:
    """An approved recipe. Frozen, fingerprinted, and read from disk."""

    id: str
    version: str
    title: str
    engine: str
    description: str = ""
    inputs: Tuple[InputSpec, ...] = ()
    computed: Mapping[str, Any] = field(default_factory=dict)
    models: Tuple[ModelRequirement, ...] = ()
    requires_nodes: Tuple[str, ...] = ()
    outputs: Mapping[str, str] = field(default_factory=dict)
    graph: Mapping[str, Any] = field(default_factory=dict)
    source: str = ""

    def input(self, name: str) -> Optional[InputSpec]:
        return next((i for i in self.inputs if i.name == name), None)

    def to_dict(self, *, with_graph: bool = False) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "id": self.id, "version": self.version, "title": self.title,
            "engine": self.engine, "description": self.description,
            "inputs": [i.to_dict() for i in self.inputs],
            "computed": dict(self.computed),
            "models": [m.to_dict() for m in self.models],
            "requires_nodes": list(self.requires_nodes),
            "outputs": dict(self.outputs),
            "fingerprint": self.fingerprint(),
            "source": os.path.basename(self.source),
        }
        if with_graph:
            out["graph"] = dict(self.graph)
        return out

    def fingerprint(self) -> str:
        """Identity of the recipe, graph included.

        The graph is in it on purpose: a template whose sampler changed is a
        different recipe, and an artifact that claims to have come from
        `image.product 1.0.0` should not silently mean two different things."""
        return fingerprint([
            ("id", self.id), ("version", self.version), ("engine", self.engine),
            ("inputs", [i.to_dict() for i in self.inputs]),
            ("computed", dict(self.computed)),
            ("graph", dict(self.graph)),
        ])


# ── reading a template ────────────────────────────────────────────────────

def _spec_from(raw: Any, name: str, path: str) -> InputSpec:
    if not isinstance(raw, Mapping):
        raise TemplateError(f"{path}.inputs.{name}", "expected an object", got=raw)
    kind = str(raw.get("type") or "")
    if kind not in INPUT_TYPES:
        raise TemplateError(f"{path}.inputs.{name}.type",
                            f"unknown input type; known: {', '.join(INPUT_TYPES)}",
                            got=raw.get("type"))
    choices = tuple(raw.get("choices") or ())
    if kind == "enum" and not choices:
        raise TemplateError(f"{path}.inputs.{name}.choices",
                            "an enum input has to list what it accepts; without "
                            "choices it is a text field with a misleading name")
    return InputSpec(
        name=name, type=kind,
        required=bool(raw.get("required", False)),
        default=raw.get("default"),
        choices=choices,
        minimum=raw.get("minimum"), maximum=raw.get("maximum"),
        max_len=int(raw.get("max_len") or 2000),
        title=str(raw.get("title") or ""),
    )


def parse(raw: Any, *, source: str = "") -> MediaWorkflow:
    """A template file → a `MediaWorkflow`, or a refusal naming the field."""
    path = "workflow"
    if not isinstance(raw, Mapping):
        raise TemplateError(path, "expected an object", got=raw)

    for required in ("id", "version", "title", "engine", "graph"):
        if not raw.get(required):
            raise TemplateError(f"{path}.{required}", "is required")

    inputs_raw = raw.get("inputs") or {}
    if not isinstance(inputs_raw, Mapping):
        raise TemplateError(f"{path}.inputs", "expected an object keyed by name",
                            got=inputs_raw)
    inputs = tuple(_spec_from(v, k, path) for k, v in inputs_raw.items())

    models = []
    for i, m in enumerate(raw.get("models") or ()):
        if not isinstance(m, Mapping) or not m.get("name"):
            raise TemplateError(f"{path}.models[{i}]",
                                "each model needs at least a name", got=m)
        models.append(ModelRequirement(
            name=str(m["name"]), kind=str(m.get("kind") or "checkpoint"),
            license=str(m.get("license") or ""),
            node=str(m.get("node") or ""), field=str(m.get("field") or "")))

    computed = raw.get("computed") or {}
    if not isinstance(computed, Mapping):
        raise TemplateError(f"{path}.computed", "expected an object", got=computed)

    known = {i.name for i in inputs}
    for name, rule in computed.items():
        if not isinstance(rule, Mapping) or "from" not in rule or "map" not in rule:
            raise TemplateError(f"{path}.computed.{name}",
                                "expected {from: <input name>, map: {value: result}} — "
                                "a lookup table, because an expression in a template "
                                "file is code in a data file")
        if rule["from"] not in known:
            raise TemplateError(f"{path}.computed.{name}.from",
                                f"no input named {rule['from']!r} in this template",
                                got=rule["from"])

    workflow = MediaWorkflow(
        id=str(raw["id"]), version=str(raw["version"]), title=str(raw["title"]),
        engine=str(raw["engine"]), description=str(raw.get("description") or ""),
        inputs=inputs, computed=dict(computed), models=tuple(models),
        requires_nodes=tuple(str(n) for n in (raw.get("requires_nodes") or ())),
        outputs=dict(raw.get("outputs") or {}),
        graph=dict(raw["graph"]), source=source,
    )
    _check_placeholders(workflow)
    return workflow


def _check_placeholders(workflow: MediaWorkflow) -> None:
    """Every `{{name}}` in the graph has to resolve to something declared.

    Caught when the template is read rather than when it runs, because a
    placeholder nobody declared is a template that will fail on the machine of
    whoever first tries to use it — usually in front of them."""
    known = {i.name for i in workflow.inputs} | set(workflow.computed)
    for name in sorted(_placeholders_in(workflow.graph)):
        if name not in known:
            raise TemplateError(
                "workflow.graph",
                f"the graph uses {{{{{name}}}}} but the template declares no input "
                f"or computed value called {name!r}; declared: "
                f"{', '.join(sorted(known)) or 'nothing'}")


def _placeholders_in(node: Any) -> set:
    found = set()
    if isinstance(node, str):
        match = PLACEHOLDER.match(node)
        if match:
            found.add(match.group(1))
    elif isinstance(node, Mapping):
        for value in node.values():
            found |= _placeholders_in(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            found |= _placeholders_in(value)
    return found


# ── the catalogue ─────────────────────────────────────────────────────────

def catalogue(*, directory: Optional[str] = None) -> Dict[str, Any]:
    """Every template on disk, and every file that could not be read.

    A broken template is reported rather than skipped: a recipe that silently
    vanished from the list is how somebody spends an afternoon wondering why
    the model refuses to use it."""
    folder = directory or WORKFLOWS_DIR
    workflows: List[MediaWorkflow] = []
    broken: List[Dict[str, str]] = []
    if not os.path.isdir(folder):
        return {"workflows": [], "broken": [], "directory": folder,
                "note": "no template directory on this machine"}

    for name in sorted(os.listdir(folder)):
        if not name.endswith(".json"):
            continue
        full = os.path.join(folder, name)
        try:
            with open(full, "r", encoding="utf-8") as fh:
                workflows.append(parse(json.load(fh), source=full))
        except TemplateError as e:
            broken.append({"file": name, "field": e.path, "reason": e.message})
        except Exception as e:                      # a bad JSON file is a fact
            broken.append({"file": name, "field": "", "reason": f"{type(e).__name__}: {e}"})

    seen: Dict[str, str] = {}
    for wf in workflows:
        key = f"{wf.id}@{wf.version}"
        if key in seen:
            broken.append({"file": os.path.basename(wf.source), "field": "workflow.id",
                           "reason": f"{key} is already defined by "
                                     f"{os.path.basename(seen[key])}"})
        seen[key] = wf.source

    return {"workflows": workflows, "broken": broken, "directory": folder}


def load(workflow_id: str, version: str = "", *,
         directory: Optional[str] = None) -> Optional[MediaWorkflow]:
    """One template by id, newest version unless one is named."""
    found = [w for w in catalogue(directory=directory)["workflows"]
             if w.id == workflow_id and (not version or w.version == version)]
    if not found:
        return None
    return sorted(found, key=lambda w: w.version)[-1]


# ── filling one in ────────────────────────────────────────────────────────

def _coerce(spec: InputSpec, value: Any) -> Any:
    """One value, checked against what the template said it accepts.

    Nothing is converted across a type: `"3"` is not 3 here, the same rule the
    contracts use. A template that meant to accept a string would have said
    text, and a silent conversion is how a resolution ends up as a string in
    the graph and ComfyUI answers with something unreadable."""
    path = f"inputs.{spec.name}"

    if spec.type == "text":
        if not isinstance(value, str):
            raise TemplateError(path, "expected a string", got=value)
        if len(value) > spec.max_len:
            raise TemplateError(path, f"is longer than the {spec.max_len} characters "
                                      "this template accepts", got=len(value))
        return value

    if spec.type == "enum":
        if value not in spec.choices:
            raise TemplateError(path, f"is not one of {list(spec.choices)}", got=value)
        return value

    if spec.type == "boolean":
        if not isinstance(value, bool):
            raise TemplateError(path, "expected true or false", got=value)
        return value

    if spec.type in ("integer", "seed"):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TemplateError(path, "expected a whole number", got=value)
    elif spec.type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TemplateError(path, "expected a number", got=value)
    elif spec.type == "artifact":
        if not isinstance(value, str) or not value:
            raise TemplateError(path, "expected an artifact filename", got=value)
        if os.path.sep in value or "/" in value or ".." in value:
            # An input image reaches the engine as a name it looks up in its
            # own input folder. A path here would be someone reading a file
            # off the machine through a template that looked harmless.
            raise TemplateError(path, "expected a bare filename, not a path", got=value)
        return value

    if spec.minimum is not None and value < spec.minimum:
        raise TemplateError(path, f"is below the minimum of {spec.minimum}", got=value)
    if spec.maximum is not None and value > spec.maximum:
        raise TemplateError(path, f"is above the maximum of {spec.maximum}", got=value)
    return value


def resolve_values(workflow: MediaWorkflow,
                   given: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """What every placeholder in this template will be, or a refusal.

    Separate from `render()` so a caller can show a person exactly what is
    about to be run — the seed included — before anything is queued."""
    supplied = dict(given or {})
    declared = {i.name for i in workflow.inputs}

    unknown = sorted(set(supplied) - declared)
    if unknown:
        raise TemplateError(
            f"inputs.{unknown[0]}",
            f"this template accepts no input called {unknown[0]!r}; it accepts "
            f"{', '.join(sorted(declared)) or 'nothing'}. Templates are the only "
            "thing that decides what a run can change")

    values: Dict[str, Any] = {}
    for spec in workflow.inputs:
        if spec.name in supplied and supplied[spec.name] is not None:
            values[spec.name] = _coerce(spec, supplied[spec.name])
        elif spec.type == "seed":
            # A seed nobody chose is generated HERE and recorded, rather than
            # left to the engine. An engine-side random seed is a picture
            # nobody can ever make again.
            values[spec.name] = secrets.randbelow(MAX_SEED)
        elif spec.default is not None:
            values[spec.name] = spec.default
        elif spec.required:
            raise TemplateError(f"inputs.{spec.name}", "is required by this template")
        else:
            values[spec.name] = None

    for name, rule in workflow.computed.items():
        source = values.get(rule["from"])
        table = rule["map"]
        if source not in table:
            raise TemplateError(
                f"computed.{name}",
                f"the template has no value of {name} for {rule['from']}="
                f"{source!r}; it covers {sorted(table)}")
        values[name] = table[source]

    return values


def render(workflow: MediaWorkflow,
           given: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """The graph to send to the engine, plus the values that made it.

    Returns `{"graph", "values", "workflow", "version", "fingerprint"}` — the
    values come back because they are the provenance. Reproducing a picture
    means replaying these, and an engine-side seed or a default nobody wrote
    down makes that impossible."""
    values = resolve_values(workflow, given)
    return {
        "graph": _substitute(workflow.graph, values),
        "values": values,
        "workflow": workflow.id,
        "version": workflow.version,
        "fingerprint": workflow.fingerprint(),
        "models": [m.to_dict() for m in workflow.models],
    }


def _substitute(node: Any, values: Mapping[str, Any]) -> Any:
    """Replace whole `{{name}}` strings; touch nothing else.

    Whole strings only, never inside a longer one. Partial substitution would
    let a prompt written by a user close a JSON string and open a field the
    template never declared — the injection this format exists to make
    impossible."""
    if isinstance(node, str):
        match = PLACEHOLDER.match(node)
        return values.get(match.group(1)) if match else node
    if isinstance(node, Mapping):
        return {k: _substitute(v, values) for k, v in node.items()}
    if isinstance(node, (list, tuple)):
        return [_substitute(v, values) for v in node]
    return node
