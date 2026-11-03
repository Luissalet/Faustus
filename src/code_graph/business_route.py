"""src/code_graph/business_route.py -- "what happens from pressing <UI action>
until <it is saved>", across the frontend/backend boundary.

This is a composition layer, not a second indexer. The Python side comes from
the existing graph (`code_symbols` route rows, `calls` edges, `impact`); what
the graph does not hold -- HTML/JSX elements and handlers, `fetch`/`axios`/XHR/
WebSocket calls, Express registrations, SQL strings, file writes -- is read from
the source files with line-anchored patterns, the same way `dossier` does.

HONESTY CONTRACT. Every hop has `evidence` (`path:line`) and a `certainty`:

* `resolved`   -- a static match: a literal handler name, a literal or
                  placeholder URL that matches a registered route pattern, a
                  `calls` edge the graph resolved, a statement in the source.
* `observed`   -- confirmed by a runtime event in `runtime_trace`.
* `name-only`  -- matched only because names look alike. It is NEVER a hop:
                  it is listed under `gaps` so nobody mistakes it for a link.
* `unresolved` -- a dynamic URL, a computed handler, a computed SQL statement.
                  Also a gap.

Nothing is guessed to fill a hole; what static reading cannot settle is a gap.
"""
from __future__ import annotations

import logging
import os
import re
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from src.context_engine import code_index
from src.context_engine import store as ce_store

from .dossier import _Files
from .query import DEFAULT_OUTPUT_CHARS, _clip, _is_test_path, _resolve_symbol, _root

logger = logging.getLogger(__name__)

_JS_EXT = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte", ".html", ".htm")
_UI_EXT = (".html", ".htm", ".jsx", ".tsx", ".vue", ".svelte", ".js", ".ts")
_HTTP_VERBS = ("get", "post", "put", "patch", "delete", "head", "options")
_MAX_DEPTH = 5
_MAX_HOPS = 60
_MAX_TEST_LOOKUPS = 10
_END_ANY_WRITE = {"saved", "save", "guardado", "guardar", "guardada", "persisted", "stored",
                  "database", "db", "bd", "disk", "file", "archivo", "written", "escrito"}
_STOP_TOKENS = {"get", "set", "post", "put", "patch", "delete", "handle", "handler", "on", "click",
                "submit", "data", "list", "item", "items", "the", "and", "for", "api", "new",
                "init", "main", "run", "do", "load", "show", "update", "fetch", "send", "button"}
_JS_BUILTINS = {
    "fetch", "json", "console", "document", "window", "promise", "settimeout", "setinterval",
    "alert", "parseint", "parsefloat", "string", "number", "array", "object", "math", "date",
    "if", "for", "while", "switch", "catch", "function", "return", "await", "async", "then",
    "stringify", "parse", "push", "map", "filter", "reduce", "foreach", "find", "includes",
    "preventdefault", "getelementbyid", "queryselector", "addeventlistener", "appendchild",
    "formdata", "urlsearchparams", "error", "require", "import", "super", "boolean", "log",
}


# ── small helpers ────────────────────────────────────────────────────────

def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9/ ]+", " ", text)).strip()


def _tokens(name: str) -> List[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(name or ""))
    parts = re.split(r"[^A-Za-z0-9]+", spaced)
    return [p.lower() for p in parts if len(p) >= 3 and p.lower() not in _STOP_TOKENS]


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, max(0, offset)) + 1


def _hop(layer: str, kind: str, label: str, evidence: str, certainty: str, **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"layer": layer, "kind": kind, "label": label, "evidence": evidence,
                           "certainty": certainty, "confirmed": False}
    out.update({k: v for k, v in extra.items() if v not in (None, "", [], {})})
    return out


def _gap(kind: str, certainty: str, label: str, evidence: str, reason: str, **extra: Any) -> Dict[str, Any]:
    out = {"kind": kind, "certainty": certainty, "label": label, "evidence": evidence, "reason": reason}
    out.update({k: v for k, v in extra.items() if v not in (None, "", [], {})})
    return out


def _balanced(text: str, start: int, open_ch: str = "(", close_ch: str = ")", limit: int = 4000) -> str:
    """Text from `start` (the opening bracket) to its matching close, string-aware."""
    depth, quote, i, end = 0, "", start, min(len(text), start + limit)
    while i < end:
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "'\"`":
            quote = ch
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        i += 1
    return text[start:end]


def _first_arg(call_text: str) -> Tuple[str, str]:
    """(first argument, remainder) of `(...)` text, split at the top-level comma."""
    inner = call_text[1:-1] if call_text.endswith(")") else call_text[1:]
    depth, quote, i = 0, "", 0
    while i < len(inner):
        ch = inner[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "'\"`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            return inner[:i].strip(), inner[i + 1:]
        i += 1
    return inner.strip(), ""


def _split_plus(expr: str) -> List[str]:
    parts, depth, quote, cur, i = [], 0, "", "", 0
    while i < len(expr):
        ch = expr[i]
        if quote:
            cur += ch
            if ch == "\\" and i + 1 < len(expr):
                cur += expr[i + 1]
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "'\"`":
            quote = ch
            cur += ch
        elif ch in "([{":
            depth += 1
            cur += ch
        elif ch in ")]}":
            depth -= 1
            cur += ch
        elif ch == "+" and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
        i += 1
    parts.append(cur.strip())
    return [p for p in parts if p]


# ── URL patterns ─────────────────────────────────────────────────────────

_CONST_RE = re.compile(r"""(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(['"`])([^'"`$\n]*)\2""")


def _file_consts(text: str) -> Dict[str, str]:
    return {m.group(1): m.group(3) for m in _CONST_RE.finditer(text)}


def _parse_url(arg: str, consts: Dict[str, str], depth: int = 0) -> Tuple[Optional[str], str]:
    """(pattern or None, note). `pattern` uses `{x}` for dynamic segments; None is
    returned (with the reason) when the start of the URL is not statically known."""
    arg = arg.strip()
    if not arg:
        return None, "empty URL argument"
    if re.fullmatch(r"[A-Za-z_$][\w$.]*", arg):
        if arg in consts and depth < 3:
            return _parse_url("'" + consts[arg] + "'", consts, depth + 1)
        return None, f"URL held in variable `{arg}`"
    parts = _split_plus(arg)
    pieces: List[Tuple[str, bool]] = []   # (text, is_literal)
    for part in parts:
        m = re.fullmatch(r"""(['"])(.*)\1""", part, re.S)
        if m:
            pieces.append((m.group(2), True))
            continue
        if part.startswith("`") and part.endswith("`") and len(part) >= 2:
            body, out, i = part[1:-1], "", 0
            first = True
            while i < len(body):
                if body.startswith("${", i):
                    j = i + 2
                    depth_b = 1
                    while j < len(body) and depth_b:
                        depth_b += {"{": 1, "}": -1}.get(body[j], 0)
                        j += 1
                    inner = body[i + 2:j - 1].strip()
                    if first and not out and inner in consts:
                        out += consts[inner]
                    else:
                        name = re.sub(r"\W+", "_", inner.split(".")[-1]) or "x"
                        out += "{" + name + "}"
                    i = j
                else:
                    out += body[i]
                    i += 1
                first = False
            pieces.append((out, True))
            continue
        if re.fullmatch(r"[A-Za-z_$][\w$]*", part) and part in consts:
            pieces.append((consts[part], True))
        else:
            pieces.append(("{" + (re.sub(r"\W+", "_", part)[:20] or "x") + "}", False))
    if not pieces:
        return None, "empty URL argument"
    text = "".join(t for t, _l in pieces)
    first_text, first_lit = pieces[0]
    if not first_lit or first_text.startswith("{"):
        if not any(lit and "/" in t for t, lit in pieces[1:]) or not text.strip("{}"):
            return None, "URL computed at run time (`" + arg[:60] + "`)"
        return None, "URL starts with a value not known statically (`" + arg[:60] + "`)"
    return text, ""


def _clean_path(url: str) -> Tuple[str, str]:
    """(path, host) of a URL pattern; query string and fragment dropped."""
    host = ""
    m = re.match(r"^[a-z][a-z0-9+.-]*://([^/]*)(.*)$", url, re.I)
    if m:
        host, url = m.group(1).lower(), m.group(2) or "/"
    url = re.split(r"[?#]", url, maxsplit=1)[0]
    if len(url) > 1:
        url = url.rstrip("/")
    return url or "/", host


def _segments(path: str) -> List[str]:
    return [s for s in path.strip("/").split("/") if s != ""]


def _is_param(seg: str) -> bool:
    return bool(re.fullmatch(r"\{[^}]*\}|<[^>]*>|:[A-Za-z_]\w*|\[[^\]]*\]", seg)) or "{" in seg


def _paths_match(a: str, b: str) -> bool:
    sa, sb = _segments(a), _segments(b)
    if len(sa) != len(sb):
        return False
    for x, y in zip(sa, sb):
        if _is_param(x) or _is_param(y):
            continue
        if x != y:
            return False
    return True


def _methods_match(a: Sequence[str], b: Sequence[str]) -> bool:
    A, B = {m.upper() for m in a}, {m.upper() for m in b}
    return "ANY" in A or "ANY" in B or bool(A & B)


# ── JS source model ──────────────────────────────────────────────────────

_FUNC_DEF_RES = [
    re.compile(r"^[ \t]*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\("),
    re.compile(r"^[ \t]*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?"
               r"(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)"),
    re.compile(r"^[ \t]*(?:window\.|this\.|exports\.|module\.exports\.)([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?"
               r"(?:function\b|\([^)]*\)\s*=>)"),
]


def _strip_strings(s: str) -> str:
    return re.sub(r"""(['"`])(?:\\.|(?!\1).)*\1""", '""', s)


def _brace_end(lines: Sequence[str], start_idx: int) -> int:
    """Index of the line where the braces opened at/after `start_idx` close."""
    depth, opened = 0, False
    for i in range(start_idx, min(len(lines), start_idx + 400)):
        clean = _strip_strings(re.sub(r"//.*$", "", lines[i]))
        for ch in clean:
            if ch == "{":
                depth += 1
                opened = True
            elif ch == "}":
                depth -= 1
        if opened and depth <= 0:
            return i
        if not opened and i > start_idx + 1 and "=>" not in lines[start_idx]:
            break
        if not opened and "=>" in lines[start_idx] and not lines[start_idx].rstrip().endswith("=>"):
            return start_idx   # expression-bodied arrow on one line
    return start_idx


class _JsModel:
    """Function definitions and constants of every frontend/Node file."""

    def __init__(self, files: _Files):
        self.files = files
        self.defs: Dict[str, List[Dict[str, Any]]] = {}
        self.consts: Dict[str, Dict[str, str]] = {}
        for rel in files.paths:
            if not rel.endswith(_JS_EXT) or _is_test_path(rel):
                continue
            lines = files.lines(rel)
            self.consts[rel] = _file_consts("\n".join(lines))
            for i, line in enumerate(lines):
                for rx in _FUNC_DEF_RES:
                    m = rx.match(line)
                    if m:
                        end = _brace_end(lines, i)
                        self.defs.setdefault(m.group(1), []).append(
                            {"name": m.group(1), "file": rel, "start": i + 1, "end": end + 1})
                        break

    def find(self, name: str, prefer_file: str = "") -> Tuple[Optional[Dict[str, Any]], str]:
        cands = self.defs.get(name) or []
        if not cands:
            return None, "no definition found"
        same = [c for c in cands if c["file"] == prefer_file]
        if len(same) == 1:
            return same[0], ""
        if len(cands) == 1:
            return cands[0], ""
        return None, f"{len(cands)} definitions named `{name}` ({', '.join(c['file'] for c in cands[:3])})"

    def body(self, d: Dict[str, Any]) -> str:
        return "\n".join(self.files.lines(d["file"])[d["start"] - 1:d["end"]])

    def enclosing(self, rel: str, line: int) -> Optional[Dict[str, Any]]:
        best = None
        for defs in self.defs.values():
            for d in defs:
                if d["file"] == rel and d["start"] <= line <= d["end"]:
                    if best is None or (d["end"] - d["start"]) < (best["end"] - best["start"]):
                        best = d
        return best


# ── network calls in JS ──────────────────────────────────────────────────

_FETCH_RE = re.compile(r"(?<![\w$.])fetch\s*\(")
_AXIOS_VERB_RE = re.compile(r"(?<![\w$])(?:axios|\$http|http|api|client|request|this\.http|httpClient|"
                            r"this\.\$http|\$)\.(get|post|put|patch|delete|head|getJSON)\s*(?:<[^>(]*>)?\s*\(")
_AXIOS_CFG_RE = re.compile(r"(?<![\w$.])axios\s*\(\s*\{|\$\.ajax\s*\(\s*\{")
_XHR_RE = re.compile(r"\.open\s*\(\s*['\"](GET|POST|PUT|PATCH|DELETE)['\"]\s*,")
_WS_RE = re.compile(r"new\s+WebSocket\s*\(")


def _scan_network(text: str, base_line: int, rel: str, consts: Dict[str, str]) -> List[Dict[str, Any]]:
    """Client calls in `text` (a function body or file slice)."""
    out: List[Dict[str, Any]] = []

    def add(method: str, arg: str, off: int, kind: str) -> None:
        pat, note = _parse_url(arg, consts)
        line = base_line + _line_of(text, off) - 1
        rec: Dict[str, Any] = {"method": method.upper(), "line": line, "file": rel, "kind": kind,
                               "raw": arg[:100]}
        if pat is None:
            rec.update(certainty="unresolved", pattern=None, reason=note)
        else:
            path, host = _clean_path(pat)
            rec.update(certainty="resolved", pattern=path, host=host, url=pat)
        out.append(rec)

    for m in _FETCH_RE.finditer(text):
        call = _balanced(text, m.end() - 1)
        arg, rest = _first_arg(call)
        mm = re.search(r"""method\s*:\s*['"`](\w+)['"`]""", rest)
        add(mm.group(1) if mm else "GET", arg, m.start(), "fetch")
    for m in _AXIOS_VERB_RE.finditer(text):
        call = _balanced(text, m.end() - 1)
        arg, _rest = _first_arg(call)
        verb = m.group(1)
        add("GET" if verb.lower() == "getjson" else verb, arg, m.start(), "axios")
    for m in _AXIOS_CFG_RE.finditer(text):
        cfg = _balanced(text, text.index("{", m.start()), "{", "}")
        um = re.search(r"""\burl\s*:\s*(.+?)\s*(?:,\s*\n|,\s*[A-Za-z]+\s*:|\n|\})""", cfg, re.S)
        mm = re.search(r"""(?:method|type)\s*:\s*['"`](\w+)['"`]""", cfg)
        if um:
            add(mm.group(1) if mm else "GET", um.group(1).strip().rstrip(","), m.start(), "axios")
    for m in _XHR_RE.finditer(text):
        call = _balanced(text, m.start() + text[m.start():].index("("))
        _first, rest = _first_arg(call)
        arg, _r = _first_arg("(" + rest + ")")
        add(m.group(1), arg, m.start(), "xhr")
    for m in _WS_RE.finditer(text):
        call = _balanced(text, m.end() - 1)
        arg, _r = _first_arg(call)
        add("WS", arg, m.start(), "websocket")
    out.sort(key=lambda r: r["line"])
    return out


_CALL_NAME_RE = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(")


def _js_calls(text: str, base_line: int) -> List[Tuple[str, int]]:
    seen, out = set(), []
    for m in _CALL_NAME_RE.finditer(text):
        name = m.group(1)
        if name.lower() in _JS_BUILTINS or name in seen:
            continue
        seen.add(name)
        out.append((name, base_line + _line_of(text, m.start()) - 1))
    return out


# ── UI: elements and handlers ────────────────────────────────────────────

_OPEN_RE = re.compile(r"<(button|a|input|form|div|span|li|label|summary|option)\b", re.I)


def _open_tag_end(text: str, pos: int) -> int:
    """Index of the `>` closing the opening tag that starts at `pos` (braces and
    quotes aware, so `onClick={() => f()}` does not end the tag early)."""
    depth, quote, i = 0, "", pos
    while i < len(text) and i < pos + 3000:
        ch = text[i]
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == ">" and depth <= 0:
            return i
        i += 1
    return -1


def _elements(text: str) -> Iterable[Dict[str, Any]]:
    for m in _OPEN_RE.finditer(text):
        tag = m.group(1).lower()
        end = _open_tag_end(text, m.end())
        if end < 0:
            continue
        attrs = text[m.end():end]
        inner = ""
        if tag != "input" and not attrs.rstrip().endswith("/"):
            close = re.search(r"</" + tag + r"\s*>", text[end + 1:], re.I)
            inner = text[end + 1:end + 1 + (close.start() if close else 300)]
        yield {"tag": tag, "attrs": attrs, "inner": inner, "start": m.start()}


_HANDLER_ATTR_RE = re.compile(
    r"""(?:\bon(?:click|submit|change)|@click|v-on:click|\(click\)|\(ngSubmit\)|@submit|v-on:submit)"""
    r"""\s*=\s*(?:"([^"]*)"|'([^']*)'|\{(.*?)\}(?=\s|>|/|$))""", re.I | re.S)
_ONCLICK_JSX_RE = re.compile(r"""\bon(?:Click|Submit|Change)\s*=\s*\{((?:[^{}]|\{[^{}]*\})*)\}""")


def _element_text(el: Dict[str, Any]) -> Tuple[str, str]:
    attrs, inner = el["attrs"], el["inner"]
    text = re.sub(r"<[^>]+>", " ", inner)
    for a in ("value", "aria-label", "title", "placeholder"):
        am = re.search(a + r"""\s*=\s*["']([^"']*)["']""", attrs)
        if am:
            text += " " + am.group(1)
    return re.sub(r"\s+", " ", text).strip(), attrs


def _score_text(want: str, got: str) -> int:
    w, g = _norm(want), _norm(got)
    if not w or not g:
        return 0
    if w == g:
        return 3
    if w in g:
        return 2
    wt = set(w.split())
    if wt and wt <= set(g.split()):
        return 1
    return 0


def _find_ui(files: _Files, start: str) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    for rel in files.paths:
        if not rel.endswith(_UI_EXT) or _is_test_path(rel):
            continue
        text = files.text(rel)
        if rel.endswith((".js", ".ts")) and "<" not in text:
            continue
        for m in _elements(text):
            etext, attrs = _element_text(m)
            score = _score_text(start, etext)
            if not score:
                idm = re.search(r"""\bid\s*=\s*["']([^"']+)["']""", attrs)
                if idm and _norm(start).replace(" ", "") == _norm(idm.group(1)).replace(" ", ""):
                    score = 2
            if not score:
                continue
            tag = m["tag"]
            # an <a>/<div> with no handler or id is plain text, not an action
            hm = _HANDLER_ATTR_RE.search(attrs) or _ONCLICK_JSX_RE.search(attrs)
            idm = re.search(r"""\bid\s*=\s*["']([^"']+)["']""", attrs)
            if tag in ("div", "span", "li", "label", "a", "option") and not hm and not idm:
                continue
            found.append({"file": rel, "line": _line_of(text, m["start"]), "tag": tag, "text": etext,
                          "attrs": attrs, "score": score + (1 if tag in ("button", "input", "form") else 0),
                          "offset": m["start"]})
    found.sort(key=lambda r: (-r["score"], r["file"], r["line"]))
    return found


def _handler_from_expr(expr: str) -> Tuple[str, str]:
    """(function name or '', note) from an inline handler expression."""
    expr = expr.strip().strip(";")
    m = re.match(r"^(?:this\.|window\.|\$ctrl\.|ctrl\.)?([A-Za-z_$][\w$]*)\s*(?:\(.*\))?$", expr, re.S)
    if m and m.group(1) not in ("function", "event"):
        return m.group(1), ""
    m = re.search(r"=>\s*\{?\s*(?:await\s+)?(?:this\.|window\.)?([A-Za-z_$][\w$]*)\s*\(", expr)
    if m:
        return m.group(1), ""
    m = re.search(r"(?:await\s+)?(?:this\.|window\.)?([A-Za-z_$][\w$]*)\s*\(", expr)
    if m and m.group(1).lower() not in _JS_BUILTINS:
        return m.group(1), ""
    return "", f"handler expression not understood: `{expr[:60]}`"


def _find_listener(files: _Files, el_id: str, tag: str) -> Optional[Dict[str, Any]]:
    """Handler bound by id from JS: addEventListener / jQuery .on / .click."""
    if not el_id:
        return None
    esc = re.escape(el_id)
    for rel in files.paths:
        if not rel.endswith(_JS_EXT) or _is_test_path(rel):
            continue
        text = files.text(rel)
        if el_id not in text:
            continue
        lookups = [rf"""document\.getElementById\(\s*['"]{esc}['"]\s*\)""",
                   rf"""document\.querySelector\(\s*['"]#{esc}['"]\s*\)""",
                   rf"""\$\(\s*['"]#{esc}['"]\s*\)"""]
        variables = [m.group(1) for m in re.finditer(
            rf"""([A-Za-z_$][\w$]*)\s*=\s*document\.(?:getElementById\(\s*['"]{esc}['"]|querySelector\(\s*['"]#{esc}['"])""",
            text)]
        subjects = lookups + [rf"(?<![\w$.]){re.escape(v)}" for v in variables]
        for subj in subjects:
            for m in re.finditer(
                    subj + r"""\s*\.\s*(?:addEventListener\(\s*['"](click|submit|change)['"]\s*,\s*|on\(\s*['"](click|submit|change)['"]\s*,\s*|(click|submit)\(\s*|onclick\s*=\s*|onsubmit\s*=\s*)""",
                    text):
                rest_start = m.end()
                tail = text[rest_start:rest_start + 400]
                line = _line_of(text, m.start())
                ident = re.match(r"\s*([A-Za-z_$][\w$]*)\s*[,)]", tail)
                if ident and ident.group(1) not in ("async", "function"):
                    return {"file": rel, "line": line, "name": ident.group(1)}
                # anonymous handler: the body starts on this line
                lines = files.lines(rel)
                end = _brace_end(lines, line - 1)
                return {"file": rel, "line": line, "name": "", "start": line, "end": end + 1}
    return None


def _find_handler(files: _Files, js: _JsModel, el: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve the handler of a UI element. Returns {name, file, start, end, line,
    certainty, reason}."""
    attrs = el["attrs"]
    hm = _HANDLER_ATTR_RE.search(attrs)
    expr = ""
    if hm:
        expr = next((g for g in hm.groups() if g), "")
    else:
        jm = _ONCLICK_JSX_RE.search(attrs)
        if jm:
            expr = jm.group(1)
    if expr:
        name, note = _handler_from_expr(expr)
        if not name:
            return {"certainty": "unresolved", "reason": note, "line": el["line"], "file": el["file"]}
        d, why = js.find(name, el["file"])
        if d:
            return {"name": name, "certainty": "resolved", "file": d["file"], "start": d["start"],
                    "end": d["end"], "line": el["line"], "def_file": d["file"], "bind_file": el["file"]}
        return {"name": name, "certainty": "unresolved", "reason": f"handler `{name}`: {why}",
                "line": el["line"], "file": el["file"]}
    idm = re.search(r"""\bid\s*=\s*["']([^"']+)["']""", attrs)
    bound = _find_listener(files, idm.group(1) if idm else "", el["tag"])
    if not bound and el["tag"] in ("button", "input"):
        # a submit button: the enclosing <form> carries the handler
        text = files.text(el["file"])
        head = text[:el["offset"]]
        fpos = head.rfind("<form")
        if fpos != -1 and head.rfind("</form") < fpos:
            fm = re.match(r"<form\b([^>]*)>", text[fpos:], re.S)
            if fm:
                fattrs = fm.group(1)
                fhm = _HANDLER_ATTR_RE.search(fattrs)
                fexpr = next((g for g in fhm.groups() if g), "") if fhm else ""
                if fexpr:
                    name, note = _handler_from_expr(fexpr)
                    d, why = js.find(name, el["file"]) if name else (None, note)
                    if d:
                        return {"name": name, "certainty": "resolved", "file": d["file"], "start": d["start"],
                                "end": d["end"], "line": _line_of(text, fpos), "def_file": d["file"],
                                "bind_file": el["file"]}
                fid = re.search(r"""\bid\s*=\s*["']([^"']+)["']""", fattrs)
                bound = _find_listener(files, fid.group(1) if fid else "", "form")
    if not bound:
        return {"certainty": "unresolved", "reason": "no onclick/onSubmit attribute and no listener bound by id was found",
                "line": el["line"], "file": el["file"]}
    if bound.get("name"):
        d, why = js.find(bound["name"], bound["file"])
        if d:
            return {"name": bound["name"], "certainty": "resolved", "file": d["file"], "start": d["start"],
                    "end": d["end"], "line": bound["line"], "def_file": d["file"], "bind_file": bound["file"]}
        return {"name": bound["name"], "certainty": "unresolved", "reason": f"handler `{bound['name']}`: {why}",
                "line": bound["line"], "file": bound["file"]}
    return {"name": "(inline listener)", "certainty": "resolved", "file": bound["file"], "start": bound["start"],
            "end": bound["end"], "line": bound["line"], "def_file": bound["file"], "bind_file": bound["file"]}


# ── backend routes ───────────────────────────────────────────────────────

_ROUTE_REG_RE = re.compile(
    r"""(?<![\w$])([A-Za-z_$][\w$]*)\.(get|post|put|patch|delete|all)\(\s*(['"`])(/[^'"`]*)\3\s*(?:,|\))""")
_USE_RE = re.compile(r"""([A-Za-z_$][\w$]*)\.use\(\s*(['"`])(/[^'"`]*)\2\s*,\s*([A-Za-z_$][\w$.()'"/]*)""")
_ROUTER_PREFIX_RE = re.compile(
    r"""^\s*([A-Za-z_]\w*)\s*=\s*(?:\w+\.)?(?:APIRouter|Blueprint)\(([^)]*)\)""", re.M | re.S)
_INCLUDE_RE = re.compile(r"""(?:include_router|register_blueprint)\(\s*([\w.]+)\s*(?:,([^)]*))?\)""", re.S)


def _py_prefixes(files: _Files) -> Tuple[Dict[Tuple[str, str], str], Dict[str, str]]:
    own: Dict[Tuple[str, str], str] = {}
    incl: Dict[str, str] = {}
    for rel in files.paths:
        if not rel.endswith(".py") or _is_test_path(rel):
            continue
        text = files.text(rel)
        for m in _ROUTER_PREFIX_RE.finditer(text):
            pm = re.search(r"""(?:prefix|url_prefix)\s*=\s*(['"])([^'"]*)\1""", m.group(2))
            if pm:
                own[(rel, m.group(1))] = pm.group(2)
        for m in _INCLUDE_RE.finditer(text):
            pm = re.search(r"""(?:prefix|url_prefix)\s*=\s*(['"])([^'"]*)\1""", m.group(2) or "")
            if pm:
                incl[m.group(1).split(".")[-1] if "." not in m.group(1) else m.group(1)] = pm.group(2)
    return own, incl


def _py_route_methods(lines: Sequence[str], def_line: int, verb: str) -> List[str]:
    if verb in _HTTP_VERBS:
        return [verb.upper()]
    if verb == "websocket":
        return ["WS"]
    window = " ".join(lines[max(0, def_line - 8):def_line])
    mm = re.search(r"methods\s*=\s*[\[(]([^\])]*)[\])]", window)
    if mm:
        ms = re.findall(r"""['"](\w+)['"]""", mm.group(1))
        if ms:
            return [m.upper() for m in ms]
    return ["GET"] if verb == "route" else ["ANY"]


def _backend_routes(root: str, project_id: str, files: _Files, js: _JsModel) -> List[Dict[str, Any]]:
    routes: List[Dict[str, Any]] = []
    own, incl = _py_prefixes(files)
    try:
        with ce_store.db() as conn:
            rows = conn.execute(
                "SELECT id, path, qualname, name, signature, start_line FROM code_symbols "
                "WHERE workspace = ? AND kind = 'route'", (root,)).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.debug("business_route: route query failed: %s", exc)
        rows = []
    for r in rows:
        if _is_test_path(str(r["path"])):
            continue
        sig = str(r["signature"] or "")
        parts = sig.split(" ", 1)
        dotted = parts[0]
        raw = parts[1].strip() if len(parts) > 1 else ""
        verb = dotted.rsplit(".", 1)[-1].lower()
        obj = dotted.rsplit(".", 1)[0].split(".")[-1] if "." in dotted else ""
        rel = str(r["path"])
        lines = files.lines(rel)
        prefix = own.get((rel, obj), "")
        stem = os.path.splitext(os.path.basename(rel))[0]
        outer = incl.get(f"{stem}.{obj}") or incl.get(obj) or ""
        full = (outer.rstrip("/") + "/" + prefix.strip("/") + "/" + raw.strip("/")).replace("//", "/")
        full = "/" + full.strip("/") if full.strip("/") else "/"
        routes.append({
            "lang": "python", "methods": _py_route_methods(lines, int(r["start_line"]), verb),
            "path": full, "declared": raw, "file": rel, "line": int(r["start_line"]),
            "symbol_id": str(r["id"]), "name": str(r["name"]), "qualname": str(r["qualname"]),
            "prefix_note": "prefix from APIRouter/Blueprint/include" if (prefix or outer) else "",
        })
    # Express/Koa-style registrations
    for rel in files.paths:
        if not rel.endswith((".js", ".ts", ".mjs", ".cjs")) or _is_test_path(rel):
            continue
        text = files.text(rel)
        if not re.search(r"express|Router\(|koa|fastify|\bapp\.(get|post)", text):
            continue
        prefixes = {m.group(4).split("(")[0]: m.group(3) for m in _USE_RE.finditer(text)}
        for m in _ROUTE_REG_RE.finditer(text):
            obj, verb, path = m.group(1), m.group(2), m.group(4)
            if obj in ("axios", "http", "api", "client", "$", "request", "fetch", "res", "req"):
                continue
            line = _line_of(text, m.start())
            call = _balanced(text, text.index("(", m.start() + len(obj)))
            tail = call[call.index(",") + 1:] if "," in call else ""
            idents = re.findall(r"(?<![\w$.'\"])([A-Za-z_$][\w$]*)(?=\s*[,)])", tail)
            inline = bool(re.search(r"=>|function\b", tail))
            handler = idents[-1] if idents and not inline and idents[-1] not in ("async", "function") else ""
            rec: Dict[str, Any] = {
                "lang": "javascript", "methods": ["ANY" if verb == "all" else verb.upper()],
                "path": (prefixes.get(obj, "").rstrip("/") + path) or "/", "declared": path, "file": rel,
                "line": line, "symbol_id": "", "name": handler or "(inline handler)", "qualname": handler,
                "prefix_note": "prefix from app.use" if obj in prefixes else "",
            }
            if handler:
                d, _why = js.find(handler, rel)
                if d:
                    rec.update(body_file=d["file"], body_start=d["start"], body_end=d["end"])
            else:
                end = _brace_end(files.lines(rel), line - 1)
                rec.update(body_file=rel, body_start=line, body_end=end + 1)
            routes.append(rec)
    return routes


# ── backend effects ──────────────────────────────────────────────────────

_SQL_WRITE_RE = re.compile(
    r"""(?i)(INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM|REPLACE\s+INTO|CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?|ALTER\s+TABLE)\s+[`"\[]?([A-Za-z_][\w.]*)""")
_SQL_READ_RE = re.compile(r"""(?i)\bSELECT\b[^'"`;]*?\bFROM\s+[`"\[]?([A-Za-z_][\w.]*)""")
_ORM_WRITE_RE = re.compile(
    r"""\b(?:self\.)?(?:session|db|s|sess|db_session|conn|connection)\.(add|add_all|merge|delete|bulk_save_objects)\(""")
_ORM_COMMIT_RE = re.compile(r"""\.(commit|flush)\(\s*\)""")
_ORM_MANAGER_RE = re.compile(r"""\.objects\.(create|bulk_create|update|delete|get_or_create|update_or_create)\(""")
_EXEC_DYN_RE = re.compile(r"""\.(execute|executemany|executescript)\(\s*([A-Za-z_]\w*)\s*[,)]""")
_FILE_WRITE_RES = [
    re.compile(r"""\bopen\([^)]*,\s*(?:mode\s*=\s*)?['"][wax][bt+]*['"]"""),
    re.compile(r"""\.write_(?:text|bytes)\("""),
    re.compile(r"""\bjson\.dump\("""), re.compile(r"""\bpickle\.dump\("""),
    re.compile(r"""\bshutil\.(?:copy|copyfile|move)\w*\("""), re.compile(r"""\.to_(?:csv|excel|parquet)\("""),
    re.compile(r"""\bfs\.(?:writeFile|appendFile|writeFileSync|appendFileSync|createWriteStream)\("""),
]


def _scan_effects(lines: Sequence[str], first_line: int, rel: str) -> List[Dict[str, Any]]:
    """Persistence statements in source `lines` (first_line is the 1-based number of lines[0])."""
    out: List[Dict[str, Any]] = []
    text = "\n".join(lines)
    sql_vars = {m.group(1) for m in re.finditer(
        r"""([A-Za-z_]\w*)\s*=\s*(?:f?['"`]{1,3})[^\n]*?\b(?:INSERT|UPDATE|DELETE|SELECT|CREATE|REPLACE)\b""",
        text, re.I)}
    for i, line in enumerate(lines):
        n = first_line + i
        stripped = line.strip()
        if stripped.startswith(("#", "//")):
            continue
        ev = f"{rel}:{n}"
        quoted = any(q in line for q in "'\"`")
        sm = _SQL_WRITE_RE.search(line) if quoted else None
        if sm:
            verb = re.sub(r"\s+", " ", sm.group(1).upper())
            out.append({"kind": "db_write", "label": f"{verb} {sm.group(2)}", "evidence": ev,
                        "table": sm.group(2), "verb": verb.split()[0]})
            continue
        rm = _SQL_READ_RE.search(line) if quoted else None
        if rm:
            out.append({"kind": "db_read", "label": f"SELECT ... FROM {rm.group(1)}", "evidence": ev,
                        "table": rm.group(1), "verb": "SELECT"})
            continue
        om = _ORM_WRITE_RE.search(line)
        if om:
            out.append({"kind": "db_write", "label": f"ORM session.{om.group(1)}", "evidence": ev,
                        "table": "", "verb": om.group(1).upper()})
            continue
        mm = _ORM_MANAGER_RE.search(line)
        if mm:
            out.append({"kind": "db_write", "label": f"ORM objects.{mm.group(1)}", "evidence": ev,
                        "table": "", "verb": mm.group(1).upper()})
            continue
        cm = _ORM_COMMIT_RE.search(line)
        if cm:
            out.append({"kind": "db_commit", "label": f"transaction {cm.group(1)}", "evidence": ev,
                        "table": "", "verb": cm.group(1).upper()})
            continue
        dm = _EXEC_DYN_RE.search(line)
        if dm and dm.group(2) not in sql_vars:
            out.append({"kind": "db_unresolved", "label": f"{dm.group(1)}({dm.group(2)}) with a computed statement",
                        "evidence": ev, "table": "", "verb": ""})
            continue
        for rx in _FILE_WRITE_RES:
            if rx.search(line):
                out.append({"kind": "file_write", "label": f"file write: {stripped[:70]}", "evidence": ev,
                            "table": "", "verb": "WRITE"})
                break
    return out


# ── python call walk ─────────────────────────────────────────────────────

def _sym_info(sid: str) -> Optional[Dict[str, Any]]:
    try:
        with ce_store.db() as conn:
            row = conn.execute("SELECT id, path, qualname, name, kind, start_line, end_line "
                               "FROM code_symbols WHERE id = ?", (sid,)).fetchone()
        return dict(row) if row else None
    except Exception:  # noqa: BLE001
        return None


def _out_edges(sid: str) -> List[Dict[str, Any]]:
    try:
        with ce_store.db() as conn:
            rows = conn.execute("SELECT dst, certainty, detail FROM code_edges WHERE src = ? AND kind = 'calls'",
                                (sid,)).fetchall()
        return [dict(r) for r in rows]
    except Exception:  # noqa: BLE001
        return []


def _call_line(files: _Files, info: Dict[str, Any], callee_name: str) -> int:
    lines = files.lines(info["path"])
    rx = re.compile(r"\b" + re.escape(callee_name) + r"\s*\(")
    for i in range(max(0, int(info["start_line"]) - 1), min(len(lines), int(info["end_line"]))):
        if rx.search(lines[i]) and i + 1 != int(info["start_line"]):
            return i + 1
    return int(info["start_line"])


class _Walker:
    def __init__(self, files: _Files, js: _JsModel, gaps: List[Dict[str, Any]], backend_syms: List[Dict[str, Any]]):
        self.files, self.js, self.gaps, self.backend_syms = files, js, gaps, backend_syms
        self.budget = _MAX_HOPS
        self.seen_gaps: set = set()

    def gap(self, g: Dict[str, Any]) -> None:
        key = (g["kind"], g["evidence"], g["label"])
        if key not in self.seen_gaps:
            self.seen_gaps.add(key)
            self.gaps.append(g)

    def py_node(self, sid: str, depth: int, visited: Tuple[str, ...], via_cert: str = "resolved",
                call_ev: str = "") -> List[Dict[str, Any]]:
        """Hops for function `sid` and what it reaches, pruned to branches that reach an effect."""
        info = _sym_info(sid)
        if not info or self.budget <= 0:
            return []
        self.budget -= 1
        lines = self.files.lines(info["path"])
        sl, el = int(info["start_line"]), int(info["end_line"])
        head = _hop("backend", "call", f"{info['qualname']}()", call_ev or f"{info['path']}:{sl}", via_cert,
                    symbol=info["qualname"], symbol_id=sid, file=info["path"], line=sl,
                    defined_at=f"{info['path']}:{sl}")
        children: List[Dict[str, Any]] = []
        has_effect = False
        for eff in _scan_effects(lines[sl - 1:el], sl, info["path"]):
            if eff["kind"] == "db_unresolved":
                self.gap(_gap("computed_sql", "unresolved", eff["label"], eff["evidence"],
                              "the statement is not a literal, so the table it writes cannot be named statically"))
                continue
            has_effect = has_effect or eff["kind"] in ("db_write", "file_write", "db_commit")
            children.append(_hop("effect", eff["kind"], eff["label"], eff["evidence"], "resolved",
                                 table=eff.get("table"), in_function=info["qualname"]))
        if depth < _MAX_DEPTH:
            for edge in _out_edges(sid):
                dst = str(edge["dst"])
                if dst in visited or dst == sid:
                    continue
                sub = _sym_info(dst)
                if not sub:
                    continue
                if str(edge["certainty"]) == "lexical":
                    self.gap(_gap("name-only", "name-only", f"{sub['qualname']}()",
                                  f"{info['path']}:{_call_line(self.files, info, sub['name'])}",
                                  f"{info['qualname']} calls a name that only matches {sub['qualname']} "
                                  f"({sub['path']}:{sub['start_line']}) lexically; not followed",
                                  candidate=f"{sub['path']}:{sub['start_line']}"))
                    continue
                if _is_test_path(sub["path"]):
                    continue
                ev = f"{info['path']}:{_call_line(self.files, info, sub['name'])}"
                children.extend(self.py_node(dst, depth + 1, visited + (sid,), "resolved", ev))
        if children and any(c["layer"] == "effect" or c.get("reaches_effect") for c in children):
            head["reaches_effect"] = True
        elif depth >= 2 and not has_effect and not any(c.get("reaches_effect") for c in children):
            return []   # a side branch that never reaches persistence
        return [head] + children

    def js_body(self, rel: str, start: int, end: int, depth: int, visited: Tuple[str, ...], owner: str,
                trace_layer: str = "backend") -> List[Dict[str, Any]]:
        """Effects and helper calls in a Node handler body."""
        lines = self.files.lines(rel)
        out: List[Dict[str, Any]] = []
        for eff in _scan_effects(lines[start - 1:end], start, rel):
            if eff["kind"] == "db_unresolved":
                self.gap(_gap("computed_sql", "unresolved", eff["label"], eff["evidence"],
                              "the statement is not a literal"))
                continue
            out.append(_hop("effect", eff["kind"], eff["label"], eff["evidence"], "resolved",
                            table=eff.get("table"), in_function=owner))
        if depth < 3:
            for name, ln in _js_calls("\n".join(lines[start - 1:end]), start):
                d, why = self.js.find(name, rel)
                if not d or d["name"] in visited:
                    continue
                if (d["file"], d["start"]) == (rel, start):
                    continue
                sub = self.js_body(d["file"], d["start"], d["end"], depth + 1, visited + (d["name"],), d["name"])
                if sub and any(h["layer"] == "effect" for h in sub):
                    out.append(_hop("backend", "call", f"{d['name']}()", f"{rel}:{ln}", "resolved",
                                    symbol=d["name"], file=d["file"], line=d["start"],
                                    defined_at=f"{d['file']}:{d['start']}", reaches_effect=True))
                    out.extend(sub)
        return out


# ── tests that cover a hop ───────────────────────────────────────────────

def _tests_for_route(files: _Files, route: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for rel in files.paths:
        if not _is_test_path(rel) or not rel.endswith((".py",) + _JS_EXT[:6]):
            continue
        for i, line in enumerate(files.lines(rel), 1):
            for m in re.finditer(r"""['"`](/[^'"`\s?#]*)""", line):
                url = m.group(1)
                if not _paths_match(url, route["path"]):
                    continue
                vm = re.search(r"""\.(get|post|put|patch|delete)\s*\(|method\s*[=:]\s*['"](\w+)['"]|fetch\(""", line)
                if vm:
                    verb = (vm.group(1) or vm.group(2) or "").upper()
                    if verb and not _methods_match([verb], route["methods"]):
                        continue
                out.append({"path": rel, "evidence": f"{rel}:{i}", "certainty": "resolved",
                            "via": "literal request to the route path in the test"})
                break
            else:
                continue
            break
    return out


def _tests_for_symbol(root: str, project_id: str, qualname: str) -> List[Dict[str, Any]]:
    try:
        from .query import impact as _impact
        res = _impact(qualname, workspace=root, project_id=project_id, depth=3, limit=60,
                      include_history=False, include_risk=False)
    except Exception as exc:  # noqa: BLE001
        logger.debug("business_route: impact(%s) failed: %s", qualname, exc)
        return []
    out = []
    for t in (res.get("affected_tests") or [])[:6]:
        out.append({"path": t, "evidence": t, "certainty": "resolved", "via": "call graph (impact)"})
    return out


# ── runtime trace ────────────────────────────────────────────────────────

def _norm_events(trace: Any) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    if not isinstance(trace, (list, tuple)):
        return events
    for raw in trace:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind") or "").lower()
        if kind not in ("http", "call", "sql"):
            continue
        path = str(raw.get("path") or "")
        if path:
            path = _clean_path(path)[0]
        events.append({"kind": kind, "method": str(raw.get("method") or "").upper(), "path": path,
                       "function": str(raw.get("function") or ""), "file": str(raw.get("file") or "").replace("\\", "/"),
                       "line": raw.get("line"), "sql": str(raw.get("sql") or raw.get("table") or ""),
                       "used": False})
    return events


def _fn_match(event_fn: str, qualname: str) -> bool:
    if not event_fn or not qualname:
        return False
    a, b = event_fn.replace("()", ""), qualname.replace("()", "")
    return a == b or a.endswith("." + b.split(".")[-1]) and b.split(".")[-1] == a.split(".")[-1] \
        or b.endswith("." + a) or a.split(".")[-1] == b.split(".")[-1]


def _apply_trace(hops: List[Dict[str, Any]], events: List[Dict[str, Any]]) -> None:
    def confirm(h: Dict[str, Any], ev: Dict[str, Any]) -> None:
        ev["used"] = True
        h["certainty"] = "observed"
        h["confirmed"] = True
        h.setdefault("observed_by", []).append(
            {"kind": ev["kind"], "method": ev["method"], "path": ev["path"], "function": ev["function"],
             "at": f"{ev['file']}:{ev['line']}" if ev["file"] else ""})

    for h in hops:
        if h["kind"] in ("http_call", "route"):
            methods = [h["method"]] if h.get("method") else ["ANY"]
            pattern = h.get("pattern") or h.get("route_path")
            if not pattern:
                continue
            for ev in events:
                if ev["kind"] != "http" or not ev["path"]:
                    continue
                if not _methods_match([ev["method"] or "ANY"], h.get("methods") or methods):
                    continue
                if _paths_match(ev["path"], pattern):
                    confirm(h, ev)
                    break
        elif h["kind"] == "call":
            for ev in events:
                if ev["kind"] == "call" and _fn_match(ev["function"], h.get("symbol", "")) and \
                        (not ev["file"] or h.get("file", "").endswith(ev["file"]) or ev["file"].endswith(h.get("file", "~"))):
                    confirm(h, ev)
                    break
        elif h["layer"] == "effect" and h["kind"].startswith(("db_", "file_")):
            for ev in events:
                if ev["kind"] != "sql":
                    continue
                table = (h.get("table") or "").lower()
                if (ev["function"] and _fn_match(ev["function"], h.get("in_function", ""))) or \
                        (table and table in ev["sql"].lower()):
                    confirm(h, ev)
                    break


# ── start / end resolution ───────────────────────────────────────────────

def _end_hit(end: str, hop: Dict[str, Any]) -> bool:
    if not end:
        return False
    e = _norm(end)
    if hop["layer"] == "effect" and hop["kind"] in ("db_write", "db_commit", "file_write") and \
            (set(e.split()) & _END_ANY_WRITE):
        return hop["kind"] != "db_commit"
    hay = _norm(" ".join([hop.get("label", ""), hop.get("table", "") or "", hop.get("symbol", "") or ""]))
    return bool(e) and (e in hay or e.replace(" ", "_") in (hop.get("label", "") + (hop.get("table") or "")).lower())


def _name_only_gaps(js_name: str, evidence: str, backend_syms: List[Dict[str, Any]], routes_taken: set,
                    what: str) -> List[Dict[str, Any]]:
    toks = _tokens(js_name)
    if not toks:
        return []
    need = min(2, len(toks))
    out = []
    for s in backend_syms:
        if s["id"] in routes_taken or s["path"].endswith(_JS_EXT) or evidence.startswith(s["path"] + ":"):
            continue
        shared = set(toks) & set(_tokens(s["name"]))
        if len(shared) >= need:
            out.append(_gap("name-only", "name-only", f"{s['qualname']}()", evidence,
                            f"{what}; `{s['qualname']}` ({s['path']}:{s['start_line']}) only looks similar "
                            f"(shared words: {', '.join(sorted(shared))}) and is not linked",
                            candidate=f"{s['path']}:{s['start_line']}"))
    return out[:5]


def _py_symbols(root: str) -> List[Dict[str, Any]]:
    try:
        with ce_store.db() as conn:
            rows = conn.execute("SELECT id, path, qualname, name, start_line FROM code_symbols WHERE workspace = ? "
                                "AND kind IN ('function','method','route')", (root,)).fetchall()
        return [dict(r) for r in rows if not _is_test_path(str(r["path"]))]
    except Exception:  # noqa: BLE001
        return []


# ── entry point ──────────────────────────────────────────────────────────

def business_route(root: str = "", start: str = "", end: Optional[str] = None,
                   runtime_trace: Optional[Sequence[Dict[str, Any]]] = None, *, project_id: str = "",
                   output_chars: int = 6000) -> Dict[str, Any]:
    """Follow one user action across the layers: UI element -> handler -> client
    request -> backend route -> service calls -> DB/file write, with the tests
    that cover each backend hop. `start` is the visible text (or id) of a button/
    link/form, `METHOD /path` of a route, or a symbol name; `end` is a free
    phrase ("saved", "INSERT INTO quotes", a table or function name); `runtime_trace`
    is an optional list of observed events (see module docstring)."""
    try:
        resolved = _root(root)
    except ValueError as exc:
        return {"error": str(exc), "exit_code": 1}
    start = str(start or "").strip()
    if not start:
        return {"error": "business_route: start is required (the visible text of the UI action, "
                         "'METHOD /path', or a symbol name)", "exit_code": 1}
    try:
        code_index.refresh(resolved, project_id=project_id, budget_files=20000)
    except Exception as exc:  # noqa: BLE001
        logger.warning("business_route: index refresh failed: %s", exc)

    files = _Files(resolved)
    js = _JsModel(files)
    routes = _backend_routes(resolved, project_id, files, js)
    backend_syms = _py_symbols(resolved)
    gaps: List[Dict[str, Any]] = []
    walker = _Walker(files, js, gaps, backend_syms)
    hops: List[Dict[str, Any]] = []
    events = _norm_events(runtime_trace)
    taken_routes: set = set()
    chains: List[Tuple[Dict[str, Any], int]] = []   # (route, index in hops of its route hop)

    def follow_route(route: Dict[str, Any], via: str) -> List[Dict[str, Any]]:
        taken_routes.add(route.get("symbol_id") or f"{route['file']}:{route['line']}")
        h = _hop("backend", "route", f"{'/'.join(route['methods'])} {route['path']}  ->  {route['qualname'] or route['name']}",
                 f"{route['file']}:{route['line']}", "resolved", methods=route["methods"], route_path=route["path"],
                 symbol=route["qualname"] or route["name"], file=route["file"], line=route["line"],
                 note=route.get("prefix_note"), via=via)
        out = [h]
        if route["lang"] == "python" and route.get("symbol_id"):
            sub = walker.py_node(route["symbol_id"], 1, (), "resolved", f"{route['file']}:{route['line']}")
            # the route handler itself is the route hop; keep only what it reaches
            out.extend(sub[1:] if sub else [])
            if sub and sub[0].get("reaches_effect"):
                h["reaches_effect"] = True
        elif route.get("body_file"):
            out.extend(walker.js_body(route["body_file"], route["body_start"], route["body_end"], 1,
                                      (route["qualname"],), route["qualname"] or "handler"))
        return out

    def match_routes(call: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [r for r in routes if _paths_match(call["pattern"], r["path"])
                and _methods_match([call["method"]], r["methods"] if call["method"] != "WS" else ["WS", "ANY"])]

    # ── 1. where does it start
    start_kind = "ui"
    ui = _find_ui(files, start)
    alternatives: List[str] = []
    calls_to_follow: List[Dict[str, Any]] = []
    handler_owner = ""
    if ui and ui[0]["score"] >= 1:
        el = ui[0]
        alternatives = [f"{u['file']}:{u['line']} \"{u['text'][:40]}\"" for u in ui[1:4]]
        hops.append(_hop("ui", "ui_event", f"{el['tag']} \"{el['text'][:60] or start}\"", f"{el['file']}:{el['line']}",
                         "resolved", element=el["tag"]))
        h = _find_handler(files, js, el)
        if h["certainty"] != "resolved":
            gaps.append(_gap("handler", "unresolved", f"handler of \"{el['text'][:40]}\"", f"{h['file']}:{h['line']}",
                             h.get("reason", "handler not resolved")))
        else:
            handler_owner = h["name"]
            hops.append(_hop("frontend", "handler", f"{h['name']}()", f"{h['bind_file']}:{h['line']}", "resolved",
                             defined_at=f"{h['def_file']}:{h['start']}", symbol=h["name"]))
            consts = js.consts.get(h["file"], {})
            text = "\n".join(files.lines(h["file"])[h["start"] - 1:h["end"]])
            seen_fn = {h["name"]}
            work: List[Tuple[str, int, int, str, int]] = [(h["file"], h["start"], h["end"], h["name"], 0)]
            helper_hops: List[Tuple[int, Dict[str, Any]]] = []
            ordered_calls: List[Tuple[Dict[str, Any], str]] = []
            while work:
                rel, s, e, owner, d = work.pop(0)
                body = "\n".join(files.lines(rel)[s - 1:e])
                consts = {**js.consts.get(rel, {})}
                for call in _scan_network(body, s, rel, consts):
                    call["owner"] = owner
                    ordered_calls.append((call, owner))
                if d < 3:
                    for name, ln in _js_calls(body, s):
                        sub, why = js.find(name, rel)
                        if sub and sub["name"] not in seen_fn:
                            seen_fn.add(sub["name"])
                            helper_hops.append((d, _hop("frontend", "handler_call", f"{sub['name']}()", f"{rel}:{ln}",
                                                       "resolved", defined_at=f"{sub['file']}:{sub['start']}",
                                                       symbol=sub["name"], owner=owner)))
                            work.append((sub["file"], sub["start"], sub["end"], sub["name"], d + 1))
                        elif not sub and "no definition" not in why:
                            gaps.append(_gap("handler_call", "unresolved", f"{name}()", f"{rel}:{ln}", why))
                        elif not sub and name not in js.defs and name.lower() not in _JS_BUILTINS \
                                and not re.match(r"^[A-Z]", name):
                            # a call with no JS definition: only a lookalike name exists on the backend
                            for g in _name_only_gaps(name, f"{rel}:{ln}", backend_syms, taken_routes,
                                                     f"the frontend calls `{name}()` but its definition was not found"):
                                gaps.append(g)
            hmap = {hh["symbol"]: hh for _d, hh in helper_hops}
            emitted: set = set()
            for call, owner in ordered_calls:
                if owner != h["name"] and owner in hmap and owner not in emitted:
                    emitted.add(owner)
                    call["pre_hop"] = hmap[owner]   # a helper without requests is not part of the route
                calls_to_follow.append(call)
    else:
        # not a UI label: a route ("POST /api/x") or a symbol
        m = re.match(r"^(?:(GET|POST|PUT|PATCH|DELETE)\s+)?(/\S*)$", start, re.I)
        if m:
            start_kind = "route"
            verb, path = (m.group(1) or "ANY").upper(), m.group(2)
            matched = [r for r in routes if _paths_match(path, r["path"]) and _methods_match([verb], r["methods"])]
            if not matched:
                gaps.append(_gap("start", "unresolved", start, "(start)", "no backend route matches this method and path"))
            for r in matched[:2]:
                hops.extend(follow_route(r, ""))
        else:
            sym = _resolve_symbol(start, root=resolved, project_id=project_id)
            if sym and not _is_test_path(str(sym.get("path", ""))) and \
                    str(sym.get("qualname", "")).split(".")[-1].lower() == start.lower().split(".")[-1]:
                start_kind = "symbol"
                walked = walker.py_node(str(sym["id"]), 1, (), "resolved", f"{sym['path']}:{sym['start_line']}")
                hops.extend(walked)
            else:
                gaps.append(_gap("start", "unresolved", start, "(start)",
                                 "no UI element with this text or id, no route and no symbol with this name were found"))
                for g in _name_only_gaps(start, "(start)", backend_syms, set(), f"`{start}` was not found"):
                    gaps.append(g)

    # ── 2. client calls -> routes
    unresolved_calls: List[Dict[str, Any]] = []
    for call in calls_to_follow:
        ev = f"{call['file']}:{call['line']}"
        if call.get("pre_hop"):
            hops.append(call["pre_hop"])
        if call["certainty"] == "unresolved":
            unresolved_calls.append(call)
            gaps.append(_gap("dynamic_url", "unresolved", f"{call['method']} {call['raw']}", ev,
                             call.get("reason", "URL not static"), function=call.get("owner")))
            for g in _name_only_gaps(call.get("owner", ""), ev, backend_syms, taken_routes,
                                     "the request URL is computed at run time"):
                gaps.append(g)
            continue
        label = f"{call['method']} {call['pattern']}"
        if call.get("host") and call["host"] not in ("localhost", "127.0.0.1") and not call["host"].startswith(("localhost:", "127.")):
            hops.append(_hop("frontend", "external_call", f"{label}  (host {call['host']})", ev, "resolved",
                             method=call["method"], pattern=call["pattern"]))
            continue
        hops.append(_hop("frontend", "http_call", label, ev, "resolved", method=call["method"],
                         pattern=call["pattern"], via=call.get("owner")))
        matched = match_routes(call)
        if not matched:
            gaps.append(_gap("no_backend_match", "unresolved", label, ev,
                             "no registered backend route matches this method and path"))
            continue
        if len(matched) > 1:
            gaps.append(_gap("ambiguous_route", "unresolved", label, ev,
                             f"{len(matched)} routes match: " + ", ".join(f"{r['file']}:{r['line']}" for r in matched[:4]) +
                             "; the first is followed"))
        hops.extend(follow_route(matched[0], label))

    # ── 3. runtime trace: confirm, then complete
    _apply_trace(hops, events)
    completed: List[int] = []
    for ev in [e for e in events if e["kind"] == "http" and not e["used"] and e["path"]]:
        r = next((r for r in routes if _paths_match(ev["path"], r["path"]) and
                  _methods_match([ev["method"] or "ANY"], r["methods"])), None)
        uc = next((c for c in unresolved_calls if not c.get("completed") and
                   (c["method"] in (ev["method"], "GET") or not ev["method"])), None) if r else None
        if not r or not uc:
            continue
        uc["completed"] = True
        ev["used"] = True
        label = f"{ev['method'] or uc['method']} {ev['path']}"
        h = _hop("frontend", "http_call", label, f"{uc['file']}:{uc['line']}", "observed", method=ev["method"] or uc["method"],
                 pattern=ev["path"], via=uc.get("owner"), confirmed=True, from_trace=True,
                 note="URL is dynamic in the source; the method and path come from the trace")
        h["confirmed"] = True
        sub = follow_route(r, label)
        hops.append(h)
        hops.extend(sub)
        _apply_trace(sub, events)
        completed.append(uc["line"])
        for g in gaps:
            if g["kind"] == "dynamic_url" and g["evidence"] == f"{uc['file']}:{uc['line']}":
                g["completed_by_trace"] = label

    # ── 4. tests per hop
    lookups = 0
    for h in hops:
        if h["layer"] != "backend":
            continue
        if h["kind"] == "route":
            r = next((r for r in routes if f"{r['file']}:{r['line']}" == h["evidence"]), None)
            if r:
                h["tests"] = _tests_for_route(files, r)
                h["_route_tests"] = h["tests"]
        elif h["kind"] == "call" and lookups < _MAX_TEST_LOOKUPS and h.get("symbol_id"):
            lookups += 1
            h["tests"] = _tests_for_symbol(resolved, project_id, h["symbol"])
    last_route_tests: List[Dict[str, Any]] = []
    for h in hops:
        if h["kind"] == "route":
            last_route_tests = h.pop("_route_tests", [])
        elif h["layer"] in ("backend", "effect") and last_route_tests:
            inherited = [{**t, "certainty": "resolved", "via": "inherited: the test calls the route that reaches this hop"}
                         for t in last_route_tests if t["path"] not in {x["path"] for x in h.get("tests", [])}]
            if inherited and h["kind"] in ("call",) or (inherited and h["layer"] == "effect"):
                h["tests"] = list(h.get("tests", [])) + inherited

    # ── 5. trace events that matched nothing are observed-only hops
    extra = [e for e in events if not e["used"]]
    for ev in extra:
        label = {"http": f"{ev['method']} {ev['path']}", "call": f"{ev['function']}()",
                 "sql": f"SQL in {ev['function'] or ev['file'] or '?'}: {ev['sql'][:50]}"}[ev["kind"]]
        hops.append(_hop("trace", f"observed_{ev['kind']}", label,
                         f"{ev['file']}:{ev['line']}" if ev["file"] else "(runtime trace)", "observed",
                         confirmed=True, from_trace=True,
                         note="seen at run time, with no matching static hop"))

    # ── 6. end
    reached = None
    if end:
        reached = False
        for h in hops:
            if _end_hit(end, h):
                h["is_end"] = True
                reached = True
        if not reached:
            gaps.append(_gap("end_not_reached", "unresolved", str(end), "(end)",
                             "no hop matches the requested end; the route may stop earlier, or persist through a computed statement"))
    elif not any(h["layer"] == "effect" and h["kind"] in ("db_write", "file_write") for h in hops):
        gaps.append(_gap("no_persistence", "unresolved", "(no write found)", "(route)",
                         "no database or file write was found along the resolved hops"))

    # number the hops; name-only items never become hops
    for n, h in enumerate(hops, 1):
        h["n"] = n
        h.pop("reaches_effect", None)
        h.pop("symbol_id", None)
    hops = hops[:_MAX_HOPS + 20]

    confirmed = [h["n"] for h in hops if h.get("confirmed")]
    by_cert: Dict[str, int] = {}
    for h in hops:
        by_cert[h["certainty"]] = by_cert.get(h["certainty"], 0) + 1
    for g in gaps:
        by_cert.setdefault("gaps:" + g["certainty"], 0)
        by_cert["gaps:" + g["certainty"]] += 1
    trace_info = {"provided": bool(events), "events": len(events), "confirmed_hops": confirmed,
                  "unmatched_events": [{k: v for k, v in e.items() if k != "used"} for e in extra]}
    if events:
        trace_info["completed_gaps"] = completed

    summary = _summary(start, start_kind, hops, gaps, reached, end)
    lines = [summary, ""]
    for h in hops:
        mark = "OBSERVED" if h["certainty"] == "observed" else h["certainty"]
        lines.append(f"{h['n']:>2}. [{mark}] {h['layer']}/{h['kind']}: {h['label']}  ({h['evidence']})"
                     + ("  <- END" if h.get("is_end") else ""))
        for t in (h.get("tests") or [])[:3]:
            lines.append(f"      test: {t['path']}  [{t['certainty']}; {t['via']}]")
    if gaps:
        lines.append("")
        lines.append("Gaps (not links):")
        for g in gaps:
            lines.append(f"  - [{g['certainty']}] {g['kind']}: {g['label']} ({g['evidence']}): {g['reason']}"
                         + (f"  [completed by trace: {g['completed_by_trace']}]" if g.get("completed_by_trace") else ""))
    return {"output": _clip("\n".join(lines), output_chars), "exit_code": 0, "root": resolved, "start": start,
            "start_kind": start_kind, "end": end, "reached_end": reached, "hops": hops, "gaps": gaps,
            "alternatives": alternatives, "certainty_counts": by_cert, "runtime_trace": trace_info,
            "summary": summary}


def _summary(start: str, kind: str, hops: List[Dict[str, Any]], gaps: List[Dict[str, Any]],
             reached: Optional[bool], end: Optional[str]) -> str:
    if not hops:
        return f"Nothing could be followed from \"{start}\": {gaps[0]['reason'] if gaps else 'no match'}."
    bits = []
    for h in hops:
        if h["kind"] == "ui_event":
            bits.append(f"pressing {h['label']} ({h['evidence']})")
        elif h["kind"] == "handler":
            bits.append(f"runs {h['label']} ({h['evidence']})")
        elif h["kind"] == "http_call":
            bits.append(f"sends {h['label']} ({h['evidence']})")
        elif h["kind"] == "route":
            bits.append(f"which {h['label'].split('  ->  ')[0]} handles in {h['label'].split('  ->  ')[-1]} ({h['evidence']})")
        elif h["kind"] == "call" and h["layer"] == "backend":
            bits.append(f"calling {h['label']} ({h['evidence']})")
        elif h["layer"] == "effect" and h["kind"] in ("db_write", "file_write"):
            bits.append(f"which does {h['label']} ({h['evidence']})")
    text = ("From " if kind == "ui" else "Starting at ") + "; ".join(bits[:8]) + "." if bits else "Hops found."
    seen_obs = sum(1 for h in hops if h.get("confirmed"))
    text += f" {len(hops)} hop(s), {seen_obs} confirmed at run time, {len(gaps)} gap(s)."
    if end:
        text += " The requested end was reached." if reached else " The requested end was NOT reached."
    return text
