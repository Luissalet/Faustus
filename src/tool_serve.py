"""On-demand tool catalog for the agent (FAUSTUS).

Tool-RAG already picks a relevant subset instead of dumping every schema into
the prompt. Domain floors still inflate that subset — a coding turn with a
bound workspace plus an incidental "email" word used to ship 40–50 native
schemas. Dropping those extras would hide them; keeping them all burns the
window.

This module is the third path: a small always-on helper (`lookup_tools`) that
searches the same index the retriever uses, plus a compact catalog of the
tools this turn can run but whose full schemas were not loaded. Availability
stays the same — the names are in the prompt and remain executable — while
native function-call schemas shrink to the hot set.

Never removes a selected tool from the executable set. Partitioning only
decides which names get a full schema this round.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

LOOKUP_TOOL = "lookup_tools"
_MAX_RETURN = 12
_DEFAULT_K = 8
_ONE_LINER_LIMIT = 160
_CATALOG_LIMIT = 80
_ELLIPSIS = "…"

_DETAIL_SCHEMA = "schema"
_DETAIL_CATALOG = "catalog"


def one_liner(name: str, text: str = "") -> str:
    """First sentence of a tool description, clipped for a catalog row."""
    raw = (text or "").strip()
    if not raw:
        raw = _description_for(name)
    if not raw:
        return name
    cut = raw.split("\n", 1)[0].strip()
    dot = cut.find(". ")
    if 24 <= dot <= _ONE_LINER_LIMIT:
        cut = cut[: dot + 1]
    if len(cut) > _ONE_LINER_LIMIT:
        cut = cut[: _ONE_LINER_LIMIT].rstrip() + _ELLIPSIS
    return cut


def _description_for(name: str) -> str:
    try:
        from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
        text = BUILTIN_TOOL_DESCRIPTIONS.get(name) or ""
        if text:
            return str(text)
    except Exception:
        logger.debug("tool catalog: builtin description lookup failed for %s", name, exc_info=True)
    schema = schema_for(name)
    if schema:
        fn = schema.get("function") if isinstance(schema.get("function"), dict) else schema
        return str((fn or {}).get("description") or "")
    return ""


def schema_for(name: str) -> Optional[Dict[str, Any]]:
    """OpenAI-style function schema for a built-in or currently-connected MCP tool."""
    if not name:
        return None
    try:
        from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
        for entry in FUNCTION_TOOL_SCHEMAS:
            fn = entry.get("function") if isinstance(entry, dict) else None
            if isinstance(fn, dict) and fn.get("name") == name:
                return entry
    except Exception:
        logger.debug("tool catalog: native schema lookup failed for %s", name, exc_info=True)
    try:
        from src.tool_utils import get_mcp_manager
        mcp = get_mcp_manager()
        if mcp and hasattr(mcp, "get_all_openai_schemas"):
            for entry in mcp.get_all_openai_schemas({}) or []:
                fn = entry.get("function") if isinstance(entry, dict) else None
                if isinstance(fn, dict) and fn.get("name") == name:
                    return entry
    except Exception:
        logger.debug("tool catalog: MCP schema lookup failed for %s", name, exc_info=True)
    return None


def connected_mcp_tool_names() -> List[str]:
    """Qualified names (`mcp__<server>__<tool>`) of every connected MCP tool."""
    try:
        from src.tool_utils import get_mcp_manager
        mcp = get_mcp_manager()
        if mcp and hasattr(mcp, "get_all_openai_schemas"):
            out: List[str] = []
            for entry in mcp.get_all_openai_schemas({}) or []:
                fn = entry.get("function") if isinstance(entry, dict) else None
                name = str((fn or {}).get("name") or "")
                if name.startswith("mcp__"):
                    out.append(name)
            return out
    except Exception:
        logger.debug("tool catalog: MCP tool listing failed", exc_info=True)
    return []


def resolve_bare_name(name: str) -> List[str]:
    """The catalog names a bare tool name stands for.

    A plugin's tools reach the model as `mcp__<server>__<tool>`, but a skill,
    a doc or the user says `screen_activity`. Passed through as written, the
    bare name came back as a stub with no schema and a `promote` of a tool
    that does not exist — seen live: a 27B spent seven `lookup_tools` rounds
    (one per app, ~150 s) finding names a single call had already asked for.
    A name that is a real built-in or qualified tool is returned as is; a
    bare name that matches the tail of one or more connected MCP tools maps
    to those; anything else is passed through unchanged, as before."""
    bare = str(name or "").strip()
    if not bare:
        return []
    if bare.startswith("mcp__") or schema_for(bare) is not None:
        return [bare]
    suffix = f"__{bare}"
    matches = [n for n in connected_mcp_tool_names() if n.endswith(suffix)]
    return matches or [bare]


def catalog_entry(name: str, *, detail: str = _DETAIL_CATALOG) -> Dict[str, Any]:
    entry: Dict[str, Any] = {"name": name, "summary": one_liner(name)}
    if detail == _DETAIL_SCHEMA:
        schema = schema_for(name)
        if schema is not None:
            entry["schema"] = schema
    return entry


def partition_offer(
    relevant: Optional[Iterable[str]],
    *,
    hot_seed: Optional[Iterable[str]] = None,
    enabled: bool = True,
) -> Tuple[Set[str], Set[str]]:
    """Split the selected set into native-schema (hot) vs catalog (deferred).

    ``hot_seed`` is what retrieval / floors / named / forced already chose.
    Domain-map expansions beyond that seed stay executable but lose their
    full schema until ``lookup_tools`` promotes them. The helper itself is
    always hot.
    """
    selected = {str(n) for n in (relevant or ()) if n}
    selected.add(LOOKUP_TOOL)
    if not enabled or not selected:
        return selected, set()
    hot = {str(n) for n in (hot_seed or ()) if n}
    hot.add(LOOKUP_TOOL)
    hot &= selected
    deferred = selected - hot
    return hot, deferred


def compact_catalog_block(
    names: Iterable[str],
    *,
    disabled: Optional[Iterable[str]] = None,
    limit: int = _CATALOG_LIMIT,
) -> str:
    """Prompt section listing deferred tools as one-liners. Empty string if none."""
    blocked = {str(n) for n in (disabled or ()) if n}
    rows = []
    skipped = 0
    for name in sorted({str(n) for n in names if n}):
        if name == LOOKUP_TOOL or name in blocked:
            continue
        if len(rows) >= limit:
            skipped += 1
            continue
        rows.append(f"- `{name}` — {one_liner(name)}")
    if not rows:
        return ""
    extra = f"\n- …and {skipped} more. Call `{LOOKUP_TOOL}` with a query to search them." if skipped else ""
    return (
        "## More tools (catalog)\n"
        "These tools are available this turn, but their full schemas are not loaded "
        f"(to save context). Call them with a fenced block, or call `{LOOKUP_TOOL}` "
        "with `query` or `names` to load native schemas for the next round:\n"
        + "\n".join(rows)
        + extra
    )


def _keyword_hits(query: str) -> List[str]:
    """Tools a keyword hint names for ``query``, best first.

    The hint values are sets, and the caller truncates to ``k``: taken in
    iteration order, which of the thirteen email tools made the cut for
    "send an email to Alex" changed from one interpreter to the next (string
    hashing is salted per process), so `send_email` was in the list on some
    runs and not on others. Ranked instead: a tool whose own name repeats
    more of the query's words comes first ("send" + "email" → `send_email`),
    then the order of the hint groups, then the name.
    """
    if not query.strip():
        return []
    try:
        from src.tool_index import ToolIndex
        ql = query.lower()
        # Words of three letters or more: "to" would otherwise tie
        # `reply_to_email` with `send_email` for "send an email to Alex".
        words = {w for w in re.findall(r"[a-z0-9]+", ql) if len(w) >= 3}
        rank: Dict[str, tuple] = {}
        for group, (keywords, tools) in enumerate(ToolIndex._KEYWORD_HINTS.items()):
            if any(re.search(rf"\b{re.escape(kw)}\b", ql) for kw in keywords):
                for name in tools:
                    if name not in rank:
                        overlap = len(words & set(name.lower().split("_")))
                        rank[name] = (-overlap, group, name)
        return sorted(rank, key=rank.__getitem__)
    except Exception:
        logger.debug("tool catalog: keyword hints unavailable", exc_info=True)
        return []


def search_catalog(
    query: str = "",
    names: Optional[Sequence[str]] = None,
    *,
    k: int = _DEFAULT_K,
    disabled: Optional[Iterable[str]] = None,
    admin: bool = True,
) -> List[str]:
    """Ranked tool names for a natural-language query and/or exact names."""
    blocked = {str(n) for n in (disabled or ()) if n}
    wanted = [str(n).strip() for n in (names or ()) if str(n).strip()]
    ordered: List[str] = []
    seen: Set[str] = set()

    def _add(name: str) -> None:
        if not name or name in seen or name in blocked:
            return
        if name != LOOKUP_TOOL and not admin and _non_admin_blocked(name):
            return
        seen.add(name)
        ordered.append(name)

    for name in wanted:
        for resolved in resolve_bare_name(name):
            _add(resolved)

    q = (query or "").strip()
    if q:
        retrieved: List[str] = []
        try:
            from src.tool_index import get_tool_index
            idx = get_tool_index()
            if idx is not None:
                retrieved = list(idx.retrieve(q, k=max(k, 1)))
        except Exception:
            logger.debug("tool catalog: index retrieve failed", exc_info=True)
        for name in _keyword_hits(q):
            _add(name)
        for name in retrieved:
            _add(name)
        if not retrieved:
            from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
            needle = q.lower()
            for name, desc in BUILTIN_TOOL_DESCRIPTIONS.items():
                hay = f"{name} {desc}".lower()
                if needle in hay or any(tok in hay for tok in needle.split() if len(tok) > 3):
                    _add(name)

    return ordered[: max(1, min(int(k or _DEFAULT_K), _MAX_RETURN))]


def _non_admin_blocked(name: str) -> bool:
    try:
        from src.tool_security import NON_ADMIN_BLOCKED_TOOLS
        return name in NON_ADMIN_BLOCKED_TOOLS
    except Exception:
        return False


def suggest_close_matches(typo: str, pool: Iterable[str], *, n: int = 6) -> List[str]:
    """Close names from the catalog, not just the schemas already sent."""
    import difflib
    names = [str(x) for x in pool if x]
    if not typo or not names:
        return []
    close = list(difflib.get_close_matches(typo, names, n=n, cutoff=0.5))
    lower = typo.lower()
    close.extend(x for x in names if lower in x.lower() and x not in close)
    return list(dict.fromkeys(close))[:n]


def serve(
    *,
    query: str = "",
    names: Optional[Sequence[str]] = None,
    detail: str = "",
    k: int = _DEFAULT_K,
    disabled: Optional[Iterable[str]] = None,
    admin: bool = True,
) -> Dict[str, Any]:
    """Search/index result the model can act on. Never executes the listed tools."""
    mode = (detail or "").strip().lower()
    if mode not in (_DETAIL_SCHEMA, _DETAIL_CATALOG):
        mode = _DETAIL_SCHEMA if names else _DETAIL_CATALOG
        if query and not names:
            mode = _DETAIL_SCHEMA
    found = search_catalog(
        query, names, k=k, disabled=disabled, admin=admin,
    )
    tools = [catalog_entry(name, detail=mode) for name in found]
    payload: Dict[str, Any] = {
        "tools": tools,
        "promote": [t["name"] for t in tools],
        "detail": mode,
        "query": query,
    }
    if not tools:
        payload["hint"] = (
            "No matching tools. Try a shorter action phrase "
            '(e.g. "send email", "git commit", "list files").'
        )
    else:
        payload["hint"] = (
            "These tools are callable this turn (fenced block with JSON args). "
            "Native function schemas load on the next round."
        )
    return payload


def execute_lookup(content: str, ctx: Optional[Mapping[str, Any]] = None) -> Tuple[str, Dict[str, Any]]:
    """Handler for the `lookup_tools` tool. Read-only catalog; no side effects."""
    ctx = ctx or {}
    raw = (content or "").strip()
    parsed: Any = {}
    if raw:
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = {"query": raw}
    if isinstance(parsed, str):
        parsed = {"query": parsed}
    if not isinstance(parsed, dict):
        return f"{LOOKUP_TOOL}: invalid", {
            "error": (
                f"{LOOKUP_TOOL} needs a JSON object with `query` (what you want to do) "
                "and/or `names` (exact tool names). Optional `detail`: catalog|schema."
            ),
            "exit_code": 1,
        }

    query = str(parsed.get("query") or parsed.get("q") or "").strip()
    names = parsed.get("names") or parsed.get("name") or []
    if isinstance(names, str):
        names = [names]
    if not isinstance(names, (list, tuple)):
        names = []
    detail = str(parsed.get("detail") or parsed.get("mode") or "").strip()
    try:
        k = int(parsed.get("k") or _DEFAULT_K)
    except (TypeError, ValueError):
        k = _DEFAULT_K

    if not query and not names:
        return f"{LOOKUP_TOOL}: invalid", {
            "error": (
                f"{LOOKUP_TOOL} needs `query` (natural language) or `names` "
                "(list of tool names)."
            ),
            "exit_code": 1,
        }

    admin = True
    try:
        from src.tool_security import owner_is_admin_or_single_user
        admin = bool(owner_is_admin_or_single_user(ctx.get("owner")))
    except Exception:
        admin = True

    payload = serve(
        query=query,
        names=list(names),
        detail=detail,
        k=k,
        disabled=ctx.get("disabled_tools"),
        admin=admin,
    )
    listed = ", ".join(payload.get("promote") or []) or "none"
    desc = f"{LOOKUP_TOOL}: {listed}"
    result = {
        "output": json.dumps(payload, ensure_ascii=False, default=str),
        "exit_code": 0,
        LOOKUP_TOOL: payload,
        "promote": payload.get("promote") or [],
    }
    return desc, result
