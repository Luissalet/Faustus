"""src/code_graph/floorplan.py — a plain-language floorplan of a workspace.

Built on the existing code graph rather than beside it: the parts are the
level-0 communities `src.code_graph.communities` already computes (named
after their level-1 group), and this module adds what a person needs to
read the map without reading code:

* one plain line per file and per part, written by the configured utility
  model and cached by content hash, so a second run only relabels files
  whose bytes changed (a file with no model label yet keeps a deterministic
  description taken from its docstring/leading comment or its symbols);
* the HTTP routes each file serves and the outgoing HTTP requests it makes
  (deterministic regex scan of Python and JS/TS sources);
* a self-contained interactive HTML export (parts → files → routes and
  requests, search, a link that opens the file in Studio);
* a "blueprint" mode: a PRD/goal text becomes a list of planned parts, each
  marked exists / partial / missing against the current floorplan with the
  matching parts as evidence.

The labeller is a plain callable `labeler(prompt) -> str` so tests inject a
fake one; production resolves the `utility` endpoint. `labels="auto"` only
calls a model that is already resident and idle (the same gate background
brain passes use); `labels="model"` calls it regardless (the Studio button);
`labels="off"` never calls a model.
"""
from __future__ import annotations

import ast
import hashlib
import json
import logging
import os
import re
import subprocess
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

Labeler = Callable[[str], str]

#: Reassigned by tests to a temporary folder.
CACHE_DIR: Optional[str] = None

MAX_FILES = 4000
MAX_FILE_BYTES = 400_000
DEFAULT_MAX_LABELS = 80
_BATCH = 10
_SNIPPET_CHARS = 700
_OUTPUT_CHARS = 4000

_CODE_EXT = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".cjs": "javascript", ".ts": "typescript", ".tsx": "typescript", ".vue": "vue",
    ".svelte": "svelte", ".go": "go", ".rs": "rust", ".java": "java", ".cs": "csharp",
    ".rb": "ruby", ".php": "php", ".kt": "kotlin", ".swift": "swift",
}
_SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "env", "__pycache__", "dist", "build",
              ".next", ".nuxt", ".cache", "coverage", ".pytest_cache", ".mypy_cache",
              "site-packages", "Library", "Temp", "obj", "bin"}
_TEST_RE = re.compile(r"(?:^|/)tests?/|(?:^|/)test_[^/]+\.py$|[^/]+_test\.py$"
                      r"|[^/]+\.test\.[a-z]+$|[^/]+\.spec\.[a-z]+$|(?:^|/)__tests__/")

# ── HTTP routes and outgoing requests ───────────────────────────────────

_PY_ROUTE = re.compile(
    r"""@(?P<obj>[A-Za-z_][\w.]*)\.(?P<m>get|post|put|patch|delete|head|options|websocket|api_route|route)"""
    r"""\(\s*(?P<q>['"])(?P<path>[^'"]*)(?P=q)(?P<rest>[^\n]*)""")
_PY_ROUTER_PREFIX = re.compile(
    r"""(?P<var>[A-Za-z_]\w*)\s*=\s*(?:[\w.]+\.)?(?:APIRouter|Blueprint)\((?P<args>[^)]*)\)""")
_PREFIX_ARG = re.compile(r"""(?:url_)?prefix\s*=\s*['"]([^'"]*)['"]""")
_METHODS_ARG = re.compile(r"""methods\s*=\s*\[([^\]]*)\]""")
_JS_ROUTE = re.compile(
    r"""\b(?P<obj>app|router|server|fastify|api_router|apiRouter)\.(?P<m>get|post|put|patch|delete|all)"""
    r"""\(\s*(?P<q>['"`])(?P<path>/[^'"`]*)(?P=q)""")
_JS_FETCH = re.compile(r"""\bfetch\(\s*(?P<q>['"`])(?P<url>[^'"`]+)(?P=q)(?P<rest>[^;]{0,240})""")
_JS_METHOD_OPT = re.compile(r"""method\s*:\s*['"`](\w+)['"`]""")
_JS_CLIENT = re.compile(
    r"""\b(?:axios|api|http|client|\$http|instance|apiClient)\.(?P<m>get|post|put|patch|delete)"""
    r"""\(\s*(?P<q>['"`])(?P<url>[^'"`]+)(?P=q)""")
_PY_REQUEST = re.compile(
    r"""\b(?:requests|httpx|session|client|aiohttp_session|self\.client|self\._client|self\.session)"""
    r"""\.(?P<m>get|post|put|patch|delete|request)\(\s*(?:f?(?P<q>['"])(?P<url>[^'"]+)(?P=q))""")
_PY_URLOPEN = re.compile(r"""\burlopen\(\s*f?(?P<q>['"])(?P<url>[^'"]+)(?P=q)""")


def _join_prefix(prefix: str, path: str) -> str:
    if not prefix:
        return path or "/"
    return (prefix.rstrip("/") + "/" + (path or "").lstrip("/")).rstrip("/") or "/"


def extract_routes(text: str, language: str) -> List[Dict[str, Any]]:
    """HTTP routes a source file serves, deterministic and line-anchored."""
    out: List[Dict[str, Any]] = []
    if language == "python":
        prefixes: Dict[str, str] = {}
        for m in _PY_ROUTER_PREFIX.finditer(text):
            pm = _PREFIX_ARG.search(m.group("args"))
            if pm:
                prefixes[m.group("var")] = pm.group(1)
        for m in _PY_ROUTE.finditer(text):
            obj = m.group("obj").split(".")[-1]
            method = m.group("m").upper()
            if method in ("ROUTE", "API_ROUTE"):
                mm = _METHODS_ARG.search(m.group("rest") or "")
                methods = re.findall(r"[A-Za-z]+", mm.group(1)) if mm else []
                method = "/".join(x.upper() for x in methods) or "GET"
            elif method == "WEBSOCKET":
                method = "WS"
            line = text.count("\n", 0, m.start()) + 1
            out.append({"method": method, "path": _join_prefix(prefixes.get(obj, ""), m.group("path")),
                        "line": line})
    elif language in ("javascript", "typescript", "vue", "svelte"):
        for m in _JS_ROUTE.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            out.append({"method": m.group("m").upper(), "path": m.group("path"), "line": line})
    return out


def extract_requests(text: str, language: str) -> List[Dict[str, Any]]:
    """Outgoing HTTP requests a source file makes (literal URLs only)."""
    out: List[Dict[str, Any]] = []
    seen = set()

    def add(method: str, url: str, pos: int) -> None:
        line = text.count("\n", 0, pos) + 1
        key = (method, url, line)
        if key in seen:
            return
        seen.add(key)
        out.append({"method": method, "url": url, "line": line})

    if language in ("javascript", "typescript", "vue", "svelte"):
        for m in _JS_FETCH.finditer(text):
            om = _JS_METHOD_OPT.search(m.group("rest") or "")
            add((om.group(1) if om else "GET").upper(), m.group("url"), m.start())
        for m in _JS_CLIENT.finditer(text):
            add(m.group("m").upper(), m.group("url"), m.start())
    elif language == "python":
        for m in _PY_REQUEST.finditer(text):
            method = m.group("m").upper()
            add("ANY" if method == "REQUEST" else method, m.group("url"), m.start())
        for m in _PY_URLOPEN.finditer(text):
            add("GET", m.group("url"), m.start())
    out.sort(key=lambda r: r["line"])
    return out


# ── per-file scan ───────────────────────────────────────────────────────

_JS_SYMBOL = re.compile(
    r"""^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function\s*\*?\s*(?P<f>[A-Za-z_$][\w$]*)"""
    r"""|class\s+(?P<c>[A-Za-z_$][\w$]*)|(?:const|let)\s+(?P<v>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\(|function|[A-Za-z_$][\w$]*\s*=>))""",
    re.M)
_JS_LEAD_COMMENT = re.compile(r"""\A\s*(?:/\*\*?(?P<block>.*?)\*/|(?P<line>(?://[^\n]*\n\s*)+))""", re.S)
_GENERIC_DOC = re.compile(r"^(?:copyright|license|eslint|@ts-|prettier|use strict|-\*-)", re.I)


def _first_sentence(text: str, limit: int = 160) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip(" *-/#\t")
    if not text or _GENERIC_DOC.match(text):
        return ""
    m = re.match(r"(.+?[.!?])(?:\s|$)", text)
    sentence = (m.group(1) if m else text).strip()
    return sentence[:limit].rstrip()


def _python_symbols(text: str) -> Tuple[List[str], str]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        names = re.findall(r"^(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)", text, re.M)
        return names[:24], ""
    names = [n.name for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    doc = ast.get_docstring(tree) or ""
    if doc:
        # Module docstrings often start with "path/file.py — what it is".
        doc = re.sub(r"^[\w./\\-]+\.py\s*[—–-]+\s*", "", doc.strip())
    return names[:24], _first_sentence(doc)


def _js_symbols(text: str) -> Tuple[List[str], str]:
    names: List[str] = []
    for m in _JS_SYMBOL.finditer(text):
        name = m.group("f") or m.group("c") or m.group("v")
        if name and name not in names:
            names.append(name)
    doc = ""
    m = _JS_LEAD_COMMENT.match(text)
    if m:
        raw = m.group("block") if m.group("block") is not None else m.group("line") or ""
        raw = re.sub(r"(?m)^\s*(?://|\*)\s?", "", raw)
        doc = _first_sentence(raw)
    return names[:24], doc


def file_hash(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def scan_file(root: str, rel: str) -> Optional[Dict[str, Any]]:
    """Deterministic facts about one file; None when it cannot be read."""
    ext = os.path.splitext(rel)[1].lower()
    language = _CODE_EXT.get(ext)
    if not language:
        return None
    path = os.path.join(root, *rel.replace("\\", "/").split("/"))
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_FILE_BYTES + 1)
    except OSError:
        return None
    truncated = len(data) > MAX_FILE_BYTES
    data = data[:MAX_FILE_BYTES]
    text = data.decode("utf-8", errors="replace")
    if language == "python":
        symbols, doc = _python_symbols(text)
    else:
        symbols, doc = _js_symbols(text)
    return {
        "path": rel.replace("\\", "/"), "language": language, "hash": file_hash(data),
        "lines": text.count("\n") + (1 if text and not text.endswith("\n") else 0),
        "symbols": symbols, "doc": doc, "truncated": truncated,
        "routes": extract_routes(text, language), "requests": extract_requests(text, language),
        "_head": _code_head(text),
    }


def _code_head(text: str) -> str:
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[:40])[:_SNIPPET_CHARS]


def heuristic_label(info: Dict[str, Any]) -> str:
    """A description that needs no model: the file's own docstring/leading
    comment, else what it serves, calls and defines."""
    if info.get("doc"):
        return info["doc"]
    bits: List[str] = []
    routes = info.get("routes") or []
    if routes:
        shown = ", ".join(f"{r['method']} {r['path']}" for r in routes[:3])
        bits.append(f"Serves {len(routes)} HTTP route{'s' if len(routes) != 1 else ''} ({shown}"
                    + (", …)" if len(routes) > 3 else ")"))
    reqs = info.get("requests") or []
    if reqs:
        bits.append(f"calls {', '.join(sorted({r['url'] for r in reqs})[:2])}")
    syms = info.get("symbols") or []
    if syms:
        bits.append("defines " + ", ".join(syms[:4]) + (" and more" if len(syms) > 4 else ""))
    if not bits:
        return f"{info.get('language', 'code').capitalize()} file"
    text = "; ".join(bits)
    return text[0].upper() + text[1:]


def is_test_path(rel: str) -> bool:
    return bool(_TEST_RE.search(rel.replace("\\", "/")))


def list_code_files(root: str, limit: int = MAX_FILES) -> Tuple[List[str], bool]:
    """Tracked code files (git ls-files), else a pruned walk."""
    files: List[str] = []
    try:
        proc = subprocess.run(["git", "-C", root, "ls-files", "-z"], capture_output=True,
                              timeout=30, check=False)
        if proc.returncode == 0 and proc.stdout:
            for raw in proc.stdout.decode("utf-8", errors="replace").split("\0"):
                if raw and os.path.splitext(raw)[1].lower() in _CODE_EXT:
                    if not any(part in _SKIP_DIRS for part in raw.split("/")[:-1]):
                        files.append(raw)
    except (OSError, subprocess.SubprocessError):
        files = []
    if not files:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not d.startswith("."))
            for name in sorted(filenames):
                if os.path.splitext(name)[1].lower() in _CODE_EXT:
                    files.append(os.path.relpath(os.path.join(dirpath, name), root).replace("\\", "/"))
            if len(files) > limit * 2:
                break
    files = sorted(f for f in set(files) if not _is_generated(f))
    return files[:limit], len(files) > limit


# ── parts (from the code graph's communities) ───────────────────────────

def _directory_parts(files: Sequence[str]) -> List[Dict[str, Any]]:
    groups: Dict[str, List[str]] = {}
    for f in files:
        segs = f.split("/")
        key = "/".join(segs[:2]) if len(segs) > 2 else (segs[0] if len(segs) > 1 else ".")
        groups.setdefault(key, []).append(f)
    return [{"id": "dir:" + key, "name": key if key != "." else "Top level", "purpose": "",
             "parent": "", "parent_name": "", "files": sorted(v), "test_files": []}
            for key, v in sorted(groups.items())]


def _is_generated(rel: str) -> bool:
    segs = rel.replace("\\", "/").split("/")
    return any(seg in _SKIP_DIRS for seg in segs[:-1]) or bool(re.search(r"\.min\.[a-z]+$|\.bundle\.js$", segs[-1]))


def collect_parts(root: str, project_id: str = "") -> Tuple[List[Dict[str, Any]], str]:
    """Level-0 communities with their level-1 group names; directory groups
    when the graph has nothing for this root. Returns (parts, method)."""
    parts: List[Dict[str, Any]] = []
    method = "directory"
    try:
        from .communities import communities as _communities
        lvl0 = _communities(root, project_id=project_id, level=0, min_files=0, output_chars=200)
        lvl1 = _communities(root, project_id=project_id, level=1, min_files=0, output_chars=200)
        names1 = {c["id"]: c.get("name", "") for c in (lvl1.get("communities") or [])}
        for c in lvl0.get("communities") or []:
            c = {**c, "files": [f for f in c.get("files") or [] if not _is_generated(f)]}
            if not c["files"]:
                continue
            parts.append({"id": c["id"], "name": c.get("name") or c["id"],
                          "purpose": c.get("summary") or c.get("purpose") or "",
                          "parent": c.get("parent") or "",
                          "parent_name": names1.get(c.get("parent") or "", ""),
                          "files": sorted(c["files"]), "test_files": sorted(c.get("test_files") or [])})
        if parts:
            method = lvl0.get("method") or "louvain"
    except Exception as exc:  # noqa: BLE001 - the map still renders by directory
        logger.info("floorplan: communities unavailable for %s: %s", root, exc)
    return parts, method


# ── cache ───────────────────────────────────────────────────────────────

def _cache_dir() -> str:
    if CACHE_DIR:
        return CACHE_DIR
    try:
        from src.constants import DATA_DIR
    except Exception:  # noqa: BLE001
        DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "data")
    return os.path.join(DATA_DIR, "code_floorplan")


def cache_key(root: str, project_id: str = "") -> str:
    norm = os.path.normcase(os.path.abspath(root))
    return hashlib.sha1(f"{norm}|{project_id}".encode("utf-8")).hexdigest()[:16]


def _cache_path(root: str, project_id: str = "") -> str:
    return os.path.join(_cache_dir(), cache_key(root, project_id) + ".json")


def load_cache(root: str, project_id: str = "") -> Dict[str, Any]:
    try:
        with open(_cache_path(root, project_id), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cache(root: str, project_id: str, data: Dict[str, Any]) -> None:
    path = _cache_path(root, project_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, path)


# ── model labels ────────────────────────────────────────────────────────

def _model_labeler(mode: str) -> Tuple[Optional[Labeler], str, str]:
    """(labeler, model name, reason when unavailable)."""
    if mode == "off":
        return None, "", "labels off"
    url = model = headers = None
    try:
        from src.endpoint_resolver import resolve_endpoint
        for prefix in ("utility", "default"):
            url, model, headers = resolve_endpoint(prefix, owner="")
            if url and model:
                break
    except Exception as exc:  # noqa: BLE001
        return None, "", f"no model endpoint ({exc})"
    if not url or not model:
        return None, "", "no utility or default model configured"
    if mode == "auto":
        try:
            from src.brain.extract import background_llm_gate
            reason = background_llm_gate(url, model)
        except Exception:  # noqa: BLE001
            reason = "residency_unknown"
        if reason:
            return None, str(model), f"model labels deferred ({reason}); run with labels='model'"

    def _call(prompt: str) -> str:
        from src import owned_loop
        from src.llm_core import llm_call_async
        raw = owned_loop.run(llm_call_async(
            url=url, model=model, messages=[{"role": "user", "content": prompt}],
            headers=headers, temperature=0.1, max_tokens=900, timeout=120,
            max_retries=1, workload="background"))
        if isinstance(raw, tuple):
            raw = raw[0]
        return str(raw or "")

    return _call, str(model), ""


def _parse_json_object(text: str) -> Dict[str, str]:
    text = re.sub(r"(?s)<think>.*?</think>", "", str(text or ""))
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): re.sub(r"\s+", " ", str(v)).strip()[:220] for k, v in data.items()
            if isinstance(v, (str, int, float)) and str(v).strip()}


_FILE_PROMPT = (
    "You write a floorplan of a software project for someone who does not read code.\n"
    "For each file below write ONE plain-language line (at most 18 words) saying what it does "
    "for the app or its users. No code terms unless unavoidable, no file names, do not invent "
    "anything the evidence does not show.\n"
    "Answer with ONLY a JSON object mapping each key to its line.\n\n")
_PART_PROMPT = (
    "You write a floorplan of a software project for someone who does not read code.\n"
    "Each item below is one PART of the app (a group of files) with what its files do. "
    "For each, write ONE plain-language line (at most 16 words) naming what this part is "
    "responsible for. Answer with ONLY a JSON object mapping each key to its line.\n\n")


def _file_item(info: Dict[str, Any]) -> str:
    bits = [f"key: {info['path']}"]
    if info.get("symbols"):
        bits.append("defines: " + ", ".join(info["symbols"][:12]))
    if info.get("doc"):
        bits.append("doc: " + info["doc"])
    if info.get("routes"):
        bits.append("serves: " + ", ".join(f"{r['method']} {r['path']}" for r in info["routes"][:6]))
    if info.get("requests"):
        bits.append("calls: " + ", ".join(f"{r['method']} {r['url']}" for r in info["requests"][:6]))
    bits.append("start of file:\n" + (info.get("_head") or ""))
    return "\n".join(bits)


def _label_batches(labeler: Labeler, prompt_head: str, items: List[Tuple[str, str]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for i in range(0, len(items), _BATCH):
        chunk = items[i:i + _BATCH]
        prompt = prompt_head + "\n\n---\n".join(text for _key, text in chunk)
        try:
            got = _parse_json_object(labeler(prompt))
        except Exception as exc:  # noqa: BLE001 - one failed batch keeps its fallbacks
            logger.info("floorplan: label batch failed: %s", exc)
            continue
        keys = {k for k, _t in chunk}
        out.update({k: v for k, v in got.items() if k in keys})
    return out


# ── generate / open ─────────────────────────────────────────────────────

def _resolve_root(root: str) -> str:
    from .query import _root
    return _root(root)


def _part_signature(files: Sequence[Dict[str, Any]]) -> str:
    joined = "\n".join(f"{f['path']}:{f['hash']}" for f in sorted(files, key=lambda f: f["path"]))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()


def generate(root: str = "", *, project_id: str = "", labels: str = "auto",
             max_labels: int = DEFAULT_MAX_LABELS, labeler: Optional[Labeler] = None,
             resolve: bool = True) -> Dict[str, Any]:
    """Build (or refresh) the floorplan for `root` and cache it.

    Only files whose content hash changed since the cached run, or that never
    had a model label, are sent to the model, at most `max_labels` per run;
    the rest keep their cached or deterministic line, so repeated runs
    converge without relabelling unchanged files."""
    resolved = _resolve_root(root) if resolve else os.path.abspath(root)
    mode = labels if labels in ("auto", "model", "off") else "auto"
    model_name, label_note = "", ""
    if labeler is None:
        labeler, model_name, label_note = _model_labeler(mode)
    else:
        model_name = "custom"
    try:
        budget = max(0, int(max_labels))
    except (TypeError, ValueError):
        budget = DEFAULT_MAX_LABELS

    parts, method = collect_parts(resolved, project_id)
    listed, truncated = list_code_files(resolved)
    in_parts = {f for p in parts for f in p["files"]}
    if not parts:
        parts = _directory_parts([f for f in listed if not is_test_path(f)])
        method = "directory"
    else:
        loose = [f for f in listed if f not in in_parts and not is_test_path(f)
                 and not any(f in p["test_files"] for p in parts)]
        if loose:
            extra = _directory_parts(loose)
            for p in extra:
                p["name"] = p["name"] + " (outside the graph)"
            parts.extend(extra)

    cache = load_cache(resolved, project_id)
    file_cache: Dict[str, Dict[str, Any]] = cache.get("files") or {}
    part_cache: Dict[str, Dict[str, Any]] = cache.get("parts") or {}

    infos: Dict[str, Dict[str, Any]] = {}
    for p in parts:
        for rel in p["files"]:
            if rel in infos or len(infos) >= MAX_FILES:
                continue
            info = scan_file(resolved, rel)
            if info:
                infos[rel] = info

    reused = 0
    todo: List[Dict[str, Any]] = []
    for rel, info in infos.items():
        hit = file_cache.get(rel)
        if hit and hit.get("hash") == info["hash"] and (hit.get("source") == "model" or labeler is None):
            info["label"], info["label_source"] = hit.get("text", ""), hit.get("source", "heuristic")
            if info["label_source"] != "model":
                info["label"] = heuristic_label(info)
                info["label_source"] = "docstring" if info.get("doc") else "heuristic"
            else:
                reused += 1
        else:
            todo.append(info)

    labelled_now = 0
    if labeler is not None and todo and budget > 0:
        batch = todo[:budget]
        got = _label_batches(labeler, _FILE_PROMPT, [(i["path"], _file_item(i)) for i in batch])
        for info in batch:
            if info["path"] in got:
                info["label"], info["label_source"] = got[info["path"]], "model"
                labelled_now += 1
        budget -= len(batch)
    pending = 0
    for info in todo:
        if info.get("label_source") != "model":
            info["label"] = heuristic_label(info)
            info["label_source"] = "docstring" if info.get("doc") else "heuristic"
            if labeler is not None or mode != "off":
                pending += 1

    out_parts: List[Dict[str, Any]] = []
    part_todo: List[Tuple[Dict[str, Any], str]] = []
    for p in parts:
        files = [infos[f] for f in p["files"] if f in infos]
        if not files:
            continue
        sig = _part_signature(files)
        rec = {"id": p["id"], "name": p["name"], "parent": p.get("parent", ""),
               "parent_name": p.get("parent_name", ""), "test_count": len(p.get("test_files") or []),
               "signature": sig, "files": files}
        hit = part_cache.get(p["id"])
        if hit and hit.get("signature") == sig and hit.get("source") == "model":
            rec["label"], rec["label_source"] = hit.get("text", ""), "model"
        else:
            rec["label"] = (p.get("purpose") or "").split(". ")[0].strip()[:200] or \
                f"{len(files)} file{'s' if len(files) != 1 else ''}: " + \
                "; ".join(f["label"] for f in files[:2])[:180]
            rec["label_source"] = "graph"
            part_todo.append((rec, sig))
        out_parts.append(rec)
    if labeler is not None and part_todo:
        items = [(rec["id"], f"key: {rec['id']}\nname: {rec['name']}\nfiles:\n" + "\n".join(
            f"- {f['label']}" for f in rec["files"][:12])) for rec, _sig in part_todo]
        got = _label_batches(labeler, _PART_PROMPT, items)
        for rec, _sig in part_todo:
            if rec["id"] in got:
                rec["label"], rec["label_source"] = got[rec["id"]], "model"

    out_parts.sort(key=lambda r: (r["parent_name"] or "~", -len(r["files"]), r["name"]))
    routes, requests = [], []
    for rec in out_parts:
        for f in rec["files"]:
            f.pop("_head", None)
            for r in f["routes"]:
                routes.append({**r, "file": f["path"], "part": rec["name"]})
            for r in f["requests"]:
                requests.append({**r, "file": f["path"], "part": rec["name"]})

    plan = {
        "root": resolved, "project_id": project_id, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "method": method, "model": model_name, "labels_mode": mode, "label_note": label_note,
        "truncated": truncated,
        "stats": {"parts": len(out_parts), "files": len(infos), "routes": len(routes),
                  "requests": len(requests), "labelled_now": labelled_now,
                  "reused_labels": reused, "pending_labels": pending},
        "parts": out_parts, "routes": routes, "requests": requests,
    }
    old_bp = (cache.get("plan") or {}).get("blueprint")
    if old_bp:
        plan["blueprint"] = old_bp
    new_files = {rel: {"hash": i["hash"], "text": i["label"], "source": i["label_source"]}
                 for rel, i in infos.items()}
    new_parts = {r["id"]: {"signature": r["signature"], "text": r["label"], "source": r["label_source"]}
                 for r in out_parts}
    _save_cache(resolved, project_id, {"root": resolved, "files": new_files, "parts": new_parts,
                                       "plan": plan})
    return {"output": render_text(plan), "exit_code": 0, "root": resolved, "plan": plan}


def open_plan(root: str = "", *, project_id: str = "", resolve: bool = True) -> Dict[str, Any]:
    resolved = _resolve_root(root) if resolve else os.path.abspath(root)
    plan = load_cache(resolved, project_id).get("plan")
    if not plan:
        return {"output": "no floorplan yet for this workspace; generate one first",
                "exit_code": 1, "root": resolved}
    return {"output": render_text(plan), "exit_code": 0, "root": resolved, "plan": plan}


def render_text(plan: Dict[str, Any], limit: int = _OUTPUT_CHARS) -> str:
    s = plan.get("stats") or {}
    lines = [f"Floorplan of {plan.get('root')}: {s.get('parts', 0)} parts, {s.get('files', 0)} files, "
             f"{s.get('routes', 0)} routes, {s.get('requests', 0)} outgoing requests "
             f"(labels: {s.get('labelled_now', 0)} new, {s.get('reused_labels', 0)} reused, "
             f"{s.get('pending_labels', 0)} without a model label yet)."]
    if plan.get("label_note"):
        lines.append(plan["label_note"])
    for p in plan.get("parts") or []:
        group = f" [{p['parent_name']}]" if p.get("parent_name") else ""
        lines.append(f"\n## {p['name']}{group} — {p.get('label', '')}")
        for f in p["files"][:6]:
            extra = ""
            if f["routes"]:
                extra = " · serves " + ", ".join(f"{r['method']} {r['path']}" for r in f["routes"][:3])
            lines.append(f"- {f['path']}: {f.get('label', '')}{extra}")
        if len(p["files"]) > 6:
            lines.append(f"- … {len(p['files']) - 6} more files")
    bp = plan.get("blueprint")
    if bp:
        lines.append("\n## Blueprint: " + ", ".join(f"{k} {v}" for k, v in (bp.get("summary") or {}).items()))
    text = "\n".join(lines)
    return text if len(text) <= limit else text[:limit] + f"\n… truncated ({len(text)} chars)"


# ── blueprint (PRD/goal → planned parts, marked against the floorplan) ──

_STOP = set("""a an the and or of to for in on at by with from into is are be as it its this that these those
we you they our your their will should must can could would may might not no all any each every some such
using use used via per than then also just only more most new add adds make makes made have has had do does
el la los las un una unos unas y o de del al en con por para que se su sus es son ser como más mas sin sobre
cada este esta estos estas ese esa lo le les nos hay tener hacer debe deben puede pueden muy ya""".split())


def _split_ident(text: str) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(text or ""))
    return re.sub(r"[_\-./\\:]+", " ", text)


def tokens(text: str) -> List[str]:
    out: List[str] = []
    for w in re.findall(r"[a-záéíóúñü0-9]+", _split_ident(text).lower()):
        if len(w) < 3 or w in _STOP or w.isdigit():
            continue
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        if w not in out:
            out.append(w)
    return out


def plan_parts_from_text(goal: str) -> List[Dict[str, Any]]:
    """Deterministic: one planned part per heading or list item; plain
    sentences when the text has neither."""
    items: List[str] = []
    lines = [raw.strip() for raw in str(goal or "").splitlines()]
    has_list = any(re.match(r"^(?:[-*+]|\d+[.)])\s+", ln) for ln in lines)
    for line in lines:
        if has_list and re.match(r"^#\s+", line):
            continue  # the document title, not a part, when the text lists its parts
        m = re.match(r"^(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+)(.+)$", line)
        if m:
            text = m.group(1).strip(" :*_`")
            if len(text) >= 3:
                items.append(text)
    if not items:
        items = [s.strip() for s in re.split(r"(?<=[.!?;])\s+", str(goal or "")) if len(s.strip()) >= 3]
    out = []
    for text in items[:40]:
        name = re.split(r"[:—–(]|\s-\s", text, maxsplit=1)[0].strip()[:80] or text[:80]
        kw = tokens(text)
        if kw:
            out.append({"name": name, "description": text[:240], "keywords": kw[:12]})
    return out


_BLUEPRINT_PROMPT = (
    "Read this product requirements / goal text and list the PARTS the software needs "
    "(features, screens, services, integrations, data stores). For each give a short name, one "
    "plain line saying what it does, and 3 to 8 lowercase keywords a developer would use in file "
    "or function names for it (English, plus the text's own language if different).\n"
    "Answer with ONLY a JSON object {\"parts\": [{\"name\": ..., \"description\": ..., "
    "\"keywords\": [...]}]}.\n\nTEXT:\n")


def _model_planned_parts(labeler: Labeler, goal: str) -> List[Dict[str, Any]]:
    raw = re.sub(r"(?s)<think>.*?</think>", "", labeler(_BLUEPRINT_PROMPT + goal[:8000]) or "")
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(raw[start:end + 1])
    except ValueError:
        return []
    out = []
    for item in (data.get("parts") if isinstance(data, dict) else None) or []:
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            continue
        kw: List[str] = []
        for k in list(item.get("keywords") or []) + [item.get("name", "")]:
            for t in tokens(str(k)):
                if t not in kw:
                    kw.append(t)
        out.append({"name": str(item["name"]).strip()[:80],
                    "description": str(item.get("description") or "").strip()[:240],
                    "keywords": kw[:12]})
    return out[:40]


def _part_tokens(part: Dict[str, Any]) -> set:
    bag = [part.get("name", ""), part.get("label", ""), part.get("parent_name", "")]
    for f in part.get("files") or []:
        bag.append(f["path"])
        bag.append(f.get("label", ""))
        bag.extend(f.get("symbols") or [])
        bag.extend(r["path"] for r in f.get("routes") or [])
    return set(tokens(" ".join(bag)))


def _best_cover(kw: Sequence[str], part: Dict[str, Any]) -> float:
    if not kw:
        return 0.0
    places = [set(tokens(" ".join([part.get("name", ""), part.get("label", "")])))]
    for f in part.get("files") or []:
        places.append(set(tokens(" ".join([f["path"], f.get("label", ""), *(f.get("symbols") or []),
                                           *(r["path"] for r in f.get("routes") or [])]))))
    return max(sum(1 for k in kw if k in place) / len(kw) for place in places)


_JUDGE_PROMPT = (
    "You compare a plan with an existing app. For each PLANNED part below decide whether the app "
    "already has it, judging only from the descriptions of the existing parts listed under it.\n"
    "status: exists (clearly implemented), partial (something related but incomplete), missing.\n"
    "Answer with ONLY a JSON object mapping each key to {\"status\": ..., \"part\": <existing part "
    "name or empty>, \"why\": <max 15 words>}.\n\n")


def _model_judge(labeler: Labeler, marked: List[Dict[str, Any]], plan: Dict[str, Any]) -> int:
    """Let the model confirm or correct each status from the parts' plain
    descriptions; returns how many it judged. Deterministic marks stay when
    the model gives no usable answer."""
    by_name = {p["name"]: p for p in plan.get("parts") or []}
    ranked = sorted(plan.get("parts") or [], key=lambda p: -len(p["files"]))
    blocks = []
    for i, m in enumerate(marked):
        cands = [by_name[x["part"]] for x in m["matches"] if x["part"] in by_name] or ranked[:3]
        lines = [f"key: P{i}", f"planned: {m['name']} — {m.get('description', '')}"]
        for c in cands[:4]:
            lines.append(f"- existing part '{c['name']}': {c.get('label', '')}")
            lines.extend(f"    · {f.get('label', '')}" for f in c["files"][:6])
        blocks.append("\n".join(lines))
    raw = re.sub(r"(?s)<think>.*?</think>", "", labeler(_JUDGE_PROMPT + "\n\n".join(blocks)) or "")
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return 0
    try:
        data = json.loads(raw[start:end + 1])
    except ValueError:
        return 0
    judged = 0
    for i, m in enumerate(marked):
        ans = data.get(f"P{i}") if isinstance(data, dict) else None
        if not isinstance(ans, dict) or ans.get("status") not in ("exists", "partial", "missing"):
            continue
        m["status"] = ans["status"]
        m["judged_by"] = "model"
        m["why"] = str(ans.get("why") or "")[:160]
        part = str(ans.get("part") or "")
        if part in by_name and not any(x["part"] == part for x in m["matches"]):
            p = by_name[part]
            m["matches"].insert(0, {"part": part, "part_id": p["id"], "score": m["score"], "keywords": [],
                                    "files": [f["path"] for f in p["files"][:5]]})
        judged += 1
    return judged


def match_blueprint(planned: List[Dict[str, Any]], plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Mark each planned part exists / partial / missing by keyword overlap
    with the current parts (deterministic; evidence = the matching parts)."""
    import math
    part_toks = [(p, _part_tokens(p)) for p in plan.get("parts") or []]
    n_parts = max(1, len(part_toks))
    df: Dict[str, int] = {}
    for _p, toks in part_toks:
        for tok in toks:
            df[tok] = df.get(tok, 0) + 1

    def weight(tok: str) -> float:
        # A keyword found in every part says little; one found nowhere weighs most.
        return math.log(1.0 + n_parts / (1.0 + df.get(tok, 0)))

    out = []
    for item in planned:
        kw = item.get("keywords") or tokens(item.get("name", ""))
        total = sum(weight(k) for k in kw) or 1.0
        scored = []
        for p, toks in part_toks:
            hit = [k for k in kw if k in toks]
            if hit:
                scored.append((sum(weight(k) for k in hit) / total, p, hit))
        scored.sort(key=lambda x: (-x[0], x[1]["name"]))
        best = scored[0][0] if scored else 0.0
        matched_kw = sorted({k for _s, _p, hit in scored[:3] for k in hit})
        # "exists" also needs most keywords together in ONE place (a file, or
        # the part's own name/label), not scattered across a large part.
        together = _best_cover(kw, scored[0][1]) if scored else 0.0
        status = ("exists" if best >= 0.6 and together >= 0.66 else
                  "partial" if best >= 0.25 else "missing")
        out.append({**item, "status": status, "score": round(best, 3), "matched_keywords": matched_kw,
                    "matches": [{"part": p["name"], "part_id": p["id"], "score": round(s, 3),
                                 "keywords": hit, "files": [f["path"] for f in p["files"][:5]]}
                                for s, p, hit in scored[:3]]})
    return out


def blueprint(root: str = "", goal: str = "", *, project_id: str = "", labels: str = "auto",
              labeler: Optional[Labeler] = None, resolve: bool = True) -> Dict[str, Any]:
    """Planned parts from a PRD/goal text, each marked against the floorplan."""
    goal = str(goal or "").strip()
    if not goal:
        return {"error": "blueprint: goal text is required", "exit_code": 1}
    resolved = _resolve_root(root) if resolve else os.path.abspath(root)
    cache = load_cache(resolved, project_id)
    plan = cache.get("plan")
    if not plan:
        plan = generate(resolved, project_id=project_id, labels="off", resolve=False)["plan"]
        cache = load_cache(resolved, project_id)
    source = "text"
    if labeler is None:
        labeler, _model, _note = _model_labeler(labels if labels in ("auto", "model", "off") else "auto")
    planned: List[Dict[str, Any]] = []
    if labeler is not None:
        try:
            planned = _model_planned_parts(labeler, goal)
            source = "model" if planned else "text"
        except Exception as exc:  # noqa: BLE001
            logger.info("floorplan: blueprint model call failed: %s", exc)
    if not planned:
        planned = plan_parts_from_text(goal)
    marked = match_blueprint(planned, plan)
    if labeler is not None and marked:
        try:
            if _model_judge(labeler, marked, plan):
                source = source + "+model-judge"
        except Exception as exc:  # noqa: BLE001
            logger.info("floorplan: blueprint judge failed: %s", exc)
    summary = {k: sum(1 for m in marked if m["status"] == k) for k in ("exists", "partial", "missing")}
    bp = {"goal": goal[:4000], "source": source, "summary": summary, "parts": marked,
          "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    plan["blueprint"] = bp
    cache["plan"] = plan
    _save_cache(resolved, project_id, cache)
    lines = [f"Blueprint ({source}): {summary['exists']} exist, {summary['partial']} partial, "
             f"{summary['missing']} missing."]
    for m in marked:
        where = f" → {m['matches'][0]['part']}" if m["matches"] else ""
        lines.append(f"- [{m['status']}] {m['name']}{where}")
    return {"output": "\n".join(lines)[:_OUTPUT_CHARS], "exit_code": 0, "root": resolved,
            "blueprint": bp}


# ── self-contained HTML export ──────────────────────────────────────────

_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Floorplan · __TITLE__</title>
<style>
:root{--bg:#f6f5f2;--panel:#fff;--ink:#1d1d1b;--muted:#6b6a66;--line:#e2e0da;--accent:#2f5d8a;--get:#2f7a4f;--post:#8a5a14;--del:#a33a2a;--chip:#efede8}
@media (prefers-color-scheme:dark){:root{--bg:#151514;--panel:#1d1d1c;--ink:#ecebe6;--muted:#9c9a93;--line:#33322f;--accent:#8fb4d9;--get:#7cc39a;--post:#d9a75a;--del:#e08a7c;--chip:#2a2927}}
*{box-sizing:border-box}html,body{margin:0;overflow-x:clip}
body{background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
header{position:sticky;top:0;z-index:2;background:var(--bg);border-bottom:1px solid var(--line);padding:14px 20px}
h1{font-size:18px;margin:0 0 4px;font-weight:600}.meta{color:var(--muted);font-size:13px;overflow-wrap:anywhere}
.bar{display:flex;gap:10px;margin-top:10px;flex-wrap:wrap}
input[type=search]{flex:1;min-width:220px;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--ink);font:inherit}
button{padding:8px 12px;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--ink);font:inherit;cursor:pointer}
button:focus-visible,input:focus-visible,a:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
main{max-width:1180px;margin:0 auto;padding:16px 20px 60px}
.group{margin:22px 0 8px;color:var(--muted);font-size:13px}
details.part{background:var(--panel);border:1px solid var(--line);border-radius:10px;margin:8px 0}
details.part>summary{cursor:pointer;padding:12px 14px;list-style:none;display:flex;gap:12px;align-items:baseline}
details.part>summary::-webkit-details-marker{display:none}
.pname{font-weight:600}.plabel{color:var(--muted);flex:1;min-width:0}.count{color:var(--muted);font-size:13px;white-space:nowrap}
ul.files{list-style:none;margin:0;padding:0 14px 10px}
ul.files li{border-top:1px solid var(--line);padding:8px 0;display:grid;grid-template-columns:minmax(0,1fr);gap:2px}
a.path{color:var(--accent);text-decoration:none;font-family:ui-monospace,Consolas,monospace;font-size:13px;overflow-wrap:anywhere}
a.path:hover{text-decoration:underline}.flabel{font-size:14px}
.tags{display:flex;flex-wrap:wrap;gap:6px;margin-top:2px}
.tag{background:var(--chip);border-radius:6px;padding:1px 7px;font-family:ui-monospace,Consolas,monospace;font-size:12px;overflow-wrap:anywhere}
.m{font-weight:600}.m-GET{color:var(--get)}.m-POST,.m-PUT,.m-PATCH{color:var(--post)}.m-DELETE{color:var(--del)}
.src{color:var(--muted);font-size:12px}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{text-align:left;padding:8px 10px;border-top:1px solid var(--line);vertical-align:top;font-size:14px}
th{font-weight:600;color:var(--muted);font-size:13px}
.st{border-radius:6px;padding:1px 8px;font-size:12px;white-space:nowrap}.st-exists{background:#d9eedf;color:#1f5a35}.st-partial{background:#f5e6c8;color:#6b4710}.st-missing{background:#f3d7d2;color:#7a2a1d}
.hidden{display:none!important}.empty{color:var(--muted);padding:20px 0}
</style></head><body>
<header><h1>Floorplan</h1><div class="meta" id="meta"></div>
<div class="bar"><input type="search" id="q" placeholder="Search parts, files, routes, requests" aria-label="Search the floorplan">
<button type="button" id="expand">Expand all</button><button type="button" id="collapse">Collapse all</button></div></header>
<main><section id="bp"></section><section id="parts"></section><p class="empty hidden" id="none">Nothing matches.</p></main>
<script id="data" type="application/json">__DATA__</script>
<script>
(function(){
var plan=JSON.parse(document.getElementById('data').textContent);var base=__BASE__;
function el(t,c,txt){var e=document.createElement(t);if(c)e.className=c;if(txt!=null)e.textContent=txt;return e}
function link(path){var a=el('a','path',path);a.href=base+'/studio?workspace='+encodeURIComponent(plan.root)+'&file='+encodeURIComponent(path);a.target='_blank';a.rel='noopener';a.title='Open in Studio';return a}
var s=plan.stats||{};document.getElementById('meta').textContent=plan.root+' · '+s.parts+' parts · '+s.files+' files · '+s.routes+' routes · '+s.requests+' outgoing requests · generated '+plan.generated_at+(plan.model?' · labels by '+plan.model:'');
var bp=plan.blueprint;if(bp&&bp.parts&&bp.parts.length){var sec=document.getElementById('bp');sec.appendChild(el('div','group','Blueprint · '+bp.summary.exists+' exist · '+bp.summary.partial+' partial · '+bp.summary.missing+' missing'));
var tb=el('table');var hr=el('tr');['Planned part','Status','Where it lives'].forEach(function(h){hr.appendChild(el('th',null,h))});tb.appendChild(hr);
bp.parts.forEach(function(p){var tr=el('tr');tr.dataset.q=(p.name+' '+p.description+' '+p.keywords.join(' ')).toLowerCase();var a=el('td');a.appendChild(el('div','pname',p.name));a.appendChild(el('div','src',p.description));tr.appendChild(a);
var b=el('td');b.appendChild(el('span','st st-'+p.status,p.status));tr.appendChild(b);var c=el('td');(p.matches||[]).slice(0,2).forEach(function(m){c.appendChild(el('div',null,m.part+' ('+Math.round(m.score*100)+'%)'));(m.files||[]).slice(0,2).forEach(function(f){c.appendChild(link(f));c.appendChild(el('br'))})});tr.appendChild(c);tb.appendChild(tr)});sec.appendChild(tb)}
var root=document.getElementById('parts');var lastGroup=null;var rows=[];
(plan.parts||[]).forEach(function(p){var g=p.parent_name||'Other parts';if(g!==lastGroup){root.appendChild(el('div','group',g));lastGroup=g}
var d=el('details','part');var sm=el('summary');sm.appendChild(el('span','pname',p.name));sm.appendChild(el('span','plabel',p.label||''));sm.appendChild(el('span','count',p.files.length+' files'+(p.test_count?' · '+p.test_count+' tests':'')));d.appendChild(sm);
var ul=el('ul','files');p.files.forEach(function(f){var li=el('li');li.appendChild(link(f.path));li.appendChild(el('div','flabel',f.label||''));var tags=el('div','tags');
(f.routes||[]).forEach(function(r){var t=el('span','tag');var m=el('span','m m-'+r.method.split('/')[0],r.method);t.appendChild(m);t.appendChild(document.createTextNode(' '+r.path));t.title='serves (line '+r.line+')';tags.appendChild(t)});
(f.requests||[]).forEach(function(r){var t=el('span','tag');t.appendChild(document.createTextNode('→ '));var m=el('span','m m-'+r.method,r.method);t.appendChild(m);t.appendChild(document.createTextNode(' '+r.url));t.title='calls (line '+r.line+')';tags.appendChild(t)});
if(tags.childNodes.length)li.appendChild(tags);li.dataset.q=(f.path+' '+(f.label||'')+' '+(f.symbols||[]).join(' ')+' '+(f.routes||[]).map(function(r){return r.method+' '+r.path}).join(' ')+' '+(f.requests||[]).map(function(r){return r.url}).join(' ')).toLowerCase();ul.appendChild(li)});
d.appendChild(ul);d.dataset.q=(p.name+' '+(p.label||'')+' '+g).toLowerCase();root.appendChild(d);rows.push(d)});
function filter(){var q=document.getElementById('q').value.trim().toLowerCase();var any=false;rows.forEach(function(d){var own=!q||d.dataset.q.indexOf(q)>=0;var hits=0;d.querySelectorAll('li').forEach(function(li){var ok=own||li.dataset.q.indexOf(q)>=0;li.classList.toggle('hidden',!ok);if(ok)hits++});var show=own||hits>0;d.classList.toggle('hidden',!show);if(q&&show)d.open=true;if(show)any=true});
document.querySelectorAll('#bp tr[data-q]').forEach(function(tr){tr.classList.toggle('hidden',!!q&&tr.dataset.q.indexOf(q)<0)});document.getElementById('none').classList.toggle('hidden',any)}
document.getElementById('q').addEventListener('input',filter);document.getElementById('expand').onclick=function(){rows.forEach(function(d){d.open=true})};document.getElementById('collapse').onclick=function(){rows.forEach(function(d){d.open=false})};
})();
</script></body></html>
"""


def export_html(plan: Dict[str, Any], *, studio_base: str = "", nonce: str = "") -> str:
    """One HTML file with the plan embedded; no network, no external assets.
    `nonce` marks the inline scripts when the page is served under a CSP
    that only runs nonced scripts (the app's own server)."""
    data = json.dumps(plan, ensure_ascii=False).replace("</", "<\\/").replace("<!--", "<\\!--")
    title = os.path.basename(str(plan.get("root") or "").rstrip("/\\")) or "workspace"
    title = re.sub(r"[<>&\"']", "", title)
    page = (_HTML.replace("__TITLE__", title)
            .replace("__BASE__", json.dumps(str(studio_base or "").rstrip("/")))
            .replace("__DATA__", data))
    safe_nonce = re.sub(r"[^A-Za-z0-9+/=_-]", "", str(nonce or ""))
    if safe_nonce:
        page = page.replace("<script id=\"data\"", f"<script nonce=\"{safe_nonce}\" id=\"data\"", 1)
        page = page.replace("<script>\n(function(){", f"<script nonce=\"{safe_nonce}\">\n(function(){{", 1)
    return page


def export(root: str = "", *, project_id: str = "", studio_base: str = "",
           resolve: bool = True, nonce: str = "") -> Dict[str, Any]:
    """Write the HTML export next to the cache (never into the workspace)."""
    opened = open_plan(root, project_id=project_id, resolve=resolve)
    if opened.get("exit_code") != 0:
        return opened
    html_text = export_html(opened["plan"], studio_base=studio_base)
    path = os.path.join(_cache_dir(), cache_key(opened["root"], project_id) + ".html")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html_text)
    if nonce:
        html_text = export_html(opened["plan"], studio_base=studio_base, nonce=nonce)
    return {"output": f"Floorplan exported to {path} ({len(html_text)} bytes)", "exit_code": 0,
            "root": opened["root"], "path": path, "bytes": len(html_text), "html": html_text}


__all__ = ["generate", "open_plan", "export", "export_html", "blueprint", "scan_file",
           "extract_routes", "extract_requests", "plan_parts_from_text", "match_blueprint",
           "heuristic_label", "tokens", "CACHE_DIR"]
