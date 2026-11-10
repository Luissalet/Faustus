"""
brain/bases.py — Obsidian Bases (`.base` files) over the Cerebro's own notes.

A `.base` is a YAML file that describes database-like views of notes: global and
per-view filters, formula properties, display names, summaries, and table / cards /
list views with an order of columns, grouping, sorting and a limit. Obsidian renders
them; this module makes the Cerebro read, evaluate and write the same files, so a view
written in Obsidian opens in Faustus and the other way round.

What it does:

* :func:`query` evaluates a base (a vault path or inline YAML) against the indexed
  notes (`notes`/`links` tables: frontmatter, tags, links, sizes, times) and returns
  every view as columns, rows (or groups of rows) and summaries.
* The expression language is the one Bases uses: literals (numbers, 'strings',
  "strings", true/false/null, [lists], /regex/flags), properties (`status`,
  `note.status`, `file.name`, `formula.x`, `this`), operators (`== != > < >= <=`,
  `&& || !`, `+ - * / %`), global functions (`if date duration now today min max number
  link list file image icon html escapeHTML`) and the methods of each type: string,
  number, list (with `filter`/`map`/`reduce` over `value`, `index`, `acc`), date (fields,
  `format` with Moment tokens, `date`, `time`, `relative`), duration (`days`, `hours`…),
  file (`hasTag` with nested tags, `inFolder`, `hasLink`, `hasProperty`, `asLink`),
  link, object and regex. Date ± "1d"/duration, date − date → duration.
* Filters are a string or a recursive `and` / `or` / `not` object, globally and per view.
* Summaries: Average, Min, Max, Sum, Range, Median, Stddev, Earliest, Latest, Checked,
  Unchecked, Empty, Filled, Unique, and custom ones from the `summaries` section
  (`values.mean()` style formulas over the column's values).
* :func:`validate` checks YAML, keys, view types, every expression's syntax and every
  `formula.X` reference; :func:`save_base` writes a base into the vault (from YAML or
  from a small form spec) after validating it; :func:`list_bases` / :func:`read_base`
  / :func:`delete_base` manage the files.

What it does not do: the `map` view is evaluated like a table (no map is drawn);
`image()`, `icon()` and `html()` return their text; `this` is the base file itself
(there is no "active note" or embedding note on the server).
"""

from __future__ import annotations

import json
import math
import os
import re
import statistics
from datetime import date as _date, datetime, timedelta
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from . import db, vault

MAX_ROWS = 2000
VIEW_TYPES = ("table", "cards", "list", "map")
TOP_KEYS = {"filters", "formulas", "properties", "summaries", "views"}
VIEW_KEYS = {"type", "name", "limit", "groupBy", "filters", "order", "summaries", "sort", "columnSize",
             "image", "imageFit", "imageAspectRatio", "cardSize", "rowHeight", "coordinates", "markers",
             "defaultZoom", "center"}
DEFAULT_SUMMARIES = ("Average", "Min", "Max", "Sum", "Range", "Median", "Stddev", "Earliest", "Latest",
                     "Checked", "Unchecked", "Empty", "Filled", "Unique")
BASES_FOLDER = "Bases"


class BaseError(ValueError):
    pass


# ── values ───────────────────────────────────────────────────────────────

class Duration:
    __slots__ = ("ms",)

    def __init__(self, ms: float):
        self.ms = float(ms)

    def __eq__(self, other):
        return isinstance(other, Duration) and other.ms == self.ms

    def __lt__(self, other):
        return self.ms < other.ms

    def __repr__(self):
        return f"Duration({self.ms})"


class Link:
    __slots__ = ("path", "display")

    def __init__(self, path: str, display: Optional[str] = None):
        self.path, self.display = path, display

    def __eq__(self, other):
        return isinstance(other, Link) and other.path == self.path


class Regex:
    __slots__ = ("rx", "source")

    def __init__(self, source: str, flags: str = ""):
        f = 0
        if "i" in flags:
            f |= re.I
        if "m" in flags:
            f |= re.M
        if "s" in flags:
            f |= re.S
        self.source = source
        try:
            self.rx = re.compile(source, f)
        except re.error as exc:
            raise BaseError(f"bad regular expression /{source}/: {exc}")


class FileObj:
    """`file` of a note (or of the base itself, for `this`)."""

    def __init__(self, ctx: "_Store", path: str):
        self.ctx, self.path = ctx, path

    def __eq__(self, other):
        return isinstance(other, FileObj) and other.path == self.path

    def __hash__(self):
        return hash(self.path)

    def field(self, name: str) -> Any:
        rec = self.ctx.notes.get(self.path) or {}
        base = os.path.basename(self.path)
        if name == "name":
            return base
        if name == "basename":
            return os.path.splitext(base)[0]
        if name == "path":
            return self.path
        if name == "folder":
            return os.path.dirname(self.path).replace("\\", "/")
        if name == "ext":
            return os.path.splitext(base)[1].lstrip(".")
        if name == "size":
            return rec.get("size", 0)
        if name == "mtime":
            return rec.get("mtime_dt")
        if name == "ctime":
            return self.ctx.ctime(self.path)
        if name == "tags":
            return list(rec.get("tags") or [])
        if name == "links":
            return [Link(p or t, t) for t, p, emb in self.ctx.links.get(self.path, []) if not emb]
        if name == "embeds":
            return [Link(p or t, t) for t, p, emb in self.ctx.links.get(self.path, []) if emb]
        if name == "backlinks":
            return [Link(src) for src in sorted(self.ctx.backlinks.get(self.path, set()))]
        if name == "properties":
            return dict(rec.get("frontmatter") or {})
        if name == "file":
            return self
        raise BaseError(f"file has no field `{name}`")


_DUR_UNITS = {"y": 365 * 86400000, "year": 365 * 86400000, "years": 365 * 86400000,
              "M": 30 * 86400000, "month": 30 * 86400000, "months": 30 * 86400000,
              "w": 7 * 86400000, "week": 7 * 86400000, "weeks": 7 * 86400000,
              "d": 86400000, "day": 86400000, "days": 86400000,
              "h": 3600000, "hour": 3600000, "hours": 3600000,
              "m": 60000, "minute": 60000, "minutes": 60000,
              "s": 1000, "second": 1000, "seconds": 1000, "ms": 1, "millisecond": 1, "milliseconds": 1}


def parse_duration(text: Any) -> Duration:
    if isinstance(text, Duration):
        return text
    s = str(text or "").strip()
    total, found = 0.0, False
    for m in re.finditer(r"(-?\d+(?:\.\d+)?)\s*([A-Za-z]+)", s):
        unit = m.group(2)
        key = unit if unit in _DUR_UNITS else unit.lower()
        if key not in _DUR_UNITS:
            raise BaseError(f"unknown duration unit `{unit}` in {s!r}")
        total += float(m.group(1)) * _DUR_UNITS[key]
        found = True
    if not found:
        raise BaseError(f"not a duration: {s!r}")
    return Duration(total)


def to_date(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, _date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value / 1000.0)
    s = str(value).strip()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ](\d{1,2}):(\d{2})(?::(\d{2}))?)?", s)
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4) or 0),
                        int(m.group(5) or 0), int(m.group(6) or 0))
    except ValueError:
        return None


_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December"]
_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def moment_format(d: datetime, fmt: str) -> str:
    tokens = {
        "YYYY": f"{d.year:04d}", "YY": f"{d.year % 100:02d}", "MMMM": _MONTHS[d.month - 1], "MMM": _MONTHS[d.month - 1][:3],
        "MM": f"{d.month:02d}", "M": str(d.month), "DD": f"{d.day:02d}", "D": str(d.day),
        "dddd": _DAYS[d.weekday()], "ddd": _DAYS[d.weekday()][:3], "HH": f"{d.hour:02d}", "H": str(d.hour),
        "hh": f"{(d.hour % 12) or 12:02d}", "h": str((d.hour % 12) or 12), "mm": f"{d.minute:02d}", "m": str(d.minute),
        "ss": f"{d.second:02d}", "s": str(d.second), "A": "AM" if d.hour < 12 else "PM", "a": "am" if d.hour < 12 else "pm",
    }
    out, i = [], 0
    order = sorted(tokens, key=len, reverse=True)
    while i < len(fmt):
        if fmt[i] == "[":
            j = fmt.find("]", i)
            if j > i:
                out.append(fmt[i + 1:j])
                i = j + 1
                continue
        for tok in order:
            if fmt.startswith(tok, i):
                out.append(tokens[tok])
                i += len(tok)
                break
        else:
            out.append(fmt[i])
            i += 1
    return "".join(out)


def _truthy(v: Any) -> bool:
    if isinstance(v, (list, dict, str)):
        return len(v) > 0
    if isinstance(v, Duration):
        return v.ms != 0
    return bool(v)


def display(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() and abs(v) < 1e15 else f"{v:.6g}"
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d") if (v.hour, v.minute, v.second) == (0, 0, 0) else v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, _date):
        return v.isoformat()
    if isinstance(v, Duration):
        days = v.ms / 86400000
        return f"{days:.4g} days" if abs(days) >= 1 else f"{v.ms / 3600000:.4g} hours"
    if isinstance(v, Link):
        return v.display or os.path.splitext(os.path.basename(v.path))[0]
    if isinstance(v, FileObj):
        return os.path.splitext(os.path.basename(v.path))[0]
    if isinstance(v, Regex):
        return f"/{v.source}/"
    if isinstance(v, list):
        return ", ".join(display(x) for x in v)
    if isinstance(v, dict):
        return json.dumps({k: display(x) for k, x in v.items()}, ensure_ascii=False)
    return str(v)


def jsonable(v: Any) -> Any:
    if isinstance(v, (datetime, _date)):
        return v.isoformat()
    if isinstance(v, Duration):
        return {"duration_ms": v.ms}
    if isinstance(v, Link):
        return {"link": v.path, "display": v.display}
    if isinstance(v, FileObj):
        return {"file": v.path}
    if isinstance(v, Regex):
        return f"/{v.source}/"
    if isinstance(v, list):
        return [jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


# ── tokenizer and parser ─────────────────────────────────────────────────

_TOKEN_RE = re.compile(r"""
    (?P<ws>\s+)
  | (?P<num>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)
  | (?P<str>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')
  | (?P<op>==|!=|>=|<=|&&|\|\||[-+*/%<>!().,\[\]{}:])
  | (?P<id>[A-Za-z_$\u00C0-\uFFFF][\w$\u00C0-\uFFFF]*)
""", re.X)


def _tokenize(src: str) -> List[Tuple[str, Any]]:
    toks: List[Tuple[str, Any]] = []
    i = 0
    while i < len(src):
        prev = toks[-1] if toks else None
        operand_pos = prev is None or (prev[0] == "op" and prev[1] not in (")", "]", "}"))
        if src[i] == "/" and operand_pos:
            j, esc, in_class = i + 1, False, False
            while j < len(src):
                c = src[j]
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == "[":
                    in_class = True
                elif c == "]":
                    in_class = False
                elif c == "/" and not in_class:
                    break
                j += 1
            if j >= len(src):
                raise BaseError(f"unclosed regular expression in {src!r}")
            k = j + 1
            while k < len(src) and src[k] in "gimsuy":
                k += 1
            toks.append(("regex", (src[i + 1:j], src[j + 1:k])))
            i = k
            continue
        m = _TOKEN_RE.match(src, i)
        if not m:
            raise BaseError(f"unexpected character {src[i]!r} at {i} in {src!r}")
        kind = m.lastgroup
        text = m.group(kind)
        i = m.end()
        if kind == "ws":
            continue
        if kind == "num":
            toks.append(("num", float(text)))
        elif kind == "str":
            body = text[1:-1]
            toks.append(("str", re.sub(r"\\(.)", lambda mm: {"n": "\n", "t": "\t"}.get(mm.group(1), mm.group(1)), body)))
        elif kind == "id":
            toks.append(("id", text))
        else:
            toks.append(("op", text))
    toks.append(("end", None))
    return toks


_BINARY = {"||": 1, "&&": 2, "==": 3, "!=": 3, "<": 4, ">": 4, "<=": 4, ">=": 4, "+": 5, "-": 5, "*": 6, "/": 6, "%": 6}


class _Parser:
    def __init__(self, src: str):
        self.src = src
        self.toks = _tokenize(src)
        self.i = 0

    def peek(self):
        return self.toks[self.i]

    def take(self, kind=None, value=None):
        tok = self.toks[self.i]
        if (kind and tok[0] != kind) or (value is not None and tok[1] != value):
            want = value or kind
            got = tok[1] if tok[0] != "end" else "the end"
            raise BaseError(f"expected {want!r} but found {got!r} in {self.src!r}")
        self.i += 1
        return tok

    def parse(self):
        node = self.expr(0)
        self.take("end")
        return node

    def expr(self, min_prec):
        left = self.unary()
        while True:
            tok = self.peek()
            if tok[0] != "op" or tok[1] not in _BINARY or _BINARY[tok[1]] < min_prec:
                break
            prec = _BINARY[tok[1]]
            self.i += 1
            right = self.expr(prec + 1)
            left = ("bin", tok[1], left, right)
        return left

    def unary(self):
        tok = self.peek()
        if tok == ("op", "!"):
            self.i += 1
            return ("not", self.unary())
        if tok == ("op", "-"):
            self.i += 1
            return ("neg", self.unary())
        return self.postfix(self.primary())

    def args(self):
        out = []
        if self.peek() == ("op", ")"):
            self.i += 1
            return out
        while True:
            out.append(self.expr(0))
            tok = self.take("op")
            if tok[1] == ")":
                return out
            if tok[1] != ",":
                raise BaseError(f"expected ',' or ')' in {self.src!r}")

    def postfix(self, node):
        while True:
            tok = self.peek()
            if tok == ("op", "."):
                self.i += 1
                name = self.take("id")[1]
                if self.peek() == ("op", "("):
                    self.i += 1
                    node = ("method", node, name, self.args())
                else:
                    node = ("get", node, name)
            elif tok == ("op", "["):
                self.i += 1
                idx = self.expr(0)
                self.take("op", "]")
                node = ("index", node, idx)
            else:
                return node

    def primary(self):
        kind, val = self.take()
        if kind == "num":
            return ("lit", int(val) if val.is_integer() else val)
        if kind == "str":
            return ("lit", val)
        if kind == "regex":
            return ("regex", val[0], val[1])
        if kind == "id":
            if val == "true":
                return ("lit", True)
            if val == "false":
                return ("lit", False)
            if val in ("null", "undefined"):
                return ("lit", None)
            if self.peek() == ("op", "("):
                self.i += 1
                return ("call", val, self.args())
            return ("var", val)
        if kind == "op" and val == "(":
            node = self.expr(0)
            self.take("op", ")")
            return node
        if kind == "op" and val == "[":
            items = []
            if self.peek() != ("op", "]"):
                while True:
                    items.append(self.expr(0))
                    t = self.take("op")
                    if t[1] == "]":
                        break
                    if t[1] != ",":
                        raise BaseError(f"expected ',' or ']' in {self.src!r}")
            else:
                self.i += 1
            return ("list", items)
        if kind == "end":
            raise BaseError(f"the expression ends too early: {self.src!r}")
        raise BaseError(f"unexpected {val!r} in {self.src!r}")


_PARSE_CACHE: Dict[str, Any] = {}


def parse(src: Any) -> Any:
    text = str(src if src is not None else "").strip()
    if not text:
        raise BaseError("empty expression")
    node = _PARSE_CACHE.get(text)
    if node is None:
        node = _Parser(text).parse()
        if len(_PARSE_CACHE) > 4000:
            _PARSE_CACHE.clear()
        _PARSE_CACHE[text] = node
    return node


def formula_refs(node: Any) -> List[str]:
    """Every `formula.X` an expression names."""
    out: List[str] = []

    def walk(n):
        if isinstance(n, tuple):
            if n[0] == "get" and n[1] == ("var", "formula"):
                out.append(n[2])
            for part in n[1:]:
                if isinstance(part, tuple):
                    walk(part)
                elif isinstance(part, list):
                    for p in part:
                        walk(p)
    walk(node)
    return out


# ── evaluation ───────────────────────────────────────────────────────────

class _Store:
    """The indexed notes of one owner, loaded once per query."""

    def __init__(self, owner: str):
        self.owner = owner
        self.root = vault.vault_root(owner)
        self.notes: Dict[str, Dict[str, Any]] = {}
        self.links: Dict[str, List[Tuple[str, Optional[str], bool]]] = {}
        self.backlinks: Dict[str, set] = {}
        self._ctime: Dict[str, Optional[datetime]] = {}
        self.titles: Dict[str, str] = {}

    def load(self) -> "_Store":
        try:
            from . import notes as notes_mod
            notes_mod.reindex(self.owner)
        except Exception:  # noqa: BLE001 - a stale index still answers
            pass
        with db.db() as conn:
            for r in conn.execute("SELECT path, title, frontmatter, tags, mtime, size FROM notes WHERE owner=?",
                                  (self.owner,)).fetchall():
                fm = db.loads(r["frontmatter"], {}) or {}
                self.notes[r["path"]] = {
                    "title": r["title"], "frontmatter": fm if isinstance(fm, dict) else {},
                    "tags": [str(t).lstrip("#") for t in (db.loads(r["tags"], []) or [])],
                    "size": int(r["size"] or 0),
                    "mtime_dt": datetime.fromtimestamp(float(r["mtime"])) if r["mtime"] else None,
                }
                self.titles.setdefault(db.fold(r["title"] or ""), r["path"])
                self.titles.setdefault(db.fold(os.path.splitext(r["path"])[0]), r["path"])
            for r in conn.execute("SELECT src_path, target, dst_path, is_embed FROM links WHERE owner=?",
                                  (self.owner,)).fetchall():
                self.links.setdefault(r["src_path"], []).append((r["target"], r["dst_path"], bool(r["is_embed"])))
                if r["dst_path"]:
                    self.backlinks.setdefault(r["dst_path"], set()).add(r["src_path"])
        return self

    def ctime(self, path: str) -> Optional[datetime]:
        if path not in self._ctime:
            try:
                st = os.stat(os.path.join(self.root, *path.split("/")))
                self._ctime[path] = datetime.fromtimestamp(getattr(st, "st_birthtime", None) or st.st_ctime)
            except OSError:
                self._ctime[path] = None
        return self._ctime[path]

    def resolve(self, ref: Any) -> Optional[str]:
        if isinstance(ref, FileObj):
            return ref.path
        if isinstance(ref, Link):
            ref = ref.path
        s = str(ref or "").strip()
        m = re.fullmatch(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]", s)
        if m:
            s = m.group(1)
        if s in self.notes:
            return s
        if s + ".md" in self.notes:
            return s + ".md"
        return self.titles.get(db.fold(os.path.splitext(s)[0])) or self.titles.get(db.fold(s))


class _Scope:
    def __init__(self, store: _Store, path: Optional[str], formulas: Mapping[str, Any], this_path: Optional[str],
                 extra: Optional[Dict[str, Any]] = None):
        self.store, self.path, self.formulas, self.this_path = store, path, formulas, this_path
        self.extra = extra or {}
        self.cache: Dict[str, Any] = {}
        self.stack: List[str] = []

    def child(self, **extra) -> "_Scope":
        s = _Scope(self.store, self.path, self.formulas, self.this_path, {**self.extra, **extra})
        s.cache, s.stack = self.cache, self.stack
        return s

    def note_props(self) -> Dict[str, Any]:
        return (self.store.notes.get(self.path) or {}).get("frontmatter") or {}

    def formula(self, name: str) -> Any:
        if name in self.cache:
            return self.cache[name]
        if name not in self.formulas:
            raise BaseError(f"formula.{name} is not defined in `formulas`")
        if name in self.stack:
            raise BaseError(f"formula.{name} refers to itself ({' -> '.join(self.stack + [name])})")
        self.stack.append(name)
        try:
            value = evaluate(parse(self.formulas[name]), self)
        finally:
            self.stack.pop()
        self.cache[name] = value
        return value


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    return None


def _coerce_pair(a: Any, b: Any) -> Tuple[Any, Any]:
    if isinstance(a, (datetime, _date)) or isinstance(b, (datetime, _date)):
        return to_date(a), to_date(b)
    if isinstance(a, Duration) or isinstance(b, Duration):
        return (a.ms if isinstance(a, Duration) else _num(a)), (b.ms if isinstance(b, Duration) else _num(b))
    if isinstance(a, (int, float)) and not isinstance(a, bool) and isinstance(b, str):
        return a, _num(b)
    if isinstance(b, (int, float)) and not isinstance(b, bool) and isinstance(a, str):
        return _num(a), b
    if isinstance(a, Link) or isinstance(b, Link) or isinstance(a, FileObj) or isinstance(b, FileObj):
        return display(a), display(b)
    return a, b


def _binary(op: str, a: Any, b: Any) -> Any:
    if op in ("==", "!="):
        x, y = _coerce_pair(a, b)
        eq = x == y
        return eq if op == "==" else not eq
    if op in ("<", ">", "<=", ">="):
        x, y = _coerce_pair(a, b)
        if x is None or y is None:
            return False
        try:
            return {"<": x < y, ">": x > y, "<=": x <= y, ">=": x >= y}[op]
        except TypeError:
            return False
    if op == "+":
        if isinstance(a, datetime):
            return a + timedelta(milliseconds=parse_duration(b).ms)
        if isinstance(b, datetime):
            return b + timedelta(milliseconds=parse_duration(a).ms)
        if isinstance(a, Duration) and isinstance(b, Duration):
            return Duration(a.ms + b.ms)
        if isinstance(a, str) or isinstance(b, str):
            return display(a) + display(b)
        if isinstance(a, list) and isinstance(b, list):
            return a + b
    if op == "-":
        if isinstance(a, datetime) and isinstance(b, (datetime, _date)):
            return Duration((a - to_date(b)).total_seconds() * 1000)
        if isinstance(a, datetime):
            return a - timedelta(milliseconds=parse_duration(b).ms)
        if isinstance(a, Duration) and isinstance(b, Duration):
            return Duration(a.ms - b.ms)
    if op == "*" and isinstance(a, Duration):
        return Duration(a.ms * (_num(b) or 0))
    if op == "/" and isinstance(a, Duration):
        n = _num(b)
        return Duration(a.ms / n) if n else None
    x, y = _num(a), _num(b)
    if x is None or y is None:
        return None
    if op == "+":
        return x + y
    if op == "-":
        return x - y
    if op == "*":
        return x * y
    if op == "/":
        return x / y if y else None
    if op == "%":
        return math.fmod(x, y) if y else None
    raise BaseError(f"unknown operator {op}")


def _field(obj: Any, name: str, scope: _Scope) -> Any:
    if isinstance(obj, FileObj):
        return obj.field(name)
    if isinstance(obj, datetime):
        if name in ("year", "month", "day", "hour", "minute", "second"):
            return getattr(obj, name)
        if name == "millisecond":
            return obj.microsecond // 1000
    if isinstance(obj, Duration):
        div = {"days": 86400000, "hours": 3600000, "minutes": 60000, "seconds": 1000, "milliseconds": 1}.get(name)
        if div:
            return obj.ms / div
    if isinstance(obj, (str, list)) and name == "length":
        return len(obj)
    if isinstance(obj, dict):
        return obj.get(name)
    if isinstance(obj, Link):
        if name in ("path",):
            return obj.path
        target = scope.store.resolve(obj)
        return FileObj(scope.store, target).field(name) if target else None
    if obj is None:
        return None
    return None


def _str_method(s: str, name: str, a: List[Any]) -> Any:
    if name == "contains":
        return str(a[0]) in s
    if name == "containsAll":
        return all(str(x) in s for x in a)
    if name == "containsAny":
        return any(str(x) in s for x in a)
    if name == "startsWith":
        return s.startswith(str(a[0]))
    if name == "endsWith":
        return s.endswith(str(a[0]))
    if name == "isEmpty":
        return len(s) == 0
    if name == "lower":
        return s.lower()
    if name == "upper":
        return s.upper()
    if name == "title":
        return s.title()
    if name == "trim":
        return s.strip()
    if name == "replace":
        pat, rep = a[0], display(a[1]) if len(a) > 1 else ""
        return pat.rx.sub(rep, s) if isinstance(pat, Regex) else s.replace(str(pat), rep)
    if name == "repeat":
        return s * int(_num(a[0]) or 0)
    if name == "reverse":
        return s[::-1]
    if name == "slice":
        return s[int(_num(a[0]) or 0): (int(_num(a[1])) if len(a) > 1 and a[1] is not None else None)]
    if name == "split":
        sep = a[0] if a else ","
        parts = sep.rx.split(s) if isinstance(sep, Regex) else s.split(str(sep))
        return parts[: int(_num(a[1]))] if len(a) > 1 else parts
    if name == "toString":
        return s
    if name == "isTruthy":
        return _truthy(s)
    raise BaseError(f"strings have no method {name}()")


def _num_method(x: float, name: str, a: List[Any]) -> Any:
    if name == "abs":
        return abs(x)
    if name == "ceil":
        return float(math.ceil(x))
    if name == "floor":
        return float(math.floor(x))
    if name == "round":
        digits = int(_num(a[0]) or 0) if a else 0
        q = 10 ** digits
        return math.floor(x * q + 0.5) / q
    if name == "toFixed":
        return f"{x:.{int(_num(a[0]) or 0)}f}"
    if name == "isEmpty":
        return False
    if name == "toString":
        return display(x)
    if name == "isTruthy":
        return _truthy(x)
    raise BaseError(f"numbers have no method {name}()")


def _date_method(d: datetime, name: str, a: List[Any]) -> Any:
    if name == "date":
        return datetime(d.year, d.month, d.day)
    if name == "format":
        return moment_format(d, str(a[0]) if a else "YYYY-MM-DD")
    if name == "time":
        return d.strftime("%H:%M:%S")
    if name == "relative":
        delta = datetime.now() - d
        days = delta.days
        if abs(days) < 1:
            hours = int(delta.total_seconds() // 3600)
            return "just now" if hours == 0 else (f"{hours} hours ago" if hours > 0 else f"in {-hours} hours")
        return f"{days} days ago" if days > 0 else f"in {-days} days"
    if name == "isEmpty":
        return False
    if name == "toString":
        return display(d)
    if name == "isTruthy":
        return True
    raise BaseError(f"dates have no method {name}()")


def _flat(xs: List[Any]) -> List[Any]:
    out = []
    for x in xs:
        out.extend(x if isinstance(x, list) else [x])
    return out


def _sort_key(v: Any):
    if v is None:
        return (2, "")
    if isinstance(v, bool):
        return (0, float(v))
    if isinstance(v, (int, float)):
        return (0, float(v))
    if isinstance(v, (datetime, _date)):
        return (0, to_date(v).timestamp())
    if isinstance(v, Duration):
        return (0, v.ms)
    return (1, db.fold(display(v)))


def _in_list(lst: List[Any], item: Any) -> bool:
    return any(_binary("==", x, item) for x in lst)


def _list_method(lst: List[Any], name: str, raw: List[Any], scope: _Scope) -> Any:
    if name in ("filter", "map", "reduce"):
        if not raw:
            raise BaseError(f"list.{name}() needs an expression")
        if name == "reduce":
            acc = evaluate(raw[1], scope) if len(raw) > 1 else None
            for i, v in enumerate(lst):
                acc = evaluate(raw[0], scope.child(value=v, index=i, acc=acc))
            return acc
        out = []
        for i, v in enumerate(lst):
            r = evaluate(raw[0], scope.child(value=v, index=i))
            if name == "map":
                out.append(r)
            elif _truthy(r):
                out.append(v)
        return out
    a = [evaluate(x, scope) for x in raw]
    if name == "contains":
        return _in_list(lst, a[0])
    if name == "containsAll":
        return all(_in_list(lst, x) for x in a)
    if name == "containsAny":
        return any(_in_list(lst, x) for x in a)
    if name == "flat":
        return _flat(lst)
    if name == "join":
        return (display(a[0]) if a else ",").join(display(x) for x in lst)
    if name == "reverse":
        return list(reversed(lst))
    if name == "slice":
        return lst[int(_num(a[0]) or 0): (int(_num(a[1])) if len(a) > 1 and a[1] is not None else None)]
    if name == "sort":
        return sorted(lst, key=_sort_key)
    if name == "unique":
        out = []
        for x in lst:
            if not _in_list(out, x):
                out.append(x)
        return out
    if name == "isEmpty":
        return len(lst) == 0
    nums = [n for n in (_num(x) for x in lst) if n is not None]
    if name in ("mean", "average"):
        return statistics.fmean(nums) if nums else None
    if name == "sum":
        return sum(nums)
    if name == "min":
        return min(nums) if nums else None
    if name == "max":
        return max(nums) if nums else None
    if name == "median":
        return statistics.median(nums) if nums else None
    if name in ("stddev", "stdev"):
        return statistics.stdev(nums) if len(nums) > 1 else 0.0
    if name == "toString":
        return display(lst)
    if name == "isTruthy":
        return _truthy(lst)
    raise BaseError(f"lists have no method {name}()")


def _file_method(f: FileObj, name: str, a: List[Any], scope: _Scope) -> Any:
    if name == "hasTag":
        tags = [str(t).lstrip("#").lower() for t in f.field("tags")]
        for want in a:
            w = str(want).lstrip("#").lower()
            if any(t == w or t.startswith(w + "/") for t in tags):
                return True
        return False
    if name == "inFolder":
        folder = str(a[0] if a else "").strip("/").replace("\\", "/")
        own = f.field("folder")
        return own == folder or own.startswith(folder + "/") or folder == ""
    if name == "hasLink":
        target = scope.store.resolve(a[0])
        return bool(target) and any((p or "") == target for _t, p, _e in scope.store.links.get(f.path, []))
    if name == "hasProperty":
        return str(a[0]) in (f.field("properties") or {})
    if name == "asLink":
        return Link(f.path, display(a[0]) if a else None)
    if name == "isEmpty":
        return False
    if name == "toString":
        return display(f)
    if name == "isTruthy":
        return True
    raise BaseError(f"files have no method {name}()")


def _call_method(obj: Any, name: str, raw: List[Any], scope: _Scope) -> Any:
    if isinstance(obj, list):
        return _list_method(obj, name, raw, scope)
    a = [evaluate(x, scope) for x in raw]
    if name == "isType":
        want = str(a[0]).lower() if a else ""
        kinds = {"string": str, "number": (int, float), "boolean": bool, "list": list, "object": dict,
                 "date": datetime, "duration": Duration, "file": FileObj, "link": Link}
        t = kinds.get(want)
        return bool(t) and isinstance(obj, t) and not (want == "number" and isinstance(obj, bool))
    if name == "isTruthy":
        return _truthy(obj)
    if obj is None:
        if name == "isEmpty":
            return True
        if name == "toString":
            return ""
        return None
    if isinstance(obj, FileObj):
        return _file_method(obj, name, a, scope)
    if isinstance(obj, Link):
        if name == "asFile":
            target = scope.store.resolve(obj)
            return FileObj(scope.store, target) if target else None
        if name == "linksTo":
            target = scope.store.resolve(a[0]) if a else None
            return bool(target) and scope.store.resolve(obj) == target
        if name == "toString":
            return display(obj)
        target = scope.store.resolve(obj)
        return _file_method(FileObj(scope.store, target), name, a, scope) if target else None
    if isinstance(obj, Regex):
        if name == "matches":
            return bool(obj.rx.search(display(a[0]) if a else ""))
        raise BaseError(f"regular expressions have no method {name}()")
    if isinstance(obj, datetime):
        return _date_method(obj, name, a)
    if isinstance(obj, Duration):
        if name in ("days", "hours", "minutes", "seconds"):
            return _field(obj, name, scope)
        if name == "toString":
            return display(obj)
        raise BaseError("a duration has no methods: read .days, .hours… first, then round")
    if isinstance(obj, bool):
        if name == "toString":
            return display(obj)
        if name == "isEmpty":
            return False
        raise BaseError(f"booleans have no method {name}()")
    if isinstance(obj, (int, float)):
        return _num_method(float(obj), name, a)
    if isinstance(obj, str):
        return _str_method(obj, name, a)
    if isinstance(obj, dict):
        if name == "isEmpty":
            return len(obj) == 0
        if name == "keys":
            return list(obj.keys())
        if name == "values":
            return list(obj.values())
        if name == "toString":
            return display(obj)
        raise BaseError(f"objects have no method {name}()")
    raise BaseError(f"cannot call {name}() on {type(obj).__name__}")


def _call_global(name: str, raw: List[Any], scope: _Scope) -> Any:
    if name == "if":
        if not raw:
            raise BaseError("if() needs a condition")
        cond = evaluate(raw[0], scope)
        if _truthy(cond):
            return evaluate(raw[1], scope) if len(raw) > 1 else None
        return evaluate(raw[2], scope) if len(raw) > 2 else None
    a = [evaluate(x, scope) for x in raw]
    if name == "date":
        d = to_date(a[0] if a else None)
        if d is None and a and a[0] not in (None, ""):
            raise BaseError(f"date(): cannot read {display(a[0])!r} as a date (YYYY-MM-DD HH:mm:ss)")
        return d
    if name == "duration":
        return parse_duration(a[0] if a else "")
    if name == "now":
        return datetime.now().replace(microsecond=0)
    if name == "today":
        n = datetime.now()
        return datetime(n.year, n.month, n.day)
    if name in ("min", "max"):
        nums = [n for n in (_num(x) for x in _flat(a)) if n is not None]
        return (min if name == "min" else max)(nums) if nums else None
    if name == "number":
        if isinstance(a[0] if a else None, (datetime,)):
            return a[0].timestamp() * 1000
        if isinstance(a[0] if a else None, Duration):
            return a[0].ms
        return _num(a[0] if a else None)
    if name == "link":
        target = a[0] if a else ""
        path = target.path if isinstance(target, (FileObj, Link)) else (scope.store.resolve(target) or str(target))
        return Link(path, display(a[1]) if len(a) > 1 else None)
    if name == "list":
        v = a[0] if a else None
        return v if isinstance(v, list) else ([] if v is None else [v])
    if name == "file":
        target = scope.store.resolve(a[0] if a else "")
        return FileObj(scope.store, target) if target else None
    if name in ("image", "icon", "html"):
        return display(a[0] if a else "")
    if name == "escapeHTML":
        import html as _html
        return _html.escape(display(a[0] if a else ""))
    raise BaseError(f"unknown function {name}()")


def evaluate(node: Any, scope: _Scope) -> Any:
    kind = node[0]
    if kind == "lit":
        return node[1]
    if kind == "list":
        return [evaluate(x, scope) for x in node[1]]
    if kind == "regex":
        return Regex(node[1], node[2])
    if kind == "var":
        name = node[1]
        if name in scope.extra:
            return scope.extra[name]
        if name == "file":
            return FileObj(scope.store, scope.path) if scope.path else None
        if name == "note":
            return scope.note_props()
        if name == "formula":
            return {k: scope.formula(k) for k in scope.formulas}
        if name == "this":
            return FileObj(scope.store, scope.this_path) if scope.this_path else None
        return scope.note_props().get(name)
    if kind == "get":
        target, name = node[1], node[2]
        if target == ("var", "formula") and "formula" not in scope.extra:
            return scope.formula(name)
        if target == ("var", "note") and "note" not in scope.extra:
            return scope.note_props().get(name)
        return _field(evaluate(target, scope), name, scope)
    if kind == "index":
        obj, idx = evaluate(node[1], scope), evaluate(node[2], scope)
        if isinstance(obj, list):
            i = _num(idx)
            return obj[int(i)] if i is not None and -len(obj) <= int(i) < len(obj) else None
        if isinstance(obj, dict):
            return obj.get(display(idx))
        if isinstance(obj, str):
            i = _num(idx)
            return obj[int(i)] if i is not None and -len(obj) <= int(i) < len(obj) else None
        return _field(obj, display(idx), scope)
    if kind == "method":
        return _call_method(evaluate(node[1], scope), node[2], node[3], scope)
    if kind == "call":
        return _call_global(node[1], node[2], scope)
    if kind == "not":
        return not _truthy(evaluate(node[1], scope))
    if kind == "neg":
        v = _num(evaluate(node[1], scope))
        return -v if v is not None else None
    if kind == "bin":
        op = node[1]
        if op == "&&":
            left = evaluate(node[2], scope)
            return evaluate(node[3], scope) if _truthy(left) else left
        if op == "||":
            left = evaluate(node[2], scope)
            return left if _truthy(left) else evaluate(node[3], scope)
        return _binary(op, evaluate(node[2], scope), evaluate(node[3], scope))
    raise BaseError(f"cannot evaluate {kind}")


# ── filters, views, summaries ────────────────────────────────────────────

def _check_filter(spec: Any, where: str, errors: List[str]) -> None:
    if spec is None:
        return
    if isinstance(spec, str):
        try:
            parse(spec)
        except BaseError as exc:
            errors.append(f"{where}: {exc}")
        return
    if isinstance(spec, Mapping):
        keys = list(spec.keys())
        if len(keys) != 1 or keys[0] not in ("and", "or", "not"):
            errors.append(f"{where}: a filter object has exactly one key, `and`, `or` or `not` (got {keys})")
            return
        items = spec[keys[0]]
        if not isinstance(items, list):
            errors.append(f"{where}.{keys[0]}: must be a list")
            return
        for i, it in enumerate(items):
            _check_filter(it, f"{where}.{keys[0]}[{i}]", errors)
        return
    if isinstance(spec, bool):
        return
    errors.append(f"{where}: a filter is a string or an and/or/not object")


def _match(spec: Any, scope: _Scope) -> bool:
    if spec is None:
        return True
    if isinstance(spec, bool):
        return spec
    if isinstance(spec, str):
        return _truthy(evaluate(parse(spec), scope))
    key = next(iter(spec))
    items = spec[key] or []
    if key == "and":
        return all(_match(x, scope) for x in items)
    if key == "or":
        return any(_match(x, scope) for x in items)
    return not any(_match(x, scope) for x in items)


def _prop_value(prop: str, scope: _Scope) -> Any:
    prop = str(prop)
    if prop.startswith("formula."):
        return scope.formula(prop[len("formula."):])
    if prop.startswith("file."):
        return FileObj(scope.store, scope.path).field(prop[len("file."):])
    if prop.startswith("note."):
        return scope.note_props().get(prop[len("note."):])
    return scope.note_props().get(prop)


def _label(prop: str, properties: Mapping[str, Any]) -> str:
    conf = properties.get(prop) or properties.get(prop.replace("note.", "", 1)) or {}
    if isinstance(conf, Mapping) and conf.get("displayName") is not None:
        return str(conf.get("displayName"))
    if prop.startswith("note."):
        return prop[5:]
    if prop.startswith("formula."):
        return prop[8:]
    return prop


def _summary(name: str, values: List[Any], custom: Mapping[str, Any], scope: _Scope) -> Any:
    if name in custom:
        return evaluate(parse(custom[name]), scope.child(values=values))
    nums = [n for n in (v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool))]
    dates = [to_date(v) for v in values if isinstance(v, (datetime, _date))]
    filled = [v for v in values if v not in (None, "", [])]
    if name == "Average":
        return statistics.fmean(nums) if nums else None
    if name == "Min":
        return min(nums) if nums else None
    if name == "Max":
        return max(nums) if nums else None
    if name == "Sum":
        return sum(nums)
    if name == "Range":
        if nums:
            return max(nums) - min(nums)
        return Duration((max(dates) - min(dates)).total_seconds() * 1000) if dates else None
    if name == "Median":
        return statistics.median(nums) if nums else None
    if name == "Stddev":
        return statistics.stdev(nums) if len(nums) > 1 else (0.0 if nums else None)
    if name == "Earliest":
        return min(dates) if dates else None
    if name == "Latest":
        return max(dates) if dates else None
    if name == "Checked":
        return sum(1 for v in values if v is True)
    if name == "Unchecked":
        return sum(1 for v in values if v is False)
    if name == "Empty":
        return len(values) - len(filled)
    if name == "Filled":
        return len(filled)
    if name == "Unique":
        return len({display(v) for v in filled})
    raise BaseError(f"unknown summary {name!r}")


def _yaml_load(text: str) -> Any:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - PyYAML ships with the app
        raise BaseError("PyYAML is not installed") from exc
    try:
        return yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise BaseError(f"the base is not valid YAML: {exc}")


def validate(text_or_doc: Any) -> Dict[str, Any]:
    """{ok, errors, warnings, views} for a base given as YAML text or a parsed dict."""
    errors: List[str] = []
    warnings: List[str] = []
    try:
        doc = _yaml_load(text_or_doc) if isinstance(text_or_doc, str) else (text_or_doc or {})
    except BaseError as exc:
        return {"ok": False, "errors": [str(exc)], "warnings": [], "views": []}
    if not isinstance(doc, Mapping):
        return {"ok": False, "errors": ["a base is a YAML mapping (filters, formulas, properties, summaries, views)"],
                "warnings": [], "views": []}
    for k in doc:
        if k not in TOP_KEYS:
            warnings.append(f"unknown top-level key `{k}` (kept, not used)")
    formulas = doc.get("formulas") or {}
    if not isinstance(formulas, Mapping):
        errors.append("`formulas` must be a mapping name: expression")
        formulas = {}
    for name, expr in formulas.items():
        try:
            node = parse(expr)
            for ref in formula_refs(node):
                if ref not in formulas:
                    errors.append(f"formulas.{name}: refers to formula.{ref}, which is not defined")
        except BaseError as exc:
            errors.append(f"formulas.{name}: {exc}")
    custom = doc.get("summaries") or {}
    if not isinstance(custom, Mapping):
        errors.append("`summaries` must be a mapping name: expression")
        custom = {}
    for name, expr in custom.items():
        try:
            parse(expr)
        except BaseError as exc:
            errors.append(f"summaries.{name}: {exc}")
    _check_filter(doc.get("filters"), "filters", errors)
    views = doc.get("views") or []
    if not isinstance(views, list):
        errors.append("`views` must be a list")
        views = []
    if not views:
        warnings.append("no views: a default table of every matching note is used")
    names = []
    for i, v in enumerate(views):
        where = f"views[{i}]"
        if not isinstance(v, Mapping):
            errors.append(f"{where}: must be a mapping")
            continue
        names.append(str(v.get("name") or f"View {i + 1}"))
        if str(v.get("type") or "table") not in VIEW_TYPES:
            errors.append(f"{where}.type: {v.get('type')!r} is not one of {', '.join(VIEW_TYPES)}")
        for k in v:
            if k not in VIEW_KEYS:
                warnings.append(f"{where}: unknown key `{k}` (kept, not used)")
        _check_filter(v.get("filters"), f"{where}.filters", errors)
        order = v.get("order") or []
        if not isinstance(order, list):
            errors.append(f"{where}.order: must be a list of properties")
            order = []
        for prop in order + [((v.get("groupBy") or {}).get("property") if isinstance(v.get("groupBy"), Mapping) else None)] + \
                list((v.get("summaries") or {}).keys() if isinstance(v.get("summaries"), Mapping) else []):
            if isinstance(prop, str) and prop.startswith("formula.") and prop[8:] not in formulas:
                errors.append(f"{where}: {prop} is used but not defined in `formulas`")
        sums = v.get("summaries") or {}
        if isinstance(sums, Mapping):
            for prop, sname in sums.items():
                if str(sname) not in DEFAULT_SUMMARIES and str(sname) not in custom:
                    errors.append(f"{where}.summaries.{prop}: unknown summary {sname!r}")
        if v.get("limit") is not None and not isinstance(v.get("limit"), int):
            errors.append(f"{where}.limit: must be a whole number")
    props = doc.get("properties") or {}
    if isinstance(props, Mapping):
        for prop in props:
            if str(prop).startswith("formula.") and str(prop)[8:] not in formulas:
                warnings.append(f"properties.{prop}: no such formula")
    return {"ok": not errors, "errors": errors, "warnings": warnings, "views": names}


def _view_specs(doc: Mapping[str, Any]) -> List[Dict[str, Any]]:
    views = [dict(v) for v in (doc.get("views") or []) if isinstance(v, Mapping)]
    return views or [{"type": "table", "name": "Table", "order": ["file.name"]}]


def run_query(owner: str, doc: Mapping[str, Any], *, base_path: Optional[str] = None, view: Optional[str] = None,
              max_rows: int = 500, store: Optional[_Store] = None) -> Dict[str, Any]:
    check = validate(dict(doc))
    if not check["ok"]:
        raise BaseError("; ".join(check["errors"][:6]))
    store = store or _Store(owner).load()
    formulas = dict(doc.get("formulas") or {})
    custom = dict(doc.get("summaries") or {})
    properties = dict(doc.get("properties") or {})
    errors: List[str] = []

    def guard(fn: Callable[[], Any], where: str, default: Any = None) -> Any:
        try:
            return fn()
        except BaseError as exc:
            if len(errors) < 30:
                errors.append(f"{where}: {exc}")
            return default
        except (TypeError, ValueError, AttributeError, IndexError, KeyError, ZeroDivisionError, OverflowError) as exc:
            if len(errors) < 30:
                errors.append(f"{where}: {type(exc).__name__}: {exc}")
            return default

    scopes: Dict[str, _Scope] = {}
    matched: List[str] = []
    for path in sorted(store.notes):
        sc = _Scope(store, path, formulas, base_path)
        scopes[path] = sc
        if guard(lambda: _match(doc.get("filters"), sc), f"{path} (filters)", False):
            matched.append(path)

    out_views = []
    for spec in _view_specs(doc):
        name = str(spec.get("name") or "Table")
        if view and name != view:
            continue
        vtype = str(spec.get("type") or "table")
        rows_paths = [p for p in matched if guard(lambda p=p: _match(spec.get("filters"), scopes[p]), f"{p} ({name} filters)", False)]
        order = [str(x) for x in (spec.get("order") or ["file.name"])]
        sorts = spec.get("sort") or []
        if isinstance(sorts, Mapping):
            sorts = [sorts]
        for s in reversed([s for s in sorts if isinstance(s, Mapping) and s.get("property")]):
            prop, desc = str(s["property"]), str(s.get("direction") or "ASC").upper() == "DESC"
            rows_paths.sort(key=lambda p, prop=prop: _sort_key(guard(lambda: _prop_value(prop, scopes[p]), p)), reverse=desc)
        total = len(rows_paths)
        limit = spec.get("limit")
        if isinstance(limit, int) and limit >= 0:
            rows_paths = rows_paths[:limit]
        truncated = len(rows_paths) > max_rows
        rows_paths = rows_paths[:max_rows]
        columns = [{"id": p, "label": _label(p, properties)} for p in order]

        def make_row(p: str) -> Dict[str, Any]:
            cells, raw = {}, {}
            for c in order:
                v = guard(lambda c=c: _prop_value(c, scopes[p]), f"{p} {c}")
                cells[c] = display(v)
                raw[c] = jsonable(v)
            return {"path": p, "title": store.notes[p]["title"], "cells": cells, "values": raw}

        rows = [make_row(p) for p in rows_paths]
        group = spec.get("groupBy") if isinstance(spec.get("groupBy"), Mapping) else None
        groups = None
        if group and group.get("property"):
            gprop, gdesc = str(group["property"]), str(group.get("direction") or "ASC").upper() == "DESC"
            buckets: Dict[str, List[Dict[str, Any]]] = {}
            keys: Dict[str, Any] = {}
            for p, row in zip(rows_paths, rows):
                v = guard(lambda p=p: _prop_value(gprop, scopes[p]), f"{p} groupBy")
                k = display(v)
                buckets.setdefault(k, []).append(row)
                keys.setdefault(k, v)
            ordered = sorted(buckets, key=lambda k: _sort_key(keys[k]), reverse=gdesc)
            groups = [{"key": k, "label": k or "(empty)", "rows": buckets[k]} for k in ordered]
        summaries = {}
        sums = spec.get("summaries") if isinstance(spec.get("summaries"), Mapping) else {}
        for prop, sname in (sums or {}).items():
            values = [guard(lambda p=p: _prop_value(str(prop), scopes[p]), f"{p} {prop}") for p in rows_paths]
            dummy = _Scope(store, None, formulas, base_path)
            v = guard(lambda: _summary(str(sname), values, custom, dummy), f"summary {prop}")
            summaries[str(prop)] = {"name": str(sname), "value": display(v), "raw": jsonable(v)}
        out_views.append({"name": name, "type": vtype, "columns": columns, "rows": rows, "groups": groups,
                          "summaries": summaries, "total": total, "shown": len(rows), "truncated": truncated,
                          **({"note": "the map view is shown as a table"} if vtype == "map" else {})})
    if view and not out_views:
        raise BaseError(f"no view called {view!r}; views: {', '.join(check['views'])}")
    return {"path": base_path, "notes_scanned": len(store.notes), "matched": len(matched), "views": out_views,
            "errors": errors, "warnings": check["warnings"]}


# ── files ────────────────────────────────────────────────────────────────

def _base_rel(path: Any) -> str:
    text = str(path or "").strip().replace("\\", "/").lstrip("/")
    if not text:
        raise BaseError("empty base path")
    if not text.lower().endswith(".base"):
        text += ".base"
    if "/" not in text:
        text = f"{BASES_FOLDER}/{text}"
    vault.validate_path(text[:-5] + ".md")  # same rules as a note path (no traversal, no .trash)
    return text


def _base_abs(owner: str, rel: str) -> str:
    return os.path.join(vault.vault_root(owner), *rel.split("/"))


def list_bases(owner: str) -> List[Dict[str, Any]]:
    root = vault.vault_root(owner)
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".trash"]
        for fn in filenames:
            if not fn.lower().endswith(".base"):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            item: Dict[str, Any] = {"path": rel, "name": os.path.splitext(fn)[0]}
            try:
                text = open(full, encoding="utf-8-sig").read()
                check = validate(text)
                item.update({"views": check["views"], "ok": check["ok"], "errors": check["errors"][:3]})
            except (OSError, BaseError) as exc:
                item.update({"views": [], "ok": False, "errors": [str(exc)]})
            try:
                item["updated"] = datetime.fromtimestamp(os.path.getmtime(full)).isoformat(timespec="seconds")
            except OSError:
                pass
            out.append(item)
    return sorted(out, key=lambda r: r["path"].lower())


def read_base(owner: str, path: str) -> Dict[str, Any]:
    rel = _base_rel(path)
    full = _base_abs(owner, rel)
    if not os.path.isfile(full):
        raise FileNotFoundError(rel)
    text = open(full, encoding="utf-8-sig").read()
    return {"path": rel, "name": os.path.splitext(os.path.basename(rel))[0], "yaml": text, "validation": validate(text)}


def query(owner: str, *, path: Optional[str] = None, yaml_text: Optional[str] = None, view: Optional[str] = None,
          max_rows: int = 500) -> Dict[str, Any]:
    if path:
        info = read_base(owner, path)
        text, rel = info["yaml"], info["path"]
    elif yaml_text:
        text, rel = yaml_text, None
    else:
        raise BaseError("give `path` (a .base in the vault) or `yaml`")
    doc = _yaml_load(text)
    if not isinstance(doc, Mapping):
        raise BaseError("a base is a YAML mapping")
    return run_query(owner, doc, base_path=rel, view=view, max_rows=max(1, min(MAX_ROWS, int(max_rows or 500))))


def _q(s: Any) -> str:
    """A YAML scalar: single-quoted when it holds anything special."""
    text = str(s)
    if re.fullmatch(r"[A-Za-z_][\w./ -]*", text) and text.strip() == text and text.lower() not in ("true", "false", "null", "yes", "no"):
        return text
    return "'" + text.replace("'", "''") + "'"


def yaml_from_spec(spec: Mapping[str, Any]) -> str:
    """A small form (tags, folder, property filters, columns, view type, groupBy, sort, limit) as base YAML."""
    clauses: List[str] = []
    for tag in spec.get("tags") or []:
        tag = str(tag).strip().lstrip("#")
        if tag:
            clauses.append(f'file.hasTag("{tag}")')
    folder = str(spec.get("folder") or "").strip().strip("/")
    if folder:
        clauses.append(f'file.inFolder("{folder}")')
    for expr in spec.get("where") or []:
        if str(expr).strip():
            clauses.append(str(expr).strip())
    lines: List[str] = []
    if clauses:
        lines.append("filters:")
        lines.append("  and:")
        lines += [f"    - {_q(c)}" for c in clauses]
    formulas = spec.get("formulas") or {}
    if formulas:
        lines.append("formulas:")
        lines += [f"  {k}: {_q(v)}" for k, v in formulas.items()]
    labels = spec.get("labels") or {}
    if labels:
        lines.append("properties:")
        for k, v in labels.items():
            lines.append(f"  {k}:")
            lines.append(f"    displayName: {_q(v)}")
    columns = [str(c).strip() for c in (spec.get("columns") or ["file.name"]) if str(c).strip()]
    lines.append("views:")
    lines.append(f"  - type: {spec.get('view_type') or 'table'}")
    lines.append(f"    name: {_q(spec.get('view_name') or 'Tabla')}")
    if spec.get("limit"):
        lines.append(f"    limit: {int(spec['limit'])}")
    if spec.get("group_by"):
        lines.append("    groupBy:")
        lines.append(f"      property: {_q(spec['group_by'])}")
        lines.append(f"      direction: {'DESC' if str(spec.get('group_direction') or '').upper() == 'DESC' else 'ASC'}")
    if spec.get("sort_by"):
        lines.append("    sort:")
        lines.append(f"      - property: {_q(spec['sort_by'])}")
        lines.append(f"        direction: {'DESC' if str(spec.get('sort_direction') or '').upper() == 'DESC' else 'ASC'}")
    lines.append("    order:")
    lines += [f"      - {_q(c)}" for c in columns]
    sums = spec.get("summaries") or {}
    if sums:
        lines.append("    summaries:")
        lines += [f"      {_q(k)}: {v}" for k, v in sums.items()]
    return "\n".join(lines) + "\n"


def save_base(owner: str, path: str, *, yaml_text: Optional[str] = None, spec: Optional[Mapping[str, Any]] = None,
              overwrite: bool = True) -> Dict[str, Any]:
    rel = _base_rel(path)
    text = yaml_text if yaml_text is not None else yaml_from_spec(spec or {})
    check = validate(text)
    if not check["ok"]:
        raise BaseError("not saved: " + "; ".join(check["errors"][:6]))
    full = _base_abs(owner, rel)
    existed = os.path.isfile(full)
    if existed and not overwrite:
        raise BaseError(f"{rel} already exists")
    vault._atomic_write(full, text)
    return {"path": rel, "created": not existed, "yaml": text, "validation": check}


def delete_base(owner: str, path: str) -> Dict[str, Any]:
    rel = _base_rel(path)
    full = _base_abs(owner, rel)
    if not os.path.isfile(full):
        raise FileNotFoundError(rel)
    os.remove(full)
    return {"deleted": rel}


def run(owner: str, args: Mapping[str, Any]) -> Dict[str, Any]:
    """Dispatcher for the agent tool, the MCP twin and the routes."""
    action = str(args.get("action") or "query").lower()
    try:
        if action == "list":
            return {"bases": list_bases(owner)}
        if action in ("get", "read"):
            return read_base(owner, str(args.get("path") or args.get("name") or ""))
        if action == "query":
            return query(owner, path=args.get("path") or args.get("name") or None, yaml_text=args.get("yaml") or None,
                         view=args.get("view") or None, max_rows=int(args.get("max_rows") or 500))
        if action == "validate":
            if args.get("yaml"):
                return validate(str(args["yaml"]))
            if args.get("spec"):
                return validate(yaml_from_spec(args["spec"]))
            return read_base(owner, str(args.get("path") or ""))["validation"]
        if action == "save":
            return save_base(owner, str(args.get("path") or args.get("name") or ""), yaml_text=args.get("yaml") or None,
                             spec=args.get("spec") if isinstance(args.get("spec"), Mapping) else None,
                             overwrite=bool(args.get("overwrite", True)))
        if action == "delete":
            return delete_base(owner, str(args.get("path") or args.get("name") or ""))
    except FileNotFoundError as exc:
        return {"error": f"no base at {exc}", "error_class": "brain_base.not_found"}
    except (BaseError, vault.VaultPathError) as exc:
        return {"error": str(exc), "error_class": "brain_base.invalid"}
    return {"error": "action must be list, get, query, validate, save or delete", "error_class": "brain_base.bad_args"}


def render_text(data: Mapping[str, Any], max_rows: int = 40) -> str:
    if data.get("error"):
        return f"error: {data['error']}"
    if "bases" in data:
        return "\n".join(f"{b['path']}  views: {', '.join(b['views']) or '-'}{'' if b.get('ok') else '  (invalid: ' + '; '.join(b['errors']) + ')'}"
                         for b in data["bases"]) or "no .base files in the vault"
    if "views" in data and isinstance(data["views"], list) and data["views"] and isinstance(data["views"][0], dict):
        out = [f"{data.get('path') or 'inline base'}: {data['matched']} of {data['notes_scanned']} notes match the base filters"]
        for v in data["views"]:
            out.append(f"\n## {v['name']} ({v['type']}, {v['shown']} of {v['total']} rows)")
            cols = v["columns"]
            out.append("| " + " | ".join(c["label"] for c in cols) + " |")
            out.append("|" + "---|" * len(cols))
            rows = v["rows"] if not v.get("groups") else [r for g in v["groups"] for r in g["rows"]]
            for r in rows[:max_rows]:
                out.append("| " + " | ".join(r["cells"].get(c["id"], "").replace("|", "\\|") for c in cols) + " |")
            if len(rows) > max_rows:
                out.append(f"… {len(rows) - max_rows} more rows")
            if v.get("groups"):
                out.append("groups: " + ", ".join(f"{g['label']} ({len(g['rows'])})" for g in v["groups"]))
            for prop, s in (v.get("summaries") or {}).items():
                out.append(f"{s['name']} of {prop}: {s['value']}")
        if data.get("errors"):
            out.append("\nevaluation errors: " + "; ".join(data["errors"][:5]))
        return "\n".join(out)
    if "deleted" in data:
        return f"deleted {data['deleted']}"
    if "yaml" in data and "path" in data:
        v = data.get("validation") or {}
        return (f"{data['path']} ({'valid' if v.get('ok') else 'INVALID'})\n" + data["yaml"]
                + "".join(f"\nerror: {e}" for e in v.get("errors", [])))
    return json.dumps(jsonable(dict(data)), ensure_ascii=False)
