"""
tool_index_memory.py

In-process vector lane for the tool index, used when ChromaDB is not
reachable (Docker Desktop closed is a normal state on a desktop PC). It needs
only the embedder (fastembed) and numpy: ~150 tool descriptions are scored by
brute-force cosine similarity, which is faster than a network round-trip to
Chroma anyway.

Two pieces:

* ``MemoryCollection`` — the subset of the ``chromadb`` Collection API that
  ``src.tool_index.ToolIndex`` uses (upsert / get / delete / query / count),
  returning the same dict shapes so ``retrieve()`` and ``index_mcp_tools()``
  consume it unchanged.
* ``EmbeddingCache`` + ``CachingEmbedder`` — the vectors of the documents in
  the collection are persisted to ``DATA_DIR/tool_index_cache.json`` keyed by
  ``sha256(model + text)``. On the next start every unchanged description is
  a cache hit, so the index is rebuilt without embedding anything; only new or
  edited descriptions (and MCP tools that changed) go through the model.
  Query vectors are never persisted: the file mirrors the collection.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np

from src.constants import DATA_DIR
from src.embedding_lanes import (
    LANE_FASTEMBED,
    EmbeddingLane,
    _fingerprint,
    _metadata,
    collection_name,
)

logger = logging.getLogger(__name__)

CACHE_VERSION = 1
DEFAULT_CACHE_PATH = os.path.join(DATA_DIR, "tool_index_cache.json")

# Persisted vectors are rounded to this many decimals. float32 carries ~7
# significant digits, so nothing is lost for unit vectors and the file stays
# a few hundred KB for the whole tool catalogue.
_ROUND_DECIMALS = 6


def embedding_cache_key(model: str, text: str) -> str:
    raw = f"{model}\n{text}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def _as_matrix(vectors: Any) -> np.ndarray:
    arr = np.asarray(vectors, dtype=np.float32)
    if arr.ndim == 1:
        arr = arr.reshape(1, -1)
    return arr


def _normalise(arr: np.ndarray) -> np.ndarray:
    if arr.size == 0:
        return arr
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return arr / norms


# ── persisted embedding cache ───────────────────────────────────────────────

class EmbeddingCache:
    """``(model, text) → vector`` map with a JSON file behind it.

    In memory it is a plain dict; :meth:`save` writes it atomically. A file
    written for a different model or cache version is ignored on load.
    """

    def __init__(self, path: Optional[str], model: str):
        self.path = path
        self.model = model or ""
        self.dimension: Optional[int] = None
        self._vectors: Dict[str, np.ndarray] = {}
        self._lock = threading.RLock()
        self._load()

    def __len__(self) -> int:
        return len(self._vectors)

    def _load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                payload = json.load(fh)
        except Exception as e:
            logger.info("tool index cache unreadable (%s); rebuilding it", e)
            return
        if not isinstance(payload, dict) or payload.get("version") != CACHE_VERSION:
            return
        if payload.get("model") != self.model:
            logger.info(
                "tool index cache was built with model %r, current is %r; re-embedding",
                payload.get("model"), self.model,
            )
            return
        # Valid JSON of the wrong shape (entries not a dict, a dimension that
        # is not a number, a vector that is a dict) used to raise out of here
        # and take the whole memory lane — hence the tool index — down. Any
        # such file is an empty cache: rebuild, and the next save rewrites it.
        try:
            entries = payload.get("entries") or {}
            if not isinstance(entries, dict):
                raise TypeError(f"entries is {type(entries).__name__}, not an object")
            dim_raw = payload.get("dimension")
            dim: Optional[int] = None
            if dim_raw is not None and not isinstance(dim_raw, bool):
                dim = int(dim_raw)
                if dim <= 0:
                    dim = None
            loaded: Dict[str, np.ndarray] = {}
            for key, vec in entries.items():
                if not isinstance(vec, (list, tuple)):
                    continue
                try:
                    arr = np.asarray(vec, dtype=np.float32)
                except Exception:
                    continue
                if arr.ndim != 1 or arr.size == 0 or (dim and arr.size != dim):
                    continue
                loaded[str(key)] = arr
        except Exception as e:
            logger.info("tool index cache has an unexpected shape (%s); rebuilding it", e)
            return
        with self._lock:
            self._vectors = loaded
            # The file's dimension is only trusted when it describes vectors
            # actually loaded; an empty cache takes its dimension from the
            # first vector remembered (a stale number would make every new
            # vector look mismatched on the next load).
            self.dimension = int(next(iter(loaded.values())).size) if loaded else None

    def lookup(self, text: str) -> Optional[np.ndarray]:
        with self._lock:
            return self._vectors.get(embedding_cache_key(self.model, text))

    def remember(self, text: str, vector: Any) -> None:
        arr = np.asarray(vector, dtype=np.float32).reshape(-1)
        with self._lock:
            self._vectors[embedding_cache_key(self.model, text)] = arr
            if self.dimension is None:
                self.dimension = int(arr.size)

    def forget(self, text: str) -> None:
        with self._lock:
            self._vectors.pop(embedding_cache_key(self.model, text), None)

    def save(self, keys: Optional[Iterable[str]] = None) -> bool:
        """Write the cache file.

        With ``keys`` only those entries are written (the collection passes
        the keys of the documents it currently holds, so the file mirrors the
        collection); the in-memory map keeps everything it has seen.
        """
        if not self.path:
            return False
        with self._lock:
            if keys is None:
                selected = dict(self._vectors)
            else:
                selected = {k: self._vectors[k] for k in keys if k in self._vectors}
            payload = {
                "version": CACHE_VERSION,
                "model": self.model,
                "dimension": self.dimension,
                "entries": {
                    key: [round(float(x), _ROUND_DECIMALS) for x in vec.tolist()]
                    for key, vec in selected.items()
                },
            }
        # A private temp file in the same directory (same filesystem, so the
        # rename is atomic): a fixed `<path>.tmp` shared by two writers (the
        # warmup thread and a request, the app and a CLI) let one rename the
        # other's half-written file into place.
        tmp: Optional[str] = None
        try:
            directory = os.path.dirname(self.path) or "."
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=os.path.basename(self.path) + ".", suffix=".tmp", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, separators=(",", ":"))
            os.replace(tmp, self.path)
            return True
        except Exception as e:
            logger.debug("tool index cache not saved (%s): %s", self.path, e)
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            return False


class CachingEmbedder:
    """Embedding client wrapper: cache hits skip the model entirely.

    Has the same surface ``EmbeddingLane`` expects from a client
    (``encode``, ``get_sentence_embedding_dimension``, ``model``, ``url``).
    Nothing is persisted here — the collection persists what it stores, so
    ad-hoc query texts never bloat the file.
    """

    def __init__(self, client: Any, cache: EmbeddingCache):
        self._client = client
        self._cache = cache
        self.model = getattr(client, "model", "") or cache.model
        self.url = getattr(client, "url", "local://fastembed")
        self._lock = threading.RLock()

    @property
    def client(self) -> Any:
        return self._client

    @property
    def cache(self) -> EmbeddingCache:
        return self._cache

    def get_sentence_embedding_dimension(self) -> int:
        if self._cache.dimension:
            return int(self._cache.dimension)
        return int(self._client.get_sentence_embedding_dimension())

    def encode(self, texts: Sequence[str], normalize_embeddings: bool = True) -> np.ndarray:
        texts = list(texts)
        if not texts:
            return np.array([], dtype=np.float32)
        with self._lock:
            vectors: List[Optional[np.ndarray]] = [self._cache.lookup(t) for t in texts]
            missing = [i for i, v in enumerate(vectors) if v is None]
            if missing:
                fresh = _as_matrix(self._client.encode([texts[i] for i in missing], normalize_embeddings=True))
                for row, i in enumerate(missing):
                    vectors[i] = fresh[row]
            out = np.vstack([v.reshape(1, -1) for v in vectors]).astype(np.float32)
        if normalize_embeddings:
            out = _normalise(out)
        return out


# ── in-memory collection ────────────────────────────────────────────────────

def _match_where(meta: Dict[str, Any], where: Optional[Dict[str, Any]]) -> bool:
    """Chroma-style ``where``: plain equality plus $eq/$ne/$in/$nin/$and/$or."""
    if not where:
        return True
    for key, cond in where.items():
        if key == "$and":
            if not all(_match_where(meta, sub) for sub in cond):
                return False
        elif key == "$or":
            if not any(_match_where(meta, sub) for sub in cond):
                return False
        elif isinstance(cond, dict):
            value = meta.get(key)
            for op, expected in cond.items():
                if op == "$eq" and value != expected:
                    return False
                if op == "$ne" and value == expected:
                    return False
                if op == "$in" and value not in expected:
                    return False
                if op == "$nin" and value in expected:
                    return False
        elif meta.get(key) != cond:
            return False
    return True


class MemoryCollection:
    """The slice of the chromadb Collection API the tool index relies on.

    Vectors are stored L2-normalised so ``query`` is a dot product;
    distances are cosine distances (``1 - cos``), the same as a Chroma
    collection created with ``hnsw:space=cosine``.
    """

    def __init__(self, name: str, metadata: Optional[Dict[str, Any]] = None, cache: Optional[EmbeddingCache] = None):
        self.name = name
        self.metadata = metadata or {}
        self._cache = cache
        self._rows: Dict[str, Dict[str, Any]] = {}
        self._dim: Optional[int] = None
        self._matrix: Optional[np.ndarray] = None
        self._order: List[str] = []
        self._lock = threading.RLock()

    # -- helpers -------------------------------------------------------------

    def _check_dim(self, arr: np.ndarray) -> None:
        if arr.size == 0:
            return
        dim = int(arr.shape[1])
        if self._dim is None:
            self._dim = dim
        elif dim != self._dim:
            raise ValueError(
                f"Collection {self.name} expects embeddings of dimension {self._dim}, got {dim}"
            )

    def _invalidate(self) -> None:
        self._matrix = None
        self._order = []

    def _ensure_matrix(self) -> None:
        if self._matrix is None:
            self._order = list(self._rows.keys())
            if self._order:
                self._matrix = np.vstack([self._rows[i]["embedding"].reshape(1, -1) for i in self._order])
            else:
                self._matrix = np.zeros((0, self._dim or 0), dtype=np.float32)

    def _persist(self) -> None:
        """Remember every stored document's vector and write the file so it
        mirrors the collection (queries are never stored, so never saved)."""
        if self._cache is None:
            return
        keys = []
        for row in self._rows.values():
            doc = row.get("document")
            if doc is None:
                continue
            self._cache.remember(doc, row["embedding"])
            keys.append(embedding_cache_key(self._cache.model, doc))
        self._cache.save(keys)

    # -- chroma-shaped API ---------------------------------------------------

    def count(self) -> int:
        with self._lock:
            return len(self._rows)

    def add(self, ids, embeddings, documents=None, metadatas=None):
        self.upsert(ids=ids, embeddings=embeddings, documents=documents, metadatas=metadatas)

    def upsert(self, ids, embeddings, documents=None, metadatas=None):
        ids = list(ids)
        if not ids:
            return
        arr = _as_matrix(embeddings)
        if arr.shape[0] != len(ids):
            raise ValueError(f"{len(ids)} ids but {arr.shape[0]} embeddings")
        documents = list(documents) if documents is not None else [None] * len(ids)
        metadatas = list(metadatas) if metadatas is not None else [{}] * len(ids)
        with self._lock:
            self._check_dim(arr)
            arr = _normalise(arr)
            for i, row_id in enumerate(ids):
                self._rows[row_id] = {
                    "embedding": arr[i].astype(np.float32),
                    "document": documents[i] if i < len(documents) else None,
                    "metadata": dict(metadatas[i] or {}) if i < len(metadatas) else {},
                }
            self._invalidate()
            self._persist()

    def get(self, ids=None, where=None, include=None, limit=None, offset=None):
        with self._lock:
            if ids is not None:
                wanted = set(ids)
                selected = [(i, r) for i, r in self._rows.items() if i in wanted]
            else:
                selected = list(self._rows.items())
            if where:
                selected = [(i, r) for i, r in selected if _match_where(r["metadata"], where)]
            if offset:
                selected = selected[offset:]
            if limit is not None:
                selected = selected[:limit]
            want_embeddings = bool(include) and "embeddings" in include
            return {
                "ids": [i for i, _ in selected],
                "documents": [r["document"] for _, r in selected],
                "metadatas": [dict(r["metadata"]) for _, r in selected],
                "embeddings": [r["embedding"].tolist() for _, r in selected] if want_embeddings else None,
            }

    def delete(self, ids=None, where=None):
        with self._lock:
            if ids is not None:
                targets = [i for i in ids if i in self._rows]
            else:
                targets = list(self._rows.keys())
            if where:
                targets = [i for i in targets if _match_where(self._rows[i]["metadata"], where)]
            if not targets:
                return
            for row_id in targets:
                self._rows.pop(row_id, None)
            self._invalidate()
            self._persist()

    def query(self, query_embeddings, n_results=10, where=None, include=None):
        queries = _as_matrix(query_embeddings)
        with self._lock:
            self._check_dim(queries)
            queries = _normalise(queries)
            self._ensure_matrix()
            matrix = self._matrix
            order = self._order
            if where:
                keep = [k for k, row_id in enumerate(order) if _match_where(self._rows[row_id]["metadata"], where)]
                matrix = matrix[keep] if keep else np.zeros((0, self._dim or 0), dtype=np.float32)
                order = [order[k] for k in keep]
            want_embeddings = bool(include) and "embeddings" in include
            result: Dict[str, Any] = {"ids": [], "documents": [], "metadatas": [], "distances": []}
            if want_embeddings:
                result["embeddings"] = []
            n = max(0, int(n_results))
            for q in queries:
                if matrix.shape[0] == 0 or n == 0:
                    for key in result:
                        result[key].append([])
                    continue
                sims = matrix @ q
                k = min(n, sims.shape[0])
                top = np.argpartition(-sims, k - 1)[:k] if k < sims.shape[0] else np.arange(sims.shape[0])
                top = top[np.argsort(-sims[top], kind="stable")]
                hit_ids = [order[int(i)] for i in top]
                result["ids"].append(hit_ids)
                result["documents"].append([self._rows[i]["document"] for i in hit_ids])
                result["metadatas"].append([dict(self._rows[i]["metadata"]) for i in hit_ids])
                result["distances"].append([float(1.0 - sims[int(i)]) for i in top])
                if want_embeddings:
                    result["embeddings"].append([self._rows[i]["embedding"].tolist() for i in hit_ids])
            return result


# ── lane factory ────────────────────────────────────────────────────────────

def build_memory_lane(base_name: str, client: Any, cache_path: Optional[str] = None) -> EmbeddingLane:
    """An ``EmbeddingLane`` over ``MemoryCollection`` for ``base_name``.

    ``client`` is the raw embedder (a ``FastEmbedClient`` or anything with the
    same surface). It is wrapped in a ``CachingEmbedder`` bound to the JSON
    cache at ``cache_path`` (default: ``DATA_DIR/tool_index_cache.json``).
    """
    if cache_path is None:
        cache_path = DEFAULT_CACHE_PATH
    model = getattr(client, "model", "") or ""
    url = getattr(client, "url", "local://fastembed") or "local://fastembed"
    cache = EmbeddingCache(cache_path, model)
    embedder = CachingEmbedder(client, cache)
    dimension = int(embedder.get_sentence_embedding_dimension())
    fp = _fingerprint(LANE_FASTEMBED, url, model, dimension)
    name = collection_name(base_name, LANE_FASTEMBED)
    collection = MemoryCollection(name, metadata=_metadata(LANE_FASTEMBED, url, model, dimension, fp), cache=cache)
    return EmbeddingLane(
        name=LANE_FASTEMBED,
        client=embedder,
        collection=collection,
        collection_name=f"memory://{name}",
        model=model,
        url=url,
        dimension=dimension,
        fingerprint=fp,
    )
