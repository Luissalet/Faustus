"""schema_extraction_tool.py -- the agent's `extract_to_schema` tool (OBJ-24).

Thin wrapper over `src.schema_extraction.extract_to_schema`: a document in the
workspace (or a text) and a JSON Schema in, the data in that shape out, with a
quote and the reader's page for every value that was kept and a list of what
was dropped and why. The check behind it is lexical (the words of the value
are in the document where the quote points); `limits` in the result says it
cannot show a value belongs to its field. Read-only: it reads the file
(through the family's document service, with OCR for scans) and calls a model
of its own in a separate, tool-less request; it writes nothing.

`path` goes through the same workspace confinement as `read_file` and
`inspect_deliverable` (`src.tool_execution._resolve_tool_path`).
"""
from __future__ import annotations

import json
from typing import Any, Dict

_ALLOWED = frozenset({"path", "text", "schema", "schema_name", "instructions", "ocr", "tier", "max_chars"})
#: Characters of the JSON result put in the model's context; the full result
#: travels in `extraction` for the UI and for callers.
_OUTPUT_CHARS = 24_000


def _usage() -> Dict[str, Any]:
    return {"error": 'extract_to_schema: expected {"path": "document in the workspace" | "text": "...", '
                     '"schema": {JSON Schema of an object} | "schema_name": "saved schema", '
                     '"instructions"?, "ocr"?: "auto|off|force", "tier"?: "auto|simple|medium|complex"}.',
            "error_code": "invalid_arguments", "exit_code": 1}


def _summary(result: Dict[str, Any]) -> str:
    compact = {
        "data": result.get("data"),
        "schema_valid": result.get("schema_valid"),
        "missing_required": result.get("missing_required"),
        "dropped": [{"path": d.get("path"), "why": d.get("why")} for d in result.get("dropped") or []][:40],
        "conflicts": result.get("conflicts")[:20] if result.get("conflicts") else [],
        "inferred": result.get("inferred"),
        "limits": [lim.get("note") for lim in result.get("limits") or [] if isinstance(lim, dict)],
        "errors": result.get("errors")[:10] if result.get("errors") else [],
        "evidence": [{"path": e.get("path"), "quote": e.get("quote"), "unit": e.get("unit")}
                     for e in result.get("evidence") or []][:80],
        "route": {k: (result.get("route") or {}).get(k) for k in ("tier", "purpose", "model", "reasons")},
        "repaired": result.get("repaired"), "escalated": result.get("escalated"),
        "input_truncated": result.get("input_truncated"), "source": result.get("source"),
    }
    text = json.dumps(compact, ensure_ascii=False, default=str)
    if len(text) > _OUTPUT_CHARS:
        compact.pop("evidence")
        compact["evidence_count"] = len(result.get("evidence") or [])
        text = json.dumps(compact, ensure_ascii=False, default=str)[:_OUTPUT_CHARS]
    return text


class ExtractToSchemaTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.schema_extraction import SchemaExtractionError, extract_to_schema
        from src.workflows.model_calls import ModelUnavailable
        try:
            args = json.loads(content) if isinstance(content, str) else content
        except (TypeError, ValueError):
            return _usage()
        if not isinstance(args, dict) or set(args) - _ALLOWED:
            return _usage()
        path = args.get("path")
        text = args.get("text")
        if (path is None) == (text is None) or (path is not None and not isinstance(path, str)) \
                or (text is not None and not isinstance(text, str)):
            return _usage()
        schema = args.get("schema")
        if isinstance(schema, str):
            try:
                schema = json.loads(schema)
            except ValueError:
                return {"error": "extract_to_schema: `schema` is not valid JSON", "error_code": "invalid_schema",
                        "exit_code": 1}
        resolved = None
        if path is not None:
            from src.tool_execution import _resolve_tool_path
            try:
                resolved = str(_resolve_tool_path(path))
            except (TypeError, ValueError) as exc:
                return {"error": f"extract_to_schema: {exc}", "error_code": "path_not_allowed", "exit_code": 1}
        owner = ctx.get("owner") if isinstance(ctx, dict) else None
        try:
            result = await extract_to_schema(
                owner, path=resolved, text=text, schema=schema, schema_name=args.get("schema_name"),
                instructions=str(args.get("instructions") or ""), ocr=str(args.get("ocr") or "auto"),
                tier=str(args.get("tier") or "auto"), max_chars=args.get("max_chars") or 96_000)
        except SchemaExtractionError as exc:
            return {"error": f"extract_to_schema: {exc}", "error_code": exc.code, "exit_code": 1}
        except ModelUnavailable as exc:
            return {"error": f"extract_to_schema: no model could be reached ({exc})",
                    "error_code": "model_unavailable", "exit_code": 1}
        return {"output": _summary(result), "extraction": result, "exit_code": 0}
