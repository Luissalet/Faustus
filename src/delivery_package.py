"""delivery_package.py -- reproducible delivery package (Hangar proposal E33).

Collects the editable sources, the exports, the data sources the owner says may
be shared, the instructions, examples and the real limitations of a piece of
work into one folder plus a zip, and calls it *ready* only after every editable
and every export has been reopened by a format reader twice:

1. from the source file, before copying it, and
2. from the zip itself, extracted into a scratch folder -- the package exactly
   as the client receives it (same SHA-256 and the same reader verdict).

Readers are structural (they parse the file the way an importer would); they do
not judge visual quality or correctness. A file whose format has no reader is
``not_checked`` and an editable/export in that state keeps the package from
being ready. Data without an explicit ``authorized: true`` is left out and
listed as excluded. A software deliverable can attach the verified client
handoff of ``code_graph.handoff`` (E10), produced beforehand by
``code_graph_handoff`` (this module never runs a repository's commands): its
HANDOFF.md and report travel in ``instructions/`` with the counts of verified,
failed and not-run steps.

Storage: DATA_DIR/delivery_packages/<owner>/<id>/ (package folder, zip and
manifest.json). One dispatcher, ``run(owner, args)``, serves the chat tool, the
HTTP routes and the MCP server. Errors come back as
``{"error", "error_class", "exit_code": 1}``, never as exceptions.
"""
from __future__ import annotations

import hashlib
import html.parser
import json
import logging
import os
import re
import shutil
import struct
import tempfile
import time
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)

ROLES = ("editable", "export", "data", "instructions", "example", "other")
ACTIONS = ("build", "list", "show", "reverify", "readers", "delete")
_ROLE_DIRS = {"editable": "editables", "export": "exports", "data": "data", "instructions": "instructions",
              "example": "examples", "other": "other"}
_CHECKED_ROLES = ("editable", "export")
MAX_ITEMS = 400
MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")

_TEXT = {
    "es": {"title": "Entrega", "ready": "Lista para entregar", "not_ready": "NO lista para entregar",
           "contents": "Contenido", "role": "Papel", "file": "Fichero", "check": "Comprobación",
           "instructions": "Instrucciones", "limitations": "Limitaciones", "excluded": "Excluido del paquete",
           "gaps": "Qué falta para declararla lista", "software": "Entrega de software comprobada",
           "how": "Cómo se comprobó", "how_text": ("Cada fichero editable y cada exportación se abrió con un lector de su "
                                                   "formato antes de copiarlo y otra vez desde el zip extraído. Los lectores "
                                                   "comprueban estructura, no calidad visual ni corrección."),
           "none": "Ninguno."},
    "en": {"title": "Delivery", "ready": "Ready to deliver", "not_ready": "NOT ready to deliver",
           "contents": "Contents", "role": "Role", "file": "File", "check": "Check",
           "instructions": "Instructions", "limitations": "Limitations", "excluded": "Left out of the package",
           "gaps": "What is missing before it is ready", "software": "Verified software handoff",
           "how": "How it was checked", "how_text": ("Every editable file and export was opened with a reader for its "
                                                     "format before copying and again from the extracted zip. Readers "
                                                     "check structure, not visual quality or correctness."),
           "none": "None."},
}


class PackageError(Exception):
    def __init__(self, error_class: str, detail: str):
        super().__init__(detail)
        self.error_class = error_class
        self.detail = detail


def _err(error_class: str, detail: str) -> Dict[str, Any]:
    return {"error": detail, "error_class": error_class, "exit_code": 1}


# ---------------------------------------------------------------------------
# Readers: each takes a Path and returns facts, or raises ValueError with the
# reason the file does not open as its format claims.

def _read_stl(path: Path) -> Dict[str, Any]:
    data = path.read_bytes()
    if len(data) >= 84:
        count = struct.unpack_from("<I", data, 80)[0]
        if 84 + 50 * count == len(data):
            if count == 0:
                raise ValueError("binary STL with zero triangles")
            lo = [float("inf")] * 3
            hi = [float("-inf")] * 3
            for i in range(count):
                off = 84 + 50 * i + 12
                for v in range(3):
                    xyz = struct.unpack_from("<3f", data, off + 12 * v)
                    for k in range(3):
                        lo[k] = min(lo[k], xyz[k]); hi[k] = max(hi[k], xyz[k])
            return {"format": "STL binary", "triangles": count,
                    "bbox_size": [round(hi[k] - lo[k], 6) for k in range(3)]}
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        raise ValueError("neither a binary STL with a consistent triangle count nor ASCII STL") from None
    if not text.lstrip().lower().startswith("solid") or "endsolid" not in text.lower():
        raise ValueError("ASCII STL without solid/endsolid")
    facets = len(re.findall(r"\bfacet\s+normal\b", text, re.I))
    vertices = len(re.findall(r"\bvertex\s", text, re.I))
    if facets == 0 or vertices != 3 * facets:
        raise ValueError(f"ASCII STL with {facets} facets and {vertices} vertices")
    return {"format": "STL ASCII", "triangles": facets}


def _read_obj(path: Path) -> Dict[str, Any]:
    verts = faces = 0
    worst = 0
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith("v "):
                verts += 1
            elif line.startswith("f "):
                faces += 1
                for tok in line.split()[1:]:
                    idx = tok.split("/")[0]
                    try:
                        n = int(idx)
                    except ValueError:
                        raise ValueError(f"face index {tok!r} is not a number") from None
                    worst = max(worst, n if n > 0 else verts + n + 1)
    if verts == 0 or faces == 0:
        raise ValueError(f"OBJ with {verts} vertices and {faces} faces")
    if worst > verts:
        raise ValueError(f"a face points to vertex {worst} but there are only {verts}")
    return {"format": "OBJ", "vertices": verts, "faces": faces}


def _gltf_facts(doc: Dict[str, Any]) -> Dict[str, Any]:
    asset = doc.get("asset") or {}
    if not isinstance(asset, dict) or not str(asset.get("version", "")).startswith("2"):
        raise ValueError("glTF without asset.version 2.x")
    return {"gltf_version": asset.get("version"), "meshes": len(doc.get("meshes") or []),
            "nodes": len(doc.get("nodes") or []), "materials": len(doc.get("materials") or []),
            "animations": len(doc.get("animations") or [])}


def _read_glb(path: Path) -> Dict[str, Any]:
    data = path.read_bytes()
    if len(data) < 20 or data[:4] != b"glTF":
        raise ValueError("missing glTF binary header")
    version, length = struct.unpack_from("<II", data, 4)
    if version != 2:
        raise ValueError(f"GLB version {version}, expected 2")
    if length != len(data):
        raise ValueError(f"GLB header says {length} bytes, file has {len(data)}")
    clen, ctype = struct.unpack_from("<II", data, 12)
    if ctype != 0x4E4F534A:
        raise ValueError("first GLB chunk is not JSON")
    doc = json.loads(data[20:20 + clen].decode("utf-8"))
    facts = _gltf_facts(doc)
    off = 20 + clen
    bin_len = 0
    if off + 8 <= len(data):
        blen, btype = struct.unpack_from("<II", data, off)
        if btype == 0x004E4942:
            bin_len = blen
    needed = sum(int(b.get("byteLength") or 0) for b in (doc.get("buffers") or []) if isinstance(b, dict) and not b.get("uri"))
    if needed > bin_len:
        raise ValueError(f"buffers need {needed} bytes but the BIN chunk has {bin_len}")
    return {"format": "GLB", **facts, "bin_bytes": bin_len}


def _read_gltf(path: Path) -> Dict[str, Any]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    facts = _gltf_facts(doc)
    for b in list(doc.get("buffers") or []) + list(doc.get("images") or []):
        uri = (b or {}).get("uri") if isinstance(b, dict) else None
        if uri and not uri.startswith("data:") and not (path.parent / uri).is_file():
            raise ValueError(f"external resource {uri!r} is missing next to the .gltf")
    return {"format": "glTF", **facts}


def _read_3mf(path: Path) -> Dict[str, Any]:
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        model = next((n for n in names if n.lower().endswith(".model") and n.lower().startswith("3d/")), None)
        if not model:
            raise ValueError("3MF without a 3D/*.model part")
        root = ET.fromstring(z.read(model))
    ns = {"m": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {"m": ""}
    q = (lambda t: f"{{{ns['m']}}}{t}") if ns["m"] else (lambda t: t)
    objects = root.findall(f".//{q('object')}")
    verts = len(root.findall(f".//{q('vertex')}"))
    tris = len(root.findall(f".//{q('triangle')}"))
    if not objects or tris == 0:
        raise ValueError(f"3MF with {len(objects)} objects and {tris} triangles")
    return {"format": "3MF", "unit": root.get("unit") or "millimeter", "objects": len(objects),
            "vertices": verts, "triangles": tris, "build_items": len(root.findall(f".//{q('item')}"))}


def _read_ply(path: Path) -> Dict[str, Any]:
    with path.open("rb") as fh:
        head = fh.read(65536)
    if not head.startswith(b"ply"):
        raise ValueError("missing 'ply' magic")
    end = head.find(b"end_header")
    if end < 0:
        raise ValueError("PLY header without end_header")
    header = head[:end].decode("ascii", errors="replace")
    counts = dict(re.findall(r"element\s+(\w+)\s+(\d+)", header))
    fmt = re.search(r"format\s+(\S+)", header)
    if int(counts.get("vertex", 0)) == 0:
        raise ValueError("PLY with no vertices")
    return {"format": "PLY", "encoding": fmt.group(1) if fmt else None,
            "vertices": int(counts.get("vertex", 0)), "faces": int(counts.get("face", 0))}


def _read_step(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="latin-1")
    if not text.lstrip().startswith("ISO-10303-21;"):
        raise ValueError("missing ISO-10303-21 header")
    for marker in ("HEADER;", "DATA;", "END-ISO-10303-21;"):
        if marker not in text:
            raise ValueError(f"STEP without {marker}")
    entities = len(re.findall(r"^\s*#\d+\s*=", text, re.M))
    if entities == 0:
        raise ValueError("STEP DATA section without entities")
    schema = re.search(r"FILE_SCHEMA\s*\(\s*\(\s*'([^']+)'", text)
    solids = len(re.findall(r"MANIFOLD_SOLID_BREP|BREP_WITH_VOIDS", text))
    return {"format": "STEP", "schema": schema.group(1) if schema else None, "entities": entities, "solids": solids}


class _HtmlCheck(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = 0

    def handle_starttag(self, tag, attrs):
        self.tags += 1


def _read_html(path: Path) -> Dict[str, Any]:
    p = _HtmlCheck()
    p.feed(path.read_text(encoding="utf-8"))
    if p.tags == 0:
        raise ValueError("no HTML elements")
    return {"format": "HTML", "elements": p.tags}


def _read_text(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError("empty text file")
    return {"format": "text", "lines": text.count("\n") + 1, "characters": len(text)}


def _read_json(path: Path) -> Dict[str, Any]:
    doc = json.loads(path.read_text(encoding="utf-8-sig"))
    return {"format": "JSON", "top_level": type(doc).__name__,
            "entries": len(doc) if isinstance(doc, (list, dict)) else None}


def _read_ipynb(path: Path) -> Dict[str, Any]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    cells = doc.get("cells")
    if not isinstance(cells, list):
        raise ValueError("notebook without a cells list")
    for i, c in enumerate(cells):
        if c.get("cell_type") == "code":
            src = "".join(c.get("source") or [])
            if not src.lstrip().startswith(("%", "!")):
                try:
                    compile(src, f"{path.name}[cell {i}]", "exec")
                except SyntaxError:
                    pass  # magics in the middle of a cell are legitimate; not a broken notebook
    return {"format": "Jupyter notebook", "cells": len(cells), "nbformat": doc.get("nbformat")}


def _read_python(path: Path) -> Dict[str, Any]:
    src = path.read_text(encoding="utf-8")
    try:
        compile(src, str(path.name), "exec")
    except SyntaxError as exc:
        raise ValueError(f"Python does not compile: {exc.msg} at line {exc.lineno}") from None
    return {"format": "Python", "lines": src.count("\n") + 1}


def _read_xml(path: Path) -> Dict[str, Any]:
    root = ET.parse(path).getroot()
    return {"format": "XML", "root": root.tag.split("}")[-1], "width": root.get("width"), "height": root.get("height")}


def _read_zip(path: Path) -> Dict[str, Any]:
    with zipfile.ZipFile(path) as z:
        bad = z.testzip()
        if bad:
            raise ValueError(f"corrupt member {bad}")
        return {"format": "ZIP", "members": len(z.namelist())}


_MAGIC = {".blend": (b"BLENDER", "Blender"), ".mp4": (None, "MP4"), ".mov": (None, "QuickTime"),
          ".m4v": (None, "MP4"), ".m4a": (None, "MP4 audio"), ".wav": (b"RIFF", "WAV"), ".webm": (b"\x1aE\xdf\xa3", "WebM"),
          ".mkv": (b"\x1aE\xdf\xa3", "Matroska"), ".ogg": (b"OggS", "Ogg"), ".opus": (b"OggS", "Ogg Opus"),
          ".flac": (b"fLaC", "FLAC"), ".fbx": (b"Kaydara FBX Binary", "FBX binary")}


def _read_magic(path: Path) -> Dict[str, Any]:
    ext = path.suffix.lower()
    magic, label = _MAGIC[ext]
    with path.open("rb") as fh:
        head = fh.read(64)
    if magic is None:
        ok = head[4:8] == b"ftyp"
    else:
        ok = head.startswith(magic)
    if ext == ".blend" and head.startswith(b"\x1f\x8b"):
        ok = True  # compressed .blend
    if ext == ".fbx" and not ok and head.lstrip().startswith(b";"):
        ok = True  # ASCII FBX
    if not ok:
        raise ValueError(f"{label} signature not found")
    return {"format": label, "level": "container signature only"}


def _di():
    from src import deliverable_inspection as di
    return di


def _read_office(kind: str) -> Callable[[Path], Dict[str, Any]]:
    def read(path: Path) -> Dict[str, Any]:
        di = _di()
        fn = {"pdf": di._pdf, "presentation": di._pptx, "document": di._docx, "spreadsheet": di._xlsx}[kind]
        try:
            facts = fn(path)
        except di.DeliverableInspectionError as exc:
            raise ValueError(str(exc)) from None
        except (zipfile.BadZipFile, ET.ParseError, KeyError):
            raise ValueError(f"{kind} package is malformed") from None
        return {"format": kind, **facts}
    return read


def _read_csv(path: Path) -> Dict[str, Any]:
    di = _di()
    try:
        facts = di._csv(path, "\t" if path.suffix.lower() == ".tsv" else None)
    except di.DeliverableInspectionError as exc:
        raise ValueError(str(exc)) from None
    if not facts.get("row_count_inspected"):
        raise ValueError("no rows")
    return {"format": "delimited text", **facts}


def _read_image(path: Path) -> Dict[str, Any]:
    di = _di()
    try:
        facts = di._image_facts(path)
    except di.DeliverableInspectionError as exc:
        raise ValueError(str(exc)) from None
    if facts.get("metadata_error"):
        raise ValueError(facts["metadata_error"])
    return {"format": "image", **facts}


READERS: Dict[str, Callable[[Path], Dict[str, Any]]] = {
    ".stl": _read_stl, ".obj": _read_obj, ".glb": _read_glb, ".gltf": _read_gltf, ".3mf": _read_3mf,
    ".ply": _read_ply, ".step": _read_step, ".stp": _read_step,
    ".pdf": _read_office("pdf"), ".pptx": _read_office("presentation"), ".potx": _read_office("presentation"),
    ".pptm": _read_office("presentation"), ".docx": _read_office("document"), ".xlsx": _read_office("spreadsheet"),
    ".csv": _read_csv, ".tsv": _read_csv,
    ".png": _read_image, ".jpg": _read_image, ".jpeg": _read_image, ".webp": _read_image, ".gif": _read_image,
    ".bmp": _read_image, ".tif": _read_image, ".tiff": _read_image, ".svg": _read_xml, ".xml": _read_xml,
    ".html": _read_html, ".htm": _read_html, ".json": _read_json, ".geojson": _read_json, ".ipynb": _read_ipynb,
    ".py": _read_python, ".md": _read_text, ".txt": _read_text, ".rst": _read_text, ".zip": _read_zip,
    **{ext: _read_magic for ext in _MAGIC},
}


def _summary(facts: Any, depth: int = 0) -> Any:
    """Small, JSON-able view of reader facts: scalars kept, lists as counts."""
    if isinstance(facts, dict):
        if depth > 0:
            return f"{len(facts)} fields"
        return {str(k): _summary(v, depth + 1) for k, v in list(facts.items())[:24]}
    if isinstance(facts, (list, tuple)):
        if facts and all(isinstance(x, (int, float)) for x in facts) and len(facts) <= 4:
            return list(facts)
        return f"{len(facts)} items"
    if isinstance(facts, str):
        return facts[:200]
    return facts


def reopen(path: Path) -> Dict[str, Any]:
    """Open one file with the reader for its extension: verified, failed or not_checked."""
    ext = path.suffix.lower()
    reader = READERS.get(ext)
    if reader is None:
        return {"status": "not_checked", "reader": None,
                "reason": f"no reader for {ext or 'files without extension'}; only identity and hash were measured"}
    try:
        facts = reader(path)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, ET.ParseError, zipfile.BadZipFile,
            struct.error, KeyError, OSError) as exc:
        return {"status": "failed", "reader": ext.lstrip("."), "reason": str(exc) or type(exc).__name__}
    level = facts.pop("level", "parsed")
    return {"status": "verified", "reader": ext.lstrip("."), "level": level, "facts": _summary(facts)}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Storage

def _owner_dir(owner: str) -> Path:
    from src.constants import DATA_DIR
    return Path(DATA_DIR) / "delivery_packages" / (_SAFE.sub("_", str(owner or "_"))[:80] or "_")


def _slug(name: str) -> str:
    s = _SAFE.sub("-", (name or "").strip()).strip("-.")
    return (s or "entrega")[:60]


def _confine(raw: str) -> Path:
    from src.tool_execution import _resolve_tool_path
    return Path(_resolve_tool_path(raw))


def _open_path(raw: str) -> Path:
    """For the admin HTTP routes and the owner-configured MCP server: any local
    path the owner names, except the sensitive ones read_file also refuses."""
    if not str(raw or "").strip():
        raise ValueError("path is required")
    resolved = os.path.realpath(os.path.expanduser(str(raw).strip().strip('"')))
    from src.tool_execution import _is_sensitive_path
    if _is_sensitive_path(resolved):
        raise ValueError(f"path {raw!r} is inside a sensitive directory or is a sensitive file")
    return Path(resolved)


def _path(raw: str, confine: bool) -> Path:
    return _confine(raw) if confine else _open_path(raw)


def _items_from(args: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Items as a list of {path, role, authorized?, note?}; also accepts the
    one-per-line text form `role: path` that the UI sends."""
    items = args.get("items")
    out: List[Dict[str, Any]] = []
    if isinstance(items, str):
        for line in items.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            role, sep, rest = line.partition(":")
            if sep and role.strip().lower() in ROLES and not re.match(r"^[A-Za-z]$", role.strip()):
                path = rest.strip()
                r = role.strip().lower()
            else:
                path, r = line, "other"
            authorized = False
            if r == "data" and path.lower().endswith("(authorized)"):
                path, authorized = path[: -len("(authorized)")].strip(), True
            out.append({"path": path.strip().strip('"'), "role": r, "authorized": authorized})
        return out
    if not isinstance(items, list):
        return out
    for it in items:
        if isinstance(it, str):
            out.append({"path": it, "role": "other"})
        elif isinstance(it, dict) and it.get("path"):
            out.append(dict(it))
    return out


def _limitations_from(args: Mapping[str, Any]) -> List[str]:
    raw = args.get("limitations")
    if isinstance(raw, str):
        raw = [ln.strip(" -*\t") for ln in raw.splitlines()]
    return [str(x).strip() for x in (raw or []) if str(x).strip()]


def _unique(dest_dir: Path, name: str) -> Path:
    target = dest_dir / name
    stem, ext = os.path.splitext(name)
    n = 2
    while target.exists():
        target = dest_dir / f"{stem}-{n}{ext}"
        n += 1
    return target


def _software(args: Mapping[str, Any], gaps: List[str], confine: bool = True) -> Optional[Dict[str, Any]]:
    raw = str(args.get("software_root") or "").strip()
    if not raw:
        return None
    root = _path(raw, confine)
    if not root.is_dir():
        raise PackageError("delivery.bad_software_root", f"software_root {raw!r} is not a folder")
    report_path = root / ".handoff" / "handoff-report.json"
    if not report_path.is_file():
        gaps.append("software handoff not verified: no .handoff/handoff-report.json in the software root "
                    "(run code_graph_handoff on it first)")
        return {"root": str(root), "verified": False, "counts": None}
    report = json.loads(report_path.read_text(encoding="utf-8"))
    counts = report.get("counts") or {}
    if not counts.get("verified"):
        gaps.append("software handoff has no verified step")
    failed = [s.get("title") for s in report.get("steps") or [] if s.get("verdict") == "failed"]
    return {"root": str(root), "verified": bool(counts.get("verified")), "counts": counts,
            "failed_steps": failed, "revision": report.get("revision"),
            "files": [str(report_path), str(root / ".handoff" / "HANDOFF.md")]}


def _render_md(m: Dict[str, Any], lang: str) -> str:
    t = _TEXT.get(lang, _TEXT["es"])
    lines = [f"# {t['title']}: {m['name']}", "",
             f"**{t['ready'] if m['ready'] else t['not_ready']}** · {m['created']}", ""]
    lines += [f"## {t['contents']}", "", f"| {t['role']} | {t['file']} | {t['check']} | SHA-256 |", "|---|---|---|---|"]
    for it in m["items"]:
        v = it["verification"]
        check = v["status"] + (f" ({v.get('reader')})" if v.get("reader") else "")
        if v["status"] != "verified" and v.get("reason"):
            check += f": {v['reason']}"
        lines.append(f"| {it['role']} | `{it['package_path']}` | {check} | `{it['sha256'][:16]}…` |")
    lines += ["", f"## {t['instructions']}", "", (m.get("instructions") or t["none"]).strip(), ""]
    sw = m.get("software")
    if sw:
        c = sw.get("counts") or {}
        lines += [f"## {t['software']}", "",
                  f"verified {c.get('verified', 0)}, failed {c.get('failed', 0)}, not run {c.get('not run', 0)}"
                  + (" — instructions/HANDOFF.md" if sw.get("verified") is not None and c else ""), ""]
    lines += [f"## {t['limitations']}", ""]
    lines += [f"- {x}" for x in m["limitations"]] or [t["none"]]
    lines += ["", f"## {t['excluded']}", ""]
    lines += [f"- `{x['path']}`: {x['reason']}" for x in m["excluded"]] or [t["none"]]
    if m["gaps"]:
        lines += ["", f"## {t['gaps']}", ""] + [f"- {g}" for g in m["gaps"]]
    lines += ["", f"## {t['how']}", "", t["how_text"], ""]
    return "\n".join(lines)


def _reverify_zip(zip_path: Path, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Extract the zip into a scratch folder and reopen every item from there."""
    tmp = Path(tempfile.mkdtemp(prefix="delivery-reimport-"))
    try:
        with zipfile.ZipFile(zip_path) as z:
            bad = z.testzip()
            if bad:
                return {"ok": False, "reason": f"corrupt zip member {bad}", "items": []}
            z.extractall(tmp)
        roots = [p for p in tmp.iterdir() if p.is_dir()]
        base = roots[0] if len(roots) == 1 else tmp
        results = []
        ok = True
        for it in items:
            p = base / it["package_path"]
            if not p.is_file():
                results.append({"package_path": it["package_path"], "status": "missing"})
                ok = False
                continue
            same = _sha256(p) == it["sha256"]
            v = reopen(p)
            status = v["status"] if same else "hash_mismatch"
            if status != it["verification"]["status"]:
                ok = False
            results.append({"package_path": it["package_path"], "status": status, "same_hash": same,
                            **({"reason": v.get("reason")} if v.get("reason") else {})})
        for extra in ("ENTREGA.md", "manifest.json"):
            if not (base / extra).is_file():
                ok = False
                results.append({"package_path": extra, "status": "missing"})
        return {"ok": ok, "items": results, "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _base_name(p: str) -> str:
    return re.split(r"[\\/]", p.rstrip("\\/"))[-1] or p


def _public(manifest: Any, private_paths: List[str]) -> Any:
    """Copy of the manifest for what travels to the client: every local path the
    owner gave or that was resolved from it is cut down to its file name."""
    ps = sorted({p for p in private_paths if p and len(p) > 3}, key=len, reverse=True)

    def walk(v: Any) -> Any:
        if isinstance(v, str):
            for p in ps:
                if p in v:
                    v = v.replace(p, _base_name(p))
            return v
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        return v
    return walk(manifest)


def _zip_dir(folder: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(folder.rglob("*")):
            if p.is_file():
                z.write(p, arcname=str(Path(folder.name) / p.relative_to(folder)).replace("\\", "/"))


def build(owner: str, args: Mapping[str, Any], *, confine: bool = True) -> Dict[str, Any]:
    name = str(args.get("name") or "").strip()
    if not name:
        raise PackageError("delivery.bad_args", "name is required")
    lang = str(args.get("lang") or "es").lower()
    lang = lang if lang in _TEXT else "es"
    items = _items_from(args)
    if not items and not args.get("software_root"):
        raise PackageError("delivery.bad_args", "items is required (files with a role) or software_root")
    if len(items) > MAX_ITEMS:
        raise PackageError("delivery.too_many", f"at most {MAX_ITEMS} items")
    gaps: List[str] = []
    excluded: List[Dict[str, str]] = []
    resolved: List[Dict[str, Any]] = []
    total = 0
    private_paths: List[str] = []
    for it in items:
        role = str(it.get("role") or "other").lower()
        if role not in ROLES:
            raise PackageError("delivery.bad_role", f"role {role!r} is not one of {', '.join(ROLES)}")
        raw = str(it.get("path") or "")
        private_paths.append(raw)
        try:
            p = _path(raw, confine)
        except ValueError as exc:
            excluded.append({"path": raw, "reason": f"refused: {exc}"})
            gaps.append(f"{raw}: path refused")
            continue
        if not p.is_file():
            excluded.append({"path": raw, "reason": "not found or not a file"})
            if role in _CHECKED_ROLES or role == "instructions":
                gaps.append(f"{role} {raw} does not exist")
            continue
        if role == "data" and not it.get("authorized"):
            excluded.append({"path": str(p), "reason": "data source without explicit authorization to share it"})
            continue
        total += p.stat().st_size
        private_paths.append(str(p))
        resolved.append({"path": p, "role": role, "note": str(it.get("note") or "")[:300]})
    if total > MAX_TOTAL_BYTES:
        raise PackageError("delivery.too_large", f"{total} bytes exceed the {MAX_TOTAL_BYTES} package limit")
    software = _software(args, gaps, confine)
    private_paths.append(str(args.get("software_root") or "").strip())
    private_paths.append(str(args.get("out_dir") or "").strip())
    if software:
        private_paths.append(str(software.get("root") or ""))
        private_paths += [str(f) for f in software.get("files") or []]
    if software and software.get("files"):
        for f in software["files"]:
            if Path(f).is_file():
                resolved.append({"path": Path(f), "role": "instructions", "note": "verified software handoff"})
    instructions = str(args.get("instructions") or "").strip()
    limitations = _limitations_from(args)

    pkg_id = time.strftime("%Y%m%d-%H%M%S") + "-" + hashlib.sha1(f"{name}{time.time()}".encode()).hexdigest()[:6]
    home = _owner_dir(owner) / pkg_id
    folder = home / _slug(name)
    folder.mkdir(parents=True, exist_ok=False)
    rows: List[Dict[str, Any]] = []
    for r in resolved:
        src: Path = r["path"]
        verification = reopen(src)
        dest_dir = folder / _ROLE_DIRS[r["role"]]
        dest_dir.mkdir(exist_ok=True)
        dest = _unique(dest_dir, src.name)
        shutil.copy2(src, dest)
        digest = _sha256(dest)
        private_paths.append(str(src))
        rows.append({"role": r["role"], "name": src.name, "source_path": str(src),
                     "package_path": str(dest.relative_to(folder)).replace("\\", "/"),
                     "sha256": digest, "size": dest.stat().st_size, "note": r["note"],
                     "verification": verification})
        if verification["status"] == "failed":
            gaps.append(f"{r['role']} {src.name} does not open: {verification.get('reason')}")
        elif verification["status"] == "not_checked" and r["role"] in _CHECKED_ROLES:
            gaps.append(f"{r['role']} {src.name} could not be reopened: {verification.get('reason')}")
    roles = {r["role"] for r in rows}
    if not instructions and "instructions" not in roles:
        gaps.append("no instructions: write them or add an instructions file")
    if not limitations:
        gaps.append("no limitations stated: list what the delivery does not do or was not checked")
    if "export" in roles and "editable" not in roles:
        gaps.append("exports without any editable source in the package")
    if not rows:
        gaps.append("the package contains no files")
    manifest: Dict[str, Any] = {
        "schema": "delivery-package/1", "id": pkg_id, "name": name, "lang": lang,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "items": rows, "excluded": excluded,
        "instructions": instructions, "limitations": limitations, "software": software,
        "gaps": gaps, "ready": False,
    }
    manifest["ready"] = not gaps
    (folder / "ENTREGA.md" if lang == "es" else folder / "DELIVERY.md").write_text(
        _render_md(_public(manifest, private_paths), lang), encoding="utf-8")
    if lang != "es":
        shutil.copy2(folder / "DELIVERY.md", folder / "ENTREGA.md")
    (folder / "manifest.json").write_text(json.dumps(_public(manifest, private_paths), indent=2, ensure_ascii=False),
                                          encoding="utf-8")
    zip_path = home / f"{folder.name}.zip"
    _zip_dir(folder, zip_path)
    reimport = _reverify_zip(zip_path, rows)
    if not reimport["ok"]:
        # The package must not carry a "ready" it did not earn: rewrite its
        # description and zip again, keeping the failed reimport as evidence.
        gaps.append("the zip did not reopen with the same results as the sources")
        manifest["ready"] = False
        md_name = "ENTREGA.md"
        (folder / md_name).write_text(_render_md(_public(manifest, private_paths), lang), encoding="utf-8")
        if lang != "es":
            (folder / "DELIVERY.md").write_text(_render_md(_public(manifest, private_paths), lang), encoding="utf-8")
        (folder / "manifest.json").write_text(json.dumps(_public(manifest, private_paths), indent=2, ensure_ascii=False),
                                              encoding="utf-8")
        zip_path.unlink()
        _zip_dir(folder, zip_path)
    manifest["reimport"] = reimport
    manifest["zip"] = {"path": str(zip_path), "sha256": _sha256(zip_path), "size": zip_path.stat().st_size}
    out_dir = str(args.get("out_dir") or "").strip()
    if out_dir:
        target = _path(out_dir, confine)
        target.mkdir(parents=True, exist_ok=True)
        copied = target / zip_path.name
        shutil.copy2(zip_path, copied)
        manifest["zip"]["copied_to"] = str(copied)
    (home / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest


def _load(owner: str, pkg_id: str) -> Dict[str, Any]:
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}", pkg_id or ""):
        raise PackageError("delivery.not_found", f"unknown package {pkg_id!r}")
    path = _owner_dir(owner) / pkg_id / "manifest.json"
    if not path.is_file():
        raise PackageError("delivery.not_found", f"unknown package {pkg_id!r}")
    return json.loads(path.read_text(encoding="utf-8"))


def list_packages(owner: str) -> List[Dict[str, Any]]:
    base = _owner_dir(owner)
    rows = []
    if base.is_dir():
        for d in sorted(base.iterdir(), reverse=True):
            mf = d / "manifest.json"
            if not mf.is_file():
                continue
            try:
                m = json.loads(mf.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            counts: Dict[str, int] = {}
            for it in m.get("items") or []:
                s = it["verification"]["status"]
                counts[s] = counts.get(s, 0) + 1
            rows.append({"id": m["id"], "name": m["name"], "created": m["created"], "ready": m["ready"],
                         "items": len(m.get("items") or []), "checks": counts, "gaps": len(m.get("gaps") or []),
                         "excluded": len(m.get("excluded") or []), "zip_size": (m.get("zip") or {}).get("size")})
    return rows


def zip_path(owner: str, pkg_id: str) -> Path:
    m = _load(owner, pkg_id)
    p = Path((m.get("zip") or {}).get("path") or "")
    if not p.is_file():
        raise PackageError("delivery.not_found", "the package zip is gone")
    return p


def reverify(owner: str, pkg_id: str) -> Dict[str, Any]:
    m = _load(owner, pkg_id)
    p = zip_path(owner, pkg_id)
    res = _reverify_zip(p, m["items"])
    res["zip_same_hash"] = _sha256(p) == (m.get("zip") or {}).get("sha256")
    res["ok"] = res["ok"] and res["zip_same_hash"]
    return {"id": pkg_id, "name": m["name"], "ready_when_built": m["ready"], "reverify": res}


def delete(owner: str, pkg_id: str) -> Dict[str, Any]:
    """Remove Faustus' own copy of a package (folder, zip, manifest). The files the
    package was built from, and a zip copied to ``out_dir``, are never touched."""
    m = _load(owner, pkg_id)
    base = _owner_dir(owner).resolve()
    home = (base / pkg_id).resolve()
    if home.parent != base or not home.is_dir():
        raise PackageError("delivery.not_found", f"unknown package {pkg_id!r}")
    from src import commission_office
    linked = commission_office.commissions_linking_package(owner, pkg_id)
    size = sum(p.stat().st_size for p in home.rglob("*") if p.is_file())
    shutil.rmtree(home)
    copied = (m.get("zip") or {}).get("copied_to")
    return {"deleted": pkg_id, "name": m.get("name"), "removed": {"files": len(m.get("items") or []), "bytes": size},
            "kept": {"source_files": "never touched", **({"zip_copy_outside_faustus": copied} if copied else {})},
            "was_linked_in": linked}


def render(m: Dict[str, Any]) -> str:
    """One-screen text summary for the chat."""
    if "deleted" in m:
        return f"Deleted package {m['deleted']} ({m.get('name')}); source files untouched."
    if "reverify" in m:
        r = m["reverify"]
        return (f"Package {m['id']} ({m['name']}): zip {'reopens identically' if r['ok'] else 'does NOT reopen identically'}; "
                f"{len(r['items'])} entries checked.")
    if "items" not in m:
        return json.dumps(m, ensure_ascii=False)
    counts: Dict[str, int] = {}
    for it in m["items"]:
        counts[it["verification"]["status"]] = counts.get(it["verification"]["status"], 0) + 1
    head = (f"Delivery package {m['id']} '{m['name']}': {'READY' if m['ready'] else 'NOT READY'}; "
            + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) + f"; zip {m['zip']['path']}")
    lines = [head]
    for it in m["items"]:
        v = it["verification"]
        lines.append(f"- {it['role']}: {it['package_path']} -> {v['status']}"
                     + (f" ({v.get('reason')})" if v.get("reason") and v["status"] != "verified" else ""))
    if m["excluded"]:
        lines.append("Excluded: " + "; ".join(f"{x['path']} ({x['reason']})" for x in m["excluded"]))
    if m["gaps"]:
        lines.append("Gaps: " + "; ".join(m["gaps"]))
    return "\n".join(lines)


def run(owner: str, args: Mapping[str, Any], *, confine: bool = True) -> Dict[str, Any]:
    """Dispatcher shared by the chat tool, the HTTP routes and the MCP server."""
    action = str(args.get("action") or "build").strip().lower()
    try:
        if action not in ACTIONS:
            raise PackageError("delivery.bad_args", f"action must be one of {', '.join(ACTIONS)}")
        if action == "readers":
            return {"readers": sorted(READERS), "roles": list(ROLES)}
        if action == "list":
            return {"packages": list_packages(owner)}
        if action == "show":
            return _load(owner, str(args.get("id") or ""))
        if action == "reverify":
            return reverify(owner, str(args.get("id") or ""))
        if action == "delete":
            return delete(owner, str(args.get("id") or ""))
        return build(owner, args, confine=confine)
    except PackageError as exc:
        return _err(exc.error_class, exc.detail)
    except (ValueError, PermissionError, OSError) as exc:
        return _err("delivery.refused", str(exc))
    except Exception as exc:  # noqa: BLE001 - a tool call never crashes the turn
        logger.warning("delivery package failed", exc_info=True)
        return _err("delivery.error", f"{type(exc).__name__}: {exc}")
