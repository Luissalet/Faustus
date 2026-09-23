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
almost always is); a small dependency-free fallback keeps the module usable
without it, for the same flat, single-document, no-anchors shape.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

try:
    import yaml as _yaml
except ImportError:  # pragma: no cover - exercised only without PyYAML
    _yaml = None

#: Order the contract lists frontmatter keys in. Unknown keys (forward
#: compatibility, or a human adding their own) are appended after these,
#: sorted, so output stays deterministic either way.
FIELD_ORDER: Tuple[str, ...] = (
    "id", "source", "kind", "type", "level", "status", "trust", "confidence",
    "valid_from", "valid_until", "created", "updated", "project",
    "tags", "aliases", "pinned",
)

_FM_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)


def split(text: Any) -> Tuple[Dict[str, Any], str]:
    """``(frontmatter_dict, body)``. No/malformed frontmatter -> ``({}, text)``."""
    raw = str(text or "")
    match = _FM_RE.match(raw)
    if not match:
        return {}, raw
    body = raw[match.end():]
    data = _parse_yaml(match.group(1))
    return (data if isinstance(data, dict) else {}), body


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

def _parse_yaml(raw: str) -> Dict[str, Any]:
    if _yaml is not None:
        try:
            loaded = _yaml.safe_load(raw)
        except Exception:  # noqa: BLE001 - a hand-edited file may be broken
            return _parse_fallback(raw)
        return loaded if isinstance(loaded, dict) else {}
    return _parse_fallback(raw)


def _dump_yaml(items: List[Tuple[str, Any]]) -> List[str]:
    if _yaml is not None:
        text = _yaml.safe_dump(
            dict(items), allow_unicode=True, sort_keys=False, default_flow_style=False,
        )
        # PyYAML orders a plain dict by insertion under sort_keys=False, and
        # `items` was already built in FIELD_ORDER — a dict() built from it
        # keeps that order (Python 3.7+ dict semantics).
        return text.rstrip("\n").split("\n")
    return _dump_fallback(items)


# ── dependency-free fallback (flat scalars/lists only) ──────────────────

_NEEDS_QUOTES = re.compile(r"^[\s]|[\s]$|[:#\[\]{}\"'|>*&!%@`,]|^$")
_LOOKS_SPECIAL = re.compile(r"(?i)^(true|false|null|~|yes|no|on|off|-?\d+(\.\d+)?)$")


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
    if text in ("true", "yes", "on"):
        return True
    if text in ("false", "no", "off"):
        return False
    try:
        if re.fullmatch(r"-?\d+", text):
            return int(text)
        return float(text)
    except ValueError:
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
