"""Text embeddings for the family, from inside an app: one model on the machine, asked through the hub.

The owner is Borges's Hoard (app id ``borges``): ``embed_texts`` turns texts into vectors with the model Borges keeps loaded, ``embed_status``
says which model that is. An app that used to carry its own embedding call (Vitruvius's ``dense.py``, Hypatia's ``llm.embed``, Babel) calls
this instead and indexes with :mod:`hoard_link.docs.vecmath` (``pack_vec``, ``topk``).

**Vector spaces.** Vectors from two models cannot be compared. Every answer says which model made them (``model``, ``dim``): store both
with the vectors and re-embed when they change. The fallback (a :class:`hoard_link.Link` embedding backend, used when Borges cannot be
reached) is a different model, so its answer carries its own ``model`` and ``via: "local"`` and the app can tell.

Standard library only (``httpx`` is imported only if the fallback runs). Nothing here raises: failures are ``{"ok": False, "error": "...",
"via": "borges", "kind": "..."}`` (``kind``: ``hub_down`` / ``app_down`` / ``app_missing`` / ``tool_missing`` / ``timeout`` / ``auth`` /
``tool_error`` / ``client_error``; a hub that does not answer says ``"hub unreachable"``)::

    from hoard_link import family, fam_embed
    from hoard_link.docs import vecmath

    family.configure("vitruvius", DATA_DIR)
    got = fam_embed.embed_texts(chunks, kind="document")           # batches of 64
    if got["ok"]:
        blobs = [vecmath.pack_vec(v) for v in got["vectors"]]
        save(model=got["model"], dim=got["dim"], blobs=blobs)
    q = fam_embed.embed_query("garantía de la lavadora")
    hits = vecmath.topk(matrix, q["vector"], 10, normalized=True)
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from . import _famsvc as _s
from .docs import vecmath

__all__ = ["status", "embed_texts", "embed_query", "available", "forget_availability"]

BORGES = "borges"


def available(timeout: float = 1.0) -> bool:
    """True when the hub is up and Borges's Hoard is running. Cached for 30 seconds."""
    return _s.available("embed", timeout)


def forget_availability() -> None:
    _s.forget_availability()


@_s.never_raises(BORGES)
def status(timeout_s: float = 30.0) -> dict[str, Any]:
    """Borges's embedder (``embed_status``): ``{ok, backend, model, dim, state, ready, via}``. ``ready`` False means it is still loading
    or downloading the model; :func:`embed_texts` then waits for it on the owner's side."""
    res = _s.call_tool(BORGES, "embed_status", {}, timeout_s=timeout_s)
    if not res["ok"]:
        return _s.public_error(res, BORGES)
    data = dict(res["data"]) if isinstance(res["data"], Mapping) else {}
    data.setdefault("ready", str(data.get("state") or "") == "ready")
    return {"ok": True, **data, "via": BORGES}


def _valid_vectors(vectors: Any, count: int) -> Optional[list[list[float]]]:
    if not isinstance(vectors, list) or len(vectors) != count:
        return None
    out: list[list[float]] = []
    dim = None
    for v in vectors:
        if not isinstance(v, (list, tuple)) or not v:
            return None
        if dim is None:
            dim = len(v)
        if len(v) != dim:
            return None
        try:
            out.append([float(x) for x in v])
        except (TypeError, ValueError):
            return None
    return out


def _local_embed(texts: list[str], normalize: bool, batch: int, link: Any) -> dict[str, Any]:
    link = link if link is not None else _s.default_link()
    vectors: list[list[float]] = []
    for i in range(0, len(texts), batch):
        part = _s.run_link(link, "embed", texts[i:i + batch])
        ok = _valid_vectors(list(part or []), len(texts[i:i + batch]))
        if ok is None:
            raise RuntimeError("the local embedding backend returned a wrong number of vectors")
        vectors.extend(ok)
    model = ""
    try:
        res = _s.run_link(link, "resolve", "embeddings")
        model = str(getattr(res, "model", "") or "")
    except Exception:  # noqa: BLE001 - the model name is informative
        model = ""
    if normalize:
        vectors = [vecmath.normalize(v) for v in vectors]
    return {"ok": True, "model": model, "dim": len(vectors[0]) if vectors else 0, "vectors": vectors, "via": "local"}


def embed_texts(texts: Sequence[str], *, kind: str = "document", normalize: bool = True, batch: int = 64, timeout_s: float = 120.0,
                local_fallback: bool = True, link: Any = None) -> dict[str, Any]:
    """Vectors for ``texts``: Borges's embedder through the hub, in batches of ``batch`` (``timeout_s`` per batch). ``kind`` is
    ``"document"`` or ``"query"`` (models with asymmetric prefixes embed them differently); ``normalize`` returns unit-length vectors.

    Returns ``{ok, model, dim, vectors: [[float]...], via}``, one vector per text in order; ``model`` and ``dim`` identify the vector space.
    When Borges cannot be reached and ``local_fallback`` is set, ``link.embed`` (a :class:`hoard_link.Link`, default one for this app; needs
    ``httpx``) answers instead with ``via: "local"`` and its own ``model`` (``kind`` is ignored there: no prefixes). A failure after the
    first batch is an error, never a mix of two models."""
    try:
        items = [str(t) for t in texts]
    except TypeError:
        return {"ok": False, "error": "texts must be a list of strings", "via": BORGES, "kind": "client_error"}
    mode = str(kind or "document").strip().lower()
    if mode not in ("document", "query"):
        return {"ok": False, "error": f"kind must be document or query, not {kind!r}", "via": BORGES, "kind": "client_error"}
    if not items:
        return {"ok": True, "model": "", "dim": 0, "vectors": [], "via": BORGES}
    size = max(1, min(int(batch or 64), 512))
    try:
        vectors: list[list[float]] = []
        model, dim = "", 0
        for i in range(0, len(items), size):
            part = items[i:i + size]
            res = _s.call_tool(BORGES, "embed_texts", {"texts": part, "kind": mode, "normalize": bool(normalize)}, timeout_s=float(timeout_s))
            if not res["ok"]:
                if i == 0 and local_fallback and _s.is_unavailable(res):
                    try:
                        return _local_embed(items, bool(normalize), size, link)
                    except Exception as exc:  # noqa: BLE001
                        return {"ok": False, "error": f"{res['error']}; local fallback failed: {type(exc).__name__}: {exc}"[:400], "via": "local",
                                "kind": res["kind"], "hub_error": res["error"]}
                out = _s.public_error(res, BORGES)
                if i:
                    out["error"] = f"{out['error']} (after {i} of {len(items)} texts)"
                return out
            data = res["data"] if isinstance(res["data"], Mapping) else {}
            got = _valid_vectors(data.get("vectors"), len(part))
            if got is None:
                return {"ok": False, "error": "Borges returned a wrong number or shape of vectors", "via": BORGES, "kind": "tool_error"}
            this_model = str(data.get("model") or "")
            if i and (this_model != model or len(got[0]) != dim):
                return {"ok": False, "error": f"the embedding model changed during the call ({model!r} -> {this_model!r}); try again", "via": BORGES,
                        "kind": "tool_error"}
            model, dim = this_model, len(got[0])
            if normalize and not data.get("normalized", True):
                got = [vecmath.normalize(v) for v in got]
            vectors.extend(got)
        return {"ok": True, "model": model, "dim": dim, "vectors": vectors, "via": BORGES}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300], "via": BORGES, "kind": "client_error"}


def embed_query(text: str, *, normalize: bool = True, timeout_s: float = 60.0, local_fallback: bool = True, link: Any = None) -> dict[str, Any]:
    """The vector of one search query (``kind="query"``): ``{ok, model, dim, vector, via}``."""
    res = embed_texts([str(text or "")], kind="query", normalize=normalize, timeout_s=timeout_s, local_fallback=local_fallback, link=link)
    if not res.get("ok"):
        return res
    return {"ok": True, "model": res["model"], "dim": res["dim"], "vector": res["vectors"][0], "via": res["via"]}
