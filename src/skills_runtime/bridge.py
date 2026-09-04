"""
skills_runtime/bridge.py — a SKILL.md, read as a capability.

`services/memory/skill_format.Skill` is a document: a slug, a description, a
procedure, some tags. `contracts.SkillManifest` is a promise about what
something may touch. This module turns the first into the second without
inventing the parts the document never said.

The interesting work is all in what it refuses to assume:

* no `permissions:` block means **no permissions** — not "the usual ones";
* a `version:` that is not a semantic version is a rejection naming the field,
  not a silent `1.0.0`. A skill whose version cannot be compared cannot be
  pinned in an approval, and an approval that cannot name a version approves
  whatever the skill becomes;
* the folder a skill was found in never changes the answer.

`survey()` is what an operator actually wants: every skill, which ones now
have a valid manifest, which ones do not and exactly why. It is the answer to
"indexed is not loaded" — the Diogenes auditor's point — applied to permissions
rather than to discovery.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from src.contracts import ContractError, Permissions, SkillManifest
from src.contracts.skill import APPROVAL_TRIGGERS, MEMORY_SCOPES

#: The frontmatter keys this bridge reads as capability declarations. Flat, in
#: the shape `skill_format`'s parser can actually express — see
#: `permissions_from_frontmatter` for why there is no nested block.
CAPABILITY_KEYS = (
    "permissions_backends", "permissions_network", "permissions_network_allowlist",
    "permissions_secrets", "permissions_filesystem", "permissions_host_access",
    "permissions_max_seconds", "permissions_max_cost_units",
    "inputs", "outputs", "memory_read_scopes", "memory_write_scopes",
    "approval_required_when",
)


def permissions_from_frontmatter(fm: Mapping[str, Any]) -> Dict[str, Any]:
    """The permissions a `SKILL.md` declared, or an empty set.

    **Flat keys, because that is what the format can express.** The frontmatter
    parser in `skill_format` reads one scalar or one block list per line and
    has no nested maps, so a `permissions:` block cannot be written in a
    SKILL.md today. Rather than invent a second file format or pretend nested
    YAML works, the bridge reads prefixed keys:

        permissions_backends: [docker_workspace]
        permissions_network: false
        permissions_max_seconds: 300

    A `permissions:` mapping is still accepted, for callers that build the
    frontmatter as a dict rather than parsing a file.

    Either way the result is passed to `Permissions.parse` unfiltered: an
    unknown key is a typo in someone's frontmatter, and that module names it.
    Dropping it here would turn `permissions_host_acces: true` into a skill
    whose author believes it has the host and does not.
    """
    fm = fm or {}
    raw = fm.get("permissions")
    if isinstance(raw, Mapping):
        return dict(raw)
    flat = {key[len("permissions_"):]: value for key, value in fm.items()
            if isinstance(key, str) and key.startswith("permissions_")}
    return flat


def _fields_from_list(raw: Any) -> Optional[Dict[str, str]]:
    """`[report=artifact:document, notes=text]` → `{"report": "artifact:document"}`.

    Same reason as above: an `outputs:` map cannot be written in this
    frontmatter, and `name=type` is unambiguous where `name:type` would fight
    with the colon inside `artifact:document`."""
    if isinstance(raw, Mapping):
        return dict(raw)
    if not isinstance(raw, (list, tuple)):
        return None
    out: Dict[str, str] = {}
    for item in raw:
        text = str(item)
        if "=" not in text:
            raise ContractError("skill.outputs", "each entry is `name=type`", got=text)
        name, _, spec = text.partition("=")
        out[name.strip()] = spec.strip()
    return out


def manifest_from_markdown(text: str, *, source: str = "") -> SkillManifest:
    """Read a SKILL.md straight from its text.

    This is the entry point that matters for files on disk: `Skill` keeps only
    the fields it knows about, so a capability declaration would be dropped
    between `from_markdown` and `to_frontmatter` and the skill would come back
    with no permissions — which looks exactly like a skill that asked for
    none. Parsing the frontmatter here keeps the author's keys."""
    from services.memory.skill_format import Skill, parse_frontmatter

    fm, _body = parse_frontmatter(text)
    skill = Skill.from_markdown(text, path=source or None)
    return manifest_from_skill(skill, source=source, frontmatter=fm)


def manifest_from_skill(skill: Any, *, source: str = "",
                        frontmatter: Optional[Mapping[str, Any]] = None) -> SkillManifest:
    """Derive the manifest. Raises `ContractError` naming the field when the
    document cannot honestly become one."""
    known = skill.to_frontmatter() if hasattr(skill, "to_frontmatter") else dict(skill)
    fm: Dict[str, Any] = {**(frontmatter or {}), **known}
    extra: Mapping[str, Any] = getattr(skill, "capabilities", None) or {}
    if not isinstance(extra, Mapping):
        extra = {}

    body: Dict[str, Any] = {
        "id": _skill_id(skill, fm),
        "version": str(fm.get("version") or ""),
        "title": str(fm.get("description") or fm.get("name") or "").strip()[:200]
                 or str(fm.get("name") or "skill"),
        "description": str(getattr(skill, "when_to_use", "") or "")[:2000],
        "family": str(fm.get("category") or "general"),
        "tags": [str(t) for t in (fm.get("tags") or [])],
        "source": source or str(getattr(skill, "path", "") or ""),
        "permissions": permissions_from_frontmatter({**fm, **extra}),
    }
    merged = {**fm, **extra}
    for key in ("inputs", "outputs"):
        fields = _fields_from_list(merged.get(key))
        if fields is not None:
            body[key] = fields
    memory = {}
    for scope_key in ("read_scopes", "write_scopes"):
        value = merged.get(f"memory_{scope_key}")
        if value is not None:
            memory[scope_key] = value
    if isinstance(merged.get("memory"), Mapping):
        memory.update(merged["memory"])
    if memory:
        body["memory"] = memory
    approval = merged.get("approval")
    if not isinstance(approval, Mapping) and merged.get("approval_required_when") is not None:
        approval = {"required_when": merged["approval_required_when"]}
    if isinstance(approval, Mapping):
        body["approval"] = approval
    return SkillManifest.parse(body, path=f"skill[{body['id'] or '?'}]")


def _skill_id(skill: Any, fm: Mapping[str, Any]) -> str:
    """`category.name`, which is where the skill already lives on disk. Built
    here rather than taken from a field so two skills with the same name in
    different categories do not collide into one id."""
    name = str(getattr(skill, "name", "") or fm.get("name") or "").strip()
    category = str(getattr(skill, "category", "") or fm.get("category") or "").strip()
    if category and category != "general":
        return f"{category}.{name}" if name else category
    return name


@dataclass(frozen=True)
class SkillBridgeResult:
    """One skill, and whether it can honestly describe itself as a capability."""

    name: str
    source: str
    ok: bool
    manifest: Optional[SkillManifest] = None
    error_path: str = ""
    error: str = ""
    runnable: bool = False
    why_not: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "source": self.source, "ok": self.ok,
            "manifest": self.manifest.to_dict() if self.manifest else None,
            "error_path": self.error_path, "error": self.error,
            "runnable": self.runnable, "why_not": self.why_not,
        }


def survey(skills: Iterable[Any], *, check_backends: bool = True) -> List[SkillBridgeResult]:
    """Every skill, with a verdict and a reason.

    The two verdicts are different questions and both matter: `ok` is "this
    document can become a manifest", `runnable` is "and something could run
    it". Almost every skill written before this bridge existed answers yes and
    then no — valid, and with no permissions, so nothing can run it. That is
    the deny-by-default state, not a failure, and the survey says which it is
    instead of collapsing them into one red mark."""
    out: List[SkillBridgeResult] = []
    for skill in skills:
        name = str(getattr(skill, "name", "") or "?")
        source = str(getattr(skill, "path", "") or "")
        try:
            # A path is read from disk so the author's capability keys survive;
            # an in-memory Skill is taken as it is.
            if source and os.path.isfile(source):
                with open(source, "r", encoding="utf-8", errors="replace") as fh:
                    manifest = manifest_from_markdown(fh.read(), source=source)
            else:
                manifest = manifest_from_skill(skill, source=source)
        except ContractError as e:
            out.append(SkillBridgeResult(name=name, source=source, ok=False,
                                         error_path=e.path, error=e.message))
            continue
        except Exception as e:                      # a broken file is not a crash
            out.append(SkillBridgeResult(name=name, source=source, ok=False,
                                         error_path="<file>", error=str(e)))
            continue

        why, runnable = "", False
        if check_backends:
            from src import capability_registry as registry
            rows = registry.candidates(manifest)
            runnable = any(r["ok"] for r in rows)
            why = registry.why_no_backend(manifest)
        out.append(SkillBridgeResult(name=name, source=source, ok=True,
                                     manifest=manifest, runnable=runnable, why_not=why))
    return out
