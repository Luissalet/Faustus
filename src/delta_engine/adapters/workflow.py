"""Two workflow definitions, compared on what reaches outside the machine.

Section 14 of the plan. A workflow is a graph whose nodes are the only place in
this system where "it ran twice" is worse than "it stopped", so the whole value
of comparing two definitions is telling a change that reaches outside from a
change that renames a heading.

Five rules, each with the failure it prevents:

* **A new effectful node is blocking, and a renamed title is not.** §28 asks
  for both halves and the second one is the harder: an adapter that files a
  changed `title` as an effect gets switched off within a week, and then it
  protects nothing. The node hash is taken over a CANONICAL rendering (sorted
  config keys), so reordering config that changes no value produces no finding
  at all, and a changed title produces one `modified` on `node:<id>` that
  points at no invariant.

* **`WorkflowNode` has no postcondition, no provider, no timeout and no
  permissions field.** This module does not invent them. What the contract
  declares is compared as a field; what it does not is compared over `config`
  IF the caller put it there, and where neither is true the answer is `unknown`
  with the gap named -- never `preserved`. The full §14 list, and which half of
  it is checkable today, is written out below.

* **The idempotency case is the one the contract already names.**
  `WorkflowNode.parse` refuses an effectful node with `max_attempts > 1` and no
  `config.idempotent`, because "the second attempt is the one that sends the
  email again". This adapter therefore reads a node the contract REFUSES rather
  than discarding it: a definition that has degraded in exactly that way is a
  definition somebody is about to fix, and a delta that answered "unreadable"
  about it would describe nothing. The refusal is recorded on the element and
  reported; it is evidence, not a gap.

* **Removing a gate widens what runs unattended.** A `human_approval` node that
  disappears is a permission change, filed against
  `security.permissions_not_widened`, for the same reason §14 files a withdrawn
  approval trigger there: the workflow may now do without a person what it
  could not do before.

* **Nothing here was executed.** Behavioural coverage is 0.0 and is DECLARED
  rather than omitted, so `coverage.sufficient(..., dimensions=("behavioral",))`
  refuses to rest a conclusion about what the workflow DOES on a reading of
  what it SAYS.

WHAT OF §14'S BLOCKING LIST IS CHECKABLE TODAY (the list this module owes its
documentation, and the reason each entry falls where it does):

* `nuevo efecto externo` -- CHECKABLE, exactly. `WorkflowNode.type` is a closed
  vocabulary and `EFFECTFUL_TYPES` is part of the contract.
* `idempotencia degradada` -- CHECKABLE, exactly. `max_attempts` and
  `config.idempotent` are both real, and the rule is the contract's own.
* `retry con riesgo de duplicación` -- CHECKABLE for retries (`max_attempts` on
  an effectful node). The `timeout` half is NOT: no node field carries one, and
  a `config.timeout` the caller supplies is compared as an ordinary parameter
  with no meaning attached.
* `gate removido` -- CHECKABLE for `human_approval`, whose type says what it
  is. Weakly checkable for `condition`: the contract does not distinguish a
  guard from a branch router, so a removed one is reported at `medium` with
  that limitation named. The `test removido` half is NOT checkable: `NODE_TYPES`
  has no test node, so a workflow cannot say it had one.
* `permiso ampliado` -- PARTIAL. `WorkflowNode` has no permissions field. A
  `config.permissions` the caller supplies is compared, and a value that goes
  from absent/false/empty to anything else is a widening. With nothing declared
  anywhere the answer is `unknown`, never `preserved`.
* `secreto nuevo` -- PARTIAL, the same way, over `config.secrets`. No credential
  scan runs here: a key pasted into some other config value would not appear in
  any element this module builds, so the invariant is `unknown` unless the
  declared list actually grew.
* `proveedor externo donde antes era local` -- PARTIAL. No provider field
  exists; `config.provider` is read, and "local" is decided by `LOCAL_PROVIDERS`
  below, which is this module's own list of names and not a contract. A source
  provider that list does not recognise gives no claim in either direction.
* `postcondición eliminada` -- NOT CHECKABLE. `WorkflowNode` declares no
  postcondition and nothing in `invariants.py` names one. A removed
  `config.postconditions.N` shows up as a `missing` parameter like any other,
  and an invariant asking about postconditions comes back `unknown` with that
  sentence as its limitation.
* `rollback eliminado` -- NOT CHECKABLE. There is no rollback field and no
  rollback node type; there is nothing to compare.

Revisions reach this adapter through `sources.resolve`. A `literal` (what
`sources.stash()` returns for a definition the caller has in hand) is readable;
a revision of kind `workflow` is NOT, because `sources.RESOLVERS` has no reader
for the workflow store and that module is not this one's to change. The refusal
carries the resolver's own reason, which names the store.

`registry.py` finds this module by the module-level `ADAPTER_FACTORY` at the
bottom. There is no list to add it to, which is why it cannot be finished and
invisible at the same time.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from src.contracts.base import ContractError
from src.contracts.workflow import (
    EFFECTFUL_TYPES,
    WorkflowDefinition,
    WorkflowNode,
)
from src.delta_engine import confidence as confidence_mod
from src.delta_engine import coverage as coverage_mod
from src.delta_engine import sources
from src.delta_engine.adapters.base import (
    Element,
    Extraction,
    Finding,
    Scope,
    Snapshot,
    unreadable,
)
from src.delta_engine.contracts import (
    Coverage,
    IntentContract,
    InvariantResult,
    RevisionRef,
)

logger = logging.getLogger(__name__)

__all__ = [
    "WorkflowAdapter",
    "ADAPTER_FACTORY",
    "EFFECTS_INVARIANT",
    "PERMISSIONS_INVARIANT",
    "NETWORK_INVARIANT",
    "SECRETS_INVARIANT",
    "GATE_TYPES",
    "LOCAL_PROVIDERS",
    "IDEMPOTENT_KEY",
    "PERMISSIONS_KEY",
    "SECRETS_KEY",
    "PROVIDER_KEY",
    "CONTRACT_REFUSED",
]


# -- the invariants this adapter can be asked about ------------------------
#
# Every id below is one `invariants.py` already declares -- the three
# `SECURITY_INVARIANTS` plus `DOMAIN_DEFAULTS["workflow"]`. They are written as
# constants rather than reached out of those tuples by index, because an index
# is a second thing to keep in step; `tests/test_delta_engine_workflow.py`
# asserts that each one is an id the catalogue actually defines, so a rename
# over there fails a test here instead of quietly producing findings that point
# at an invariant nobody declared.

#: §14's "nuevo efecto externo" and "idempotencia degradada". Class `security`
#: in the catalogue, so a violation is `blocking` and cannot be filed lower.
EFFECTS_INVARIANT = "workflow.effects_not_widened"

#: §14's "permiso ampliado" and "gate removido". Class `permissions`: removing
#: the human from the loop is a permission the workflow did not have before.
PERMISSIONS_INVARIANT = "security.permissions_not_widened"

#: §14's "proveedor externo donde antes era local".
NETWORK_INVARIANT = "security.network_not_widened"

#: §14's "secreto nuevo".
SECRETS_INVARIANT = "security.secrets_not_exposed"

#: Node types that gate what follows them. `human_approval` is a gate by
#: definition; `condition` is one only sometimes -- the contract does not
#: distinguish a guard from a branch router -- which is why a removed
#: `condition` is reported at `medium` with that limitation attached and a
#: removed `human_approval` is not.
GATE_TYPES: Tuple[str, ...] = ("human_approval", "condition")

#: Provider names this module recognises as running on the user's own machine.
#: THIS IS NOT A CONTRACT. `WorkflowNode` has no provider field at all, so the
#: whole check rests on a `config.provider` the caller supplied plus this list,
#: and a name absent from it is not evidence of anything: the source side must
#: be recognisably local before a move away from it is called an externalisation.
LOCAL_PROVIDERS = frozenset((
    "local", "localhost", "offline", "builtin", "in_process", "inprocess",
    "ollama", "lmstudio", "lm_studio", "llamacpp", "llama_cpp", "vllm",
    "comfyui", "faustus",
))

#: The config keys this adapter reads for the §14 properties the contract does
#: not carry as fields. Named constants because `compare` and `check_invariants`
#: both address them, and a second spelling in either would compare a key
#: against nothing and report `added`.
IDEMPOTENT_KEY = "idempotent"
PERMISSIONS_KEY = "permissions"
SECRETS_KEY = "secrets"
PROVIDER_KEY = "provider"

#: The label a node carries when `WorkflowNode.parse` refused it. §28 asks for a
#: "parser degradado etiquetado"; this is the workflow spelling, and it is
#: EVIDENCE rather than a gap: the fields were read, the contract simply would
#: not accept the combination -- which for the idempotency rule is the very
#: change §14 calls blocking.
CONTRACT_REFUSED = "contract_refused"

#: How many element keys a coverage lists. `Coverage.regions_analyzed` accepts
#: 1024; the cut is made below it and REPORTED, because a list silently clipped
#: at the contract's ceiling reads as the complete set of regions examined.
MAX_LISTED_REGIONS = 512

#: How long a rendered value may be. `Element.value` is documented as a SHORT
#: rendering, and `DeltaAssertion.parse` accepts 2000 for `before`/`after`; a
#: config blob long enough to hold a prompt would make a snapshot as expensive
#: as the definition it describes.
MAX_VALUE_CHARS = 300

#: Renderings that mean "nothing was granted". A parameter moving OUT of this
#: set is a widening; one moving into it is a narrowing, and only the first
#: direction is a §14 blocking change.
FALSY_RENDERS = frozenset(("false", "null", "0", '""', "[]", "{}", ""))


# -- small helpers ---------------------------------------------------------


def _short(value: Any, limit: int = 480) -> str:
    """One line, bounded. The contracts cap observations and limitations at 500.

    Truncation is marked with an ellipsis rather than silent, because a reason
    cut at a period reads as a complete sentence that happens to be wrong.
    """
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _unique(values: Sequence[Any]) -> Tuple[str, ...]:
    """Order-preserving de-duplication for lists this module CONCATENATES.

    Same rule as `coverage._unique`: `Coverage` and `DeltaAssertion` both reject
    a repeated entry, and the repeat here is an artifact of joining two
    snapshots' notes -- both ends cut by the same budget produce the same
    sentence twice. It is removed where it was created.
    """
    seen: set = set()
    out: List[str] = []
    for value in values:
        text = str(value)
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return tuple(out)


def _canonical(value: Any) -> str:
    """A value rendered so that two equal values render identically.

    Sorted keys and no stray whitespace, which is what makes "reordenar claves
    de config que no cambian de valor" produce no finding: the node hash is
    taken over this rendering, so key order is not part of the identity of a
    node. Unserialisable values fall back to `str()` -- a config carrying an
    object is already outside what a definition should hold, and refusing the
    whole comparison over it would be a worse answer than a weaker one.
    """
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover - `default=str` covers it
        return str(value)


def _digest(value: Any) -> str:
    """sha256 of the canonical rendering. One spelling, used by both ends."""
    return hashlib.sha256(_canonical(value).encode("utf-8", "replace")).hexdigest()


def _flatten(value: Any, prefix: str = "") -> Iterator[Tuple[str, str]]:
    """`config` as dotted keys and rendered scalars. §14's "parámetros".

    A list index becomes a segment (`secrets.0`), so a credential added to a
    list has its own address and shows up as an `added` parameter rather than
    as one long value that changed. Scalars go through `_canonical`, so `"1"`
    and `1` are different parameters -- which they are, and a config that
    changed a number into a string is exactly the quiet break a delta exists to
    surface.
    """
    if isinstance(value, Mapping):
        for key in sorted(value, key=str):
            child = f"{prefix}.{key}" if prefix else str(key)
            yield from _flatten(value[key], child)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            child = f"{prefix}.{index}" if prefix else str(index)
            yield from _flatten(item, child)
    else:
        yield prefix or "<root>", _canonical(value)[:MAX_VALUE_CHARS]


def _widened(before: str, after: str) -> bool:
    """Whether a parameter went from granting nothing to granting something.

    The asymmetry is the point. `false -> true` and `[] -> ["prod"]` are §14's
    "permiso ampliado"; the reverse is a narrowing, and reporting it as a
    blocking change would teach a reader to skip these rows.
    """
    return before in FALSY_RENDERS and after not in FALSY_RENDERS


def _is_local(provider: str) -> bool:
    return str(provider or "").strip().lower() in LOCAL_PROVIDERS


# -- reading one definition ------------------------------------------------


@dataclass(frozen=True)
class _NodeRead:
    """One node as this adapter could read it, plus the contract's verdict on it.

    `refusal` is what `WorkflowNode.parse` said when it would not accept the
    node. It is kept rather than raised because the single most important
    change §14 names -- an effectful node whose retry lost its idempotency
    declaration -- is a node the contract REFUSES, and an adapter that answered
    "unreadable" about it would be silent about the one case it exists for.
    """

    id: str
    type: str = ""
    title: str = ""
    needs: Tuple[str, ...] = ()
    config: Mapping[str, Any] = None  # type: ignore[assignment]
    max_attempts: int = 1
    continue_on_failure: bool = False
    refusal: str = ""

    def __post_init__(self) -> None:
        if self.config is None:
            object.__setattr__(self, "config", {})

    @property
    def effectful(self) -> bool:
        return self.type in EFFECTFUL_TYPES

    @property
    def idempotent(self) -> bool:
        """Truthiness, exactly as `WorkflowNode.parse` reads it.

        The contract writes `not (config or {}).get("idempotent")`, so `1` and
        `"yes"` pass there. Reading it any more strictly here would make this
        adapter report a degradation on a definition the contract accepts.
        """
        return bool(self.config.get(IDEMPOTENT_KEY))

    @property
    def provider(self) -> str:
        return str(self.config.get(PROVIDER_KEY) or "").strip()

    def shape(self) -> Dict[str, Any]:
        """What makes two nodes the same node. Hashed; key order excluded.

        `needs` is sorted because a reordered dependency list is the same graph,
        and `config` is rendered with sorted keys by `_canonical`. Both are
        §28's "cambio cosmético no se clasifica como efecto", written where the
        identity of a node is decided rather than argued about downstream.
        """
        return {
            "type": self.type,
            "title": self.title,
            "needs": sorted(set(self.needs)),
            "config": dict(self.config),
            "max_attempts": self.max_attempts,
            "continue_on_failure": self.continue_on_failure,
        }


def _int_or(raw: Any, default: int) -> int:
    """A whole number, or the default. Booleans are not numbers here.

    `True` is an `int` in Python and `max_attempts: true` is a typo, not a
    request for one attempt; reading it as `1` would hide the typo behind a
    plausible value.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return default
    return int(raw)


def _read_node(raw: Any, path: str) -> Optional[_NodeRead]:
    """One node through `WorkflowNode.parse`, or defensively when it refuses.

    The contract first, always: when it accepts the node, every field below
    comes from the parsed object and carries the validation that goes with it.
    When it refuses -- an effectful node retried without `config.idempotent`, a
    type outside `NODE_TYPES`, an id that is not an identifier -- the raw
    mapping is read field by field and the refusal is carried on the result.

    `None` is returned only for a node with no readable id. An element cannot be
    addressed without one, and inventing an address would make two unrelated
    nodes align.
    """
    if not isinstance(raw, Mapping):
        return None
    try:
        node = WorkflowNode.parse(raw, path)
    except ContractError as exc:
        identifier = str(raw.get("id") or "").strip()
        if not identifier:
            return None
        config = raw.get("config")
        needs = raw.get("needs")
        return _NodeRead(
            id=identifier,
            type=str(raw.get("type") or "").strip(),
            title=str(raw.get("title") or ""),
            needs=tuple(str(item) for item in needs
                        if isinstance(item, str)) if isinstance(needs, (list, tuple)) else (),
            config=dict(config) if isinstance(config, Mapping) else {},
            max_attempts=_int_or(raw.get("max_attempts"), 1),
            continue_on_failure=raw.get("continue_on_failure") is True,
            refusal=_short(exc),
        )
    return _NodeRead(
        id=node.id, type=node.type, title=node.title, needs=node.needs,
        config=dict(node.config), max_attempts=node.max_attempts,
        continue_on_failure=node.continue_on_failure,
    )


def _read_definition(payload: Mapping[str, Any]) -> Tuple[Tuple[_NodeRead, ...],
                                                          Tuple[str, ...],
                                                          Tuple[str, ...]]:
    """`(nodes, notes, skipped)` out of a definition payload.

    `WorkflowDefinition.parse` is tried first because it validates the GRAPH --
    a `needs` naming nothing, a cycle, duplicate ids -- and a comparison run
    over a definition with a cycle would describe a process that can never
    start. When it refuses, the refusal is kept as a note and the nodes are read
    one at a time, so that a definition which is broken in exactly the way §14
    calls blocking still produces a delta a person can act on.
    """
    notes: List[str] = []
    skipped: List[str] = []
    try:
        definition = WorkflowDefinition.parse(payload, "workflow")
    except ContractError as exc:
        notes.append(f"the definition does not satisfy `WorkflowDefinition.parse` "
                     f"({_short(exc, 300)}); its nodes were read one at a time and "
                     f"the graph itself -- cycles, dangling `needs` -- was not "
                     f"validated")
        raw_nodes = payload.get("nodes")
        items = list(raw_nodes) if isinstance(raw_nodes, (list, tuple)) else []
        reads: List[_NodeRead] = []
        for index, raw in enumerate(items):
            read = _read_node(raw, f"workflow.nodes[{index}]")
            if read is None:
                skipped.append(f"nodes[{index}] carries no readable id and could "
                               f"not be addressed")
                continue
            reads.append(read)
        for read in reads:
            if read.refusal:
                notes.append(f"node {read.id} is refused by the contract: "
                             f"{_short(read.refusal, 300)}")
        return tuple(reads), tuple(_unique(notes)), tuple(_unique(skipped))

    reads = tuple(
        _NodeRead(id=node.id, type=node.type, title=node.title, needs=node.needs,
                  config=dict(node.config), max_attempts=node.max_attempts,
                  continue_on_failure=node.continue_on_failure)
        for node in definition.nodes
    )
    return reads, (), ()


# -- addressing ------------------------------------------------------------


def _node_elements(node: _NodeRead) -> Tuple[List[Element], List[Element]]:
    """`(anchors, rest)` for one node. §14's five element kinds, and no sixth.

    The anchors -- `node:`, `retry:`, `effect:` -- are the three every §14
    blocking check reads, and `_budgeted` never drops them. `edge:` and
    `param:` are the layers a budget may cut, because losing them costs detail
    and losing an anchor would make a large workflow indistinguishable from one
    that declares no effects at all.
    """
    detail: Dict[str, Any] = {
        "type": node.type,
        "title": node.title,
        "needs": sorted(set(node.needs)),
        "max_attempts": node.max_attempts,
        "effectful": node.effectful,
        "idempotent": node.idempotent,
        "provider": node.provider,
        "declares_permissions": PERMISSIONS_KEY in node.config,
        "declares_secrets": SECRETS_KEY in node.config,
        "declares_provider": PROVIDER_KEY in node.config,
    }
    if node.refusal:
        detail[CONTRACT_REFUSED] = node.refusal

    anchors: List[Element] = [
        # The hash carries the WHOLE node, so a changed title is a `modified`
        # here and nowhere else -- and `value` carries the type, so a reader
        # who only looks at the rendering still sees what kind of step it is.
        Element(key=f"node:{node.id}", kind=node.type or "node",
                hash=_digest(node.shape()), value=node.type, detail=detail),
        Element(key=f"retry:{node.id}", kind="retry",
                value=(f"max_attempts={node.max_attempts}, "
                       f"{IDEMPOTENT_KEY}={'true' if node.idempotent else 'false'}"),
                detail={"max_attempts": node.max_attempts,
                        "idempotent": node.idempotent,
                        "effectful": node.effectful}),
    ]
    if node.effectful:
        anchors.append(Element(
            key=f"effect:{node.id}", kind="effect", value=node.type,
            detail={"type": node.type, "idempotent": node.idempotent,
                    "max_attempts": node.max_attempts, "provider": node.provider}))

    rest: List[Element] = []
    for dependency in sorted(set(node.needs)):
        rest.append(Element(key=f"edge:{dependency}->{node.id}", kind="edge",
                            value=f"{dependency} -> {node.id}"))
    for key, rendered in _flatten(node.config):
        rest.append(Element(key=f"param:{node.id}#{key}", kind="param",
                            value=rendered))
    return anchors, rest


def _param_field(key: str) -> str:
    """The first dotted segment of a `param:` address, or `""`.

    `param:publish#permissions.network` -> `permissions`. Used to decide which
    invariant a parameter bears on, and written once so `compare` and
    `check_invariants` cannot disagree about where `permissions` ends and
    `network` begins.
    """
    _, separator, rest = key.partition("#")
    if not separator:
        return ""
    return rest.split(".", 1)[0]


def _node_of(key: str) -> str:
    """The node id an element address belongs to, for a readable detail line."""
    body = key.split(":", 1)[1] if ":" in key else key
    return body.split("#", 1)[0]


# -- what the two snapshots amount to --------------------------------------


@dataclass(frozen=True)
class _Assessment:
    """The §14 questions, answered once over two snapshots.

    Computed in one place and read by `check_invariants`, so that the invariant
    rows and the findings cannot disagree about whether an effect is new. They
    are derived from the ELEMENTS rather than from the definitions, because the
    elements are what a budget may have cut and an assessment over the full
    definition would describe a comparison that did not happen.
    """

    new_effects: Tuple[str, ...] = ()
    degraded_retries: Tuple[str, ...] = ()
    removed_gates: Tuple[str, ...] = ()
    removed_routers: Tuple[str, ...] = ()
    widened_permissions: Tuple[str, ...] = ()
    new_secrets: Tuple[str, ...] = ()
    externalised: Tuple[str, ...] = ()
    declares_permissions: bool = False
    declares_secrets: bool = False
    declares_provider: bool = False
    target_effects: Tuple[str, ...] = ()
    source_gates: Tuple[str, ...] = ()


def _risky_retry(element: Optional[Element]) -> bool:
    """The contract's own rule, read off one `retry:` element.

    `WorkflowNode.parse`: an effectful node with `max_attempts > 1` and no
    `config.idempotent` is refused, "because the second attempt is the one that
    sends the email again". This is that sentence as a predicate, so that the
    finding and the invariant row are decided by one reading of it.
    """
    if element is None:
        return False
    detail = dict(element.detail or {})
    return bool(detail.get("effectful")
                and int(detail.get("max_attempts") or 1) > 1
                and not detail.get("idempotent"))


def _externalised(before: Optional[Element], after: Optional[Element]) -> bool:
    """A provider that was recognisably local and is not any more.

    Both halves are required. A source provider this module does not recognise
    is not evidence that it was local, so a change away from it says nothing --
    which is the honest reading of a check that rests on a name list rather
    than on anything that resolved a host.
    """
    if before is None or after is None:
        return False
    was = str(dict(before.detail or {}).get("provider") or "")
    now = str(dict(after.detail or {}).get("provider") or "")
    if not was or not now or was == now:
        return False
    return _is_local(was) and not _is_local(now)


def _assess(source: Snapshot, target: Snapshot) -> _Assessment:
    """Every §14 question this adapter can answer, over two snapshots."""
    left, right = source.index(), target.index()
    keys = sorted(set(left) | set(right))

    new_effects: List[str] = []
    degraded: List[str] = []
    removed_gates: List[str] = []
    removed_routers: List[str] = []
    widened: List[str] = []
    secrets: List[str] = []
    externalised: List[str] = []
    target_effects: List[str] = []
    source_gates: List[str] = []

    for key in keys:
        before, after = left.get(key), right.get(key)
        if key.startswith("effect:"):
            if after is not None:
                target_effects.append(_node_of(key))
                if before is None:
                    new_effects.append(_node_of(key))
        elif key.startswith("retry:"):
            if _risky_retry(after) and not _risky_retry(before):
                degraded.append(_node_of(key))
        elif key.startswith("node:"):
            if before is not None and before.value in GATE_TYPES:
                source_gates.append(_node_of(key))
                if after is None:
                    (removed_gates if before.value == "human_approval"
                     else removed_routers).append(_node_of(key))
            if _externalised(before, after):
                externalised.append(_node_of(key))
        elif key.startswith("param:"):
            field = _param_field(key)
            if field not in (PERMISSIONS_KEY, SECRETS_KEY):
                continue
            grew = after is not None and (
                before is None or _widened(before.value, after.value))
            if not grew:
                continue
            (widened if field == PERMISSIONS_KEY else secrets).append(key)

    declares = [dict(element.detail or {}) for element in
                list(source.elements) + list(target.elements)
                if element.key.startswith("node:")]
    return _Assessment(
        new_effects=tuple(_unique(new_effects)),
        degraded_retries=tuple(_unique(degraded)),
        removed_gates=tuple(_unique(removed_gates)),
        removed_routers=tuple(_unique(removed_routers)),
        widened_permissions=tuple(_unique(widened)),
        new_secrets=tuple(_unique(secrets)),
        externalised=tuple(_unique(externalised)),
        declares_permissions=any(d.get("declares_permissions") for d in declares),
        declares_secrets=any(d.get("declares_secrets") for d in declares),
        declares_provider=any(d.get("declares_provider") for d in declares),
        target_effects=tuple(_unique(target_effects)),
        source_gates=tuple(_unique(source_gates)),
    )


# -- the adapter -----------------------------------------------------------


class WorkflowAdapter:
    """The `workflow` domain. Implements the `DeltaAdapter` Protocol.

    One revision shape reaches `snapshot` today: a `literal` holding the JSON
    of a definition, which is what `sources.stash(definition.to_dict())` hands
    back. A revision of kind `workflow` is refused by `sources.resolve` with
    that module's own reason -- there is no resolver against the workflow store
    and `sources.py` is not this module's to change -- and the refusal travels
    into the snapshot rather than becoming an exception, because "the store has
    no reader here" is an ANSWER and raising would discard the other end's work.
    """

    domain = "workflow"
    version = "1"

    def available(self) -> bool:
        """Always true. Nothing here can be missing from a machine.

        `src.contracts.workflow` is in this repository and everything else is
        the standard library. An adapter that probed for something it does not
        use would go unavailable for a reason nobody can act on, and
        `registry.adapter_for` turns an unavailable adapter into a refused
        comparison rather than a weaker one.
        """
        return True

    # -- reading one end ---------------------------------------------------

    def snapshot(self, revision: RevisionRef, *, scope: Scope) -> Snapshot:
        """One definition as nodes, edges, parameters, retries and effects.

        An unreadable revision returns `unreadable(...)` carrying the resolver's
        reason: which end failed and why is exactly what a caller needs, and an
        exception here would say neither.

        A payload that is JSON but not a definition -- no `nodes` list -- is
        also `readable=False`. An empty node list would compare against a real
        workflow as though every node had been deleted, which is the most
        expensive wrong answer this adapter could give.
        """
        resolved = sources.resolve(revision, scope=scope)
        if not resolved.readable:
            return unreadable(revision, resolved.reason, tier="parser")
        try:
            payload = resolved.mapping()
        except sources.SourceError as exc:
            return unreadable(revision, _short(exc), tier="parser")

        nodes, notes, skipped = _read_definition(payload)
        if not nodes:
            return unreadable(revision, (
                "this payload declares no readable workflow node, so there is "
                "nothing to address; an empty snapshot would compare against a "
                "real definition as if every node had been deleted"),
                tier="parser")

        anchors: List[Element] = []
        rest: List[Element] = []
        seen: set = set()

        def _keep(element: Element, into: List[Element]) -> None:
            # `WorkflowDefinition.parse` refuses duplicate node ids, so a repeat
            # is only reachable on the defensive path -- and there it matters:
            # two elements at one address make alignment arbitrary, and keeping
            # the first is that decision made where the cause is visible.
            if element.key in seen:
                logger.warning("workflow adapter: duplicate element address %r; "
                               "keeping the first", element.key)
                return
            seen.add(element.key)
            into.append(element)

        for node in nodes:
            node_anchors, node_rest = _node_elements(node)
            for element in node_anchors:
                _keep(element, anchors)
            for element in node_rest:
                _keep(element, rest)

        elements, cut = self._budgeted(anchors, rest, scope=scope)
        return Snapshot(
            revision=revision,
            elements=elements,
            readable=True,
            # `parser`: every element above came from `WorkflowNode.parse` or,
            # where the contract refused the node, from a field-by-field read of
            # the same declared keys. Nothing here was guessed from a pattern.
            tier="parser",
            excluded=tuple(_unique(([cut] if cut else []) + list(skipped))),
            notes=tuple(_unique(notes)),
            truncated=bool(cut),
            detail={
                "nodes": len(nodes),
                "effectful_nodes": sum(1 for node in nodes if node.effectful),
                "refused_nodes": {node.id: node.refusal
                                  for node in nodes if node.refusal},
                # Both numbers, never one: `addressed` alone would let a
                # truncated snapshot report full structural coverage, which is
                # the direction that must never happen quietly.
                "addressable": len(anchors) + len(rest),
                "addressed": len(elements),
            },
        )

    def _budgeted(self, anchors: List[Element], rest: List[Element], *,
                  scope: Scope) -> Tuple[Tuple[Element, ...], str]:
        """The elements, cut to `budget.max_elements`, and the sentence for it.

        `node:`, `retry:` and `effect:` are spent first and are never dropped:
        every §14 blocking check reads one of them, and a budget that removed an
        `effect:` would make a workflow that publishes look like one that does
        not. What the cut removes is edges and parameters, in address order, so
        two runs over one revision produce the same snapshot -- which is what
        the §22 cache key depends on.
        """
        limit = scope.budget.max_elements
        ordered = (sorted(anchors, key=lambda item: item.key)
                   + sorted(rest, key=lambda item: item.key))
        if limit is None or len(ordered) <= limit:
            return tuple(ordered), ""
        kept = ordered[:max(limit, len(anchors))]
        dropped = len(ordered) - len(kept)
        return tuple(kept), (
            f"{dropped} edge(s) and parameter(s) were not addressed; "
            f"`budget.max_elements` ({limit}) stopped the snapshot and nothing "
            f"past that point was compared")


    # -- comparing two ends ------------------------------------------------

    def compare(self, source: Snapshot, target: Snapshot, *,
                scope: Scope) -> Extraction:
        """What differs between two definitions, and which of it reaches outside.

        Elements are matched by ADDRESS and by address only. A workflow's node
        id is what every edge and every parameter hangs off, so a node that
        changed id is a different graph and reporting it as `moved` would claim
        a correspondence the definition itself does not make. The cost is named
        in the limitations rather than papered over with a name-similarity
        guess.

        An unreadable end produces no findings at all rather than a `missing`
        for every element: "we could not read it" and "every node was deleted"
        are opposite facts, and a list of removals says the second.
        """
        readable = source.readable and target.readable
        limitations = [
            "this adapter compares declared fields: node types, dependencies, "
            "config parameters, retries and effects. Nothing was executed, so "
            "nothing here is evidence about what the workflow does at run time",
            "nodes are matched by id; a node that changed id is reported as a "
            "removal plus an addition, because the id is the address every edge "
            "and parameter hangs off",
        ]
        if not readable:
            which = "source" if not source.readable else "target"
            limitations.append(
                f"the {which} revision could not be read, so nothing was "
                f"compared; this is not a report that nothing changed")
            return Extraction(
                findings=(),
                coverage=self._coverage(source, target, regions=(), readable=False),
                invariants=(),
                extractor_versions={self.domain: self.version},
                limitations=tuple(_unique(limitations)),
            )

        left, right = source.index(), target.index()
        keys = sorted(set(left) | set(right))
        findings: List[Finding] = []
        for key in keys:
            finding = self._finding(key, left.get(key), right.get(key))
            if finding is not None:
                findings.append(finding)

        refused = dict(source.detail or {}).get("refused_nodes") or {}
        refused_target = dict(target.detail or {}).get("refused_nodes") or {}
        for where, refusals in (("source", refused), ("target", refused_target)):
            for node_id, reason in sorted(dict(refusals).items()):
                limitations.append(_short(
                    f"the {where} node {node_id} is one `WorkflowNode.parse` "
                    f"refuses: {reason}. Its fields were read anyway, and the "
                    f"refusal is evidence rather than a gap"))

        return Extraction(
            findings=tuple(findings),
            coverage=self._coverage(source, target, regions=tuple(keys),
                                    readable=True),
            invariants=(),
            extractor_versions={self.domain: self.version},
            limitations=tuple(_unique(limitations)),
        )

    def _finding(self, key: str, before: Optional[Element],
                 after: Optional[Element]) -> Optional[Finding]:
        """One observation about one address. Never a classification.

        `unchanged` is emitted rather than skipped: it is a real observation and
        says we looked, which is what lets a later layer report `preserved` with
        something behind it. What this method decides is only the OPERATION and
        which invariant the row bears on; `classification.classify_all` is what
        turns either into a judgement, and it has seen the intent while this
        method has not.
        """
        if before is None and after is None:  # pragma: no cover - union of keys
            return None
        common: Dict[str, Any] = {
            "path": key,
            "method": "manifest_field",
            "tier": confidence_mod.tier_of("manifest_field"),
            "confidence": "exact",
        }
        kind = key.split(":", 1)[0]
        if kind == "effect":
            return self._effect_finding(key, before, after, common)
        if kind == "retry":
            return self._retry_finding(key, before, after, common)
        if kind == "node":
            return self._node_finding(key, before, after, common)
        if kind == "param":
            return self._param_finding(key, before, after, common)
        return self._plain_finding(key, before, after, common)

    def _plain_finding(self, key: str, before: Optional[Element],
                       after: Optional[Element],
                       common: Dict[str, Any]) -> Finding:
        """`added`/`missing`/`modified`/`unchanged` with nothing attached.

        The floor every other branch starts from, so that the four operations
        are decided in one place and a branch that only wants to add an
        invariant reference does not get to redecide what it saw.
        """
        sample = before if before is not None else after
        kind = sample.kind if sample is not None else ""
        if before is None and after is not None:
            return Finding(operation="added", after=_signature(after),
                           element_kind=kind, **common)
        if after is None and before is not None:
            return Finding(operation="missing", before=_signature(before),
                           element_kind=kind, **common)
        if before is None or after is None:  # pragma: no cover - handled above
            return Finding(operation="unchanged", element_kind=kind, **common)
        if _signature(before) == _signature(after):
            return Finding(operation="unchanged", before=_signature(before),
                           after=_signature(after), element_kind=kind, **common)
        return Finding(operation="modified", before=_signature(before),
                       after=_signature(after), element_kind=kind, **common)

    def _effect_finding(self, key: str, before: Optional[Element],
                        after: Optional[Element],
                        common: Dict[str, Any]) -> Finding:
        """§14's first blocking row: a side effect the source did not declare.

        `EFFECTFUL_TYPES` is part of the contract and `WorkflowNode.type` is a
        closed vocabulary, so this is an identity comparison and nothing weaker:
        the node either is one of the three types that reach outside or it is
        not. An effect that DISAPPEARED is `missing` and carries no invariant --
        a workflow that stopped publishing is narrower, not wider.
        """
        base = self._plain_finding(key, before, after, common)
        if base.operation != "added":
            return base
        node_id = _node_of(key)
        return Finding(
            path=base.path, operation="added", after=base.after,
            element_kind=base.element_kind, method=base.method, tier=base.tier,
            confidence="exact",
            invariant_refs=(EFFECTS_INVARIANT,),
            detail=_short(
                f"node {node_id} is a `{after.value if after else ''}` node, one "
                f"of EFFECTFUL_TYPES, and the source declares no effect at that "
                f"address; the target reaches outside where the source did not"),
        )

    def _retry_finding(self, key: str, before: Optional[Element],
                       after: Optional[Element],
                       common: Dict[str, Any]) -> Finding:
        """§14's "idempotencia degradada", in the contract's own words.

        The rule is `WorkflowNode.parse`'s: an effectful node with
        `max_attempts > 1` and no `config.idempotent` is refused, "because the
        second attempt is the one that sends the email again". A target in that
        state whose source was not is the degradation, and it is reported here
        whether or not the definition as a whole satisfied the contract -- a
        definition that has degraded in exactly this way is one somebody is
        about to fix, and a delta silent about it would be silent about the one
        case this row exists for.
        """
        base = self._plain_finding(key, before, after, common)
        if not _risky_retry(after) or _risky_retry(before):
            return base
        node_id = _node_of(key)
        attempts = int(dict((after.detail if after else {}) or {}).get("max_attempts") or 1)
        return Finding(
            path=base.path,
            operation=base.operation if base.operation != "unchanged" else "modified",
            before=base.before, after=base.after, element_kind=base.element_kind,
            method=base.method, tier=base.tier, confidence="exact",
            invariant_refs=(EFFECTS_INVARIANT,),
            detail=_short(
                f"node {node_id} reaches outside, is retried {attempts} times and "
                f"no longer declares `config.{IDEMPOTENT_KEY}`; "
                f"`WorkflowNode.parse` refuses exactly this, because the second "
                f"attempt is the one that sends the email again"),
        )

    def _node_finding(self, key: str, before: Optional[Element],
                      after: Optional[Element],
                      common: Dict[str, Any]) -> Finding:
        """A node that appeared, vanished, or is not the same node any more.

        Two of the four cases carry an invariant and the other two carry none,
        which is where §28's "cambio cosmético no se clasifica como efecto"
        lives: a changed `title` reaches here as `modified` with no reference to
        anything, because the node hash covers the title and the two checks
        below both come back empty.
        """
        base = self._plain_finding(key, before, after, common)
        if base.operation == "missing" and before is not None:
            if before.value == "human_approval":
                return Finding(
                    path=base.path, operation="missing", before=base.before,
                    element_kind=base.element_kind, method=base.method,
                    tier=base.tier, confidence="exact",
                    invariant_refs=(PERMISSIONS_INVARIANT,),
                    detail=_short(
                        f"the `human_approval` gate {_node_of(key)} is gone; what "
                        f"needed a person to say yes now runs unattended, which is "
                        f"a permission the workflow did not have before"),
                )
            if before.value == "condition":
                return Finding(
                    path=base.path, operation="missing", before=base.before,
                    element_kind=base.element_kind, method=base.method,
                    tier=base.tier,
                    # `medium`, and never better: the contract does not tell a
                    # guard from a branch router, so this is a measurement of a
                    # proxy for the property rather than of the property.
                    confidence="medium",
                    invariant_refs=(PERMISSIONS_INVARIANT,),
                    limitations=(
                        "a `condition` node may be a guard or a branch router "
                        "and `NODE_TYPES` does not distinguish them, so a "
                        "removed one is a gate removal only if it was gating",),
                    detail=_short(f"the `condition` node {_node_of(key)} is gone"),
                )
            return base
        if base.operation in ("modified", "added") and _externalised(before, after):
            was = str(dict((before.detail if before else {}) or {}).get("provider") or "")
            now = str(dict((after.detail if after else {}) or {}).get("provider") or "")
            return Finding(
                path=base.path, operation=base.operation, before=base.before,
                after=base.after, element_kind=base.element_kind,
                method=base.method, tier=base.tier,
                # `medium`: "external" is decided by `LOCAL_PROVIDERS`, a list
                # in this module, and nothing here resolved a host.
                confidence="medium",
                invariant_refs=(NETWORK_INVARIANT,),
                limitations=(
                    "`WorkflowNode` has no provider field; this reads "
                    f"`config.{PROVIDER_KEY}` and decides `local` from this "
                    "module's own name list, not from anything that resolved a "
                    "host",),
                detail=_short(f"node {_node_of(key)} moved from the local provider "
                              f"{was!r} to {now!r}"),
            )
        return base

    def _param_finding(self, key: str, before: Optional[Element],
                       after: Optional[Element],
                       common: Dict[str, Any]) -> Finding:
        """A config parameter, and the two prefixes that make one blocking.

        `WorkflowNode` carries no permissions and no secrets, so the ONLY thing
        this branch can honestly claim is about a `config.permissions` or a
        `config.secrets` the caller put there. A parameter that appears, or that
        goes from absent/false/empty to something, is a widening; every other
        parameter is a parameter, and the limitation says which of the two this
        row is.
        """
        base = self._plain_finding(key, before, after, common)
        field = _param_field(key)
        if field not in (PERMISSIONS_KEY, SECRETS_KEY):
            return base
        grew = base.operation == "added" or (
            base.operation == "modified" and _widened(base.before, base.after))
        if not grew:
            return base
        invariant = (PERMISSIONS_INVARIANT if field == PERMISSIONS_KEY
                     else SECRETS_INVARIANT)
        return Finding(
            path=base.path, operation=base.operation, before=base.before,
            after=base.after, element_kind=base.element_kind,
            method=base.method, tier=base.tier, confidence="exact",
            invariant_refs=(invariant,),
            limitations=(
                f"`WorkflowNode` declares no {field} field; this compares a "
                f"`config.{field}` the caller supplied, so a grant expressed "
                f"anywhere else in the config is not covered by this row",),
            detail=_short(f"node {_node_of(key)} grants {field} it did not grant "
                          f"before: {base.before or '<absent>'} -> {base.after}"),
        )

    def _coverage(self, source: Snapshot, target: Snapshot, *,
                  regions: Tuple[str, ...], readable: bool) -> Coverage:
        """How much was compared, on the two axes this adapter can speak to.

        `structural` is the fraction of addressable elements this comparison
        actually saw -- 1.0 unless a budget cut one of the snapshots.
        `behavioral` is 0.0 and is DECLARED rather than omitted: `Coverage.ratio`
        returns `None` for an axis nobody measured and `0.0` for one measured as
        empty, and this adapter did not fail to observe behaviour, it never runs
        anything. That zero is what makes `coverage.sufficient(...,
        dimensions=("behavioral",))` refuse to rest a claim about what the
        workflow DOES on a reading of what it SAYS.
        """
        structural = 0.0
        if readable:
            structural = min(_addressed_ratio(source), _addressed_ratio(target))
        listed = regions[:MAX_LISTED_REGIONS]
        notes = [
            "nothing was executed: this adapter reads a definition, so it "
            "cannot say whether a node that declares no effect has none",
        ]
        if len(regions) > len(listed):
            notes.append(f"{len(regions) - len(listed)} further element keys "
                         f"were compared and are not listed here")
        return coverage_mod.build(
            source_readable=source.readable,
            target_readable=target.readable,
            dimensions={"structural": structural, "behavioral": 0.0},
            regions=listed,
            excluded=_unique(list(source.excluded) + list(target.excluded)),
            notes=_unique(notes + list(source.notes) + list(target.notes)),
        )

    # -- the invariants ----------------------------------------------------

    def check_invariants(self, intent: IntentContract, source: Snapshot,
                         target: Snapshot, *,
                         scope: Scope) -> Tuple[InvariantResult, ...]:
        """One result per declared invariant, and four ids this adapter can answer.

        `preserved` is reachable for exactly one of them --
        `workflow.effects_not_widened` -- because that is the only §14 property
        whose evidence is a field the contract actually carries. Permissions are
        `preserved` only when the caller declared some to compare; the network
        and secrets invariants are NEVER `preserved` here, because a provider
        name is not a host and this module runs no credential scan. Everything
        else is `unknown` with the gap named, which is rule 1 of `contracts.py`:
        not detected is not preserved.
        """
        readable = source.readable and target.readable
        gap = _gap(source, target)
        complete = bool(readable and not gap)
        assessment = _assess(source, target) if readable else _Assessment()

        results: List[InvariantResult] = []
        for invariant in intent.invariants:
            payload: Dict[str, Any] = {
                "invariant_id": invariant.id,
                "severity": invariant.severity,
                "method": "manifest_field",
                "tier": "parser",
            }
            if not readable:
                which = "source" if not source.readable else "target"
                payload.update({
                    "status": "unknown", "confidence": "unknown",
                    "limitations": [f"the {which} revision could not be read, so "
                                    f"no property of it was observed"],
                })
            elif invariant.id == EFFECTS_INVARIANT:
                payload.update(self._effects_result(assessment, complete=complete,
                                                    gap=gap))
            elif invariant.id == PERMISSIONS_INVARIANT:
                payload.update(self._permissions_result(assessment, complete=complete,
                                                        gap=gap))
            elif invariant.id == NETWORK_INVARIANT:
                payload.update(self._network_result(assessment, gap=gap))
            elif invariant.id == SECRETS_INVARIANT:
                payload.update(self._secrets_result(assessment, gap=gap))
            elif invariant.klass == "budget":
                payload.update(self._budget_result(source, target))
            else:
                payload.update(self._unanswered(invariant))
            results.append(InvariantResult.parse(
                payload, f"invariant_result[{invariant.id}]"))
        return tuple(results)

    def _effects_result(self, assessment: _Assessment, *, complete: bool,
                        gap: str) -> Dict[str, Any]:
        """§14's effects property. The one this adapter can hold to `preserved`.

        The evidence is `WorkflowNode.type` against `EFFECTFUL_TYPES` plus
        `max_attempts` and `config.idempotent` -- three declared fields and a
        rule the contract itself enforces -- so a clean, complete comparison is
        an identity claim and says `exact`. A budget that cut a snapshot makes
        it `unknown` instead: the nodes nobody addressed are not evidence that
        they declare no effect.
        """
        observations: List[str] = []
        for node_id in assessment.new_effects:
            observations.append(f"new effectful node: {node_id}")
        for node_id in assessment.degraded_retries:
            observations.append(
                f"{node_id} is retried without `config.{IDEMPOTENT_KEY}` while "
                f"reaching outside")
        if observations:
            return {
                "status": "violated",
                "confidence": "exact" if complete else "high",
                "observations": [_short(item) for item in observations][:8],
                "limitations": [gap] if gap else [],
            }
        if not complete:
            return {"status": "unknown", "confidence": "unknown",
                    "limitations": [gap or "the comparison was not complete"]}
        return {
            "status": "preserved", "confidence": "exact",
            "observations": [
                _short(f"the target declares {len(assessment.target_effects)} "
                       f"effectful node(s) and every one of them is an address "
                       f"the source already declared as effectful"),
                _short(f"no effectful node is retried more than once without "
                       f"`config.{IDEMPOTENT_KEY}`"),
            ],
        }

    def _permissions_result(self, assessment: _Assessment, *, complete: bool,
                            gap: str) -> Dict[str, Any]:
        """§14's "permiso ampliado" and "gate removido", kept honest apart.

        `preserved` requires a `config.permissions` to have been declared
        somewhere, because otherwise the only thing observed is that the gates
        are still there -- which is a fact about approval nodes and not about
        permissions, and reporting it as this invariant preserved would be the
        exact shape of "we did not look" rendered as "it is fine".
        """
        observations: List[str] = []
        for node_id in assessment.removed_gates:
            observations.append(f"the `human_approval` gate {node_id} is gone")
        for node_id in assessment.removed_routers:
            observations.append(f"the `condition` node {node_id} is gone")
        for key in assessment.widened_permissions:
            observations.append(f"{key} grants what it did not grant before")
        if observations:
            only_routers = bool(assessment.removed_routers
                                and not assessment.removed_gates
                                and not assessment.widened_permissions)
            return {
                "status": "violated",
                # A removed `condition` alone is a proxy: the contract does not
                # say whether it was gating anything.
                "confidence": "medium" if only_routers
                else ("exact" if complete else "high"),
                "observations": [_short(item) for item in observations][:8],
                "limitations": [gap] if gap else [],
            }
        if not assessment.declares_permissions:
            return {
                "status": "unknown", "confidence": "unknown",
                "limitations": [_short(
                    f"`WorkflowNode` has no permissions field and no node "
                    f"declares a `config.{PERMISSIONS_KEY}`; the gates were "
                    f"compared and none was removed, but a gate is not a "
                    f"permission and this row would otherwise say we looked "
                    f"where we did not")],
            }
        if not complete:
            return {"status": "unknown", "confidence": "unknown",
                    "limitations": [gap or "the comparison was not complete"]}
        return {
            "status": "preserved", "confidence": "exact",
            "observations": [
                _short(f"every `config.{PERMISSIONS_KEY}` parameter present in "
                       f"the target was already granted in the source"),
                _short(f"the source's {len(assessment.source_gates)} gate node(s) "
                       f"are all still in the target"),
            ],
            "limitations": [_short(
                f"only a `config.{PERMISSIONS_KEY}` the caller supplied is "
                f"visible here; a grant expressed anywhere else in the config "
                f"was not compared")],
        }

    def _network_result(self, assessment: _Assessment, *, gap: str) -> Dict[str, Any]:
        """§14's "proveedor externo donde antes era local". Never `preserved`.

        A provider NAME is not a host. A `deliver` node's destination, a webhook
        URL and an allowlist all live in config this module does not interpret,
        so the strongest true statement available is "we saw one provider move
        from local to not-local" or "we did not see that", and only the first is
        a finding. Reporting the second as `preserved` would claim the target
        reaches nowhere new, which nothing here measured.
        """
        if assessment.externalised:
            return {
                "status": "violated", "confidence": "medium",
                "observations": [_short(f"{node_id} moved from a local provider "
                                        f"to one this module does not recognise "
                                        f"as local")
                                 for node_id in assessment.externalised][:8],
                "limitations": [_short(
                    f"`local` is decided by this module's `LOCAL_PROVIDERS` list "
                    f"over a `config.{PROVIDER_KEY}` the caller supplied; nothing "
                    f"here resolved a host")] + ([gap] if gap else []),
            }
        detail = (f"no node declares a `config.{PROVIDER_KEY}`"
                  if not assessment.declares_provider
                  else f"no declared `config.{PROVIDER_KEY}` moved away from a "
                       f"name in `LOCAL_PROVIDERS`")
        return {
            "status": "unknown", "confidence": "unknown",
            "limitations": [_short(
                f"`WorkflowNode` has no provider field and this adapter opens no "
                f"socket; {detail}, and a provider name is not the set of hosts "
                f"a workflow reaches")],
        }

    def _secrets_result(self, assessment: _Assessment, *, gap: str) -> Dict[str, Any]:
        """§14's "secreto nuevo". Violated on a declared list that grew, else unknown.

        Same shape as the `code` adapter's answer for this id, and for the same
        reason: no credential scan runs here, so a key pasted into a prompt or a
        URL would not appear in any element this module builds. `preserved`
        would be a claim about every config value, and this looked at one key.
        """
        if assessment.new_secrets:
            return {
                "status": "violated", "confidence": "exact",
                "observations": [_short(f"{key} names a secret the source did not")
                                 for key in assessment.new_secrets][:8],
                "limitations": [gap] if gap else [],
            }
        return {
            "status": "unknown", "confidence": "unknown",
            "limitations": [_short(
                f"no credential scan runs in this adapter; it compares a "
                f"`config.{SECRETS_KEY}` list when the caller declares one, and "
                f"a key added inside any other config value would not appear in "
                f"any element it builds")],
        }

    def _budget_result(self, source: Snapshot, target: Snapshot) -> Dict[str, Any]:
        """Whether the comparison stayed inside its budget. Exact, and ours.

        A claim about our own two snapshots and not about the workflow: either
        they carry `truncated` or they do not, and there is no margin in reading
        a boolean this module set itself.
        """
        if source.truncated or target.truncated:
            excluded = _unique(list(source.excluded) + list(target.excluded))
            return {
                "status": "violated", "confidence": "exact",
                "observations": [_short(item) for item in excluded][:8]
                                or ["a snapshot was truncated"],
            }
        return {
            "status": "preserved", "confidence": "exact",
            "observations": [_short(
                f"neither snapshot was truncated: {len(source.elements)} source "
                f"and {len(target.elements)} target elements were addressed "
                f"within the budget")],
        }

    def _unanswered(self, invariant: Any) -> Dict[str, Any]:
        """Every invariant this adapter has no method for. Always `unknown`.

        The limitation names the §14 properties that are NOT fields of
        `WorkflowNode` -- postconditions, rollback, timeouts -- because those
        are the ones a caller is most likely to have asked about, and "we have
        no method for a `behavior` property" without them reads as a shrug where
        a specific, checkable gap belongs.
        """
        return {
            "status": "unknown", "confidence": "unknown",
            "limitations": [_short(
                f"this adapter has no method for a `{invariant.klass}` property; "
                f"it compares node types, dependencies, config parameters, "
                f"retries and effects, and `WorkflowNode` declares no "
                f"postcondition, no rollback and no timeout for it to read")],
        }


# -- module-level readers used by the adapter ------------------------------


def _signature(element: Element) -> str:
    """What makes two elements the same element. Hash first, then value.

    Hash first because a content digest is identity and a rendered value is a
    description; a `node:` element carries both, and comparing it on the value
    -- the node type -- would report a rewritten node as unchanged.
    """
    return element.hash or element.value


def _addressed_ratio(snapshot: Snapshot) -> float:
    """How much of this end the budget let the snapshot address, 0..1.

    Read from the snapshot's own detail rather than recomputed, so the number in
    the coverage is the number the snapshot reported. A snapshot with nothing
    addressable counts as fully addressed: nothing was left out of it.
    """
    detail = dict(snapshot.detail or {})
    addressable = int(detail.get("addressable") or 0)
    addressed = int(detail.get("addressed") or 0)
    if addressable <= 0:
        return 1.0
    return max(0.0, min(1.0, round(addressed / addressable, 4)))


def _gap(source: Snapshot, target: Snapshot) -> str:
    """The one sentence naming why a check could not be complete, or `""`.

    A node the CONTRACT refused is deliberately not a gap: its fields were read
    field by field and the refusal is on the element, so the comparison saw it.
    A node with no readable id, and a budget that cut a snapshot, are gaps --
    those are addresses nobody looked at, and the part nobody looked at is not
    evidence that nothing happened there.
    """
    if source.truncated or target.truncated:
        return ("a budget cut at least one snapshot, so the edges and parameters "
                "nobody addressed are not evidence that nothing happened there")
    skipped = _unique(list(source.excluded) + list(target.excluded))
    if skipped:
        return _short(f"part of a definition could not be addressed: "
                      f"{'; '.join(skipped[:3])}")
    return ""


#: How `registry.py` finds this adapter. A module-level factory and NOT an entry
#: in a tuple somewhere else: `state_mirror/adapters/__init__.py` records what
#: the tuple cost -- five correct adapters that did not exist as far as the
#: running system was concerned, with green tests and no warning, because nobody
#: added them to it. Writing this line IS the registration.
ADAPTER_FACTORY = WorkflowAdapter
