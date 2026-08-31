"""Re-establish the ChromaDB-backed stores without restarting Faustus.

FAUSTUS addition. The failure this exists for: Docker Desktop gets closed (or
crashes), ChromaDB goes with it, and `VectorRAG` / `MemoryVectorStore` flip
unhealthy. Bringing the container back does not bring *them* back — the objects
are held by the chat processor, the memory provider and half the routes, so
until now the only cure was a full restart of the app.

`reconnect_vector_stores` resets the client singleton and re-runs each store's
initializer **on the existing object**, so every holder sees a healthy store
again with no re-wiring and no restart.

What it deliberately does not do: create a store that was never created. If
ChromaDB was down at startup the manager is `None`, nothing holds a reference,
and inventing one here would leave the rest of the app still pointing at
nothing. That case returns `"absent"` and the hint tells you to restart.
"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _reconnect_one(store: Any) -> str:
    """Re-init one vector store. Returns absent | healthy | unhealthy | error."""
    if store is None:
        return "absent"
    try:
        reconnect = getattr(store, "reconnect", None)
        if callable(reconnect):
            return "healthy" if reconnect() else "unhealthy"
        # Older/stub stores: fall back to the private initializer if present.
        for attr in ("_initialize_system", "_initialize"):
            fn = getattr(store, attr, None)
            if callable(fn):
                fn()
                return "healthy" if getattr(store, "healthy", False) else "unhealthy"
        return "unhealthy"
    except Exception as e:  # never let a broken store break the endpoint
        logger.warning("service_recovery: reconnect failed: %s", type(e).__name__)
        return "error"


def reconnect_vector_stores(rag_manager: Any = None,
                            memory_vector: Any = None) -> Dict[str, Optional[str]]:
    """Drop the cached Chroma client and re-init both stores in place.

    Returns {"chroma_client": ..., "rag": ..., "memory": ...} with one of
    absent | healthy | unhealthy | error per store, for display in the panel.
    """
    result: Dict[str, Optional[str]] = {}
    try:
        from src.chroma_client import reset_client
        reset_client()
        result["chroma_client"] = "reset"
    except Exception as e:
        logger.warning("service_recovery: client reset failed: %s", type(e).__name__)
        result["chroma_client"] = "error"

    # Clear the lazy RAG singleton's retry throttle so the next caller that
    # goes through get_rag_manager() retries immediately instead of waiting
    # out the 30s window.
    try:
        from src import rag_singleton
        rag_singleton._last_attempt = 0.0
        if rag_singleton.rag_instance is not None and rag_manager is None:
            rag_manager = rag_singleton.rag_instance
    except Exception:
        pass

    result["rag"] = _reconnect_one(rag_manager)
    result["memory"] = _reconnect_one(memory_vector)
    return result
