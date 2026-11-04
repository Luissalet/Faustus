"""decision_calibration_server.py

MCP server exposing `decision_calibrate`: the lowest confidence threshold for a typed decision whose
accepted answers stay under a chosen error rate, with a Clopper-Pearson upper bound computed from labelled
examples (src/decision_calibration.py). With `apply` the threshold is stored for the question and
`typed_decision.decide` uses it by default. `decision_calibrations` lists the stored ones.

stdout is the JSON-RPC stream; src/stdio_guard.py is raised first, like the other built-in servers.
"""

import asyncio
import json
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool, ToolAnnotations

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from src.stdio_guard import guard as stdout_guard
except Exception:  # pragma: no cover - the server must start regardless
    from contextlib import nullcontext as stdout_guard

server = Server("decision_calibration")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="decision_calibrate",
            description=(
                "Pick the confidence threshold for a typed (closed-question) decision with a statistical "
                "guarantee. Give samples, a list of {confidence, correct} from labelled examples the "
                "decision answered, and get the lowest threshold whose accepted answers have an error rate "
                "below max_error (default 0.05) with a Clopper-Pearson upper bound at level 1 - delta "
                "(default 0.05), plus coverage and the audit_rate (share of accepted answers to re-check "
                "with the slow path). Too few samples never pass: 0 errors needs 59 samples for 5 % at "
                "delta 0.05. With apply true and a field name, the threshold is stored for that question "
                "and typed decisions use it by default. Calibra el umbral de una decision tipada con cota "
                "estadistica."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "samples": {"type": "array", "items": {"type": "object", "properties": {
                        "confidence": {"type": "number"}, "correct": {"type": "boolean"}}},
                        "description": "Labelled examples: the confidence the decision reported and whether its answer was right."},
                    "max_error": {"type": "number", "description": "Highest acceptable error rate of accepted answers (default 0.05)."},
                    "delta": {"type": "number", "description": "Chance the true error rate is above the bound (default 0.05)."},
                    "audit_rate": {"type": "number", "description": "Share of accepted answers to re-check (default 0.02)."},
                    "field": {"type": "string", "description": "Question name; needed with apply."},
                    "apply": {"type": "boolean", "description": "Store the threshold for the field (default false)."},
                },
                "required": ["samples"],
            },
            annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
        ),
        Tool(
            name="decision_calibrations",
            description=("List the stored per-question thresholds with their bound, coverage and sample count. "
                         "Lista los umbrales calibrados guardados. Read-only."),
            inputSchema={"type": "object", "properties": {}},
            annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch, and never raise -- an exception here would kill the server."""
    args = arguments if isinstance(arguments, dict) else {}
    try:
        from src import decision_calibration as dc

        if name == "decision_calibrations":
            payload = {"calibrations": dc.all_calibrations()}
        elif name == "decision_calibrate":
            samples = args.get("samples")
            if not isinstance(samples, list) or not samples:
                return [TextContent(type="text", text="decision_calibrate needs samples: a non-empty list of {confidence, correct}")]
            payload = dc.calibrate(samples, max_error=args.get("max_error", dc.DEFAULT_MAX_ERROR),
                                   delta=args.get("delta", dc.DEFAULT_DELTA),
                                   audit_rate=args.get("audit_rate", dc.DEFAULT_AUDIT_RATE))
            if args.get("apply"):
                payload["saved"] = dc.save(str(args.get("field") or ""), payload)
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as exc:  # noqa: BLE001
        return [TextContent(type="text", text=f"Error in {name}: {type(exc).__name__}: {exc}")]
    return [TextContent(type="text", text=json.dumps(payload, indent=2, ensure_ascii=False))]


async def run():
    async with stdio_server() as (read_stream, write_stream):
        with stdout_guard():
            await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
