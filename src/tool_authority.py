"""One authority for what a tool is (H05).

Faustus used to describe a tool in several places that could drift apart: the
native function schema, the code that turns a native call back into the text
a handler expects, the limits a handler really applies, the effect class the
approval gate uses, and whether the tool is offered directly or only on
request. `RegisteredTool` puts those facts on one record and the registry
below is the only place that

  * GENERATES the schema a model is shown (`emit`),
  * GENERATES the text a handler receives from a native call (`encode_native`),
  * resolves a name, a canonical id or an alias to the one tool it stands for
    (`resolve`), so a revocation of the tool holds under every spelling,
  * states the limits, resources and exposure of the tool, and
  * answers what a given CALL may do (`effects_for_call`), because the effect
    of a multi-action tool depends on its arguments, not on its name.

A tool is either `authored` (its spec lives in `src.tool_authority_schemas`
and `src.tool_schemas.FUNCTION_TOOL_SCHEMAS` is generated from it), `contract`
(its spec lives in a contract module that already owned it, such as the PDF
navigation contract, and the registry adopts it) or `wrapped` (registered from
the catalogue that still owns its spec, so that the descriptor fields exist
for every tool while the family is not migrated yet). A tool discovered from
an MCP server is described with `descriptor_from_mcp` at discovery time; that
descriptor is a value, never added to the process-wide registry, because the
set of MCP tools changes while a run is in flight.

This module is pure: it imports no handler, dispatcher, settings store or
schema catalogue at import time. Handlers are looked up in a table the caller
hands over, and effects read through `src.tool_capabilities` on demand, which
stays the single classification of effects.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence, Tuple

_WIRE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
#: MCP servers choose their own tool names; the qualified wire name may carry
#: `-` and `.` and is bounded at the same 512 characters the catalogue uses.
_MCP_WIRE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.\-]{0,511}$")
_CANONICAL_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
_ALIAS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]*$")

ORIGINS = ("authored", "contract", "wrapped", "mcp")


class ToolAuthorityError(ValueError):
    """A registration or lookup that would make the authority ambiguous."""


class Exposure(str, Enum):
    """How a tool reaches the model.

    DIRECT    its schema may be sent natively whenever the tool is selected.
    DEFERRED  selected and executable, but only listed as a one-line catalog
              entry until the model (or the user's words) asks for it; the
              full schema then loads through `lookup_tools`.
    CODE_ONLY callable only from inside a Code Mode program; never sent as a
              native schema and never promoted by discovery.
    """

    DIRECT = "direct"
    DEFERRED = "deferred"
    CODE_ONLY = "code_only"


@dataclass(frozen=True)
class LimitRef:
    """A limit that is owned by the module that applies it.

    `target` is "package.module:ATTRIBUTE"; it is resolved when read, so the
    authority can state a limit without importing a heavy handler module.
    """

    target: str

    def resolve(self) -> Any:
        module, _, attribute = self.target.partition(":")
        if not module or not attribute:
            raise ToolAuthorityError(f"bad limit reference {self.target!r}")
        return getattr(importlib.import_module(module), attribute)


@dataclass(frozen=True)
class ToolLimits:
    """Named limits a tool's handler applies to a call.

    Keys in use: `hard_timeout_s`, `idle_timeout_setting` (the live setting
    that bounds a silent command), `max_output_chars`, `max_results`,
    `max_line_chars`, `max_bytes`, `max_image_bytes`. A value may be a number,
    a string or a `LimitRef`. Nothing is declared for a limit the handler does
    not apply.
    """

    items: Tuple[Tuple[str, Any], ...] = ()

    @staticmethod
    def of(**limits: Any) -> "ToolLimits":
        return ToolLimits(tuple(sorted(limits.items())))

    def keys(self) -> Tuple[str, ...]:
        return tuple(key for key, _ in self.items)

    def get(self, key: str, default: Any = None) -> Any:
        for name, value in self.items:
            if name == key:
                return value.resolve() if isinstance(value, LimitRef) else value
        return default

    def resolved(self) -> dict:
        return {key: self.get(key) for key in self.keys()}


@dataclass(frozen=True)
class ToolEffects:
    """Effects of a tool, read through `src.tool_capabilities`.

    That module stays the only classification of effects; this record only
    says how to ask it. `action_field` names the argument that selects the
    sub-action of a multiplexed tool, `None` for a single-purpose tool.
    """

    action_field: Optional[str] = None

    def static(self, name: str) -> frozenset:
        from src.tool_capabilities import capabilities_for_tool
        return frozenset(effect.value for effect in capabilities_for_tool(name).effects)

    def known(self, name: str) -> bool:
        from src.tool_capabilities import capabilities_for_tool
        return bool(capabilities_for_tool(name).known)

    def for_call(self, name: str, content: Any) -> frozenset:
        """Effects of THIS call: a read action of a manager tool is a read."""
        from src.tool_capabilities import capabilities_for_action
        return frozenset(effect.value for effect in capabilities_for_action(name, content).effects)

    def by_action(self, name: str, actions: Iterable[str]) -> dict:
        return {action: self.for_call(name, {self.action_field or "action": action}) for action in actions}


@dataclass(frozen=True)
class ToolResources:
    """What a call claims: paths it reads/writes, the network, a process."""

    path_args: Tuple[str, ...] = ()
    access: str = "none"  # none | read | write
    network: bool = False
    process: bool = False

    def claims(self, args: Mapping[str, Any]) -> Tuple[str, ...]:
        out = []
        if self.access in ("read", "write"):
            named = [str(args[key]).strip() for key in self.path_args
                     if isinstance(args.get(key), str) and str(args.get(key)).strip()]
            out.extend(f"fs:{self.access}:{path}" for path in named)
            if not named and self.access == "write":
                out.append("fs:write:*")
        if self.network:
            out.append("net")
        if self.process:
            out.append("proc")
        return tuple(out)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


@dataclass(frozen=True)
class ParserContract:
    """How a native call becomes the text its handler reads.

    `encode` is the one place that turns native arguments into handler input.
    `required_any` lists groups of argument names of which at least one must
    be non-blank (the native parser refuses a call that misses a group).
    `arg_aliases` are spellings the parser accepts in addition to the schema
    (alias, canonical) and `internal_keys` are arguments it forwards that the
    schema deliberately does not advertise.
    """

    encode: Callable[[Mapping[str, Any]], str]
    required_any: Tuple[Tuple[str, ...], ...] = ()
    arg_aliases: Tuple[Tuple[str, str], ...] = ()
    internal_keys: frozenset = frozenset()

    def missing_required(self, args: Mapping[str, Any]) -> Tuple[Tuple[str, ...], ...]:
        return tuple(group for group in self.required_any
                     if not any(str(args.get(key) or "").strip() for key in group))

    def accepted_keys(self, properties: Iterable[str]) -> frozenset:
        return frozenset(properties) | {alias for alias, _ in self.arg_aliases} | self.internal_keys


@dataclass(frozen=True)
class Resolution:
    canonical: str
    tool: "RegisteredTool"
    via: str  # name | canonical_id | alias


@dataclass(frozen=True)
class RegisteredTool:
    """Everything the runtime needs to know about one tool, in one place."""

    name: str
    canonical_id: str
    family: str
    description: str
    parameters_json: str
    parser: ParserContract
    effects: ToolEffects = ToolEffects()
    resources: ToolResources = ToolResources()
    limits: ToolLimits = ToolLimits()
    exposure: Exposure = Exposure.DIRECT
    aliases: Tuple[str, ...] = ()
    handler_key: str = ""
    origin: str = "authored"

    @property
    def parameters(self) -> dict:
        return json.loads(self.parameters_json)

    @property
    def properties(self) -> dict:
        return dict(self.parameters.get("properties") or {})

    @property
    def required(self) -> Tuple[str, ...]:
        return tuple(self.parameters.get("required") or ())

    def schema(self) -> dict:
        """The native function schema, as a fresh object every time."""
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters}}

    def spellings(self) -> frozenset:
        return frozenset({self.name, self.canonical_id, *self.aliases})

    def summary(self) -> dict:
        """The admin view of the descriptor: what it is, not its schema."""
        limits: dict = {}
        for key in self.limits.keys():
            try:
                limits[key] = self.limits.get(key)
            except Exception:  # noqa: BLE001 - a dangling reference is reported by the parity check
                limits[key] = None
        return {
            "name": self.name, "canonical_id": self.canonical_id, "family": self.family, "origin": self.origin,
            "exposure": self.exposure.value, "aliases": sorted(self.aliases), "limits": limits,
            "resources": {"path_args": list(self.resources.path_args), "access": self.resources.access,
                          "network": self.resources.network, "process": self.resources.process},
            "effects": sorted(self.effects.static(self.name)), "fingerprint": self.fingerprint(),
            "parameters": sorted(self.properties), "required": list(self.required),
        }

    def fingerprint(self) -> str:
        return hashlib.sha256(_json({
            "name": self.name, "canonical_id": self.canonical_id, "family": self.family,
            "description": self.description, "parameters": self.parameters,
            "aliases": sorted(self.aliases), "exposure": self.exposure.value,
            "limits": [[k, v.target if isinstance(v, LimitRef) else v] for k, v in self.limits.items],
            "resources": [list(self.resources.path_args), self.resources.access,
                          self.resources.network, self.resources.process],
            "handler_key": self.handler_key or self.name, "origin": self.origin,
        }).encode("utf-8")).hexdigest()


def make_tool(*, name: str, canonical_id: str, family: str, description: str, parameters: Mapping[str, Any],
              parser: ParserContract, effects: ToolEffects = ToolEffects(), resources: ToolResources = ToolResources(),
              limits: ToolLimits = ToolLimits(), exposure: Exposure = Exposure.DIRECT,
              aliases: Sequence[str] = (), handler_key: str = "", origin: str = "authored") -> RegisteredTool:
    """Validate and freeze a registration. Raises `ToolAuthorityError`."""
    wire_re = _MCP_WIRE_NAME_RE if origin == "mcp" else _WIRE_NAME_RE
    if not isinstance(name, str) or not wire_re.match(name):
        raise ToolAuthorityError(f"bad tool name {name!r}")
    if not isinstance(canonical_id, str) or not _CANONICAL_ID_RE.match(canonical_id):
        raise ToolAuthorityError(f"{name}: canonical id must be dotted lower-case, got {canonical_id!r}")
    if origin not in ORIGINS:
        raise ToolAuthorityError(f"{name}: unknown origin {origin!r}")
    if not isinstance(description, str):
        raise ToolAuthorityError(f"{name}: description must be text")
    if not isinstance(parameters, Mapping) or parameters.get("type") != "object":
        raise ToolAuthorityError(f"{name}: parameters must be an object schema")
    properties = parameters.get("properties") or {}
    if not isinstance(properties, Mapping):
        raise ToolAuthorityError(f"{name}: properties must be a mapping")
    missing = [key for key in (parameters.get("required") or ()) if key not in properties]
    if missing and origin != "mcp":  # a server's own sloppy schema must not break discovery
        raise ToolAuthorityError(f"{name}: required {missing} are not declared properties")
    for group in parser.required_any:
        unknown = [key for key in group if key not in parser.accepted_keys(properties)]
        if unknown:
            raise ToolAuthorityError(f"{name}: parser requires {unknown} which no schema property or alias accepts")
    for alias in aliases:
        if not isinstance(alias, str) or not _ALIAS_RE.match(alias):
            raise ToolAuthorityError(f"{name}: bad alias {alias!r}")
    if not isinstance(exposure, Exposure):
        raise ToolAuthorityError(f"{name}: exposure must be an Exposure")
    return RegisteredTool(
        name=name, canonical_id=canonical_id, family=family, description=description,
        parameters_json=json.dumps(parameters, ensure_ascii=False, allow_nan=False), parser=parser, effects=effects, resources=resources,
        limits=limits, exposure=exposure, aliases=tuple(dict.fromkeys(a for a in aliases if a != name)),
        handler_key=handler_key or name, origin=origin)


@dataclass(frozen=True)
class ParityIssue:
    tool: str
    check: str
    detail: str


class ToolAuthority:
    """The registry. Registration order is preserved for schema emission."""

    def __init__(self) -> None:
        self._tools: dict = {}
        self._spelling: dict = {}
        self._populator: Optional[Callable[[], None]] = None
        self._populated = False
        self._populating = False

    def set_populator(self, populator: Callable[[], None]) -> None:
        """A callable that registers the tools this module cannot know about
        at import time (the `wrapped` catalogue). It runs once, on the first
        lookup; an import failure leaves the registry unpopulated so a later
        lookup retries instead of serving a silently partial registry."""
        self._populator = populator
        self._populated = False

    def _ensure(self) -> None:
        if self._populator is None or self._populated or self._populating:
            return
        self._populating = True
        try:
            self._populator()
            self._populated = True
        except ImportError:
            pass  # still importing; the next lookup retries
        finally:
            self._populating = False

    # -- registration -----------------------------------------------------
    def register(self, tool: RegisteredTool) -> RegisteredTool:
        if tool.name in self._tools:
            raise ToolAuthorityError(f"duplicate tool {tool.name!r}")
        for spelling in tool.spellings():
            owner = self._spelling.get(spelling)
            if owner is not None and owner != tool.name:
                raise ToolAuthorityError(f"{spelling!r} already identifies {owner!r}")
        self._tools[tool.name] = tool
        for spelling in tool.spellings():
            self._spelling[spelling] = tool.name
        return tool

    def unregister(self, name: str) -> None:
        tool = self._tools.pop(name, None)
        if tool is not None:
            for spelling in tool.spellings():
                if self._spelling.get(spelling) == name:
                    del self._spelling[spelling]

    # -- lookup -------------------------------------------------------------
    def names(self, *, family: Optional[str] = None, origin: Optional[str] = None) -> Tuple[str, ...]:
        self._ensure()
        return tuple(name for name, tool in self._tools.items()
                     if (family is None or tool.family == family) and (origin is None or tool.origin == origin))

    def get(self, name: Any) -> Optional[RegisteredTool]:
        self._ensure()
        return self._tools.get(name) if isinstance(name, str) else None

    def __contains__(self, name: Any) -> bool:
        return self.get(name) is not None

    def resolve(self, name: Any) -> Optional[Resolution]:
        """The tool a name, canonical id or alias stands for; None if unknown."""
        if not isinstance(name, str):
            return None
        self._ensure()
        canonical = self._spelling.get(name)
        if canonical is None:
            return None
        tool = self._tools[canonical]
        via = "name" if name == tool.name else "canonical_id" if name == tool.canonical_id else "alias"
        return Resolution(canonical, tool, via)

    def is_spelling(self, name: Any) -> bool:
        """Is `name` already the wire name, canonical id or alias of a tool?
        (Does not trigger population: used while the catalogue is built.)"""
        return isinstance(name, str) and name in self._spelling

    def spellings(self, name: Any) -> frozenset:
        """Every spelling of the tool `name` stands for (just `name` if unknown)."""
        resolution = self.resolve(name)
        return resolution.tool.spellings() if resolution else frozenset({name} if isinstance(name, str) else ())

    def any_spelling_in(self, name: Any, names: Iterable[str]) -> bool:
        """Is any spelling of the tool in `names`? Used for revocation, so a
        denied tool cannot be reached through its alias or canonical id."""
        pool = {str(n) for n in names or ()}
        return bool(self.spellings(name) & pool)

    # -- emission -------------------------------------------------------------
    def emit(self, name: str) -> dict:
        tool = self.get(name)
        if tool is None:
            raise ToolAuthorityError(f"unknown tool {name!r}")
        return tool.schema()

    def emit_all(self, *, origins: Sequence[str] = ("authored",)) -> list:
        return [tool.schema() for tool in self._tools.values() if tool.origin in origins]

    # -- parser contract ---------------------------------------------------------
    def encode_native(self, name: str, args: Mapping[str, Any]) -> str:
        tool = self.get(name)
        if tool is None:
            raise ToolAuthorityError(f"unknown tool {name!r}")
        return tool.parser.encode(args)

    def required_groups(self, name: str) -> Tuple[Tuple[str, ...], ...]:
        tool = self.get(name)
        return tool.parser.required_any if tool else ()

    # -- limits, resources, exposure, effects -----------------------------------
    def limits(self, name: str) -> ToolLimits:
        tool = self.get(name)
        if tool is None:
            raise ToolAuthorityError(f"unknown tool {name!r}")
        return tool.limits

    def limit(self, name: str, key: str, default: Any = None) -> Any:
        return self.limits(name).get(key, default)

    def exposure(self, name: str) -> Exposure:
        resolution = self.resolve(name)
        return resolution.tool.exposure if resolution else Exposure.DIRECT

    def exposure_map(self) -> dict:
        self._ensure()
        return {name: tool.exposure for name, tool in self._tools.items()}

    def claims_for_call(self, name: str, args: Mapping[str, Any]) -> Tuple[str, ...]:
        tool = self.get(name)
        return tool.resources.claims(args) if tool else ()

    def effects_for_call(self, name: str, content: Any) -> frozenset:
        resolution = self.resolve(name)
        tool = resolution.tool if resolution else None
        effects = tool.effects if tool else ToolEffects()
        return effects.for_call(resolution.canonical if resolution else name, content)

    # -- dispatch -------------------------------------------------------------
    def handler(self, name: str, table: Mapping[str, Callable]) -> Optional[Callable]:
        """The executable registration of a registered tool, or None.

        Only the tool's own wire name dispatches: an alias or a canonical id
        is a spelling for parsing and policy, not a second way to execute,
        because the content a handler reads is encoded for the wire name.
        A registered tool with no entry in the table has no handler: the
        registry never invents one, and a descriptive entry alone does not
        make a tool executable.
        """
        tool = self.get(name)
        if tool is None:
            return None
        return table.get(tool.handler_key or tool.name)

    # -- identity -----------------------------------------------------------------
    def fingerprint(self, names: Optional[Iterable[str]] = None) -> str:
        chosen = sorted(names) if names is not None else sorted(self._tools)
        return hashlib.sha256(_json([[n, self._tools[n].fingerprint()] for n in chosen if n in self._tools]
                                    ).encode("utf-8")).hexdigest()

    # -- parity ---------------------------------------------------------------------
    def parity_report(self, handlers: Optional[Mapping[str, Callable]] = None,
                      *, sample_args: Optional[Callable[[RegisteredTool], Mapping[str, Any]]] = None) -> list:
        """Machine-checkable statements that must hold for every tool.

        * the schema is a well-formed object schema whose required names are
          properties;
        * every advertised property reaches the handler input: changing it
          changes what `encode` produces (nothing declared is dropped);
        * every name the parser insists on is one the schema or an alias
          names;
        * an authored or contract tool has a handler in the executable table;
        * declared limits are either numbers/strings or resolvable.
        """
        self._ensure()
        issues: list = []
        for name, tool in self._tools.items():
            params = tool.parameters
            properties = params.get("properties") or {}
            if tool.origin != "wrapped":
                issues.extend(self._encode_issues(tool, properties, sample_args))
            # A wrapped tool may be executed by a dispatcher branch that is not
            # in the handler table; an owned (authored/contract) tool may not.
            if (handlers is not None and tool.origin in ("authored", "contract")
                    and handlers.get(tool.handler_key or name) is None):
                issues.append(ParityIssue(name, "handler", "no executable registration in the handler table"))
            for key in tool.limits.keys():
                try:
                    tool.limits.get(key)
                except Exception as exc:  # noqa: BLE001 - a dangling reference is the finding
                    issues.append(ParityIssue(name, "limits", f"{key}: {exc}"))
        return issues

    def _encode_issues(self, tool: RegisteredTool, properties: Mapping[str, Any],
                       sample_args: Optional[Callable[[RegisteredTool], Mapping[str, Any]]]) -> list:
        issues: list = []
        base = dict(sample_args(tool) if sample_args else sample_arguments(tool))
        try:
            baseline = tool.parser.encode(base)
        except Exception as exc:  # noqa: BLE001
            return [ParityIssue(tool.name, "encode", f"cannot encode a schema-valid call: {exc}")]
        for key, prop in properties.items():
            varied = dict(base)
            varied[key] = _other_value(prop, base.get(key))
            try:
                changed = tool.parser.encode(varied) != baseline
            except Exception as exc:  # noqa: BLE001
                issues.append(ParityIssue(tool.name, "encode", f"{key}: {exc}"))
                continue
            if not changed:
                issues.append(ParityIssue(tool.name, "dropped_property",
                                          f"{key} is advertised but does not reach the handler input"))
        return issues


def sample_arguments(tool: RegisteredTool) -> dict:
    """A minimal schema-valid call: the required properties only."""
    properties = tool.properties
    return {key: _sample_value(properties[key]) for key in tool.required}


def _sample_value(prop: Mapping[str, Any]) -> Any:
    if prop.get("enum"):
        return prop["enum"][0]
    kind = prop.get("type")
    if kind == "integer":
        return max(1, int(prop.get("minimum", 1)))
    if kind == "number":
        return max(1.0, float(prop.get("minimum", 1)))
    if kind == "boolean":
        return False
    if kind == "array":
        return []
    if kind == "object":
        return {}
    if prop.get("pattern") == "^#[0-9a-fA-F]{6}$":
        return "#112233"
    return "sample"


def _other_value(prop: Mapping[str, Any], current: Any) -> Any:
    """A schema-valid value different from `current` (or from the default sample)."""
    if prop.get("enum"):
        members = list(prop["enum"])
        first = members[0]
        pick = current if current is not None else first
        return next((m for m in members if m != pick), first)
    kind = prop.get("type")
    if kind == "integer":
        return int(current if isinstance(current, int) and not isinstance(current, bool) else 1) + 1
    if kind == "number":
        return float(current if isinstance(current, (int, float)) and not isinstance(current, bool) else 1) + 1
    if kind == "boolean":
        return not bool(current)
    if kind == "array":
        return ["x"] if not current else []
    if kind == "object":
        return {"x": 1} if not current else {}
    if prop.get("pattern") == "^#[0-9a-fA-F]{6}$":
        return "#445566" if current != "#445566" else "#778899"
    return "other-value" if current != "other-value" else "another-value"


_MCP_ID_PART = re.compile(r"[^a-z0-9_]+")


def _mcp_id_part(text: Any) -> str:
    part = _MCP_ID_PART.sub("_", str(text or "").lower()).strip("_")
    if not part or not part[0].isalpha():
        part = "x" + part
    return part


def _mcp_no_encode(args: Mapping[str, Any]) -> str:
    raise ToolAuthorityError("an MCP tool is called through its server, not through a native parser")


def descriptor_from_mcp(tool: Mapping[str, Any], *, readonly: Optional[bool] = None) -> RegisteredTool:
    """Build the descriptor of one discovered MCP tool.

    `tool` is the mapping `McpManager.get_all_tools()` reports. The descriptor
    carries the schema the server announced at discovery, so a later server
    update cannot change the contract a call already holding this descriptor
    was made under. Raises `ToolAuthorityError` for a tool the descriptor
    rules refuse (a nameless tool, a schema that is not an object schema).
    """
    if not isinstance(tool, Mapping):
        raise ToolAuthorityError("an MCP tool must be a mapping")
    server_id = str(tool.get("server_id") or "").strip()
    bare = str(tool.get("name") or "").strip()
    name = str(tool.get("qualified_name") or (f"mcp__{server_id}__{bare}" if server_id and bare else "")).strip()
    if not name or not server_id:
        raise ToolAuthorityError("an MCP tool needs a server id and a name")
    schema = tool.get("input_schema")
    if not isinstance(schema, Mapping) or not schema:
        schema = {"type": "object", "properties": {}}
    schema = dict(schema)
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    if readonly is None:
        readonly = False  # fail closed: an unclassified MCP tool reads as a write
    canonical = f"mcp.{_mcp_id_part(server_id)}.{_mcp_id_part(bare or name)}"
    return make_tool(
        name=name, canonical_id=canonical, family=f"mcp:{server_id}",
        description=str(tool.get("description") or "")[:4000], parameters=schema,
        parser=ParserContract(_mcp_no_encode), effects=ToolEffects(),
        resources=ToolResources(network=True, access="none"),
        limits=ToolLimits(), exposure=Exposure.DIRECT, handler_key=name, origin="mcp")


def deepcopy_schema(entry: Mapping[str, Any]) -> dict:
    return copy.deepcopy(dict(entry))


#: Process-wide registry. Specs are registered by `src.tool_authority_specs`.
AUTHORITY = ToolAuthority()
