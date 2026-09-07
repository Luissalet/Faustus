"""Run a project's declared script skill in the container execution backend.

SKILL.md instructions alone are not an executable. A workflow selects a script
inside that skill, and a bounded immutable source copy is what the approved
container runs. No shell splitting, host fallback or ambient credential lookup.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import replace
from pathlib import Path, PurePosixPath

from src import approval_store, artifact_store, execution_router
from src.skills_runtime import bridge, discovery
from src.workflows.scope import validate_output_scope

MAX_BUNDLE_BYTES = 16 * 1024 * 1024
MAX_BUNDLE_FILES = 256
INTERPRETERS = {".py": "python", ".js": "node", ".mjs": "node", ".sh": "bash"}


def _bundle(folder):
    root = folder.resolve()
    entries = {}
    total = 0
    directories = 0
    for base, dirs, files in os.walk(root, followlinks=False):
        directories += 1
        if directories > MAX_BUNDLE_FILES:
            raise ValueError("skill source bundle exceeds 256 directories")
        for name in dirs:
            path = Path(base, name)
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError("skill directory links are not executable bundle sources")
        for name in sorted(files):
            path = Path(base, name)
            resolved = path.resolve()
            if path.is_symlink() or not resolved.is_relative_to(root) or not resolved.is_file():
                raise ValueError("skill file escapes its source folder or is not regular")
            if len(entries) >= MAX_BUNDLE_FILES:
                raise ValueError("skill source bundle exceeds 256 files")
            before = resolved.stat()
            with resolved.open("rb") as fh:
                opened = os.fstat(fh.fileno())
                if (not stat.S_ISREG(opened.st_mode) or
                        (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
                    raise ValueError("skill source changed while opening")
                data = fh.read(MAX_BUNDLE_BYTES - total + 1)
            total += len(data)
            if total > MAX_BUNDLE_BYTES:
                raise ValueError("skill source bundle exceeds 16 MiB")
            entries[path.relative_to(root).as_posix()] = data
    digest = hashlib.sha256()
    for name, data in sorted(entries.items()):
        digest.update(json.dumps([name, hashlib.sha256(data).hexdigest()]).encode("utf-8"))
    return entries, digest.hexdigest()


def run(node, context):
    owner = str(context.get("owner") or "")
    project_id = str(context.get("project_id") or "")
    if not owner or not project_id or not callable(context.get("mark_effect")):
        raise ValueError("script skills need an owned project and a claimed workflow node")
    validate_output_scope(owner, project_id,
                          str((context.get("inputs") or {}).get("session_id") or ""))
    from services.projects import get_store
    project = get_store().get(project_id, owner=owner)
    workspace = Path(project.get("workspace") or "")
    if not project.get("workspace") or not workspace.is_dir():
        raise ValueError("the workflow project needs a workspace folder")
    workspace = workspace.resolve()
    from src.tool_execution import vet_workspace
    if not vet_workspace(str(workspace)):
        raise ValueError("the project workspace is no longer allowed")
    skill_id = str(node.config.get("skill") or "")
    match = None
    for found in discovery.discover(str(workspace)):
        # Parent repositories may supply instructions, but must not add a
        # filesystem mount outside this project's explicit work root.
        if not Path(found.path).resolve().is_relative_to(workspace):
            continue
        if found.error:
            continue
        try:
            text = discovery.read_markdown(found)
            candidate = bridge.manifest_from_markdown(text, source=found.path)
        except (ValueError, OSError):
            # An unrelated old/invalid skill must not hide a valid match.
            continue
        if candidate.id == skill_id:
            match = (found, candidate)
            break
    if match is None:
        raise ValueError("script skill not found in this project's skill folders")
    found, manifest = match
    if node.config.get("version") and node.config["version"] != manifest.version:
        raise ValueError("installed skill version differs from the workflow's pinned version")
    from src.workflows import credentials
    secret_bindings = node.config.get("secret_bindings", {})
    bound_secrets = credentials.bind(owner, manifest.permissions.secrets, secret_bindings)
    if manifest.permissions.filesystem == "none":
        raise ValueError("a script skill needs declared workspace filesystem access")
    # Scripts may modify the mounted project. That capability needs an
    # explicit answer even when the source omitted an approval trigger.
    manifest = replace(manifest, approval_required_when=tuple(sorted(
        set(manifest.approval_required_when) | {"destructive"})))
    entries, digest = _bundle(Path(found.path).parent)
    # Build the executable manifest from the same captured bytes as the code.
    captured = bridge.manifest_from_markdown(entries["SKILL.md"].decode("utf-8", "replace"), source=found.path)
    if captured.to_dict() != replace(manifest, approval_required_when=captured.approval_required_when).to_dict():
        raise ValueError("skill manifest changed while collecting its source")
    script = node.config.get("script")
    if not isinstance(script, str) or "\\" in script or ":" in script:
        raise ValueError("script must name a relative file inside the skill")
    relative = PurePosixPath(script)
    if relative.is_absolute() or ".." in relative.parts or relative.as_posix() not in entries:
        raise ValueError("script is outside the skill source bundle")
    interpreter = INTERPRETERS.get(relative.suffix.lower())
    if not interpreter:
        raise ValueError("script skills support Python, JavaScript and Bash entrypoints")
    args = node.config.get("args") or []
    if (not isinstance(args, list) or len(args) > 64
            or any(not isinstance(arg, str) or len(arg) > 8192 or "\x00" in arg for arg in args)):
        raise ValueError("script args must be a bounded list of strings")
    command = [interpreter, "/artifacts/.faustus-skill/" + relative.as_posix(), *args]
    run_id = str(context["run_id"])
    execution_id = "wskill_" + hashlib.sha256(
        f"{run_id}:{node.id}:{context['attempt']}".encode()).hexdigest()[:32]
    root = artifact_store.ARTIFACT_RUNS_DIR
    decision = execution_router.choose(manifest, workspace=str(workspace), artifacts_root=root,
                                        run_id=execution_id, prefer="docker_workspace", create_dirs=False)
    if not decision.ok:
        return {"status": "failed", "reason": decision.detail or decision.reason}
    binding = {"run_id": run_id, "node_id": node.id, "source_sha256": digest,
               "secret_bindings": bound_secrets}
    plans = {action: execution_router.plan_for(manifest, decision.spec, action,
                                              command=command, binding=binding)
             for action in manifest.effective_approvals()}
    previous = context.get("previous") or {}
    cards = dict(previous.get("approval_cards") or {})
    waiting = []
    for action, plan in plans.items():
        card_id = cards.get(action)
        if not card_id:
            card = approval_store.request(plan, owner=owner, project_id=project_id, run_id=run_id)
            cards[action] = card.id
            waiting.append(card.id)
            continue
        card = approval_store.get(card_id)
        if card is None or card.owner != owner or card.plan.fingerprint() != plan.fingerprint():
            return {"status": "failed", "reason": "script approval or source changed"}
        from src.workflows.clock import due
        from src.contracts.base import now_iso
        if card.expires_at and due(card.expires_at, now_iso()):
            return {"status": "failed", "reason": "script approval expired"}
        if card.status == "pending":
            waiting.append(card.id)
        elif not card.covers(plan)["ok"]:
            return {"status": "failed", "reason": "script approval denied or already consumed"}
    if waiting:
        return {"status": "paused", "approval_id": waiting[0], "approval_cards": cards,
                "reason": "Review the script skill's workspace permissions before execution"}
    scratch = Path(artifact_store.run_dir(execution_id))
    snapshot = scratch / ".faustus-skill"
    snapshot.mkdir()  # Existing output is evidence of an earlier admission; do not overwrite it.
    for name, data in entries.items():
        target = snapshot / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def event(kind, payload):
        if kind == "backend.started" and not context["mark_effect"]("pending"):
            from src.workflows.engine import LostClaim
            raise LostClaim("workflow stopped before script execution")

    granted_secrets = credentials.bind(owner, manifest.permissions.secrets, secret_bindings,
                                       reveal=True, expected=bound_secrets)
    decision, result = execution_router.execute(
        manifest, command, workspace=str(workspace), artifacts_root=root, run_id=execution_id,
        owner=owner, prefer="docker_workspace", approval_binding=binding, on_event=event,
        cancel_requested=context.get("cancel_requested"), secrets=granted_secrets)
    if result is None:
        return {"status": "failed", "reason": decision.detail or decision.reason}
    def redact(text):
        for value in sorted(set(granted_secrets.values()), key=len, reverse=True):
            text = text.replace(value, "[REDACTED]")
        return text
    result = replace(result, stdout_tail=redact(result.stdout_tail),
                     stderr_tail=redact(result.stderr_tail), reason=redact(result.reason))
    if result.status != "refused":
        context["mark_effect"]("confirmed" if result.status == "completed" else "unknown")
    collected = artifact_store.collect(replace(result, run_id=run_id), source_dir=str(scratch),
        owner=owner, project_id=project_id, skill_id=manifest.id, skill_version=manifest.version,
        provenance={"recipe": str(context.get("workflow") or ""),
                    "note": f"node {node.id}; source SHA-256 {digest}; execution {execution_id}"})
    artifact_store.persist(collected.artifacts,
                           session_id=str((context.get("inputs") or {}).get("session_id") or ""))
    return {"status": "completed" if result.status == "completed" else "failed",
            "reason": result.reason, "execution_id": execution_id,
            "stdout": result.stdout_tail, "stderr": result.stderr_tail,
            "output_truncated": result.output_truncated,
            "artifact_ids": [item.id for item in collected.artifacts],
            "artifacts": [item.to_dict() for item in collected.artifacts],
            "source_sha256": digest}
