"""TOON — Token-Oriented Object Notation (FAUSTUS).

A YAML-ish, line-oriented encoding for the payloads Faustus hands to a
COORDINATING MODEL (Fable/Claude through the MCP server or the robot-mode
endpoints). JSON spends a large share of its characters on punctuation that
repeats once per row — `{"id":1,"name":"ana","active":true}` names every key
again for every record. TOON names them once, in a header, and writes the rows
as CSV-ish lines — 40-46 % fewer characters than compact JSON on the row-shaped
payloads a coordinator reads (measured in tests/test_toon.py)::

    ok: true
    data.objectives[3]{id,status,priority,title}:
      OBJ-1,open,1,Ship the API
      OBJ-2,done,2,Write the docs
      OBJ-3,blocked,1,"Fix cart, then bill"

The grammar, in full:

* **Scalars** are bare when unambiguous (``ok: true``, ``count: 3``,
  ``name: hello world``) and double-quoted when the string is empty, has
  leading/trailing spaces, contains ``:``+space, ``#``, a newline/tab, or
  would otherwise parse back as a number/bool/null/empty-container.
  ``\\`` ``"`` ``\\n`` ``\\r`` ``\\t`` are escaped inside quotes. A scalar
  written alone at the ROOT is also quoted when it holds a colon, since there
  is no surrounding structure to say it is not a ``key: value`` line.
* **Objects** are ``key: value`` per line; a nested object goes on its own
  lines indented two spaces under ``key:``.
* **Key folding**: a nested object with exactly one key collapses to a dotted
  path (``config.database.host: localhost``), applied recursively. A key that
  is not bare-safe (it contains ``.``/``:``/quotes/brackets…) is quoted and
  never folded, so the dots in a path are always structure.
* **Tabular arrays**: an array of ≥ 2 objects that all share one key set, all
  of whose values are scalars, becomes ``key[N]{c1,c2}:`` plus one comma-joined
  row per line (a cell containing ``,``/``"``/newline is quoted the same way).
  The header hangs off a key, so an array at the very ROOT stays ``- `` items;
  every payload robot mode sends is the envelope object, so its arrays are
  keyed. An array whose objects hold a list or an object under some key is not
  tabular either — it is written out as items, which for deeply nested data
  can cost slightly more than compact JSON (two spaces per line per level,
  with no repeated keys to save). TOON pays on rows; that is the trade.
* Any other array is ``- `` items — scalars inline, containers as a bare ``-``
  followed by an indented block.
* Empty containers are ``key: []`` / ``key: {}``; ``None`` is ``null``.
* Dict order is preserved exactly as given — callers control the order,
  nothing is sorted, so the same object always encodes to the same bytes.

``decode(encode(x)) == x`` for everything ``encode`` produces. ``encode`` never
raises: an unsupported object (a set, a datetime, a cycle, something whose
``__str__`` explodes) degrades to its ``str()``, and ``decode`` answers ``None``
rather than raising on input it cannot parse — neither may take down a request.

Pure stdlib.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = ["encode", "decode", "estimate_savings"]

_INDENT = "  "
_MAX_DEPTH = 64

_INT_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?$")
_TABLE_RE = re.compile(r"^(.+)\[(\d+)\]\{(.*)\}$")

# A bare key is one that can carry the dotted-path folding without ambiguity.
_KEY_FORBIDDEN = frozenset(':.[]{},"#\n\r\t')
_RESERVED_SCALARS = ("null", "true", "false", "[]", "{}")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}


# ── encoding ────────────────────────────────────────────────────────────────

def _safe_str(value: Any) -> str:
    """``str(value)`` that cannot raise — the fallback for unknown objects."""
    try:
        return str(value)
    except Exception:  # noqa: BLE001 - a broken __str__ must not break a response
        try:
            return repr(value)
        except Exception:  # noqa: BLE001
            return "<unprintable>"


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _bare_key_ok(key: Any) -> bool:
    if not isinstance(key, str) or not key or key != key.strip():
        return False
    return not any(ch in _KEY_FORBIDDEN for ch in key)


def _bare_value_ok(text: str) -> bool:
    if text == "" or text != text.strip():
        return False
    if text[0] == '"' or text.endswith(":"):
        return False
    if text.startswith("- ") or text in _RESERVED_SCALARS:
        return False
    if any(bad in text for bad in (": ", "#", "\n", "\r", "\t")):
        return False
    return not (_INT_RE.match(text) or _FLOAT_RE.match(text))


def _bare_cell_ok(text: str) -> bool:
    return "," not in text and _bare_value_ok(text)


def _quote(text: str) -> str:
    out = (text.replace("\\", "\\\\").replace('"', '\\"')
               .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
    return '"' + out + '"'


def _key_text(key: str) -> str:
    return key if _bare_key_ok(key) else _quote(key)


def _scalar(value: Any, *, cell: bool = False) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # inf/nan have no bare spelling that reads back as a float: quote them
        # so the line stays parseable (they arrive as strings, never a crash).
        return repr(value) if math.isfinite(value) else _quote(repr(value))
    text = value if isinstance(value, str) else _safe_str(value)
    ok = _bare_cell_ok(text) if cell else _bare_value_ok(text)
    return text if ok else _quote(text)


def _table_columns(seq: Sequence[Any]) -> Optional[List[str]]:
    """The shared column order when `seq` is a table, else None."""
    if len(seq) < 2 or not isinstance(seq[0], dict) or not seq[0]:
        return None
    cols: List[str] = []
    for key in seq[0].keys():
        if not _bare_key_ok(key):
            return None
        cols.append(key)
    keyset = set(cols)
    if len(keyset) != len(cols):
        return None
    for row in seq:
        if not isinstance(row, dict) or set(row.keys()) != keyset:
            return None
        for value in row.values():
            if not _is_scalar(value):
                return None
    return cols


def _emit_mapping(obj: Dict[Any, Any], lines: List[str], level: int, depth: int,
                  seen: set) -> None:
    for raw_key, value in obj.items():
        key = raw_key if isinstance(raw_key, str) else _safe_str(raw_key)
        path = [key]
        # Key folding: dive while the value is an object with exactly one key.
        while (isinstance(value, dict) and len(value) == 1
               and _bare_key_ok(path[-1]) and depth + len(path) < _MAX_DEPTH):
            child_key, child_value = next(iter(value.items()))
            child_key = child_key if isinstance(child_key, str) else _safe_str(child_key)
            if not _bare_key_ok(child_key):
                break
            path.append(child_key)
            value = child_value
        head = ".".join(path) if len(path) > 1 else _key_text(key)
        _emit_pair(head, value, lines, level, depth + len(path) - 1, seen)


def _emit_pair(head: str, value: Any, lines: List[str], level: int, depth: int,
               seen: set) -> None:
    pad = _INDENT * level
    if isinstance(value, dict):
        if depth >= _MAX_DEPTH or id(value) in seen:
            lines.append(f"{pad}{head}: {_scalar(_safe_str(value))}")
        elif not value:
            lines.append(f"{pad}{head}: {{}}")
        else:
            lines.append(f"{pad}{head}:")
            seen.add(id(value))
            _emit_mapping(value, lines, level + 1, depth + 1, seen)
            seen.discard(id(value))
        return
    if isinstance(value, (list, tuple)):
        seq = list(value)
        if depth >= _MAX_DEPTH or id(value) in seen:
            lines.append(f"{pad}{head}: {_scalar(_safe_str(value))}")
            return
        if not seq:
            lines.append(f"{pad}{head}: []")
            return
        cols = _table_columns(seq)
        if cols is not None:
            lines.append(f"{pad}{head}[{len(seq)}]{{{','.join(cols)}}}:")
            row_pad = _INDENT * (level + 1)
            for row in seq:
                lines.append(row_pad + ",".join(_scalar(row.get(c), cell=True) for c in cols))
            return
        lines.append(f"{pad}{head}:")
        seen.add(id(value))
        _emit_sequence(seq, lines, level + 1, depth + 1, seen)
        seen.discard(id(value))
        return
    if _is_scalar(value):
        lines.append(f"{pad}{head}: {_scalar(value)}")
        return
    lines.append(f"{pad}{head}: {_scalar(_safe_str(value))}")


def _emit_sequence(seq: Sequence[Any], lines: List[str], level: int, depth: int,
                   seen: set) -> None:
    pad = _INDENT * level
    for item in seq:
        if isinstance(item, (dict, list, tuple)):
            if depth >= _MAX_DEPTH or id(item) in seen:
                lines.append(f"{pad}- {_scalar(_safe_str(item))}")
                continue
            if not item:
                lines.append(f"{pad}- " + ("{}" if isinstance(item, dict) else "[]"))
                continue
            lines.append(f"{pad}-")
            seen.add(id(item))
            if isinstance(item, dict):
                _emit_mapping(item, lines, level + 1, depth + 1, seen)
            else:
                _emit_sequence(list(item), lines, level + 1, depth + 1, seen)
            seen.discard(id(item))
            continue
        if _is_scalar(item):
            lines.append(f"{pad}- {_scalar(item)}")
        else:
            lines.append(f"{pad}- {_scalar(_safe_str(item))}")


def _root_scalar(value: Any) -> str:
    """A scalar written on a line of its own. A bare string holding a colon
    (`C:\\LocalAI`) would read back as a `key: value` pair with nothing around
    it to say otherwise, so at the root it is quoted."""
    text = _scalar(value)
    if text[:1] != '"' and ":" in text:
        return _quote(value if isinstance(value, str) else _safe_str(value))
    return text


def encode(value: Any, *, indent: int = 0) -> str:
    """Encode `value` as TOON. `indent` is a number of spaces put in front of
    every line (for embedding a block in a larger document). Never raises."""
    lines: List[str] = []
    try:
        if isinstance(value, dict):
            if not value:
                lines.append("{}")
            else:
                _emit_mapping(value, lines, 0, 0, set())
        elif isinstance(value, (list, tuple)):
            seq = list(value)
            if not seq:
                lines.append("[]")
            else:
                _emit_sequence(seq, lines, 0, 0, set())
        elif _is_scalar(value):
            lines.append(_root_scalar(value))
        else:
            lines.append(_root_scalar(_safe_str(value)))
    except Exception:  # noqa: BLE001 - a serializer may never break a response
        lines = [_root_scalar(_safe_str(value))]
    pad = " " * max(0, int(indent or 0))
    return "\n".join((pad + line) if line else line for line in lines)


# ── decoding ────────────────────────────────────────────────────────────────

def _read_quoted(text: str, start: int) -> Tuple[Optional[str], int]:
    """(value, index after the closing quote), or (None, start) if unterminated."""
    if start >= len(text) or text[start] != '"':
        return None, start
    buf: List[str] = []
    i = start + 1
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 1
            if i >= len(text):
                break
            buf.append(_ESCAPES.get(text[i], text[i]))
            i += 1
            continue
        if ch == '"':
            return "".join(buf), i + 1
        buf.append(ch)
        i += 1
    return None, start


def _parse_scalar(text: str) -> Any:
    t = text.strip()
    if t == "":
        return ""
    if t[0] == '"':
        value, pos = _read_quoted(t, 0)
        if value is not None and pos == len(t):
            return value
        return t
    if t == "null":
        return None
    if t == "true":
        return True
    if t == "false":
        return False
    if t == "[]":
        return []
    if t == "{}":
        return {}
    if _INT_RE.match(t):
        try:
            return int(t)
        except ValueError:  # pragma: no cover - the regex already proved it
            return t
    if _FLOAT_RE.match(t):
        try:
            return float(t)
        except ValueError:  # pragma: no cover
            return t
    return t


def _split_cells(line: str) -> List[str]:
    """Split a table row on commas, honouring quoted cells. Cells come back raw
    (still quoted when they were quoted) for `_parse_scalar`."""
    cells: List[str] = []
    i, n = 0, len(line)
    while True:
        if i < n and line[i] == '"':
            value, pos = _read_quoted(line, i)
            if value is not None:
                cells.append(line[i:pos])
                i = pos
                while i < n and line[i] != ",":
                    i += 1
                if i < n:
                    i += 1
                    continue
                break
        j = line.find(",", i)
        if j < 0:
            cells.append(line[i:])
            break
        cells.append(line[i:j])
        i = j + 1
    return cells


def _split_head(line: str) -> Tuple[Optional[List[str]], str, Optional[List[str]]]:
    """(key path, inline value text, table columns) for one ``key: …`` line."""
    if line.startswith('"'):
        key, pos = _read_quoted(line, 0)
        if key is None or pos >= len(line) or line[pos] != ":":
            return None, "", None
        rest = line[pos + 1:]
        return [key], rest[1:] if rest.startswith(" ") else rest, None
    idx = line.find(":")
    if idx < 0:
        return None, "", None
    head, rest = line[:idx], line[idx + 1:]
    if rest.startswith(" "):
        rest = rest[1:]
    match = _TABLE_RE.match(head)
    if match:
        cols = [c for c in match.group(3).split(",") if c] if match.group(3) else []
        return match.group(1).split("."), rest, cols
    return head.split("."), rest, None


def _assign(out: Dict[str, Any], path: List[str], value: Any) -> None:
    node = out
    for part in path[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[path[-1]] = value


def _parse_block(lines: List[Tuple[int, str]], i: int, indent: int) -> Tuple[Any, int]:
    if i >= len(lines):
        return None, i
    first = lines[i][1]
    if first == "-" or first.startswith("- "):
        return _parse_sequence(lines, i, indent)
    return _parse_mapping(lines, i, indent)


def _parse_mapping(lines: List[Tuple[int, str]], i: int, indent: int) -> Tuple[Dict[str, Any], int]:
    out: Dict[str, Any] = {}
    while i < len(lines):
        ind, line = lines[i]
        if ind != indent or line == "-" or line.startswith("- "):
            break
        path, rest, cols = _split_head(line)
        i += 1
        if path is None:
            continue
        if cols is not None:
            rows: List[Dict[str, Any]] = []
            while i < len(lines) and lines[i][0] > indent:
                cells = _split_cells(lines[i][1])
                rows.append({col: (_parse_scalar(cells[n]) if n < len(cells) else None)
                             for n, col in enumerate(cols)})
                i += 1
            value: Any = rows
        elif rest == "":
            if i < len(lines) and lines[i][0] > indent:
                value, i = _parse_block(lines, i, lines[i][0])
            else:
                value = None
        else:
            value = _parse_scalar(rest)
        _assign(out, path, value)
    return out, i


def _parse_sequence(lines: List[Tuple[int, str]], i: int, indent: int) -> Tuple[List[Any], int]:
    out: List[Any] = []
    while i < len(lines):
        ind, line = lines[i]
        if ind != indent:
            break
        if line == "-":
            i += 1
            if i < len(lines) and lines[i][0] > indent:
                value, i = _parse_block(lines, i, lines[i][0])
            else:
                value = None
            out.append(value)
            continue
        if line.startswith("- "):
            out.append(_parse_scalar(line[2:]))
            i += 1
            continue
        break
    return out, i


def decode(text: str) -> Any:
    """Parse TOON back into Python. Round-trips everything `encode` writes;
    answers None for empty or unparseable input instead of raising."""
    if not isinstance(text, str):
        return None
    rows: List[Tuple[int, str]] = []
    for raw in text.split("\n"):
        line = raw[:-1] if raw.endswith("\r") else raw
        if not line.strip():
            continue
        body = line.lstrip(" ")
        rows.append((len(line) - len(body), body.rstrip()))
    if not rows:
        return None
    try:
        # A lone line that is not a `key: …` pair and not a `- ` item is the
        # root scalar (or empty container) `encode` writes for a bare value.
        if len(rows) == 1:
            only = rows[0][1]
            if not (only == "-" or only.startswith("- ")) and _split_head(only)[0] is None:
                return _parse_scalar(only)
        value, _ = _parse_block(rows, 0, rows[0][0])
        return value
    except Exception:  # noqa: BLE001 - a parser may never break a caller
        return None


# ── measuring ───────────────────────────────────────────────────────────────

def estimate_savings(obj: Any) -> Dict[str, Any]:
    """How much smaller the TOON form is than the JSON one. The JSON side uses
    the COMPACT separators (no spaces), so the ratio is the conservative one —
    against `json.dumps` defaults the reduction is larger still."""
    try:
        json_text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=_safe_str)
    except Exception:  # noqa: BLE001
        json_text = _safe_str(obj)
    toon_text = encode(obj)
    json_chars = len(json_text)
    ratio = round(len(toon_text) / json_chars, 4) if json_chars else 0.0
    return {"json_chars": json_chars, "toon_chars": len(toon_text), "ratio": ratio}
