"""src/pizarra.py -- Pizarra, the whiteboard: a JSON Canvas board the person and the agent edit together.

The durable file is a JSON Canvas 1.0 ``.canvas`` (the format the Brain reads and writes, so a board opens in
the Brain and in any JSON Canvas tool). What JSON Canvas has no field for rides in one extra key the format
tolerates:

    node.fx = {"k": "sticky"|"rect"|"ellipse"|"diamond"|"text"|"stroke"|"image", "pts": [[x, y], ...], "sw": 3}

* a sticky / rect / ellipse / diamond / text / stroke is a ``text`` node (its words stay readable elsewhere);
* a frame is a ``group`` node (its ``label`` is the title);
* an image is a ``file`` node pointing at ``assets/<name>`` next to the board;
* a connector is an edge; ``label``, ``fromEnd`` and ``toEnd`` are standard.

Everything below the store is pure: ``apply_ops`` (what ``pizarra_edit`` sends), ``apply_patch`` (what the
person's window sends), the layouts, ``outline`` (what ``pizarra_read`` returns) and ``to_svg``.

Storage: ``DATA_DIR/surfaces/pizarra/<id>/{meta.json, board.canvas, assets/, history/}``. One process owns a
board at a time; every change takes the board's lock, bumps ``rev`` and is written atomically.
"""
from __future__ import annotations

import copy
import html
import json
import math
import os
import re
import secrets
import shutil
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

SPEC_VERSION = "1.0"
KINDS = ("sticky", "text", "rect", "ellipse", "diamond", "frame", "link", "image", "stroke")
SHAPE_KINDS = ("sticky", "text", "rect", "ellipse", "diamond", "stroke")
LAYOUTS = ("grid", "tree", "flow", "timeline", "mindmap")
MAX_NODES = 5000
MAX_TEXT = 4000
MAX_OPS = 400
COORD_LIMIT = 1_000_000

COLORS = {"red": "1", "orange": "2", "yellow": "3", "green": "4", "cyan": "5", "blue": "5", "purple": "6",
          "violet": "6", "pink": "#e879a8", "gray": "#8a8f98", "grey": "#8a8f98", "black": "#2b2d31",
          "white": "#ffffff",
          "rojo": "1", "naranja": "2", "amarillo": "3", "verde": "4", "azul": "5", "morado": "6",
          "rosa": "#e879a8", "gris": "#8a8f98", "negro": "#2b2d31", "blanco": "#ffffff"}
#: Preset colours of JSON Canvas, used when a board is drawn without the Studio (export).
PRESET_HEX = {"1": "#e5484d", "2": "#f76b15", "3": "#e2b714", "4": "#30a46c", "5": "#0090ff", "6": "#8e4ec6"}

DEFAULT_SIZE = {"sticky": (200, 140), "text": (240, 48), "rect": (220, 100), "ellipse": (200, 110),
                "diamond": (200, 130), "frame": (480, 320), "link": (260, 80), "image": (320, 240),
                "stroke": (60, 60)}
GAP = 40
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


class BoardError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def new_id() -> str:
    return secrets.token_hex(8)


def empty_doc() -> Dict[str, Any]:
    return {"nodes": [], "edges": []}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    return max(-COORD_LIMIT, min(COORD_LIMIT, v))


def _int(value: Any, default: int = 0) -> int:
    return int(round(_num(value, default)))


def norm_color(value: Any) -> Optional[str]:
    """A JSON Canvas colour (preset "1".."6" or hex) from a name, preset or hex; None when it is not one."""
    if value is None or value == "":
        return None
    s = str(value).strip().lower()
    if s in COLORS:
        return COLORS[s]
    if s in PRESET_HEX:
        return s
    if _HEX_RE.match(s):
        return s
    return None


def kind_of(node: Dict[str, Any]) -> str:
    fx = node.get("fx") if isinstance(node.get("fx"), dict) else {}
    k = fx.get("k")
    if k in KINDS:
        return k
    t = node.get("type")
    if t == "group":
        return "frame"
    if t == "link":
        return "link"
    if t == "file":
        return "image"
    return "rect"


def box(node: Dict[str, Any]) -> Tuple[int, int, int, int]:
    return (int(node.get("x", 0)), int(node.get("y", 0)), int(node.get("width", 0)), int(node.get("height", 0)))


def _overlap(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int], margin: int = 0) -> bool:
    return not (a[0] + a[2] + margin <= b[0] or b[0] + b[2] + margin <= a[0]
                or a[1] + a[3] + margin <= b[1] or b[1] + b[3] + margin <= a[1])


def bbox_of(nodes: Iterable[Dict[str, Any]]) -> Optional[Tuple[int, int, int, int]]:
    xs0, ys0, xs1, ys1 = [], [], [], []
    for n in nodes:
        x, y, w, h = box(n)
        xs0.append(x); ys0.append(y); xs1.append(x + w); ys1.append(y + h)
    if not xs0:
        return None
    return (min(xs0), min(ys0), max(xs1) - min(xs0), max(ys1) - min(ys0))


def fit_height(kind: str, text: str, width: int, base_h: int) -> int:
    """A height that holds ``text`` at ``width`` (about 8 px a character, 22 px a line)."""
    if kind in ("stroke", "frame", "image"):
        return base_h
    cpl = max(6, int((width - 28) / 8.2))
    lines = 0
    for line in (text or "").split("\n"):
        lines += max(1, math.ceil(len(line) / cpl))
    need = lines * 22 + 30
    if kind == "diamond":
        need = int(need * 1.5)
    elif kind == "ellipse":
        need = int(need * 1.3)
    return max(base_h, need)


def make_node(kind: str, *, id: Optional[str] = None, x: float = 0, y: float = 0, w: Optional[float] = None,
              h: Optional[float] = None, text: str = "", color: Any = None, url: str = "", file: str = "",
              pts: Any = None, sw: Any = None) -> Dict[str, Any]:
    if kind not in KINDS:
        raise BoardError("bad_args", f"unknown kind {kind!r}; use one of {', '.join(KINDS)}")
    bw, bh = DEFAULT_SIZE[kind]
    width = max(24, _int(w, bw)) if w is not None else bw
    text = str(text or "")[:MAX_TEXT]
    height = max(16, _int(h, bh)) if h is not None else fit_height(kind, text, width, bh)
    node: Dict[str, Any] = {"id": id or new_id(), "x": _int(x), "y": _int(y), "width": width, "height": height}
    if kind == "frame":
        node["type"] = "group"
        node["label"] = text
    elif kind == "link":
        node["type"] = "link"
        node["url"] = str(url or text)[:2000]
    elif kind == "image":
        node["type"] = "file"
        node["file"] = str(file)[:500]
    else:
        node["type"] = "text"
        node["text"] = text
        if kind != "rect":
            node["fx"] = {"k": kind}
        if kind == "stroke":
            node["fx"]["pts"] = _clean_pts(pts)
            node["fx"]["sw"] = max(1, min(40, _int(sw, 3)))
    c = norm_color(color)
    if c:
        node["color"] = c
    return node


def _clean_pts(pts: Any) -> List[List[int]]:
    out: List[List[int]] = []
    for p in pts if isinstance(pts, list) else []:
        if isinstance(p, (list, tuple)) and len(p) >= 2 and len(out) < 4000:
            out.append([_int(p[0]), _int(p[1])])
    return out


def node_text(node: Dict[str, Any]) -> str:
    t = node.get("type")
    if t == "group":
        return str(node.get("label") or "")
    if t == "link":
        return str(node.get("url") or "")
    if t == "file":
        return str(node.get("file") or "")
    return str(node.get("text") or "")


def sanitize_node(raw: Any) -> Dict[str, Any]:
    """A node from the person's window or a file: known fields kept, numbers rounded, extras of ``fx`` cleaned."""
    if not isinstance(raw, dict):
        raise BoardError("bad_args", "a node must be an object")
    nid = str(raw.get("id") or "")
    if not _ID_RE.match(nid):
        raise BoardError("bad_args", f"bad node id {nid!r}")
    t = raw.get("type") if raw.get("type") in ("text", "group", "link", "file") else "text"
    node: Dict[str, Any] = {"id": nid, "type": t, "x": _int(raw.get("x")), "y": _int(raw.get("y")),
                            "width": max(8, _int(raw.get("width"), 200)), "height": max(8, _int(raw.get("height"), 100))}
    if t == "text":
        node["text"] = str(raw.get("text") or "")[:MAX_TEXT]
    elif t == "group":
        node["label"] = str(raw.get("label") or "")[:300]
        if isinstance(raw.get("background"), str):
            node["background"] = raw["background"][:500]
    elif t == "link":
        node["url"] = str(raw.get("url") or "")[:2000]
    else:
        node["file"] = str(raw.get("file") or "")[:500]
        if isinstance(raw.get("subpath"), str) and raw["subpath"].startswith("#"):
            node["subpath"] = raw["subpath"][:200]
    c = norm_color(raw.get("color"))
    if c:
        node["color"] = c
    fx = raw.get("fx")
    if isinstance(fx, dict) and fx.get("k") in KINDS:
        clean: Dict[str, Any] = {"k": fx["k"]}
        if fx["k"] == "stroke":
            clean["pts"] = _clean_pts(fx.get("pts"))
            clean["sw"] = max(1, min(40, _int(fx.get("sw"), 3)))
        node["fx"] = clean
    return node


def sanitize_edge(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise BoardError("bad_args", "an edge must be an object")
    eid, a, b = str(raw.get("id") or ""), str(raw.get("fromNode") or ""), str(raw.get("toNode") or "")
    if not (_ID_RE.match(eid) and a and b):
        raise BoardError("bad_args", "an edge needs id, fromNode and toNode")
    edge: Dict[str, Any] = {"id": eid, "fromNode": a, "toNode": b}
    for key in ("fromSide", "toSide"):
        if raw.get(key) in ("top", "right", "bottom", "left"):
            edge[key] = raw[key]
    for key in ("fromEnd", "toEnd"):
        if raw.get(key) in ("none", "arrow"):
            edge[key] = raw[key]
    if isinstance(raw.get("label"), str) and raw["label"]:
        edge["label"] = raw["label"][:200]
    c = norm_color(raw.get("color"))
    if c:
        edge["color"] = c
    return edge


def sanitize_doc(raw: Any) -> Dict[str, Any]:
    """A whole canvas from untrusted JSON (import, file): bad items are dropped, edges to nothing too."""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8-sig")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw.lstrip("﻿"))
        except ValueError as exc:
            raise BoardError("bad_args", f"the canvas is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise BoardError("bad_args", "a canvas is a JSON object with `nodes` and `edges`")
    nodes: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for n in (raw.get("nodes") or [])[:MAX_NODES]:
        try:
            node = sanitize_node(n)
        except BoardError:
            continue
        if node["id"] in seen:
            continue
        seen.add(node["id"])
        nodes.append(node)
    edges: List[Dict[str, Any]] = []
    eseen: Set[str] = set()
    for e in (raw.get("edges") or [])[:MAX_NODES * 2]:
        try:
            edge = sanitize_edge(e)
        except BoardError:
            continue
        if edge["id"] in eseen or edge["id"] in seen or edge["fromNode"] not in seen or edge["toNode"] not in seen:
            continue
        eseen.add(edge["id"])
        edges.append(edge)
    return {"nodes": nodes, "edges": edges}


def dumps(doc: Dict[str, Any]) -> str:
    """The file text, tab-indented like the reference editor writes it."""
    return json.dumps(doc, ensure_ascii=False, indent="\t") + "\n"


def validate(doc: Dict[str, Any]) -> List[str]:
    """Problems against JSON Canvas 1.0, by the Brain's own validator when it can be loaded."""
    try:
        from src.brain.canvas import validate as brain_validate
        return brain_validate(doc)
    except Exception:  # noqa: BLE001 - the Brain's imports are heavy; the board stays usable without it
        return []


# ---------------------------------------------------------------------------
# queries
# ---------------------------------------------------------------------------
def index_nodes(doc: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {n["id"]: n for n in doc.get("nodes", [])}


def frame_members(doc: Dict[str, Any], frame_id: str) -> List[str]:
    """Ids of the nodes whose centre lies inside the frame (a JSON Canvas group contains by geometry)."""
    idx = index_nodes(doc)
    fr = idx.get(frame_id)
    if not fr or kind_of(fr) != "frame":
        return []
    fx, fy, fw, fh = box(fr)
    out = []
    for n in doc["nodes"]:
        if n["id"] == frame_id:
            continue
        x, y, w, h = box(n)
        cx, cy = x + w / 2, y + h / 2
        if fx <= cx <= fx + fw and fy <= cy <= fy + fh:
            out.append(n["id"])
    return out


def counts(doc: Dict[str, Any]) -> Dict[str, int]:
    kinds: Dict[str, int] = {}
    for n in doc["nodes"]:
        k = kind_of(n)
        kinds[k] = kinds.get(k, 0) + 1
    return {"nodes": len(doc["nodes"]), "edges": len(doc["edges"]), "frames": kinds.get("frame", 0),
            "stickies": kinds.get("sticky", 0), "strokes": kinds.get("stroke", 0)}


def _color_label(c: Optional[str]) -> str:
    names = {"1": "red", "2": "orange", "3": "yellow", "4": "green", "5": "blue", "6": "purple"}
    return names.get(c or "", c or "")


def outline(doc: Dict[str, Any], *, title: str = "", board_id: str = "", rev: int = 0, region: Any = None,
            selection: Sequence[str] = (), viewport: Optional[Dict[str, Any]] = None, limit: int = 250) -> str:
    """The compact text ``pizarra_read`` returns: frames with their members, nodes with id, kind, colour, box and
    text, then connectors. ``region`` is a frame id or ``{x, y, w, h}``."""
    idx = index_nodes(doc)
    c = counts(doc)
    lines = [f"Board {title!r} id={board_id} rev={rev}: {c['nodes']} items ({c['frames']} frames, {c['stickies']} stickies, "
             f"{c['strokes']} strokes), {c['edges']} connectors"]
    if viewport:
        lines.append("Person's view: x={x} y={y} zoom={zoom}".format(x=viewport.get("x", 0), y=viewport.get("y", 0),
                                                                    zoom=viewport.get("zoom", 1)))
    sel = [s for s in selection if s in idx]
    if sel:
        lines.append("Person has selected: " + ", ".join(sel[:30]))
    keep: Optional[Set[str]] = None
    if isinstance(region, str) and region in idx:
        keep = set(frame_members(doc, region)) | {region}
    elif isinstance(region, dict):
        rb = (_int(region.get("x")), _int(region.get("y")), max(1, _int(region.get("w"), 1000)), max(1, _int(region.get("h"), 800)))
        keep = {n["id"] for n in doc["nodes"] if _overlap(box(n), rb)}
    shown = [n for n in doc["nodes"] if keep is None or n["id"] in keep]
    members: Dict[str, str] = {}
    for n in shown:
        if kind_of(n) == "frame":
            for m in frame_members(doc, n["id"]):
                members.setdefault(m, n["id"])
    for n in shown[:limit]:
        k = kind_of(n)
        x, y, w, h = box(n)
        txt = node_text(n).replace("\n", " / ")
        if len(txt) > 90:
            txt = txt[:87] + "..."
        col = _color_label(n.get("color"))
        extra = f" in={members[n['id']]}" if n["id"] in members else ""
        if k == "stroke":
            txt = f"{len((n.get('fx') or {}).get('pts') or [])} points"
        lines.append(f"- {n['id']} {k}{' ' + col if col else ''} ({x},{y} {w}x{h}){extra}" + (f" {txt!r}" if txt else ""))
    if len(shown) > limit:
        lines.append(f"... {len(shown) - limit} more items (pass region to narrow)")
    ids = {n["id"] for n in shown}
    es = [e for e in doc["edges"] if e["fromNode"] in ids or e["toNode"] in ids]
    for e in es[:limit]:
        both = " <->" if e.get("fromEnd") == "arrow" else (" --" if e.get("toEnd") == "none" else " ->")
        lines.append(f"= {e['id']}: {e['fromNode']}{both} {e['toNode']}" + (f" {e['label']!r}" if e.get("label") else ""))
    if len(es) > limit:
        lines.append(f"... {len(es) - limit} more connectors")
    if not doc["nodes"]:
        lines.append("(the board is empty)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# free space and layouts
# ---------------------------------------------------------------------------
def free_spot(doc: Dict[str, Any], w: int, h: int, *, anchor: Optional[Tuple[int, int, int, int]] = None,
              view: Optional[Dict[str, Any]] = None, skip: Optional[Set[str]] = None) -> Tuple[int, int]:
    """A top-left for a ``w`` x ``h`` block that touches no existing item: next to ``anchor`` (right, below,
    left, above, then widening rings), else around the person's view centre, else right of the content."""
    skip = skip or set()
    obstacles = [box(n) for n in doc["nodes"] if kind_of(n) != "frame" and n["id"] not in skip]
    if anchor is None and not obstacles:
        if view and view.get("w") and view.get("zoom"):
            z = float(view.get("zoom") or 1)
            return (_int(-view.get("x", 0) / z + (view["w"] / z - w) / 2), _int(-view.get("y", 0) / z + (view["h"] / z - h) / 2))
        return (0, 0)

    def free(x: int, y: int) -> bool:
        return not any(_overlap((x, y, w, h), o, 24) for o in obstacles)

    if anchor is not None:
        ax, ay, aw, ah = anchor
        for (x, y) in ((ax + aw + 80, ay), (ax, ay + ah + 80), (ax - w - 80, ay), (ax, ay - h - 80)):
            if free(x, y):
                return (x, y)
        cx, cy = ax + aw // 2 - w // 2, ay + ah // 2 - h // 2
    elif view and view.get("w") and view.get("zoom"):
        z = float(view.get("zoom") or 1)
        cx, cy = _int(-view.get("x", 0) / z + (view["w"] / z - w) / 2), _int(-view.get("y", 0) / z + (view["h"] / z - h) / 2)
    else:
        bb = bbox_of([n for n in doc["nodes"] if n["id"] not in skip]) or (0, 0, 0, 0)
        cx, cy = bb[0] + bb[2] + 100, bb[1]
        if free(cx, cy):
            return (cx, cy)
    if free(cx, cy):
        return (cx, cy)
    step = 60
    for ring in range(1, 80):
        pts = []
        for dx in range(-ring, ring + 1):
            for dy in range(-ring, ring + 1):
                if max(abs(dx), abs(dy)) == ring:
                    pts.append((cx + dx * step, cy + dy * step))
        pts.sort(key=lambda p: (p[0] - cx) ** 2 + (p[1] - cy) ** 2)
        for (x, y) in pts:
            if free(x, y):
                return (x, y)
    return (cx + 2000, cy)


def _children_map(ids: List[str], edges: List[Dict[str, Any]], directed: bool = True,
                  root: Optional[str] = None) -> Tuple[Dict[str, List[str]], List[str]]:
    """A forest over ``ids`` from the connectors between them (first parent wins, cycles are cut)."""
    idset = set(ids)
    out: Dict[str, List[str]] = {i: [] for i in ids}
    inn: Dict[str, List[str]] = {i: [] for i in ids}
    und: Dict[str, List[str]] = {i: [] for i in ids}
    for e in edges:
        a, b = e["fromNode"], e["toNode"]
        if a in idset and b in idset and a != b:
            out[a].append(b); inn[b].append(a); und[a].append(b); und[b].append(a)
    adj = out if directed else und
    if root is not None:
        roots = [root]
    else:
        roots = [i for i in ids if not inn[i]] if directed else ([ids[0]] if ids else [])
        if not roots and ids:
            roots = [ids[0]]
    children: Dict[str, List[str]] = {i: [] for i in ids}
    seen: Set[str] = set()
    order_roots: List[str] = []
    queue: List[str] = []
    for r in roots:
        if r in seen:
            continue
        seen.add(r); order_roots.append(r); queue.append(r)
        while queue:
            cur = queue.pop(0)
            for nxt in adj[cur]:
                if nxt not in seen:
                    seen.add(nxt); children[cur].append(nxt); queue.append(nxt)
    for i in ids:  # whatever no root reached
        if i not in seen:
            seen.add(i); order_roots.append(i); queue.append(i)
            while queue:
                cur = queue.pop(0)
                for nxt in adj[cur]:
                    if nxt not in seen:
                        seen.add(nxt); children[cur].append(nxt); queue.append(nxt)
    return children, order_roots


def _tidy(children: Dict[str, List[str]], roots: List[str], size: Dict[str, Tuple[int, int]], horizontal: bool,
          gap_main: int = 80, gap_cross: int = 36) -> Dict[str, Tuple[int, int]]:
    """Top-left positions of a forest laid out in layers (left to right when ``horizontal``, else top down)."""
    def msz(i: str) -> int:
        return size[i][0] if horizontal else size[i][1]

    def csz(i: str) -> int:
        return size[i][1] if horizontal else size[i][0]

    ext: Dict[str, int] = {}
    depth: Dict[str, int] = {}

    def measure(n: str, d: int) -> int:
        depth[n] = d
        kids = children[n]
        if not kids:
            ext[n] = csz(n)
        else:
            total = sum(measure(k, d + 1) for k in kids) + gap_cross * (len(kids) - 1)
            ext[n] = max(csz(n), total)
        return ext[n]

    for r in roots:
        measure(r, 0)
    layer_max: Dict[int, int] = {}
    for n, d in depth.items():
        layer_max[d] = max(layer_max.get(d, 0), msz(n))
    layer_off: Dict[int, int] = {}
    acc = 0
    for d in sorted(layer_max):
        layer_off[d] = acc
        acc += layer_max[d] + gap_main
    pos: Dict[str, Tuple[int, int]] = {}

    def place(n: str, start: int) -> None:
        centre = start + ext[n] / 2
        c0 = int(centre - csz(n) / 2)
        m0 = layer_off[depth[n]] + (layer_max[depth[n]] - msz(n)) // 2
        pos[n] = (m0, c0) if horizontal else (c0, m0)
        kids = children[n]
        if kids:
            total = sum(ext[k] for k in kids) + gap_cross * (len(kids) - 1)
            cur = int(centre - total / 2)
            for k in kids:
                place(k, cur)
                cur += ext[k] + gap_cross

    cursor = 0
    for r in roots:
        place(r, cursor)
        cursor += ext[r] + gap_cross * 2
    return pos


def layout_positions(kind: str, ids: List[str], nodes: Dict[str, Dict[str, Any]], edges: List[Dict[str, Any]],
                     *, cols: Optional[int] = None, root: Optional[str] = None) -> Dict[str, Tuple[int, int]]:
    """Positions with the block's top-left at (0, 0) for ``ids`` laid out as ``kind``."""
    if kind not in LAYOUTS:
        raise BoardError("bad_args", f"unknown layout {kind!r}; use one of {', '.join(LAYOUTS)}")
    if not ids:
        return {}
    size = {i: (int(nodes[i]["width"]), int(nodes[i]["height"])) for i in ids}
    pos: Dict[str, Tuple[int, int]] = {}
    has_edges = any(e["fromNode"] in size and e["toNode"] in size for e in edges)
    if kind == "grid" or (kind in ("tree", "flow", "mindmap") and not has_edges and kind != "mindmap"):
        n = len(ids)
        c = max(1, cols or math.ceil(math.sqrt(n)))
        cw = max(s[0] for s in size.values()) + GAP
        rows = [ids[i:i + c] for i in range(0, n, c)]
        y = 0
        for row in rows:
            rh = max(size[i][1] for i in row)
            for j, i in enumerate(row):
                pos[i] = (j * cw, y)
            y += rh + GAP
    elif kind == "timeline":
        x = 0
        base = max(s[1] for s in size.values()) + 30
        for j, i in enumerate(ids):
            w, h = size[i]
            pos[i] = (x, base - h - 24 if j % 2 == 0 else base + 24)
            x += w // 2 + 70 if j % 2 == 0 else w // 2 + 70
            x = max(x, (j + 1) * 90)
        # spread so neighbours on the same side never touch
        for side in (0, 1):
            last = None
            for j, i in enumerate(ids):
                if j % 2 != side:
                    continue
                if last is not None:
                    need = pos[last][0] + size[last][0] + 30
                    if pos[i][0] < need:
                        shift = need - pos[i][0]
                        for k in range(j, len(ids)):
                            pos[ids[k]] = (pos[ids[k]][0] + shift, pos[ids[k]][1])
                last = i
    elif kind in ("tree", "flow"):
        if kind == "tree":
            kids, roots = _children_map(ids, edges, True, root)
            pos = _tidy(kids, roots, size, horizontal=False, gap_main=70, gap_cross=30)
        else:
            pos = _flow_layers(ids, edges, size)
    elif kind == "mindmap":
        pos = _mindmap(ids, edges, size, root)
    if not pos:
        return {}
    minx = min(p[0] for p in pos.values()); miny = min(p[1] for p in pos.values())
    return {i: (p[0] - minx, p[1] - miny) for i, p in pos.items()}


def _flow_layers(ids: List[str], edges: List[Dict[str, Any]], size: Dict[str, Tuple[int, int]]) -> Dict[str, Tuple[int, int]]:
    idset = set(ids)
    succ: Dict[str, List[str]] = {i: [] for i in ids}
    indeg = {i: 0 for i in ids}
    for e in edges:
        a, b = e["fromNode"], e["toNode"]
        if a in idset and b in idset and a != b and b not in succ[a]:
            succ[a].append(b); indeg[b] += 1
    layer = {i: 0 for i in ids}
    queue = [i for i in ids if indeg[i] == 0]
    done: Set[str] = set()
    deg = dict(indeg)
    while queue:
        cur = queue.pop(0)
        done.add(cur)
        for nxt in succ[cur]:
            layer[nxt] = max(layer[nxt], layer[cur] + 1)
            deg[nxt] -= 1
            if deg[nxt] == 0:
                queue.append(nxt)
    top = max(layer.values()) if layer else 0
    for i in ids:  # nodes in a cycle go after everything else
        if i not in done:
            layer[i] = top + 1
    groups: Dict[int, List[str]] = {}
    for i in ids:
        groups.setdefault(layer[i], []).append(i)
    pos: Dict[str, Tuple[int, int]] = {}
    x = 0
    heights = {l: sum(size[i][1] for i in g) + 36 * (len(g) - 1) for l, g in groups.items()}
    tallest = max(heights.values())
    for l in sorted(groups):
        g = groups[l]
        y = (tallest - heights[l]) // 2
        for i in g:
            pos[i] = (x, y)
            y += size[i][1] + 36
        x += max(size[i][0] for i in g) + 90
    return pos


def _mindmap(ids: List[str], edges: List[Dict[str, Any]], size: Dict[str, Tuple[int, int]], root: Optional[str]) -> Dict[str, Tuple[int, int]]:
    idset = set(ids)
    if root not in idset:
        deg = {i: 0 for i in ids}
        for e in edges:
            if e["fromNode"] in idset and e["toNode"] in idset:
                deg[e["fromNode"]] += 2
                deg[e["toNode"]] += 1
        root = max(ids, key=lambda i: (deg[i], -ids.index(i))) if any(deg.values()) else ids[0]
    kids, roots = _children_map(ids, edges, False, root)
    top = [r for r in roots]
    # the branches hang from `root`; stray components join as extra branches
    branches = list(kids[root]) + [r for r in top if r != root]
    for r in top:
        if r != root and r not in kids[root]:
            kids[root].append(r)
    right: List[str] = []
    left: List[str] = []
    weight = {}

    def count(n: str) -> int:
        return 1 + sum(count(k) for k in kids[n])
    tot_r = tot_l = 0
    for b in sorted(kids[root], key=lambda b: -count(b)):
        w = count(b)
        if tot_r <= tot_l:
            right.append(b); tot_r += w
        else:
            left.append(b); tot_l += w
    order = {b: ids.index(b) for b in kids[root]}
    right.sort(key=order.get); left.sort(key=order.get)

    def side(branch: List[str], mirror: bool) -> Dict[str, Tuple[int, int]]:
        if not branch:
            return {}
        sub = {i: list(kids[i]) for i in ids}
        sub["__root__"] = list(branch)
        sz = dict(size)
        sz["__root__"] = (size[root][0], size[root][1])
        p = _tidy(sub, ["__root__"], sz, horizontal=True, gap_main=90, gap_cross=28)
        rx, ry = p.pop("__root__")
        rw = size[root][0]
        out = {}
        for i, (x, y) in p.items():
            out[i] = ((rw - (x - rx) - size[i][0]) if mirror else (x - rx), y - ry)
        return out

    pos: Dict[str, Tuple[int, int]] = {root: (0, 0)}
    pos.update(side(right, False))
    pos.update(side(left, True))
    return pos


# ---------------------------------------------------------------------------
# the ops engine (pizarra_edit)
# ---------------------------------------------------------------------------
class Changes:
    """What a batch of ops (or a patch) did, in the form that is sent to the other windows."""

    def __init__(self) -> None:
        self.nodes: Dict[str, Optional[Dict[str, Any]]] = {}
        self.edges: Dict[str, Optional[Dict[str, Any]]] = {}
        self.order = False
        self.cursor: List[Dict[str, Any]] = []

    def node(self, n: Dict[str, Any]) -> None:
        self.nodes[n["id"]] = n

    def edge(self, e: Dict[str, Any]) -> None:
        self.edges[e["id"]] = e

    def gone_node(self, nid: str) -> None:
        self.nodes[nid] = None

    def gone_edge(self, eid: str) -> None:
        self.edges[eid] = None

    def empty(self) -> bool:
        return not (self.nodes or self.edges or self.order)

    def patch(self, doc: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {"nodes": {}, "edges": {}}
        idx = index_nodes(doc)
        eidx = {e["id"]: e for e in doc["edges"]}
        for nid in self.nodes:
            out["nodes"][nid] = copy.deepcopy(idx.get(nid))
        for eid in self.edges:
            out["edges"][eid] = copy.deepcopy(eidx.get(eid))
        if self.order:
            out["order"] = [n["id"] for n in doc["nodes"]]
        return out


def _ids_of(op: Dict[str, Any]) -> List[str]:
    ids = op.get("ids")
    if isinstance(ids, list):
        return [str(i) for i in ids]
    if op.get("id") is not None:
        return [str(op["id"])]
    return []


def _get(idx: Dict[str, Dict[str, Any]], nid: Any, what: str = "item") -> Dict[str, Any]:
    n = idx.get(str(nid))
    if n is None:
        raise BoardError("not_found", f"no {what} with id {nid!r}")
    return n


def apply_ops(doc: Dict[str, Any], ops: Any, *, view: Optional[Dict[str, Any]] = None) -> Tuple[List[str], Changes]:
    """Apply a batch of ops to ``doc`` (mutated in place). Returns one line per op and the changes. An op that
    fails stops the batch with a ``BoardError`` that names it; what ran before stays applied (the caller
    decides whether to keep the doc, ``pizarra_edit`` keeps it and says so)."""
    if not isinstance(ops, list) or not ops:
        raise BoardError("bad_args", "`ops` must be a non-empty list")
    if len(ops) > MAX_OPS:
        raise BoardError("bad_args", f"at most {MAX_OPS} ops per call")
    ch = Changes()
    results: List[str] = []
    unplaced: List[str] = []
    anchors: Dict[str, Optional[str]] = {}
    declared: Dict[str, str] = {}          # new item id -> the frame it was declared to live in (`frame` / `in`)
    inner_layout: Dict[str, str] = {}      # frame id -> how its items are laid out inside it
    for n, op in enumerate(ops):
        if not isinstance(op, dict):
            raise BoardError("bad_args", f"op {n} is not an object")
        name = str(op.get("op") or op.get("action") or "").lower()
        before = set(index_nodes(doc))
        try:
            if name == "layout":
                _settle_frames(doc, declared, inner_layout, unplaced, ch)
            msg = _run_op(doc, name, op, ch, unplaced, anchors, view)
            fid = op.get("frame") if name in ("add", "outline") else None
            if name == "add" and op.get("in") is not None:
                fid = op.get("in")
            if fid is not None:
                new = [i for i in index_nodes(doc) if i not in before]
                if str(fid) not in index_nodes(doc) or kind_of(index_nodes(doc)[str(fid)]) != "frame":
                    raise BoardError("not_found", f"no frame with id {fid!r} (create it first with kind=frame)")
                for i in new:
                    if kind_of(index_nodes(doc)[i]) != "frame":
                        declared[i] = str(fid)
            if name == "add" and str(op.get("kind") or op.get("type") or "") == "frame" and op.get("layout"):
                inner_layout[[i for i in index_nodes(doc) if i not in before][0]] = str(op["layout"]).lower()
            msg += _ignored_fields(name, op)
        except BoardError as exc:
            exc.message = f"op {n} ({name or '?'}): {exc.message}"
            exc.results = results  # type: ignore[attr-defined]
            exc.changes = ch  # type: ignore[attr-defined]
            _settle_frames(doc, declared, inner_layout, unplaced, ch)
            _place_unplaced(doc, unplaced, anchors, ch, view)
            raise
        results.append(msg)
    _settle_frames(doc, declared, inner_layout, unplaced, ch)
    _place_unplaced(doc, unplaced, anchors, ch, view)
    if len(doc["nodes"]) > MAX_NODES:
        raise BoardError("too_big", f"a board holds at most {MAX_NODES} items")
    return results, ch


_COMMON_OP_KEYS = {"op", "action"}
_OP_FIELDS: Dict[str, Set[str]] = {
    "add": {"kind", "type", "id", "x", "y", "w", "width", "h", "height", "text", "label", "color", "url", "file", "points",
            "width_px", "near", "frame", "in", "layout"},
    "update": {"id", "ids", "text", "label", "color", "x", "y", "w", "width", "h", "height", "kind"},
    "move": {"id", "ids", "x", "y", "dx", "dy", "with_content"},
    "delete": {"id", "ids"},
    "connect": {"from", "to", "fromNode", "toNode", "id", "arrow", "label", "color", "from_side", "to_side"},
    "group": {"id", "ids", "title", "text", "label", "color", "x", "y", "w", "h"},
    "layout": {"layout", "kind", "frame", "ids", "id", "all", "cols", "root", "x", "y", "near"},
    "align": {"id", "ids", "how"},
    "order": {"id", "ids", "to"},
    "outline": {"items", "layout", "kind", "root_kind", "parent", "near", "cols", "frame_title", "frame"},
}
_OP_ALIASES = {"link": "connect", "edge": "connect", "frame": "group", "z": "order"}


def _ignored_fields(name: str, op: Dict[str, Any]) -> str:
    """A note for the result line when an op carried fields it does not use, with the ones it does."""
    accepted = _OP_FIELDS.get(_OP_ALIASES.get(name, name))
    if accepted is None:
        return ""
    extra = sorted(k for k in op if k not in accepted and k not in _COMMON_OP_KEYS)
    if not extra:
        return ""
    return f" [ignored fields: {', '.join(extra)}; {name} accepts: {', '.join(sorted(accepted))}]"


PAD, HEAD = 40, 64


def _inner_kind(kind: str, ids: List[str], edges: List[Dict[str, Any]]) -> str:
    inside = set(ids)
    has = any(e["fromNode"] in inside and e["toNode"] in inside for e in edges)
    return kind if has and kind in LAYOUTS else "grid"


def _fill_frame(doc: Dict[str, Any], frame: Dict[str, Any], ids: List[str], kind: str, ch: Changes,
                unplaced: List[str], at: Optional[Tuple[int, int]] = None) -> None:
    """Lay ``ids`` out inside ``frame`` (top-left padded), then size the frame to hold them."""
    idx = index_nodes(doc)
    members = {i: idx[i] for i in ids}
    rel = layout_positions(_inner_kind(kind, ids, doc["edges"]), ids, members, doc["edges"])
    bw = max(rel[i][0] + members[i]["width"] for i in ids)
    bh = max(rel[i][1] + members[i]["height"] for i in ids)
    fw, fh = max(240, bw + 2 * PAD), max(160, bh + HEAD + PAD)
    if at is not None:
        frame["x"], frame["y"] = at
    frame["width"], frame["height"] = fw, fh
    for i in ids:
        members[i]["x"], members[i]["y"] = frame["x"] + PAD + rel[i][0], frame["y"] + HEAD + rel[i][1]
        ch.node(members[i])
        if i in unplaced:
            unplaced.remove(i)
    if frame["id"] in unplaced:
        unplaced.remove(frame["id"])
    ch.node(frame)
    ch.cursor.append({"x": frame["x"] + fw // 2, "y": frame["y"] + 20, "id": frame["id"]})


def _settle_frames(doc: Dict[str, Any], declared: Dict[str, str], inner_layout: Dict[str, str], unplaced: List[str],
                   ch: Changes) -> None:
    """Items declared to live in a frame are laid out inside it and the frame is sized to them; a frame that was
    not placed yet goes to the right of the other frames. Membership afterwards is plain geometry."""
    if not declared:
        return
    idx = index_nodes(doc)
    by_frame: Dict[str, List[str]] = {}
    for nid, fid in declared.items():
        if nid in idx and fid in idx:
            by_frame.setdefault(fid, []).append(nid)
    declared.clear()
    claimed = {i for mem in by_frame.values() for i in mem}
    for fid, mem in by_frame.items():
        frame = idx[fid]
        already = [m for m in frame_members(doc, fid)
                   if m not in claimed and m not in unplaced and kind_of(idx[m]) not in ("frame", "stroke")]
        ids = already + mem
        at = None
        if fid in unplaced:
            others = [n for n in doc["nodes"] if kind_of(n) == "frame" and n["id"] != fid and n["id"] not in unplaced]
            if others:
                bb = bbox_of(others)
                at = (bb[0] + bb[2] + 80, bb[1])
            else:
                free = [n for n in doc["nodes"] if n["id"] not in set(ids) | {fid} and n["id"] not in unplaced]
                bb = bbox_of(free) if free else None
                at = (bb[0] + bb[2] + 80, bb[1]) if bb else (0, 0)
        _fill_frame(doc, frame, ids, inner_layout.get(fid, "grid"), ch, unplaced, at)


def _layout_board(doc: Dict[str, Any], kind: str, ch: Changes, unplaced: List[str], cols: Optional[int] = None,
                  root: Optional[str] = None) -> str:
    """The whole board: what is inside each frame is laid out inside it, the frame is sized to fit, and the frames
    (and the items that are in none) are laid out among themselves; connectors between items of two frames count
    as a connector between the frames."""
    idx = index_nodes(doc)
    frames = [n for n in doc["nodes"] if kind_of(n) == "frame"]
    frames.sort(key=lambda n: (n["x"], n["y"]))
    owner: Dict[str, str] = {}
    for n in doc["nodes"]:
        if kind_of(n) == "frame":
            continue
        cx, cy = n["x"] + n["width"] / 2, n["y"] + n["height"] / 2
        best = None
        for f in frames:
            if f["x"] <= cx <= f["x"] + f["width"] and f["y"] <= cy <= f["y"] + f["height"]:
                if best is None or f["width"] * f["height"] < best["width"] * best["height"]:
                    best = f
        if best is not None:
            owner[n["id"]] = best["id"]
    free = [n["id"] for n in doc["nodes"] if kind_of(n) not in ("frame", "stroke") and n["id"] not in owner]
    strokes_in = [n["id"] for n in doc["nodes"] if kind_of(n) == "stroke" and n["id"] in owner]
    if not frames and not free:
        raise BoardError("bad_args", "the board has nothing to lay out")
    everything = [n for n in doc["nodes"] if kind_of(n) != "stroke" or n["id"] in owner]
    bb = bbox_of(everything) or (0, 0, 0, 0)
    inner: Dict[str, Dict[str, Tuple[int, int]]] = {}
    size: Dict[str, Tuple[int, int]] = {}
    unit_node: Dict[str, Dict[str, Any]] = {}
    for f in frames:
        mem = [i for i, o in owner.items() if o == f["id"] and kind_of(idx[i]) != "stroke"]
        if mem:
            rel = layout_positions(_inner_kind(kind, mem, doc["edges"]), mem, {i: idx[i] for i in mem}, doc["edges"], cols=cols, root=root)
            bw = max(rel[i][0] + idx[i]["width"] for i in mem)
            bh = max(rel[i][1] + idx[i]["height"] for i in mem)
            inner[f["id"]] = rel
            size[f["id"]] = (max(240, bw + 2 * PAD), max(160, bh + HEAD + PAD))
        else:
            size[f["id"]] = (f["width"], f["height"])
        unit_node[f["id"]] = {"width": size[f["id"]][0], "height": size[f["id"]][1]}
    for i in free:
        unit_node[i] = idx[i]
    units = [f["id"] for f in frames] + free
    uedges: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, str]] = set()
    for e in doc["edges"]:
        a, b = owner.get(e["fromNode"], e["fromNode"]), owner.get(e["toNode"], e["toNode"])
        if a != b and a in unit_node and b in unit_node and (a, b) not in seen:
            seen.add((a, b)); uedges.append({"id": f"u{len(uedges)}", "fromNode": a, "toNode": b})
    if uedges or kind == "grid":
        pos = layout_positions(kind, units, unit_node, uedges, cols=cols, root=root)
    else:                                   # no connectors between the blocks: a row (a column for a tree)
        pos = layout_positions("grid", units, unit_node, [], cols=1 if kind == "tree" else len(units))
    for f in frames:
        fx, fy = bb[0] + pos[f["id"]][0], bb[1] + pos[f["id"]][1]
        f["x"], f["y"] = fx, fy
        f["width"], f["height"] = size[f["id"]]
        ch.node(f)
        for i, (rx, ry) in inner.get(f["id"], {}).items():
            idx[i]["x"], idx[i]["y"] = fx + PAD + rx, fy + HEAD + ry
            ch.node(idx[i])
    for i in free:
        idx[i]["x"], idx[i]["y"] = bb[0] + pos[i][0], bb[1] + pos[i][1]
        ch.node(idx[i])
    for i in set(free) | {m for rel in inner.values() for m in rel}:
        if i in unplaced:
            unplaced.remove(i)
    for f in frames:
        if f["id"] in unplaced:
            unplaced.remove(f["id"])
    first = frames[0] if frames else idx[free[0]]
    ch.cursor.append({"x": first["x"] + first["width"] // 2, "y": first["y"] + 20, "id": first["id"]})
    ch.order = True
    return f"laid out the board as {kind}: {len(frames)} frames, {len(owner)} items inside them, {len(free)} loose"


def _place_unplaced(doc: Dict[str, Any], unplaced: List[str], anchors: Dict[str, Optional[str]], ch: Changes,
                    view: Optional[Dict[str, Any]]) -> None:
    ids = [i for i in unplaced if i in index_nodes(doc)]
    unplaced.clear()
    if not ids:
        return
    idx = index_nodes(doc)
    rel = layout_positions("grid", ids, idx, [])
    bw = max(rel[i][0] + idx[i]["width"] for i in ids)
    bh = max(rel[i][1] + idx[i]["height"] for i in ids)
    anchor_id = next((anchors.get(i) for i in ids if anchors.get(i)), None)
    anchor = box(idx[anchor_id]) if anchor_id and anchor_id in idx else None
    x0, y0 = free_spot(doc, bw, bh, anchor=anchor, view=view, skip=set(ids))
    for i in ids:
        idx[i]["x"], idx[i]["y"] = x0 + rel[i][0], y0 + rel[i][1]
        ch.node(idx[i])
        ch.cursor.append({"x": idx[i]["x"] + idx[i]["width"] // 2, "y": idx[i]["y"] + idx[i]["height"] // 2, "id": i})


def _run_op(doc, name, op, ch: Changes, unplaced: List[str], anchors, view) -> str:
    idx = index_nodes(doc)
    if name == "add":
        kind = str(op.get("kind") or op.get("type") or "sticky").lower()
        if kind == "note":
            kind = "sticky"
        if kind in ("shape", "box", "card"):
            kind = "rect"
        if kind == "circle":
            kind = "ellipse"
        nid = str(op["id"]) if op.get("id") is not None else new_id()
        if not _ID_RE.match(nid):
            raise BoardError("bad_args", f"bad id {nid!r} (letters, digits, - and _)")
        if nid in idx or any(e["id"] == nid for e in doc["edges"]):
            raise BoardError("bad_args", f"id {nid!r} is already used")
        node = make_node(kind, id=nid, x=op.get("x", 0), y=op.get("y", 0), w=op.get("w", op.get("width")),
                         h=op.get("h", op.get("height")), text=op.get("text", op.get("label", "")),
                         color=op.get("color"), url=op.get("url", ""), file=op.get("file", ""),
                         pts=op.get("points"), sw=op.get("width_px"))
        doc["nodes"].append(node)
        idx[nid] = node
        ch.node(node); ch.order = True
        if op.get("x") is None or op.get("y") is None:
            anchors[nid] = str(op["near"]) if op.get("near") is not None else None
            unplaced.append(nid)
        else:
            ch.cursor.append({"x": node["x"] + node["width"] // 2, "y": node["y"] + node["height"] // 2, "id": nid})
        return f"added {kind} {nid}"
    if name == "update":
        ids = _ids_of(op)
        if not ids:
            raise BoardError("bad_args", "`id` is required")
        for nid in ids:
            n = _get(idx, nid)
            kind = kind_of(n)
            if "text" in op or "label" in op:
                txt = str(op.get("text", op.get("label")) or "")[:MAX_TEXT]
                if n["type"] == "group":
                    n["label"] = txt
                elif n["type"] == "link":
                    n["url"] = txt
                elif n["type"] == "text":
                    n["text"] = txt
                    if op.get("h") is None and op.get("height") is None:
                        n["height"] = max(n["height"], fit_height(kind, txt, n["width"], DEFAULT_SIZE[kind][1]))
            if "color" in op:
                c = norm_color(op["color"])
                if c:
                    n["color"] = c
                else:
                    n.pop("color", None)
            for src, dst in (("x", "x"), ("y", "y")):
                if op.get(src) is not None:
                    n[dst] = _int(op[src])
            for src in ("w", "width"):
                if op.get(src) is not None:
                    n["width"] = max(24, _int(op[src]))
            for src in ("h", "height"):
                if op.get(src) is not None:
                    n["height"] = max(16, _int(op[src]))
            if op.get("kind") and n["type"] == "text" and str(op["kind"]) in SHAPE_KINDS and str(op["kind"]) != "stroke":
                k = str(op["kind"])
                if k == "rect":
                    n.pop("fx", None)
                else:
                    n["fx"] = {"k": k}
            ch.node(n)
            ch.cursor.append({"x": n["x"] + n["width"] // 2, "y": n["y"] + n["height"] // 2, "id": nid})
        return f"updated {', '.join(ids[:6])}" + ("..." if len(ids) > 6 else "")
    if name == "move":
        ids = _ids_of(op)
        if not ids:
            raise BoardError("bad_args", "`id` or `ids` is required")
        nodes = [_get(idx, i) for i in ids]
        moved = set(ids)
        if op.get("dx") is not None or op.get("dy") is not None:
            dx, dy = _int(op.get("dx")), _int(op.get("dy"))
        elif op.get("x") is not None or op.get("y") is not None:
            bb = bbox_of(nodes)
            dx = _int(op["x"]) - bb[0] if op.get("x") is not None else 0
            dy = _int(op["y"]) - bb[1] if op.get("y") is not None else 0
        else:
            raise BoardError("bad_args", "give `x`/`y` (new top-left) or `dx`/`dy`")
        for n in nodes:
            n["x"] += dx; n["y"] += dy
            ch.node(n)
            if n["type"] == "group" and op.get("with_content", True):  # a frame carries what is inside it
                for m in frame_members(doc, n["id"]):
                    if m not in moved:
                        moved.add(m)
                        idx[m]["x"] += dx; idx[m]["y"] += dy
                        ch.node(idx[m])
        c = nodes[0]
        ch.cursor.append({"x": c["x"] + c["width"] // 2, "y": c["y"] + c["height"] // 2, "id": c["id"]})
        return f"moved {len(moved)} items by ({dx},{dy})"
    if name == "delete":
        ids = _ids_of(op)
        if not ids:
            raise BoardError("bad_args", "`id` or `ids` is required")
        eids = [i for i in ids if any(e["id"] == i for e in doc["edges"])]
        nids = [i for i in ids if i in idx]
        missing = [i for i in ids if i not in idx and i not in eids]
        if missing:
            raise BoardError("not_found", f"no item with id {missing[0]!r}")
        for i in nids:
            n = idx.pop(i)
            ch.cursor.append({"x": n["x"] + n["width"] // 2, "y": n["y"] + n["height"] // 2, "id": i})
            ch.gone_node(i)
        doc["nodes"] = [n for n in doc["nodes"] if n["id"] in idx]
        gone_e = set(eids)
        for e in doc["edges"]:
            if e["fromNode"] not in idx or e["toNode"] not in idx:
                gone_e.add(e["id"])
        for e in list(doc["edges"]):
            if e["id"] in gone_e:
                ch.gone_edge(e["id"])
        doc["edges"] = [e for e in doc["edges"] if e["id"] not in gone_e]
        ch.order = True
        return f"deleted {len(nids)} items and {len(gone_e)} connectors"
    if name in ("connect", "link", "edge"):
        a, b = _get(idx, op.get("from") or op.get("fromNode")), _get(idx, op.get("to") or op.get("toNode"))
        if a["id"] == b["id"]:
            raise BoardError("bad_args", "a connector needs two different items")
        eid = str(op["id"]) if op.get("id") is not None else new_id()
        if not _ID_RE.match(eid) or eid in idx or any(e["id"] == eid for e in doc["edges"]):
            raise BoardError("bad_args", f"bad or used id {eid!r}")
        edge: Dict[str, Any] = {"id": eid, "fromNode": a["id"], "toNode": b["id"]}
        arrow = str(op.get("arrow") or "end").lower()
        edge["fromEnd"] = "arrow" if arrow in ("both", "start") else "none"
        edge["toEnd"] = "none" if arrow in ("none", "start") else "arrow"
        if op.get("label"):
            edge["label"] = str(op["label"])[:200]
        c = norm_color(op.get("color"))
        if c:
            edge["color"] = c
        for key, nk in (("from_side", "fromSide"), ("to_side", "toSide")):
            if op.get(key) in ("top", "right", "bottom", "left"):
                edge[nk] = op[key]
        doc["edges"].append(edge)
        ch.edge(edge)
        return f"connected {a['id']} -> {b['id']}"
    if name in ("group", "frame"):
        ids = _ids_of(op)
        members = [_get(idx, i) for i in ids]
        title = str(op.get("title", op.get("text", op.get("label", ""))) or "")[:300]
        pad, head = 40, 56
        if members:
            bb = bbox_of(members)
            x, y, w, h = bb[0] - pad, bb[1] - pad - head + 24, bb[2] + 2 * pad, bb[3] + 2 * pad + head - 24
        else:
            x, y = _int(op.get("x")), _int(op.get("y"))
            w, h = _int(op.get("w"), DEFAULT_SIZE["frame"][0]), _int(op.get("h"), DEFAULT_SIZE["frame"][1])
            if op.get("x") is None:
                x, y = free_spot(doc, w, h, view=view)
        nid = str(op["id"]) if op.get("id") is not None else new_id()
        if not _ID_RE.match(nid) or nid in idx:
            raise BoardError("bad_args", f"bad or used id {nid!r}")
        node = make_node("frame", id=nid, x=x, y=y, w=w, h=h, text=title, color=op.get("color"))
        doc["nodes"].insert(0, node)  # a frame sits behind what it holds
        ch.node(node); ch.order = True
        ch.cursor.append({"x": x + w // 2, "y": y + 20, "id": nid})
        return f"frame {nid} around {len(members)} items"
    if name == "layout":
        return _op_layout(doc, op, idx, ch, unplaced, view)
    if name == "align":
        ids = _ids_of(op)
        nodes = [_get(idx, i) for i in ids]
        how = str(op.get("how") or "left")
        _align(nodes, how)
        for n in nodes:
            ch.node(n)
        ch.cursor.append({"x": nodes[0]["x"] + nodes[0]["width"] // 2, "y": nodes[0]["y"] + nodes[0]["height"] // 2, "id": nodes[0]["id"]})
        return f"aligned {len(nodes)} items ({how})"
    if name in ("order", "z"):
        ids = _ids_of(op)
        nodes = [_get(idx, i) for i in ids]
        to = str(op.get("to") or "front")
        rest = [n for n in doc["nodes"] if n["id"] not in set(ids)]
        doc["nodes"] = (nodes + rest) if to == "back" else (rest + nodes)
        ch.order = True
        return f"moved {len(ids)} items to the {to}"
    if name == "outline":
        return _op_outline(doc, op, idx, ch, unplaced, anchors, view)
    raise BoardError("bad_args", f"unknown op {name!r}; use add, update, move, delete, connect, group, layout, align, order or outline")


def _align(nodes: List[Dict[str, Any]], how: str) -> None:
    if len(nodes) < 2 and not how.startswith("distribute"):
        return
    bb = bbox_of(nodes)
    if how == "left":
        for n in nodes: n["x"] = bb[0]
    elif how == "right":
        for n in nodes: n["x"] = bb[0] + bb[2] - n["width"]
    elif how in ("center", "centre"):
        for n in nodes: n["x"] = bb[0] + (bb[2] - n["width"]) // 2
    elif how == "top":
        for n in nodes: n["y"] = bb[1]
    elif how == "bottom":
        for n in nodes: n["y"] = bb[1] + bb[3] - n["height"]
    elif how == "middle":
        for n in nodes: n["y"] = bb[1] + (bb[3] - n["height"]) // 2
    elif how in ("distribute-h", "distribute_h", "distribute-x") and len(nodes) > 2:
        ns = sorted(nodes, key=lambda n: n["x"])
        gap = (bb[2] - sum(n["width"] for n in ns)) / (len(ns) - 1)
        cur = bb[0]
        for n in ns:
            n["x"] = int(round(cur)); cur += n["width"] + gap
    elif how in ("distribute-v", "distribute_v", "distribute-y") and len(nodes) > 2:
        ns = sorted(nodes, key=lambda n: n["y"])
        gap = (bb[3] - sum(n["height"] for n in ns)) / (len(ns) - 1)
        cur = bb[1]
        for n in ns:
            n["y"] = int(round(cur)); cur += n["height"] + gap
    elif not how.startswith("distribute"):
        raise BoardError("bad_args", "`how` is left, center, right, top, middle, bottom, distribute-h or distribute-v")


def _op_layout(doc, op, idx, ch: Changes, unplaced: List[str], view) -> str:
    kind = str(op.get("layout") or op.get("kind") or "grid").lower()
    frame_id = op.get("frame")
    ids = _ids_of(op)
    frame = None
    if frame_id is None and (op.get("all") or not ids):       # nothing named: the whole board, frames included
        if kind not in LAYOUTS:
            raise BoardError("bad_args", f"unknown layout {kind!r}; use one of {', '.join(LAYOUTS)}")
        return _layout_board(doc, kind, ch, unplaced, cols=_int(op["cols"]) if op.get("cols") else None,
                             root=str(op["root"]) if op.get("root") else None)
    if frame_id is not None:
        frame = _get(idx, frame_id, "frame")
        ids = [i for i in frame_members(doc, frame["id"]) if kind_of(idx[i]) != "stroke"]
    if not ids:
        raise BoardError("bad_args", "give `ids` (or a `frame`) to lay out")
    nodes = {i: _get(idx, i) for i in ids}
    ids = [i for i in ids if kind_of(nodes[i]) != "frame" or frame is None]
    if kind == "timeline" or kind == "grid":
        pass
    rel = layout_positions(kind, ids, {i: nodes[i] for i in ids}, doc["edges"],
                           cols=_int(op["cols"]) if op.get("cols") else None, root=str(op["root"]) if op.get("root") else None)
    bw = max(rel[i][0] + nodes[i]["width"] for i in ids)
    bh = max(rel[i][1] + nodes[i]["height"] for i in ids)
    fresh = all(i in unplaced for i in ids)
    if op.get("x") is not None and op.get("y") is not None:
        x0, y0 = _int(op["x"]), _int(op["y"])
    elif frame is not None:
        x0, y0 = frame["x"] + PAD, frame["y"] + HEAD
    elif fresh:
        anc = op.get("near")
        x0, y0 = free_spot(doc, bw, bh, anchor=box(idx[str(anc)]) if anc is not None and str(anc) in idx else None, view=view, skip=set(ids))
    else:
        bb = bbox_of([nodes[i] for i in ids])
        x0, y0 = bb[0], bb[1]
    for i in ids:
        nodes[i]["x"], nodes[i]["y"] = x0 + rel[i][0], y0 + rel[i][1]
        ch.node(nodes[i])
        if i in unplaced:
            unplaced.remove(i)
    if frame is not None:
        frame["x"], frame["y"] = x0 - PAD, y0 - HEAD
        frame["width"], frame["height"] = max(240, bw + 2 * PAD), max(160, bh + HEAD + PAD)
        ch.node(frame)
    n0 = nodes[ids[0]]
    ch.cursor.append({"x": n0["x"] + n0["width"] // 2, "y": n0["y"] + n0["height"] // 2, "id": n0["id"]})
    return f"laid out {len(ids)} items as {kind}"


def _op_outline(doc, op, idx, ch: Changes, unplaced, anchors, view) -> str:
    """``{"op": "outline", "items": [{"text": "Launch", "children": [...]}], "layout": "mindmap", "kind": "sticky"}``:
    builds the nodes and connectors of a nested outline and lays them out in one go."""
    items = op.get("items")
    if not isinstance(items, list) or not items:
        raise BoardError("bad_args", "`items` must be a non-empty list of {text, children?, kind?, color?, label?}")
    layout = str(op.get("layout") or "tree").lower()
    if layout not in LAYOUTS:
        raise BoardError("bad_args", f"unknown layout {layout!r}")
    default_kind = str(op.get("kind") or "sticky")
    made: List[str] = []
    new_edges: List[Dict[str, Any]] = []
    root_of_outline: List[Optional[str]] = [None]

    def build(item: Any, parent: Optional[str], depth: int) -> None:
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            raise BoardError("bad_args", "an outline item is a string or an object")
        if len(idx) + len(made) > MAX_NODES:
            raise BoardError("too_big", f"a board holds at most {MAX_NODES} items")
        kind = str(item.get("kind") or (op.get("root_kind") if depth == 0 and op.get("root_kind") else default_kind))
        nid = str(item["id"]) if item.get("id") is not None else new_id()
        if not _ID_RE.match(nid) or nid in idx:
            raise BoardError("bad_args", f"bad or used id {nid!r}")
        txt = str(item.get("text", "") or "")
        iw = item.get("w") or DEFAULT_SIZE.get(kind, (200, 100))[0]
        ih = item.get("h") or fit_height(kind, txt, int(iw), 84 if kind in ("sticky", "rect") else DEFAULT_SIZE.get(kind, (200, 100))[1])
        node = make_node(kind, id=nid, text=txt, color=item.get("color"), w=iw, h=ih)
        doc["nodes"].append(node); idx[nid] = node; made.append(nid)
        ch.node(node)
        if parent:
            e = {"id": new_id(), "fromNode": parent, "toNode": nid, "toEnd": "arrow"}
            if item.get("label"):
                e["label"] = str(item["label"])[:200]
            doc["edges"].append(e); new_edges.append(e); ch.edge(e)
        for child in item.get("children") or []:
            build(child, nid, depth + 1)

    for it in items:
        build(it, str(op["parent"]) if op.get("parent") is not None else None, 0)
    ch.order = True
    if layout == "timeline" or layout == "grid":
        lay = {"op": "layout", "ids": made, "layout": layout, "cols": op.get("cols"), "near": op.get("near")}
    else:
        lay = {"op": "layout", "ids": made, "layout": layout, "near": op.get("near")}
    unplaced.extend(made)
    _op_layout(doc, lay, idx, ch, unplaced, view)
    if op.get("frame_title"):
        _run_op(doc, "group", {"ids": made, "title": op["frame_title"]}, ch, unplaced, anchors, view)
    return f"outline: {len(made)} items, {len(new_edges)} connectors, {layout}"


# ---------------------------------------------------------------------------
# the person's patches
# ---------------------------------------------------------------------------
def apply_patch(doc: Dict[str, Any], patch: Any) -> Changes:
    """Merge a patch ``{nodes: {id: node|null}, edges: {id: edge|null}, order?: [ids]}`` from a window: per item
    the last write wins, nothing else on the board is touched. Returns what was applied (for the others)."""
    if not isinstance(patch, dict):
        raise BoardError("bad_args", "a patch is an object")
    ch = Changes()
    idx = index_nodes(doc)
    nodes_in = patch.get("nodes") if isinstance(patch.get("nodes"), dict) else {}
    edges_in = patch.get("edges") if isinstance(patch.get("edges"), dict) else {}
    if len(nodes_in) + len(edges_in) > MAX_NODES * 2:
        raise BoardError("too_big", "that patch is too large")
    for nid, raw in nodes_in.items():
        nid = str(nid)
        if raw is None:
            if nid in idx:
                idx.pop(nid)
                doc["nodes"] = [n for n in doc["nodes"] if n["id"] != nid]
                ch.gone_node(nid)
            continue
        node = sanitize_node({**raw, "id": nid} if isinstance(raw, dict) else raw)
        if nid in idx:
            pos = doc["nodes"].index(idx[nid])
            doc["nodes"][pos] = node
        else:
            if len(doc["nodes"]) >= MAX_NODES:
                raise BoardError("too_big", f"a board holds at most {MAX_NODES} items")
            doc["nodes"].append(node)
            ch.order = True
        idx[nid] = node
        ch.node(node)
    eidx = {e["id"]: e for e in doc["edges"]}
    for eid, raw in edges_in.items():
        eid = str(eid)
        if raw is None:
            if eid in eidx:
                doc["edges"] = [e for e in doc["edges"] if e["id"] != eid]
                eidx.pop(eid)
                ch.gone_edge(eid)
            continue
        edge = sanitize_edge({**raw, "id": eid} if isinstance(raw, dict) else raw)
        if edge["fromNode"] not in idx or edge["toNode"] not in idx or eid in idx:
            continue
        if eid in eidx:
            doc["edges"][doc["edges"].index(eidx[eid])] = edge
        else:
            doc["edges"].append(edge)
        eidx[eid] = edge
        ch.edge(edge)
    # a connector never outlives its ends
    dead = [e for e in doc["edges"] if e["fromNode"] not in idx or e["toNode"] not in idx]
    for e in dead:
        ch.gone_edge(e["id"])
    if dead:
        gone = {e["id"] for e in dead}
        doc["edges"] = [e for e in doc["edges"] if e["id"] not in gone]
    order = patch.get("order")
    if isinstance(order, list):
        pos = {str(i): k for k, i in enumerate(order)}
        doc["nodes"].sort(key=lambda n: pos.get(n["id"], len(pos)))
        ch.order = True
    return ch


# ---------------------------------------------------------------------------
# SVG export
# ---------------------------------------------------------------------------
def _hex(c: Optional[str], default: str) -> str:
    if not c:
        return default
    return PRESET_HEX.get(c, c)


def _wrap(text: str, width: int) -> List[str]:
    cpl = max(6, int((width - 20) / 7.4))
    lines: List[str] = []
    for para in str(text).split("\n"):
        words, cur = para.split(" "), ""
        for w in words:
            if len(cur) + len(w) + (1 if cur else 0) <= cpl:
                cur = (cur + " " + w) if cur else w
            else:
                if cur:
                    lines.append(cur)
                cur = w
        lines.append(cur)
    return lines


def to_svg(doc: Dict[str, Any], *, dark: bool = False, pad: int = 40) -> str:
    """The board as a standalone SVG (what PNG export rasterises)."""
    bb = bbox_of(doc["nodes"]) or (0, 0, 400, 300)
    x0, y0, w, h = bb[0] - pad, bb[1] - pad, bb[2] + 2 * pad, bb[3] + 2 * pad
    ink, bg = ("#e6e8ec", "#16181d") if dark else ("#24262b", "#ffffff")
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x0} {y0} {w} {h}" width="{w}" height="{h}" '
           f'font-family="Inter, Segoe UI, Arial, sans-serif" font-size="15">',
           '<defs><marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">'
           f'<path d="M0 0L10 5L0 10z" fill="{ink}"/></marker></defs>',
           f'<rect x="{x0}" y="{y0}" width="{w}" height="{h}" fill="{bg}"/>']
    idx = index_nodes(doc)
    for n in doc["nodes"]:
        k = kind_of(n)
        x, y, nw, nh = box(n)
        col = n.get("color")
        stroke = _hex(col, "#8a8f98")
        if k == "frame":
            out.append(f'<rect x="{x}" y="{y}" width="{nw}" height="{nh}" rx="10" fill="{stroke}" fill-opacity="0.07" '
                       f'stroke="{stroke}" stroke-opacity="0.6" stroke-dasharray="6 4"/>')
            out.append(f'<text x="{x + 12}" y="{y + 24}" fill="{ink}" font-weight="600">{html.escape(str(n.get("label") or ""))}</text>')
            continue
        if k == "stroke":
            pts = (n.get("fx") or {}).get("pts") or []
            if len(pts) > 1:
                d = "M" + " L".join(f"{x + p[0]} {y + p[1]}" for p in pts)
                out.append(f'<path d="{d}" fill="none" stroke="{_hex(col, ink)}" stroke-width="{(n.get("fx") or {}).get("sw", 3)}" '
                           'stroke-linecap="round" stroke-linejoin="round"/>')
            continue
        fill = _hex(col, "#fff3a3" if k == "sticky" else ("none" if k == "text" else ("#2b2f36" if dark else "#f6f7f9")))
        if k == "sticky":
            out.append(f'<rect x="{x}" y="{y}" width="{nw}" height="{nh}" rx="4" fill="{fill}" fill-opacity="0.92"/>')
            tcol = "#24262b"
        elif k == "ellipse":
            out.append(f'<ellipse cx="{x + nw / 2}" cy="{y + nh / 2}" rx="{nw / 2}" ry="{nh / 2}" fill="{fill}" fill-opacity="0.25" stroke="{stroke}" stroke-width="2"/>')
            tcol = ink
        elif k == "diamond":
            out.append(f'<polygon points="{x + nw / 2},{y} {x + nw},{y + nh / 2} {x + nw / 2},{y + nh} {x},{y + nh / 2}" fill="{fill}" fill-opacity="0.25" stroke="{stroke}" stroke-width="2"/>')
            tcol = ink
        elif k == "text":
            tcol = _hex(col, ink)
        elif k in ("link", "image"):
            out.append(f'<rect x="{x}" y="{y}" width="{nw}" height="{nh}" rx="6" fill="{fill}" stroke="{stroke}"/>')
            tcol = ink
        else:
            out.append(f'<rect x="{x}" y="{y}" width="{nw}" height="{nh}" rx="8" fill="{fill}" fill-opacity="0.3" stroke="{stroke}" stroke-width="2"/>')
            tcol = ink
        lines = _wrap(node_text(n), nw)
        lh = 20
        ty = y + nh / 2 - (len(lines) - 1) * lh / 2 + 5
        spans = "".join(f'<tspan x="{x + nw / 2}" y="{ty + i * lh}">{html.escape(l)}</tspan>' for i, l in enumerate(lines[:max(1, int(nh / lh))]))
        out.append(f'<text text-anchor="middle" fill="{tcol}">{spans}</text>')
    for e in doc["edges"]:
        a, b = idx.get(e["fromNode"]), idx.get(e["toNode"])
        if not a or not b:
            continue
        ax, ay = a["x"] + a["width"] / 2, a["y"] + a["height"] / 2
        bx, by = b["x"] + b["width"] / 2, b["y"] + b["height"] / 2
        p1, p2 = _edge_point(a, bx, by), _edge_point(b, ax, ay)
        c = _hex(e.get("color"), ink)
        ms = ' marker-end="url(#ar)"' if e.get("toEnd", "arrow") == "arrow" else ""
        mst = ' marker-start="url(#ar)"' if e.get("fromEnd") == "arrow" else ""
        out.append(f'<line x1="{p1[0]:.1f}" y1="{p1[1]:.1f}" x2="{p2[0]:.1f}" y2="{p2[1]:.1f}" stroke="{c}" stroke-width="2"{ms}{mst}/>')
        if e.get("label"):
            out.append(f'<text x="{(p1[0] + p2[0]) / 2:.1f}" y="{(p1[1] + p2[1]) / 2 - 6:.1f}" text-anchor="middle" fill="{ink}" font-size="13">{html.escape(e["label"])}</text>')
    out.append("</svg>")
    return "\n".join(out)


def _edge_point(n: Dict[str, Any], tx: float, ty: float) -> Tuple[float, float]:
    cx, cy = n["x"] + n["width"] / 2, n["y"] + n["height"] / 2
    dx, dy = tx - cx, ty - cy
    if dx == 0 and dy == 0:
        return cx, cy
    sx = (n["width"] / 2) / abs(dx) if dx else float("inf")
    sy = (n["height"] / 2) / abs(dy) if dy else float("inf")
    s = min(sx, sy)
    return cx + dx * s, cy + dy * s


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------
_lock = threading.RLock()
_cache: Dict[str, Dict[str, Any]] = {}
_root_override: Optional[str] = None


def root() -> str:
    if _root_override:
        return _root_override
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "surfaces", "pizarra")


def set_root_for_tests(path: Optional[str]) -> None:
    global _root_override
    with _lock:
        _root_override = path
        _cache.clear()


def _dir(board_id: str) -> str:
    if not _ID_RE.match(str(board_id or "")):
        raise BoardError("bad_args", "bad board id")
    return os.path.join(root(), board_id)


def _atomic_write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _write(board_id: str, entry: Dict[str, Any]) -> None:
    d = _dir(board_id)
    _atomic_write(os.path.join(d, "meta.json"), json.dumps(entry["meta"], ensure_ascii=False, indent=1))
    _atomic_write(os.path.join(d, "board.canvas"), dumps(entry["doc"]))


def _load(board_id: str) -> Dict[str, Any]:
    with _lock:
        if board_id in _cache:
            return _cache[board_id]
        d = _dir(board_id)
        try:
            with open(os.path.join(d, "meta.json"), encoding="utf-8") as fh:
                meta = json.load(fh)
            with open(os.path.join(d, "board.canvas"), encoding="utf-8") as fh:
                doc = sanitize_doc(fh.read())
        except (OSError, ValueError):
            raise BoardError("not_found", f"no board {board_id!r}") from None
        if meta.get("deleted"):
            raise BoardError("not_found", f"no board {board_id!r}")
        entry = {"meta": meta, "doc": doc}
        _cache[board_id] = entry
        return entry


def create(title: str = "", *, session_id: str = "", project_id: str = "", owner: str = "",
           doc: Any = None) -> Dict[str, Any]:
    with _lock:
        board_id = new_id()
        now = time.time()
        meta = {"id": board_id, "kind": "pizarra", "title": (title or "").strip()[:200] or "Untitled board",
                "session_id": session_id or "", "project_id": project_id or "", "owner": owner or "",
                "created": now, "updated": now, "rev": 1}
        entry = {"meta": meta, "doc": sanitize_doc(doc) if doc is not None else empty_doc()}
        _cache[board_id] = entry
        _write(board_id, entry)
        return dict(meta)


def get(board_id: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """``(meta, doc)`` copies of a board."""
    with _lock:
        e = _load(board_id)
        return dict(e["meta"]), copy.deepcopy(e["doc"])


def list_boards(*, session_id: Optional[str] = None, project_id: Optional[str] = None, owner: Optional[str] = None) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    base = root()
    if not os.path.isdir(base):
        return out
    for name in os.listdir(base):
        try:
            e = _load(name)
        except BoardError:
            continue
        m = e["meta"]
        if session_id is not None and m.get("session_id") != session_id:
            continue
        if project_id is not None and m.get("project_id") != project_id:
            continue
        if owner and m.get("owner") and m["owner"] != owner:
            continue
        row = dict(m)
        row.update(counts(e["doc"]))
        out.append(row)
    out.sort(key=lambda r: r.get("updated", 0), reverse=True)
    return out


def _snapshot(board_id: str, entry: Dict[str, Any]) -> None:
    hist = os.path.join(_dir(board_id), "history")
    last = entry["meta"].get("snap_at", 0)
    if time.time() - last < 20:
        return
    try:
        os.makedirs(hist, exist_ok=True)
        _atomic_write(os.path.join(hist, f"{entry['meta']['rev']:06d}.canvas"), dumps(entry["doc"]))
        entry["meta"]["snap_at"] = time.time()
        names = sorted(os.listdir(hist))
        for old in names[:-20]:
            os.remove(os.path.join(hist, old))
    except OSError:
        pass


def history(board_id: str) -> List[Dict[str, Any]]:
    _load(board_id)
    hist = os.path.join(_dir(board_id), "history")
    if not os.path.isdir(hist):
        return []
    rows = []
    for name in sorted(os.listdir(hist), reverse=True):
        p = os.path.join(hist, name)
        rows.append({"rev": int(name.split(".")[0]), "at": os.path.getmtime(p)})
    return rows


def restore(board_id: str, rev: int) -> Tuple[Dict[str, Any], Changes]:
    """Bring back a snapshot as the current board (the present one is snapshotted first)."""
    with _lock:
        e = _load(board_id)
        p = os.path.join(_dir(board_id), "history", f"{int(rev):06d}.canvas")
        try:
            with open(p, encoding="utf-8") as fh:
                old = sanitize_doc(fh.read())
        except OSError:
            raise BoardError("not_found", f"no snapshot {rev}") from None
        e["meta"]["snap_at"] = 0
        _snapshot(board_id, e)
        ch = Changes()
        for n in e["doc"]["nodes"]:
            ch.gone_node(n["id"])
        for ed in e["doc"]["edges"]:
            ch.gone_edge(ed["id"])
        e["doc"] = old
        for n in old["nodes"]:
            ch.node(n)
        for ed in old["edges"]:
            ch.edge(ed)
        ch.order = True
        _commit(board_id, e)
        return dict(e["meta"]), ch


def _commit(board_id: str, entry: Dict[str, Any]) -> None:
    entry["meta"]["rev"] = int(entry["meta"].get("rev", 0)) + 1
    entry["meta"]["updated"] = time.time()
    _write(board_id, entry)


def rename(board_id: str, title: str) -> Dict[str, Any]:
    with _lock:
        e = _load(board_id)
        e["meta"]["title"] = (title or "").strip()[:200] or e["meta"]["title"]
        _commit(board_id, e)
        return dict(e["meta"])


def delete(board_id: str) -> bool:
    with _lock:
        try:
            e = _load(board_id)
        except BoardError:
            return False
        e["meta"]["deleted"] = True
        _write(board_id, e)
        _cache.pop(board_id, None)
        return True


def edit(board_id: str, ops: Any, *, view: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], List[str], Changes, Optional[BoardError], Dict[str, Any]]:
    """Run ``ops`` on the board. A failing op keeps what ran before it (and says which op failed)."""
    with _lock:
        e = _load(board_id)
        _snapshot(board_id, e)
        err: Optional[BoardError] = None
        try:
            results, ch = apply_ops(e["doc"], ops, view=view)
        except BoardError as exc:
            if not hasattr(exc, "changes"):
                raise
            results, ch, err = list(getattr(exc, "results", [])), getattr(exc, "changes"), exc
        if not ch.empty():
            _commit(board_id, e)
        return dict(e["meta"]), results, ch, err, ch.patch(e["doc"])


def patch(board_id: str, body: Any) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Merge the person's patch; returns the meta and the patch as it was applied (what the other windows get)."""
    with _lock:
        e = _load(board_id)
        ch = apply_patch(e["doc"], body)
        if not ch.empty():
            _commit(board_id, e)
        return dict(e["meta"]), ch.patch(e["doc"])


def replace_doc(board_id: str, raw: Any) -> Tuple[Dict[str, Any], Changes]:
    """Import: the board becomes ``raw`` (a .canvas file's content)."""
    with _lock:
        e = _load(board_id)
        new = sanitize_doc(raw)
        ch = Changes()
        for n in e["doc"]["nodes"]:
            ch.gone_node(n["id"])
        for ed in e["doc"]["edges"]:
            ch.gone_edge(ed["id"])
        e["meta"]["snap_at"] = 0
        _snapshot(board_id, e)
        e["doc"] = new
        for n in new["nodes"]:
            ch.node(n)
        for ed in new["edges"]:
            ch.edge(ed)
        ch.order = True
        _commit(board_id, e)
        return dict(e["meta"]), ch


def add_asset(board_id: str, data: bytes, ext: str) -> str:
    """Store an image next to the board; returns the ``assets/<name>`` path a file node uses."""
    ext = re.sub(r"[^a-z0-9]", "", ext.lower())[:5] or "png"
    if ext not in ("png", "jpg", "jpeg", "gif", "webp", "svg"):
        raise BoardError("bad_args", "images are png, jpg, gif, webp or svg")
    if len(data) > 12 * 1024 * 1024:
        raise BoardError("too_big", "an image is at most 12 MB")
    _load(board_id)
    name = f"{secrets.token_hex(6)}.{ext}"
    d = os.path.join(_dir(board_id), "assets")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, name), "wb") as fh:
        fh.write(data)
    return "assets/" + name


def asset_path(board_id: str, name: str) -> Optional[str]:
    if not re.match(r"^[a-f0-9]{12}\.(png|jpg|jpeg|gif|webp|svg)$", name or ""):
        return None
    p = os.path.join(_dir(board_id), "assets", name)
    return p if os.path.isfile(p) else None


def reset_for_tests() -> None:
    with _lock:
        _cache.clear()
