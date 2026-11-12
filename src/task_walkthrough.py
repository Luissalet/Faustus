"""Delivery walkthroughs use Prospero's real browser recording job."""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

import httpx


SCHEMA = {"name": "task_walkthrough", "description": "Record a real journey through a finished app or website with captions using Prospero. Start with url, title and steps (click/selector/caption, navigate/url/caption, scroll/y/caption, wait/caption). Poll with job_id. No GPU. The recording browser has no existing login. Keywords: delivery video, website walkthrough, video recorrido, entrega.",
          "parameters": {"type": "object", "properties": {
              "url": {"type": "string"}, "title": {"type": "string"}, "project": {"type": "string"},
              "steps": {"type": "array", "items": {"type": "object", "properties": {
                  "action": {"type": "string", "enum": ["click", "navigate", "scroll", "wait"]},
                  "selector": {"type": "string"}, "url": {"type": "string"}, "y": {"type": "number"}, "caption": {"type": "string"}}, "required": ["action"]}},
              "hold_s": {"type": "number"}, "job_id": {"type": "string"}}, "additionalProperties": False}}


def connection(owner):
    from src import plugin_runtime, connector_sidecar
    from src.connectors import read_token_file
    from src.owner_identity import single_user_mode, owners_match
    found = plugin_runtime.resolve("prospero", owner=None if single_user_mode() else owner)
    row = found.get("connector")
    if not row:
        raise ValueError("Connect Prospero to record a walkthrough")
    row = connector_sidecar.get_connector(row["id"], redact=False) or row
    if row.get("owner") and not owners_match(row["owner"], owner):
        raise ValueError("Prospero connection belongs to another account")
    base = str(row.get("app_url") or "").rstrip("/")
    if urlsplit(base).scheme not in ("http", "https"):
        raise ValueError("Prospero has no HTTP address configured")
    token_file = (row.get("values") or {}).get("TOKEN_FILE")
    token = read_token_file(token_file) if token_file else ""
    return base, {"Authorization": "Bearer " + token} if token else {}


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("Invalid walkthrough identifier")
    return value


async def request(client, method, path, **kwargs):
    response = await client.request(method, path, **kwargs)
    if not response.is_success:
        raise ValueError(f"Prospero responded {response.status_code}: {response.text[:200]}")
    return response.json()


async def run(owner, args):
    try:
        base, headers = connection(owner)
        async with httpx.AsyncClient(base_url=base, headers=headers, timeout=35, trust_env=False) as client:
            if args.get("job_id"):
                job = await request(client, "GET", "/api/jobs/" + identifier(args["job_id"]))
            else:
                address = str(args.get("url") or "")
                if urlsplit(address).scheme not in ("http", "https"):
                    raise ValueError("Give the running app's http(s) address")
                payload = {k: args[k] for k in ("url", "title", "project", "steps", "hold_s") if k in args}
                data = await request(client, "POST", "/api/agent/website_walkthrough", json=payload)
                job = data["job"]
            if job.get("type") != "website_walkthrough":
                raise ValueError("This job is not a website walkthrough")
            job_id = identifier(job["id"])
            outputs = job.get("outputs") or job.get("result") or {}
            asset = outputs.get("asset_id") or next(iter(job.get("asset_ids") or []), None)
            out = {"job_id": job_id, "state": job["state"], "message": job.get("message"), "progress": job.get("progress")}
            if asset and job["state"] == "done":
                out["video_url"] = f"/api/task-walkthrough/{job_id}/video"
                out["asset_id"] = asset
                out["steps"] = outputs.get("steps") or []
            return out
    except (ValueError, KeyError, httpx.HTTPError) as exc:
        return {"error": str(exc), "exit_code": 1}


async def video(owner, job_id, range_header=None):
    base, headers = connection(owner)
    async with httpx.AsyncClient(base_url=base, headers=headers, timeout=60, trust_env=False) as client:
        job = await request(client, "GET", "/api/jobs/" + identifier(job_id))
        if job.get("type") != "website_walkthrough" or job.get("state") != "done":
            raise ValueError("The walkthrough is not ready")
        asset = identifier((job.get("outputs") or {})["asset_id"])
        response = await client.get(f"/api/assets/{asset}/file", headers={"Range": range_header} if range_header else {})
        response.raise_for_status()
        return response


class TaskWalkthroughTool:
    async def execute(self, content, ctx):
        try:
            args = content if isinstance(content, dict) else json.loads(content)
            if not isinstance(args, dict):
                raise ValueError()
        except (ValueError, TypeError):
            return {"error": "task_walkthrough expects a JSON object", "exit_code": 1}
        result = await run((ctx or {}).get("owner"), args)
        return result if result.get("error") else {"output": json.dumps(result, ensure_ascii=False), "result": result, "exit_code": 0}
