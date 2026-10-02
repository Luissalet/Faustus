"""Explicit configuration: an app's ``backend.json`` plus environment overrides.

This is resolution-order source #1 in the README: whatever is set here wins
outright, no probing needed (a real HTTP call will surface a
:class:`~hoard_link.errors.BackendError` on its own if the address is
stale).

``backend.json`` schema (every key optional)::

    {
      "only_resident": true,
      "faustus": {"url": "http://127.0.0.1:7000", "token": "ody_..."},
      "comfy": {"url": "http://127.0.0.1:8188"},
      "gpu_lease": {"enabled": true, "hub_url": "http://127.0.0.1:8810", "timeout_s": 300, "vram_mb": 8192},
      "routes": {"enabled": true, "file": "~/.hoard/routes.json"},
      "capabilities": {
        "llm": {
          "url": "http://127.0.0.1:8081/v1/chat/completions",
          "model": "qwen3.8-27b-q8-llamacpp",
          "api": "openai",
          "provider": "llamacpp",
          "allow_load": false,
          "vram_mb": 20000
        },
        "tts": {"command": ["piper", "--model", "es_ES.onnx", "--output_file", "{out}"]}
      }
    }

Environment overrides (highest priority, applied on top of the file):

- ``HOARD_<CAP>_URL`` / ``HOARD_<CAP>_MODEL`` for each capability, e.g.
  ``HOARD_LLM_URL``, ``HOARD_VISION_MODEL``.
- ``HOARD_<CAP>_EFFORT`` (``off`` / ``low`` / ``medium`` / ``high`` / ``max``):
  how hard :meth:`Link.chat` asks the model to reason when the caller does not
  say (``capabilities.<cap>.effort`` in the file). Unset leaves the server's
  own default.
- ``HOARD_FAUSTUS_URL``, ``HOARD_FAUSTUS_TOKEN``.
- ``HOARD_COMFY_URL``.
- ``HOARD_GPU_LEASE=0`` turns the GPU lease off (``HOARD_HUB_URL`` points
  the lease client at a hub on another port).
- ``HOARD_ROUTES=0`` ignores the measured routes (``routes.enabled: false``
  in the file); ``HOARD_ROUTES_FILE`` names the routes file (``routes.file``),
  by default ``~/.hoard/routes.json``. See :mod:`hoard_link.routes`.

``gpu_lease`` only matters when a call is about to make a server *load* a
model (``allow_load`` / ``only_resident: false``): :class:`~hoard_link.link.Link`
then asks the hub for ``vram_mb`` (the capability's own ``vram_mb``, else an
estimate from the model's size, else ``gpu_lease.vram_mb``) before the call
and releases it afterwards. Calls to an already-resident model take no lease.

A ``model`` without a ``url`` (in the file or as ``HOARD_<CAP>_MODEL``)
does not pin a server: it is a *preference* used wherever resolution has a
choice (Ollama resident models, the Faustus registry's model list, an
OpenAI-compatible server's model list). Empty environment variables are
treated as unset.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from .types import CAPABILITIES


def _section(raw: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    return value if isinstance(value, dict) else {}


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _clean_url(url: str) -> str:
    return str(url).strip().rstrip("/")


@dataclass(frozen=True)
class CapabilityConfig:
    url: Optional[str] = None
    model: Optional[str] = None
    api: Optional[str] = None
    provider: Optional[str] = None
    allow_load: bool = False
    command: Optional[list[str]] = None
    vram_mb: Optional[int] = None      # VRAM a load of this capability's model needs (GPU lease)
    effort: Optional[str] = None       # default reasoning level for chat (see hoard_link.reasoning)

    @property
    def explicit(self) -> bool:
        """True when this capability was pinned by config/env (not probed)."""
        return bool(self.url or self.command)


@dataclass(frozen=True)
class LinkConfig:
    app: str = "app"
    only_resident: bool = True
    faustus_urls: tuple[str, ...] = ("http://127.0.0.1:7000", "http://127.0.0.1:7001")
    faustus_token: Optional[str] = None
    comfy_url: Optional[str] = None
    capabilities: dict[str, CapabilityConfig] = field(default_factory=dict)
    gpu_lease: bool = True
    hub_url: Optional[str] = None
    lease_timeout_s: float = 300.0
    lease_vram_mb: int = 8192
    use_routes: bool = True             # order candidates by the measured routes (hoard_link.routes)
    routes_file: Optional[str] = None   # None: HOARD_ROUTES_FILE, else ~/.hoard/routes.json

    def capability(self, capability: str) -> CapabilityConfig:
        return self.capabilities.get(capability, CapabilityConfig())

    @classmethod
    def load(
        cls,
        path: Optional[str | Path] = None,
        env: Optional[Mapping[str, str]] = None,
        app: str = "app",
    ) -> "LinkConfig":
        env = env if env is not None else os.environ
        raw: dict[str, Any] = {}
        if path is not None:
            p = Path(path)
            if p.is_file():
                # utf-8-sig: Windows Notepad saves UTF-8 with a BOM, which
                # plain json.loads rejects.
                try:
                    raw = json.loads(p.read_text(encoding="utf-8-sig"))
                except ValueError as exc:
                    raise ValueError(f"{p}: not valid JSON ({exc})") from exc
                if not isinstance(raw, dict):
                    raise ValueError(f"{p}: expected a JSON object at the top level")

        def env_value(key: str) -> Optional[str]:
            # An empty variable (``set HOARD_LLM_URL=``) means "not set".
            value = env.get(key)
            return value.strip() if value and value.strip() else None

        only_resident = bool(raw.get("only_resident", True))

        faustus_raw = _section(raw, "faustus")
        faustus_urls: tuple[str, ...]
        if faustus_raw.get("url"):
            faustus_urls = (_clean_url(faustus_raw["url"]),)
        else:
            faustus_urls = ("http://127.0.0.1:7000", "http://127.0.0.1:7001")
        faustus_token = faustus_raw.get("token") or None

        comfy_url = _section(raw, "comfy").get("url")

        caps: dict[str, CapabilityConfig] = {}
        raw_caps = _section(raw, "capabilities")
        for cap in CAPABILITIES:
            c = _section(raw_caps, cap)
            command = c.get("command")
            if command is not None and (
                not isinstance(command, list) or not all(isinstance(x, str) for x in command)
            ):
                raise ValueError(
                    f"capabilities.{cap}.command must be a list of strings, e.g. "
                    '["piper", "--model", "voice.onnx", "--output_file", "{out}"]'
                )
            caps[cap] = CapabilityConfig(
                url=c.get("url") or None,
                model=c.get("model") or None,
                api=c.get("api") or None,
                provider=c.get("provider") or None,
                allow_load=bool(c.get("allow_load", False)),
                command=command or None,
                vram_mb=_int_or_none(c.get("vram_mb")),
                effort=(str(c["effort"]).strip() or None) if c.get("effort") else None,
            )

        # --- environment overrides (highest priority) ---
        if env_value("HOARD_FAUSTUS_URL"):
            faustus_urls = (_clean_url(env_value("HOARD_FAUSTUS_URL")),)
        if env_value("HOARD_FAUSTUS_TOKEN"):
            faustus_token = env_value("HOARD_FAUSTUS_TOKEN")
        if env_value("HOARD_COMFY_URL"):
            comfy_url = env_value("HOARD_COMFY_URL")
        if comfy_url:
            comfy_url = _clean_url(comfy_url)

        for cap in CAPABILITIES:
            env_url = env_value(f"HOARD_{cap.upper()}_URL")
            env_model = env_value(f"HOARD_{cap.upper()}_MODEL")
            if env_url or env_model:
                current = caps[cap]
                caps[cap] = CapabilityConfig(
                    url=env_url or current.url,
                    model=env_model or current.model,
                    api=current.api,
                    provider=current.provider,
                    allow_load=current.allow_load,
                    command=current.command,
                    vram_mb=current.vram_mb,
                    effort=current.effort,
                )
            env_effort = env_value(f"HOARD_{cap.upper()}_EFFORT")
            if env_effort:
                current = caps[cap]
                caps[cap] = CapabilityConfig(
                    url=current.url, model=current.model, api=current.api,
                    provider=current.provider, allow_load=current.allow_load,
                    command=current.command, vram_mb=current.vram_mb, effort=env_effort,
                )

        lease_raw = _section(raw, "gpu_lease")
        gpu_lease = bool(lease_raw.get("enabled", True))
        if (env_value("HOARD_GPU_LEASE") or "").lower() in ("0", "false", "no", "off"):
            gpu_lease = False
        hub_url = lease_raw.get("hub_url") or None
        try:
            lease_timeout_s = float(lease_raw.get("timeout_s", 300.0))
        except (TypeError, ValueError):
            lease_timeout_s = 300.0
        lease_vram_mb = _int_or_none(lease_raw.get("vram_mb")) or 8192

        routes_raw = _section(raw, "routes")
        use_routes = bool(routes_raw.get("enabled", True))
        if (env_value("HOARD_ROUTES") or "").lower() in ("0", "false", "no", "off"):
            use_routes = False
        routes_file = env_value("HOARD_ROUTES_FILE") or (str(routes_raw.get("file") or "").strip() or None)

        return cls(
            app=app,
            only_resident=only_resident,
            faustus_urls=faustus_urls,
            faustus_token=faustus_token,
            comfy_url=comfy_url,
            capabilities=caps,
            gpu_lease=gpu_lease,
            hub_url=_clean_url(hub_url) if hub_url else None,
            lease_timeout_s=lease_timeout_s,
            lease_vram_mb=lease_vram_mb,
            use_routes=use_routes,
            routes_file=routes_file,
        )
