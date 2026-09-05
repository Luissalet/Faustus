"""
contracts/blob.py — bytes are not artifacts, and an artifact is not its bytes.

`contracts/artifact.py` models an output as one object whose id happens to be
its content hash. B-017 is what that costs: two runs that write identical bytes
get identical ids, so the second one's owner, run, label, provenance, approval
and retention are dropped as "already stored", and a later run ends up
referencing a row that names an earlier — possibly different — owner.

The fix is three identities instead of one:

* **Blob** — immutable bytes addressed by hash. Its id IS the content
  (`blob_<sha256>`), which is what makes creation atomic: two writers of the
  same bytes race on a primary key, and the loser reads the winner's row. A
  blob has no owner, no run and no provenance, because bytes belong to nobody.
* **ArtifactOccurrence** — one logical output, owned. Its id is an event, not a
  content hash (`occ_<random>`), so producing the same bytes twice produces two
  rows with two provenances. Physical deduplication grants no logical access:
  sharing a blob is not sharing an artifact, and every read path takes an
  occurrence id plus an owner, never a hash.
* **DerivedArtifact** — preview, thumbnail, proxy, waveform, subtitle,
  transcode, embedding. Regenerable by definition, so deleting one loses
  nothing that cannot be recomputed.

A derivative hangs off the BLOB, not off the occurrence, and that is the one
choice here worth arguing about. A thumbnail of some bytes is a function of
those bytes; hanging it off the occurrence would store two byte-identical
thumbnails for two owners of the same original. Access control does not leak
through it, because a derivative is only ever reached through an occurrence the
caller may already see.

Same three rules as the rest of `src/contracts`: an unknown key is an error, no
value is coerced across a type boundary, and an unknown field stays None rather
than becoming a plausible guess.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from .artifact import Provenance, Retention
from .base import (
    ContractError, SCHEMA_VERSION, as_mapping, fingerprint, flag, now_iso,
    one_of, reject_unknown, semver, sha256_hex, text, text_list, timestamp, whole,
)
from .skill import ARTIFACT_KINDS

#: What a derivative can be. Closed on purpose: a derivative kind ends up in a
#: filename and in a regeneration recipe, and "whatever the caller typed" is
#: how a store grows two spellings of thumbnail.
DERIVED_KINDS = ("preview", "thumbnail", "proxy", "waveform", "subtitle",
                 "transcode", "embedding")

#: The four relations F-012 asks every artifact to be able to state.
RELATION_KINDS = ("variation_of", "derived_from", "supersedes", "part_of")


def blob_id_for(sha256: str) -> str:
    """`blob_<sha256>`, in full — not truncated the way `art_<hash[:24]>` was.

    The truncation is why the legacy id cannot be resolved back to a hash and
    why the alias has to be a stored, indexed column instead of a string
    transformation."""
    digest = (sha256 or "").strip().lower()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ContractError("blob.sha256", "must be 64 lowercase hex chars (a SHA-256)",
                            got=sha256)
    return f"blob_{digest}"


def new_occurrence_id() -> str:
    """An occurrence id names an event, never its content.

    Deriving it from the bytes is precisely the bug: it makes the second
    occurrence indistinguishable from the first and therefore droppable."""
    return f"occ_{uuid.uuid4().hex}"


def derived_id_for(source_sha256: str, derived_kind: str,
                   recipe: str = "", recipe_version: str = "") -> str:
    """Deterministic, so regenerating a thumbnail updates one row instead of
    appending a new one every time the factory runs."""
    digest = fingerprint([("source", (source_sha256 or "").lower()),
                          ("kind", derived_kind),
                          ("recipe", recipe or ""),
                          ("recipe_version", recipe_version or "")])
    return f"der_{digest[:32]}"


def _bare_name(data: Any, key: str, path: str, *, required: bool = True) -> str:
    """A name inside the store, never a path. The store decides where things
    live; a path here is how a run writes outside it."""
    value = text(data, key, path, required=required, max_len=512)
    if value and ("/" in value or "\\" in value or os.sep in value
                  or value in (".", "..")):
        raise ContractError(
            f"{path}.{key}",
            "is a bare name inside the artifact store, not a path",
            got=value,
        )
    return value


@dataclass(frozen=True)
class Blob:
    """Immutable bytes, addressed by hash, owned by nobody.

    `refcount` is a fast index over the occurrences that point here, not the
    authority on them. Garbage collection counts the occurrences instead and
    reports the difference: a refcount that drifted low is how a collector
    deletes bytes somebody still references."""

    sha256: str
    byte_size: int
    filename: str
    media_type: str = ""
    refcount: int = 0
    created_at: str = ""
    schema_version: int = SCHEMA_VERSION

    _KEYS = ("sha256", "byte_size", "filename", "media_type", "refcount",
             "created_at", "schema_version")

    @property
    def id(self) -> str:
        return blob_id_for(self.sha256)

    @classmethod
    def parse(cls, raw: Any, path: str = "blob") -> "Blob":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            sha256=sha256_hex(data, "sha256", path),
            byte_size=whole(data, "byte_size", path, required=True, minimum=0),
            filename=_bare_name(data, "filename", path),
            media_type=text(data, "media_type", path, required=False, max_len=128),
            refcount=whole(data, "refcount", path, default=0, minimum=0),
            created_at=timestamp(data, "created_at", path, default=now_iso()),
            schema_version=whole(data, "schema_version", path,
                                 default=SCHEMA_VERSION, minimum=1),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"schema_version": self.schema_version, "id": self.id,
                "sha256": self.sha256, "byte_size": self.byte_size,
                "filename": self.filename, "media_type": self.media_type,
                "refcount": self.refcount, "created_at": self.created_at}


@dataclass(frozen=True)
class Relations:
    """What this output is to other outputs. Every entry is an occurrence id:
    a variation is a variation of somebody's artifact, not of some bytes."""

    variation_of: Tuple[str, ...] = ()
    derived_from: Tuple[str, ...] = ()
    supersedes: Tuple[str, ...] = ()
    part_of: Tuple[str, ...] = ()

    _KEYS = RELATION_KINDS

    @classmethod
    def parse(cls, raw: Any, path: str) -> "Relations":
        data = as_mapping(raw or {}, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(**{name: text_list(data, name, path, max_items=200, max_len=128)
                      for name in RELATION_KINDS})

    def to_dict(self) -> Dict[str, Any]:
        return {name: list(getattr(self, name)) for name in RELATION_KINDS}

    def is_empty(self) -> bool:
        return not any(getattr(self, name) for name in RELATION_KINDS)


@dataclass(frozen=True)
class ArtifactOccurrence:
    """One logical output with an owner, a run and a provenance of its own.

    `blob_sha256` is a reference, not an identity: two occurrences of the same
    blob are two artifacts. `legacy_artifact_id` carries the old `art_<hash>`
    id so that a reference written before the split keeps resolving after it —
    the alias B-017 asks for, stored and indexed rather than recomputed, since
    the old id truncated the hash and cannot be inverted."""

    id: str
    kind: str
    blob_sha256: str
    label: str = ""
    owner: str = ""
    project_id: str = ""
    run_id: str = ""
    session_id: str = ""
    skill_id: str = ""
    skill_version: str = ""
    created_at: str = ""
    partial: bool = False
    approval_id: str = ""
    provenance: Provenance = field(default_factory=Provenance)
    retention: Retention = field(default_factory=Retention)
    relations: Relations = field(default_factory=Relations)
    legacy_artifact_id: str = ""
    schema_version: int = SCHEMA_VERSION

    _KEYS = ("id", "kind", "blob_sha256", "label", "owner", "project_id", "run_id",
             "session_id", "skill_id", "skill_version", "created_at", "partial",
             "approval_id", "provenance", "retention", "relations",
             "legacy_artifact_id", "schema_version")

    @classmethod
    def parse(cls, raw: Any, path: str = "occurrence") -> "ArtifactOccurrence":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        skill_id = text(data, "skill_id", path, required=False, max_len=128)
        skill_version = semver(data, "skill_version", path, required=False)
        if skill_id and not skill_version:
            raise ContractError(f"{path}.skill_version", "is required alongside skill_id")
        occurrence_id = text(data, "id", path, max_len=128)
        if occurrence_id.startswith("blob_"):
            raise ContractError(
                f"{path}.id",
                "must not be a blob id: an occurrence is an event, and giving it "
                "the identity of its bytes is the defect this contract exists to fix",
                got=occurrence_id,
            )
        return cls(
            id=occurrence_id,
            kind=one_of(data, "kind", path, choices=ARTIFACT_KINDS),
            blob_sha256=sha256_hex(data, "blob_sha256", path),
            label=text(data, "label", path, required=False, max_len=300),
            owner=text(data, "owner", path, required=False, max_len=128),
            project_id=text(data, "project_id", path, required=False, max_len=128),
            run_id=text(data, "run_id", path, required=False, max_len=64),
            session_id=text(data, "session_id", path, required=False, max_len=64),
            skill_id=skill_id,
            skill_version=skill_version,
            created_at=timestamp(data, "created_at", path, default=now_iso()),
            partial=flag(data, "partial", path, default=False),
            approval_id=text(data, "approval_id", path, required=False, max_len=64),
            provenance=Provenance.parse(data.get("provenance"), f"{path}.provenance"),
            retention=Retention.parse(data.get("retention"), f"{path}.retention"),
            relations=Relations.parse(data.get("relations"), f"{path}.relations"),
            legacy_artifact_id=text(data, "legacy_artifact_id", path,
                                    required=False, max_len=64),
            schema_version=whole(data, "schema_version", path,
                                 default=SCHEMA_VERSION, minimum=1),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id, "kind": self.kind, "blob_sha256": self.blob_sha256,
            "label": self.label, "owner": self.owner, "project_id": self.project_id,
            "run_id": self.run_id, "session_id": self.session_id,
            "skill_id": self.skill_id, "skill_version": self.skill_version,
            "created_at": self.created_at, "partial": self.partial,
            "approval_id": self.approval_id,
            "provenance": self.provenance.to_dict(),
            "retention": self.retention.to_dict(),
            "relations": self.relations.to_dict(),
            "legacy_artifact_id": self.legacy_artifact_id,
        }

    def belongs_to(self, owner: str) -> bool:
        """Whether `owner` may reach this occurrence.

        An occurrence with no owner is unowned, not public: it answers False
        for every named caller, and the caller that wants it has to ask for it
        without an owner and say so. The alternative — treating "" as a
        wildcard — is how the second producer of some bytes ends up reading the
        first producer's artifact."""
        return bool(owner) and self.owner == owner


@dataclass(frozen=True)
class DerivedArtifact:
    """A regenerable rendering of a blob: preview, proxy, waveform, subtitle.

    Keyed by (source, kind, recipe, recipe version) through `derived_id_for`,
    so a factory that reruns updates one row rather than growing a new one per
    pass. `source_sha256` is the blob it was computed from; deleting the
    derivative never touches it."""

    id: str
    source_sha256: str
    derived_kind: str
    filename: str
    sha256: str = ""
    byte_size: Optional[int] = None
    media_type: str = ""
    recipe: str = ""
    recipe_version: str = ""
    created_at: str = ""
    schema_version: int = SCHEMA_VERSION

    _KEYS = ("id", "source_sha256", "derived_kind", "filename", "sha256",
             "byte_size", "media_type", "recipe", "recipe_version", "created_at",
             "schema_version")

    @classmethod
    def parse(cls, raw: Any, path: str = "derived") -> "DerivedArtifact":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        source = sha256_hex(data, "source_sha256", path)
        kind = one_of(data, "derived_kind", path, choices=DERIVED_KINDS)
        recipe = text(data, "recipe", path, required=False, max_len=200)
        recipe_version = semver(data, "recipe_version", path, required=False)
        if recipe and not recipe_version:
            raise ContractError(f"{path}.recipe_version",
                                "is required alongside recipe (a derivative that "
                                "cannot name the version that made it cannot be "
                                "regenerated identically)")
        expected = derived_id_for(source, kind, recipe, recipe_version)
        given = text(data, "id", path, required=False, max_len=128) or expected
        if given != expected:
            raise ContractError(
                f"{path}.id",
                f"must be {expected} for this source, kind and recipe — the id is "
                "derived so that regenerating updates one row instead of adding one",
                got=given,
            )
        return cls(
            id=given,
            source_sha256=source,
            derived_kind=kind,
            filename=_bare_name(data, "filename", path),
            sha256=sha256_hex(data, "sha256", path, required=False),
            byte_size=whole(data, "byte_size", path, minimum=0),
            media_type=text(data, "media_type", path, required=False, max_len=128),
            recipe=recipe,
            recipe_version=recipe_version,
            created_at=timestamp(data, "created_at", path, default=now_iso()),
            schema_version=whole(data, "schema_version", path,
                                 default=SCHEMA_VERSION, minimum=1),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version, "id": self.id,
            "source_sha256": self.source_sha256, "derived_kind": self.derived_kind,
            "filename": self.filename, "sha256": self.sha256,
            "byte_size": self.byte_size, "media_type": self.media_type,
            "recipe": self.recipe, "recipe_version": self.recipe_version,
            "created_at": self.created_at,
        }
