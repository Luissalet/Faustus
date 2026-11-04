"""src/design_canvas_history.py — versions, semantic diff and decision log for
design canvases (Darwin #989, OBJ-30).

A design canvas (``src/design_canvas.py``) is filed as a ``decision`` concept;
filing it again with the same ``concept_id`` overwrote the previous design and
nothing remembered what changed or why. Here every filing is a numbered
version of that canvas, kept in ``<data>/design_canvases/<canvas id>.json``,
and two things are built on top:

* **semantic diff** — not a JSON diff. Each dimension is a list of entries
  (requirements, entities, structure...), and an entry is an element: the diff
  says which elements were ``added``, ``removed``, ``changed`` (the same element
  reworded: a close textual match in the same dimension), ``moved`` to another
  dimension (same text, different section) or ``reordered`` inside its own
  dimension. The prose ``approach`` is compared sentence by sentence.
* **decision log** — who decided what and why, attached to a version, so the
  history reads "v3: dropped the cache (Luis: the API already caches)".
"""
from __future__ import annotations

import difflib
import json
import os
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src import design_canvas

#: Two entries closer than this are the same element reworded.
CHANGED_RATIO = 0.55
MAX_VERSIONS = 200
MAX_DECISIONS = 500
_LOCK = threading.Lock()
_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")


class CanvasHistoryError(ValueError):
    """An unknown canvas, an unknown version or a malformed request."""


def base_dir() -> str:
    override = os.environ.get("FAUSTUS_DESIGN_CANVAS_DIR")
    if override:
        return os.path.abspath(override)
    from src.constants import DATA_DIR
    return os.path.join(str(DATA_DIR), "design_canvases")


def _path(canvas_id: str) -> str:
    if not _ID_RE.match(str(canvas_id or "")):
        raise CanvasHistoryError(f"not a canvas id: {canvas_id!r}")
    return os.path.join(base_dir(), f"{canvas_id}.json")


def _load(canvas_id: str) -> Optional[Dict[str, Any]]:
    try:
        with open(_path(canvas_id), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _save(doc: Dict[str, Any]) -> None:
    path = _path(doc["id"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{uuid.uuid4().hex[:6]}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def get(canvas_id: str) -> Dict[str, Any]:
    doc = _load(canvas_id)
    if doc is None:
        raise CanvasHistoryError(f"no design canvas {canvas_id}")
    return doc


def record_version(canvas_id: str, canvas: Dict[str, Any], *, goal: str = "", name: str = "",
                   author: str = "", source: str = "agent", note: str = "",
                   project_id: str = "", workspace: str = "", validate: bool = True) -> Dict[str, Any]:
    """Append `canvas` as the next version of `canvas_id` (created on first
    use). An identical canvas is not a new version: the latest one is
    returned with ``unchanged: true``."""
    canvas = design_canvas.validate(canvas) if validate else dict(canvas)
    with _LOCK:
        doc = _load(canvas_id) or {"id": canvas_id, "name": name or canvas_id, "project_id": project_id,
                                    "workspace": workspace, "created": time.time(), "versions": [],
                                    "decisions": []}
        if name:
            doc["name"] = name
        doc["project_id"] = doc.get("project_id") or project_id
        doc["workspace"] = doc.get("workspace") or workspace
        versions: List[Dict[str, Any]] = doc.setdefault("versions", [])
        if versions and versions[-1].get("canvas") == canvas:
            return {**versions[-1], "unchanged": True}
        entry = {"version": (versions[-1]["version"] + 1) if versions else 1, "ts": time.time(),
                 "author": str(author or source), "source": str(source), "goal": str(goal or ""),
                 "note": str(note or ""), "canvas": canvas}
        versions.append(entry)
        del versions[:-MAX_VERSIONS]
        doc["updated"] = entry["ts"]
        _save(doc)
        return dict(entry)


def list_canvases(*, project_id: str = "") -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        names = sorted(os.listdir(base_dir()))
    except OSError:
        return out
    for fname in names:
        if not fname.endswith(".json"):
            continue
        doc = _load(fname[:-5])
        if not doc or (project_id and doc.get("project_id") != project_id):
            continue
        versions = doc.get("versions") or []
        out.append({"id": doc["id"], "name": doc.get("name") or doc["id"], "project_id": doc.get("project_id") or "",
                    "versions": len(versions), "latest": versions[-1]["version"] if versions else 0,
                    "updated": doc.get("updated") or doc.get("created"), "decisions": len(doc.get("decisions") or []),
                    "goal": (versions[-1].get("goal") if versions else "") or ""})
    out.sort(key=lambda r: float(r.get("updated") or 0), reverse=True)
    return out


def version(canvas_id: str, number: Optional[int] = None) -> Dict[str, Any]:
    versions = get(canvas_id).get("versions") or []
    if not versions:
        raise CanvasHistoryError(f"design canvas {canvas_id} has no versions")
    if number is None or int(number) <= 0:
        return versions[-1 if number is None else max(-len(versions), int(number) - 1)]
    for v in versions:
        if int(v["version"]) == int(number):
            return v
    raise CanvasHistoryError(f"design canvas {canvas_id} has no version {number}")


# ---------------------------------------------------------------------------
# Semantic diff
# ---------------------------------------------------------------------------
def _norm(text: str) -> str:
    return " ".join(str(text or "").split()).casefold()


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _sentences(text: str) -> List[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?;])\s+", str(text or "").strip()) if s.strip()]


def _list_dims() -> List[Tuple[str, str]]:
    return [(key, heading) for key, heading, _ask in design_canvas.DIMENSIONS if key not in design_canvas._PROSE]


def _reordered(old: Sequence[str], new: Sequence[str]) -> List[str]:
    """Entries kept in both lists whose relative order changed: the ones
    outside the longest common subsequence of the shared entries."""
    shared_old = [e for e in old if e in set(new)]
    shared_new = [e for e in new if e in set(old)]
    matcher = difflib.SequenceMatcher(None, shared_old, shared_new, autojunk=False)
    kept = set()
    for block in matcher.get_matching_blocks():
        kept.update(shared_old[block.a:block.a + block.size])
    return [e for e in shared_new if e not in kept]


def semantic_diff(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    """Element-level changes from canvas `old` to canvas `new`."""
    changes: List[Dict[str, Any]] = []
    removed: Dict[str, List[str]] = {}
    added: Dict[str, List[str]] = {}
    headings = dict(_list_dims())
    for key, _heading in _list_dims():
        a = [str(e) for e in old.get(key) or []]
        b = [str(e) for e in new.get(key) or []]
        na, nb = {_norm(e) for e in a}, {_norm(e) for e in b}
        removed[key] = [e for e in a if _norm(e) not in nb]
        added[key] = [e for e in b if _norm(e) not in na]
        for entry in _reordered([_norm(e) for e in a], [_norm(e) for e in b]):
            text = next(e for e in b if _norm(e) == entry)
            was, now = [_norm(e) for e in a].index(entry), [_norm(e) for e in b].index(entry)
            if was == now:
                continue  # kept its place while others moved around it
            changes.append({"kind": "reordered", "dimension": key, "text": text,
                            "from_index": was, "to_index": now})
    # Same text, another dimension: moved.
    for src_key in list(removed):
        for text in list(removed[src_key]):
            for dst_key, items in added.items():
                hit = next((e for e in items if _norm(e) == _norm(text)), None)
                if dst_key != src_key and hit is not None:
                    changes.append({"kind": "moved", "text": hit, "from_dimension": src_key, "to_dimension": dst_key})
                    removed[src_key].remove(text)
                    items.remove(hit)
                    break
    # Reworded inside the same dimension: changed (best pairs first).
    for key in headings:
        pairs = sorted(((_ratio(r, n), r, n) for r in removed[key] for n in added[key]), reverse=True)
        used_r, used_n = set(), set()
        for score, r, n in pairs:
            if score < CHANGED_RATIO or r in used_r or n in used_n:
                continue
            used_r.add(r)
            used_n.add(n)
            changes.append({"kind": "changed", "dimension": key, "before": r, "after": n,
                            "similarity": round(score, 2)})
        changes += [{"kind": "removed", "dimension": key, "text": r} for r in removed[key] if r not in used_r]
        changes += [{"kind": "added", "dimension": key, "text": n} for n in added[key] if n not in used_n]
    # The prose approach, sentence by sentence.
    for key in design_canvas._PROSE:
        a, b = str(old.get(key) or ""), str(new.get(key) or "")
        if _norm(a) != _norm(b):
            sa, sb = _sentences(a), _sentences(b)
            changes.append({"kind": "changed", "dimension": key, "before": a, "after": b,
                            "similarity": round(_ratio(a, b), 2),
                            "sentences_added": [s for s in sb if _norm(s) not in {_norm(x) for x in sa}],
                            "sentences_removed": [s for s in sa if _norm(s) not in {_norm(x) for x in sb}]})
    order = {"added": 0, "removed": 1, "changed": 2, "moved": 3, "reordered": 4}
    dim_order = {k: i for i, (k, _h, _a) in enumerate(design_canvas.DIMENSIONS)}
    changes.sort(key=lambda c: (dim_order.get(c.get("dimension") or c.get("to_dimension"), 99), order[c["kind"]]))
    counts = {k: sum(1 for c in changes if c["kind"] == k) for k in order}
    return {"changes": changes, "counts": counts, "identical": not changes}


def diff(canvas_id: str, from_version: Optional[int] = None, to_version: Optional[int] = None) -> Dict[str, Any]:
    """Diff two versions of a canvas; by default the one before the latest
    against the latest. The decisions logged on the versions in between
    come with it: the why next to the what."""
    doc = get(canvas_id)
    versions = doc.get("versions") or []
    if len(versions) < 2 and from_version is None:
        raise CanvasHistoryError(f"design canvas {canvas_id} has only one version: nothing to compare")
    new = version(canvas_id, to_version)
    if from_version is None:
        if int(new["version"]) <= 1:
            raise CanvasHistoryError("version 1 is the first one: say which version to compare it with")
        earlier = [v for v in versions if int(v["version"]) < int(new["version"])]
        old = earlier[-1]
    else:
        old = version(canvas_id, from_version)
    result = semantic_diff(old["canvas"], new["canvas"])
    lo, hi = sorted((int(old["version"]), int(new["version"])))
    result.update({
        "canvas_id": canvas_id, "name": doc.get("name") or canvas_id,
        "from": {k: old.get(k) for k in ("version", "ts", "author", "goal", "note")},
        "to": {k: new.get(k) for k in ("version", "ts", "author", "goal", "note")},
        "decisions": [d for d in doc.get("decisions") or [] if lo < int(d.get("version") or 0) <= hi],
    })
    return result


def render_diff(result: Dict[str, Any]) -> str:
    """The diff as a few lines a model or a person reads."""
    heads = {k: h for k, h, _a in design_canvas.DIMENSIONS}
    lines = [f"{result.get('name')}: v{result['from']['version']} -> v{result['to']['version']} · "
             + ", ".join(f"{n} {k}" for k, n in result["counts"].items() if n) if not result["identical"]
             else f"{result.get('name')}: v{result['from']['version']} and v{result['to']['version']} are the same design"]
    for c in result["changes"]:
        dim = heads.get(c.get("dimension") or "", c.get("dimension") or "")
        if c["kind"] == "changed":
            lines.append(f"~ {dim}: {c['before'][:160]} => {c['after'][:160]}")
        elif c["kind"] == "moved":
            lines.append(f"> moved {heads.get(c['from_dimension'])} -> {heads.get(c['to_dimension'])}: {c['text'][:160]}")
        elif c["kind"] == "reordered":
            lines.append(f"^ {dim}: #{c['from_index'] + 1} -> #{c['to_index'] + 1}: {c['text'][:160]}")
        else:
            lines.append(f"{'+' if c['kind'] == 'added' else '-'} {dim}: {c['text'][:200]}")
    for d in result.get("decisions") or []:
        lines.append(f"decision v{d['version']} ({d.get('author') or '?'}): {d['decision']}" + (f" — why: {d['why']}" if d.get("why") else ""))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Decision log
# ---------------------------------------------------------------------------
def add_decision(canvas_id: str, decision: str, *, why: str = "", author: str = "",
                 version_number: Optional[int] = None, kind: str = "decision") -> Dict[str, Any]:
    """Record who decided what and why, attached to a version (default: the
    latest)."""
    decision = " ".join(str(decision or "").split())
    if not decision:
        raise CanvasHistoryError("a decision needs its text")
    with _LOCK:
        doc = get(canvas_id)
        target = version(canvas_id, version_number)
        entry = {"id": uuid.uuid4().hex[:10], "version": int(target["version"]), "ts": time.time(),
                 "author": str(author or "user"), "decision": decision[:2000], "why": str(why or "").strip()[:4000],
                 "kind": str(kind or "decision")}
        decisions = doc.setdefault("decisions", [])
        decisions.append(entry)
        del decisions[:-MAX_DECISIONS]
        _save(doc)
        return entry


def decisions(canvas_id: str, version_number: Optional[int] = None) -> List[Dict[str, Any]]:
    rows = get(canvas_id).get("decisions") or []
    if version_number:
        rows = [d for d in rows if int(d.get("version") or 0) == int(version_number)]
    return list(rows)


def history(canvas_id: str) -> Dict[str, Any]:
    """The canvas with every version's summary (no full canvases) and its log."""
    doc = get(canvas_id)
    rows = []
    for v in doc.get("versions") or []:
        rows.append({k: v.get(k) for k in ("version", "ts", "author", "source", "goal", "note")}
                    | {"summary": design_canvas.summarise(v.get("canvas") or {}, v.get("goal") or "")})
    return {"id": doc["id"], "name": doc.get("name"), "project_id": doc.get("project_id") or "",
            "workspace": doc.get("workspace") or "", "versions": rows, "decisions": doc.get("decisions") or []}
