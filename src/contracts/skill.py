"""
contracts/skill.py — what a capability is allowed to say about itself.

The masterplan's first non-negotiable is that policy precedes the tool: a skill
never gets disk, network, keys, camera, publishing or the host by virtue of
being installed.  This module is where that stops being a slogan.  Every field
of `Permissions` defaults to deny, and a manifest that declares no backend is
a manifest that cannot run anywhere — which is the honest reading of "it did
not ask", not a reason to pick one for it.

Nothing here executes anything.  A manifest is a claim; the registry decides
whether the claim is satisfiable and the router decides where.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

from .base import (
    ContractError, SCHEMA_VERSION, as_mapping, fingerprint, flag, ident,
    one_of, reject_unknown, semver, text, text_list, whole,
)

# The kinds an artifact can be.  Kept closed on purpose: a `kind` nobody can
# preview, retain or hand to a downstream skill is a string, not a type.
#: `binary` is the honest bucket, not a lazy one: a run wrote bytes we cannot
#: type from their name, and the choice is between keeping them under a label
#: that admits as much, guessing a label that would make the provenance table
#: lie, or dropping the user's output. Only the first is defensible.
ARTIFACT_KINDS = ("image", "video", "audio", "document", "code", "dataset",
                  "json", "text", "archive", "binary")

SCALAR_TYPES = ("text", "integer", "number", "boolean", "json")

MEMORY_SCOPES = ("user", "project", "skill", "run")

# What can force a human into the loop.  Names are stable: they are what an
# approval card, an audit row and a policy rule all key on.
APPROVAL_TRIGGERS = (
    "publish",             # anything that leaves the machine as content
    "deliver",             # email, message, upload to a third party
    "cloud_model",         # inference off this machine
    "cost_over_budget",    # the run would spend more than the project allows
    "network",             # the run wants the network at all
    "secrets",             # a credential is handed to the run
    "host_access",         # something outside the workspace
    "destructive",         # deletes or overwrites the user's own files
)


_TYPE_RE = re.compile(r"^(?P<base>[a-z]+)(?::(?P<sub>[a-z]+))?(?P<many>\[\])?$")


@dataclass(frozen=True)
class TypeSpec:
    """`text`, `integer`, `artifact:video`, `artifact[]` — the whole type
    language.  It is small so that a manifest cannot describe a shape the
    runtime has no way to validate, hand over or preview."""

    base: str
    subtype: Optional[str] = None
    many: bool = False

    @classmethod
    def parse(cls, raw: Any, path: str) -> "TypeSpec":
        if not isinstance(raw, str):
            raise ContractError(path, "expected a type name like 'text' or 'artifact:video'", got=raw)
        m = _TYPE_RE.fullmatch(raw.strip())
        if not m:
            raise ContractError(path, "is not a type name (expected e.g. 'text', 'artifact:image', 'artifact[]')", got=raw)
        base, sub, many = m.group("base"), m.group("sub"), bool(m.group("many"))
        if base == "artifact":
            if sub is not None and sub not in ARTIFACT_KINDS:
                raise ContractError(path, f"artifact subtype must be one of {list(ARTIFACT_KINDS)}", got=raw)
        elif base in SCALAR_TYPES:
            if sub is not None:
                raise ContractError(path, f"'{base}' takes no subtype", got=raw)
        else:
            raise ContractError(path, f"unknown type; expected 'artifact' or one of {list(SCALAR_TYPES)}", got=raw)
        return cls(base=base, subtype=sub, many=many)

    def __str__(self) -> str:
        return self.base + (f":{self.subtype}" if self.subtype else "") + ("[]" if self.many else "")


def _fields(raw: Any, path: str) -> Tuple[Tuple[str, TypeSpec], ...]:
    data = as_mapping(raw or {}, path)
    out = []
    for name in data:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name):
            raise ContractError(f"{path}.{name}", "field names are lowercase snake_case, up to 64 chars")
        out.append((name, TypeSpec.parse(data[name], f"{path}.{name}")))
    return tuple(sorted(out))


@dataclass(frozen=True)
class Permissions:
    """Deny by default, every field.  `secrets` holds names, never values —
    a manifest that carries a credential is a manifest that leaked one."""

    network: bool = False
    network_allowlist: Tuple[str, ...] = ()
    secrets: Tuple[str, ...] = ()
    backends: Tuple[str, ...] = ()
    filesystem: str = "workspace"      # workspace | project | none
    host_access: bool = False
    max_seconds: Optional[int] = None
    max_cost_units: Optional[int] = None

    _KEYS = ("network", "network_allowlist", "secrets", "backends",
             "filesystem", "host_access", "max_seconds", "max_cost_units")

    @classmethod
    def parse(cls, raw: Any, path: str) -> "Permissions":
        data = as_mapping(raw or {}, path)
        reject_unknown(data, cls._KEYS, path)
        network = flag(data, "network", path, default=False)
        allowlist = text_list(data, "network_allowlist", path, max_items=64, max_len=253)
        if allowlist and not network:
            raise ContractError(
                f"{path}.network_allowlist",
                "lists hosts while network is false; one of the two is a mistake and "
                "guessing which would either open the network or silently drop the list",
            )
        return cls(
            network=network,
            network_allowlist=allowlist,
            secrets=text_list(data, "secrets", path, max_items=32, max_len=128),
            backends=text_list(data, "backends", path, max_items=8, max_len=64),
            filesystem=one_of(data, "filesystem", path, choices=("workspace", "project", "none"),
                              required=False, default="workspace"),
            host_access=flag(data, "host_access", path, default=False),
            max_seconds=whole(data, "max_seconds", path, minimum=1, maximum=86400),
            max_cost_units=whole(data, "max_cost_units", path, minimum=0),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "network": self.network,
            "network_allowlist": list(self.network_allowlist),
            "secrets": list(self.secrets),
            "backends": list(self.backends),
            "filesystem": self.filesystem,
            "host_access": self.host_access,
            "max_seconds": self.max_seconds,
            "max_cost_units": self.max_cost_units,
        }


@dataclass(frozen=True)
class MemoryPolicy:
    """Which scopes a skill may read and write.  Writing to `user` is not on
    offer: the masterplan says an agent's inference does not become a durable
    user preference on its own, so a skill writes to `run` and a curator — with
    the user in the loop — is what can promote it."""

    read_scopes: Tuple[str, ...] = ()
    write_scopes: Tuple[str, ...] = ("run",)

    _KEYS = ("read_scopes", "write_scopes")
    WRITABLE = ("run", "skill", "project")

    @classmethod
    def parse(cls, raw: Any, path: str) -> "MemoryPolicy":
        data = as_mapping(raw or {}, path)
        reject_unknown(data, cls._KEYS, path)
        writes = text_list(data, "write_scopes", path, default=("run",),
                           choices=MEMORY_SCOPES, max_items=4)
        for scope in writes:
            if scope not in cls.WRITABLE:
                raise ContractError(
                    f"{path}.write_scopes",
                    f"'{scope}' is readable but not writable by a skill; a durable "
                    "user preference is promoted by the curator with the user in the loop",
                )
        return cls(
            read_scopes=text_list(data, "read_scopes", path, choices=MEMORY_SCOPES, max_items=4),
            write_scopes=writes,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"read_scopes": list(self.read_scopes), "write_scopes": list(self.write_scopes)}


@dataclass(frozen=True)
class SkillManifest:
    """A capability's claim about itself: what it takes, what it produces,
    what it may touch and when a human has to say yes."""

    id: str
    version: str
    title: str
    description: str = ""
    family: str = "general"
    inputs: Tuple[Tuple[str, TypeSpec], ...] = ()
    outputs: Tuple[Tuple[str, TypeSpec], ...] = ()
    memory: MemoryPolicy = field(default_factory=MemoryPolicy)
    permissions: Permissions = field(default_factory=Permissions)
    approval_required_when: Tuple[str, ...] = ()
    tags: Tuple[str, ...] = ()
    source: str = ""                 # where this manifest was read from
    schema_version: int = SCHEMA_VERSION

    _KEYS = ("id", "version", "title", "description", "family", "inputs", "outputs",
             "memory", "permissions", "approval", "tags", "source", "schema_version")

    @classmethod
    def parse(cls, raw: Any, path: str = "skill") -> "SkillManifest":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        declared = ()
        if "approval" in data and data["approval"] is not None:
            approval = as_mapping(data["approval"], f"{path}.approval")
            reject_unknown(approval, ("required_when",), f"{path}.approval")
            declared = text_list(approval, "required_when", f"{path}.approval",
                                 choices=APPROVAL_TRIGGERS, max_items=len(APPROVAL_TRIGGERS))
        version_seen = whole(data, "schema_version", path, default=SCHEMA_VERSION, minimum=1)
        if version_seen > SCHEMA_VERSION:
            raise ContractError(
                f"{path}.schema_version",
                f"is from a newer Faustus (this build understands up to {SCHEMA_VERSION}); "
                "refusing to read it as if the unknown fields were absent",
                got=version_seen,
            )
        return cls(
            id=ident(data, "id", path),
            version=semver(data, "version", path),
            title=text(data, "title", path, max_len=200),
            description=text(data, "description", path, required=False, max_len=2000),
            family=text(data, "family", path, required=False, default="general", max_len=64),
            inputs=_fields(data.get("inputs"), f"{path}.inputs"),
            outputs=_fields(data.get("outputs"), f"{path}.outputs"),
            memory=MemoryPolicy.parse(data.get("memory"), f"{path}.memory"),
            permissions=Permissions.parse(data.get("permissions"), f"{path}.permissions"),
            approval_required_when=declared,
            tags=text_list(data, "tags", path, max_items=32, max_len=64),
            source=text(data, "source", path, required=False, max_len=1024),
            schema_version=version_seen,
        )


    # ── the triggers the manifest did not think to declare ────────────────

    def implied_approvals(self) -> Tuple[str, ...]:
        """A manifest that asks for the network but forgets to list `network`
        under `approval.required_when` still gets the card.  What the skill
        asked for is the evidence; what it declared is only a claim about it."""
        implied = []
        if self.permissions.network:
            implied.append("network")
        if self.permissions.secrets:
            implied.append("secrets")
        if self.permissions.host_access:
            implied.append("host_access")
        return tuple(sorted(set(implied) - set(self.approval_required_when)))

    def effective_approvals(self) -> Tuple[str, ...]:
        return tuple(sorted(set(self.approval_required_when) | set(self.implied_approvals())))

    def output_kinds(self) -> Tuple[str, ...]:
        return tuple(sorted({
            spec.subtype or "json"
            for _, spec in self.outputs
            if spec.base == "artifact"
        }))

    # ── serialization ─────────────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "version": self.version,
            "title": self.title,
            "description": self.description,
            "family": self.family,
            "inputs": {name: str(spec) for name, spec in self.inputs},
            "outputs": {name: str(spec) for name, spec in self.outputs},
            "memory": self.memory.to_dict(),
            "permissions": self.permissions.to_dict(),
            "approval": {"required_when": list(self.approval_required_when)},
            "tags": list(self.tags),
            "source": self.source,
        }

    def fingerprint(self) -> str:
        """Identity of the *contract*, not of the file: two manifests that
        differ only in `source` or `description` are the same promise, and two
        that differ by one permission are not."""
        return fingerprint([
            ("id", self.id),
            ("version", self.version),
            ("inputs", {n: str(s) for n, s in self.inputs}),
            ("outputs", {n: str(s) for n, s in self.outputs}),
            ("memory_read", list(self.memory.read_scopes)),
            ("memory_write", list(self.memory.write_scopes)),
            ("permissions", self.permissions.to_dict()),
            ("approvals", list(self.effective_approvals())),
        ])
