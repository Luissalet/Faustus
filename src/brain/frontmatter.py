"""
brain/frontmatter.py — split/join a note's YAML frontmatter and body.

Why a dedicated module: every generated note (memory, entity, project...) and
every free note under Notes/ shares one header shape, and `vault.sync` needs
to tell "the human changed the text" from "the human changed a frontmatter
field" apart cheaply — that only works if parsing is exact and round-trip
stable (split(join(d, b)) == (d, b) for any d/b this module produced).

Keys are written in a fixed order (the contract's) so two renders of the same
data are byte-identical, which is what lets `vault.sync` skip writing a file
whose rendered content did not change. PyYAML is used when importable (it
almost always is, and its C accelerated loader/dumper when compiled in — the
pure-python one made a no-op sync of a big vault take seconds); a small
dependency-free fallback keeps the module usable without it, for the same
flat, single-document, no-anchors shape.

What a human typed must come back as what they typed. The loader therefore
resolves only the unambiguous core scalars (true/false, null, plain decimal
numbers): ISO dates and timestamps stay the exact strings in the file (a
datetime object would be re-serialised in another format and the store would
see a different value), `10:30` is not a base-60 integer and `yes`/`no`/`on`
are plain words. A `---` block that is not a mapping (a horizontal-rule
fenced paragraph at the top of a note) is not frontmatter at all: it stays in
the body, so nothing rewriting the note can drop it.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Any, Dict, List, Tuple

try:
    import yaml as _yaml
except ImportError:  # pragma: no cover - exercised only without PyYAML
    _yaml = None


def _build_yaml_classes():
    """(Loader, Dumper): the C-accelerated safe ones when libyaml is compiled
    in, else the pure-python safe ones — with the loader's implicit type
    resolution narrowed to the unambiguous core scalars (see the module
    docstring for why)."""
    if _yaml is None:  # pragma: no cover
        return None, None
    base_loader = getattr(_yaml, "CSafeLoader", None) or _yaml.SafeLoader
    dumper = getattr(_yaml, "CSafeDumper", None) or _yaml.SafeDumper

    class _Loader(base_loader):  # type: ignore[misc, valid-type]
        pass

    _Loader.yaml_implicit_resolvers = {}
    _Loader.add_implicit_resolver(
        "tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF"))
    _Loader.add_implicit_resolver(
        "tag:yaml.org,2002:int", re.compile(r"^[-+]?(?:0|[1-9][0-9]*)$"), list("-+0123456789"))
    _Loader.add_implicit_resolver(
        "tag:yaml.org,2002:float",
        re.compile(r"^(?:[-+]?(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?"
                   r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$"),
        list("-+0123456789."))
    _Loader.add_implicit_resolver(
        "tag:yaml.org,2002:null", re.compile(r"^(?:~|null|Null|NULL|)$"), ["~", "n", "N", ""])
    return _Loader, dumper


_LOADER, _DUMPER = _build_yaml_classes()

#: Order the contract lists frontmatter keys in. Unknown keys (forward
#: compatibility, or a human adding their own) are appended after these,
#: sorted, so output stays deterministic either way.
FIELD_ORDER: Tuple[str, ...] = (
    "id", "source", "kind", "type", "level", "status", "trust", "confidence",
    "valid_from", "valid_until", "created", "updated", "project",
    "tags", "aliases", "pinned",
)

_FM_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_EMPTY_FM_RE = re.compile(r"\A---\r?\n---[ \t]*(?:\r?\n|\Z)")
BOM = "\ufeff"


def _match(raw: str):
    """(data, header_end) when `raw` opens with a real frontmatter mapping,
    else None. A block whose YAML is not a mapping — or is so broken that
    even the lenient flat parser finds no `key: value` in it — is body text
    (a horizontal-rule fenced paragraph), never silently dropped."""
    empty = _EMPTY_FM_RE.match(raw)
    if empty:
        return {}, empty.end()
    match = _FM_RE.match(raw)
    if not match:
        return None
    data = _parse_yaml(match.group(1))
    if not isinstance(data, dict):
        return None
    return data, match.end()


def split(text: Any) -> Tuple[Dict[str, Any], str]:
    """``(frontmatter_dict, body)``. No frontmatter, or a leading ``---``
    block that is not a mapping -> ``({}, text)`` with the text intact. A
    leading byte-order mark (some editors save one) is ignored."""
    raw = str(text or "")
    if raw.startswith(BOM):
        raw = raw[1:]
    found = _match(raw)
    if found is None:
        return {}, raw
    data, end = found
    return data, raw[end:]


def split_raw(text: Any) -> Tuple[str, str]:
    """``(header, body)`` where `header` is the frontmatter block exactly as
    written (delimiters included, "" when there is none) — for callers that
    rewrite only the body and must keep a human's YAML byte-for-byte."""
    raw = str(text or "")
    if raw.startswith(BOM):
        raw = raw[1:]
    found = _match(raw)
    if found is None:
        return "", raw
    return raw[:found[1]], raw[found[1]:]


_TOP_KEY_RE = re.compile(r"^([^\s#:'\"-][^:]*?)\s*:(?:\s|$)")
_INNER_RE = re.compile(r"\A---\r?\n(.*?)(?:\r?\n)?---[ \t]*(?:\r?\n|\Z)", re.DOTALL)


def patch_header(text: Any, updates: Dict[str, Any]) -> str:
    """The frontmatter block of `text` with just the `updates` keys
    rewritten (replaced in place, or appended) — every other line, comments
    and quoting included, exactly as written. A text with no frontmatter
    gets a fresh block holding only `updates`."""
    header, _ = split_raw(text)
    if not header:
        return join(updates, "")
    match = _INNER_RE.match(header)
    inner = match.group(1) if match else ""
    lines = inner.split("\n") if inner else []
    lines = [ln.rstrip("\r") for ln in lines]
    for key, value in updates.items():
        entry = _dump_yaml([(key, value)]) if value is not None else [f"{key}: null"]
        start = next((i for i, ln in enumerate(lines)
                      if (m := _TOP_KEY_RE.match(ln)) and m.group(1).strip() == key), -1)
        if start < 0:
            lines.extend(entry)
            continue
        end = start + 1
        while end < len(lines) and (re.match(r"^\s+\S", lines[end]) or re.match(r"^-(\s|$)", lines[end])):
            end += 1
        lines[start:end] = entry
    return "---\n" + "\n".join(lines) + "\n---\n"


def join(data: Any, body: Any) -> str:
    """The inverse of :func:`split`: a full file text from fields + body."""
    ordered = _ordered_items(dict(data or {}))
    header = "\n".join(_dump_yaml(ordered)) if ordered else ""
    return f"---\n{header}\n---\n{body or ''}"


# ── ordering ─────────────────────────────────────────────────────────────

def _ordered_items(data: Dict[str, Any]) -> List[Tuple[str, Any]]:
    items: List[Tuple[str, Any]] = []
    seen = set()
    for key in FIELD_ORDER:
        if key in data and data[key] is not None:
            items.append((key, data[key]))
            seen.add(key)
    for key in sorted(k for k in data if k not in seen):
        if data[key] is not None:
            items.append((key, data[key]))
    return items


# ── PyYAML path ──────────────────────────────────────────────────────────

def _plain(value: Any) -> Any:
    """Dates/timestamps (a `!!timestamp` tag, or the fallback path) as ISO
    strings, recursively — the vault never hands a datetime object on."""
    if isinstance(value, _dt.datetime):
        text = value.isoformat()
        return text[:-6] + "Z" if text.endswith("+00:00") else text
    if isinstance(value, _dt.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _parse_yaml(raw: str) -> Any:
    """The parsed block: a dict for a real frontmatter, anything else (or
    None) for a block that is not one. A block PyYAML refuses falls back to
    the lenient flat parser, and counts as frontmatter only when that finds
    at least one key."""
    if _yaml is not None:
        try:
            loaded = _yaml.load(raw, Loader=_LOADER)  # noqa: S506 - a SafeLoader subclass
        except Exception:  # noqa: BLE001 - a hand-edited file may be broken
            loaded = _parse_fallback(raw) or None
        if loaded is None and not raw.strip():
            return {}
        return _plain(loaded) if isinstance(loaded, dict) else loaded
    loaded = _parse_fallback(raw)
    return loaded if loaded or not raw.strip() else None


def _dump_yaml(items: List[Tuple[str, Any]]) -> List[str]:
    if _yaml is not None:
        text = _yaml.dump(
            dict(items), Dumper=_DUMPER, allow_unicode=True, sort_keys=False,
            default_flow_style=False,
        )
        # PyYAML orders a plain dict by insertion under sort_keys=False, and
        # `items` was already built in FIELD_ORDER — a dict() built from it
        # keeps that order (Python 3.7+ dict semantics).
        return text.rstrip("\n").split("\n")
    return _dump_fallback(items)


# ── dependency-free fallback (flat scalars/lists only) ──────────────────

_NEEDS_QUOTES = re.compile(r"^[\s]|[\s]$|[:#\[\]{}\"'|>*&!%@`,]|^$")
_LOOKS_SPECIAL = re.compile(
    r"(?i)^(true|false|null|~|yes|no|on|off|[-+]?[0-9][0-9_:.eE+-]*|\.[0-9].*|\d{4}-\d\d-\d\d.*)$")


def _scalar_dump(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if _NEEDS_QUOTES.search(text) or _LOOKS_SPECIAL.match(text):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'
    return text


def _scalar_parse(text: str) -> Any:
    text = text.strip()
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        inner = text[1:-1].replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
        return inner
    if text.startswith("'") and text.endswith("'") and len(text) >= 2:
        return text[1:-1].replace("''", "'")
    if text in ("null", "~", ""):
        return None
    if text in ("true", "True", "TRUE"):
        return True
    if text in ("false", "False", "FALSE"):
        return False
    # Same narrow numeric forms as the PyYAML path's loader: `10:30` or
    # `1_000` stay text, exactly as typed.
    if re.fullmatch(r"[-+]?(?:0|[1-9][0-9]*)", text):
        return int(text)
    if re.fullmatch(r"[-+]?(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?", text):
        return float(text)
    return text


def _dump_fallback(items: List[Tuple[str, Any]]) -> List[str]:
    lines: List[str] = []
    for key, value in items:
        if isinstance(value, (list, tuple)):
            if not value:
                lines.append(f"{key}: []")
            else:
                lines.append(f"{key}:")
                lines.extend(f"  - {_scalar_dump(v)}" for v in value)
        else:
            lines.append(f"{key}: {_scalar_dump(value)}")
    return lines


def _parse_fallback(raw: str) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    lines = raw.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        m = re.match(r"^([^\s:][^:]*):\s*(.*)$", line)
        if not m:
            i += 1
            continue
        key, rest = m.group(1).strip(), m.group(2)
        if rest.strip() == "":
            values: List[Any] = []
            j = i + 1
            while j < len(lines) and re.match(r"^\s*-\s?", lines[j]):
                item_text = re.sub(r"^\s*-\s?", "", lines[j])
                values.append(_scalar_parse(item_text))
                j += 1
            if values:
                data[key] = values
                i = j
                continue
            data[key] = None
            i += 1
            continue
        if rest.strip() == "[]":
            data[key] = []
        else:
            data[key] = _scalar_parse(rest)
        i += 1
    return data
