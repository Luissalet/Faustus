"""agent_tools/blender_scene_tool.py -- `blender_scene` tool executor.

Thin wrapper over `src.blender_scene`: describe a 3D scene as typed JSON, have
it validated, run it headless in Blender and get back a structured report
(per-op status, assertions, files written) and the rendered image.

Actions: probe (is Blender installed), schema (the format), validate (check a
scene without starting Blender), run, smoke (a built-in scene that renders,
saves and re-loads itself).

Files are written only inside a per-session folder under the Faustus data
directory (`BLENDER_SCENES_DIR/<session>/<folder>`); a scene cannot name a path
outside it. A rendered PNG/JPEG is returned as `images` (the model and the chat
see it) and registered with the gallery like other generated images.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

ACTIONS = ("probe", "schema", "validate", "run", "smoke")
MAX_IMAGES = 3
MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


def _args(content: str) -> Dict[str, Any]:
    raw = (content or "").strip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _safe_segment(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "")).strip("._")[:80]
    return cleaned or fallback


def scenes_dir() -> str:
    from src import constants

    return constants.BLENDER_SCENES_DIR


def session_root(ctx: dict, folder: str = "") -> str:
    """The folder a run may write to: ``<scenes>/<session>[/<folder>]``."""
    base = os.path.join(scenes_dir(), _safe_segment((ctx or {}).get("session_id") or "", "default"))
    if folder:
        base = os.path.join(base, _safe_segment(folder, "scene"))
    return base


def _scene_arg(args: Dict[str, Any]):
    scene = args.get("scene")
    if scene is None and "ops" in args:
        scene = {"ops": args["ops"]}
        if isinstance(args.get("meta"), dict):
            scene["meta"] = args["meta"]
    return scene


def _rel(path: str, root: str) -> str:
    try:
        rel = os.path.relpath(path, root)
    except ValueError:
        return path
    return "${PROJECT_ROOT}/" + rel.replace(os.sep, "/")


def _describe_run(result: dict, root: str) -> str:
    lines: List[str] = []
    report = result.get("report") or {}
    blender = result.get("blender") or {}
    ver = f" {blender.get('version')}" if blender.get("version") else ""
    if result.get("stage") == "done":
        ops = report.get("ops") or []
        ok = len([o for o in ops if o.get("status") == "ok"])
        bad = len([o for o in ops if o.get("status") == "error"])
        skipped = len([o for o in ops if o.get("status") == "skipped"])
        head = (f"Blender{ver} ran {len(ops)} ops in {result.get('duration_s')} s: {ok} ok, {bad} failed"
                + (f", {skipped} skipped" if skipped else "") + ".")
        lines.append(("OK. " if result.get("ok") else "FAILED. ") + head)
        asserts = result.get("assertions") or {}
        if asserts.get("total"):
            lines.append(f"Assertions: {asserts['total'] - asserts['failed']}/{asserts['total']} passed.")
    else:
        lines.append(f"FAILED at stage '{result.get('stage')}'.")
    for err in result.get("errors") or []:
        lines.append(f"  - {err}")
    for item in (report.get("assertions") or []):
        if not item.get("passed"):
            lines.append(f"  assertion {item.get('label')}: observed {item.get('observed')!r}, expected {item.get('expected')!r}")
    outputs = result.get("outputs") or []
    if outputs:
        lines.append("Files written (relative to the project folder):")
        for out in outputs:
            lines.append(f"  {_rel(out['path'], root)}  {out.get('bytes', 0)} bytes  [{out.get('kind')}]")
    if result.get("warnings"):
        lines.append("Warnings:")
        lines += [f"  - {w}" for w in result["warnings"][:10]]
    if result.get("stderr_tail"):
        lines.append("Blender stderr (tail):\n" + result["stderr_tail"])
    return "\n".join(lines)


def _publish_image(path: str, prompt: str, size: str, model: str, ctx: dict) -> Dict[str, Any]:
    """Copy a render next to the other generated images and add a gallery row.

    Best effort: a failure here never fails the run; the image is still
    attached to the tool result.
    """
    try:
        from src.constants import GENERATED_IMAGES_DIR

        ext = os.path.splitext(path)[1].lower() or ".png"
        # Plain hex: the generated-image route only serves hex or UUID names
        # (src/generated_images.py), so a prefixed name showed a broken image.
        filename = f"{uuid.uuid4().hex}{ext}"
        os.makedirs(GENERATED_IMAGES_DIR, exist_ok=True)
        shutil.copyfile(path, os.path.join(GENERATED_IMAGES_DIR, filename))
    except Exception as exc:  # noqa: BLE001
        logger.debug("blender_scene: could not copy the render to the gallery folder: %s", exc)
        return {}
    info: Dict[str, Any] = {"image_url": f"/api/generated-image/{filename}", "image_prompt": prompt,
                            "image_model": model, "image_size": size}
    try:
        from src.database import SessionLocal, GalleryImage

        new_id = str(uuid.uuid4())
        db = SessionLocal()
        try:
            db.add(GalleryImage(id=new_id, filename=filename, prompt=prompt, model=model, size=size,
                                quality="", session_id=(ctx or {}).get("session_id") or None,
                                owner=(ctx or {}).get("owner") or None))
            db.commit()
        finally:
            db.close()
        info["image_id"] = new_id
    except Exception as exc:  # noqa: BLE001
        logger.debug("blender_scene: gallery row skipped: %s", exc)
    return info


def _attach_images(result: dict, out: Dict[str, Any], ctx: dict, title: str) -> None:
    paths = result.get("images") or []
    if not paths:
        return
    images = []
    for path in paths[-MAX_IMAGES:]:
        try:
            if os.path.getsize(path) > MAX_IMAGE_BYTES:
                continue
            with open(path, "rb") as fh:
                data = base64.b64encode(fh.read()).decode("ascii")
        except OSError:
            continue
        images.append({"data": data, "mimeType": _MIME.get(os.path.splitext(path)[1].lower(), "image/png")})
    if images:
        out["images"] = images
        out["output"] += f"\n{len(images)} rendered image(s) attached."
    last = paths[-1]
    report = result.get("report") or {}
    size = ""
    for entry in reversed(report.get("ops") or []):
        info = entry.get("info") or {}
        if entry.get("op") == "render" and info.get("resolution"):
            size = "x".join(str(v) for v in info["resolution"])
            break
    version = (result.get("blender") or {}).get("version") or ""
    if not (ctx or {}).get("no_gallery"):
        out.update(_publish_image(last, title or "3D scene rendered with Blender", size,
                                  f"blender {version}".strip(), ctx))


class BlenderSceneTool:
    """`blender_scene` {action, scene?, folder?, timeout?, format?, op?}."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        action = str(args.get("action") or "").strip().lower()
        if not action:
            action = "run" if _scene_arg(args) is not None else ""
        if action not in ACTIONS:
            return {"error": f"blender_scene: action must be one of {', '.join(ACTIONS)}", "exit_code": 1}
        try:
            if action == "probe":
                return await self._probe()
            if action == "schema":
                return self._schema(args)
            if action == "smoke":
                return await self._smoke(args, ctx or {})
            scene = _scene_arg(args)
            if scene is None:
                return {"error": f"blender_scene {action}: provide `scene` (an object with `ops`, or a list of ops)",
                        "exit_code": 1}
            if action == "validate":
                return self._validate(scene, args, ctx or {})
            return await self._run(scene, args, ctx or {})
        except Exception as exc:  # noqa: BLE001 - a tool call never crashes the turn
            logger.warning("blender_scene %s failed: %s", action, exc, exc_info=True)
            return {"error": f"blender_scene {action}: {type(exc).__name__}: {exc}", "exit_code": 1}

    # -- actions ---------------------------------------------------------

    async def _probe(self) -> dict:
        from src.blender_scene import probe

        info = await asyncio.to_thread(probe)
        return {"output": info.get("message") or "", "probe": info, "exit_code": 0}

    def _schema(self, args: Dict[str, Any]) -> dict:
        from src.blender_scene import OPS, scene_schema, scene_summary

        op = str(args.get("op") or "").strip()
        if op and op not in OPS:
            return {"error": f"blender_scene schema: unknown op '{op}'; valid ops: {', '.join(OPS)}", "exit_code": 1}
        fmt = str(args.get("format") or "summary").lower()
        if fmt == "json_schema":
            schema = scene_schema()
            return {"output": json.dumps(schema, separators=(",", ":")), "schema": schema, "exit_code": 0}
        return {"output": scene_summary(op or None), "exit_code": 0}

    def _validate(self, scene: Any, args: Dict[str, Any], ctx: dict) -> dict:
        from src.blender_scene import validate_scene

        root = session_root(ctx, str(args.get("folder") or ""))
        result = validate_scene(scene, root)
        data = result.to_dict()
        if result.ok:
            text = f"Valid: {result.op_count} ops. Files would be written under the project folder."
            if result.warnings:
                text += "\nWarnings:\n" + "\n".join(f"  - {w}" for w in result.warnings)
            return {"output": text, "validation": data, "exit_code": 0}
        text = f"Invalid scene, {len(result.errors)} problem(s):\n" + "\n".join(f"  - {line}" for line in result.error_lines())
        return {"output": text, "validation": data, "exit_code": 1}

    async def _run(self, scene: Any, args: Dict[str, Any], ctx: dict) -> dict:
        from src.blender_scene import run_scene

        root = session_root(ctx, str(args.get("folder") or ""))
        timeout = args.get("timeout")
        timeout = float(timeout) if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) else None
        result = await asyncio.to_thread(run_scene, scene, root, timeout=timeout)
        return self._finish(result, root, ctx, title=self._title(scene))

    async def _smoke(self, args: Dict[str, Any], ctx: dict) -> dict:
        from src.blender_scene import probe, reload_scene, run_scene, smoke_scene

        engine = str(args.get("engine") or "workbench").lower()
        if engine not in ("workbench", "eevee", "cycles"):
            return {"error": "blender_scene smoke: engine must be workbench, eevee or cycles", "exit_code": 1}
        root = session_root(ctx, "smoke")
        info = await asyncio.to_thread(probe)
        first = await asyncio.to_thread(run_scene, smoke_scene(engine), root, probe_result=info)
        out = self._finish(first, root, ctx, title="Blender smoke scene")
        if not first.get("ok"):
            return out
        second = await asyncio.to_thread(run_scene, reload_scene(engine=engine), root, probe_result=info)
        ra = second.get("assertions") or {}
        verdict = (f"Reload check: {ra.get('total', 0) - ra.get('failed', 0)}/{ra.get('total', 0)} assertions "
                   "passed after re-opening the saved .blend." if second.get("stage") == "done"
                   else f"Reload check failed at stage '{second.get('stage')}'.")
        out["output"] += "\n" + verdict
        if second.get("errors"):
            out["output"] += "\n" + "\n".join(f"  - {e}" for e in second["errors"])
        out["reload"] = {"ok": bool(second.get("ok")), "assertions": ra, "errors": second.get("errors") or []}
        if not second.get("ok"):
            out["exit_code"] = 1
        return out

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _title(scene: Any) -> str:
        meta = scene.get("meta") if isinstance(scene, dict) else None
        if isinstance(meta, dict):
            return str(meta.get("name") or meta.get("description") or "")[:200]
        return ""

    def _finish(self, result: dict, root: str, ctx: dict, title: str = "") -> dict:
        output = _describe_run(result, root)
        compact = {k: v for k, v in result.items() if k != "report"}
        compact["ops"] = [{k: e.get(k) for k in ("index", "op", "status", "duration_s", "message")}
                          for e in (result.get("report") or {}).get("ops") or []]
        compact["assertion_details"] = (result.get("report") or {}).get("assertions") or []
        out: Dict[str, Any] = {"output": output, "exit_code": 0 if result.get("ok") else 1,
                               "run": compact, "project_root": root}
        if result.get("stage") != "done":
            # Nothing ran (invalid scene, no Blender, timeout, crash): a plain
            # error. A run that finished with failing ops keeps its output,
            # report and images, like a shell command with a non-zero exit.
            out["error"] = output
        if result.get("images"):
            _attach_images(result, out, ctx, title)
        return out
