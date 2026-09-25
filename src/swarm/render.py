"""src/swarm/render.py — the pure parts of a swarm run: items in, prompts
out, replies parsed back into rows, rows written out as a table.

No I/O except :func:`write_exports`, which writes the three table files into a
directory it is given.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
from typing import Any, Dict, List, Optional, Sequence

#: A rendered item longer than this is not a "small object": the swarm is for
#: many short items, and a long document belongs in a file the item names.
MAX_ITEM_CHARS = 8000
MAX_FIELDS = 24
_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_ -]{0,63}$")
_PLACEHOLDER_RE = re.compile(r"\{item(?:\.([A-Za-z0-9_-]+))?\}")


class SwarmSpecError(ValueError):
    pass


def render_item(item: Any) -> str:
    if isinstance(item, str):
        return item
    return json.dumps(item, ensure_ascii=False, sort_keys=False, default=str)


def normalize_items(items: Any, limit: int) -> List[Any]:
    if isinstance(items, str):
        # One item per non-empty line: the natural way to paste a list.
        items = [line.strip() for line in items.splitlines() if line.strip()]
    if not isinstance(items, list) or not items:
        raise SwarmSpecError("`items` must be a non-empty list (strings or small JSON objects)")
    if len(items) > limit:
        raise SwarmSpecError(
            f"{len(items)} items is more than this install allows in one run ({limit}, setting "
            f"swarm_max_items); split the list into several runs")
    out: List[Any] = []
    for index, item in enumerate(items):
        if item is None or (isinstance(item, str) and not item.strip()):
            raise SwarmSpecError(f"item {index + 1} is empty")
        text = render_item(item)
        if len(text) > MAX_ITEM_CHARS:
            raise SwarmSpecError(
                f"item {index + 1} is {len(text)} characters once rendered (max {MAX_ITEM_CHARS}); "
                "pass a path or URL the worker can read instead of the whole content")
        out.append(item)
    return out


def normalize_fields(output_fields: Any) -> Optional[List[Dict[str, str]]]:
    """`output_fields` as a list of ``{"name", "description"}``. Accepts a
    list of names, a list of objects, or a ``{name: description}`` map."""
    if not output_fields:
        return None
    raw: List[Dict[str, str]] = []
    if isinstance(output_fields, dict):
        raw = [{"name": str(k), "description": str(v or "")} for k, v in output_fields.items()]
    elif isinstance(output_fields, str):
        raw = [{"name": part.strip(), "description": ""} for part in output_fields.split(",") if part.strip()]
    elif isinstance(output_fields, list):
        for entry in output_fields:
            if isinstance(entry, str):
                raw.append({"name": entry.strip(), "description": ""})
            elif isinstance(entry, dict) and entry.get("name"):
                raw.append({"name": str(entry["name"]).strip(),
                            "description": str(entry.get("description") or "")})
    else:
        raise SwarmSpecError("`output_fields` must be a list of field names")
    seen = set()
    out = []
    for field in raw:
        name = field["name"]
        if not _FIELD_RE.match(name):
            raise SwarmSpecError(f"output field {name!r} is not a plain name")
        if name in seen or name in ("index", "item", "status", "error", "attempts"):
            raise SwarmSpecError(f"output field {name!r} is repeated or reserved")
        seen.add(name)
        out.append(field)
    if len(out) > MAX_FIELDS:
        raise SwarmSpecError(f"at most {MAX_FIELDS} output fields")
    return out or None


def response_schema(fields: Optional[Sequence[Dict[str, str]]]) -> Optional[Dict[str, Any]]:
    if not fields:
        return None
    props = {}
    for field in fields:
        prop: Dict[str, Any] = {"type": "string"}
        if field.get("description"):
            prop["description"] = field["description"]
        props[field["name"]] = prop
    return {"type": "object", "properties": props,
            "required": [f["name"] for f in fields], "additionalProperties": False}


def render_prompt(instruction: str, item: Any, fields: Optional[Sequence[Dict[str, str]]] = None) -> str:
    """`{item}` becomes the item (JSON for an object), `{item.key}` one of
    its keys. An instruction with no placeholder gets the item appended."""
    text = render_item(item)

    def _sub(match: "re.Match[str]") -> str:
        key = match.group(1)
        if key is None:
            return text
        if isinstance(item, dict) and key in item:
            return render_item(item[key])
        return match.group(0)

    prompt, count = _PLACEHOLDER_RE.subn(_sub, instruction)
    if count == 0:
        prompt = f"{instruction.rstrip()}\n\nItem:\n{text}"
    if fields:
        lines = [f'- "{f["name"]}"' + (f": {f['description']}" if f.get("description") else "") for f in fields]
        prompt += ("\n\nAnswer ONLY with one JSON object with exactly these keys (string values; "
                   "write \"unknown\" when you cannot tell):\n" + "\n".join(lines))
    return prompt


def _strip_fences(text: str) -> str:
    t = text.strip()
    fence = re.match(r"^```[a-zA-Z0-9_-]*\s*\n(.*?)\n?```\s*$", t, re.S)
    return fence.group(1) if fence else t


def parse_fields(text: str, fields: Sequence[Dict[str, str]]) -> Optional[Dict[str, Any]]:
    """The JSON object a reply carries, restricted to the asked fields, or
    None when there is none. Tolerates code fences and prose around it; the
    LAST well-formed object wins (a worker's final answer comes last)."""
    body = _strip_fences(text or "")
    candidates: List[Any] = []
    try:
        candidates.append(json.loads(body))
    except ValueError:
        decoder = json.JSONDecoder()
        pos = 0
        while True:
            start = body.find("{", pos)
            if start < 0:
                break
            try:
                obj, end = decoder.raw_decode(body, start)
            except ValueError:
                pos = start + 1
                continue
            candidates.append(obj)
            pos = end
    names = [f["name"] for f in fields]
    for obj in reversed(candidates):
        if isinstance(obj, dict) and any(n in obj for n in names):
            return {n: obj.get(n) for n in names}
    return None


def _cell(value: Any, limit: int) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def columns(fields: Optional[Sequence[Dict[str, str]]]) -> List[str]:
    return ["index", "item", "status"] + ([f["name"] for f in fields] if fields else ["output"]) + ["error", "attempts"]


def table_rows(rows: Sequence[Dict[str, Any]], items: Sequence[Any],
               fields: Optional[Sequence[Dict[str, str]]]) -> List[Dict[str, Any]]:
    """One flat record per item, in item order, pending ones included."""
    by_index = {r["index"]: r for r in rows}
    out = []
    for index, item in enumerate(items):
        row = by_index.get(index) or {"index": index, "status": "pending"}
        flat: Dict[str, Any] = {"index": index + 1, "item": render_item(item), "status": row.get("status")}
        if fields:
            values = row.get("fields") or {}
            for f in fields:
                flat[f["name"]] = values.get(f["name"])
        else:
            flat["output"] = row.get("output")
        flat["error"] = row.get("error")
        flat["attempts"] = row.get("attempts")
        out.append(flat)
    return out


def to_markdown(records: Sequence[Dict[str, Any]], cols: Sequence[str], *, cell_limit: int = 300) -> str:
    def esc(v: Any) -> str:
        return _cell(v, cell_limit).replace("|", "\\|")
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for rec in records:
        lines.append("| " + " | ".join(esc(rec.get(c)) for c in cols) + " |")
    return "\n".join(lines) + "\n"


def to_csv(records: Sequence[Dict[str, Any]], cols: Sequence[str]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(cols)
    for rec in records:
        writer.writerow(["" if rec.get(c) is None else (rec.get(c) if isinstance(rec.get(c), (str, int, float))
                                                       else json.dumps(rec.get(c), ensure_ascii=False))
                         for c in cols])
    return buf.getvalue()


def to_jsonl(records: Sequence[Dict[str, Any]]) -> str:
    return "".join(json.dumps(rec, ensure_ascii=False, default=str) + "\n" for rec in records)


def write_exports(directory: str, base: str, records: Sequence[Dict[str, Any]], cols: Sequence[str],
                  *, title: str = "", reduce_output: str = "") -> Dict[str, str]:
    """Write ``<base>.md``, ``<base>.csv``, ``<base>.jsonl`` (and
    ``<base>-reduce.md`` when there is a reduce output). Returns name -> path."""
    os.makedirs(directory, exist_ok=True)
    files = {
        f"{base}.md": (f"# {title}\n\n" if title else "") + to_markdown(records, cols),
        f"{base}.csv": to_csv(records, cols),
        f"{base}.jsonl": to_jsonl(records),
    }
    if reduce_output:
        files[f"{base}-reduce.md"] = reduce_output.rstrip() + "\n"
    out = {}
    for name, body in files.items():
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(body)
        out[name] = path
    return out
