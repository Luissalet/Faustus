"""Two skill manifests, compared on what the capability is allowed to touch.

Section 14, the skill half. `contracts/skill.py` opens with the masterplan's
first non-negotiable -- policy precedes the tool, a skill never gets disk,
network, keys or the host by virtue of being installed -- and `Permissions` is
where that stops being a slogan: **every field defaults to deny**. That single
sentence from its docstring is what makes this adapter able to say `preserved`
at all. "The manifest does not declare a network" is not a gap here; it is a
declaration that there is no network, because the contract gives the field a
deny default rather than leaving it absent.

Five rules, each with the failure it prevents:

* **Widening is blocking; narrowing is not.** `network` false -> true, an
  addition to `network_allowlist`, `secrets` or `backends`, `host_access` going
  true, and `filesystem` reaching further are §14's blocking rows. The reverse
  of each is a manifest asking for less, and filing that as blocking would
  teach a reader to skip these rows -- after which the blocking ones are not
  read either.

* **Withdrawing a required approval IS widening a permission.** It reads like
  less -- one fewer line in the manifest -- and it means the skill may now do
  unattended what needed a person. It is filed against
  `security.permissions_not_widened`, and it is the case
  `tests/test_delta_engine_workflow.py` proves.

* **`effective_approvals()` decides, not `approval_required_when`.**
  `SkillManifest.implied_approvals()` exists precisely so a manifest that asks
  for the network and forgets to list `network` under `approval.required_when`
  still gets the card. So a trigger that leaves the DECLARED list while the
  permission behind it stays is reported `modified` and is not blocking -- the
  card still appears -- and a trigger that leaves the EFFECTIVE set is
  blocking. Treating the two alike would file a manifest that changed nothing
  operational as a security regression.

* **`filesystem` is ranked, not compared for equality.** §14's wording is
  "`workspace` -> anything else", and `none` is NARROWER than `workspace`, so
  the order used here is `none < workspace < project` and only an increase is a
  violation. This is a deliberate departure from the literal wording, written
  down rather than assumed: a skill that gives up filesystem access entirely
  must not be reported as having widened it.

* **Nothing here was executed.** Behavioural coverage is 0.0 and is DECLARED,
  so `coverage.sufficient(..., dimensions=("behavioral",))` refuses to rest a
  claim about what the skill DOES on a reading of what it asks for.

WHAT IS NOT CHECKABLE HERE, named rather than left to be discovered: a
credential pasted into a description (`Permissions.secrets` holds NAMES, never
values, so there is nothing to scan); whether a declared backend is reachable;
whether a declared network allowlist is what the code actually calls. Memory
scopes are compared and reported, but widening `memory.write_scopes` is NOT in
§14's default blocking list and this adapter does not add it to one --
`MemoryPolicy` already refuses a write to `user`, which is the escalation that
list would have been for.

Revisions reach this adapter through `sources.resolve`. A `literal` holding
`SkillManifest.to_dict()` is readable; a revision of kind `skill` is not,
because `sources.RESOLVERS` has no reader for the skill store and that module is
not this one's to change. The refusal carries the resolver's own reason.

`registry.py` finds this module by the module-level `ADAPTER_FACTORY` at the
bottom. There is no list to add it to.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.contracts.base import ContractError
from src.contracts.skill import Permissions, SkillManifest
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
    "SkillAdapter",
    "ADAPTER_FACTORY",
    "EFFECTS_INVARIANT",
    "PERMISSIONS_INVARIANT",
    "NETWORK_INVARIANT",
    "SECRETS_INVARIANT",
    "FILESYSTEM_REACH",
    "LIST_PERMISSIONS",
    "FLAG_PERMISSIONS",
    "META_FIELDS",
]


# -- the invariants this adapter can be asked about ------------------------
#
# Every id is one `invariants.py` already declares: the three
# `SECURITY_INVARIANTS` plus `DOMAIN_DEFAULTS["skill"]`. Written as constants
# rather than reached out of those tuples by index, and asserted against the
# catalogue in `tests/test_delta_engine_workflow.py`, so a rename there fails a
# test here instead of producing findings that point at nothing.

#: §14's "nuevo efecto externo" for a capability: reach the manifest asks for
#: and did not ask for before. Class `security`, so a violation is `blocking`.
EFFECTS_INVARIANT = "skill.effects_not_widened"

#: §14's "permiso ampliado", including the approval a manifest stopped
#: requiring. Class `permissions`.
PERMISSIONS_INVARIANT = "security.permissions_not_widened"

#: `permissions.network` and `permissions.network_allowlist`.
NETWORK_INVARIANT = "security.network_not_widened"

#: `permissions.secrets` -- the NAMES a skill asks for, never a value.
SECRETS_INVARIANT = "security.secrets_not_exposed"

#: How far each `filesystem` setting reaches. `Permissions.filesystem` is
#: `workspace | project | none` and this is the only ordering of the three that
#: is true: `none` grants nothing, `workspace` grants the run's own directory,
#: `project` grants everything the project owns. §14 words the blocking case as
#: "`workspace` -> anything else"; taken literally that would make giving up
#: filesystem access a violation, so an INCREASE in this rank is the rule and
#: the departure is named in the module docstring.
FILESYSTEM_REACH: Dict[str, int] = {"none": 0, "workspace": 1, "project": 2}

#: Permission fields whose value is a list of names. An ADDITION to any of them
#: is a §14 blocking change; a removal is the manifest asking for less.
LIST_PERMISSIONS: Tuple[str, ...] = ("network_allowlist", "secrets", "backends")

#: Permission fields that are a plain deny-by-default flag. `False -> True` is
#: the widening; the reverse is not.
FLAG_PERMISSIONS: Tuple[str, ...] = ("network", "host_access")

#: The manifest fields that become `meta:` elements. `fingerprint` is included
#: because `SkillManifest.fingerprint()` is the identity of the CONTRACT -- two
#: manifests differing only in `source` or `description` share it -- which makes
#: one element answer "is this the same promise" without a reader having to
#: compare eight others.
META_FIELDS: Tuple[str, ...] = (
    "id", "version", "title", "description", "family", "tags", "source",
)

#: How many element keys a coverage lists. `Coverage.regions_analyzed` accepts
#: 1024; the cut is made below it and REPORTED, because a list silently clipped
#: at the contract's ceiling reads as the complete set of regions examined.
MAX_LISTED_REGIONS = 512

#: How long a rendered value may be. `Element.value` is documented as a SHORT
#: rendering; a description long enough to hold a page of prose would make a
#: snapshot as expensive as the manifest it describes.
MAX_VALUE_CHARS = 300


# -- small helpers ---------------------------------------------------------


def _short(value: Any, limit: int = 480) -> str:
    """One line, bounded. The contracts cap observations and limitations at 500."""
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _unique(values: Sequence[Any]) -> Tuple[str, ...]:
    """Order-preserving de-duplication for lists this module CONCATENATES.

    Same rule as `coverage._unique`: `Coverage` and `DeltaAssertion` both reject
    a repeated entry, and the repeat here is an artifact of joining two
    snapshots' notes rather than a contradiction inside either of them.
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


def _entries(element: Optional[Element]) -> Tuple[str, ...]:
    """The list a `permission:` element carries, or `()`.

    Read off `detail` rather than split back out of `value`: a host name may
    contain the separator the rendering uses, and a comparison that recovered
    the list by splitting a string would invent or lose an entry exactly where
    the entry is a network destination.
    """
    if element is None:
        return ()
    raw = dict(element.detail or {}).get("entries")
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(str(item) for item in raw)


def _flag(element: Optional[Element]) -> bool:
    """The boolean a `permission:` flag element carries. Absent reads as deny.

    Deny, not `None`: `Permissions` defaults every field to deny, so an element
    that is not there means the manifest did not ask -- which under this
    contract is the same as asking for nothing.
    """
    if element is None:
        return False
    return bool(dict(element.detail or {}).get("value"))


def _reach(element: Optional[Element]) -> int:
    """`filesystem` as a rank. An unknown word ranks as `workspace`, the default.

    Unknown cannot happen through `Permissions.parse` -- `one_of` closes the
    choice -- so this is the impossible case, and ranking it at the contract's
    own default is the answer that cannot manufacture a widening out of a value
    nobody can produce.
    """
    if element is None:
        return FILESYSTEM_REACH["workspace"]
    return FILESYSTEM_REACH.get(str(element.value or ""), FILESYSTEM_REACH["workspace"])


# -- addressing ------------------------------------------------------------


def _permission_elements(permissions: Permissions) -> List[Element]:
    """One element per `Permissions` field. All eight, always.

    All eight and never only the ones that are set, because `Permissions` is
    deny by default in every field: an element carrying `network: false` is a
    DECLARATION that there is no network, and omitting it would turn that
    declaration back into a silence -- which is the difference between this
    adapter being able to say `preserved` and never being able to.
    """
    out: List[Element] = []
    for name in FLAG_PERMISSIONS:
        value = bool(getattr(permissions, name))
        out.append(Element(key=f"permission:{name}", kind="permission",
                           value="true" if value else "false",
                           detail={"value": value}))
    for name in LIST_PERMISSIONS:
        entries = sorted(str(item) for item in getattr(permissions, name))
        out.append(Element(key=f"permission:{name}", kind="permission",
                           value=(", ".join(entries) or "<none>")[:MAX_VALUE_CHARS],
                           detail={"entries": entries}))
    out.append(Element(
        key="permission:filesystem", kind="permission",
        value=permissions.filesystem,
        detail={"reach": FILESYSTEM_REACH.get(permissions.filesystem, 1)}))
    for name in ("max_seconds", "max_cost_units"):
        raw = getattr(permissions, name)
        out.append(Element(key=f"permission:{name}", kind="permission",
                           value="<unset>" if raw is None else str(raw),
                           detail={"value": raw}))
    return out


def _approval_elements(manifest: SkillManifest) -> List[Element]:
    """One element per EFFECTIVE approval trigger, saying which kind it is.

    `effective_approvals()` is the union of what the manifest declared and what
    `implied_approvals()` derives from the permissions it asked for. Addressing
    the effective set is what makes "a trigger was withdrawn" mean the card
    stopped appearing, rather than merely that a line moved; `value` keeps the
    distinction so a reader can still see which of the two a trigger is.
    """
    declared = set(manifest.approval_required_when)
    implied = set(manifest.implied_approvals())
    out: List[Element] = []
    for trigger in sorted(set(manifest.effective_approvals())):
        out.append(Element(
            key=f"approval:{trigger}", kind="approval",
            value="declared" if trigger in declared else "implied",
            detail={"declared": trigger in declared, "implied": trigger in implied}))
    return out


def _manifest_elements(manifest: SkillManifest) -> Tuple[List[Element], List[Element]]:
    """`(anchors, rest)` for one manifest. §14's six element kinds, and no seventh.

    The anchors -- `permission:` and `approval:` -- are what every blocking check
    reads, and `_budgeted` never drops them: a budget that removed
    `permission:network` would make a skill that asks for the internet look like
    one that does not.
    """
    anchors = _permission_elements(manifest.permissions) + _approval_elements(manifest)

    body = manifest.to_dict()
    rest: List[Element] = []
    for field in META_FIELDS:
        raw = body.get(field)
        rendered = (", ".join(str(item) for item in raw)
                    if isinstance(raw, (list, tuple)) else str(raw or ""))
        rest.append(Element(key=f"meta:{field}", kind="meta",
                            value=rendered[:MAX_VALUE_CHARS]))
    rest.append(Element(
        key="meta:fingerprint", kind="meta", hash=manifest.fingerprint(),
        value="contract fingerprint",
        detail={"note": "identity of the promise: id, version, inputs, outputs, "
                        "memory scopes, permissions and effective approvals"}))
    for name, spec in manifest.inputs:
        rest.append(Element(key=f"input:{name}", kind="input", value=str(spec)))
    for name, spec in manifest.outputs:
        rest.append(Element(key=f"output:{name}", kind="output", value=str(spec)))
    for name in ("read_scopes", "write_scopes"):
        entries = sorted(str(item) for item in getattr(manifest.memory, name))
        rest.append(Element(key=f"memory:{name}", kind="memory",
                            value=(", ".join(entries) or "<none>"),
                            detail={"entries": entries}))
    return anchors, rest


def _signature(element: Element) -> str:
    """What makes two elements the same element. Hash first, then value."""
    return element.hash or element.value


# -- what the two snapshots amount to --------------------------------------


@dataclass(frozen=True)
class _Assessment:
    """§14's skill questions, answered once over two snapshots.

    Computed in one place and read by both the findings and the invariant rows,
    so the two cannot disagree about whether the network was opened. Derived
    from the ELEMENTS, because the elements are what a budget may have cut and
    an assessment over the manifest itself would describe a comparison that did
    not happen.
    """

    network_opened: bool = False
    new_hosts: Tuple[str, ...] = ()
    new_secrets: Tuple[str, ...] = ()
    new_backends: Tuple[str, ...] = ()
    host_access_opened: bool = False
    filesystem_widened: str = ""
    withdrawn_approvals: Tuple[str, ...] = ()
    weakened_approvals: Tuple[str, ...] = ()
    target_network: bool = False
    target_host_access: bool = False
    target_filesystem: str = ""
    target_hosts: Tuple[str, ...] = ()
    target_secrets: Tuple[str, ...] = ()
    target_approvals: Tuple[str, ...] = ()

    @property
    def reach_widened(self) -> Tuple[str, ...]:
        """Everything that made the capability able to touch more than before."""
        out: List[str] = []
        if self.network_opened:
            out.append("`permissions.network` went from false to true")
        for host in self.new_hosts:
            out.append(f"`network_allowlist` gained {host}")
        for backend in self.new_backends:
            out.append(f"`backends` gained {backend}")
        if self.host_access_opened:
            out.append("`permissions.host_access` went from false to true")
        if self.filesystem_widened:
            out.append(f"`filesystem` widened: {self.filesystem_widened}")
        return tuple(out)

    @property
    def permissions_widened(self) -> Tuple[str, ...]:
        """Reach, plus the two widenings that are not about reach.

        A new secret NAME and a withdrawn approval both grant the skill
        something it did not have, and neither of them makes it touch a new
        host -- which is why they are here and not in `reach_widened`.
        """
        out = list(self.reach_widened)
        for name in self.new_secrets:
            out.append(f"`secrets` gained {name}")
        for trigger in self.withdrawn_approvals:
            out.append(f"the approval trigger {trigger!r} is no longer required")
        return tuple(out)


def _assess(source: Snapshot, target: Snapshot) -> _Assessment:
    """Every §14 skill question this adapter can answer, over two snapshots."""
    left, right = source.index(), target.index()

    def _perm(index: Mapping[str, Element], name: str) -> Optional[Element]:
        return index.get(f"permission:{name}")

    new_lists: Dict[str, Tuple[str, ...]] = {}
    for name in LIST_PERMISSIONS:
        before = set(_entries(_perm(left, name)))
        after = _entries(_perm(right, name))
        new_lists[name] = tuple(item for item in after if item not in before)

    source_reach = _reach(_perm(left, "filesystem"))
    target_element = _perm(right, "filesystem")
    target_reach = _reach(target_element)
    widened = ""
    if target_reach > source_reach:
        before_value = (_perm(left, "filesystem").value
                        if _perm(left, "filesystem") is not None else "<absent>")
        after_value = target_element.value if target_element is not None else "<absent>"
        widened = f"{before_value} -> {after_value}"

    source_approvals = {key for key in left if key.startswith("approval:")}
    target_approvals = {key for key in right if key.startswith("approval:")}
    withdrawn = tuple(sorted(key.split(":", 1)[1]
                             for key in source_approvals - target_approvals))
    weakened = tuple(sorted(
        key.split(":", 1)[1] for key in source_approvals & target_approvals
        if left[key].value == "declared" and right[key].value == "implied"))

    return _Assessment(
        network_opened=(not _flag(_perm(left, "network"))
                        and _flag(_perm(right, "network"))),
        new_hosts=new_lists["network_allowlist"],
        new_secrets=new_lists["secrets"],
        new_backends=new_lists["backends"],
        host_access_opened=(not _flag(_perm(left, "host_access"))
                            and _flag(_perm(right, "host_access"))),
        filesystem_widened=widened,
        withdrawn_approvals=withdrawn,
        weakened_approvals=weakened,
        target_network=_flag(_perm(right, "network")),
        target_host_access=_flag(_perm(right, "host_access")),
        target_filesystem=(target_element.value if target_element is not None else ""),
        target_hosts=_entries(_perm(right, "network_allowlist")),
        target_secrets=_entries(_perm(right, "secrets")),
        target_approvals=tuple(sorted(key.split(":", 1)[1]
                                      for key in target_approvals)),
    )


# -- the adapter -----------------------------------------------------------


class SkillAdapter:
    """The `skill` domain. Implements the `DeltaAdapter` Protocol.

    One revision shape reaches `snapshot` today: a `literal` holding the JSON of
    a manifest, which is what `sources.stash(manifest.to_dict())` returns. A
    revision of kind `skill` is refused by `sources.resolve` with that module's
    own reason, and the refusal travels into the snapshot rather than becoming
    an exception -- "the skill store has no reader here" is an ANSWER, and
    raising would discard the other end's work and say nothing about which end
    failed.
    """

    domain = "skill"
    version = "1"

    def available(self) -> bool:
        """Always true. `src.contracts.skill` is in this repository.

        There is no parser to install and no server to reach, so a `False` here
        could only ever be a lie -- and `registry.adapter_for` turns an
        unavailable adapter into a refused comparison, which is a much larger
        answer than this adapter has any reason to give.
        """
        return True

    # -- reading one end ---------------------------------------------------

    def snapshot(self, revision: RevisionRef, *, scope: Scope) -> Snapshot:
        """One manifest as permissions, approvals, inputs, outputs, memory, meta.

        A payload the contract refuses is `readable=False` with the contract's
        own message, and that is the right answer rather than a defensive read:
        unlike a workflow node -- where the refusal IS the change §14 names --
        a manifest that does not parse grants nothing, because nothing accepted
        it. `SkillManifest.parse` also refuses a manifest from a newer build
        rather than reading it as if the unknown fields were absent, and a
        comparison against half a manifest would report permissions it could
        not see as permissions that are not there.
        """
        resolved = sources.resolve(revision, scope=scope)
        if not resolved.readable:
            return unreadable(revision, resolved.reason, tier="parser")
        try:
            payload = resolved.mapping()
        except sources.SourceError as exc:
            return unreadable(revision, _short(exc), tier="parser")
        try:
            manifest = SkillManifest.parse(payload, "skill")
        except ContractError as exc:
            return unreadable(revision, _short(
                f"this payload is not a manifest this build accepts: {exc}"),
                tier="parser")

        anchors, rest = _manifest_elements(manifest)
        elements, cut = self._budgeted(anchors, rest, scope=scope)
        return Snapshot(
            revision=revision,
            elements=elements,
            readable=True,
            # `parser`: every element came out of `SkillManifest.parse`, which
            # validated the type language, the permission vocabulary and the
            # approval triggers. Nothing here was guessed from a pattern.
            tier="parser",
            excluded=tuple(_unique([cut] if cut else [])),
            notes=(),
            truncated=bool(cut),
            detail={
                "skill_id": manifest.id,
                "skill_version": manifest.version,
                "fingerprint": manifest.fingerprint(),
                # Both numbers, never one: `addressed` alone would let a
                # truncated snapshot report full structural coverage.
                "addressable": len(anchors) + len(rest),
                "addressed": len(elements),
            },
        )

    def _budgeted(self, anchors: List[Element], rest: List[Element], *,
                  scope: Scope) -> Tuple[Tuple[Element, ...], str]:
        """The elements, cut to `budget.max_elements`, and the sentence for it.

        `permission:` and `approval:` are spent first and never dropped. What a
        cut removes is metadata, the type signature and the memory scopes, in
        address order, so two runs over one revision produce the same snapshot.
        """
        limit = scope.budget.max_elements
        ordered = (sorted(anchors, key=lambda item: item.key)
                   + sorted(rest, key=lambda item: item.key))
        if limit is None or len(ordered) <= limit:
            return tuple(ordered), ""
        kept = ordered[:max(limit, len(anchors))]
        dropped = len(ordered) - len(kept)
        return tuple(kept), (
            f"{dropped} metadata, signature and memory element(s) were not "
            f"addressed; `budget.max_elements` ({limit}) stopped the snapshot "
            f"and nothing past that point was compared")

    # -- comparing two ends ------------------------------------------------

    def compare(self, source: Snapshot, target: Snapshot, *,
                scope: Scope) -> Extraction:
        """What differs between two manifests, and which of it widens reach.

        Elements are matched by address. A manifest's addresses are field names
        from closed vocabularies -- `Permissions._KEYS`, `APPROVAL_TRIGGERS`,
        `MEMORY_SCOPES` -- so an identical address is identity here in the
        strongest sense available, and there is nothing for a rename detector to
        do.

        An unreadable end produces no findings rather than a `missing` for every
        permission: "we could not read it" and "it asks for nothing now" are
        opposite facts, and a list of removals says the second.
        """
        readable = source.readable and target.readable
        limitations = [
            "this adapter compares a manifest: what the skill ASKS for. Nothing "
            "was executed, so nothing here is evidence about what it does",
            "`Permissions.secrets` holds names and never values, so a comparison "
            "of it says which credentials are requested and nothing about any "
            "credential's content",
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
        """One observation about one field. Never a classification."""
        if before is None and after is None:  # pragma: no cover - union of keys
            return None
        common: Dict[str, Any] = {
            "path": key,
            "method": "manifest_field",
            "tier": confidence_mod.tier_of("manifest_field"),
            "confidence": "exact",
        }
        kind = key.split(":", 1)[0]
        if kind == "permission":
            return self._permission_finding(key, before, after, common)
        if kind == "approval":
            return self._approval_finding(key, before, after, common)
        return self._plain_finding(key, before, after, common)

    def _plain_finding(self, key: str, before: Optional[Element],
                       after: Optional[Element],
                       common: Dict[str, Any]) -> Finding:
        """`added`/`missing`/`modified`/`unchanged`, with nothing attached.

        `unchanged` is emitted rather than skipped: it says we looked, which is
        what lets a later layer report `preserved` with something behind it.
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

    def _permission_finding(self, key: str, before: Optional[Element],
                            after: Optional[Element],
                            common: Dict[str, Any]) -> Finding:
        """A permission field, and the one direction that is blocking.

        The invariant references differ per field because they are answers to
        different questions: a new host is a network question AND a permission
        question, a new secret is a secrets question AND a permission question,
        and a wider filesystem is neither of those and is still reach. Attaching
        one blanket reference to all of them would make every row read the same
        and the specific ones unfindable.
        """
        base = self._plain_finding(key, before, after, common)
        name = key.split(":", 1)[1]

        if name in FLAG_PERMISSIONS and not _flag(before) and _flag(after):
            refs = ((NETWORK_INVARIANT, PERMISSIONS_INVARIANT, EFFECTS_INVARIANT)
                    if name == "network"
                    else (PERMISSIONS_INVARIANT, EFFECTS_INVARIANT))
            return self._widening(base, refs, _short(
                f"`permissions.{name}` went from false to true; the field is deny "
                f"by default, so the source declared it did not want this"))

        if name in LIST_PERMISSIONS:
            gained = [item for item in _entries(after)
                      if item not in set(_entries(before))]
            if not gained:
                return base
            refs = {
                "network_allowlist": (NETWORK_INVARIANT, PERMISSIONS_INVARIANT,
                                      EFFECTS_INVARIANT),
                "secrets": (SECRETS_INVARIANT, PERMISSIONS_INVARIANT),
                "backends": (EFFECTS_INVARIANT, PERMISSIONS_INVARIANT),
            }[name]
            return self._widening(base, refs, _short(
                f"`permissions.{name}` gained {', '.join(sorted(gained))}; every "
                f"entry the source did not list is reach it did not have"))

        if name == "filesystem" and _reach(after) > _reach(before):
            return self._widening(
                base, (PERMISSIONS_INVARIANT, EFFECTS_INVARIANT), _short(
                    f"`permissions.filesystem` widened from "
                    f"{before.value if before else '<absent>'} to "
                    f"{after.value if after else '<absent>'}; the order is "
                    f"none < workspace < project"))
        return base

    def _approval_finding(self, key: str, before: Optional[Element],
                          after: Optional[Element],
                          common: Dict[str, Any]) -> Finding:
        """A required approval, and why only one of its two losses is blocking.

        Gone from the EFFECTIVE set means the card stops appearing and the skill
        may now act unattended -- §14's "gate removido", filed as a permission
        widening because that is what it is. Gone from the DECLARED list while
        the permission behind it remains is `modified`: `implied_approvals()`
        puts the trigger back, the card still appears, and calling that blocking
        would file a manifest that changed nothing operational as a regression.
        """
        base = self._plain_finding(key, before, after, common)
        trigger = key.split(":", 1)[1]
        if base.operation == "missing":
            return self._widening(base, (PERMISSIONS_INVARIANT,), _short(
                f"the approval trigger {trigger!r} is no longer required, by "
                f"declaration or by implication; removing a mandatory approval "
                f"is widening a permission, however much less it looks like one"))
        if (before is not None and after is not None
                and before.value == "declared" and after.value == "implied"):
            return Finding(
                path=base.path, operation="modified", before=base.before,
                after=base.after, element_kind=base.element_kind,
                method=base.method, tier=base.tier, confidence="exact",
                limitations=(
                    "the trigger left `approval.required_when` and is still "
                    "derived by `SkillManifest.implied_approvals()` from the "
                    "permission behind it, so the approval card still appears",),
                detail=_short(f"{trigger!r} is now implied rather than declared"))
        return base

    def _widening(self, base: Finding, refs: Tuple[str, ...],
                  detail: str) -> Finding:
        """One blocking row, built from the observation `_plain_finding` made.

        The operation is never redecided here: what was seen is what was seen,
        and this method only says which invariants it bears on. A branch that
        re-derived the operation could report `modified` where the field was
        `added`, and the two mean different things to the classifier.
        """
        return Finding(
            path=base.path, operation=base.operation, before=base.before,
            after=base.after, element_kind=base.element_kind,
            method=base.method, tier=base.tier, confidence="exact",
            invariant_refs=refs, detail=detail)

    def _coverage(self, source: Snapshot, target: Snapshot, *,
                  regions: Tuple[str, ...], readable: bool) -> Coverage:
        """How much was compared, on the two axes this adapter can speak to.

        `structural` is the share of addressable fields the budget let through.
        `behavioral` is 0.0 and DECLARED rather than omitted: this adapter did
        not fail to observe behaviour, it never runs anything, and that zero is
        what makes `coverage.sufficient(..., dimensions=("behavioral",))` refuse
        to rest a claim about what a skill does on what it asks for.
        """
        structural = 0.0
        if readable:
            structural = min(_addressed_ratio(source), _addressed_ratio(target))
        listed = regions[:MAX_LISTED_REGIONS]
        notes = [
            "nothing was executed: a manifest is a claim, and this compares two "
            "claims",
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
        """One result per declared invariant, and four ids this adapter answers.

        `preserved` is reachable for all four, which is unusual in this package
        and is earned by one sentence in `contracts/skill.py`: **`Permissions`
        is deny by default in every field**. A manifest that says nothing about
        the network is not a manifest nobody asked about; it is a manifest that
        declared no network. That is what makes "not declared" safe here rather
        than a hole, and it is why this adapter can say a permission was held
        still without having run anything.

        Everything outside those four is `unknown` with the gap named.
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
                payload.update(self._reach_result(assessment, complete=complete,
                                                  gap=gap))
            elif invariant.id == PERMISSIONS_INVARIANT:
                payload.update(self._permissions_result(assessment,
                                                        complete=complete, gap=gap))
            elif invariant.id == NETWORK_INVARIANT:
                payload.update(self._network_result(assessment, complete=complete,
                                                    gap=gap))
            elif invariant.id == SECRETS_INVARIANT:
                payload.update(self._secrets_result(assessment, complete=complete,
                                                    gap=gap))
            elif invariant.klass == "budget":
                payload.update(self._budget_result(source, target))
            else:
                payload.update({
                    "status": "unknown", "confidence": "unknown",
                    "limitations": [_short(
                        f"this adapter has no method for a `{invariant.klass}` "
                        f"property; it compares the permissions, approvals, "
                        f"inputs, outputs, memory scopes and metadata a manifest "
                        f"declares, and nothing here observed that property")],
                })
            results.append(InvariantResult.parse(
                payload, f"invariant_result[{invariant.id}]"))
        return tuple(results)

    def _violation(self, observations: Sequence[str], *, complete: bool,
                   gap: str) -> Dict[str, Any]:
        """A violated row. `exact` when the comparison was whole, `high` when not.

        `high` rather than `exact` on an incomplete comparison for the `code`
        adapter's reason: the violation WAS seen, and the set it was seen in was
        not the whole set. Lowering it further would under-claim a fact that a
        closed vocabulary decided.
        """
        return {
            "status": "violated",
            "confidence": "exact" if complete else "high",
            "observations": [_short(item) for item in observations][:8],
            "limitations": [gap] if gap else [],
        }

    def _reach_result(self, assessment: _Assessment, *, complete: bool,
                      gap: str) -> Dict[str, Any]:
        """`skill.effects_not_widened`: write, spawn, external call.

        A new secret NAME is deliberately not here. It is a permission widening
        and it is reported as one, but a credential the skill may read is not by
        itself a place the skill can reach, and folding the two would make this
        row fire for a change that touches nothing outside.
        """
        widened = assessment.reach_widened
        if widened:
            return self._violation(widened, complete=complete, gap=gap)
        if not complete:
            return {"status": "unknown", "confidence": "unknown",
                    "limitations": [gap or "the comparison was not complete"]}
        return {
            "status": "preserved", "confidence": "exact",
            "observations": [
                _short(f"the target declares network={assessment.target_network}, "
                       f"host_access={assessment.target_host_access}, "
                       f"filesystem={assessment.target_filesystem!r} and "
                       f"{len(assessment.target_hosts)} allowlisted host(s), and "
                       f"none of the four reaches further than the source"),
                "`Permissions` is deny by default in every field, so a field the "
                "target does not declare is a declaration that it wants nothing "
                "there rather than a field nobody looked at",
            ],
        }

    def _permissions_result(self, assessment: _Assessment, *, complete: bool,
                            gap: str) -> Dict[str, Any]:
        """`security.permissions_not_widened`: reach, plus secrets and approvals."""
        widened = assessment.permissions_widened
        if widened:
            return self._violation(widened, complete=complete, gap=gap)
        if not complete:
            return {"status": "unknown", "confidence": "unknown",
                    "limitations": [gap or "the comparison was not complete"]}
        observations = [
            _short(f"every one of the source's required approvals is still "
                   f"required in the target ({len(assessment.target_approvals)} "
                   f"effective trigger(s))"),
            _short(f"no permission field grants more than the source's: "
                   f"filesystem={assessment.target_filesystem!r}, "
                   f"{len(assessment.target_secrets)} secret name(s), "
                   f"host_access={assessment.target_host_access}"),
        ]
        limitations: List[str] = []
        if assessment.weakened_approvals:
            limitations.append(_short(
                f"{', '.join(assessment.weakened_approvals)} left "
                f"`approval.required_when` and is still derived by "
                f"`implied_approvals()`; the card appears, the declaration does "
                f"not"))
        return {
            "status": "preserved", "confidence": "exact",
            "observations": observations,
            "limitations": limitations,
        }

    def _network_result(self, assessment: _Assessment, *, complete: bool,
                        gap: str) -> Dict[str, Any]:
        """`security.network_not_widened`, over two declared fields.

        This is the one place in the delta engine where a network claim can be
        `preserved` at `exact`, and the reason is structural rather than
        clever: `Permissions.parse` refuses a `network_allowlist` while
        `network` is false, so the two fields cannot contradict each other, and
        both default to deny. The claim is about what the manifest ASKS for; the
        skill's code is not read here and that is said in the limitation.
        """
        observations = []
        if assessment.network_opened:
            observations.append("`permissions.network` went from false to true")
        for host in assessment.new_hosts:
            observations.append(f"`network_allowlist` gained {host}")
        if observations:
            return self._violation(observations, complete=complete, gap=gap)
        if not complete:
            return {"status": "unknown", "confidence": "unknown",
                    "limitations": [gap or "the comparison was not complete"]}
        return {
            "status": "preserved", "confidence": "exact",
            "observations": [
                _short(f"the target declares network={assessment.target_network} "
                       f"and an allowlist of {len(assessment.target_hosts)} "
                       f"host(s), none of which the source did not already list"),
                "`Permissions.parse` refuses an allowlist while `network` is "
                "false, so the two fields cannot disagree about whether the "
                "skill asked for a network",
            ],
            "limitations": [
                "this is what the manifest asks for; the skill's own code is not "
                "read here, and a call it makes outside its allowlist is a "
                "runtime failure rather than a manifest change"],
        }

    def _secrets_result(self, assessment: _Assessment, *, complete: bool,
                        gap: str) -> Dict[str, Any]:
        """`security.secrets_not_exposed`, over the NAMES a manifest asks for.

        `high` and not `exact` on the good news. `Permissions.secrets` holds
        names and never values -- "a manifest that carries a credential is a
        manifest that leaked one" -- so this row is a complete reading of which
        credentials are REQUESTED and says nothing about a credential that
        reached the target some other way. That is a limitation, and a
        limitation is exactly what separates `high` from identity.
        """
        if assessment.new_secrets:
            return self._violation(
                [f"`permissions.secrets` gained {name}"
                 for name in assessment.new_secrets], complete=complete, gap=gap)
        if not complete:
            return {"status": "unknown", "confidence": "unknown",
                    "limitations": [gap or "the comparison was not complete"]}
        return {
            "status": "preserved", "confidence": "high",
            "observations": [
                _short(f"the target asks for {len(assessment.target_secrets)} "
                       f"secret name(s) and every one of them is a name the "
                       f"source already asked for")],
            "limitations": [
                "`Permissions.secrets` holds names and never values, so this "
                "compares which credentials are requested; it is not a scan of "
                "the manifest for a literal credential"],
        }

    def _budget_result(self, source: Snapshot, target: Snapshot) -> Dict[str, Any]:
        """Whether the comparison stayed inside its budget. Exact, and ours."""
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


# -- module-level readers used by the adapter ------------------------------


def _addressed_ratio(snapshot: Snapshot) -> float:
    """How much of this end the budget let the snapshot address, 0..1.

    Read from the snapshot's own detail rather than recomputed, so the number in
    the coverage is the number the snapshot reported.
    """
    detail = dict(snapshot.detail or {})
    addressable = int(detail.get("addressable") or 0)
    addressed = int(detail.get("addressed") or 0)
    if addressable <= 0:
        return 1.0
    return max(0.0, min(1.0, round(addressed / addressable, 4)))


def _gap(source: Snapshot, target: Snapshot) -> str:
    """The one sentence naming why a check could not be complete, or `""`.

    Built once and reused by every row, so that four invariants that went
    unanswered for one reason say the same thing about it: three wordings of one
    gap read as three gaps.
    """
    if source.truncated or target.truncated:
        return ("a budget cut at least one snapshot, so the fields nobody "
                "addressed are not evidence that nothing was asked for there")
    return ""


#: How `registry.py` finds this adapter. A module-level factory and NOT an entry
#: in a tuple somewhere else: `state_mirror/adapters/__init__.py` records what
#: the tuple cost -- five correct adapters that did not exist as far as the
#: running system was concerned, with green tests and no warning, because nobody
#: added them to it. Writing this line IS the registration.
ADAPTER_FACTORY = SkillAdapter
