"""
agent_profiles/packs.py — a pack groups agents. It is not a new kind of agent.

§17 is one sentence long and easy to get wrong: *"packs declare dependencies
and compatibility, but every agent keeps its own permissions and tools."* The
tempting implementation is a pack that carries a `tools:` list "for the whole
team", or `defaults` that quietly include a permission rule, and the result is
a third place — after the definition and after the task — where an agent's
authority can change. That is the failure this file is built against: the
permission ladder of §3.4 has a fixed number of rungs, and a pack is not one of
them.

So the rule is structural rather than aspirational:

**An `AgentPack` has no field a permission could be written into.** No `tools`,
no `deny`, no `permission`, no work roots, no effects. A test reads the field
names back and fails if one ever appears.

**`defaults` is an allowlist, not a free mapping.** Only the keys in
:data:`PACK_DEFAULT_KEYS` are accepted — a completion mode, the profile
references, the operational ceilings. `tools: [...]` in a pack's defaults is
reported by :func:`validate_pack` as a problem naming the rule it breaks,
rather than being applied by whoever reads the mapping next.

**`resolve_pack` resolves; it does not merge.** It answers "which definitions
is this pack made of" by looking each slug up and handing back the definitions
themselves, untouched. The envelope of every member before and after is the
same object with the same contents, which is again a test rather than a
promise.

What a pack legitimately adds is the part §17 does want: a named, versioned
grouping (coordinator, workers, reviewer), the defaults a task may start from,
and a `requires` list of capabilities the group has to cover between them so a
pack that lost its only image agent says so instead of failing at run time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, fields as dataclass_fields
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.agent_profiles import catalog
from src.agent_profiles.builtin import profile_defs
from src.agent_profiles.contracts import CAPABILITIES, COMPLETION_MODES, ProfileError

logger = logging.getLogger(__name__)

__all__ = [
    "AgentPack",
    "BUILTIN_PACKS",
    "PACK_DEFAULT_KEYS",
    "PACK_FIELDS",
    "pack",
    "resolve_pack",
    "validate_pack",
]

#: The only keys a pack's `defaults` may carry. Every one of them is either a
#: depth (`completion_mode`), a reference to somebody else's versioned policy,
#: or a ceiling a run may lower. None of them can widen anything, which is the
#: property that keeps a pack out of the permission ladder of §3.4.
PACK_DEFAULT_KEYS: Sequence[str] = (
    "completion_mode",
    "verification_profile",
    "context_profile",
    "budget_profile",
    "collaboration_profile",
    "output_contract",
    "max_rounds",
    "timeout_s",
)

#: Keys that name authority. A pack that carries one of these is not a
#: grouping, it is a permission grant wearing a friendlier word.
_FORBIDDEN_DEFAULT_KEYS: Sequence[str] = (
    "tools", "deny", "permission", "permissions", "work_roots", "effects",
    "files", "model", "endpoint_id", "runner",
)


@dataclass(frozen=True)
class AgentPack:
    """A named group of definitions, and nothing else (§17).

    Not hashable in practice, because `defaults` is a mapping — which is the
    honest trade: a pack is a small configuration object that gets read and
    printed, never used as a dictionary key.
    """

    pack: str
    version: str
    coordinator: str
    workers: Tuple[str, ...]
    reviewer: str
    defaults: Mapping[str, Any]
    description: str
    requires: Tuple[str, ...] = ()

    def members(self) -> Tuple[str, ...]:
        """Every slug this pack names, coordinator first, deduplicated."""
        out: List[str] = []
        for slug in (self.coordinator,) + tuple(self.workers) + (self.reviewer,):
            name = str(slug or "").strip()
            if name and name not in out:
                out.append(name)
        return tuple(out)

    def to_dict(self) -> Dict[str, Any]:
        return {"pack": self.pack, "version": self.version, "coordinator": self.coordinator,
                "workers": list(self.workers), "reviewer": self.reviewer,
                "defaults": dict(self.defaults), "description": self.description,
                "requires": list(self.requires)}


def _pack(pack: str, version: str, *, coordinator: str, workers: Sequence[str], reviewer: str,
          defaults: Mapping[str, Any], description: str,
          requires: Sequence[str] = ()) -> AgentPack:
    return AgentPack(pack=pack, version=version, coordinator=coordinator,
                     workers=tuple(workers), reviewer=reviewer,
                     defaults=MappingProxyType(dict(defaults)), description=description,
                     requires=tuple(requires))


# ── the shipped packs (§17) ────────────────────────────────────────────────
#
# Every member is a slug that actually ships — the built-ins of
# `agent_profiles.builtin` plus the three in `agent_defs`. A pack naming an
# agent nobody has is a pack that fails at dispatch, which is why
# `validate_pack` checks membership and a test runs it over all of these.

BUILTIN_PACKS: Dict[str, AgentPack] = {
    "secure_feature_team": _pack(
        "secure_feature_team", "1.0.0",
        coordinator="planner",
        workers=("builder", "implementer"),
        reviewer="reviewer",
        defaults={"completion_mode": "professional",
                  "verification_profile": "full_delivery_v1"},
        description="Plan the work, build it finished, and review it adversarially before it lands.",
        requires=("code", "review", "security"),
    ),
    "research_team": _pack(
        "research_team", "1.0.0",
        coordinator="explorer",
        workers=("builder",),
        reviewer="reviewer",
        defaults={"completion_mode": "maximalist",
                  "context_profile": "research_primary_sources_v1",
                  "budget_profile": "deep_research_v1",
                  "output_contract": "research_report_v1"},
        description="Compare hypotheses, write the findings up, and have the claims checked.",
        requires=("research", "documents", "review"),
    ),
    "media_production": _pack(
        "media_production", "1.0.0",
        coordinator="creative-director",
        workers=("image-artist", "video-producer"),
        reviewer="reviewer",
        defaults={"completion_mode": "greedy",
                  "context_profile": "creative_reference_pack_v1",
                  "budget_profile": "creative_preview_v1"},
        description="Direct a brief, generate the assets, assemble the cut, review the result.",
        requires=("image", "video", "review"),
    ),
    "incident_response": _pack(
        "incident_response", "1.0.0",
        coordinator="incident-responder",
        workers=("implementer",),
        reviewer="reviewer",
        defaults={"completion_mode": "literal",
                  "verification_profile": "incident_containment_v1",
                  "context_profile": "incident_v1"},
        description="Contain the incident with a reversible change; the review comes after.",
        requires=("diagnostics", "code", "review"),
    ),
    "document_pipeline": _pack(
        "document_pipeline", "1.0.0",
        coordinator="planner",
        workers=("builder",),
        reviewer="reviewer",
        defaults={"completion_mode": "professional",
                  "verification_profile": "document_evidence_v1",
                  "context_profile": "document_evidence_v1"},
        description="Split a document job, produce it with its citations, and verify they resolve.",
        requires=("documents", "review"),
    ),
}


# ── looking one up ─────────────────────────────────────────────────────────

def pack(name: Any) -> Optional[AgentPack]:
    """One pack by name, or None. Use :func:`resolve_pack` when absence is a bug."""
    return BUILTIN_PACKS.get(str(name or "").strip())


def _catalogue(defs: Any = None) -> Dict[str, Any]:
    """The definitions to resolve members against.

    `None` means "what this build ships": the profile built-ins first, then
    `agent_defs`' own, so the two that were AUGMENTED (`reviewer`,
    `implementer`) resolve to the enriched version rather than to the plain
    one. A mapping or a sequence of definitions is taken as given, which is how
    a caller with a loaded catalogue — user files, repo files and all — avoids
    a second walk of the disk.
    """
    if defs is None:
        try:
            from src import agent_defs
            shipped = list(profile_defs()) + list(agent_defs.builtins())
        except Exception as exc:  # noqa: BLE001 - a broken catalogue is not a bad pack
            logger.warning("packs: built-in definitions unavailable: %s", exc)
            shipped = []
        index: Dict[str, Any] = {}
        for definition in shipped:
            index.setdefault(str(getattr(definition, "slug", "")), definition)
        index.pop("", None)
        return index
    if isinstance(defs, Mapping):
        return {str(k): v for k, v in defs.items() if str(k)}
    if hasattr(defs, "by_slug"):                    # an agent_defs.LoadResult
        return dict(defs.by_slug())
    return {str(getattr(d, "slug", "")): d for d in defs if str(getattr(d, "slug", ""))}


def validate_pack(p: AgentPack, *, defs: Any = None) -> List[str]:
    """Everything wrong with a pack, in sentences. Empty means it is sound.

    A list rather than an exception because the caller is usually showing a
    page or loading a directory of packs: one bad pack must not take the others
    with it, and "which of my five packs is broken, and how" is a question a
    raised error answers one problem at a time.

    What is checked:

    * the shape: a name, a version, a coordinator, at least one worker;
    * every member exists in the catalogue, and fills the slot it was put in —
      a `reviewer` in a worker slot is a read-only agent being asked to write;
    * `requires` names real capabilities, and the members cover them between
      them;
    * `defaults` carries only keys from :data:`PACK_DEFAULT_KEYS`, and every
      profile id in it resolves. A key that names tools or permissions is
      reported as what it is: an attempt to grant authority from a pack.
    """
    problems: List[str] = []
    if not isinstance(p, AgentPack):
        return [f"not an AgentPack: {type(p).__name__}"]
    if not str(p.pack or "").strip():
        problems.append("has no name")
    if not str(p.version or "").strip():
        problems.append(f"`{p.pack}` has no version; a pack without one cannot be pinned")
    if not str(p.coordinator or "").strip():
        problems.append(f"`{p.pack}` names no coordinator")
    if not p.workers:
        problems.append(f"`{p.pack}` names no workers; a group of one is a definition")

    index = _catalogue(defs)
    slots = [("coordinator", p.coordinator, ("coordinator",))]
    slots += [("worker", w, ("worker", "coordinator")) for w in p.workers]
    if str(p.reviewer or "").strip():
        slots.append(("reviewer", p.reviewer, ("reviewer",)))

    covered: set = set()
    for slot, slug, allowed_modes in slots:
        name = str(slug or "").strip()
        if not name:
            continue
        definition = index.get(name)
        if definition is None:
            problems.append(f"`{p.pack}` names `{name}` in the {slot} slot, and no such "
                            f"definition exists in this build")
            continue
        mode = str(getattr(definition, "mode", "") or "")
        if mode not in allowed_modes:
            problems.append(f"`{p.pack}` puts `{name}` (a {mode}) in the {slot} slot, which "
                            f"takes {' or '.join(allowed_modes)}")
        covered.update(str(c) for c in (getattr(definition, "capabilities", ()) or ()))

    for capability in p.requires:
        if capability not in CAPABILITIES:
            problems.append(f"`{p.pack}` requires `{capability}`, which is not a capability this "
                            f"build knows")
        elif capability not in covered:
            problems.append(f"`{p.pack}` requires `{capability}` and no member declares it")

    for key, value in (p.defaults or {}).items():
        if key in _FORBIDDEN_DEFAULT_KEYS:
            problems.append(f"`{p.pack}` defaults carry `{key}`: a pack groups agents and never "
                            f"merges their tools or permissions — each member keeps its own "
                            f"(§17)")
        elif key not in PACK_DEFAULT_KEYS:
            problems.append(f"`{p.pack}` defaults carry `{key}`, which is not one of "
                            f"{', '.join(PACK_DEFAULT_KEYS)}")
        elif key == "completion_mode" and value not in COMPLETION_MODES:
            problems.append(f"`{p.pack}` defaults set completion_mode `{value}`, which is not one "
                            f"of {', '.join(COMPLETION_MODES)}")
        elif key.endswith("_profile") and not catalog.exists(key[: -len("_profile")], value):
            problems.append(f"`{p.pack}` defaults reference {key} `{value}`, which no catalogue "
                            f"entry answers to")
        elif key == "output_contract" and not catalog.exists("output", value):
            problems.append(f"`{p.pack}` defaults reference output contract `{value}`, which no "
                            f"catalogue entry answers to")
    return problems


def resolve_pack(name: Any, *, defs: Any = None) -> Dict[str, Any]:
    """The pack with its slugs replaced by the definitions themselves.

    **It resolves; it does not merge.** The definitions come back as they are —
    the same tools, the same denies, the same permission rules each of them
    already had — because §17 keeps authority with the agent and this function
    is the obvious place somebody would otherwise "helpfully" combine them.
    Nothing here reads a permission, so nothing here can change one.

    Raises :class:`ProfileError` for an unknown pack or a member that does not
    exist: unlike selection, this is an explicit "give me this pack" and a
    caller who gets a half-resolved team back would run it.

    `problems` is returned alongside rather than raised, because a pack can be
    usable and imperfect at the same time — a missing capability is worth
    printing, not worth refusing a run over.
    """
    key = str(name or "").strip()
    found = pack(key)
    if found is None:
        available = ", ".join(sorted(BUILTIN_PACKS)) or "(none)"
        raise ProfileError("agent_pack", f"no such pack; available: {available}", got=name)

    index = _catalogue(defs)
    missing = [slug for slug in found.members() if slug not in index]
    if missing:
        raise ProfileError(
            "agent_pack." + found.pack,
            "names definitions this build does not have: " + ", ".join(missing)
            + ". A pack groups definitions; it cannot create one.")

    return {
        "pack": found.pack,
        "version": found.version,
        "description": found.description,
        "coordinator": index[found.coordinator],
        "workers": [index[slug] for slug in found.workers],
        "reviewer": index[found.reviewer] if found.reviewer else None,
        "members": {slug: index[slug] for slug in found.members()},
        "defaults": dict(found.defaults),
        "requires": list(found.requires),
        "problems": validate_pack(found, defs=index),
    }


#: The field names an `AgentPack` carries. Read by the test that asserts none
#: of them could hold a tool or a permission; kept next to the type so the two
#: are read together.
PACK_FIELDS: Sequence[str] = tuple(f.name for f in dataclass_fields(AgentPack))
