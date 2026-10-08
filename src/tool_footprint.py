"""tool_footprint.py - what the tool catalogue costs in the prompt, and where
it overlaps.

`context_engine.budgets.tool_schema_tokens` already measures the size of the
tool list a turn sends, as one number. With several hundred built-in tools and
a growing set of MCP servers, one number does not say *which* tools or servers
make the list heavy, nor whether two servers publish the same tool twice. This
module answers that, without a model:

* the cost of each tool, serialised the way the provider sees it
  (`{"type": "function", "function": {name, description, parameters}}`), split
  into description and schema;
* totals per source (built-in, or each MCP server) and per exposure (tools the
  authority marks `deferred` or `code_only` are not offered up front);
* the heaviest tools and the descriptions long enough to be worth trimming;
* name collisions: the same short tool name published by more than one source
  (`mcp__a__search` and `mcp__b__search`);
* near duplicates: different tools whose descriptions share most of their
  words (token-set Jaccard), which usually means two servers doing one job.

Native twins (MCP copies of native tools from the built-in servers listed in
`builtin_mcp.NATIVE_TWIN_SERVERS`) are never offered to the agent; they are
counted apart (`hidden_tokens`) and kept out of the collision lists, so what
remains there is a real duplicate the model can see twice.

It reads descriptors (or plain mappings) and never executes or edits a tool.
`offered_tokens` is an upper bound: what the catalogue would cost if every
non-deferred tool were offered at once. Per-turn tool selection sends fewer.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

#: A description over this many tokens is listed as worth trimming.
OVERSIZED_DESCRIPTION_TOKENS = 250
#: Jaccard similarity of description word sets at or above which two tools
#: are reported as near duplicates.
DEFAULT_SIMILARITY = 0.6
DEFAULT_TOP = 15
#: Descriptions with fewer distinct content words than this are too short to
#: compare meaningfully ("Read a file." vs "Read a file.") and are skipped.
_MIN_WORDS_TO_COMPARE = 4
_MAX_PAIRS_REPORTED = 50
_NOT_UPFRONT = frozenset({"deferred", "code_only"})

_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset("""
a an and are as at be by can for from has have if in into is it its may not of
on or that the their then this to use used uses using was when which will with
without you your does do one any all each also only more than other such via
returns return given get set
""".split())

Counter = Callable[[str], int]


def _get(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)


def source_of(executor: str) -> str:
    """`mcp:<server_id>` -> the server id; anything else is `builtin`."""
    executor = str(executor or "")
    if executor.startswith("mcp:"):
        return executor.split(":", 1)[1] or "mcp"
    return "builtin"


def short_name(name: str) -> str:
    """The tool's own name without the `mcp__<server>__` qualifier."""
    name = str(name or "")
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3 and parts[2]:
            return parts[2]
    return name


def wire_schema(name: str, description: str, schema: Any) -> Dict[str, Any]:
    parameters = schema if isinstance(schema, Mapping) and schema else {"type": "object", "properties": {}}
    return {"type": "function", "function": {"name": name, "description": description or "", "parameters": parameters}}


def _dumps(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except Exception:
        return str(value)


def default_counter(model: str = "") -> Counter:
    """The same estimator the context engine budgets with; a chars/4 fallback
    keeps the report usable when that module cannot be imported."""
    try:
        from src.context_engine.budgets import estimator_for
        estimator = estimator_for(model)
        return estimator.count
    except Exception:
        return lambda text: (len(text) + 3) // 4 if text else 0


def description_words(text: str) -> frozenset:
    return frozenset(w for w in _WORD.findall(str(text or "").lower()) if len(w) >= 3 and w not in _STOP)


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def builtin_exposure(name: str) -> Optional[str]:
    """`direct`, `deferred` or `code_only` from the tool authority, or None
    when the authority does not know the tool (MCP tools, fence-only tools)."""
    try:
        from src.tool_authority import AUTHORITY
        if AUTHORITY.get(name) is None:
            return None
        return AUTHORITY.exposure(name).value
    except Exception:
        return None


def native_twin(name: str) -> bool:
    """True for `mcp__<server>__<tool>` from a built-in server that only
    mirrors native tools (`src.builtin_mcp.NATIVE_TWIN_SERVERS`). The tool
    index never offers those to the agent, so they cost nothing per turn and
    their name collisions with the native tool are expected."""
    name = str(name or "")
    if not name.startswith("mcp__"):
        return False
    try:
        from src.builtin_mcp import NATIVE_TWIN_SERVERS
    except Exception:
        return False
    return name[5:].split("__", 1)[0] in NATIVE_TWIN_SERVERS


def rows_from_mappings(tools: Iterable[Any]) -> List[Dict[str, Any]]:
    """Normalise plain tool mappings (an MCP `tools/list`, an OpenAI tool list
    or catalogue rows) to the fields this module reads."""
    rows: List[Dict[str, Any]] = []
    for raw in tools or []:
        if not isinstance(raw, Mapping):
            continue
        item = raw.get("function") if isinstance(raw.get("function"), Mapping) else raw
        name = str(item.get("name") or item.get("qualified_name") or "").strip()
        if not name:
            continue
        schema = item.get("input_schema") or item.get("inputSchema") or item.get("parameters") or {}
        source = str(raw.get("source") or raw.get("server_id") or raw.get("server") or "").strip()
        executor = str(raw.get("executor") or (f"mcp:{source}" if source and source != "builtin" else "native"))
        if executor.startswith("mcp:") and not name.startswith("mcp__"):
            # Qualify the way the app does, so two servers publishing the same
            # short name stay two tools (and show up as a name collision).
            name = f"mcp__{source_of(executor)}__{name}"
        rows.append({
            "name": name,
            "description": str(item.get("description") or ""),
            "input_schema": schema if isinstance(schema, Mapping) else {},
            "executor": executor,
            "exposure": raw.get("exposure"),
        })
    return rows


def tool_costs(rows: Sequence[Any], *, count: Counter,
               exposure_of: Optional[Callable[[str], Optional[str]]] = None,
               hidden_of: Optional[Callable[[str], bool]] = None) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in rows:
        name = str(_get(row, "name", "") or "")
        if not name:
            continue
        description = str(_get(row, "description", "") or "")
        schema = _get(row, "input_schema", {}) or {}
        source = source_of(_get(row, "executor", ""))
        exposure = _get(row, "exposure", None)
        if exposure is None and exposure_of is not None and source == "builtin":
            exposure = exposure_of(name)
        out.append({
            "name": name,
            "short_name": short_name(name),
            "source": source,
            "exposure": exposure,
            "hidden": bool(hidden_of(name)) if hidden_of is not None else False,
            "tokens": count(_dumps(wire_schema(name, description, schema))),
            "description_tokens": count(description) if description else 0,
            "schema_tokens": count(_dumps(schema)) if schema else 0,
            "_words": description_words(description),
        })
    return out


def _public(cost: Mapping[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in cost.items() if not k.startswith("_")}


def name_collisions(costs: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[str, List[Mapping[str, Any]]] = {}
    for cost in costs:
        groups.setdefault(cost["short_name"].lower(), []).append(cost)
    found = []
    for key, members in groups.items():
        names = sorted({m["name"] for m in members})
        if len(names) > 1:
            found.append({
                "short_name": key,
                "tools": names,
                "sources": sorted({m["source"] for m in members}),
                "tokens": sum(m["tokens"] for m in members),
            })
    found.sort(key=lambda g: (-len(g["tools"]), -g["tokens"], g["short_name"]))
    return found


def near_duplicates(costs: Sequence[Mapping[str, Any]], *, similarity: float = DEFAULT_SIMILARITY,
                    limit: int = _MAX_PAIRS_REPORTED) -> List[Dict[str, Any]]:
    candidates = [c for c in costs if len(c["_words"]) >= _MIN_WORDS_TO_COMPARE]
    pairs = []
    for i, a in enumerate(candidates):
        for b in candidates[i + 1:]:
            if a["name"] == b["name"]:
                continue
            score = jaccard(a["_words"], b["_words"])
            if score >= similarity:
                first, second = sorted((a["name"], b["name"]))
                pairs.append({
                    "a": first,
                    "b": second,
                    "similarity": round(score, 2),
                    "same_source": a["source"] == b["source"],
                    "tokens": a["tokens"] + b["tokens"],
                })
    pairs.sort(key=lambda p: (-p["similarity"], -p["tokens"], p["a"], p["b"]))
    return pairs[:max(0, int(limit))]


def footprint_report(rows: Sequence[Any], *, model: str = "", top: int = DEFAULT_TOP,
                     similarity: float = DEFAULT_SIMILARITY,
                     exposure_of: Optional[Callable[[str], Optional[str]]] = None,
                     hidden_of: Optional[Callable[[str], bool]] = None,
                     count: Optional[Counter] = None) -> Dict[str, Any]:
    """The whole report for one catalogue. Deterministic for a given input.

    `hidden_of` marks tools the agent is never offered (native twins): they
    count in `hidden_tokens`, not in `offered_tokens`, and are left out of the
    collision and near-duplicate lists, where they would only repeat the
    native tool they mirror."""
    count = count or default_counter(model)
    top = max(1, min(int(DEFAULT_TOP if top is None else top), 200))
    similarity = min(max(float(similarity), 0.1), 1.0)
    costs = tool_costs(rows, count=count, exposure_of=exposure_of, hidden_of=hidden_of)
    visible = [c for c in costs if not c["hidden"]]
    total = sum(c["tokens"] for c in costs)
    hidden_tokens = total - sum(c["tokens"] for c in visible)
    offered = sum(c["tokens"] for c in visible if c["exposure"] not in _NOT_UPFRONT)

    by_source: Dict[str, Dict[str, Any]] = {}
    for cost in costs:
        bucket = by_source.setdefault(cost["source"], {"source": cost["source"], "tools": 0, "tokens": 0})
        bucket["tools"] += 1
        bucket["tokens"] += cost["tokens"]
    sources = sorted(by_source.values(), key=lambda b: (-b["tokens"], b["source"]))
    for bucket in sources:
        bucket["share"] = round(bucket["tokens"] / total, 3) if total else 0.0
        bucket["avg_tokens"] = round(bucket["tokens"] / bucket["tools"]) if bucket["tools"] else 0

    by_exposure: Dict[str, Dict[str, int]] = {}
    for cost in costs:
        key = "hidden_twin" if cost["hidden"] else (cost["exposure"] or "unknown")
        slot = by_exposure.setdefault(key, {"tools": 0, "tokens": 0})
        slot["tools"] += 1
        slot["tokens"] += cost["tokens"]

    ranked = sorted(visible, key=lambda c: (-c["tokens"], c["name"]))
    oversized = sorted(
        (c for c in visible if c["description_tokens"] > OVERSIZED_DESCRIPTION_TOKENS),
        key=lambda c: (-c["description_tokens"], c["name"]),
    )
    return {
        "model": model or "",
        "count": len(costs),
        "total_tokens": total,
        "offered_tokens": offered,
        "deferred_tokens": total - hidden_tokens - offered,
        "hidden_tokens": hidden_tokens,
        "hidden_tools": len(costs) - len(visible),
        "by_source": sources,
        "by_exposure": by_exposure,
        "heaviest": [_public(c) for c in ranked[:top]],
        "oversized_descriptions": [
            {"name": c["name"], "source": c["source"], "description_tokens": c["description_tokens"]}
            for c in oversized[:top]
        ],
        "name_collisions": name_collisions(visible),
        "near_duplicates": near_duplicates(visible, similarity=similarity),
        "thresholds": {
            "similarity": similarity,
            "oversized_description_tokens": OVERSIZED_DESCRIPTION_TOKENS,
            "min_words_to_compare": _MIN_WORDS_TO_COMPARE,
        },
    }


__all__ = [
    "OVERSIZED_DESCRIPTION_TOKENS", "DEFAULT_SIMILARITY", "DEFAULT_TOP",
    "source_of", "short_name", "wire_schema", "default_counter", "description_words",
    "jaccard", "builtin_exposure", "native_twin", "rows_from_mappings", "tool_costs",
    "name_collisions", "near_duplicates", "footprint_report",
]
