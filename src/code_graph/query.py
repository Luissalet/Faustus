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
_TEST_PATH_RE = re.compile(
    r"(?:^|/)tests?/|(?:^|/)test_[^/]+\.py$|[^/]+_test\.py$"
    r"|[^/]+\.test\.(?:ts|tsx|js)$|[^/]+\.spec\.[^/]+$"
)
# exact > static_inferred > lexical: the weakest certainty seen along a path
# is what the caller should trust for the whole path.
_CERTAINTY_RANK = {"exact": 0, "static_inferred": 1, "lexical": 2}
# `impact` needs every test file in the workspace actually indexed, not just
# the first DEFAULT_BUDGET_FILES (2000) a plain refresh() would walk — a repo
# with ~4200 tracked files (~1680 of them tests) would silently lose the back
# half of its test suite otherwise. The walk is hash-incremental, so a wider
# budget only costs more on the first call for a given workspace.
_IMPACT_INDEX_BUDGET = 20000


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


# ── impact ───────────────────────────────────────────────────────────────

def _is_test_path(path: str) -> bool:
    return bool(_TEST_PATH_RE.search((path or "").replace("\\", "/")))


def _weaker(a: str, b: str) -> str:
    """The less certain of two certainty labels (unknown labels count as
    weakest, since we cannot vouch for them)."""
    ra = _CERTAINTY_RANK.get(a, 99)
    rb = _CERTAINTY_RANK.get(b, 99)
    return a if ra >= rb else b


def _import_affected_tests(root: str, project_id: str, paths: Sequence[str]) -> List[str]:
    """Test modules that `import` (or `tests`) one of `paths`' modules,
    whether or not a `calls`-edge BFS ever reaches into them.

    A pure call-graph walk misses a test that exercises a changed module
    through a fixture, a monkeypatch, or an attribute access rather than a
    direct call to the reached symbol — but if the test file imports that
    module at all, it is still worth flagging as possibly affected."""
    found: set = set()
    seen: set = set()
    for path in paths:
        if not path or path in seen:
            continue
        seen.add(path)
        try:
            module_sym = next(
                (s for s in code_index.symbols_in(path, workspace=root, project_id=project_id)
                 if s.kind == "module"), None)
        except Exception:  # noqa: BLE001
            module_sym = None
        if not module_sym:
            continue
        try:
            hops = code_index.neighbors(module_sym.id, kinds=("imports", "tests"))
        except Exception:  # noqa: BLE001
            hops = []
        for hop in hops:
            if hop.get("direction") != "in" or not hop.get("resolved"):
                continue
            hop_path = str(hop.get("path") or "")
            if hop_path and _is_test_path(hop_path):
                found.add(hop_path)
    return sorted(found)


def _changed_seeds(root: str, *, base_ref: str, project_id: str) -> List[Dict[str, Any]]:
    """Resolve `detect_changes`' changed symbols to graph node ids."""
    changes = detect_changes(root, base_ref=base_ref, project_id=project_id)
    seeds: List[Dict[str, Any]] = []
    seen_paths: Dict[str, List[Any]] = {}
    for entry in changes.get("changed_symbols") or []:
        path = str(entry.get("path") or "")
        if path not in seen_paths:
            seen_paths[path] = code_index.symbols_in(path, workspace=root, project_id=project_id)
        for sym in seen_paths[path]:
            if sym.qualname == entry.get("symbol") and sym.start_line == entry.get("start_line"):
                seeds.append({"id": sym.id, "qualname": sym.qualname, "path": sym.path,
                              "start_line": sym.start_line})
                break
    return seeds


def impact(symbol: str = "", *, workspace: str = "", project_id: str = "",
           base_ref: str = "HEAD", depth: int = 3, limit: int = 200,
           include_history: bool = True,
           output_chars: int = DEFAULT_OUTPUT_CHARS) -> Dict[str, Any]:
    """What else can break, and which tests to run.

    Seeds are the resolved `symbol`, or — when `symbol` is empty — every
    symbol `detect_changes(base_ref)` finds touched by the current git diff.
    From each seed, BFS over *incoming* `calls` edges (who calls this, who
    calls THAT, ...) up to `depth` hops, deduped by symbol id and capped at
    `limit` nodes. Unresolved edges are counted but never followed — we
    cannot say what an unresolved call site reaches. A reached node whose
    file looks like a test file is collected into `affected_tests`, along
    with the seed's own test file when the seed itself lives in one.

    When `include_history` (default True), `historical_cochanges` adds
    files that usually change together with the seed file(s) in git history
    but were not reached by the call graph — config, templates, tests or
    i18n files a static call/import walk cannot see. This is a correlation
    signal, not a dependency, and is reported separately."""
    try:
        root = _root(workspace)
    except ValueError as exc:
        return {"error": str(exc), "exit_code": 1}
    try:
        code_index.refresh(root, project_id=project_id, budget_files=_IMPACT_INDEX_BUDGET)
    except Exception as exc:  # noqa: BLE001
        logger.warning("code_graph.impact refresh failed: %s", exc)

    try:
        depth_cap = max(1, min(int(depth or 3), 6))
    except (TypeError, ValueError):
        depth_cap = 3
    try:
        node_cap = max(1, min(int(limit or 200), 2000))
    except (TypeError, ValueError):
        node_cap = 200

    seeds: List[Dict[str, Any]] = []
    mode = "symbol"
    if str(symbol or "").strip():
        sym = _resolve_symbol(symbol, root=root, project_id=project_id)
        if not sym:
            return {"output": f"symbol not found: {symbol!r} under {root}", "exit_code": 1,
                    "root": root}
        seeds = [{"id": sym["id"], "qualname": sym["qualname"], "path": sym["path"],
                  "start_line": sym.get("start_line", 0)}]
    else:
        mode = "diff"
        try:
            seeds = _changed_seeds(root, base_ref=base_ref, project_id=project_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("code_graph.impact diff seeding failed: %s", exc)
            return {"error": f"impact: {exc}", "exit_code": 1, "root": root}
        if not seeds:
            return {"output": f"no changed symbols vs {base_ref}", "exit_code": 0,
                    "root": root, "mode": mode, "seeds": [], "reached": [],
                    "affected_tests": [], "affected_tests_via_call": [],
                    "affected_tests_via_import": [], "affected_test_functions": [],
                    "unresolved_edges": 0, "suggested_command": "",
                    "historical_cochanges": []}

    # BFS state: symbol id -> best-known node info (weakest certainty wins).
    nodes: Dict[str, Dict[str, Any]] = {}
    seed_ids = {s["id"] for s in seeds}
    frontier = [(s["id"], s["qualname"]) for s in seeds]
    visited = set(seed_ids)
    unresolved_edges = 0
    d = 0
    try:
        while frontier and d < depth_cap and len(nodes) < node_cap:
            d += 1
            next_frontier: List[Tuple[str, str]] = []
            for sym_id, callee_qual in frontier:
                for hop in code_index.neighbors(sym_id, kinds=("calls",)):
                    if hop.get("direction") != "in":
                        continue
                    if not hop.get("resolved"):
                        unresolved_edges += 1
                        continue
                    other = str(hop.get("symbol_id") or "")
                    if not other or other in seed_ids:
                        continue
                    certainty = str(hop.get("certainty") or "")
                    if other in nodes:
                        entry = nodes[other]
                        entry["certainty"] = _weaker(entry["certainty"], certainty)
                        entry["depth"] = min(entry["depth"], d)
                        continue
                    if len(nodes) >= node_cap:
                        break
                    nodes[other] = {
                        "qualname": hop.get("qualname", ""), "path": hop.get("path", ""),
                        "start_line": hop.get("start_line", 0), "depth": d,
                        "certainty": certainty, "via": callee_qual,
                    }
                    if other not in visited:
                        visited.add(other)
                        next_frontier.append((other, str(hop.get("qualname") or "")))
            frontier = next_frontier
    except Exception as exc:  # noqa: BLE001
        logger.warning("code_graph.impact BFS failed: %s", exc)
        return {"error": f"impact: {exc}", "exit_code": 1, "root": root}

    reached = [{"symbol": n["qualname"], "path": n["path"], "start_line": n["start_line"],
               "depth": n["depth"], "certainty": n["certainty"], "via": n["via"]}
              for n in nodes.values()]
    reached.sort(key=lambda e: (e["depth"], e["path"], e["start_line"]))

    test_files = set()
    test_functions: List[str] = []
    for entry in reached:
        if _is_test_path(entry["path"]):
            test_files.add(entry["path"])
            test_functions.append(entry["symbol"])
    for s in seeds:
        if _is_test_path(s["path"]):
            test_files.add(s["path"])

    # Only the seeds' own modules: a reached hub (agent_loop is imported by
    # a hundred tests) would turn "affected" into "everything".
    seed_paths = sorted({s["path"] for s in seeds})
    import_test_files = set(_import_affected_tests(root, project_id, seed_paths)) - test_files

    historical_cochanges: List[Dict[str, Any]] = []
    if include_history:
        reached_paths = {e["path"] for e in reached} | set(seed_paths)
        try:
            from .cochange import cochanges as _cochanges
            hist = _cochanges(seed_paths, workspace=root, limit=30)
            for r in hist.get("results") or []:
                if r["path"] in reached_paths:
                    continue
                historical_cochanges.append(r)
                if len(historical_cochanges) >= 10:
                    break
        except Exception as exc:  # noqa: BLE001
            logger.debug("code_graph.impact: cochanges lookup failed: %s", exc)

    all_test_files = test_files | import_test_files
    py_tests = sorted(p for p in all_test_files if p.endswith(".py"))
    other_tests = sorted(p for p in all_test_files if not p.endswith(".py"))
    if py_tests:
        capped = py_tests[:30]
        cmd = "python -m pytest -q " + " ".join(capped)
        if len(py_tests) > 30:
            cmd += f"  # ({len(py_tests) - 30} more test files not shown)"
        suggested_command = cmd
    elif other_tests:
        suggested_command = "JS/TS tests affected (run with your project's test runner): " \
                             + ", ".join(other_tests[:30])
    else:
        suggested_command = ""

    lines: List[str] = []
    seed_desc = ", ".join(s["qualname"] for s in seeds) if mode == "symbol" else \
        f"{len(seeds)} changed symbol(s) vs {base_ref}"
    lines.append(f"Impact of {seed_desc}:")
    cur_depth = 0
    for entry in reached:
        if entry["depth"] != cur_depth:
            cur_depth = entry["depth"]
            lines.append(f"-- depth {cur_depth} --")
        lines.append(f"  {entry['path']}:{entry['start_line']} {entry['symbol']} "
                     f"[{entry['certainty']}] via {entry['via']}")
    if not reached:
        lines.append("  (nothing reachable — no known callers)")
    lines.append(f"unresolved call edges skipped: {unresolved_edges}")
    if all_test_files:
        lines.append(f"affected tests ({len(all_test_files)}):")
        lines += [f"  {t}" for t in sorted(test_files)]
        lines += [f"  {t}  (via import)" for t in sorted(import_test_files)]
        if suggested_command:
            lines.append(f"suggested: {suggested_command}")
    else:
        lines.append("affected tests: none found")
    if historical_cochanges:
        lines.append(
            "Often changed together (history-based, not a dependency):")
        lines += [f"  {r['path']}  (support={r['support']} "
                  f"confidence={r['confidence']:.2f})" for r in historical_cochanges]

    return {
        "output": _clip("\n".join(lines), output_chars), "exit_code": 0, "root": root,
        "mode": mode, "seeds": seeds, "reached": reached,
        "affected_tests": sorted(all_test_files),
        "affected_tests_via_call": sorted(test_files),
        "affected_tests_via_import": sorted(import_test_files),
        "affected_test_functions": sorted(set(test_functions)),
        "unresolved_edges": unresolved_edges, "suggested_command": suggested_command,
        "depth": depth_cap, "base_ref": base_ref,
        "historical_cochanges": historical_cochanges,
    }
