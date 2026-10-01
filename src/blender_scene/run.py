"""Launch Blender on a validated scene and turn its report into a result."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional

from . import discovery
from . import scene_common as common
from .validate import validate_scene

RUNNER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blender_runner.py")
DEFAULT_TIMEOUT = 300
MIN_TIMEOUT, MAX_TIMEOUT = 5, 3600
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg")


def default_timeout() -> int:
    try:
        from src.settings import get_setting
        value = int(get_setting("blender_timeout_seconds", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT)
    except Exception:  # noqa: BLE001
        value = DEFAULT_TIMEOUT
    return max(MIN_TIMEOUT, min(MAX_TIMEOUT, value))


def parse_report(stdout: str) -> Optional[dict]:
    """The last ``BLENDER_SCENE_REPORT {json}`` line of Blender's output, or None."""
    if not stdout:
        return None
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith(common.REPORT_PREFIX):
            try:
                data = json.loads(line[len(common.REPORT_PREFIX):])
            except ValueError:
                continue
            return data if isinstance(data, dict) else None
    return None


def tail(text: str, limit: int = 1500) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "..." + text[-limit:]


def build_command(blender: str, scene_json: str, root: str) -> List[str]:
    return [blender, "--background", "--factory-startup", "--disable-autoexec", "-noaudio",
            "--python-exit-code", "3", "--python", RUNNER_PATH, "--", scene_json, root]


def _clean_env() -> Dict[str, str]:
    env = dict(os.environ)
    # A host interpreter's variables would break Blender's bundled Python.
    for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP"):
        env.pop(key, None)
    env["PYTHONNOUSERSITE"] = "1"
    return env


def error_lines(report: Optional[dict]) -> List[str]:
    """One human line per failed op or assertion in a report."""
    lines: List[str] = []
    if not report:
        return lines
    for entry in report.get("ops") or []:
        if entry.get("status") == "error":
            lines.append(f"ops[{entry.get('index')}] {entry.get('op')}: {entry.get('message')}")
    if report.get("fatal"):
        lines.append(f"runner: {report['fatal']}")
    return lines


def run_scene(
    scene: Any,
    root: str,
    *,
    timeout: Optional[float] = None,
    blender_path: Optional[str] = None,
    runner: Callable[..., "subprocess.CompletedProcess"] = subprocess.run,
    probe_result: Optional[dict] = None,
) -> dict:
    """Validate ``scene`` against ``root``, run it in Blender and report.

    Never raises: every failure is a result with ``ok: false``, a ``stage``
    saying where it stopped and ``errors`` meant to go back to the model.
    """
    started = time.time()
    root = os.path.abspath(root)
    result: Dict[str, Any] = {"ok": False, "stage": "validate", "root": root, "errors": [], "warnings": [],
                              "outputs": [], "images": []}

    validation = validate_scene(scene, root)
    result["validation"] = validation.to_dict()
    result["warnings"] = list(validation.warnings)
    if not validation.ok:
        result["errors"] = validation.error_lines()
        result["duration_s"] = round(time.time() - started, 3)
        return result

    info = probe_result if probe_result is not None else (
        discovery.probe(setting=blender_path) if blender_path else discovery.probe())
    result["blender"] = {k: info.get(k) for k in ("found", "path", "version", "source") if k in info}
    if not info.get("found"):
        result.update(stage="discovery", errors=[info.get("message") or discovery.NOT_FOUND_MESSAGE])
        result["duration_s"] = round(time.time() - started, 3)
        return result

    try:
        os.makedirs(root, exist_ok=True)
        scene_dir = os.path.join(root, ".blender_scene")
        os.makedirs(scene_dir, exist_ok=True)
        scene_json = os.path.join(scene_dir, "scene.json")
        with open(scene_json, "w", encoding="utf-8") as fh:
            json.dump(validation.scene, fh, indent=1)
    except OSError as exc:
        result.update(stage="launch", errors=[f"could not prepare the project folder {root}: {exc}"])
        return result

    limit = float(timeout) if timeout else float(default_timeout())
    limit = max(MIN_TIMEOUT, min(MAX_TIMEOUT, limit))
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    cmd = build_command(info["path"], scene_json, root)
    result["timeout_s"] = limit
    try:
        proc = runner(cmd, cwd=root, capture_output=True, text=True, timeout=limit, env=_clean_env(),
                      stdin=subprocess.DEVNULL, creationflags=flags, errors="replace")
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        result.update(stage="timeout", stdout_tail=tail(out), stderr_tail=tail(err),
                      errors=[f"Blender did not finish within {limit:g} s and was stopped. Reduce the "
                              "resolution or samples, or raise the blender_timeout_seconds setting."])
        result["duration_s"] = round(time.time() - started, 3)
        return result
    except OSError as exc:
        result.update(stage="launch", errors=[f"could not start Blender at {info['path']}: {exc}"])
        return result

    report = parse_report(proc.stdout or "")
    result.update(returncode=proc.returncode, report=report, duration_s=round(time.time() - started, 3))
    if report is None:
        result.update(stage="no_report", stderr_tail=tail(proc.stderr), stdout_tail=tail(proc.stdout),
                      errors=[f"Blender exited with code {proc.returncode} without a {common.REPORT_PREFIX.strip()} "
                              "line; its error output is in stderr_tail."])
        return result

    result["stage"] = "done"
    result["outputs"] = list(report.get("outputs") or [])
    result["images"] = [o["path"] for o in result["outputs"]
                        if o.get("kind") == "render" and str(o.get("path", "")).lower().endswith(IMAGE_SUFFIXES)
                        and o.get("bytes", 0) > 0]
    result["warnings"] += list(report.get("warnings") or [])
    failed = [a for a in report.get("assertions") or [] if not a.get("passed")]
    result["assertions"] = {"total": len(report.get("assertions") or []), "failed": len(failed)}
    result["errors"] = error_lines(report)
    result["ok"] = proc.returncode == 0 and bool(report.get("ok")) and not result["errors"]
    if not result["ok"] and not result["errors"]:
        result["errors"] = [f"Blender exited with code {proc.returncode}"]
        result["stderr_tail"] = tail(proc.stderr)
    return result
