"""The on-disk index: one folder per collection, vectors as float16, flat inner-product search.

::

    <root>/<owner key>/<collection>/
        meta.json      {"version", "embedder", "dim", "created", "updated"}
        vectors.npy    float16, (n, dim), rows L2-normalised, same order as strips.json
        strips.json    [{"id", "source", "page", "x", "y", "width", "height", "md5"}, ...]
        sources.json   {source: {"kind", "fingerprint", "title", "added", "updated", "strips": n, "pages": n}}
        strips/<id>.png

The root is ``DATA_DIR/visual_index``; the owner key is a hash of the owner name, so one person's collections are not
another's. Search is a flat product of the query against every vector (numpy, no extra dependency): at the sizes a
person builds by hand (thousands of strips) it takes milliseconds. A collection records the embedder that built it; adding
or searching with another one is refused (vectors of different models cannot be compared).

``sync_source`` is the one write path: it takes the strips of a source as they are now, keeps the vectors of strips whose
MD5 did not change, embeds only the new or changed ones and drops the ones that are gone.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

from .tiling import Strip

COLLECTION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")
DEFAULT_COLLECTION = "default"
FORMAT_VERSION = 1


class StoreError(ValueError):
    def __init__(self, message: str, code: str = "invalid_arguments"):
        super().__init__(message)
        self.code = code


def default_root() -> str:
    from src.constants import DATA_DIR

    return os.path.join(DATA_DIR, "visual_index")


def check_collection(name: str) -> str:
    name = str(name or DEFAULT_COLLECTION).strip()
    if not COLLECTION_RE.match(name):
        raise StoreError("a collection name is letters, digits, '_' and '-' (1 to 48 characters, starting with a letter or digit)",
                         "invalid_collection")
    return name


def owner_key(owner: Optional[str]) -> str:
    try:        # the tool path (ctx owner) and the route path (request owner) must land in the same folder
        from src.owner_identity import effective_storage_owner

        owner = effective_storage_owner(owner)
    except Exception:  # noqa: BLE001
        pass
    return hashlib.sha256(str(owner or "").encode("utf-8")).hexdigest()[:16]


def strip_id(collection: str, source: str, page: int, x: int, y: int) -> str:
    digest = hashlib.sha1(f"{source}\x00{page}\x00{x}\x00{y}".encode("utf-8")).hexdigest()[:12]
    return f"{collection}~{digest}"


def split_strip_id(sid: str) -> Tuple[str, str]:
    collection, sep, digest = str(sid or "").partition("~")
    if not sep or not COLLECTION_RE.match(collection) or not re.fullmatch(r"[0-9a-f]{12}", digest):
        raise StoreError(f"{sid!r} is not a strip id (they look like 'collection~0123456789ab')", "invalid_id")
    return collection, digest


_locks: Dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def _lock_for(path: str) -> threading.RLock:
    with _locks_guard:
        return _locks.setdefault(path, threading.RLock())


def _atomic_write(path: str, write: Callable[[Any], None], mode: str = "wb") -> None:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    try:
        with os.fdopen(fd, mode) as f:
            write(f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_json(path: str, data: Any) -> None:
    _atomic_write(path, lambda f: f.write(json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")))


def _read_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


class Collection:
    """One collection of one owner. Methods take the collection's lock; callers never hold files open."""

    def __init__(self, directory: str, name: str):
        self.dir = directory
        self.name = name
        self.lock = _lock_for(directory)

    # ---- files
    @property
    def _meta_path(self) -> str: return os.path.join(self.dir, "meta.json")
    @property
    def _vec_path(self) -> str: return os.path.join(self.dir, "vectors.npy")
    @property
    def _strips_path(self) -> str: return os.path.join(self.dir, "strips.json")
    @property
    def _sources_path(self) -> str: return os.path.join(self.dir, "sources.json")
    @property
    def strips_dir(self) -> str: return os.path.join(self.dir, "strips")

    def exists(self) -> bool:
        return os.path.isfile(self._meta_path)

    def meta(self) -> Dict[str, Any]:
        return _read_json(self._meta_path, {})

    def strips(self) -> List[Dict[str, Any]]:
        return _read_json(self._strips_path, [])

    def sources(self) -> Dict[str, Dict[str, Any]]:
        return _read_json(self._sources_path, {})

    def vectors(self) -> np.ndarray:
        try:
            return np.load(self._vec_path)
        except (OSError, ValueError):
            dim = int(self.meta().get("dim") or 0)
            return np.zeros((0, dim), dtype=np.float16)

    def image_path(self, sid: str) -> str:
        return os.path.join(self.strips_dir, sid.split("~", 1)[-1] + ".png")

    def _check_embedder(self, embedder_name: str, dim: int, *, create: bool) -> None:
        meta = self.meta()
        if not meta:
            if not create:
                return
            os.makedirs(self.strips_dir, exist_ok=True)
            now = time.time()
            _write_json(self._meta_path, {"version": FORMAT_VERSION, "embedder": embedder_name, "dim": dim,
                                          "created": now, "updated": now})
            return
        if meta.get("embedder") != embedder_name or int(meta.get("dim") or 0) != int(dim):
            raise StoreError(f"collection {self.name!r} was built with {meta.get('embedder')} ({meta.get('dim')} dimensions), "
                             f"not {embedder_name} ({dim}); use another collection name or remove this one first",
                             "embedder_mismatch")

    def _save(self, vectors: np.ndarray, strips: List[Dict[str, Any]], sources: Dict[str, Any]) -> None:
        _atomic_write(self._vec_path, lambda f: np.save(f, vectors.astype(np.float16)))
        _write_json(self._strips_path, strips)
        _write_json(self._sources_path, sources)
        meta = self.meta()
        meta["updated"] = time.time()
        _write_json(self._meta_path, meta)

    # ---- write path
    def sync_source(self, source: str, kind: str, strips: Iterable[Strip], embed: Callable[[Sequence[Image.Image]], np.ndarray], *,
                    embedder_name: str, dim: int, fingerprint: str = "", title: str = "", batch: int = 8,
                    replace: Optional[Callable[[Dict[str, Any]], bool]] = None,
                    progress: Optional[Callable[[int, int], None]] = None) -> Dict[str, int]:
        """Make the collection hold exactly ``strips`` for ``source``. Returns counts: added, reused, removed, total.

        ``replace``: which of the source's stored rows this call supersedes (a predicate on the row, e.g. "page 3" or
        "the 8192 px tile at y=16384"). ``None`` means the whole source. Strips of the source outside that scope stay
        as they are: that is how a long PDF is indexed one page at a time without holding every page in memory."""
        strips = list(strips)
        with self.lock:
            self._check_embedder(embedder_name, dim, create=False)       # a failed first add must not leave an empty collection
            os.makedirs(self.strips_dir, exist_ok=True)
            old_rows = self.strips()
            vectors = self.vectors()
            if vectors.shape[0] != len(old_rows) or (old_rows and vectors.shape[1] != dim):
                old_rows, vectors = [], np.zeros((0, dim), dtype=np.float16)       # a torn write: trust nothing

            def in_scope(row: Dict[str, Any]) -> bool:
                return row["source"] == source and (replace is None or bool(replace(row)))

            by_key = {(r["page"], r["x"], r["y"]): i for i, r in enumerate(old_rows) if in_scope(r)}
            new_rows: List[Dict[str, Any]] = []
            new_vecs: List[np.ndarray] = []
            todo: List[Tuple[int, Strip]] = []
            reused = 0
            for s in strips:
                i = by_key.get((s.page, s.x, s.y))
                row = {"id": strip_id(self.name, source, s.page, s.x, s.y), "source": source, "page": s.page, "x": s.x,
                       "y": s.y, "width": s.width, "height": s.height, "md5": s.md5}
                new_rows.append(row)
                if i is not None and old_rows[i].get("md5") == s.md5 and os.path.isfile(self.image_path(row["id"])):
                    new_vecs.append(vectors[i])
                    reused += 1
                else:
                    new_vecs.append(np.zeros(dim, dtype=np.float16))
                    todo.append((len(new_rows) - 1, s))
            done = 0
            for start in range(0, len(todo), max(1, batch)):
                chunk = todo[start:start + max(1, batch)]
                emb = np.asarray(embed([s.image for _, s in chunk]), dtype=np.float32)
                if emb.shape != (len(chunk), dim):
                    raise StoreError(f"the embedder returned {emb.shape}, expected {(len(chunk), dim)}", "embedder_failed")
                for (pos, s), vec in zip(chunk, emb):
                    new_vecs[pos] = vec.astype(np.float16)
                    s.image.save(self.image_path(new_rows[pos]["id"]), format="PNG")
                done += len(chunk)
                if progress:
                    progress(done, len(todo))
            keep_idx = [i for i, r in enumerate(old_rows) if not in_scope(r)]
            new_ids = {r["id"] for r in new_rows}
            gone = [r for r in old_rows if in_scope(r) and r["id"] not in new_ids]
            out_rows = [old_rows[i] for i in keep_idx] + new_rows
            out_vecs = [vectors[i] for i in keep_idx] + new_vecs
            matrix = np.stack(out_vecs).astype(np.float16) if out_vecs else np.zeros((0, dim), dtype=np.float16)
            sources = self.sources()
            now = time.time()
            prev = sources.get(source, {})
            mine = [r for r in out_rows if r["source"] == source]
            if mine:
                sources[source] = {"kind": kind, "fingerprint": fingerprint, "title": title or prev.get("title", ""),
                                   "added": prev.get("added", now), "updated": now, "strips": len(mine),
                                   "pages": len({r["page"] for r in mine})}
            else:
                sources.pop(source, None)
            self._check_embedder(embedder_name, dim, create=True)
            self._save(matrix, out_rows, sources)
            for r in gone:
                self._remove_image(r["id"])
            return {"added": len(todo), "reused": reused, "removed": len(gone), "total": len(mine)}

    def _remove_image(self, sid: str) -> None:
        try:
            os.unlink(self.image_path(sid))
        except OSError:
            pass

    def prune_source(self, source: str, keep: Callable[[Dict[str, Any]], bool]) -> int:
        """Drop the source's rows for which ``keep(row)`` is false (what a re-render no longer produces)."""
        with self.lock:
            rows, vectors, sources = self.strips(), self.vectors(), self.sources()
            dim = int(self.meta().get("dim") or 0)
            if vectors.shape[0] != len(rows):
                return 0
            gone = [r for r in rows if r["source"] == source and not keep(r)]
            if not gone:
                return 0
            gone_ids = {r["id"] for r in gone}
            idx = [i for i, r in enumerate(rows) if r["id"] not in gone_ids]
            matrix = vectors[idx] if idx else np.zeros((0, dim), dtype=np.float16)
            out = [rows[i] for i in idx]
            mine = [r for r in out if r["source"] == source]
            if mine:
                sources[source].update(strips=len(mine), pages=len({r["page"] for r in mine}), updated=time.time())
            else:
                sources.pop(source, None)
            self._save(matrix, out, sources)
            for r in gone:
                self._remove_image(r["id"])
            return len(gone)

    def remove_source(self, source: str) -> int:
        with self.lock:
            rows, vectors, sources = self.strips(), self.vectors(), self.sources()
            if source not in sources:
                return 0
            dim = int(self.meta().get("dim") or 0)
            if vectors.shape[0] != len(rows):
                rows, vectors = [], np.zeros((0, dim), dtype=np.float16)
            keep = [i for i, r in enumerate(rows) if r["source"] != source]
            gone = [r for r in rows if r["source"] == source]
            matrix = vectors[keep] if keep else np.zeros((0, dim), dtype=np.float16)
            sources.pop(source, None)
            self._save(matrix, [rows[i] for i in keep], sources)
            for r in gone:
                self._remove_image(r["id"])
            return len(gone)

    def source_fingerprint(self, source: str) -> str:
        return str(self.sources().get(source, {}).get("fingerprint") or "")

    # ---- read path
    def search(self, query_vec: np.ndarray, k: int) -> List[Dict[str, Any]]:
        with self.lock:
            rows, vectors = self.strips(), self.vectors()
        if not rows or vectors.shape[0] != len(rows):
            return []
        scores = vectors.astype(np.float32) @ np.asarray(query_vec, dtype=np.float32)
        k = max(1, min(int(k), len(rows)))
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top])]
        return [{**rows[i], "score": float(scores[i])} for i in top]

    def get_strip(self, sid: str) -> Optional[Dict[str, Any]]:
        for r in self.strips():
            if r["id"] == sid:
                return r
        return None

    def summary(self) -> Dict[str, Any]:
        meta, sources = self.meta(), self.sources()
        return {"collection": self.name, "embedder": meta.get("embedder"), "dim": meta.get("dim"),
                "sources": len(sources), "strips": sum(int(s.get("strips") or 0) for s in sources.values()),
                "updated": meta.get("updated")}


class VisualStore:
    def __init__(self, root: Optional[str] = None):
        self.root = root or default_root()

    def _owner_dir(self, owner: Optional[str]) -> str:
        return os.path.join(self.root, owner_key(owner))

    def collection(self, owner: Optional[str], name: str) -> Collection:
        name = check_collection(name)
        return Collection(os.path.join(self._owner_dir(owner), name), name)

    def list_collections(self, owner: Optional[str]) -> List[Dict[str, Any]]:
        base = self._owner_dir(owner)
        try:
            names = sorted(n for n in os.listdir(base) if COLLECTION_RE.match(n))
        except OSError:
            return []
        out = []
        for n in names:
            c = Collection(os.path.join(base, n), n)
            if c.exists():
                out.append(c.summary())
        return out

    def delete_collection(self, owner: Optional[str], name: str) -> int:
        import shutil

        c = self.collection(owner, name)
        if not c.exists():
            return 0
        with c.lock:
            n = len(c.strips())
            shutil.rmtree(c.dir, ignore_errors=True)
        return n
