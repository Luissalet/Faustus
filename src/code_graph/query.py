"""src/code_graph/query.py — architecture, tracing and change-impact queries
over `src.context_engine.code_index`'s resolved graph.

Every public function here: (1) confines `root`/`workspace` to the active
workspace the same way grep/glob/find_symbol do
(`src.tool_execution._resolve_search_root` — outside it raises `ValueError`,
which the tool layer turns into `{"error": ..., "exit_code": 1}`); (2)
refreshes the index for that root before answering, so the answer reflects
the files on disk right now, not a stale snapshot; (3) never raises past its
own boundary — a query that hits a sqlite error returns an empty/short
answer rather than crashing the tool call; (4) keeps its rendered `output`
under a small character budget (default 4000, `limit`-tunable) because the
entire point of a code graph is spending fewer tokens than reading files.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.context_engine import code_index
from src.context_engine import store as ce_store

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_CHARS = 4000
_DIFF_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _root(raw: str) -> str:
    """Confine to the active workspace, same guard `read_file` uses."""
    from src.tool_execution import _resolve_search_root
    return _resolve_search_root(raw or "")


def _clip(text: str, limit: Optional[int]) -> str:
    text = text or ""
    cap = int(limit) if limit else DEFAULT_OUTPUT_CHARS
    if cap <= 0 or len(text) <= cap:
        return text
    return text[:cap] + f"\n… truncated ({len(text)} chars total, limit {cap})"


def index(root: str = "", *, force: bool = False, project_id: str = "") -> Dict[str, Any]:
    """Bring the graph for `root` up to date (incremental unless `force`)."""
    resolved = _root(root)
    result = code_index.refresh(resolved, project_id=project_id, full=bool(force))
    result["root"] = resolved
    lines = [
        f"scanned {result['scanned']}, reindexed {result['reindexed']}, "
        f"removed {result['removed']} — {result['symbols']} symbols, "
        f"{result['edges']} edges" + (" (truncated)" if result.get("truncated") else ""),
    ]
    result["output"] = "\n".join(lines)
    result["exit_code"] = 0
    return result


def _resolve_symbol(name: str, *, root: str, project_id: str) -> Optional[Dict[str, Any]]:
    """Best match for a symbol name/qualname — exact wins, else top search hit."""
    name = (name or "").strip()
    if not name:
        return None
    try:
        hits = code_index.search(name, workspace=root, project_id=project_id, k=8)
    except Exception as exc:  # noqa: BLE001
        logger.debug("code_graph: resolve(%s) failed: %s", name, exc)
        return None
    if not hits:
        return None
    low = name.lower()
    for hit in hits:
        qual = str(hit.get("qualname") or "")
        if qual.lower() == low or qual.rsplit(".", 1)[-1].lower() == low:
            return hit
    return hits[0]


def _fmt_symbol_row(h: Dict[str, Any]) -> str:
    sig = h.get("signature") or ""
    return f"{h.get('path', '')}:{h.get('start_line', 0)}-{h.get('end_line', 0)}  " \
           f"{h.get('kind', ''):<8} {h.get('qualname', '')}" + (f"  {sig}" if sig else "")


# ── search ───────────────────────────────────────────────────────────────

def search_graph(pattern: str, *, kinds: Sequence[str] = (), limit: int = 40,
                  workspace: str = "", project_id: str = "",
                  output_chars: int = DEFAULT_OUTPUT_CHARS) -> Dict[str, Any]:
    root = _root(workspace)
    code_index.refresh(root, project_id=project_id)
    try:
        k = max(1, min(int(limit or 40), 200))
    except (TypeError, ValueError):
        k = 40
    hits = code_index.search(pattern, workspace=root, project_id=project_id,
                             k=k, kinds=tuple(kinds or ()))
    text = "\n".join(_fmt_symbol_row(h) for h in hits) or f"no matches for {pattern!r} under {root}"
    return {"output": _clip(text, output_chars), "exit_code": 0, "matches": hits, "root": root}


# ── trace_path ───────────────────────────────────────────────────────────

def trace_path(from_symbol: str, to_symbol: str, *, workspace: str = "",
                project_id: str = "", max_depth: int = 5,
                output_chars: int = DEFAULT_OUTPUT_CHARS) -> Dict[str, Any]:
    root = _root(workspace)
    code_index.refresh(root, project_id=project_id)
    src = _resolve_symbol(from_symbol, root=root, project_id=project_id)
    dst = _resolve_symbol(to_symbol, root=root, project_id=project_id)
    if not src or not dst:
        missing = "from" if not src else "to"
        return {"output": f"could not resolve the {missing!r} symbol", "exit_code": 1,
                "found": False, "root": root}
    if src["id"] == dst["id"]:
        return {"output": f"{src['qualname']} is the same symbol", "exit_code": 0,
                "found": True, "path": [_fmt_symbol_row(src)], "root": root}
    try:
        depth_cap = max(1, min(int(max_depth or 5), 8))
    except (TypeError, ValueError):
        depth_cap = 5

    start_id, target_id = src["id"], dst["id"]
    parent: Dict[str, Tuple[str, str, str]] = {}
    visited = {start_id}
    frontier = [start_id]
    found = False
    try:
        with ce_store.db() as conn:
            depth = 0
            while frontier and depth < depth_cap and not found:
                depth += 1
                marks = ",".join("?" * len(frontier))
                rows = conn.execute(
                    f"SELECT src, dst, kind, certainty FROM code_edges "
                    f"WHERE src IN ({marks}) AND kind IN ('calls', 'imports') "
                    f"AND workspace = ?", [*frontier, root]).fetchall()
                nxt: List[str] = []
                for row in rows:
                    dst_id = str(row["dst"])
                    if dst_id in visited:
                        continue
                    visited.add(dst_id)
                    parent[dst_id] = (str(row["src"]), str(row["kind"]), str(row["certainty"]))
                    if dst_id == target_id:
                        found = True
                        break
                    nxt.append(dst_id)
                frontier = nxt
            if not found:
                return {"output": f"no call/import path from {src['qualname']} to "
                                   f"{dst['qualname']} within {depth_cap} hops",
                        "exit_code": 0, "found": False, "root": root}
            chain = [target_id]
            cur = target_id
            while cur != start_id:
                prev, _kind, _cert = parent[cur]
                chain.append(prev)
                cur = prev
            chain.reverse()
            marks = ",".join("?" * len(chain))
            info_rows = conn.execute(
                f"SELECT id, qualname, kind, path, start_line FROM code_symbols "
                f"WHERE id IN ({marks})", chain).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("code_graph.trace_path failed: %s", exc)
        return {"output": f"trace failed: {exc}", "exit_code": 1, "found": False, "root": root}

    info = {str(r["id"]): dict(r) for r in info_rows}
    nodes: List[Dict[str, Any]] = []
    for i, sid in enumerate(chain):
        row = info.get(sid, {"qualname": sid, "kind": "?", "path": "", "start_line": 0})
        entry: Dict[str, Any] = {
            "symbol": row["qualname"], "kind": row["kind"],
            "location": f"{row['path']}:{row['start_line']}",
        }
        if i > 0:
            _prev, ekind, ecert = parent[sid]
            entry["via"] = ekind
            entry["certainty"] = ecert
        nodes.append(entry)
    lines = []
    for n in nodes:
        via = f"  --{n['via']}[{n['certainty']}]-->" if "via" in n else ""
        lines.append(f"{n['symbol']} ({n['kind']}) {n['location']}{via}")
    return {"output": _clip("\n".join(lines), output_chars), "exit_code": 0,
            "found": True, "path": nodes, "hops": len(nodes) - 1, "root": root}


# ── callers / callees ───────────────────────────────────────────────────

def _direct(symbol: str, *, workspace: str, project_id: str, direction: str,
            limit: int, output_chars: int) -> Dict[str, Any]:
    root = _root(workspace)
    code_index.refresh(root, project_id=project_id)
    sym = _resolve_symbol(symbol, root=root, project_id=project_id)
    if not sym:
        return {"output": f"symbol not found: {symbol!r} under {root}", "exit_code": 1,
                "hits": [], "root": root}
    hops = code_index.neighbors(sym["id"], kinds=("calls",))
    try:
        cap = max(1, min(int(limit or 40), 500))
    except (TypeError, ValueError):
        cap = 40
    picked = [h for h in hops if h.get("direction") == direction][:cap]
    lines = []
    for h in picked:
        if h.get("resolved"):
            lines.append(f"{h['path']}:{h['start_line']} {h['qualname']} [{h['certainty']}]")
        else:
            lines.append(f"(unresolved) {h.get('symbol_id', '')}")
    text = "\n".join(lines) or f"no {'callers' if direction == 'in' else 'callees'} found for {sym['qualname']}"
    return {"output": _clip(text, output_chars), "exit_code": 0, "hits": picked,
            "symbol": sym["qualname"], "root": root}


def callers(symbol: str, *, workspace: str = "", project_id: str = "", limit: int = 40,
            output_chars: int = DEFAULT_OUTPUT_CHARS) -> Dict[str, Any]:
    return _direct(symbol, workspace=workspace, project_id=project_id, direction="in",
                   limit=limit, output_chars=output_chars)


def callees(symbol: str, *, workspace: str = "", project_id: str = "", limit: int = 40,
            output_chars: int = DEFAULT_OUTPUT_CHARS) -> Dict[str, Any]:
    return _direct(symbol, workspace=workspace, project_id=project_id, direction="out",
                   limit=limit, output_chars=output_chars)


# ── detect_changes ──────────────────────────────────────────────────────

def _parse_unified_diff(diff_text: str) -> Dict[str, List[Tuple[int, int]]]:
    """`{path: [(start_line, end_line), ...]}` of the NEW file's changed hunks."""
    changed: Dict[str, List[Tuple[int, int]]] = {}
    cur: Optional[str] = None
    for line in (diff_text or "").splitlines():
        if line.startswith("+++ "):
            raw = line[4:].strip()
            if raw == "/dev/null":
                cur = None
                continue
            cur = raw[2:] if raw.startswith("b/") else raw
            changed.setdefault(cur, [])
            continue
        if cur and line.startswith("@@"):
            m = _DIFF_HUNK_RE.match(line)
            if not m:
                continue
            start = int(m.group(1))
            length = int(m.group(2) or "1")
            if length <= 0:
                # a pure deletion hunk reports 0 new lines; still flag the
                # insertion point so the symbol surrounding it is caught.
                length = 1
            changed[cur].append((start, start + length - 1))
    return {p: ranges for p, ranges in changed.items() if ranges}


def detect_changes(root: str = "", *, base_ref: str = "HEAD", project_id: str = "",
                    output_chars: int = DEFAULT_OUTPUT_CHARS) -> Dict[str, Any]:
    resolved = _root(root)
    git = shutil.which("git")
    if not git:
        return {"output": "git is not available on PATH", "exit_code": 1, "changed_symbols": []}
    try:
        proc = subprocess.run(
            [git, "diff", "--unified=0", "--no-color", str(base_ref or "HEAD")],
            cwd=resolved, capture_output=True, text=True, timeout=30, shell=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": f"git diff failed: {exc}", "exit_code": 1, "changed_symbols": []}
    if proc.returncode not in (0, 1):
        return {"error": (proc.stderr or "git diff failed").strip()[:2000], "exit_code": 1,
                "changed_symbols": []}

    changed = _parse_unified_diff(proc.stdout)
    if not changed:
        return {"output": f"no changes vs {base_ref}", "exit_code": 0, "changed_symbols": [],
                "base_ref": base_ref, "root": resolved}
    code_index.refresh(resolved, project_id=project_id, paths=list(changed.keys()))

    results: List[Dict[str, Any]] = []
    for path, ranges in changed.items():
        for sym in code_index.symbols_in(path, workspace=resolved, project_id=project_id):
            if sym.kind == "module":
                continue
            if not any(not (sym.end_line < s or sym.start_line > e) for s, e in ranges):
                continue
            hops = code_index.neighbors(sym.id, kinds=("calls",))
            caller_lines = [f"{h['path']}:{h['start_line']} {h['qualname']}"
                            for h in hops if h.get("direction") == "in" and h.get("resolved")][:10]
            results.append({
                "symbol": sym.qualname, "kind": sym.kind, "path": sym.path,
                "start_line": sym.start_line, "end_line": sym.end_line,
                "callers": caller_lines,
            })

    lines = []
    for r in results:
        tail = f"  <- callers: {', '.join(r['callers'])}" if r["callers"] else ""
        lines.append(f"{r['path']}:{r['start_line']}-{r['end_line']} {r['kind']} {r['symbol']}{tail}")
    text = "\n".join(lines) or "diff touched no indexed symbol"
    return {"output": _clip(text, output_chars), "exit_code": 0, "changed_symbols": results,
            "base_ref": base_ref, "root": resolved}


# ── get_architecture ────────────────────────────────────────────────────

def get_architecture(root: str = "", *, project_id: str = "",
                      output_chars: int = DEFAULT_OUTPUT_CHARS) -> Dict[str, Any]:
    resolved = _root(root)
    code_index.refresh(resolved, project_id=project_id)
    status = code_index.status(resolved, project_id=project_id)

    routes: List[Dict[str, Any]] = []
    top_fan_in: List[Dict[str, Any]] = []
    top_fan_out: List[Dict[str, Any]] = []
    hotspots: List[Dict[str, Any]] = []
    try:
        with ce_store.db() as conn:
            route_rows = conn.execute(
                "SELECT qualname, signature, path, start_line FROM code_symbols "
                "WHERE workspace = ? AND kind = 'route' ORDER BY path LIMIT 60",
                (resolved,)).fetchall()
            routes = [{"route": r["signature"] or r["qualname"], "path": r["path"],
                      "line": r["start_line"]} for r in route_rows]

            fanin_rows = conn.execute(
                "SELECT dst, COUNT(*) AS n FROM code_edges WHERE workspace = ? "
                "GROUP BY dst ORDER BY n DESC LIMIT 20", (resolved,)).fetchall()
            fanout_rows = conn.execute(
                "SELECT src, COUNT(*) AS n FROM code_edges WHERE workspace = ? "
                "GROUP BY src ORDER BY n DESC LIMIT 20", (resolved,)).fetchall()
            fanin_by_id = {str(r["dst"]): int(r["n"]) for r in fanin_rows}
            ids = set(fanin_by_id) | {str(r["src"]) for r in fanout_rows}
            info: Dict[str, Any] = {}
            if ids:
                marks = ",".join("?" * len(ids))
                for row in conn.execute(
                        f"SELECT id, qualname, path, kind FROM code_symbols WHERE id IN ({marks})",
                        list(ids)):
                    info[str(row["id"])] = dict(row)
            top_fan_in = [
                {"symbol": info[sid]["qualname"], "path": info[sid]["path"], "callers": n}
                for sid, n in fanin_by_id.items() if sid in info
            ]
            top_fan_in.sort(key=lambda e: -e["callers"])
            top_fan_out = [
                {"symbol": info[str(r["src"])]["qualname"], "path": info[str(r["src"])]["path"],
                 "calls": int(r["n"])}
                for r in fanout_rows if str(r["src"]) in info
            ]

            length_rows = conn.execute(
                "SELECT id, qualname, path, start_line, end_line FROM code_symbols "
                "WHERE workspace = ? AND kind IN ('function', 'method') "
                "ORDER BY (end_line - start_line) DESC LIMIT 100", (resolved,)).fetchall()
        for row in length_rows:
            length = max(0, int(row["end_line"]) - int(row["start_line"]))
            n_callers = fanin_by_id.get(str(row["id"]), 0)
            hotspots.append({"symbol": row["qualname"], "path": row["path"], "lines": length,
                             "callers": n_callers, "score": length * (1 + n_callers)})
        hotspots.sort(key=lambda e: -e["score"])
        hotspots = hotspots[:12]
    except Exception as exc:  # noqa: BLE001
        logger.warning("code_graph.get_architecture aggregates failed: %s", exc)

    summary = {
        "root": resolved, "files": status["files"], "symbols": status["symbols"],
        "edges": status["edges"], "languages": status["languages"],
        "by_kind": status["by_kind"], "by_certainty": status["by_certainty"],
        "routes": routes, "top_fan_in": top_fan_in[:12], "top_fan_out": top_fan_out[:12],
        "hotspots": hotspots, "last_indexed_at": status["last_indexed_at"],
    }
    lines = [
        f"Languages: {status['languages']}",
        f"Files: {status['files']}  Symbols: {status['symbols']}  Edges: {status['edges']}",
        f"By kind: {status['by_kind']}",
    ]
    if routes:
        lines.append("Routes:")
        lines += [f"  {r['route']}  {r['path']}:{r['line']}" for r in routes[:20]]
    if top_fan_in:
        lines.append("Most-called (top fan-in):")
        lines += [f"  {t['symbol']} ({t['callers']} callers) {t['path']}" for t in top_fan_in[:10]]
    if hotspots:
        lines.append("Hotspots (long + heavily called):")
        lines += [f"  {h['symbol']} {h['lines']}L x{h['callers']} callers {h['path']}"
                 for h in hotspots]
    return {"output": _clip("\n".join(lines), output_chars), "exit_code": 0, **summary}


# ── snippet ──────────────────────────────────────────────────────────────

def snippet(symbol: str, *, workspace: str = "", project_id: str = "",
            output_chars: int = 6000) -> Dict[str, Any]:
    root = _root(workspace)
    code_index.refresh(root, project_id=project_id)
    sym = _resolve_symbol(symbol, root=root, project_id=project_id)
    if not sym:
        return {"output": f"symbol not found: {symbol!r} under {root}", "exit_code": 1}
    abs_path = os.path.join(root, *str(sym["path"]).split("/"))
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as handle:
            all_lines = handle.read().split("\n")
    except OSError as exc:
        return {"output": f"could not read {sym['path']}: {exc}", "exit_code": 1}
    start = max(1, int(sym.get("start_line") or 1))
    end = min(len(all_lines), int(sym.get("end_line") or start))
    end = max(start, end)
    body = "\n".join(all_lines[start - 1:end])
    return {"output": _clip(body, output_chars), "exit_code": 0, "symbol": sym["qualname"],
            "path": sym["path"], "start_line": start, "end_line": end,
            "language": sym.get("language", "")}
