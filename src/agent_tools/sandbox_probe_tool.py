"""agent_tools/sandbox_probe_tool.py -- `sandbox_probe` tool executor.

Runs the real allowed/forbidden checks of `src/sandbox_probe.py` and returns
the per-operation report (shell, python, Code Mode, files, descendants).
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional


class SandboxProbeTool:
    async def execute(self, content: Any, ctx: Optional[dict]) -> dict:
        from src import sandbox_probe
        args: Dict[str, Any] = content if isinstance(content, dict) else {}
        if isinstance(content, str) and content.strip().startswith("{"):
            try:
                parsed = json.loads(content)
                args = parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                args = {}
        ops = args.get("operations")
        ops = [str(o) for o in ops] if isinstance(ops, list) else None
        report = await sandbox_probe.run_sandbox_probe(ops)
        return {"output": sandbox_probe.render_text(report), "exit_code": 0, "report": report,
                "ok": report["ok"]}
