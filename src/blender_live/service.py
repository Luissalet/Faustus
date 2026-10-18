"""Shared live operation dispatch, without importing unrelated agent tools."""
import asyncio
import base64
import json
from pathlib import Path

from .client import BridgeClient


async def execute_live(action, arguments):
    client = BridgeClient(port=arguments.get("port"))
    if action == "official_tools":
        from .official import tools
        result = {"tools": [tool.model_dump(mode="json") for tool in await tools()]}
    elif action == "official_call":
        from .official import call
        content = await call(arguments["name"], arguments.get("arguments", {}))
        failed = isinstance(content, tuple) and content[1].get("status") == "error"
        if isinstance(content, tuple):
            content = content[0]
        return {"output": "\n".join(item.text for item in content if item.type == "text"),
                "images": [{"data": item.data, "mimeType": item.mimeType} for item in content if item.type == "image"],
                "exit_code": int(failed)}
    elif action == "live_probe":
        result = await asyncio.to_thread(client.probe)
    elif action == "live_submit":
        result = await asyncio.to_thread(client.submit, arguments.get("operation"), arguments.get("arguments", {}))
    elif action == "live_cancel":
        result = await asyncio.to_thread(client.cancel, arguments.get("job_id", ""))
    elif action == "live_job":
        result = await asyncio.to_thread(client.job, arguments["job_id"]) if arguments.get("job_id") else await asyncio.to_thread(client.jobs)
    else:
        raise ValueError("Unknown live action")
    out = {"output": json.dumps(result, ensure_ascii=False, default=str), "live": result,
           "exit_code": int(result.get("state") == "failed")}
    sheet = result.get("result", {}).get("sheet")
    if sheet and Path(sheet["path"]).is_file():
        out["images"] = [{"data": base64.b64encode(Path(sheet["path"]).read_bytes()).decode("ascii"), "mimeType": "image/png"}]
    return out
