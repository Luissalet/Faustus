"""agent_tools/research_podcast_tools.py — `research_podcast` tool executor.

Thin dispatcher over `src.research_podcast`: start the two-voice podcast of a
saved Deep Research report (a background job) or read its status. Only the
caller's own reports are reachable; the id is confined to the research
directory by `src.research_handler._research_json_path`.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict

logger = logging.getLogger(__name__)


def _args(content: Any) -> Dict[str, Any]:
    raw = (content or "").strip() if isinstance(content, str) else content
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    if isinstance(raw, str) and raw.startswith("{"):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {"research_id": raw} if isinstance(raw, str) else {}


def _same_owner(stored: Any, caller: Any) -> bool:
    from src.owner_identity import effective_storage_owner
    a = effective_storage_owner(str(stored or ""))
    b = effective_storage_owner(str(caller or ""))
    return bool(a) and a == b


def _summary(status: Dict[str, Any]) -> str:
    state = status.get("status")
    if state == "running":
        p = status.get("progress") or {}
        if p.get("phase") == "synthesizing":
            return f"Podcast in progress: voicing line {p.get('lines_done', 0)} of {p.get('lines_total', 0)}."
        return f"Podcast in progress ({p.get('phase') or 'starting'})."
    if state == "done":
        pod = status.get("podcast") or {}
        return (f"Podcast ready: {pod.get('lines', 0)} lines, {round(float(pod.get('duration_s') or 0) / 60, 1)} min, "
                f"voices {', '.join((pod.get('voices') or {}).values())}. Play it from the Research screen "
                f"or open {status.get('audio_url', '')}.")
    if state == "failed":
        return f"The podcast failed: {status.get('error') or 'unknown error'}"
    return "This report has no podcast yet."


class ResearchPodcastTool:
    """`research_podcast` {research_id, action?: "start"|"status", regenerate?}."""

    async def execute(self, content: str, ctx: dict) -> dict:
        from src import research_podcast
        from src.research_handler import _research_json_path

        args = _args(content)
        rid = str(args.get("research_id") or args.get("id") or args.get("session_id") or "").strip()
        action = str(args.get("action") or "start").strip().lower()
        if not rid or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", rid):
            return {"error": "research_podcast: 'research_id' is required (the id from manage_research list).",
                    "exit_code": 1}
        if action not in ("start", "status"):
            return {"error": "research_podcast: action must be 'start' or 'status'.", "exit_code": 1}
        path = _research_json_path(rid)
        owner = str((ctx or {}).get("owner") or "")
        data = research_podcast.read_research(path) if path is not None and path.exists() else {}
        if not data or not _same_owner(data.get("owner"), owner):
            return {"error": f"research_podcast: research '{rid}' not found.", "exit_code": 1}
        try:
            if action == "status":
                status = research_podcast.status_payload(rid, path)
            else:
                current = research_podcast.status_payload(rid, path)
                if current.get("status") in ("running",) or (
                        current.get("status") == "done" and not args.get("regenerate")):
                    status = current
                else:
                    status = await research_podcast.start_podcast(rid, path, owner)
        except research_podcast.PodcastError as e:
            return {"error": f"research_podcast: {e}", "exit_code": 1}
        except Exception as e:  # noqa: BLE001
            logger.warning("[research_podcast] tool failed for %s", rid, exc_info=True)
            return {"error": f"research_podcast: {type(e).__name__}: {e}", "exit_code": 1}
        out = {k: v for k, v in status.items() if k != "script"}
        out["lines"] = len(status.get("script") or [])
        out["output"] = _summary(status)
        out["exit_code"] = 0
        return out
