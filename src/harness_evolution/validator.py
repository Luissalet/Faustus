"""harness_evolution/validator.py — reject before availability (A26).

`validate()` is the ONLY gate `service.propose_candidate` runs before a
patch becomes visible as `validated` (i.e. before it is ever eligible for
`evaluate`/`promote`). A rejected patch never reaches those steps and the
currently active revision is never touched — this module writes nothing to
the store; it only returns a verdict for `service.py` to record.

Three typed changes, three different things to check for real:

* `add_skill` — the payload must be an honest `SKILL.md`: parsed by
  `src.skills_runtime.bridge.manifest_from_markdown`, the SAME parser
  production uses to turn a skill folder into a `SkillManifest` (no second,
  looser schema invented here). An invalid `permissions_*`/`version`/
  `inputs` frontmatter raises `ContractError` there, which this module turns
  into a named `ValidationError` instead of letting a broken skill through
  because nobody happened to call the real bridge. `src.skill_governance`
  adds the two checks the bridge does not: `privilege_escalation_flags`
  (a skill asking the agent to act with more privilege than granted is
  rejected outright, never "reviewed later") and `evaluation_manifest`
  (an unevaluable skill — no `when_to_use`/`verification`/`version` — is
  rejected rather than silently promotable with no eval surface). If the
  patch bundles a script, it is actually run — a real subprocess against a
  throwaway fixture directory, not a syntax check — because "validates"
  has to mean the script does not immediately crash on the simplest input
  the caller supplies.
* `update_instruction` / `update_specialist` — the patch must name an
  existing ref in the PARENT revision it targets (no dangling pointer) and
  carry non-empty replacement content.
"""
from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

from src.contracts import ContractError
from src.skills_runtime import bridge
from src import skill_governance

from .models import CHANGE_TYPES, CandidatePatch, HarnessRevision

#: Real interpreters a bundled fixture script may be run with. Anything else
#: is a validation failure, not a silent skip — the same closed set
#: `src/workflows/skills.py::INTERPRETERS` runs in production, kept in sync
#: by hand rather than imported so a change to the production execution
#: surface (container image, new language) cannot silently widen what this
#: validator will subprocess.run() on the host.
_SCRIPT_INTERPRETERS = {".py": "python3", ".js": "node", ".sh": "bash"}

_SCRIPT_TIMEOUT_S = 20


class ValidationError(ValueError):
    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"{field}: {reason}")
        self.field = field
        self.reason = reason


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reason: str = ""
    field: str = ""
    manifest: Optional[Dict[str, Any]] = None
    script_output: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "reason": self.reason, "field": self.field,
                "manifest": self.manifest, "script_output": self.script_output}


def _run_fixture_script(markdown: str, script: Mapping[str, Any]) -> Dict[str, Any]:
    """Actually execute the candidate skill's script against `script.args`
    (a list of strings the proposer supplies as the fixture input) inside a
    throwaway directory. Real subprocess, real exit code — this is the
    "external process" the contract allows to stay a fake in TESTS (the
    fixture script itself is fake work), but the execution here is genuine
    host code, not a mock."""
    relative = str(script.get("relative_path") or "")
    content = str(script.get("content") or "")
    args = [str(a) for a in (script.get("args") or [])]
    if not relative or ".." in relative.replace("\\", "/").split("/"):
        raise ValidationError("changes.script.relative_path",
                              "must be a relative path without `..`")
    suffix = os.path.splitext(relative)[1].lower()
    interpreter = _SCRIPT_INTERPRETERS.get(suffix)
    if not interpreter:
        raise ValidationError("changes.script.relative_path",
                              f"unsupported script type {suffix!r}")
    with tempfile.TemporaryDirectory(prefix="harness_evolution_fixture_") as tmp:
        skill_path = os.path.join(tmp, "SKILL.md")
        with open(skill_path, "w", encoding="utf-8") as fh:
            fh.write(markdown)
        script_path = os.path.join(tmp, os.path.basename(relative))
        with open(script_path, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.chmod(script_path, os.stat(script_path).st_mode | stat.S_IXUSR)
        try:
            proc = subprocess.run(
                [interpreter, script_path, *args], cwd=tmp, capture_output=True,
                text=True, timeout=_SCRIPT_TIMEOUT_S, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValidationError("changes.script", f"fixture run failed: {exc}") from None
        if proc.returncode != 0:
            raise ValidationError(
                "changes.script",
                f"fixture run exited {proc.returncode}: {proc.stderr[-2000:]}")
        return {"returncode": proc.returncode, "stdout": proc.stdout[-4000:],
                "stderr": proc.stderr[-4000:]}


def _validate_add_skill(changes: Mapping[str, Any]) -> ValidationResult:
    markdown = str(changes.get("markdown") or "")
    if not markdown.strip():
        raise ValidationError("changes.markdown", "must not be empty")
    try:
        manifest = bridge.manifest_from_markdown(markdown, source="harness_evolution.candidate")
    except ContractError as exc:
        raise ValidationError(f"changes.markdown.{exc.path}", exc.message) from None
    flags = skill_governance.privilege_escalation_flags(markdown)
    if flags:
        raise ValidationError(
            "changes.markdown",
            f"skill text reads as a privilege-escalation attempt ({len(flags)} pattern(s))")
    # `SkillManifest` (the permissions/schema contract `bridge` builds) does
    # not carry `verification`/`when_to_use` — those are
    # `services/memory/skill_format.Skill`'s own body sections, the same
    # ones `skill_governance.evaluation_manifest` was written to read off a
    # `Skill.to_dict()`-shaped mapping. Parse the same text through that
    # reader too, rather than inventing a duplicate field on the manifest.
    from services.memory.skill_format import Skill
    skill = Skill.from_markdown(markdown, path="harness_evolution.candidate")
    manifest_check = skill_governance.evaluation_manifest(skill.to_dict())
    if not manifest_check["evaluable"]:
        raise ValidationError(
            "changes.markdown",
            f"skill is not evaluable: missing {', '.join(manifest_check['missing'])}")
    script_output = None
    script = changes.get("script")
    if isinstance(script, Mapping) and script:
        script_output = _run_fixture_script(markdown, script)
    return ValidationResult(ok=True, manifest=manifest.to_dict(), script_output=script_output)


def _validate_ref_update(changes: Mapping[str, Any], parent: HarnessRevision) -> ValidationResult:
    ref = str(changes.get("ref") or "")
    content = changes.get("content")
    if not ref:
        raise ValidationError("changes.ref", "must name an existing ref on the parent revision")
    refs = parent.refs or {}
    exists = (ref in refs
              or ref in (refs.get("skills") or {})
              or ref in (refs.get("specialists") or {})
              or ref in (refs.get("memory_refs") or []))
    if not exists:
        raise ValidationError(
            "changes.ref", f"{ref!r} is not a reference on parent revision {parent.revision_id!r}")
    if not str(content or "").strip():
        raise ValidationError("changes.content", "must not be empty")
    return ValidationResult(ok=True)


def validate(patch: CandidatePatch, *, parent: HarnessRevision) -> ValidationResult:
    """The single entry point `service.propose_candidate` calls. Raises
    `ValidationError` (never returns `ok=False` — a caller that wants a
    verdict object instead of an exception uses `try_validate`)."""
    change_type = patch.change_type
    if change_type not in CHANGE_TYPES:
        raise ValidationError("changes.type", f"must be one of {CHANGE_TYPES}")
    if patch.parent_revision != parent.revision_id:
        raise ValidationError("parent_revision", "does not match the supplied parent revision")
    if change_type == "add_skill":
        return _validate_add_skill(patch.changes)
    return _validate_ref_update(patch.changes, parent)


def try_validate(patch: CandidatePatch, *, parent: HarnessRevision) -> ValidationResult:
    try:
        return validate(patch, parent=parent)
    except ValidationError as exc:
        return ValidationResult(ok=False, reason=exc.reason, field=exc.field)
