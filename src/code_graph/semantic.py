"""src/code_graph/semantic.py — semantic_query over the code graph.

`code_index.search` already fuses lexical scoring with `two_tier_search`'s
model-free hash-embedding lane; this module supplies the ONE thing that
fusion needs to become genuinely semantic — a real local embedder — reusing
`src.embeddings.FastEmbedClient`, the same fastembed wrapper
`src.tool_index.ToolIndex` builds its in-memory lane from
(`_build_fastembed_client` in `src.embedding_lanes`). No second embedding
stack: when fastembed is not installed or the model fails to load,
`_embedder()` returns None and `code_index.search` degrades to its lexical
lane exactly as it already does for every other caller that passes no
embedder — never an error, never a duplicate vector index.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.context_engine import code_index

from .query import _clip, _root, DEFAULT_OUTPUT_CHARS, _fmt_symbol_row

logger = logging.getLogger(__name__)

_embedder_cache: Dict[str, Any] = {}


def _embedder() -> Optional[Any]:
    if "client" in _embedder_cache:
        return _embedder_cache["client"]
    client = None
    try:
        from src.embeddings import FastEmbedClient
        client = FastEmbedClient()
        client.get_sentence_embedding_dimension()
    except Exception as exc:  # noqa: BLE001 - fastembed missing/broken: degrade, don't raise
        logger.info("code_graph.semantic: no local embedder (%s); falling back to lexical search", exc)
        client = None
    _embedder_cache["client"] = client
    return client


def semantic_query(text: str, *, workspace: str = "", project_id: str = "", limit: int = 12,
                    output_chars: int = DEFAULT_OUTPUT_CHARS) -> Dict[str, Any]:
    root = _root(workspace)
    code_index.refresh(root, project_id=project_id)
    embedder = _embedder()
    try:
        k = max(1, min(int(limit or 12), 100))
    except (TypeError, ValueError):
        k = 12
    hits = code_index.search(text, workspace=root, project_id=project_id, k=k, embedder=embedder)
    body = "\n".join(_fmt_symbol_row(h) for h in hits) or f"no matches for {text!r}"
    return {"output": _clip(body, output_chars), "exit_code": 0, "matches": hits,
            "semantic": embedder is not None, "root": root}
