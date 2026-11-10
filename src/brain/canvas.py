"""
brain/canvas.py — the Cerebro as JSON Canvas 1.0 files (`.canvas`).

Spec: https://jsoncanvas.org/spec/1.0/ (MIT, Obsidian). A canvas is
``{"nodes": [...], "edges": [...]}``: text/file/link/group nodes with an id,
integer x/y/width/height and an optional colour, and edges with
fromNode/toNode, optional sides, ends, colour and label.

* :func:`export_canvas` turns the notes graph (notes + [[wikilinks]]) or the
  entities graph (typed entities + relations) into a canvas — the whole graph
  or a subgraph (a centre and its neighbours up to N hops, a kind filter, a
  tag filter, a node cap). Placement is deterministic: a seeded
  force-directed pass (or columns by hop distance) per group, snapped to a
  grid of cells larger than a node so no two nodes overlap, and the groups
  packed in shelves. ``mode="file"`` points file nodes at the notes
  themselves (the canvas opens in Obsidian when saved inside the vault);
  ``mode="text"`` copies an excerpt so the canvas travels on its own.
* :func:`import_canvas` writes a canvas into the vault as notes and
  wikilinks, idempotent by node id (frontmatter ``canvas`` + ``canvas_id``):
  text nodes become notes, link nodes notes with their URL, file nodes join
  the note they point at (or become a note), a text node that is only
  ``[[Title]]`` is a reference to that note, groups become ``canvas/<group>``
  tags, edges become ``- label: [[Target]]`` lines inside a managed block
  (``%% canvas:<name> %%`` … ``%% /canvas:<name> %%``). Notes the brain
  generates itself (memories, entities, projects) are never rewritten: an
  edge that leaves one of them is written to the canvas's index note.
* :func:`remove_import` undoes an import (notes to the brain trash, or gone
  with ``purge``; managed blocks stripped from other notes).
* :func:`validate` checks a document against the spec.
"""

from __future__ import annotations

import json
import math
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse

from . import db, frontmatter as fm, render, vault, wikilinks

SPEC_VERSION = "1.0"
NODE_TYPES = ("text", "file", "link", "group")
SIDES = ("top", "right", "bottom", "left")
ENDS = ("none", "arrow")
BACKGROUND_STYLES = ("cover", "ratio", "repeat")
SCOPES = ("notes", "entities")
GROUP_BY = ("kind", "folder", "tag", "none")
LAYOUTS = ("auto", "force", "layers")
MODES = ("file", "text")

_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_SINGLE_LINK_RE = re.compile(r"^\s*!?\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]\s*$")
_INLINE_LINK_RE = re.compile(r"!?\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]")

#: Node sizes per export mode, and the gaps that make a grid cell.
SIZES = {"file": (300, 140), "text": (360, 240)}
GAP_X, GAP_Y = 80, 60
GROUP_PAD, GROUP_HEAD = 40, 40
CLUSTER_GAP = 160

KIND_COLORS = {"memory": "4", "personal": "6", "entity": "5", "project": "2",
               "objective": "3", "concept": "5", "unresolved": "1"}
KIND_LABELS = {"note": "Notas", "memory": "Memorias", "personal": "Personal",
               "entity": "Entidades", "project": "Proyectos", "objective": "Objetivos",
               "concept": "Conceptos", "daily": "Diario", "home": "Inicio",
               "unresolved": "Sin resolver"}
INDEX_ID = "__index__"
#: Longest title an imported note gets. Notes the brain writes can carry
#: titles near 100 characters; a shorter cut would break the links in a copy.
TITLE_LIMIT = 120


# ── validation ───────────────────────────────────────────────────────────

def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_color(value: Any) -> bool:
    return isinstance(value, str) and (value in ("1", "2", "3", "4", "5", "6") or bool(_HEX_RE.match(value)))


def validate(doc: Any) -> List[str]:
    """Every way `doc` departs from JSON Canvas 1.0 (empty list = valid)."""
    if not isinstance(doc, dict):
        return ["the top level must be a JSON object"]
    errors: List[str] = []
    nodes = doc.get("nodes", [])
    edges = doc.get("edges", [])
    if not isinstance(nodes, list):
        errors.append("`nodes` must be an array")
        nodes = []
    if not isinstance(edges, list):
        errors.append("`edges` must be an array")
        edges = []
    ids: Set[str] = set()
    node_ids: Set[str] = set()
    for i, node in enumerate(nodes):
        where = f"nodes[{i}]"
        if not isinstance(node, dict):
            errors.append(f"{where} is not an object")
            continue
        nid = node.get("id")
        if not isinstance(nid, str) or not nid:
            errors.append(f"{where}: `id` must be a non-empty string")
        else:
            if nid in ids:
                errors.append(f"{where}: duplicate id {nid!r}")
            ids.add(nid)
            node_ids.add(nid)
        ntype = node.get("type")
        if ntype not in NODE_TYPES:
            errors.append(f"{where}: `type` must be one of {', '.join(NODE_TYPES)}")
        for key in ("x", "y", "width", "height"):
            if not _is_int(node.get(key)):
                errors.append(f"{where}: `{key}` must be an integer")
        if "color" in node and not _valid_color(node["color"]):
            errors.append(f"{where}: `color` must be a preset \"1\"-\"6\" or a hex colour")
        if ntype == "text" and not isinstance(node.get("text"), str):
            errors.append(f"{where}: a text node needs a string `text`")
        if ntype == "file":
            if not isinstance(node.get("file"), str) or not node.get("file"):
                errors.append(f"{where}: a file node needs a string `file`")
            sub = node.get("subpath")
            if sub is not None and (not isinstance(sub, str) or not sub.startswith("#")):
                errors.append(f"{where}: `subpath` must be a string starting with #")
        if ntype == "link" and (not isinstance(node.get("url"), str) or not node.get("url")):
            errors.append(f"{where}: a link node needs a string `url`")
        if ntype == "group":
            if "label" in node and not isinstance(node["label"], str):
                errors.append(f"{where}: `label` must be a string")
            if "background" in node and not isinstance(node["background"], str):
                errors.append(f"{where}: `background` must be a string")
            if "backgroundStyle" in node and node["backgroundStyle"] not in BACKGROUND_STYLES:
                errors.append(f"{where}: `backgroundStyle` must be one of {', '.join(BACKGROUND_STYLES)}")
    for i, edge in enumerate(edges):
        where = f"edges[{i}]"
        if not isinstance(edge, dict):
            errors.append(f"{where} is not an object")
            continue
        eid = edge.get("id")
        if not isinstance(eid, str) or not eid:
            errors.append(f"{where}: `id` must be a non-empty string")
        else:
            if eid in ids:
                errors.append(f"{where}: duplicate id {eid!r}")
            ids.add(eid)
        for key in ("fromNode", "toNode"):
            ref = edge.get(key)
            if not isinstance(ref, str) or not ref:
                errors.append(f"{where}: `{key}` must be a node id")
            elif ref not in node_ids:
                errors.append(f"{where}: `{key}` {ref!r} is not a node of this canvas")
        for key in ("fromSide", "toSide"):
            if key in edge and edge[key] not in SIDES:
                errors.append(f"{where}: `{key}` must be one of {', '.join(SIDES)}")
        for key in ("fromEnd", "toEnd"):
            if key in edge and edge[key] not in ENDS:
                errors.append(f"{where}: `{key}` must be one of {', '.join(ENDS)}")
        if "color" in edge and not _valid_color(edge["color"]):
            errors.append(f"{where}: `color` must be a preset \"1\"-\"6\" or a hex colour")
        if "label" in edge and not isinstance(edge["label"], str):
            errors.append(f"{where}: `label` must be a string")
    return errors


def load(raw: Any) -> Dict[str, Any]:
    """A canvas from a dict, a JSON string or bytes. Floats in the geometry
    (some editors write them) are rounded to the integers the spec asks for."""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8-sig")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw.lstrip("﻿"))
        except ValueError as exc:
            raise ValueError(f"the canvas is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("the canvas must be a JSON object with `nodes` and `edges`")
    for node in raw.get("nodes") or []:
        if isinstance(node, dict):
            for key in ("x", "y", "width", "height"):
                value = node.get(key)
                if isinstance(value, float) and math.isfinite(value):
                    node[key] = int(round(value))
    return raw


def dumps(doc: Dict[str, Any]) -> str:
    """The file text, tab-indented like the reference editor writes it."""
    return json.dumps(doc, ensure_ascii=False, indent="\t") + "\n"


# ── the graph to export ──────────────────────────────────────────────────

def _hid(key: str) -> str:
    return db.sha(key)[:16]


def _note_rows(owner: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    with db.db() as conn:
        note_rows = conn.execute(
            "SELECT path, title, kind, source, tags, frontmatter FROM notes WHERE owner=?", (owner,)
        ).fetchall()
        link_rows = conn.execute(
            "SELECT src_path, target, dst_path, label, is_embed FROM links WHERE owner=?", (owner,)
        ).fetchall()
    return [dict(r) for r in note_rows], [dict(r) for r in link_rows]


def _notes_graph(owner: str) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    note_rows, link_rows = _note_rows(owner)
    nodes: Dict[str, Dict[str, Any]] = {}
    for row in note_rows:
        fmd = db.loads(row["frontmatter"], {})
        nodes[row["path"]] = {
            "key": row["path"], "label": row["title"] or vault._title_from_path(row["path"]),
            "kind": row["kind"] or "note", "tags": db.loads(row["tags"], []) or [],
            "path": row["path"], "source": row["source"] or "",
            "fm": fmd if isinstance(fmd, dict) else {},
        }
    edges: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in link_rows:
        src = row["src_path"]
        if src not in nodes:
            continue
        dst = row["dst_path"]
        if dst and dst in nodes:
            key = dst
        else:
            target = str(row["target"] or "").strip()
            if not target:
                continue
            key = f"unresolved:{target}"
            nodes.setdefault(key, {"key": key, "label": target, "kind": "unresolved", "tags": [],
                                   "path": None, "source": "", "fm": {}})
        edge = edges.setdefault((src, key), {"from": src, "to": key, "label": "", "valid": True})
        if not edge["label"] and row["label"]:
            edge["label"] = str(row["label"])
    return nodes, list(edges.values())


def _entities_graph(owner: str) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    from . import entities

    raw = entities.graph(owner, limit=5000)
    with db.db() as conn:
        rows = conn.execute(
            "SELECT path, source FROM notes WHERE owner=? AND source LIKE 'ent:%'", (owner,)
        ).fetchall()
    path_of = {r["source"]: r["path"] for r in rows}
    nodes = {
        n["id"]: {"key": n["id"], "label": n["label"], "kind": n["kind"] or "entity", "tags": [],
                  "path": path_of.get(n["id"]), "source": n["id"], "fm": {}}
        for n in raw.get("nodes", [])
    }
    edges: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for e in raw.get("edges", []):
        if e["from"] not in nodes or e["to"] not in nodes:
            continue
        valid = bool(e.get("valid_now", True))
        label = str(e.get("kind") or "") + ("" if valid else " (cerrada)")
        edges.setdefault((e["from"], e["to"], label),
                         {"from": e["from"], "to": e["to"], "label": label, "valid": valid})
    return nodes, list(edges.values())


def _resolve_center(nodes: Dict[str, Dict[str, Any]], center: Any) -> Optional[str]:
    text = str(center or "").strip().replace("\\", "/")
    if not text:
        return None
    for candidate in (text, f"ent:{text}", f"{text}.md"):
        if candidate in nodes:
            return candidate
    bare = text[:-3] if text.lower().endswith(".md") else text
    folded = db.fold(os.path.basename(bare))
    matches = sorted(k for k, n in nodes.items() if n["kind"] != "unresolved" and db.fold(n["label"]) == folded)
    if matches:
        return matches[0]
    raise ValueError(f"no note or entity matches {center!r}")


def _tag_matches(node_tags: Iterable[str], wanted: Set[str]) -> bool:
    for tag in node_tags:
        low = str(tag).lower().lstrip("#")
        if any(low == w or low.startswith(w + "/") for w in wanted):
            return True
    return False


def _select(nodes, edges, *, center, depth, kinds, tags, include_unresolved, limit):
    adjacency: Dict[str, Set[str]] = {}
    for e in edges:
        adjacency.setdefault(e["from"], set()).add(e["to"])
        adjacency.setdefault(e["to"], set()).add(e["from"])
    dist: Dict[str, int] = {}
    if center:
        dist[center] = 0
        frontier = [center]
        for hop in range(1, depth + 1):
            nxt = []
            for key in frontier:
                for other in sorted(adjacency.get(key, ())):
                    if other not in dist:
                        dist[other] = hop
                        nxt.append(other)
            frontier = nxt
        keep = set(dist)
    else:
        keep = set(nodes)
    kind_set = {str(k).strip().lower() for k in (kinds or []) if str(k).strip()}
    tag_set = {str(t).strip().lower().lstrip("#") for t in (tags or []) if str(t).strip().lstrip("#")}

    def passes(key: str) -> bool:
        if key == center:
            return True
        node = nodes[key]
        if node["kind"] == "unresolved":
            return include_unresolved and not tag_set and (not kind_set or "unresolved" in kind_set)
        if kind_set and node["kind"].lower() not in kind_set:
            return False
        if tag_set and not _tag_matches(node["tags"], tag_set):
            return False
        return True

    keep = {k for k in keep if passes(k)}
    keep = {k for k in keep if nodes[k]["kind"] != "unresolved"
            or any(m in keep and nodes[m]["kind"] != "unresolved" for m in adjacency.get(k, ()))}
    degree = {k: len(adjacency.get(k, ())) for k in keep}
    ordered = sorted(keep, key=lambda k: (dist.get(k, 0), -degree[k], k))
    truncated = len(ordered) > limit
    kept = set(ordered[:limit])
    chosen = [e for e in edges if e["from"] in kept and e["to"] in kept]
    return kept, chosen, dist, truncated


# ── layout ───────────────────────────────────────────────────────────────

def _nearest_free(tx: float, ty: float, taken: Set[Tuple[int, int]]) -> Tuple[int, int]:
    c0, r0 = int(math.floor(tx + 0.5)), int(math.floor(ty + 0.5))
    radius = 0
    while True:
        ring = []
        for dc in range(-radius, radius + 1):
            for dr in range(-radius, radius + 1):
                if max(abs(dc), abs(dr)) != radius:
                    continue
                cell = (c0 + dc, r0 + dr)
                if cell not in taken:
                    ring.append(((cell[0] - tx) ** 2 + (cell[1] - ty) ** 2, cell[1], cell[0]))
        if ring:
            best = min(ring)
            return best[2], best[1]
        radius += 1


def _snap(keys: Sequence[str], xs: List[float], ys: List[float], aspect: float) -> Dict[str, Tuple[int, int]]:
    """Float positions -> one distinct grid cell each, nearest first from the
    middle: a cell is wider and taller than a node, so nothing overlaps."""
    n = len(keys)
    cx, cy = sum(xs) / n, sum(ys) / n
    order = sorted(range(n), key=lambda i: ((xs[i] - cx) ** 2 + (ys[i] - cy) ** 2, keys[i]))
    taken: Set[Tuple[int, int]] = set()
    cells: Dict[str, Tuple[int, int]] = {}
    for i in order:
        cell = _nearest_free(xs[i] - cx, (ys[i] - cy) * aspect, taken)
        taken.add(cell)
        cells[keys[i]] = cell
    return cells


def _force_cells(keys: Sequence[str], edges: Sequence[Dict[str, Any]], aspect: float) -> Dict[str, Tuple[int, int]]:
    """Fruchterman-Reingold seeded on a sunflower spiral (no randomness), a
    fixed number of rounds that shrinks with the node count, then snapped."""
    n = len(keys)
    if n == 1:
        return {keys[0]: (0, 0)}
    idx = {k: i for i, k in enumerate(keys)}
    golden = math.pi * (3 - math.sqrt(5))
    xs = [1.2 * math.sqrt(i + 0.5) * math.cos(i * golden) for i in range(n)]
    ys = [1.2 * math.sqrt(i + 0.5) * math.sin(i * golden) for i in range(n)]
    pairs = [(idx[e["from"]], idx[e["to"]]) for e in edges
             if e["from"] in idx and e["to"] in idx and e["from"] != e["to"]]
    k = 1.5
    k2 = k * k
    rounds = max(20, min(200, int(1_500_000 / max(1.0, n * (n - 1) / 2))))
    temp = max(1.0, math.sqrt(n))
    cool = temp / (rounds + 1)
    for _ in range(rounds):
        dx = [0.0] * n
        dy = [0.0] * n
        for i in range(n):
            xi, yi = xs[i], ys[i]
            for j in range(i + 1, n):
                ddx, ddy = xi - xs[j], yi - ys[j]
                d2 = ddx * ddx + ddy * ddy
                if d2 < 1e-6:
                    ddx, ddy = 0.01 * (i - j), 0.01
                    d2 = ddx * ddx + ddy * ddy
                f = k2 / d2
                dx[i] += ddx * f
                dy[i] += ddy * f
                dx[j] -= ddx * f
                dy[j] -= ddy * f
        for a, b in pairs:
            ddx, ddy = xs[a] - xs[b], ys[a] - ys[b]
            f = math.sqrt(ddx * ddx + ddy * ddy) / k
            dx[a] -= ddx * f
            dy[a] -= ddy * f
            dx[b] += ddx * f
            dy[b] += ddy * f
        for i in range(n):
            dx[i] -= 0.05 * xs[i]
            dy[i] -= 0.05 * ys[i]
            m = math.sqrt(dx[i] * dx[i] + dy[i] * dy[i])
            if m > temp:
                dx[i] *= temp / m
                dy[i] *= temp / m
            xs[i] += dx[i]
            ys[i] += dy[i]
        temp = max(0.05, temp - cool)
    return _snap(keys, xs, ys, aspect)


def _layer_cells(keys: Sequence[str], edges: Sequence[Dict[str, Any]], center: Optional[str]) -> Dict[str, Tuple[int, int]]:
    """Columns by hop distance from a root (the centre, else the busiest
    node of each connected piece); pieces stacked top to bottom."""
    keyset = set(keys)
    adjacency: Dict[str, Set[str]] = {k: set() for k in keys}
    for e in edges:
        if e["from"] in keyset and e["to"] in keyset and e["from"] != e["to"]:
            adjacency[e["from"]].add(e["to"])
            adjacency[e["to"]].add(e["from"])
    remaining = set(keys)
    cells: Dict[str, Tuple[int, int]] = {}
    row_offset = 0
    while remaining:
        root = center if center in remaining else min(remaining, key=lambda k: (-len(adjacency[k]), k))
        level = {root: 0}
        queue = [root]
        while queue:
            nxt = []
            for key in queue:
                for other in sorted(adjacency[key]):
                    if other in remaining and other not in level:
                        level[other] = level[key] + 1
                        nxt.append(other)
            queue = nxt
        columns: Dict[int, List[str]] = {}
        for key, lvl in level.items():
            columns.setdefault(lvl, []).append(key)
        height = max(len(v) for v in columns.values())
        rows_of: Dict[str, int] = {}
        for lvl in sorted(columns):
            def parent_row(key: str) -> float:
                parents = [rows_of[p] for p in adjacency[key] if p in rows_of and level.get(p) == lvl - 1]
                return min(parents) if parents else 0.0
            members = sorted(columns[lvl], key=lambda k: (parent_row(k), k))
            top = (height - len(members)) // 2
            for i, key in enumerate(members):
                rows_of[key] = top + i
                cells[key] = (lvl, row_offset + top + i)
        row_offset += height + 1
        remaining -= set(level)
    return cells


def _group_key(node: Dict[str, Any], group_by: str) -> str:
    if group_by == "kind":
        return node["kind"] or "note"
    if group_by == "folder":
        path = node.get("path") or ""
        if not path:
            return "(sin nota)"
        return path.split("/")[0] if "/" in path else "(raíz)"
    if group_by == "tag":
        tags = sorted(str(t) for t in node.get("tags") or [])
        return tags[0] if tags else "(sin etiqueta)"
    return ""


def _group_label(key: str, group_by: str) -> str:
    return KIND_LABELS.get(key, key) if group_by == "kind" else key


def _excerpt(owner: str, path: str, title: str, chars: int) -> str:
    text = vault._read(vault.abs_path(owner, path)) or ""
    _, body = fm.split(text)
    body = body.replace(render.GENERATED_MARKER, "").strip()
    first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
    heading = re.sub(r"^#{1,6}\s+", "", first) if first.startswith("#") else ""
    if heading and db.fold(heading) != db.fold(title):
        heading = ""
    if len(body) > chars:
        cut = body[:chars]
        nl = cut.rfind("\n")
        body = (cut[:nl] if nl > chars // 2 else cut).rstrip() + "\n…"
    return body if heading else (f"# {title}\n\n{body}".rstrip() if body else f"# {title}")


def _color_for(node: Dict[str, Any], scope: str) -> Optional[str]:
    if node["kind"] == "unresolved":
        return "1"
    if scope == "entities":
        return str(1 + int(db.sha(node["kind"])[:8], 16) % 6)
    return KIND_COLORS.get(node["kind"])


def export_canvas(owner: str = "", *, scope: str = "notes", center: Any = None, depth: int = 1,
                  kinds: Optional[Sequence[str]] = None, tags: Optional[Sequence[str]] = None,
                  group_by: Optional[str] = None, layout: str = "auto", mode: str = "file",
                  include_unresolved: bool = True, limit: int = 300, excerpt_chars: int = 600,
                  save: bool = False, name: str = "", save_path: str = "") -> Dict[str, Any]:
    """The owner's graph (or a subgraph) as a JSON Canvas 1.0 document."""
    owner = str(owner or "")
    scope = scope if scope in SCOPES else "notes"
    mode = mode if mode in MODES else "file"
    layout = layout if layout in LAYOUTS else "auto"
    depth = max(0, min(6, int(depth if depth is not None else 1)))
    limit = max(1, min(2000, int(limit or 300)))
    excerpt_chars = max(80, min(4000, int(excerpt_chars or 600)))
    if isinstance(kinds, str):
        kinds = [k for k in kinds.split(",") if k.strip()]
    if isinstance(tags, str):
        tags = [t for t in tags.split(",") if t.strip()]

    if scope == "notes":
        from . import notes as notes_mod
        notes_mod.reindex(owner)
        nodes, edges = _notes_graph(owner)
    else:
        nodes, edges = _entities_graph(owner)
    center_key = _resolve_center(nodes, center)
    kept, chosen, dist, truncated = _select(
        nodes, edges, center=center_key, depth=depth, kinds=kinds, tags=tags,
        include_unresolved=bool(include_unresolved), limit=limit)

    if group_by not in GROUP_BY:
        group_by = "none" if center_key else "kind"
    if layout == "auto":
        layout = "layers" if center_key else "force"
    node_w, node_h = SIZES[mode]
    cell_w, cell_h = node_w + GAP_X, node_h + GAP_Y
    grouped = group_by != "none"

    clusters: Dict[str, List[str]] = {}
    for key in sorted(kept):
        clusters.setdefault(_group_key(nodes[key], group_by) if grouped else "", []).append(key)

    placed: List[Tuple[str, Dict[str, Tuple[int, int]], int, int, int, int]] = []
    for gkey, members in clusters.items():
        inner = [e for e in chosen if e["from"] in set(members) and e["to"] in set(members)]
        if layout == "layers":
            cells = _layer_cells(members, inner, center_key)
        else:
            cells = _force_cells(members, inner, cell_w / cell_h)
        min_c = min(c for c, _ in cells.values())
        min_r = min(r for _, r in cells.values())
        cols = max(c for c, _ in cells.values()) - min_c + 1
        rows = max(r for _, r in cells.values()) - min_r + 1
        placed.append((gkey, cells, min_c, min_r, cols, rows))

    def footprint(item) -> Tuple[int, int]:
        _, _, _, _, cols, rows = item
        width = cols * cell_w - GAP_X + (2 * GROUP_PAD if grouped else 0)
        height = rows * cell_h - GAP_Y + (GROUP_PAD + GROUP_HEAD if grouped else 0)
        return width, height

    placed.sort(key=lambda item: (-(item[4] * item[5]), item[0]))
    sizes = [footprint(item) for item in placed]
    total_area = sum(w * h for w, h in sizes) or 1
    shelf_w = max([w for w, _ in sizes] + [int(math.sqrt(total_area) * 1.6)])

    out_nodes: List[Dict[str, Any]] = []
    group_nodes: List[Dict[str, Any]] = []
    position: Dict[str, Tuple[int, int]] = {}
    used_ids: Set[str] = set()

    def unique_id(seed: str) -> str:
        nid = _hid(seed)
        n = 1
        while nid in used_ids:
            nid = _hid(f"{seed}#{n}")
            n += 1
        used_ids.add(nid)
        return nid

    node_id: Dict[str, str] = {}
    x = y = row_h = 0
    for item, (width, height) in zip(placed, sizes):
        gkey, cells, min_c, min_r, _, _ = item
        if x > 0 and x + width > shelf_w:
            x, y, row_h = 0, y + row_h + CLUSTER_GAP, 0
        if grouped:
            group = {"id": unique_id(f"group:{scope}:{group_by}:{gkey}"), "type": "group",
                     "x": x, "y": y, "width": width, "height": height,
                     "label": _group_label(gkey, group_by)}
            color = KIND_COLORS.get(gkey) if group_by == "kind" and scope == "notes" else None
            if color:
                group["color"] = color
            group_nodes.append(group)
        ox = x + (GROUP_PAD if grouped else 0)
        oy = y + (GROUP_PAD + GROUP_HEAD if grouped else 0)
        for key in sorted(cells, key=lambda k: (cells[k][1], cells[k][0], k)):
            c, r = cells[key]
            node = nodes[key]
            px, py = ox + (c - min_c) * cell_w, oy + (r - min_r) * cell_h
            nid = unique_id(f"{scope}:{key}")
            node_id[key] = nid
            position[nid] = (px, py)
            out = _node_body(owner, node, scope, mode, excerpt_chars)
            out = {"id": nid, **out, "x": px, "y": py, "width": node_w, "height": node_h}
            color = _color_for(node, scope)
            if color:
                out["color"] = color
            out_nodes.append(out)
        x += width + CLUSTER_GAP
        row_h = max(row_h, height)

    out_edges: List[Dict[str, Any]] = []
    for e in sorted(chosen, key=lambda e: (e["from"], e["to"], e["label"])):
        a, b = node_id[e["from"]], node_id[e["to"]]
        (ax, ay), (bx, by) = position[a], position[b]
        dx, dy = bx - ax, by - ay
        if a == b:
            sides = ("right", "top")
        elif abs(dx) * cell_h >= abs(dy) * cell_w:
            sides = ("right", "left") if dx >= 0 else ("left", "right")
        else:
            sides = ("bottom", "top") if dy >= 0 else ("top", "bottom")
        edge = {"id": unique_id(f"edge:{a}->{b}:{e['label']}"), "fromNode": a, "fromSide": sides[0],
                "toNode": b, "toSide": sides[1], "toEnd": "arrow"}
        if e["label"]:
            edge["label"] = e["label"]
        if not e.get("valid", True):
            edge["color"] = "1"
        out_edges.append(edge)

    doc = {"nodes": group_nodes + out_nodes, "edges": out_edges}
    errors = validate(doc)
    if not name:
        name = (f"Cerebro - {nodes[center_key]['label']}" if center_key
                else ("Cerebro - entidades" if scope == "entities" else "Cerebro"))
    name = db.safe_filename(name, limit=80)
    result: Dict[str, Any] = {
        "ok": not errors, "spec": SPEC_VERSION, "scope": scope, "mode": mode, "layout": layout,
        "group_by": group_by, "center": center_key, "depth": depth if center_key else None,
        "name": name, "file_name": f"{name}.canvas", "truncated": truncated,
        "stats": {"nodes": len(out_nodes), "edges": len(out_edges), "groups": len(group_nodes),
                  "graph_nodes": len(nodes), "graph_edges": len(edges)},
        "errors": errors, "canvas": doc, "saved_to": None, "path": None,
    }
    if save:
        rel = f"Canvas/{name}.canvas"
        full = os.path.join(vault.vault_root(owner), "Canvas", f"{name}.canvas")
        vault._atomic_write(full, dumps(doc))
        result["path"] = rel
        result["saved_to"] = full
    if save_path:
        target = os.path.abspath(os.path.expanduser(str(save_path)))
        if os.path.isdir(target):
            target = os.path.join(target, f"{name}.canvas")
        vault._atomic_write(target, dumps(doc))
        result["saved_to"] = target
    return result


def _node_body(owner: str, node: Dict[str, Any], scope: str, mode: str, chars: int) -> Dict[str, Any]:
    if node["kind"] == "unresolved":
        return {"type": "text", "text": f"[[{node['label']}]]"}
    fmd = node.get("fm") or {}
    url = fmd.get("url")
    if str(fmd.get("canvas_type") or "") == "link" and isinstance(url, str) and url:
        return {"type": "link", "url": url}
    path = node.get("path")
    if mode == "file" and path:
        return {"type": "file", "file": path}
    if scope == "notes" and path:
        return {"type": "text", "text": _excerpt(owner, path, node["label"], chars)}
    if scope == "entities":
        return {"type": "text", "text": f"**{node['label']}**\n\n_{node['kind']}_"}
    return {"type": "text", "text": f"# {node['label']}"}


# ── import ───────────────────────────────────────────────────────────────

def _block_re(name: str) -> "re.Pattern[str]":
    marker = re.escape(name)
    return re.compile(r"\n*%% canvas:" + marker + r" %%\n.*?%% /canvas:" + marker + r" %%\n?", re.S)


def _strip_block(text: str, name: str) -> str:
    stripped = _block_re(name).sub("\n", text)
    return stripped.rstrip("\n") + "\n" if stripped.strip() else stripped


def _clean_line(line: str) -> str:
    s = line.strip()
    s = re.sub(r"^#{1,6}\s+", "", s)
    s = re.sub(r"^(?:[-*+]\s+(?:\[.\]\s+)?|\d+[.)]\s+|>\s*)+", "", s)
    s = _INLINE_LINK_RE.sub(lambda m: (m.group(2) or m.group(1)).strip(), s)
    s = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", s)
    for token in ("**", "__", "`", "==", "~~"):
        s = s.replace(token, "")
    s = s.strip("*_ ")
    return s


def _title_from_text(text: str, fallback: str) -> str:
    for line in str(text or "").splitlines():
        cleaned = _clean_line(line)
        if cleaned:
            title = db.safe_filename(cleaned, limit=TITLE_LIMIT)
            if title and title != "untitled":
                return title
    return fallback


def _title_from_url(url: str, fallback: str) -> str:
    try:
        parsed = urlparse(url)
    except ValueError:
        return fallback
    text = (parsed.netloc + parsed.path).strip("/") or url
    title = db.safe_filename(text.replace("/", " "), limit=TITLE_LIMIT)
    return title if title and title != "untitled" else fallback


def _inside(node: Dict[str, Any], group: Dict[str, Any]) -> bool:
    cx = node["x"] + node["width"] / 2
    cy = node["y"] + node["height"] / 2
    return (group["x"] <= cx <= group["x"] + group["width"]
            and group["y"] <= cy <= group["y"] + group["height"])


def _tag_slug(label: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", db.fold(label)).strip("-")
    return f"canvas/{slug or 'grupo'}"


def _links_in(text: str) -> Set[str]:
    return {db.fold(link["target"]) for link in wikilinks.parse(text)}


def import_canvas(owner: str = "", raw: Any = None, *, name: str = "", folder: str = "Canvas",
                  dry_run: bool = False, prune: bool = False, keep_file: bool = True) -> Dict[str, Any]:
    """Write a canvas into the vault (see the module docstring). Running it
    again with the same canvas changes nothing; with an edited canvas only
    the notes whose node changed are rewritten."""
    from . import notes as notes_mod

    owner = str(owner or "")
    doc = load(raw)
    errors = validate(doc)
    if errors:
        raise ValueError("not a valid JSON Canvas 1.0 document: " + "; ".join(errors[:8]))
    name = db.safe_filename(name or "Canvas", limit=60)
    folder = str(folder or "Canvas").strip().strip("/") or "Canvas"
    base = f"{folder}/{name}"
    vault.validate_path(f"{base}/x.md")
    root = vault.vault_root(owner)

    notes_mod.reindex(owner)
    rows, _ = _note_rows(owner)
    by_path = {r["path"]: r for r in rows}
    fm_of = {r["path"]: (db.loads(r["frontmatter"], {}) or {}) for r in rows}
    mine: Dict[str, str] = {}
    for r in sorted(rows, key=lambda r: r["path"]):
        fmd = fm_of[r["path"]]
        if isinstance(fmd, dict) and str(fmd.get("canvas") or "") == name and fmd.get("canvas_id"):
            mine.setdefault(str(fmd["canvas_id"]), r["path"])
    own_paths = set(mine.values())
    title_index: Dict[str, str] = {}
    for r in sorted(rows, key=lambda r: r["path"]):
        if r["path"] not in own_paths:
            title_index.setdefault(db.fold(r["title"]), r["path"])
    taken = set(title_index)

    def unique_title(title: str) -> str:
        candidate, n = title, 2
        while db.fold(candidate) in taken:
            candidate = f"{title} {n}"
            n += 1
        taken.add(db.fold(candidate))
        return candidate

    texts: Dict[str, Optional[str]] = {}

    def text_of(path: str) -> Optional[str]:
        if path not in texts:
            texts[path] = vault._read(os.path.join(root, *path.split("/")))
        return texts[path]

    def editable(path: str) -> bool:
        row = by_path.get(path)
        if row is None or row["source"]:
            return False
        text = text_of(path)
        return text is not None and render.GENERATED_MARKER not in text

    nodes = doc.get("nodes") or []
    groups = [n for n in nodes if n["type"] == "group"]
    items = [n for n in nodes if n["type"] != "group"]
    group_ids = {g["id"] for g in groups}
    group_tags: Dict[str, List[str]] = {}
    for n in items:
        labels = sorted({_tag_slug(g.get("label") or f"grupo {g['id'][:6]}") for g in groups if _inside(n, g)})
        group_tags[n["id"]] = labels

    plan: Dict[str, Dict[str, Any]] = {}
    # nodes imported before keep their titles first, so a new node cannot take one
    for n in sorted(items, key=lambda n: (0 if n["id"] in mine else 1, n["id"])):
        nid, ntype = n["id"], n["type"]
        if ntype == "text":
            m = _SINGLE_LINK_RE.match(n["text"])
            if m:
                target = m.group(1).strip()
                path = title_index.get(db.fold(target))
                if path:
                    plan[nid] = {"kind": "existing", "path": path, "title": by_path[path]["title"]}
                else:
                    plan[nid] = {"kind": "ref", "path": None, "title": target}
                continue
        if ntype == "file":
            file_ref = n["file"].replace("\\", "/").lstrip("/")
            if file_ref.lower().endswith(".md"):
                path = file_ref if file_ref in by_path else None
                if path is None:
                    path = title_index.get(db.fold(vault._title_from_path(file_ref)))
                if path:
                    plan[nid] = {"kind": "existing", "path": path, "title": by_path[path]["title"]}
                    continue
        fallback = f"Nodo {nid[:8]}"
        if ntype == "text":
            title = _title_from_text(n["text"], fallback)
        elif ntype == "link":
            title = _title_from_url(n["url"], fallback)
        else:
            title = db.safe_filename(vault._title_from_path(n["file"]), limit=TITLE_LIMIT) or fallback
        old = mine.get(nid)
        if old and db.fold(vault._title_from_path(old)) == db.fold(title) and db.fold(title) not in taken:
            title = vault._title_from_path(old)
            taken.add(db.fold(title))
        else:
            title = unique_title(title)
        plan[nid] = {"kind": "create", "path": f"{base}/{title}.md", "title": title, "old": old, "node": n}

    block_lines: Dict[str, List[str]] = {}
    index_lines: List[str] = []
    counts = {"edges_written": 0, "edges_duplicate": 0, "edges_via_index": 0, "edges_skipped": 0}
    seen_lines: Set[Tuple[str, str]] = set()
    base_links: Dict[str, Set[str]] = {}
    for p in plan.values():
        if p["kind"] == "create":
            node = p["node"]
            base_links[p["path"]] = _links_in(node.get("text") or "")
        elif p["kind"] == "existing" and p["path"] not in base_links:
            base_links[p["path"]] = _links_in(_strip_block(text_of(p["path"]) or "", name))
    for e in doc.get("edges") or []:
        a, b = e["fromNode"], e["toNode"]
        if a in group_ids or b in group_ids or a not in plan or b not in plan:
            counts["edges_skipped"] += 1
            continue
        if e.get("fromEnd") == "arrow" and e.get("toEnd", "arrow") == "none":
            a, b = b, a
        src, dst = plan[a], plan[b]
        label = " ".join(str(e.get("label") or "").split())
        link = f"[[{dst['title']}]]"
        if src["kind"] == "create" or (src["kind"] == "existing" and editable(src["path"])):
            path = src["path"]
            if db.fold(dst["title"]) in base_links.get(path, set()):
                counts["edges_duplicate"] += 1
                continue
            line = f"- {label}: {link}" if label else f"- {link}"
            if (path, line) in seen_lines:
                counts["edges_duplicate"] += 1
                continue
            seen_lines.add((path, line))
            base_links.setdefault(path, set()).add(db.fold(dst["title"]))
            block_lines.setdefault(path, []).append(line)
            counts["edges_written"] += 1
        else:
            middle = f" {label} " if label else " "
            line = f"- [[{src['title']}]] →{middle}→ {link}" if label else f"- [[{src['title']}]] → {link}"
            if ("__index__", line) not in seen_lines:
                seen_lines.add(("__index__", line))
                index_lines.append(line)
            counts["edges_via_index"] += 1

    def block_for(path: str, tags: Sequence[str] = ()) -> str:
        lines = list(block_lines.get(path, []))
        if tags:
            lines.append(" ".join(f"#{t}" for t in tags))
        if not lines:
            return ""
        return f"%% canvas:{name} %%\n" + "\n".join(lines) + f"\n%% /canvas:{name} %%\n"

    now = db.now_iso()
    writes: Dict[str, str] = {}
    moves: List[Tuple[str, str]] = []
    report_notes: List[Dict[str, Any]] = []
    tally = {"created": 0, "updated": 0, "unchanged": 0, "linked_existing": 0, "refs_unresolved": 0}

    def compose(fields: Dict[str, Any], body: str, previous: Optional[str]) -> Optional[str]:
        """The new file text, or None when nothing but `updated` would change."""
        prev_fm = fm.split(previous)[0] if previous else {}
        fields = dict(fields)
        fields["created"] = prev_fm.get("created") or now
        fields["updated"] = prev_fm.get("updated") or now
        text = fm.join(fields, body)
        if previous is not None and text == previous:
            return None
        fields["updated"] = now
        return fm.join(fields, body)

    for nid, p in sorted(plan.items()):
        if p["kind"] == "ref":
            tally["refs_unresolved"] += 1
            report_notes.append({"id": nid, "path": None, "title": p["title"], "action": "reference"})
            continue
        if p["kind"] == "existing":
            tally["linked_existing"] += 1
            report_notes.append({"id": nid, "path": p["path"], "title": p["title"], "action": "linked"})
            continue
        node = p["node"]
        fields: Dict[str, Any] = {"kind": "note", "canvas": name, "canvas_id": nid, "canvas_type": node["type"]}
        if group_tags.get(nid):
            fields["tags"] = group_tags[nid]
        if node.get("color"):
            fields["canvas_color"] = node["color"]
        if node["type"] == "text":
            main = node["text"].strip("\n")
        elif node["type"] == "link":
            fields["url"] = node["url"]
            main = f"[{node['url']}]({node['url']})"
        else:
            ref = node["file"] + (node.get("subpath") or "")
            fields["canvas_file"] = ref
            main = (f"Nota del canvas original: `{ref}`" if node["file"].lower().endswith(".md")
                    else f"![](<{ref}>)")
        block = block_for(p["path"])
        body = "\n" + main + "\n" + ("\n" + block if block else "")
        old = p.get("old")
        previous = text_of(old) if old else None
        if old and old != p["path"]:
            moves.append((old, p["path"]))
        new_text = compose(fields, body, previous)
        if new_text is None and not (old and old != p["path"]):
            tally["unchanged"] += 1
            action = "unchanged"
        else:
            writes[p["path"]] = new_text if new_text is not None else (previous or "")
            action = "updated" if old else "created"
            tally[action] += 1
        report_notes.append({"id": nid, "path": p["path"], "title": p["title"], "action": action})

    # existing free notes get our block (the edges out of them) or lose it;
    # group tags are only written on notes the canvas created
    existing_paths: Dict[str, Set[str]] = {}
    for nid, p in plan.items():
        if p["kind"] == "existing":
            existing_paths.setdefault(p["path"], set())
    marker = f"%% canvas:{name} %%"
    with db.db() as conn:
        holders = [r["path"] for r in conn.execute(
            "SELECT path FROM notes_fts WHERE owner=? AND body LIKE ?",
            (owner, "%" + db.fold(f"canvas:{name}") + "%"),
        ).fetchall()]
    for path in sorted(set(existing_paths) | set(holders)):
        if path in own_paths or path.startswith(base + "/") or not editable(path):
            continue
        current = text_of(path) or ""
        if path not in existing_paths and marker not in current:
            continue
        tags = sorted(existing_paths.get(path, set()))
        stripped = _strip_block(current, name) if marker in current else current
        block = block_for(path, tags)
        new_text = (stripped.rstrip("\n") + "\n\n" + block) if block else stripped
        if new_text != current:
            writes[path] = new_text
            tally["updated"] += 1

    index_path = None
    if index_lines or INDEX_ID in mine:
        old = mine.get(INDEX_ID)
        title = vault._title_from_path(old) if old else unique_title(f"{name} - enlaces")
        index_path = old or f"{base}/{title}.md"
        body = ("\nEnlaces del canvas que salen de notas que el Cerebro genera "
                "(memorias, entidades, proyectos) o de referencias sin nota:\n\n"
                + "\n".join(index_lines) + "\n")
        new_text = compose({"kind": "note", "canvas": name, "canvas_id": INDEX_ID}, body, text_of(index_path) if old else None)
        if new_text is not None:
            writes[index_path] = new_text
            tally["updated" if old else "created"] += 1

    current_ids = set(plan) | ({INDEX_ID} if index_path else set())
    stale = sorted(path for cid, path in mine.items() if cid not in current_ids)
    pruned: List[str] = []

    canvas_rel = f"{folder}/{name}.canvas"
    if not dry_run:
        for old, new in moves:
            src = os.path.join(root, *old.split("/"))
            if os.path.exists(src):
                os.makedirs(os.path.dirname(os.path.join(root, *new.split("/"))), exist_ok=True)
                os.replace(src, os.path.join(root, *new.split("/")))
        for path, text in writes.items():
            vault._atomic_write(os.path.join(root, *path.split("/")), text)
        if keep_file:
            vault._atomic_write(os.path.join(root, *canvas_rel.split("/")), dumps(doc))
        notes_mod.reindex(owner)
        if prune:
            for path in stale:
                try:
                    notes_mod.delete_note(owner, path)
                    pruned.append(path)
                except FileNotFoundError:
                    pass
    return {
        "ok": True, "name": name, "folder": base, "dry_run": bool(dry_run),
        "canvas_path": canvas_rel if keep_file and not dry_run else None,
        **tally, **counts, "groups": len(groups), "nodes": len(items),
        "index_note": index_path, "stale": stale, "pruned": pruned,
        "written": sorted(writes), "notes": report_notes[:500],
    }


def remove_import(owner: str = "", name: str = "", *, folder: str = "Canvas", purge: bool = False) -> Dict[str, Any]:
    """Undo an import: its notes go to the brain trash (or are deleted for
    good with `purge`), its blocks leave every other note, its file goes."""
    from . import notes as notes_mod

    owner = str(owner or "")
    name = db.safe_filename(name or "", limit=60)
    if not name or name == "untitled":
        raise ValueError("`name` is required")
    folder = str(folder or "Canvas").strip().strip("/") or "Canvas"
    root = vault.vault_root(owner)
    notes_mod.reindex(owner)
    rows, _ = _note_rows(owner)
    removed: List[str] = []
    cleaned: List[str] = []
    marker = f"%% canvas:{name} %%"
    for r in sorted(rows, key=lambda r: r["path"]):
        fmd = db.loads(r["frontmatter"], {}) or {}
        full = os.path.join(root, *r["path"].split("/"))
        if isinstance(fmd, dict) and str(fmd.get("canvas") or "") == name:
            if purge:
                with db.db() as conn:
                    notes_mod._wipe_path(conn, owner, r["path"])
                try:
                    os.remove(full)
                except OSError:
                    pass
            else:
                try:
                    notes_mod.delete_note(owner, r["path"])
                except FileNotFoundError:
                    pass
            removed.append(r["path"])
            continue
        if r["source"]:
            continue
        text = vault._read(full)
        if text and marker in text:
            vault._atomic_write(full, _strip_block(text, name))
            cleaned.append(r["path"])
    canvas_file = os.path.join(root, *folder.split("/"), f"{name}.canvas")
    file_removed = False
    if os.path.exists(canvas_file):
        os.remove(canvas_file)
        file_removed = True
    folder_dir = os.path.join(root, *folder.split("/"), name)
    if os.path.isdir(folder_dir) and not os.listdir(folder_dir):
        os.rmdir(folder_dir)
    notes_mod.reindex(owner)
    return {"ok": True, "name": name, "removed": removed, "cleaned": cleaned,
            "canvas_file_removed": file_removed, "purged": bool(purge)}


__all__ = ["SPEC_VERSION", "validate", "load", "dumps", "export_canvas", "import_canvas", "remove_import"]
