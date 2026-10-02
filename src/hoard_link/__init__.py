"""Hoard Link: the shared library of the Hoard family.

It started as the shared model backend (``Link``): for a capability
(``llm``, ``vision``, ``embeddings``, ``tts``, ``stt``, ``image``,
``video``, ``music``) it answers which server and model to use right now,
favouring servers other local apps (and Faustus) already have resident.

Since 0.4 it carries what an app needs to belong to the *family*
(``hoard_link.family``, the ``fam_*`` clients of the hub's facets), and
since 0.8 the code the apps used to copy from each other (the *commons*):

* ``hoard_link.web``   — polite fetching, SSRF guard, robots, block
  detection, HTML to text/markdown, page metadata and JSON-LD, feeds, page
  watching, URL canonical forms, the shared Playwright rung;
* ``hoard_link.media`` — ffmpeg/ffprobe/yt-dlp discovery, the ffmpeg
  runner and probe, subtitles, local speech to text;
* ``hoard_link.docs``  — FTS queries and ``fold``, chunking, file sniffing,
  page ranges, vectors, citations, images, light document readers;
* money, dates, identifiers, tracking numbers, merchants, ICS, business
  days; and the app plumbing (atomic writes, tokens, ids, subprocesses,
  SQLite, the request guard, ports, lanes).

Importing the package is cheap and needs only the standard library: the
two names that need ``httpx`` (``Link`` and ``ComfyClient``)
are loaded the first time they are used (PEP 562), so a stdlib-only app can
``from .hoard_link import fam_notify, money`` without ``httpx`` installed.

See ``README.md`` and ``docs/COMMONS.md``.
"""

from __future__ import annotations

import importlib
from typing import Any

__version__ = "0.8.1"

from .config import CapabilityConfig, LinkConfig
from .errors import BackendError, HoardLinkError, Unavailable
from .gpu import GpuMemory, gpu_free_mb
from .lease import Lease, LeaseError, LeaseTimeout, lease
from .types import CAPABILITIES, ChatResult, OutputFile, Resolution, Usage

#: names that need ``httpx``: loaded on first use (PEP 562)
_LAZY: dict[str, tuple[str, str]] = {
    "Link": ("link", "Link"),
    "ComfyClient": ("_comfy", "ComfyClient"),
}

__all__ = [
    "__version__", "Link", "LinkConfig", "CapabilityConfig", "ComfyClient", "Resolution", "ChatResult", "Usage",
    "OutputFile", "Unavailable", "BackendError", "HoardLinkError", "gpu_free_mb", "GpuMemory", "lease", "Lease",
    "LeaseError", "LeaseTimeout", "CAPABILITIES", "family",
]


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(f"{__name__}.{target[0]}"), target[1])
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))


from . import family  # noqa: E402  (standard library only)
