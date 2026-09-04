"""
media_backends — the engines that make pictures, video and sound.

Phase 3 of the masterplan. The shape mirrors `execution_backends`: a backend
knows how to probe itself, refuse a job it cannot do, run one, and cancel it —
and it never quietly becomes more capable than it said it was.

ComfyUI is GPL-3.0, so it is integrated as a **separate service over its HTTP
API** and none of its code lives here. That is a licence decision before it is
an architecture one, but it turns out to be the right architecture too: a
render that takes twenty minutes has no business inside the web process.
"""

from .comfyui import (  # noqa: F401
    ComfyUIBackend, ComfyUIError, DEFAULT_BASE_URL,
)
from .pool import (  # noqa: F401
    Engine, choose, survey, urls,
)

__all__ = ["ComfyUIBackend", "ComfyUIError", "DEFAULT_BASE_URL",
           "Engine", "choose", "survey", "urls"]
