"""Thin async client for a ComfyUI server (``/prompt``, `/history`, `/view`...).

Kept deliberately small: it does not know about specific workflows, only
about the ComfyUI HTTP surface. ``queue()`` validates that it was handed
the **API format** (a dict of node-id -> ``{class_type, inputs}``), not the
UI's ``{"nodes": [...], "links": [...]}`` export, because that mistake is
easy to make and produces a confusing 400 otherwise.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Optional

import httpx

from .errors import BackendError
from .types import OutputFile

_OUTPUT_KINDS = ("images", "gifs", "videos", "audio")


def _looks_like_ui_format(workflow: Any) -> bool:
    return isinstance(workflow, dict) and (
        isinstance(workflow.get("nodes"), list) or isinstance(workflow.get("links"), list)
    )


def _looks_like_api_format(workflow: Any) -> bool:
    if not isinstance(workflow, dict) or not workflow:
        return False
    for value in workflow.values():
        if not isinstance(value, dict):
            return False
        if "class_type" not in value or "inputs" not in value:
            return False
    return True


def _check(resp: httpx.Response) -> httpx.Response:
    if resp.status_code >= 400:
        # /prompt answers 400 with {"error": ..., "node_errors": ...}; keep
        # that body, it is the only useful part of the failure.
        raise BackendError("comfyui", resp.status_code, resp.text[:500])
    return resp


def _execution_error(prompt_id: str, status: dict) -> str:
    for msg in status.get("messages") or []:
        if isinstance(msg, list) and len(msg) == 2 and msg[0] == "execution_error":
            info = msg[1] if isinstance(msg[1], dict) else {}
            return (
                f"job {prompt_id} failed in node {info.get('node_id')} "
                f"({info.get('node_type')}): {info.get('exception_message', '')}"
            )[:500]
    return f"job {prompt_id} finished with status 'error'"


class ComfyClient:
    """Every method raises :class:`BackendError` on an HTTP error status and
    lets ``httpx`` transport errors (server gone) propagate as they are."""

    def __init__(self, url: str, client: Optional[httpx.AsyncClient] = None):
        self.url = url.rstrip("/")
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def system_stats(self) -> dict:
        resp = await self._client.get(f"{self.url}/system_stats", timeout=5.0)
        _check(resp)
        return resp.json()

    async def object_info(self, node: Optional[str] = None) -> dict:
        path = f"/object_info/{node}" if node else "/object_info"
        resp = await self._client.get(f"{self.url}{path}", timeout=5.0)
        _check(resp)
        return resp.json()

    async def upload_image(
        self, data: bytes, filename: str, overwrite: bool = True
    ) -> dict:
        files = {"image": (filename, data)}
        form = {"overwrite": "true" if overwrite else "false"}
        resp = await self._client.post(
            f"{self.url}/upload/image", files=files, data=form, timeout=15.0
        )
        _check(resp)
        return resp.json()

    async def queue(self, workflow: dict, client_id: str) -> str:
        if _looks_like_ui_format(workflow):
            raise ValueError(
                "This workflow looks like the ComfyUI UI export "
                "({'nodes': [...], 'links': [...]}). ComfyClient.queue() needs "
                "the API format instead: a dict of node id -> "
                "{'class_type': ..., 'inputs': {...}}. In the ComfyUI web UI, "
                "use 'Save (API Format)', not 'Save'."
            )
        if not _looks_like_api_format(workflow):
            raise ValueError(
                "This does not look like a ComfyUI API-format workflow: every "
                "value must be a dict with 'class_type' and 'inputs'."
            )
        resp = await self._client.post(
            f"{self.url}/prompt",
            json={"prompt": workflow, "client_id": client_id},
            timeout=10.0,
        )
        _check(resp)
        data = resp.json()
        prompt_id = data.get("prompt_id")
        if not prompt_id:
            raise ValueError(f"ComfyUI did not return a prompt_id: {data!r}")
        return prompt_id

    async def wait(
        self,
        prompt_id: str,
        timeout_s: float = 120.0,
        on_progress: Optional[Callable[[dict], None]] = None,
        poll_interval_s: float = 1.0,
    ) -> dict:
        deadline = time.monotonic() + timeout_s
        while True:
            resp = await self._client.get(
                f"{self.url}/history/{prompt_id}", timeout=5.0
            )
            _check(resp)
            history = resp.json()
            entry = history.get(prompt_id) if isinstance(history, dict) else None
            if entry:
                status = entry.get("status") or {}
                if status.get("status_str") == "error":
                    raise BackendError("comfyui", 200, _execution_error(prompt_id, status))
                return entry
            if on_progress is not None:
                on_progress({"prompt_id": prompt_id, "status": "pending"})
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"ComfyUI job {prompt_id} did not finish within {timeout_s}s"
                )
            await asyncio.sleep(poll_interval_s)

    async def outputs(self, prompt_id: str) -> list[OutputFile]:
        resp = await self._client.get(f"{self.url}/history/{prompt_id}", timeout=5.0)
        _check(resp)
        history = resp.json()
        entry = history.get(prompt_id) or {}
        outputs_by_node = entry.get("outputs") or {}
        result: list[OutputFile] = []
        for node_id, node_out in outputs_by_node.items():
            for kind in _OUTPUT_KINDS:
                for f in node_out.get(kind, []) or []:
                    result.append(
                        OutputFile(
                            node_id=node_id,
                            filename=f["filename"],
                            subfolder=f.get("subfolder", ""),
                            type=f.get("type", "output"),
                            kind=kind[:-1] if kind.endswith("s") else kind,
                        )
                    )
        return result

    async def download(self, output: OutputFile) -> bytes:
        params = {
            "filename": output.filename,
            "subfolder": output.subfolder,
            "type": output.type,
        }
        resp = await self._client.get(f"{self.url}/view", params=params, timeout=30.0)
        _check(resp)
        return resp.content

    async def interrupt(self) -> None:
        resp = await self._client.post(f"{self.url}/interrupt", timeout=5.0)
        _check(resp)

    async def free(
        self, unload_models: bool = False, free_memory: bool = False
    ) -> None:
        resp = await self._client.post(
            f"{self.url}/free",
            json={"unload_models": unload_models, "free_memory": free_memory},
            timeout=5.0,
        )
        _check(resp)
