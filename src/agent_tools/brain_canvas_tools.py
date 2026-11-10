"""The Cerebro as JSON Canvas files: export the graph to a `.canvas`, import a
`.canvas` into the vault (src/brain/canvas.py)."""
import asyncio
import json
import os


def _args(content):
    if isinstance(content, dict):
        return dict(content)
    text = str(content or "").strip()
    if not text:
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("arguments must be a JSON object")
    return data


def _list(value):
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()] or None
    return [str(v) for v in value] if isinstance(value, list) and value else None


class BrainCanvasExportTool:
    async def execute(self, content, ctx):
        from src.brain import canvas
        try:
            args = _args(content)
            out = await asyncio.to_thread(
                canvas.export_canvas, (ctx or {}).get("owner") or "",
                scope=str(args.get("scope") or "notes"), center=args.get("center") or None,
                depth=int(args.get("depth", 1) or 0), kinds=_list(args.get("kinds")), tags=_list(args.get("tags")),
                group_by=args.get("group_by") or None, layout=str(args.get("layout") or "auto"),
                mode=str(args.get("mode") or "file"), include_unresolved=bool(args.get("include_unresolved", True)),
                limit=int(args.get("limit") or 300), excerpt_chars=int(args.get("excerpt_chars") or 600),
                save=bool(args.get("save", True)), name=str(args.get("name") or ""),
                save_path=str(args.get("save_path") or ""))
            if not args.get("include_canvas") and out["stats"]["nodes"] > 40:
                out = {k: v for k, v in out.items() if k != "canvas"}
                out["canvas_omitted"] = "pass include_canvas=true to receive the document inline; it is in saved_to"
            return {"output": json.dumps(out, ensure_ascii=False), "exit_code": 0 if out.get("ok") else 1}
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported to the model as a tool error
            return {"error": f"brain_canvas_export: {exc}", "exit_code": 1}


class BrainCanvasImportTool:
    async def execute(self, content, ctx):
        from src.brain import canvas, vault
        try:
            args = _args(content)
            owner = (ctx or {}).get("owner") or ""
            folder = str(args.get("folder") or "Canvas")
            if args.get("undo"):
                out = await asyncio.to_thread(canvas.remove_import, owner, str(args.get("name") or ""),
                                              folder=folder, purge=bool(args.get("purge")))
                return {"output": json.dumps(out, ensure_ascii=False), "exit_code": 0}
            raw = args.get("canvas")
            name = str(args.get("name") or "")
            path = str(args.get("path") or "").strip()
            if raw is None and path:
                full = os.path.expanduser(path)
                if not os.path.isabs(full):
                    full = os.path.join(vault.vault_root(owner), *path.replace("\\", "/").split("/"))
                with open(full, "rb") as fh:
                    raw = fh.read()
                name = name or os.path.splitext(os.path.basename(full))[0]
            if raw is None:
                raise ValueError("give `canvas` (the document) or `path` (a .canvas file)")
            out = await asyncio.to_thread(
                canvas.import_canvas, owner, raw, name=name or "Canvas", folder=folder,
                dry_run=bool(args.get("dry_run")), prune=bool(args.get("prune")))
            return {"output": json.dumps(out, ensure_ascii=False), "exit_code": 0}
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            return {"error": f"brain_canvas_import: {exc}", "exit_code": 1}
