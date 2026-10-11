"""The one class every vendoring app imports: :class:`Link`.

See the package README for the resolution order and policies; this module
is the orchestration of :mod:`._probes` and :mod:`._faustus` into that
order, plus the four actions (`chat`, `embed`, `tts`, `comfy`) and the
`wait_idle` / `status` helpers.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx

from . import _faustus, _probes
from ._comfy import ComfyClient
from ._sync import SyncFacade
from .config import CapabilityConfig, LinkConfig
from .errors import BackendError, Unavailable
from .gpu import GpuMemory, gpu_free_mb
from .lease import Lease, LeaseError, LeaseTimeout
from . import reasoning as _reasoning
from . import routes as _routes
from .types import CAPABILITIES, ChatResult, Resolution, Usage

_THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)
_THINK_OPEN_RE = re.compile(r"<think>", re.IGNORECASE)
_THINK_CLOSE_RE = re.compile(r"</think>", re.IGNORECASE)
_CACHE_TTL_S = 30.0
# Per-URL llama.cpp base probes (registry residency / status) stay fresh for Hub's
# ~5 s polls without repeating the same /health+/props sweep on every capability.
_BASE_PROBE_CACHE_TTL_S = 2.5
# The last model registry Faustus returned, kept for when it is too busy to answer within the short timeout.
_REGISTRY_MEMORY_TTL_S = 900.0
_last_registry: Optional[tuple[dict, float]] = None


def _remember_registry(data: dict) -> None:
    global _last_registry
    _last_registry = (data, time.monotonic())


def _remembered_registry() -> Optional[tuple[dict, float]]:
    """``(registry, age_s)`` while the last registry is recent enough to try, else None."""
    if _last_registry is None:
        return None
    age = time.monotonic() - _last_registry[1]
    return (_last_registry[0], age) if age <= _REGISTRY_MEMORY_TTL_S else None


_log = logging.getLogger("hoard_link")
TTS_COMMAND_TIMEOUT_S = 120.0

# Endpoint suffixes someone may paste into a URL field; stripped to get
# back to the server root before appending the path each call needs.
_ENDPOINT_SUFFIXES = (
    "/chat/completions",
    "/completions",
    "/embeddings",
    "/api/chat",
    "/api/generate",
    "/api/embed",
    "/api/embeddings",
)

_LLAMACPP_BACKENDS = {"llamacpp", "llama.cpp", "llama-server", "llama_cpp"}


def _host(url: Optional[str]) -> str:
    if not url:
        return "?"
    parts = urlsplit(url)
    return parts.netloc or url


def _strip_endpoint(url: str) -> str:
    """``http://h:1/v1/chat/completions`` -> ``http://h:1/v1`` (keeps ``/v1``)."""
    u = url.rstrip("/")
    for suffix in _ENDPOINT_SUFFIXES:
        if u.lower().endswith(suffix):
            return u[: -len(suffix)].rstrip("/")
    return u


def _server_root(url: str) -> str:
    """``http://h:1/v1/chat/completions`` -> ``http://h:1``."""
    u = _strip_endpoint(url)
    if u.lower().endswith("/v1"):
        u = u[:-3]
    return u.rstrip("/")


def _openai_endpoint(url: str, path: str) -> str:
    """Accept a base URL, a ``/v1`` URL or a full endpoint URL alike."""
    u = url.rstrip("/")
    if u.lower().endswith("/v1" + path):
        return u
    base = _strip_endpoint(u)
    if not base.lower().endswith("/v1"):
        base += "/v1"
    return base + path


def _ollama_endpoint(url: str, path: str) -> str:
    return _server_root(url) + path


def _strip_think(text: Optional[str]) -> tuple[str, Optional[str]]:
    """Split reasoning out of a reasoning model's answer.

    Handles the three shapes seen in practice:

    - ``<think>...</think>answer`` (one or more closed blocks);
    - ``...reasoning</think>answer`` — the chat template already opened
      ``<think>`` inside the prompt, so the output only has the close tag;
    - ``answer? <think>reasoning`` with no close tag — generation was cut
      off by ``max_tokens`` mid-thought; everything after the tag is
      reasoning, never answer.
    """
    if not text:
        return "", None
    parts: list[str] = []
    found = False

    first_close = _THINK_CLOSE_RE.search(text)
    first_open = _THINK_OPEN_RE.search(text)
    if first_close and (first_open is None or first_close.start() < first_open.start()):
        parts.append(text[: first_close.start()])
        text = text[first_close.end():]
        found = True

    blocks = _THINK_RE.findall(text)
    if blocks:
        parts.extend(blocks)
        text = _THINK_RE.sub("", text)
        found = True

    dangling = _THINK_OPEN_RE.search(text)
    if dangling:
        parts.append(text[dangling.end():])
        text = text[: dangling.start()]
        found = True

    if not found:
        return text, None
    reasoning = "\n\n".join(p.strip() for p in parts if p.strip())
    return text.strip(), reasoning or None


def _join_reasoning(*parts: Optional[str]) -> Optional[str]:
    kept = [p.strip() for p in parts if isinstance(p, str) and p.strip()]
    return "\n\n".join(kept) if kept else None


def _extract_usage(raw: Any, api: Optional[str]) -> Usage:
    if not isinstance(raw, dict):
        return Usage()
    if api == "ollama":
        prompt = raw.get("prompt_eval_count")
        completion = raw.get("eval_count")
        total = None
        if isinstance(prompt, int) and isinstance(completion, int):
            total = prompt + completion
        return Usage(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total)
    u = raw.get("usage")
    if not isinstance(u, dict):
        return Usage()
    return Usage(
        prompt_tokens=u.get("prompt_tokens"),
        completion_tokens=u.get("completion_tokens"),
        total_tokens=u.get("total_tokens"),
    )


def _b64_image(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _ollama_format(response_format: Optional[dict[str, Any]]) -> Any:
    """Map an OpenAI ``response_format`` onto Ollama's ``format`` field."""
    if not isinstance(response_format, dict):
        return None
    kind = response_format.get("type")
    if kind == "json_object":
        return "json"
    if kind == "json_schema":
        schema = (response_format.get("json_schema") or {}).get("schema")
        return schema if isinstance(schema, dict) else "json"
    return None


def _explicit_api(cc: CapabilityConfig) -> str:
    if cc.api in ("openai", "ollama"):
        return cc.api
    if (cc.provider or "").lower() == "ollama":
        return "ollama"
    path = urlsplit(cc.url or "").path
    if path.startswith("/api/"):
        return "ollama"
    return "openai"


def _ollama_fits(capability: str, caps: list[str]) -> bool:
    if capability == "llm":
        # Empty = an older Ollama that does not report capabilities; accept.
        # Otherwise an embedding-only model must never be chosen for chat.
        return not caps or "completion" in caps
    needed = {"vision": "vision", "embeddings": "embedding"}.get(capability)
    return needed is None or needed in caps


def _tag_size_mb(tags: Any, name: Optional[str]) -> Optional[int]:
    """Size on disk of an installed Ollama model, from ``/api/tags``."""
    for t in tags or []:
        if isinstance(t, dict) and name in (t.get("name"), t.get("model")) and isinstance(t.get("size"), (int, float)):
            return int(t["size"] // (1024 * 1024))
    return None


def _load_vram_mb(size_mb: Any) -> Optional[int]:
    """Rough VRAM for loading a model file: weights + 20% + 512 MiB of
    context. Only used when the capability has no ``vram_mb`` configured."""
    if isinstance(size_mb, (int, float)) and size_mb > 0:
        return int(size_mb * 1.2) + 512
    return None


class _NoLease:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _GuardedLease:
    """A :class:`Lease` whose refusals never break the call: a request the
    hub rejects outright (e.g. an estimate larger than any single GPU, for a
    model Ollama would split across several) proceeds without a lease, and a
    queue wait past ``lease_timeout_s`` surfaces as :class:`Unavailable`."""

    def __init__(self, capability: str, lease: Lease):
        self.capability = capability
        self.lease = lease

    async def __aenter__(self) -> Lease:
        try:
            return await self.lease.aacquire()
        except LeaseTimeout as exc:
            raise Unavailable(self.capability, [f"GPU busy: {exc}"]) from exc
        except LeaseError as exc:
            _log.warning("%s; loading without a GPU lease", exc)
            return self.lease

    async def __aexit__(self, *exc: Any) -> None:
        await self.lease.arelease()


def _prefer(names: list[str], preferred: Optional[str]) -> list[str]:
    if preferred and preferred in names:
        return [preferred] + [n for n in names if n != preferred]
    return names


def _prefer_many(names: list[str], prefs: list[str]) -> list[str]:
    """``names`` reordered by the measured preferences: a name that matches an
    earlier preference goes first (see :func:`hoard_link.routes.names_match`);
    names that match none keep their original order after them."""
    if not prefs:
        return names
    ranked: list[tuple[int, int, str]] = []
    for i, name in enumerate(names):
        rank = next((j for j, pref in enumerate(prefs) if _routes.names_match(name, pref)), len(prefs))
        ranked.append((rank, i, name))
    ranked.sort(key=lambda t: (t[0], t[1]))
    return [name for _r, _i, name in ranked]


@dataclasses.dataclass(frozen=True)
class _RouteCtx:
    """The measured routes in force for one resolution."""

    capability: str
    task: Optional[str]
    routes: _routes.Routes
    prefs: tuple[str, ...]

    def note(self) -> str:
        return f"; ranked by measured routes ({'task ' + self.task if self.task else self.capability})"

    def details(self) -> dict[str, Any]:
        return {"task": self.task, "source": self.routes.source, "updated_at": self.routes.updated_at}


def _promoted(model: Optional[str], ranked: list[str], base: list[str]) -> bool:
    """True when the measured routes moved ``model`` ahead of where the
    original order (with the explicit preference) had it."""
    return model in ranked and model in base and ranked.index(model) < base.index(model)


class Link:
    """Resolves and calls the shared model backend for one app."""

    def __init__(self, config: LinkConfig, client: Optional[httpx.AsyncClient] = None):
        self.config = config
        # trust_env=False: loopback traffic must never be routed through an
        # HTTP(S)_PROXY from the environment (a proxy would answer instead
        # of "connection refused", and nothing here should leave the box).
        self._client = client or httpx.AsyncClient(trust_env=False)
        self._owns_client = client is None
        self._cache: dict[str, tuple[float, "asyncio.Future[Any]", Any]] = {}
        # Overridable by tests: replace with a fake clock / no-op sleep to
        # test wait_idle() timing without real delays.
        self._now = time.monotonic
        self._sleep = asyncio.sleep
        self.sync = SyncFacade(self)

    async def aclose(self) -> None:
        self.sync.close()
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "Link":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # caching of probes (30 s, single-flight)
    # ------------------------------------------------------------------

    async def _cached(self, key: str, factory: Any, ttl_s: float | None = None) -> Any:
        """Cache a probe; concurrent callers share one in-flight probe.

        Without single-flight, ``status()`` resolving eight capabilities in
        parallel would fire eight identical sweeps of eleven llama.cpp
        ports at once. Default TTL is 30 s; per-URL base probes use a shorter
        window so residency stays fresh under Hub polling.

        An in-flight Future is always reused, even when its start timestamp is
        older than ``ttl_s``. Only a *completed* result expires; otherwise a
        slow probe that outlasts the TTL would launch a second factory.
        """
        ttl = _CACHE_TTL_S if ttl_s is None else ttl_s
        loop = asyncio.get_running_loop()
        now = self._now()
        hit = self._cache.get(key)
        reuse = (
            hit is not None
            and hit[2] is loop
            and (not hit[1].done() or now - hit[0] < ttl)
        )
        if not reuse:
            hit = (now, asyncio.ensure_future(factory()), loop)
            self._cache[key] = hit
        try:
            return await asyncio.shield(hit[1])
        except asyncio.CancelledError:
            raise
        except Exception:
            if self._cache.get(key) is hit:
                del self._cache[key]
            raise

    async def _probe_llamacpp_base(self, base: str) -> Any:
        base = base.rstrip("/")
        return await self._cached(
            f"llamacpp_base:{base}",
            lambda: _probes.probe_llamacpp_base(self._client, base),
            ttl_s=_BASE_PROBE_CACHE_TTL_S,
        )

    async def _probe_llamacpp(self) -> list[dict]:
        return await self._cached("llamacpp", lambda: _probes.probe_llamacpp(self._client))

    async def _probe_ollama(self, base: str = f"http://127.0.0.1:{_probes.OLLAMA_PORT}") -> Optional[dict]:
        base = base.rstrip("/")
        return await self._cached(f"ollama:{base}", lambda: _probes.probe_ollama(self._client, base))

    async def _probe_openai_compat(self) -> Optional[dict]:
        return await self._cached(
            "openai_compat", lambda: _probes.probe_openai_compat(self._client)
        )

    async def _probe_comfy(self, url: str) -> Optional[dict]:
        return await self._cached(f"comfy:{url}", lambda: _probes.probe_comfy(self._client, url))

    async def _gpus(self) -> list[GpuMemory]:
        # nvidia-smi is a blocking subprocess: keep it off the event loop.
        return await self._cached("gpu", lambda: asyncio.to_thread(gpu_free_mb))

    async def _faustus_url(self) -> Optional[str]:
        return await self._cached(
            "faustus_url",
            lambda: _faustus.find_reachable(self._client, self.config.faustus_urls),
        )

    # ------------------------------------------------------------------
    # resolution
    # ------------------------------------------------------------------

    def _may_load(self, capability: str) -> bool:
        return self.config.capability(capability).allow_load or not self.config.only_resident

    def _routes(self) -> _routes.Routes:
        """The measured routes file (cached by mtime); empty when routes are switched off."""
        if not self.config.use_routes:
            return _routes.Routes(problem="routes disabled")
        return _routes.load_routes(self.config.routes_file)

    def _route_ctx(self, capability: str, task: Optional[str]) -> Optional[_RouteCtx]:
        if not self.config.use_routes:
            return None
        routes = _routes.load_routes(self.config.routes_file)
        prefs = routes.preferences(capability, task)
        return _RouteCtx(capability, task, routes, tuple(prefs)) if prefs else None

    @staticmethod
    def _rank(
        names: list[str], preferred: Optional[str], ctx: Optional[_RouteCtx]
    ) -> tuple[list[str], list[str]]:
        """``(ranked, base)``: the explicit preference first, then the measured
        routes, then the original order; ``base`` is the same without routes."""
        base = _prefer(names, preferred)
        if ctx is None:
            return base, base
        return _prefer(_prefer_many(names, list(ctx.prefs)), preferred), base

    async def resolve(self, capability: str, task: Optional[str] = None) -> Resolution:
        """Which server and model to use for ``capability``.

        ``task`` (e.g. ``"code"``) names a measured route from
        ``~/.hoard/routes.json``: among the candidates resolution already
        considers, the model measured best for that task is preferred."""
        if capability not in CAPABILITIES:
            raise ValueError(f"unknown capability {capability!r}, expected one of {CAPABILITIES}")

        reasons: list[str] = []
        ctx = self._route_ctx(capability, task)

        explicit = self._resolve_explicit(capability)
        if explicit is not None:
            return explicit

        faustus_url = await self._faustus_url()
        if faustus_url:
            res = await self._resolve_from_faustus(capability, faustus_url, reasons, ctx)
            if res is not None:
                return res
        elif _remembered_registry() is not None:
            # Faustus did not answer its health check in time (a long turn can hold it), but it listed its
            # servers recently: try those (each one is verified) before loading anything on this PC.
            reasons.append("Faustus not reachable on configured/default ports right now")
            res = await self._resolve_from_faustus(capability, None, reasons, ctx)
            if res is not None:
                return res
        else:
            reasons.append("Faustus not reachable on configured/default ports")

        res = await self._resolve_from_loopback(capability, reasons, ctx)
        if res is not None:
            return res

        return Resolution(
            capability=capability,
            provider=None,
            url=None,
            model=None,
            api=None,
            state="unavailable",
            reason="; ".join(reasons) if reasons else "no source available",
            details={"reasons": reasons},
        )

    def _resolve_explicit(self, capability: str) -> Optional[Resolution]:
        cc = self.config.capability(capability)
        if not cc.explicit:
            return None
        if cc.command:
            reason = f"{capability} -> configured command, explicit configuration"
            return Resolution(
                capability=capability,
                provider=cc.provider or "configured-command",
                url=None,
                model=cc.model,
                api=None,
                state="resolved",
                reason=reason,
                details={"source": "explicit", "command": cc.command},
            )
        api = _explicit_api(cc)
        provider = cc.provider or "configured"
        model_part = f" ({cc.model})" if cc.model else ""
        reason = f"{capability} -> {provider} at {_host(cc.url)}{model_part}, explicit configuration"
        return Resolution(
            capability=capability,
            provider=provider,
            url=cc.url,
            model=cc.model,
            api=api,  # type: ignore[arg-type]
            state="resolved",
            reason=reason,
            details={"source": "explicit"},
        )

    async def _resolve_from_faustus(
        self, capability: str, faustus_url: Optional[str], reasons: list[str], ctx: Optional[_RouteCtx] = None
    ) -> Optional[Resolution]:
        """``faustus_url`` None means Faustus did not answer its health check: only a recent registry is tried."""
        token = self.config.faustus_token
        headers = _faustus.auth_headers(token)
        if faustus_url:
            status, data = await _faustus.get(self._client, f"{faustus_url}/api/models", headers=headers)
        else:
            status, data = None, None
        if status == 200 and isinstance(data, dict):
            _remember_registry(data)
        elif status is None:
            remembered = _remembered_registry()
            if remembered is not None:
                # A busy Faustus can miss the short timeout; the servers it listed a moment ago are still
                # verified one by one below, so a stale entry is skipped rather than trusted.
                reasons.append(
                    f"Faustus /api/models did not answer; using the registry it gave {remembered[1]:.0f}s ago"
                )
                status, data = 200, remembered[0]

        if status == 200 and isinstance(data, dict):
            matches = _faustus.find_model_items(_faustus.model_items(data), capability)
            local = [i for i in matches if _faustus.is_local_item(i)]
            # A model another of the person's machines is already serving (the Sparks) goes before anything this
            # PC would have to hold or load on its own GPUs; the registry's order decides within each group.
            local.sort(key=lambda i: 0 if _faustus.is_lan_item(i) else 1)
            if matches and not local:
                reasons.append(
                    f"Faustus registry lists only non-local servers for '{capability}'; "
                    "skipped to keep data on this machine and its local network"
                )
            elif not matches:
                reasons.append(f"Faustus registry has no server for capability '{capability}'")
            for item in local:
                res = await self._resolution_from_registry_item(capability, item, reasons, ctx)
                if res is not None:
                    return res
        elif status in (401, 403):
            if token:
                reasons.append(
                    f"Faustus reachable but /api/models rejected the token (401/403, got HTTP {status})"
                )
            else:
                reasons.append(
                    f"Faustus reachable but /api/models needs a token (HTTP {status}); "
                    "set HOARD_FAUSTUS_TOKEN or faustus.token in backend.json"
                )
        elif status is None:
            reasons.append("Faustus /api/models request failed to connect")
        else:
            reasons.append(f"Faustus /api/models returned HTTP {status}")

        if capability in ("tts", "stt") and faustus_url:
            svc_status, svc_data = await _faustus.get(
                self._client, f"{faustus_url}/api/{capability}/capabilities", headers=headers
            )
            disabled = (
                svc_status == 200
                and isinstance(svc_data, dict)
                and (
                    svc_data.get("ready") is False
                    or str(svc_data.get("provider") or "").lower() in ("disabled", "none", "off")
                )
            )
            if disabled:
                # Seen on a real Faustus: the service answers 200 with
                # provider "disabled" and ready false. Calling synthesize
                # there fails, so it must not count as resolved.
                reasons.append(
                    f"Faustus {capability.upper()} is switched off "
                    f"(provider={svc_data.get('provider')!r}, ready={svc_data.get('ready')!r}); "
                    f"enable it in Faustus Settings -> Voice"
                )
            elif svc_status == 200:
                reason = f"{capability} -> Faustus {capability} service at {_host(faustus_url)}, native Faustus service"
                return Resolution(
                    capability=capability,
                    provider=f"faustus_{capability}",
                    url=faustus_url,
                    model=None,
                    api=None,
                    state="resolved",
                    reason=reason,
                    details={"source": f"faustus_{capability}", "capabilities": svc_data},
                )
            elif svc_status in (401, 403):
                reasons.append(
                    f"Faustus {capability.upper()} needs a browser session; token not allowed"
                )
            elif svc_status is None:
                reasons.append(f"Faustus {capability.upper()} service request failed to connect")
            else:
                reasons.append(f"Faustus {capability}/capabilities returned HTTP {svc_status}")

        return None

    async def _resolution_from_registry_item(
        self, capability: str, item: dict, reasons: list[str], ctx: Optional[_RouteCtx] = None
    ) -> Optional[Resolution]:
        backend = str(item.get("backend") or "?")
        provider = "llamacpp" if backend.lower() in _LLAMACPP_BACKENDS else backend
        api = _faustus.api_for_backend(backend.lower())
        url = item["url"]
        listed = [m for m in (item.get("models") or []) if isinstance(m, str) and m]
        preferred = self.config.capability(capability).model
        resident: Optional[bool]
        size_mb: Optional[int] = None
        by_routes = False

        if api == "ollama":
            # The registry lists what Faustus *can* use, not what is loaded:
            # cross-check with that Ollama's /api/ps before claiming residency.
            ollama = await self._probe_ollama(_server_root(url))
            if ollama is None:
                reasons.append(f"Faustus registry names Ollama at {_host(url)} but it does not answer /api/ps")
                return None
            loaded = [m["name"] for m in ollama["resident"]]
            candidates, base = self._rank([m for m in listed if m in loaded], preferred, ctx)
            if candidates:
                model, resident = candidates[0], True
                by_routes = _promoted(model, candidates, base)
            elif self._may_load(capability) and listed:
                ranked, base = self._rank(listed, preferred, ctx)
                model, resident = ranked[0], False
                by_routes = _promoted(model, ranked, base)
                size_mb = _tag_size_mb(ollama.get("tags"), model)
            else:
                reasons.append(
                    f"Faustus registry lists Ollama models for '{capability}' but none is resident "
                    "(only_resident=True)"
                )
                return None
        elif provider == "llamacpp":
            # Faustus' registry describes configured endpoints, not live
            # processes. Verify this exact URL before reporting it resident.
            # The shared probe checks llama.cpp's /props signature (so a
            # generic OpenAI-compatible server is not mistaken for it) and
            # also reads model identity from /v1/models or model_path.
            server = await self._probe_llamacpp_base(_server_root(url))
            if server is None:
                reasons.append(
                    f"Faustus registry names llama.cpp at {_host(url)} but it is offline, "
                    "not ready, or did not identify itself as llama.cpp"
                )
                return None
            aliases = self._llamacpp_aliases(server)
            matching = [name for name in listed if any(_routes.names_match(name, alias) for alias in aliases)]
            if not matching:
                actual = self._llamacpp_model_name(server) or "unknown model"
                reasons.append(
                    f"Faustus registry model does not match the llama.cpp model at {_host(url)} "
                    f"({actual})"
                )
                return None
            ranked, base = self._rank(matching, preferred, ctx)
            model = ranked[0]
            by_routes = _promoted(model, ranked, base)
            resident = True
        elif _faustus.is_lan_item(item) or api == "openai":
            # Any other OpenAI-compatible server (vLLM on the Sparks, a server on this PC Faustus only knows by URL): it is
            # only worth choosing while it answers and still serves the model Faustus lists, and then it is resident by
            # definition (nothing loads). A registry entry left behind by a stopped server is skipped instead of called.
            status, served_data = await _faustus.get(self._client, _openai_endpoint(url, "/models"))
            served = [m.get("id") for m in (served_data or {}).get("data", []) if isinstance(m, dict) and m.get("id")] \
                if status == 200 and isinstance(served_data, dict) else []
            if not served:
                reasons.append(f"Faustus registry names {backend} at {_host(url)} but it does not answer /v1/models")
                return None
            matching = [m for m in listed if m in served] or ([] if listed else served)
            if not matching:
                reasons.append(f"the server at {_host(url)} serves {', '.join(served)}, not {', '.join(listed)}")
                return None
            ranked, base = self._rank(matching, preferred, ctx)
            model = ranked[0]
            by_routes = _promoted(model, ranked, base)
            resident = True
        else:
            ranked, base = self._rank(listed, preferred, ctx)
            model = ranked[0] if listed else None
            by_routes = bool(listed) and _promoted(model, ranked, base)
            resident = None

        details_extra: dict[str, Any] = {}
        if api == "ollama" and resident is False and size_mb is not None:
            details_extra["size_mb"] = size_mb
        if provider == "llamacpp":
            details_extra["served_model"] = self._llamacpp_model_name(server)
            details_extra["resident"] = True
        tail = {True: "; resident", False: "; would load", None: ""}[resident]
        reason = (
            f"{capability} -> {backend} at {_host(url)} ({model}), from Faustus registry{tail}"
        )
        if by_routes and ctx is not None:
            reason += ctx.note()
            details_extra["routes"] = ctx.details()
        return Resolution(
            capability=capability,
            provider=provider,
            url=url,
            model=model,
            api=api,  # type: ignore[arg-type]
            state="resolved",
            reason=reason,
            details={
                "source": "faustus_registry",
                "endpoint_id": item.get("endpoint_id"),
                "endpoint_name": item.get("endpoint_name"),
                "category": item.get("category"),
                "resident": resident,
                **details_extra,
            },
        )

    async def _resolve_from_loopback(
        self, capability: str, reasons: list[str], ctx: Optional[_RouteCtx] = None
    ) -> Optional[Resolution]:
        if capability in ("llm", "vision"):
            res = await self._loopback_llamacpp(capability, reasons, ctx)
            if res is not None:
                return res
            res = await self._loopback_ollama(capability, reasons, ctx)
            if res is not None:
                return res
            if capability == "llm":
                res = await self._loopback_openai_compat(reasons, ctx)
                if res is not None:
                    return res
            return None

        if capability == "embeddings":
            return await self._loopback_ollama(capability, reasons, ctx)

        if capability in ("image", "video"):
            return await self._loopback_comfy(capability, reasons)

        reasons.append(
            f"no loopback provider implemented for '{capability}' outside explicit configuration/Faustus"
        )
        return None

    @staticmethod
    def _llamacpp_aliases(server: dict) -> list[str]:
        """Every name a llama-server's model goes by: the served id and the GGUF file name."""
        names = [Link._llamacpp_model_name(server)]
        path = (server.get("props") or {}).get("model_path")
        if isinstance(path, str) and path:
            names.append(path.replace("\\", "/").rsplit("/", 1)[-1])
        return [n for n in names if n]

    async def _loopback_llamacpp(
        self, capability: str, reasons: list[str], ctx: Optional[_RouteCtx] = None
    ) -> Optional[Resolution]:
        servers = await self._probe_llamacpp()
        if not servers:
            reasons.append("no llama.cpp server found on ports 8080-8090")
            return None
        suitable = [s for s in servers
                    if not (capability == "vision" and not _probes.llamacpp_supports_vision(s))]
        if suitable:
            s = suitable[0]
            by_routes = False
            if ctx is not None and len(suitable) > 1:
                # Several resident servers: the one whose model was measured best wins.
                explicit = self.config.capability(capability).model

                def rank(server: dict) -> tuple[int, int]:
                    aliases = self._llamacpp_aliases(server)
                    if explicit and explicit in aliases:          # config/env still wins
                        return (0, 0)
                    return (1, next((j for j, pref in enumerate(ctx.prefs)
                                     if any(_routes.names_match(a, pref) for a in aliases)), len(ctx.prefs)))
                best = min(suitable, key=rank)       # min keeps the first of equals: original order
                if best is not s and rank(best)[1] < len(ctx.prefs) and rank(best)[0] == 1:
                    s, by_routes = best, True
                elif best is not s:
                    s = best
            model = self._llamacpp_model_name(s)
            busy = _probes.llamacpp_busy(s)
            reason = (
                f"{capability} -> llama.cpp at {_host(s['url'])} ({model}), "
                f"shared loopback server; resident"
            )
            if busy:
                reason += "; busy"
            details: dict[str, Any] = {"source": "loopback", "busy": busy, "port": s["port"], "resident": True}
            if by_routes and ctx is not None:
                reason += ctx.note()
                details["routes"] = ctx.details()
            return Resolution(
                capability=capability,
                provider="llamacpp",
                url=f"{s['url']}/v1/chat/completions",
                model=model,
                api="openai",
                state="resolved",
                reason=reason,
                details=details,
            )
        reasons.append(f"llama.cpp server found but does not support '{capability}' (no vision modality)")
        return None

    @staticmethod
    def _llamacpp_model_name(server: dict) -> Optional[str]:
        models_resp = server.get("models")
        if isinstance(models_resp, dict):
            data = models_resp.get("data")
            if isinstance(data, list) and data and isinstance(data[0], dict) and data[0].get("id"):
                return data[0]["id"]
        props = server.get("props") or {}
        model_path = props.get("model_path")
        if isinstance(model_path, str) and model_path:
            # Windows paths (C:\models\x.gguf) must work on any host OS.
            return model_path.replace("\\", "/").rsplit("/", 1)[-1]
        return None

    async def _loopback_ollama(
        self, capability: str, reasons: list[str], ctx: Optional[_RouteCtx] = None
    ) -> Optional[Resolution]:
        ollama = await self._probe_ollama()
        if not ollama:
            reasons.append("Ollama not reachable on 11434")
            return None

        preferred = self.config.capability(capability).model
        fitting = {
            m["name"]: m for m in ollama["resident"] if _ollama_fits(capability, m["capabilities"])
        }
        model: Optional[str] = None
        resident = True
        by_routes = False
        names, base = self._rank(list(fitting), preferred, ctx)
        if names:
            model = names[0]
            by_routes = _promoted(model, names, base)
        elif self._may_load(capability):
            loaded = {m["name"] for m in ollama["resident"]}
            tag_names = [
                t.get("name") or t.get("model") for t in ollama.get("tags") or []
            ]
            tag_names = [n for n in tag_names if isinstance(n, str) and n and n not in loaded]
            ranked_tags, base_tags = self._rank(tag_names, preferred, ctx)
            for name in ranked_tags[:8]:
                caps = await _probes.ollama_show_capabilities(self._client, ollama["url"], name)
                if _ollama_fits(capability, caps):
                    model, resident = name, False
                    by_routes = _promoted(name, ranked_tags, base_tags)
                    break

        if model is None:
            if ollama["resident"]:
                reasons.append(f"Ollama has resident models but none support '{capability}'")
            elif self._may_load(capability):
                reasons.append(f"Ollama has no installed model that supports '{capability}'")
            else:
                reasons.append("Ollama has no resident models (only_resident=True)")
            return None

        reason = (
            f"{capability} -> Ollama at {_host(ollama['url'])} ({model}), "
            f"shared loopback server; {'resident' if resident else 'would load'}"
        )
        if preferred and preferred != model:
            reason += f"; preferred '{preferred}' not available"
        details = {"source": "loopback", "resident": resident,
                   **({"size_mb": _tag_size_mb(ollama.get("tags"), model)} if not resident else {})}
        if by_routes and ctx is not None:
            reason += ctx.note()
            details["routes"] = ctx.details()
        return Resolution(
            capability=capability,
            provider="ollama",
            url=ollama["url"],
            model=model,
            api="ollama",
            state="resolved",
            reason=reason,
            details=details,
        )

    async def _loopback_openai_compat(
        self, reasons: list[str], ctx: Optional[_RouteCtx] = None
    ) -> Optional[Resolution]:
        compat = await self._probe_openai_compat()
        if not compat:
            reasons.append("no OpenAI-compatible server found on 1234")
            return None
        ranked, base = self._rank(compat["models"], self.config.capability("llm").model, ctx)
        model = ranked[0]
        reason = f"llm -> OpenAI-compatible server at {_host(compat['url'])} ({model}), shared loopback server"
        details: dict[str, Any] = {"source": "loopback"}
        if ctx is not None and _promoted(model, ranked, base):
            reason += ctx.note()
            details["routes"] = ctx.details()
        return Resolution(
            capability="llm",
            provider="openai_compat",
            url=f"{compat['url']}/v1/chat/completions",
            model=model,
            api="openai",
            state="resolved",
            reason=reason,
            details=details,
        )

    async def _loopback_comfy(self, capability: str, reasons: list[str]) -> Optional[Resolution]:
        comfy_url = (self.config.comfy_url or "http://127.0.0.1:8188").rstrip("/")
        comfy = await self._probe_comfy(comfy_url)
        if not comfy:
            reasons.append(f"ComfyUI not reachable at {_host(comfy_url)}")
            return None
        gpus = await self._gpus()
        free_mb = max((g.free_mb for g in gpus), default=None)
        reason = f"{capability} -> ComfyUI at {_host(comfy['url'])}, shared loopback server"
        if free_mb is not None:
            reason += f"; {free_mb} MB VRAM free" + (" (best GPU)" if len(gpus) > 1 else "")
        return Resolution(
            capability=capability,
            provider="comfyui",
            url=comfy["url"],
            model=None,
            api=None,
            state="resolved",
            reason=reason,
            details={
                "source": "loopback",
                "checkpoints": comfy["checkpoints"],
                "vram_free_mb": free_mb,
                "gpus": [
                    {"index": g.index, "total_mb": g.total_mb, "free_mb": g.free_mb} for g in gpus
                ],
            },
        )

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------

    def _load_lease(self, res: Resolution) -> Any:
        """``async with`` guard for a call that makes the server LOAD a model
        (``details["resident"] is False``): hold a GPU lease from the hub for
        the load and the call. A resident model needs none (no-op)."""
        if not self.config.gpu_lease or res.details.get("resident") is not False:
            return _NoLease()
        cc = self.config.capability(res.capability)
        vram = cc.vram_mb or _load_vram_mb(res.details.get("size_mb")) or self.config.lease_vram_mb
        return _GuardedLease(res.capability, Lease(
            vram, purpose=f"{res.capability}: load {res.model} on {res.provider}", owner=self.config.app,
            timeout_s=self.config.lease_timeout_s, hub_url=self.config.hub_url, client=self._client,
        ))

    async def _post_json(
        self,
        provider: Optional[str],
        url: str,
        payload: Any,
        timeout: float,
        headers: Optional[dict[str, str]] = None,
    ) -> httpx.Response:
        """POST that only ever raises :class:`BackendError` for server trouble.

        ``status=0`` means no HTTP response at all (refused, timed out) —
        typically a stale explicit URL or a server that just went away.
        """
        try:
            resp = await self._client.post(url, json=payload, headers=headers, timeout=timeout)
        except httpx.HTTPError as exc:
            raise BackendError(provider, 0, f"{type(exc).__name__} at {_host(url)}: {exc}"[:200]) from exc
        if resp.status_code >= 400:
            err = BackendError(provider, resp.status_code, resp.text[:200])
            err.body = resp.text[:4000]
            raise err
        return resp

    async def _post_chat(
        self, provider: Optional[str], url: str, payload: dict[str, Any], level: Optional[str]
    ) -> httpx.Response:
        """A chat POST whose timeout grows with the reasoning asked for; a
        server that refuses the reasoning fields (400 naming them) gets the
        call once more without them, at its own default."""
        try:
            return await self._post_json(provider, url, payload, timeout=_reasoning.timeout_for(120.0, level))
        except BackendError as exc:
            body = getattr(exc, "body", None) or exc.body_excerpt
            if level is None or not _reasoning.looks_like_reasoning_error(exc.status, body):
                raise
            # A chat template that only knows some effort names says which;
            # ask again with the nearest one before giving the reasoning up.
            if _reasoning.remap_effort(payload, _reasoning.supported_efforts(body)):
                try:
                    return await self._post_json(provider, url, payload,
                                                 timeout=_reasoning.timeout_for(120.0, level))
                except BackendError as exc2:
                    body2 = getattr(exc2, "body", None) or exc2.body_excerpt
                    if not _reasoning.looks_like_reasoning_error(exc2.status, body2):
                        raise
            if not _reasoning.strip(payload):
                raise
            return await self._post_json(provider, url, payload, timeout=_reasoning.timeout_for(120.0, level))

    @staticmethod
    def _json(provider: Optional[str], resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except ValueError as exc:
            raise BackendError(provider, resp.status_code, f"response is not JSON: {resp.text[:160]}") from exc

    async def chat(
        self,
        messages: list[dict[str, Any]],
        images: Optional[list[bytes]] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        capability: str = "llm",
        response_format: Optional[dict[str, Any]] = None,
        effort: Optional[str] = None,
        task: Optional[str] = None,
    ) -> ChatResult:
        """One chat call to the resolved model.

        `task` (e.g. ``"code"``) asks for the model measured best for that
        task in ``~/.hoard/routes.json`` among the servers already resident
        (see :meth:`resolve`).

        `effort` (``off`` / ``low`` / ``medium`` / ``high`` / ``max``) is how
        hard the model reasons before answering; None or ``auto`` uses the
        capability's configured default (``HOARD_LLM_EFFORT``), and with none
        configured the server's own. Quality work (a graded answer, a report,
        a plan) should ask for ``max``; titles and tags ``off``. See
        :mod:`hoard_link.reasoning`."""
        level = _reasoning.normalize(effort)
        if level is None and (effort is None or str(effort).strip().lower() in ("", "auto")):
            level = _reasoning.normalize(self.config.capability(capability).effort)
        res = await self.resolve(capability, task)
        if not res.resolved:
            raise Unavailable(capability, res.details.get("reasons", [res.reason]))
        if not res.url:
            raise Unavailable(capability, [f"resolved provider '{res.provider}' has no URL to chat with"])

        start = self._now()
        async with self._load_lease(res):
            if res.api == "ollama":
                text, extra_reasoning, raw = await self._chat_ollama(
                    res, messages, images, max_tokens, temperature, response_format, level
                )
            else:
                text, extra_reasoning, raw = await self._chat_openai(
                    res, messages, images, max_tokens, temperature, response_format, level
                )
        elapsed_ms = (self._now() - start) * 1000.0

        clean_text, think = _strip_think(text)
        usage = _extract_usage(raw, res.api)
        return ChatResult(
            text=clean_text,
            model=res.model,
            provider=res.provider,
            usage=usage,
            elapsed_ms=elapsed_ms,
            reasoning=_join_reasoning(extra_reasoning, think),
            effort=level,
        )

    async def _chat_openai(
        self,
        res: Resolution,
        messages: list[dict[str, Any]],
        images: Optional[list[bytes]],
        max_tokens: Optional[int],
        temperature: Optional[float],
        response_format: Optional[dict[str, Any]],
        level: Optional[str] = None,
    ) -> tuple[str, Optional[str], dict]:
        msgs = [dict(m) for m in messages]
        if images:
            content: list[dict[str, Any]] = []
            last_user = None
            for m in reversed(msgs):
                if m.get("role") == "user":
                    last_user = m
                    break
            if last_user is None:
                last_user = {"role": "user", "content": ""}
                msgs.append(last_user)
            existing = last_user.get("content", "")
            if isinstance(existing, str) and existing:
                content.append({"type": "text", "text": existing})
            elif isinstance(existing, list):
                content.extend(existing)
            for img in images:
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{_image_mime(img)};base64,{_b64_image(img)}"},
                    }
                )
            last_user["content"] = content

        payload: dict[str, Any] = {"model": res.model, "messages": msgs, "stream": False}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature
        if response_format is not None:
            payload["response_format"] = response_format

        _reasoning.apply_openai(payload, level)

        endpoint = _openai_endpoint(res.url or "", "/chat/completions")
        resp = await self._post_chat(res.provider, endpoint, payload, level)
        data = self._json(res.provider, resp)
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise BackendError(res.provider, resp.status_code, f"unexpected chat response: {resp.text[:160]}") from exc
        if not isinstance(message, dict):
            raise BackendError(res.provider, resp.status_code, f"unexpected chat response: {resp.text[:160]}")
        text = message.get("content")
        # llama-server --reasoning-format and several OpenAI-compatible
        # servers return the thinking out of band rather than in <think>.
        extra = message.get("reasoning_content") or message.get("reasoning")
        return (text if isinstance(text, str) else ""), (extra if isinstance(extra, str) else None), data

    async def _chat_ollama(
        self,
        res: Resolution,
        messages: list[dict[str, Any]],
        images: Optional[list[bytes]],
        max_tokens: Optional[int],
        temperature: Optional[float],
        response_format: Optional[dict[str, Any]] = None,
        level: Optional[str] = None,
    ) -> tuple[str, Optional[str], dict]:
        msgs = [dict(m) for m in messages]
        if images:
            last_user = None
            for m in reversed(msgs):
                if m.get("role") == "user":
                    last_user = m
                    break
            if last_user is None:
                last_user = {"role": "user", "content": ""}
                msgs.append(last_user)
            last_user["images"] = [_b64_image(img) for img in images]

        options: dict[str, Any] = {}
        if temperature is not None:
            options["temperature"] = temperature
        if max_tokens is not None:
            options["num_predict"] = max_tokens

        payload: dict[str, Any] = {"model": res.model, "messages": msgs, "stream": False}
        if options:
            payload["options"] = options
        fmt = _ollama_format(response_format)
        if fmt is not None:
            payload["format"] = fmt

        _reasoning.apply_ollama(payload, level)

        endpoint = _ollama_endpoint(res.url or "", "/api/chat")
        resp = await self._post_chat(res.provider, endpoint, payload, level)
        data = self._json(res.provider, resp)
        message = data.get("message") if isinstance(data, dict) else None
        if not isinstance(message, dict):
            raise BackendError(res.provider, resp.status_code, f"unexpected chat response: {resp.text[:160]}")
        text = message.get("content")
        extra = message.get("thinking")
        return (text if isinstance(text, str) else ""), (extra if isinstance(extra, str) else None), data

    async def embed(self, texts: list[str]) -> list[list[float]]:
        res = await self.resolve("embeddings")
        if not res.resolved:
            raise Unavailable("embeddings", res.details.get("reasons", [res.reason]))
        if not res.url:
            raise Unavailable("embeddings", [f"resolved provider '{res.provider}' has no URL"])

        if res.api == "ollama":
            endpoint = _ollama_endpoint(res.url, "/api/embed")
            async with self._load_lease(res):
                resp = await self._post_json(
                    res.provider, endpoint, {"model": res.model, "input": texts}, timeout=60.0
                )
            data = self._json(res.provider, resp)
            if not isinstance(data, dict):
                raise BackendError(res.provider, resp.status_code, "unexpected embeddings response")
            vectors = data.get("embeddings")
            if vectors is None and "embedding" in data:
                vectors = [data["embedding"]]
            return vectors or []

        endpoint = _openai_endpoint(res.url, "/embeddings")
        async with self._load_lease(res):
            resp = await self._post_json(
                res.provider, endpoint, {"model": res.model, "input": texts}, timeout=60.0
            )
        data = self._json(res.provider, resp)
        try:
            items = sorted(data["data"], key=lambda item: item.get("index", 0))
            return [item["embedding"] for item in items]
        except (KeyError, TypeError, AttributeError) as exc:
            raise BackendError(res.provider, resp.status_code, f"unexpected embeddings response: {resp.text[:160]}") from exc

    async def tts(self, text: str, voice: Optional[str] = None) -> bytes:
        res = await self.resolve("tts")
        if not res.resolved:
            raise Unavailable("tts", res.details.get("reasons", [res.reason]))

        if res.provider and res.provider.startswith("faustus_"):
            headers = _faustus.auth_headers(self.config.faustus_token)
            payload: dict[str, Any] = {"text": text}
            if voice:
                payload["voice"] = voice
            resp = await self._post_json(
                res.provider, f"{res.url}/api/tts/synthesize", payload, timeout=30.0, headers=headers
            )
            content_type = resp.headers.get("content-type", "")
            if content_type.startswith("audio/") or content_type == "application/octet-stream":
                return resp.content
            data = self._json(res.provider, resp)
            audio_b64 = data.get("audio") if isinstance(data, dict) else None
            if audio_b64:
                return base64.b64decode(audio_b64)
            raise BackendError(res.provider, 200, "no audio field in TTS response")

        if res.details.get("command"):
            cc = self.config.capability("tts")
            return await self._tts_via_command(cc.command or [], text, voice)

        raise Unavailable("tts", [f"no TTS implementation for resolved provider '{res.provider}'"])

    @staticmethod
    async def _tts_via_command(command: list[str], text: str, voice: Optional[str]) -> bytes:
        """Run a local TTS command (e.g. Piper).

        Placeholders ``{text}``, ``{voice}`` and ``{out}`` are substituted
        literally (other braces are left alone). Without ``{text}`` the
        text is written to the command's stdin — which is how Piper reads
        it — so the process never waits on an inherited console. Without
        ``{out}`` the audio is read from stdout. Runs through
        :func:`subprocess.run` in a worker thread, so it works on any event
        loop (including a Windows SelectorEventLoop) and never opens a
        console window.
        """
        if not command:
            raise Unavailable("tts", ["configured TTS command is empty"])
        uses_out_file = any("{out}" in part for part in command)
        uses_text_arg = any("{text}" in part for part in command)
        out_path: Optional[Path] = None
        try:
            if uses_out_file:
                fd, name = tempfile.mkstemp(prefix="hoard-tts-", suffix=".wav")
                os.close(fd)
                out_path = Path(name)

            def fill(part: str) -> str:
                part = part.replace("{text}", text).replace("{voice}", voice or "")
                return part.replace("{out}", str(out_path)) if out_path else part

            argv = [fill(p) for p in command]
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0

            def run() -> subprocess.CompletedProcess:
                return subprocess.run(
                    argv,
                    input=None if uses_text_arg else text.encode("utf-8"),
                    stdin=subprocess.DEVNULL if uses_text_arg else None,
                    capture_output=True,
                    timeout=TTS_COMMAND_TIMEOUT_S,
                    creationflags=creationflags,
                )

            try:
                proc = await asyncio.to_thread(run)
            except FileNotFoundError as exc:
                raise BackendError("configured-command", 0, f"TTS command not found: {argv[0]}") from exc
            except subprocess.TimeoutExpired as exc:
                raise BackendError(
                    "configured-command", 0, f"TTS command timed out after {TTS_COMMAND_TIMEOUT_S:.0f}s"
                ) from exc
            except OSError as exc:
                raise BackendError("configured-command", 0, f"TTS command failed to start: {exc}"[:200]) from exc
            if proc.returncode != 0:
                raise BackendError(
                    "configured-command", proc.returncode or 1, proc.stderr[:200].decode("utf-8", "replace")
                )
            if out_path is not None:
                return out_path.read_bytes()
            return proc.stdout
        finally:
            if out_path is not None:
                out_path.unlink(missing_ok=True)

    async def comfy(self) -> Optional[ComfyClient]:
        candidate = self.config.comfy_url
        if not candidate:
            res = await self.resolve("image")
            if res.resolved and res.provider == "comfyui":
                candidate = res.url
        if not candidate:
            return None
        return ComfyClient(candidate, client=self._client)

    async def wait_idle(self, capability: str, max_wait_s: float = 30.0, task: Optional[str] = None) -> bool:
        """Wait until the chosen llama-server has no slot processing.

        Works whether the server came from loopback probing, the Faustus
        registry or explicit configuration (it is probed by the resolved
        URL's server root). Ollama exposes no busy signal: returns True.
        """
        res = await self.resolve(capability, task)
        if not res.resolved or res.provider != "llamacpp" or not res.url:
            return True

        base = _server_root(res.url)
        deadline = self._now() + max_wait_s
        while True:
            server = await _probes.probe_llamacpp_base(self._client, base)
            if server is None or not _probes.llamacpp_busy(server):
                return True
            remaining = deadline - self._now()
            if remaining <= 0:
                return False
            await self._sleep(min(2.0, remaining))

    async def status(self, task: Optional[str] = None) -> dict[str, Any]:
        """Every capability's Resolution, plus a ``routes`` entry with the
        measured-routes file in use (``file``, ``updated_at``, ``source``,
        ``tasks``, ``problem``)."""
        results = await asyncio.gather(*(self.resolve(cap, task) for cap in CAPABILITIES))
        out: dict[str, Any] = {cap: res.to_dict() for cap, res in zip(CAPABILITIES, results)}
        out["routes"] = self._routes().summary()
        return out
