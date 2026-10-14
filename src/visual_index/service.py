"""The visual index as a service: add sources, search, fetch a strip, list, remove.

Used by the REST routes, the agent tools and the MCP server alike, so all three behave the same. Everything that
touches the world is injectable (store, embedder pool, renderers, path resolver), so the tests run on a fake embedder
and synthetic renders.
"""
from __future__ import annotations

import logging
import os
import threading
from collections import OrderedDict
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from .embedder import EmbedderPool, EmbedderUnavailable, Qwen3VLEmbedder, default_pool
from .render import Rendered, RenderError, Renderers, render_source
from .store import DEFAULT_COLLECTION, StoreError, VisualStore, check_collection, split_strip_id
from .tiling import TILE_HEIGHT, describe_geometry, strips_from_image

logger = logging.getLogger(__name__)

MAX_K = 20
MAX_SOURCES_PER_CALL = 50


class VisualIndexError(ValueError):
    def __init__(self, message: str, code: str = "invalid_arguments"):
        super().__init__(message)
        self.code = code


def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting

        return get_setting(key, default)
    except Exception:  # noqa: BLE001
        return default


def enabled() -> bool:
    return bool(_setting("visual_index_enabled", True))


def _int_setting(key: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(int(_setting(key, default)), hi))
    except (TypeError, ValueError):
        return default


class VisualIndexService:
    def __init__(self, *, store: Optional[VisualStore] = None, pool: Optional[EmbedderPool] = None,
                 renderers: Optional[Renderers] = None, resolve: Optional[Callable[[str], str]] = None):
        self._store = store
        self._pool = pool
        self.renderers = renderers
        self.resolve = resolve

    @property
    def store(self) -> VisualStore:
        return self._store or VisualStore()

    @property
    def pool(self) -> EmbedderPool:
        return self._pool or default_pool()

    # ------------------------------------------------------------------------------------------ add
    def add(self, owner: Optional[str], sources: Sequence[str], *, collection: str = DEFAULT_COLLECTION, pages: Any = None,
            force: bool = False, progress: Optional[Callable[[str, int, int], None]] = None) -> Dict[str, Any]:
        if not enabled():
            raise VisualIndexError("The visual index is switched off (visual_index_enabled)", "disabled")
        sources = [str(s).strip() for s in (sources or []) if str(s).strip()]
        if not sources:
            raise VisualIndexError("give at least one path or URL in `sources`")
        if len(sources) > MAX_SOURCES_PER_CALL:
            raise VisualIndexError(f"at most {MAX_SOURCES_PER_CALL} sources per call")
        try:
            col = self.store.collection(owner, collection)
        except StoreError as exc:
            raise VisualIndexError(str(exc), exc.code) from exc
        max_pages = _int_setting("visual_index_max_pages", 100, 1, 2000)
        max_tiles = _int_setting("visual_index_max_web_tiles", 8, 1, 32)
        results: List[Dict[str, Any]] = []
        with self.pool.use() as embedder:
            for source in sources:
                results.append(self._add_one(owner, col, embedder, source, pages=pages, force=force, max_pages=max_pages,
                                             max_tiles=max_tiles, progress=progress))
        return {"collection": col.name, "results": results, "summary": col.summary() if col.exists() else {}}

    def _add_one(self, owner: Optional[str], col: Any, embedder: Any, source: str, *, pages: Any, force: bool, max_pages: int,
                 max_tiles: int, progress: Optional[Callable[[str, int, int], None]]) -> Dict[str, Any]:
        try:
            rendered = render_source(source, owner=owner, pages=pages, max_pages=max_pages, max_tiles=max_tiles,
                                     renderers=self.renderers, resolve=self.resolve)
        except RenderError as exc:
            return {"source": source, "ok": False, "error": str(exc), "code": exc.code}
        key = rendered.source
        known = col.sources().get(key)
        if (known and not force and pages in (None, "", []) and rendered.fingerprint
                and known.get("fingerprint") == rendered.fingerprint and known.get("complete")):
            _close(rendered)
            return {"source": key, "ok": True, "unchanged": True, "added": 0, "reused": known.get("strips", 0), "removed": 0,
                    "total": known.get("strips", 0), "kind": rendered.kind, "notes": ["unchanged since it was last indexed"]}
        totals = {"added": 0, "reused": 0, "removed": 0}
        scopes: List[Any] = []
        try:
            for piece in rendered.pieces:
                strips = strips_from_image(piece.image, page=piece.page, y_offset=piece.y_offset)
                y0, y1, page = piece.y_offset, piece.y_offset + piece.image.height, piece.page
                scopes.append((page, y0, y1))
                counts = col.sync_source(
                    key, rendered.kind, strips, embedder.embed_images, embedder_name=embedder.name, dim=embedder.dim,
                    fingerprint=rendered.fingerprint, title=rendered.title,
                    replace=lambda r, p=page, a=y0, b=y1: r["page"] == p and a <= r["y"] < b,
                    progress=(lambda d, n, s=key: progress(s, d, n)) if progress else None)
                for k in totals:
                    totals[k] += counts[k]
                piece.image.close()
            partial = bool(rendered.metrics.get("partial_pages"))
            if not partial:
                totals["removed"] += col.prune_source(key, lambda r: any(p == r["page"] and a <= r["y"] < b for p, a, b in scopes))
            entry = col.sources().get(key)
            if entry is not None:
                entry["complete"] = not partial
                _patch_source(col, key, entry)
        except EmbedderUnavailable:
            raise
        except StoreError as exc:
            return {"source": key, "ok": False, "error": str(exc), "code": exc.code}
        except RenderError as exc:
            return {"source": key, "ok": False, "error": str(exc), "code": exc.code}
        finally:
            _close(rendered)
        total = (col.sources().get(key) or {}).get("strips", 0)
        return {"source": key, "ok": True, "unchanged": False, "kind": rendered.kind, "added": totals["added"],
                "reused": totals["reused"], "removed": totals["removed"], "total": total,
                "pages": len(set(rendered.pages_rendered)), "pages_total": rendered.pages_total, "notes": list(rendered.notes)}

    # ------------------------------------------------------------------------------------------ search
    def search(self, owner: Optional[str], query: str, *, k: int = 5, collection: Optional[str] = None) -> Dict[str, Any]:
        query = str(query or "").strip()
        if not query:
            raise VisualIndexError("give a `query`")
        k = max(1, min(int(k or 5), MAX_K))
        if collection:
            try:
                names = [check_collection(collection)]
            except StoreError as exc:
                raise VisualIndexError(str(exc), exc.code) from exc
            if not self.store.collection(owner, names[0]).exists():
                raise VisualIndexError(f"there is no collection called {names[0]!r}", "unknown_collection")
        else:
            names = [c["collection"] for c in self.store.list_collections(owner)]
        if not names:
            return {"query": query, "results": [], "collections": [], "note": "the visual index is empty; add PDFs, pictures or URLs first"}
        hits: List[Dict[str, Any]] = []
        skipped: List[Dict[str, str]] = []
        searched: List[str] = []
        with self.pool.use() as embedder:
            qv = embedder.embed_query(query)
            for name in names:
                col = self.store.collection(owner, name)
                meta = col.meta()
                if meta.get("embedder") != embedder.name or int(meta.get("dim") or 0) != int(embedder.dim):
                    if collection:
                        raise VisualIndexError(f"collection {name!r} was built with {meta.get('embedder')}; the current embedder is "
                                               f"{embedder.name}", "embedder_mismatch")
                    skipped.append({"collection": name, "reason": f"built with {meta.get('embedder')}"})
                    continue
                searched.append(name)
                sources = col.sources()
                for row in col.search(qv, k):
                    hits.append(_present(col, row, sources.get(row["source"], {})))
        hits.sort(key=lambda h: -h["score"])
        out = {"query": query, "results": hits[:k], "collections": searched}
        if skipped:
            out["skipped"] = skipped
        return out

    # ------------------------------------------------------------------------------------------ strips
    def get_tile(self, owner: Optional[str], sid: str) -> Dict[str, Any]:
        try:
            collection, _ = split_strip_id(sid)
        except StoreError as exc:
            raise VisualIndexError(str(exc), exc.code) from exc
        col = self.store.collection(owner, collection)
        row = col.get_strip(sid) if col.exists() else None
        path = col.image_path(sid) if row else ""
        if not row or not os.path.isfile(path):
            raise VisualIndexError(f"there is no strip {sid!r} (it may have been removed or re-indexed)", "unknown_strip")
        return _present(col, row, col.sources().get(row["source"], {}))

    def read_tile(self, owner: Optional[str], sid: str, question: str = "") -> Dict[str, Any]:
        """Ask the resident vision model to read one strip. ``answered_by`` is the model's name, or "none" (with
        ``vision_available: false`` and the reason) when no vision model could answer: nothing is ever invented."""
        tile = self.get_tile(owner, sid)
        from PIL import Image

        from .vision import ask, vision_ready

        ok, why, _model = vision_ready(owner)
        if not ok:
            return {"tile": tile, "answered_by": "none", "vision_available": False, "text": f"no vision model is available ({why})"}
        prompt = (question or "").strip() or ("Transcribe what is visible: text, tables as Markdown, chart titles, axes and "
                                              "printed values. Say \"unclear\" instead of guessing.")
        with Image.open(tile["path"]) as im:
            answer = ask(im.convert("RGB"), prompt, owner)
        model = str(answer.get("model") or "")
        if not model:
            return {"tile": tile, "answered_by": "none", "vision_available": False, "text": str(answer.get("text") or "")}
        return {"tile": tile, "answered_by": model, "vision_available": True, "text": str(answer.get("text") or "")}

    # ------------------------------------------------------------------------------------------ list / remove
    def list(self, owner: Optional[str], collection: Optional[str] = None) -> Dict[str, Any]:
        if collection:
            try:
                col = self.store.collection(owner, collection)
            except StoreError as exc:
                raise VisualIndexError(str(exc), exc.code) from exc
            if not col.exists():
                raise VisualIndexError(f"there is no collection called {col.name!r}", "unknown_collection")
            return {"collection": col.summary(),
                    "sources": [{"source": s, **info} for s, info in sorted(col.sources().items())]}
        return {"collections": self.store.list_collections(owner)}

    def remove(self, owner: Optional[str], collection: str, source: Optional[str] = None) -> Dict[str, Any]:
        try:
            col = self.store.collection(owner, collection)
        except StoreError as exc:
            raise VisualIndexError(str(exc), exc.code) from exc
        if not col.exists():
            raise VisualIndexError(f"there is no collection called {col.name!r}", "unknown_collection")
        if source:
            candidates = [source]
            try:
                candidates.append((self.resolve or _resolve_quiet)(source))
            except Exception:  # noqa: BLE001
                pass
            for cand in candidates:
                removed = col.remove_source(cand)
                if removed:
                    return {"collection": col.name, "source": cand, "removed_strips": removed}
            raise VisualIndexError(f"{source!r} is not in collection {col.name!r}", "unknown_source")
        n = self.store.delete_collection(owner, col.name)
        return {"collection": col.name, "removed_strips": n, "deleted_collection": True}

    # ------------------------------------------------------------------------------------------ status
    def status(self, owner: Optional[str]) -> Dict[str, Any]:
        pool = self.pool
        if pool._factory is None:
            embedder = Qwen3VLEmbedder().status()
            embedder["loaded"] = pool.loaded()
        else:
            embedder = {"embedder": "custom", "loaded": pool.loaded()}
        vision: Dict[str, Any] = {"available": False}
        try:
            from .vision import vision_ready

            ok, why, model = vision_ready(owner)
            vision = {"available": ok, "model": model, "note": why}
        except Exception as exc:  # noqa: BLE001
            vision["note"] = str(exc)
        return {"enabled": enabled(), "embedder": embedder, "vision": vision, "geometry": describe_geometry(),
                "collections": self.store.list_collections(owner),
                "limits": {"max_k": MAX_K, "max_pages_per_source": _int_setting("visual_index_max_pages", 100, 1, 2000),
                           "view_limit_per_turn": view_limit()}}


def _present(col: Any, row: Dict[str, Any], source_info: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": row["id"], "collection": col.name, "score": round(float(row.get("score", 0.0)), 6) if "score" in row else None,
            "source": row["source"], "kind": source_info.get("kind", ""), "title": source_info.get("title", ""),
            "page": row["page"], "y_offset": row["y"], "x_offset": row["x"], "width": row["width"], "height": row["height"],
            "md5": row.get("md5", ""), "path": col.image_path(row["id"])}


def _patch_source(col: Any, key: str, entry: Dict[str, Any]) -> None:
    from .store import _write_json

    with col.lock:
        sources = col.sources()
        if key in sources:
            sources[key] = entry
            _write_json(col._sources_path, sources)


def _close(rendered: Rendered) -> None:
    try:
        close = getattr(rendered.pieces, "close", None)
        if close:
            close()
    except Exception:  # noqa: BLE001
        pass


def _resolve_quiet(raw: str) -> str:
    from .render import resolve_local_path

    return resolve_local_path(raw)


# ---------------------------------------------------------------------------------------------- strips seen per turn
_VIEWED: "OrderedDict[str, int]" = OrderedDict()
_VIEWED_LOCK = threading.Lock()


def view_limit() -> int:
    return _int_setting("visual_index_view_limit_per_turn", 6, 1, 50)


def take_view_slot(turn_key: str) -> Optional[int]:
    """Count one more strip shown in this turn. Returns how many remain after it, or None when the cap is already used."""
    key = str(turn_key or "-")
    limit = view_limit()
    with _VIEWED_LOCK:
        used = _VIEWED.get(key, 0)
        if used >= limit:
            return None
        _VIEWED[key] = used + 1
        _VIEWED.move_to_end(key)
        while len(_VIEWED) > 256:
            _VIEWED.popitem(last=False)
        return limit - used - 1


def reset_views_for_tests() -> None:
    with _VIEWED_LOCK:
        _VIEWED.clear()


_SERVICE: Optional[VisualIndexService] = None


def get_service() -> VisualIndexService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = VisualIndexService()
    return _SERVICE


def set_service_for_tests(service: Optional[VisualIndexService]) -> Optional[VisualIndexService]:
    global _SERVICE
    previous, _SERVICE = _SERVICE, service
    return previous
