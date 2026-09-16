"""src/creator/render/ — WP14: deterministic FFmpeg renderer.

Compiles a ``timeline`` :class:`~src.creator.documents.CreatorDocument`
(WP02/WP12/WP13 — rational clocks, tracks/clips, EDL) into a reproducible
FFmpeg composition: same document revision + same output profile + same
resolved inputs + same ``ffmpeg`` build ALWAYS produces the same
``filter_complex`` string and the same cache key (``cache.py``).

Sub-modules, one job each:

* :mod:`.graph` — pure compiler, timeline content -> :class:`RenderGraph`
  (ordered inputs, a deterministic ``filter_complex``, output maps). No
  filesystem/process access.
* :mod:`.profiles` — the fixed catalogue of output profiles (YouTube,
  Shorts/Reels/TikTok, Instagram, LinkedIn, Cinema) a render targets.
* :mod:`.cache` — the render cache (``DATA_DIR/creator/render_cache.db``):
  a deterministic key -> the occurrence id of a prior identical render.
* :mod:`.service` — ``plan()``/``render()``: wires the graph compiler, the
  cache, the WP10 ffmpeg adapter (``src/creator/adapters/ffmpeg.py``, its
  additive ``graph`` task) and ``src/creator/production_runs.py`` (the one
  ``MediaRun`` projection every non-ComfyUI adapter output goes through).
"""
from __future__ import annotations
