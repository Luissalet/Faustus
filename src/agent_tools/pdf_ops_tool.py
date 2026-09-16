"""agent_tools/pdf_ops_tool.py — the `pdf_ops` tool: merge/split/rotate/
compress/watermark/etc. over `src.pdf_ops`.

One tool, `{op, ...}`, rather than one-tool-per-operation — the same shape
`git_tools`/`board_tools` use for their own multi-action surfaces — so the
tool index carries one schema instead of eleven near-identical ones. `op` is
an enum in the JSON schema (`src/tool_schemas.py`), so an unknown op is
already rejected at the schema layer before this module ever runs.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict

from src import pdf_ops

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


def _one_input(args: Dict[str, Any], op: str) -> str:
    value = str(args.get("input") or "").strip()
    if not value:
        raise pdf_ops.PdfOpsError(f"pdf_ops({op}): `input` is required")
    return value


class PdfOpsTool:
    """`pdf_ops`: merge, split, extract_pages, rotate, reorder, delete_pages,
    metadata (read or write), compress, watermark_text, page_count, to_images
    (optional dependency), ocr (optional external CLI)."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        op = str(args.get("op") or "").strip().lower()
        if not op:
            return {"error": "pdf_ops: `op` is required", "exit_code": 1}
        try:
            result = self._dispatch(op, args)
        except pdf_ops.PdfOpsError as exc:
            return {"error": f"pdf_ops({op}): {exc}", "exit_code": 1, "error_class": "pdf_ops.invalid"}
        except Exception as exc:  # noqa: BLE001 — a bad PDF is data, not a crash
            logger.warning("pdf_ops(%s) failed: %s", op, exc)
            return {"error": f"pdf_ops({op}): {exc}", "exit_code": 1, "error_class": "pdf_ops.error"}
        output_line = result.get("output") or result.get("outputs") or result.get("input") or ""
        return {"output": f"pdf_ops {op}: {output_line}", "exit_code": 0, **result}

    def _dispatch(self, op: str, args: Dict[str, Any]) -> Dict[str, Any]:
        overwrite = bool(args.get("overwrite"))
        if op == "page_count":
            return pdf_ops.page_count(_one_input(args, op))
        if op == "merge":
            inputs = args.get("inputs")
            if not isinstance(inputs, list) or not inputs:
                raise pdf_ops.PdfOpsError("merge: `inputs` (a list of PDF paths) is required")
            return pdf_ops.merge([str(p) for p in inputs], args.get("output"), overwrite=overwrite)
        if op == "split":
            ranges = args.get("ranges")
            if not isinstance(ranges, list) or not ranges:
                raise pdf_ops.PdfOpsError("split: `ranges` (a list of page-range strings) is required")
            return pdf_ops.split(_one_input(args, op), [str(r) for r in ranges],
                                 args.get("output_dir"), overwrite=overwrite)
        if op == "extract_pages":
            return pdf_ops.extract_pages(_one_input(args, op), args.get("pages"),
                                         args.get("output"), overwrite=overwrite)
        if op == "rotate":
            degrees = args.get("degrees")
            if degrees is None:
                raise pdf_ops.PdfOpsError("rotate: `degrees` is required (multiple of 90)")
            return pdf_ops.rotate(_one_input(args, op), args.get("pages"), int(degrees),
                                  args.get("output"), overwrite=overwrite)
        if op == "reorder":
            order = args.get("order")
            if not isinstance(order, list) or not order:
                raise pdf_ops.PdfOpsError("reorder: `order` (every page, once) is required")
            return pdf_ops.reorder(_one_input(args, op), order, args.get("output"), overwrite=overwrite)
        if op == "delete_pages":
            return pdf_ops.delete_pages(_one_input(args, op), args.get("pages"),
                                        args.get("output"), overwrite=overwrite)
        if op == "metadata":
            set_fields = args.get("set_fields") if isinstance(args.get("set_fields"), dict) else None
            return pdf_ops.metadata(_one_input(args, op), set_fields, args.get("output"),
                                    overwrite=overwrite)
        if op == "compress":
            return pdf_ops.compress(_one_input(args, op), args.get("output"), overwrite=overwrite)
        if op == "watermark_text":
            text = str(args.get("text") or "")
            kwargs: Dict[str, Any] = {}
            if args.get("opacity") is not None:
                kwargs["opacity"] = float(args["opacity"])
            if args.get("font_size") is not None:
                kwargs["font_size"] = int(args["font_size"])
            if args.get("angle") is not None:
                kwargs["angle"] = float(args["angle"])
            return pdf_ops.watermark_text(_one_input(args, op), text, args.get("output"),
                                          overwrite=overwrite, **kwargs)
        if op == "to_images":
            kwargs = {}
            if args.get("dpi") is not None:
                kwargs["dpi"] = int(args["dpi"])
            return pdf_ops.to_images(_one_input(args, op), args.get("output_dir"),
                                     pages=args.get("pages"), **kwargs)
        if op == "ocr":
            kwargs = {}
            if args.get("language"):
                kwargs["language"] = str(args["language"])
            return pdf_ops.ocr(_one_input(args, op), args.get("output"), overwrite=overwrite, **kwargs)
        raise pdf_ops.PdfOpsError(
            f"unknown op '{op}' — one of merge, split, extract_pages, rotate, reorder, "
            f"delete_pages, metadata, compress, watermark_text, page_count, to_images, ocr"
        )
