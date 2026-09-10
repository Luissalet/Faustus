"""agent_tools/exec_tools.py — EXEC-05/EXEC-06 tool wrappers (Lote 54).

Both EXEC-05 (dependency install with control) and EXEC-06 (reusable
scripts + gated remote execution) already had a real, tested library in
`src/tool_execution.py` (Lote 48) — planning, validation, versioning, SSH
pairing — with no tool the model could actually call. This module is thin
executors over that library, the same shape `code_tools.py` already uses
for `find_symbol`/`callers`/`tests_for`: parse args, call the library
function, format the result. No new authority is introduced here.

    install_dependencies   EXEC-05: plan | install a project-scoped dependency
    manage_scripts          EXEC-06: save | run | run_remote a versioned recipe

Both are effect-classified in `src/tool_capabilities.py` as
ADMIN_CHANGE/EXECUTE_CODE (see that module) so the existing post-external-
context approval gate covers them exactly like `bash`/`download_model`
already are — no new approval mechanism, per rule 4.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from src import tool_execution as te

logger = logging.getLogger(__name__)


def _args(content: Any) -> Dict[str, Any]:
    raw = (content or "").strip() if isinstance(content, str) else content
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _root(raw_path: str) -> str:
    return te._resolve_search_root(raw_path or "")


def _owner(ctx: dict) -> str:
    return str((ctx or {}).get("owner") or "")


class InstallDependenciesTool:
    """`install_dependencies` (EXEC-05).

    ``action: "plan"`` (default) detects the manager, validates every package
    name and returns the plan — never runs anything. ``action: "install"``
    only runs it when the caller ALSO passes ``approved: true`` explicitly —
    the module's own permission half (`plan_already_approved`); a plan that
    changed since the last approval (a package added/removed) hashes
    differently and has to be approved again, matching EXEC-05's own
    acceptance criterion.
    """

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        action = str(args.get("action") or "plan").strip().lower()
        raw_packages = args.get("packages") or args.get("package") or []
        if isinstance(raw_packages, str):
            raw_packages = [p.strip() for p in raw_packages.split(",") if p.strip()]
        if not isinstance(raw_packages, list) or not raw_packages:
            return {"error": "install_dependencies: `packages` (a list of package names) is required",
                    "exit_code": 1}
        try:
            root = _root(str(args.get("project_root") or args.get("path") or ""))
        except ValueError as exc:
            return {"error": f"install_dependencies: {exc}", "exit_code": 1}
        try:
            plan = te.plan_dependency_install(root, raw_packages)
        except ValueError as exc:
            return {"error": f"install_dependencies: {exc}", "exit_code": 1}
        owner = _owner(ctx)
        if action == "plan":
            return {
                "output": (
                    f"Plan ({plan.manager}, project-scoped, never global): "
                    f"{', '.join(plan.packages)} — plan_hash={plan.plan_hash}. "
                    f"Call install_dependencies again with action=\"install\" and "
                    f"approved=true to run it."
                ),
                "exit_code": 0,
                "plan": plan.to_mapping(),
                "already_approved": te.plan_already_approved(owner, plan.plan_hash),
            }
        if action != "install":
            return {"error": f"install_dependencies: unknown action {action!r} (use \"plan\" or \"install\")",
                    "exit_code": 1}
        approved = bool(args.get("approved")) or te.plan_already_approved(owner, plan.plan_hash)
        if not approved:
            return {
                "error": (
                    f"install_dependencies: plan {plan.plan_hash} was not approved — call again with "
                    f"approved=true once the user has reviewed it (package/origin/files above)."
                ),
                "exit_code": 1,
                "plan": plan.to_mapping(),
            }
        try:
            outcome = await te.execute_dependency_install(plan, approved=True, owner=owner)
        except PermissionError as exc:
            return {"error": f"install_dependencies: {exc}", "exit_code": 1, "plan": plan.to_mapping()}
        return {
            "output": (
                f"{'Installed' if outcome.ok else 'FAILED to install'} {', '.join(plan.packages)} "
                f"via {plan.manager} (return code {outcome.returncode})."
            ),
            "exit_code": outcome.returncode,
            "plan": plan.to_mapping(),
            "command": list(outcome.command),
            "stdout": outcome.stdout[:4000],
            "stderr": outcome.stderr[:4000],
        }


class ManageScriptsTool:
    """`manage_scripts` (EXEC-06): save | run | run_remote a versioned recipe.

    - ``save``: version-bumps under `name` (never overwrites in place).
    - ``run``: renders `values` into the saved script's argv (never a shell
      string — no injection surface) and runs it locally.
    - ``run_remote``: same rendering, over SSH to an alias PAIRED earlier by
      an operator (`pair_ssh_target`, out of this tool's scope — see the
      module docstring in `src/tool_execution.py`); the presented
      host/fingerprint must match the paired ones EXACTLY, so a similar
      hostname can never inherit another target's trust.
    """

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        action = str(args.get("action") or "").strip().lower()
        name = str(args.get("name") or "").strip()
        if not name:
            return {"error": "manage_scripts: `name` is required", "exit_code": 1}
        try:
            if action == "save":
                template = str(args.get("command_template") or "")
                params = args.get("params") or []
                if isinstance(params, str):
                    params = [p.strip() for p in params.split(",") if p.strip()]
                script = te.save_script(name, template, params)
                return {
                    "output": f"Saved {name!r} as version {script.version} "
                              f"(params: {', '.join(script.params) or 'none'}).",
                    "exit_code": 0, "script": script.to_mapping(),
                }
            script = te.load_script(name)
            if script is None:
                return {"error": f"manage_scripts: no saved script named {name!r}", "exit_code": 1}
            values = args.get("values") or {}
            if not isinstance(values, dict):
                return {"error": "manage_scripts: `values` must be an object of {param: value}", "exit_code": 1}
            values = {str(k): str(v) for k, v in values.items()}
            if action == "run":
                outcome = await te.run_saved_script(script, values, cwd=_root(str(args.get("cwd") or "")))
            elif action == "run_remote":
                alias = str(args.get("alias") or "").strip()
                host = str(args.get("host") or "").strip()
                fingerprint = str(args.get("fingerprint") or "").strip()
                if not (alias and host and fingerprint):
                    return {"error": "manage_scripts: run_remote needs `alias`, `host` and `fingerprint` "
                                     "(matching an already-paired SSH target)", "exit_code": 1}
                outcome = await te.run_remote_script(
                    script, values, alias=alias, presented_host=host, presented_fingerprint=fingerprint,
                )
            else:
                return {"error": f"manage_scripts: unknown action {action!r} "
                                 f"(use \"save\", \"run\" or \"run_remote\")", "exit_code": 1}
        except ValueError as exc:
            return {"error": f"manage_scripts: {exc}", "exit_code": 1}
        except PermissionError as exc:
            return {"error": f"manage_scripts: {exc}", "exit_code": 1}
        return {
            "output": f"{'ok' if outcome.ok else 'FAILED'}: {' '.join(outcome.command)} "
                      f"(return code {outcome.returncode})",
            "exit_code": outcome.returncode,
            "command": list(outcome.command),
            "stdout": outcome.stdout[:4000],
            "stderr": outcome.stderr[:4000],
        }
