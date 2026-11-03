"""src/code_graph/context_pack.py -- the change context pack.

For a planned change, one budgeted answer to "what must I see before I edit
this": the target's exact source, its contracts (decorators, signature, type
hints, docstring, pydantic models it touches), its direct consumers with the
exact call lines, the tests that exercise it with their assertion lines, and
the execution flows it sits in. Everything is composed from existing
primitives (`code_index.search/neighbors/symbols_in`, `query.impact`) plus
exact-line reads of the files.

The pack is ordered by relevance and trimmed to `budget_lines`. Whatever does
not fit is listed under `left_out` with a `more` handle (a tool + arguments)
that fetches exactly that piece. `known_hashes` ({path: hash} from an earlier
pack's `hashes`) makes unchanged files report "unchanged since <hash>" instead
of being re-sent.
"""
from __future__ import annotations

import ast
import hashlib
import json
import logging
import os
import re
from typing import Any, Dict, List, Tuple

from src.context_engine import code_index

from .query import (_changed_seeds, _clip, _is_test_path, _resolve_symbol, _root,
                    impact as _impact)

logger = logging.getLogger(__name__)

DEFAULT_BUDGET_LINES = 120
_ASSERT_RE = re.compile(r"\bassert\b|pytest\.raises|\bexpect\(|\.to(?:Be|Equal|Have|Throw)|self\.assert\w*\(")
_RELEVANCE = {"source": 100, "contract": 90, "consumer": 80, "test": 70, "flow": 50}
_MAX_TARGETS = 6
_PLAIN_TYPES = {"str", "int", "float", "bool", "dict", "list", "None", "Any", "Optional", "List", "Dict",
                "Tuple", "Set", "Union", "bytes", "tuple", "set"}


class _Reader:
    def __init__(self, root: str):
        self.root = root
        self._lines: Dict[str, List[str]] = {}
        self._hash: Dict[str, str] = {}

    def lines(self, rel: str) -> List[str]:
        if rel not in self._lines:
            try:
                with open(os.path.join(self.root, *rel.split("/")), "rb") as handle:
                    raw = handle.read()
                self._hash[rel] = hashlib.sha256(raw).hexdigest()[:12]
                self._lines[rel] = raw.decode("utf-8", "replace").replace("\r\n", "\n").split("\n")
            except OSError:
                self._lines[rel], self._hash[rel] = [], ""
        return self._lines[rel]

    def hash(self, rel: str) -> str:
        self.lines(rel)
        return self._hash.get(rel, "")

    def span(self, rel: str, start: int, end: int) -> List[Tuple[int, str]]:
        ls = self.lines(rel)
        start, end = max(1, start), min(len(ls), end)
        return [(i, ls[i - 1]) for i in range(start, end + 1)]


def _known(known_hashes: Any) -> Dict[str, str]:
    if isinstance(known_hashes, str):
        try:
            known_hashes = json.loads(known_hashes)
        except ValueError:
            known_hashes = [known_hashes]
    out: Dict[str, str] = {}
    if isinstance(known_hashes, dict):
        out = {str(k): str(v) for k, v in known_hashes.items()}
    elif isinstance(known_hashes, (list, tuple)):
        for item in known_hashes:
            if isinstance(item, str) and "@" in item:
                p, h = item.rsplit("@", 1)
                out[p] = h
    return out


def _targets(symbol_or_paths: Any, root: str, project_id: str, base_ref: str) -> List[Dict[str, Any]]:
    items = [symbol_or_paths] if isinstance(symbol_or_paths, str) else list(symbol_or_paths or [])
    items = [i for i in items if str(i or "").strip()]
    out: List[Dict[str, Any]] = []
    seen = set()

    def push(d: Dict[str, Any]) -> None:
        if d.get("id") and d["id"] not in seen:
            seen.add(d["id"])
            out.append(d)

    for raw in items:
        name = str(raw).strip()
        rel = name.replace("\\", "/")
        if os.path.isfile(os.path.join(root, *rel.split("/"))):
            syms = [s for s in code_index.symbols_in(rel, workspace=root, project_id=project_id)
                    if s.kind not in ("module", "import")]
            for s in syms[:_MAX_TARGETS]:
                push(s.to_dict())
            continue
        sym = _resolve_symbol(name, root=root, project_id=project_id)
        if sym:
            push(sym)
    if not items and base_ref:
        for seed in _changed_seeds(root, base_ref=base_ref, project_id=project_id)[:_MAX_TARGETS]:
            sym = _resolve_symbol(seed["qualname"], root=root, project_id=project_id)
            if sym:
                push(sym)
    return out[:_MAX_TARGETS]


def _ast_node(source: str, name: str, start: int):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
            first = min([node.lineno] + [d.lineno for d in node.decorator_list])
            dist = abs(first - start)
            if best is None or dist < best[0]:
                best = (dist, node)
    return best[1] if best and best[0] <= 3 else None


def _contract(rd: _Reader, sym: Dict[str, Any], root: str, project_id: str
              ) -> Tuple[List[Tuple[int, str]], List[Dict[str, Any]]]:
    """Header lines (decorators, signature, docstring) and the pydantic models
    referenced by the annotations."""
    rel, start = sym["path"], int(sym.get("start_line") or 1)
    name = str(sym["qualname"]).rsplit(".", 1)[-1]
    models: List[Dict[str, Any]] = []
    if not rel.endswith(".py"):
        return rd.span(rel, start, start), models
    node = _ast_node("\n".join(rd.lines(rel)), name, start)
    if node is None:
        return rd.span(rel, start, start), models
    first = min([node.lineno] + [d.lineno for d in node.decorator_list])
    body0 = node.body[0] if node.body else None
    hdr_end = (body0.lineno - 1) if body0 else node.lineno
    if ast.get_docstring(node) and isinstance(body0, ast.Expr):
        hdr_end = min(body0.end_lineno or hdr_end, body0.lineno + 6)
    header = rd.span(rel, first, max(first, hdr_end))
    names = set()
    anns: List[Any] = []
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        a = node.args
        anns = [x.annotation for x in a.args + a.kwonlyargs + a.posonlyargs if x.annotation] + \
               ([node.returns] if node.returns else [])
    for ann in anns:
        for n in ast.walk(ann):
            if isinstance(n, ast.Name):
                names.add(n.id)
            elif isinstance(n, ast.Attribute):
                names.add(n.attr)
    for cname in sorted(names - _PLAIN_TYPES):
        for hit in code_index.search(cname, workspace=root, project_id=project_id, k=5, kinds=("class",)):
            if str(hit.get("qualname", "")).rsplit(".", 1)[-1] != cname:
                continue
            hl = rd.lines(hit["path"])
            hs = int(hit.get("start_line") or 1)
            if hs - 1 < len(hl) and re.search(r"\b(BaseModel|SQLModel)\b", hl[hs - 1]):
                models.append({"name": cname, "path": hit["path"],
                               "lines": rd.span(hit["path"], hs, min(int(hit.get("end_line") or hs), hs + 20))})
            break
    return header, models


def _call_lines(rd: _Reader, caller: Dict[str, Any], target_name: str) -> List[Tuple[int, str]]:
    rx = re.compile(r"(?<![\w])" + re.escape(target_name) + r"\s*\(")
    rel, s, e = caller["path"], int(caller.get("start_line") or 1), int(caller.get("end_line") or 1)
    ls = rd.lines(rel)
    return [(i, ls[i - 1]) for i in range(s + 1, min(e, len(ls)) + 1) if rx.search(ls[i - 1])][:3]


def _assertion_lines(rd: _Reader, rel: str, start: int, end: int, name: str) -> List[Tuple[int, str]]:
    cands = [(i, t) for i, t in rd.span(rel, start, end) if _ASSERT_RE.search(t)]
    mention = [c for c in cands if name in c[1]]
    rest = [c for c in cands if name not in c[1]]
    return (mention + rest)[:6]


def context_pack(symbol_or_paths: Any = "", root: str = "", *, project_id: str = "",
                 budget_lines: int = DEFAULT_BUDGET_LINES, base_ref: str = "",
                 known_hashes: Any = None, output_chars: int = 12000) -> Dict[str, Any]:
    """Budgeted context for a planned change. `symbol_or_paths` is a symbol
    name, a file path, or a list of either; empty with `base_ref` uses the
    symbols the git diff against `base_ref` touches."""
    try:
        resolved = _root(root)
    except ValueError as exc:
        return {"error": str(exc), "exit_code": 1}
    try:
        budget = max(5, min(int(budget_lines or DEFAULT_BUDGET_LINES), 2000))
    except (TypeError, ValueError):
        budget = DEFAULT_BUDGET_LINES
    try:
        code_index.refresh(resolved, project_id=project_id, budget_files=20000)
    except Exception as exc:  # noqa: BLE001
        logger.warning("code_graph.context_pack: refresh failed: %s", exc)
    known = _known(known_hashes)
    targets = _targets(symbol_or_paths, resolved, project_id, base_ref)
    if not targets:
        return {"error": f"no symbol found for {symbol_or_paths!r}", "exit_code": 1, "root": resolved}
    rd = _Reader(resolved)
    items: List[Dict[str, Any]] = []

    def add(kind: str, label: str, path: str, lines: List[Tuple[int, str]], bonus: float,
            more: Dict[str, Any], **extra: Any) -> None:
        items.append({"kind": kind, "label": label, "path": path, "lines": lines,
                      "relevance": round(_RELEVANCE[kind] - bonus, 2), "more": more, **extra})

    for ti, t in enumerate(targets):
        qn, rel = str(t["qualname"]), t["path"]
        name = qn.rsplit(".", 1)[-1]
        snip = {"tool": "code_graph_snippet", "args": {"symbol": qn, "root": resolved}}
        add("source", f"source of {qn}", rel,
            rd.span(rel, int(t.get("start_line") or 1), int(t.get("end_line") or 1)), ti, snip)
        try:
            header, models = _contract(rd, t, resolved, project_id)
            add("contract", f"contract of {qn} (decorators, signature, docstring)", rel, header, ti, snip)
            for m in models:
                add("contract", f"pydantic model {m['name']} used by {qn}", m["path"], m["lines"], ti + 1,
                    {"tool": "code_graph_snippet", "args": {"symbol": m["name"], "root": resolved}})
        except Exception as exc:  # noqa: BLE001
            logger.debug("context_pack contract failed: %s", exc)
        try:
            hops = [h for h in code_index.neighbors(t["id"], kinds=("calls",))
                    if h.get("direction") == "in" and h.get("resolved")]
        except Exception:  # noqa: BLE001
            hops = []
        rank = {"exact": 0, "static_inferred": 1, "lexical": 2}
        hops.sort(key=lambda h: (rank.get(h.get("certainty"), 3), h.get("path", ""), h.get("start_line", 0)))
        seen_callers = set()
        for ci, h in enumerate(hops):
            if h["symbol_id"] in seen_callers or _is_test_path(h["path"]):
                continue
            seen_callers.add(h["symbol_id"])
            calls = _call_lines(rd, h, name)
            note = ""
            if not calls:
                calls = rd.span(h["path"], int(h["start_line"]), int(h["start_line"]))
                note = "call line not found textually (aliased or indirect call); showing the caller's definition line"
            add("consumer", f"{h['qualname']} calls {qn} [{h['certainty']}]", h["path"], calls,
                min(ci, 9) * 0.1 + ti,
                {"tool": "code_graph_snippet", "args": {"symbol": h["qualname"], "root": resolved}}, note=note)
        try:
            imp = _impact(qn, workspace=resolved, project_id=project_id, depth=2, include_history=False,
                          include_risk=False, output_chars=500)
        except Exception as exc:  # noqa: BLE001
            logger.debug("context_pack impact failed: %s", exc)
            imp = {}
        covered = set()
        for r in [r for r in imp.get("reached") or [] if _is_test_path(r["path"])][:8]:
            end = r["start_line"]
            for s in code_index.symbols_in(r["path"], workspace=resolved, project_id=project_id):
                if s.qualname == r["symbol"] and s.start_line == r["start_line"]:
                    end = s.end_line
                    break
            lines = _assertion_lines(rd, r["path"], r["start_line"], end, name)
            covered.add(r["path"])
            add("test", f"assertions of {r['symbol']} ({r['path']})", r["path"],
                lines or rd.span(r["path"], r["start_line"], r["start_line"]), ti,
                {"tool": "code_graph_snippet", "args": {"symbol": r["symbol"], "root": resolved}},
                note="" if lines else "no assert line found inside the test body")
        for tp in [p for p in imp.get("affected_tests") or [] if p not in covered][:4]:
            hits = [(i, x) for i, x in enumerate(rd.lines(tp), 1) if _ASSERT_RE.search(x) and name in x][:4]
            add("test", f"assertions mentioning {name} in {tp} (imports the module)", tp,
                hits or rd.span(tp, 1, 1), ti + 5,
                {"tool": "code_graph_impact", "args": {"symbol": qn, "root": resolved}},
                note="" if hits else "imports the module; no assertion line mentions the symbol")
        for f in (imp.get("affected_flows") or [])[:3]:
            add("flow", f"flow {f['name']} (criticality {f['criticality']:.2f})", "", [], ti,
                {"tool": "code_graph_flows", "args": {"id": f["id"], "root": resolved}},
                flow_id=f["id"], criticality=f["criticality"])

    items.sort(key=lambda it: -it["relevance"])
    used = 0
    included: List[Dict[str, Any]] = []
    unchanged: List[Dict[str, Any]] = []
    left_out: List[Dict[str, Any]] = []
    hashes: Dict[str, str] = {}
    for it in items:
        path = it["path"]
        cost = 1 if it["kind"] == "flow" else max(1, len(it["lines"]))
        h = rd.hash(path) if path else ""
        remaining = budget - used
        if path and h and known.get(path) == h:
            if remaining >= 1:
                used += 1
                hashes[path] = h
                unchanged.append({"kind": it["kind"], "label": it["label"], "path": path,
                                  "status": f"unchanged since {h}", "more": it["more"]})
            else:
                left_out.append({"kind": it["kind"], "label": it["label"], "lines": cost, "more": it["more"]})
            continue
        if cost <= remaining:
            used += cost
            if path:
                hashes[path] = h
            included.append(it)
        elif it["kind"] in ("source", "contract") and remaining >= 5 and it["lines"]:
            included.append(dict(it, lines=it["lines"][:remaining], truncated=True))
            used += remaining
            hashes[path] = h
            left_out.append({"kind": it["kind"], "label": it["label"] + " (remaining lines)",
                             "lines": cost - remaining, "more": it["more"]})
        else:
            left_out.append({"kind": it["kind"], "label": it["label"], "lines": cost, "more": it["more"]})

    def view(it: Dict[str, Any]) -> Dict[str, Any]:
        v: Dict[str, Any] = {k: it[k] for k in ("kind", "label", "path", "relevance")}
        v["lines"] = [{"line": i, "text": t} for i, t in it["lines"]]
        for k in ("note", "truncated", "flow_id", "criticality", "more"):
            if it.get(k):
                v[k] = it[k]
        if it["lines"]:
            v["range"] = f"{it['path']}:{it['lines'][0][0]}-{it['lines'][-1][0]}"
        return v

    included_v = [view(i) for i in included]
    text = [f"Context pack for {', '.join(t['qualname'] for t in targets)} "
            f"({used}/{budget} lines, {len(left_out)} left out, {len(unchanged)} unchanged)"]
    for v in included_v:
        text.append(f"## {v['label']}" + (f"  [{v['range']}]" if v.get("range") else ""))
        text += [f"{x['line']:>5}  {x['text']}" for x in v["lines"]]
        if v.get("note"):
            text.append(f"  note: {v['note']}")
    for u in unchanged:
        text.append(f"## {u['label']}  -- {u['status']}")
    if left_out:
        text.append("## left out (fetch with the `more` handle)")
        text += [f"  - {x['kind']}: {x['label']} (~{x['lines']} lines) -> {x['more']['tool']} "
                 f"{json.dumps(x['more']['args'])}" for x in left_out]
    return {"output": _clip("\n".join(text), output_chars), "exit_code": 0, "root": resolved,
            "targets": [{"symbol": t["qualname"], "path": t["path"], "start_line": t["start_line"],
                         "end_line": t["end_line"]} for t in targets],
            "budget_lines": budget, "used_lines": used, "items": included_v, "unchanged": unchanged,
            "left_out": left_out, "hashes": hashes,
            "more": [{"tool": "code_graph_impact", "args": {"symbol": targets[0]["qualname"], "root": resolved}}]}


__all__ = ["context_pack"]
