"""
contracts/artifact.py — every output, with the record of where it came from.

"Todo output es un artefacto."  A final text, a commit, an image, a video, a
PDF and a dataset all carry their run and their provenance, which is what turns
the gallery, the chat history and the code output into one thing you can audit
instead of three places you have to correlate by timestamp.

Provenance holds a `model_license` because the masterplan makes licences a
frontier, not a footnote: an image whose model forbids redistribution and an
image that is yours to sell look identical on disk, and the difference has to
travel with the file.

An unknown provenance field is `None`, never a plausible guess.  "We do not
know which model made this" is a fact worth storing; inventing one destroys the
only reason the record exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

from .base import (
    ContractError, SCHEMA_VERSION, as_mapping, fingerprint, ident, now_iso,
    one_of, reject_unknown, semver, sha256_hex, text, text_list, timestamp, whole,
)
from .skill import ARTIFACT_KINDS

RETENTION_POLICIES = ("keep", "session", "run", "days", "ephemeral")


@dataclass(frozen=True)
class Retention:
    """How long this is allowed to survive, and who said so.  `days` is the
    only policy that carries a number, and it is required there — a retention
    of "days" with no count is the kind of half-answer that gets a user's
    footage deleted or kept forever depending on which default won."""

    policy: str = "keep"
    days: Optional[int] = None
    reason: str = ""

    _KEYS = ("policy", "days", "reason")

    @classmethod
    def parse(cls, raw: Any, path: str) -> "Retention":
        data = as_mapping(raw or {}, path)
        reject_unknown(data, cls._KEYS, path)
        policy = one_of(data, "policy", path, choices=RETENTION_POLICIES,
                        required=False, default="keep")
        days = whole(data, "days", path, minimum=1, maximum=36500)
        if policy == "days" and days is None:
            raise ContractError(f"{path}.days", "is required when policy is 'days'")
        if policy != "days" and days is not None:
            raise ContractError(f"{path}.days", f"is set while policy is '{policy}'", got=days)
        return cls(policy=policy, days=days,
                   reason=text(data, "reason", path, required=False, max_len=300))

    def to_dict(self) -> Dict[str, Any]:
        return {"policy": self.policy, "days": self.days, "reason": self.reason}


@dataclass(frozen=True)
class Provenance:
    """What produced this.  Every field is optional and every unknown stays
    None — the record's whole value is that it does not guess."""

    model: Optional[str] = None
    model_license: Optional[str] = None
    backend: Optional[str] = None
    recipe: Optional[str] = None          # workflow/template id, versioned
    recipe_version: Optional[str] = None
    #: The recipe's own fingerprint. `recipe` + `recipe_version` name it;
    #: this says the file had not been edited underneath that name, which is
    #: the difference between "made by image.product 1.0.0" and "made by
    #: whatever image.product 1.0.0 happened to be that week".
    recipe_fingerprint: Optional[str] = None
    inputs_digest: Optional[str] = None   # fingerprint of inputs, never the inputs
    #: The seed, when the thing that made this had one. Not folded into
    #: `inputs_digest`: a digest proves two runs matched, and a seed is what
    #: someone types to make the picture again.
    seed: Optional[int] = None
    #: The engine and the job it ran as, when it was not this process — a
    #: ComfyUI prompt id, a remote worker's job. It is how a file on disk is
    #: traced back to a render somebody can still look up.
    engine: Optional[str] = None
    engine_job_id: Optional[str] = None
    source_artifact_ids: Tuple[str, ...] = ()
    note: str = ""

    _KEYS = ("model", "model_license", "backend", "recipe", "recipe_version",
             "recipe_fingerprint", "inputs_digest", "seed", "engine",
             "engine_job_id", "source_artifact_ids", "note")


    @classmethod
    def parse(cls, raw: Any, path: str) -> "Provenance":
        data = as_mapping(raw or {}, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            model=text(data, "model", path, required=False, max_len=200) or None,
            model_license=text(data, "model_license", path, required=False, max_len=200) or None,
            backend=text(data, "backend", path, required=False, max_len=64) or None,
            recipe=text(data, "recipe", path, required=False, max_len=200) or None,
            recipe_version=text(data, "recipe_version", path, required=False, max_len=64) or None,
            recipe_fingerprint=sha256_hex(data, "recipe_fingerprint", path,
                                          required=False) or None,
            inputs_digest=sha256_hex(data, "inputs_digest", path, required=False) or None,
            seed=whole(data, "seed", path, minimum=0),
            engine=text(data, "engine", path, required=False, max_len=64) or None,
            engine_job_id=text(data, "engine_job_id", path, required=False, max_len=128) or None,
            source_artifact_ids=text_list(data, "source_artifact_ids", path, max_items=200, max_len=64),
            note=text(data, "note", path, required=False, max_len=1000),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model": self.model, "model_license": self.model_license,
            "backend": self.backend, "recipe": self.recipe,
            "recipe_version": self.recipe_version,
            "recipe_fingerprint": self.recipe_fingerprint,
            "inputs_digest": self.inputs_digest, "seed": self.seed,
            "engine": self.engine, "engine_job_id": self.engine_job_id,
            "source_artifact_ids": list(self.source_artifact_ids), "note": self.note,
        }

    def unknowns(self) -> Tuple[str, ...]:
        """What this artifact cannot account for.  The gallery shows it as
        "unknown", which is the point: a missing model is a gap in the record,
        not a blank to be filled in by whatever produced the preview."""
        missing = [name for name in ("model", "backend", "recipe", "inputs_digest")
                   if getattr(self, name) is None]
        if self.model and not self.model_license:
            missing.append("model_license")
        return tuple(missing)


@dataclass(frozen=True)
class Artifact:
    """A typed output with an owner, a hash, a run and a retention policy."""

    id: str
    kind: str
    filename: str
    sha256: str = ""
    media_type: str = ""
    byte_size: Optional[int] = None
    label: str = ""
    owner: str = ""
    project_id: str = ""
    run_id: str = ""
    skill_id: str = ""
    skill_version: str = ""
    created_at: str = ""
    partial: bool = False
    preview_filename: str = ""
    provenance: Provenance = field(default_factory=Provenance)
    retention: Retention = field(default_factory=Retention)
    schema_version: int = SCHEMA_VERSION

    _KEYS = ("id", "kind", "filename", "sha256", "media_type", "byte_size", "label",
             "owner", "project_id", "run_id", "skill_id", "skill_version", "created_at",
             "partial", "preview_filename", "provenance", "retention", "schema_version")

    @classmethod
    def parse(cls, raw: Any, path: str = "artifact") -> "Artifact":
        from .base import flag
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        filename = text(data, "filename", path, max_len=512)
        if "/" in filename or "\\" in filename or filename in (".", ".."):
            raise ContractError(
                f"{path}.filename",
                "is a bare name inside the artifact store, not a path (the store "
                "decides where things live; a path here is how a run writes outside it)",
                got=filename,
            )
        skill_id = ident(data, "skill_id", path, required=False)
        skill_version = semver(data, "skill_version", path, required=False)
        if skill_id and not skill_version:
            raise ContractError(f"{path}.skill_version", "is required alongside skill_id")
        return cls(
            id=text(data, "id", path, max_len=64),
            kind=one_of(data, "kind", path, choices=ARTIFACT_KINDS),
            filename=filename,
            sha256=sha256_hex(data, "sha256", path, required=False),
            media_type=text(data, "media_type", path, required=False, max_len=128),
            byte_size=whole(data, "byte_size", path, minimum=0),
            label=text(data, "label", path, required=False, max_len=300),
            owner=text(data, "owner", path, required=False, max_len=128),
            project_id=text(data, "project_id", path, required=False, max_len=128),
            run_id=text(data, "run_id", path, required=False, max_len=64),
            skill_id=skill_id,
            skill_version=skill_version,
            created_at=timestamp(data, "created_at", path, default=now_iso()),
            partial=flag(data, "partial", path, default=False),
            preview_filename=text(data, "preview_filename", path, required=False, max_len=512),
            provenance=Provenance.parse(data.get("provenance"), f"{path}.provenance"),
            retention=Retention.parse(data.get("retention"), f"{path}.retention"),
            schema_version=whole(data, "schema_version", path, default=SCHEMA_VERSION, minimum=1),
        )


    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id, "kind": self.kind, "filename": self.filename,
            "sha256": self.sha256, "media_type": self.media_type,
            "byte_size": self.byte_size, "label": self.label, "owner": self.owner,
            "project_id": self.project_id, "run_id": self.run_id,
            "skill_id": self.skill_id, "skill_version": self.skill_version,
            "created_at": self.created_at, "partial": self.partial,
            "preview_filename": self.preview_filename,
            "provenance": self.provenance.to_dict(),
            "retention": self.retention.to_dict(),
        }

    def provenance_gaps(self) -> Tuple[str, ...]:
        gaps = list(self.provenance.unknowns())
        if not self.sha256:
            gaps.append("sha256")
        if not self.run_id:
            gaps.append("run_id")
        return tuple(gaps)
