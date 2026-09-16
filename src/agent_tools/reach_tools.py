"""Reach tools (R1): `reach_read`, `reach_search`, `reach_doctor` -- thin
executors over `src/reach/router.py` + `src/reach/doctor.py`, following the
same `execute(content, ctx) -> dict` shape every other agent tool uses."""
from __future__ import annotations

import json
from typing import Any, Dict

from src.constants import MAX_OUTPUT_CHARS


def _parse_json_or_bare(content: str, bare_key: str) -> dict:
    raw = (content or "").strip()
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return {bare_key: raw}


def _result_to_output(result) -> Dict[str, Any]:
    data = result.to_dict()
    text = data.get("text") or ""
    truncated = data.get("truncated", False)
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS]
        truncated = True
    header = f"# {data.get('title') or data.get('url') or ''}\n" if (data.get("title") or data.get("url")) else ""
    header += f"Channel: {data['channel']} (backend: {data.get('backend') or 'none'}, trust: {data['source_trust']})\n"
    if data.get("author"):
        header += f"Author: {data['author']}\n"
    if data.get("published_at"):
        header += f"Published: {data['published_at']}\n"
    header += "\n"
    output = header + text
    if data.get("items"):
        items_txt = "\n\n## Items\n" + "\n".join(
            f"- {it.get('author', '')}: {it.get('text', '')[:500]}" for it in data["items"][:50]
        )
        if len(output) + len(items_txt) <= MAX_OUTPUT_CHARS:
            output += items_txt
        else:
            truncated = True
    if data.get("error") and not text:
        return {
            "error": f"reach_read: {data['channel']}: {data['error']} (attempts: {data['attempts']})",
            "exit_code": 1,
            "attempts": data["attempts"],
        }
    return {
        "output": output,
        "exit_code": 0,
        "channel": data["channel"],
        "backend": data["backend"],
        "source_trust": data["source_trust"],
        "truncated": truncated,
        "attempts": data["attempts"],
        "untrusted_content": True,
    }


class ReachReadTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.reach import router

        parsed = _parse_json_or_bare(content, "url")
        url = str(parsed.get("url") or "").strip()
        if not url:
            return {"error": "reach_read: provide a url", "exit_code": 1}
        channel = parsed.get("channel")
        call_ctx = ctx if isinstance(ctx, dict) else {}
        try:
            result = await router.read(url, channel=channel, ctx=call_ctx)
        except Exception as e:  # noqa: BLE001
            return {"error": f"reach_read: {url}: {type(e).__name__}: {e}", "exit_code": 1}
        return _result_to_output(result)


class ReachSearchTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.reach import router

        parsed = _parse_json_or_bare(content, "query")
        query = str(parsed.get("query") or "").strip()
        if not query:
            return {"error": "reach_search: provide a query", "exit_code": 1}
        channels = parsed.get("channels")
        if isinstance(channels, str):
            channels = [c.strip() for c in channels.split(",") if c.strip()]
        limit = parsed.get("limit")
        limit = int(limit) if isinstance(limit, (int, str)) and str(limit).isdigit() else 10
        call_ctx = ctx if isinstance(ctx, dict) else {}
        try:
            by_channel = await router.search(query, channels=channels, limit=limit, ctx=call_ctx)
        except Exception as e:  # noqa: BLE001
            return {"error": f"reach_search: {query}: {type(e).__name__}: {e}", "exit_code": 1}

        lines = [f"# Search results for: {query}"]
        attempts_all: Dict[str, Any] = {}
        any_hit = False
        for ch_name, results in by_channel.items():
            lines.append(f"\n## {ch_name}")
            for r in results:
                d = r.to_dict()
                attempts_all[ch_name] = d.get("attempts", [])
                if d.get("error") and not d.get("text") and not d.get("title"):
                    lines.append(f"- (no results: {d['error']})")
                    continue
                any_hit = True
                lines.append(f"- [{d.get('title') or d.get('url')}]({d.get('url')}) -- {d.get('text', '')[:200]}")
        output = "\n".join(lines)
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS] + "\n\n[...truncated]"
        return {
            "output": output,
            "exit_code": 0,
            "channels": list(by_channel.keys()),
            "attempts": attempts_all,
            "untrusted_content": True,
        }


class ReachDoctorTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.reach.doctor import doctor as run_doctor

        parsed = _parse_json_or_bare(content, "live")
        live_raw = parsed.get("live", False)
        live = live_raw in (True, "true", "1", 1, "True")
        report = await run_doctor(live=live)
        lines = [f"# Reach doctor -- {report['summary']} (live={live})"]
        for name, info in report["channels"].items():
            marker = {"ready": "OK", "needs_config": "NEEDS-CONFIG", "unavailable": "UNAVAILABLE"}.get(info["status"], "?")
            lines.append(f"\n## {name}: {marker} (active backend: {info['active_backend'] or 'none'})")
            for b in info["backends"]:
                lines.append(f"- {b['name']}: {b['status']}{' -- ' + b['reason'] if b['reason'] else ''}")
        return {
            "output": "\n".join(lines),
            "exit_code": 0,
            "ready_count": report["ready_count"],
            "total_count": report["total_count"],
        }
