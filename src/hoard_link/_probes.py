"""Best-effort probes for shared servers on loopback.

Every function here is a coroutine that takes an ``httpx.AsyncClient`` and
returns a plain dict on success or ``None`` on any failure (connection
refused, timeout, bad JSON, unexpected shape). None of them ever raises —
that is the whole point: probing five ports nobody may be listening on
must be cheap and quiet.

Two guarantees, both enforced here rather than trusted to callers:

- each HTTP request is bounded by a **wall-clock** ``PROBE_TIMEOUT_S``
  (httpx's own timeout is per phase, so a server that accepts and then
  drips bytes could otherwise hold a probe much longer);
- each public probe is wrapped by :func:`_never_raise`, so a server that
  answers 200 with JSON of an unexpected shape (a list instead of a dict,
  ``null`` fields...) yields ``None`` instead of an ``AttributeError``.
"""

from __future__ import annotations

import asyncio
import functools
from typing import Any, Awaitable, Callable, Optional, TypeVar

import httpx

PROBE_TIMEOUT_S = 1.0
LLAMACPP_PORTS = range(8080, 8091)
OLLAMA_PORT = 11434
OPENAI_COMPAT_PORT = 1234

T = TypeVar("T")


def _never_raise(default: Any) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Decorator: any ``Exception`` inside the probe becomes ``default``.

    ``asyncio.CancelledError`` is a ``BaseException`` and still propagates,
    so cancelling a caller is never swallowed.
    """

    def deco(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            try:
                return await fn(*args, **kwargs)
            except Exception:
                return default() if callable(default) else default

        return wrapper

    return deco


async def _request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    json_body: Any = None,
    headers: Optional[dict[str, str]] = None,
) -> Optional[Any]:
    timeout = PROBE_TIMEOUT_S
    try:
        resp = await asyncio.wait_for(
            client.request(method, url, json=json_body, headers=headers, timeout=timeout),
            timeout,
        )
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


async def _get_json(
    client: httpx.AsyncClient, url: str, headers: Optional[dict[str, str]] = None
) -> Optional[Any]:
    return await _request_json(client, "GET", url, headers=headers)


async def _post_json(
    client: httpx.AsyncClient,
    url: str,
    json_body: Any,
    headers: Optional[dict[str, str]] = None,
) -> Optional[Any]:
    return await _request_json(client, "POST", url, json_body=json_body, headers=headers)


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _looks_like_llamacpp_props(props: Any) -> bool:
    # Any web app on 8080-8090 could answer 200 JSON on /props (an SPA
    # fallback, another plugin's API); llama-server's always carries one of
    # these keys.
    return isinstance(props, dict) and (
        "default_generation_settings" in props or "model_path" in props
    )


@_never_raise(None)
async def probe_llamacpp_base(client: httpx.AsyncClient, base: str) -> Optional[dict]:
    """Probe one llama-server by base URL (``http://host:port``)."""
    base = base.rstrip("/")
    props = await _get_json(client, f"{base}/props")
    if not _looks_like_llamacpp_props(props):
        return None
    models, slots = await asyncio.gather(
        _get_json(client, f"{base}/v1/models"), _get_json(client, f"{base}/slots")
    )
    port = httpx.URL(base).port
    return {
        "port": port,
        "url": base,
        "props": props,
        "models": models if isinstance(models, dict) else None,
        "slots": [s for s in slots if isinstance(s, dict)] if isinstance(slots, list) else None,
    }


async def probe_llamacpp_port(client: httpx.AsyncClient, port: int) -> Optional[dict]:
    return await probe_llamacpp_base(client, f"http://127.0.0.1:{port}")


@_never_raise(list)
async def probe_llamacpp(client: httpx.AsyncClient) -> list[dict]:
    """Probe every candidate llama.cpp port in parallel.

    Returns the list of ports that answered ``/props`` (usually 0 or 1).
    """
    results = await asyncio.gather(
        *(probe_llamacpp_port(client, p) for p in LLAMACPP_PORTS)
    )
    return [r for r in results if r is not None]


def llamacpp_busy(server: dict) -> bool:
    slots = _list(server.get("slots"))
    return any(bool(_dict(s).get("is_processing")) for s in slots)


def llamacpp_supports_vision(server: dict) -> bool:
    modalities = _dict(server.get("props")).get("modalities")
    if isinstance(modalities, dict):  # newer llama-server: {"vision": true, "audio": false}
        return bool(modalities.get("vision"))
    return "vision" in _list(modalities)


async def _ollama_show(client: httpx.AsyncClient, base: str, name: str) -> list[str]:
    show = await _post_json(client, f"{base}/api/show", {"model": name})
    caps = _dict(show).get("capabilities")
    return [c for c in _list(caps) if isinstance(c, str)]


@_never_raise(None)
async def probe_ollama(
    client: httpx.AsyncClient, base: str = f"http://127.0.0.1:{OLLAMA_PORT}"
) -> Optional[dict]:
    base = base.rstrip("/")
    ps = await _get_json(client, f"{base}/api/ps")
    if not isinstance(ps, dict):
        return None
    names: list[str] = []
    raws: list[dict] = []
    for m in _list(ps.get("models")):
        m = _dict(m)
        name = m.get("model") or m.get("name")
        if isinstance(name, str) and name:
            names.append(name)
            raws.append(m)
    tags, *caps = await asyncio.gather(
        _get_json(client, f"{base}/api/tags"),
        *(_ollama_show(client, base, n) for n in names),
    )
    resident = [
        {"name": n, "capabilities": c, "raw": r} for n, c, r in zip(names, caps, raws)
    ]
    tag_list = [t for t in _list(_dict(tags).get("models")) if isinstance(t, dict)]
    return {"url": base, "resident": resident, "tags": tag_list}


async def ollama_show_capabilities(client: httpx.AsyncClient, base: str, name: str) -> list[str]:
    """Capabilities of one (possibly non-resident) Ollama model; never raises."""
    try:
        return await _ollama_show(client, base.rstrip("/"), name)
    except Exception:
        return []


@_never_raise(None)
async def probe_openai_compat(
    client: httpx.AsyncClient, port: int = OPENAI_COMPAT_PORT
) -> Optional[dict]:
    base = f"http://127.0.0.1:{port}"
    models = await _get_json(client, f"{base}/v1/models")
    ids = [
        m.get("id")
        for m in _list(_dict(models).get("data"))
        if isinstance(m, dict) and isinstance(m.get("id"), str)
    ]
    if not ids:
        return None
    return {"url": base, "models": ids}


@_never_raise(None)
async def probe_comfy(
    client: httpx.AsyncClient, url: str = "http://127.0.0.1:8188"
) -> Optional[dict]:
    base = url.rstrip("/")
    stats = await _get_json(client, f"{base}/system_stats")
    if not isinstance(stats, dict):
        return None
    checkpoints_info = await _get_json(
        client, f"{base}/object_info/CheckpointLoaderSimple"
    )
    checkpoints: list[str] = []
    try:
        raw = checkpoints_info["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
        checkpoints = [c for c in raw if isinstance(c, str)]
    except (KeyError, IndexError, TypeError):
        checkpoints = []
    return {"url": base, "system_stats": stats, "checkpoints": checkpoints}
