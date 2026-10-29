"""`read_artifact` / `artifact_search`: the model's way back into an
offloaded tool result.

`src/tool_result_offload.py` (A12) stores an oversized tool result whole in
the artifact store and leaves the model a bounded summary plus an
`artifact_id`. `read_artifact` lets the model open that artifact by
character range or by substring query — scoped to the owner exactly like the
HTTP routes, so another tenant's id reads as "not found" (A13).

`artifact_search` (src/offload_search.py) is the search half of the same
story: instead of paging through an offloaded result blindly, the model can
search inside it (and every other offloaded result this owner/session has)
for a term and get back ranked hits with the offset to open next.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, Optional

TOOL_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "read_artifact",
        "description": "Read stored text directly by id and query with surrounding context, or by character range (start/end, 0-based, end exclusive). For a known artifact, query + context_chars retrieves the passage in one call without artifact_search followed by offset reads. Set occurrence (1-based, default 1) to skip declarations/calls. text_pointer selects a literal source string such as /full_text: offsets and returned line numbers then refer to that source, without duplicated preview or JSON escaping. For JSON values, select json_pointer and optionally filter an array by match_pointer + match_value; json_source selects a serialized JSON body such as /stdout first.",
        "parameters": {
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string"},
                "start": {"type": "integer", "description": "0-based start offset (range mode)."},
                "end": {"type": "integer", "description": "Exclusive end offset (range mode)."},
                "query": {"type": "string", "description": "Case-insensitive substring to locate instead of a range."},
                "text_pointer": {"type": "string", "description": "RFC 6901 pointer to a literal source-text string in the stored result, e.g. /full_text. Query/range offsets and returned 1-based line numbers refer to that string. Use with query and context_chars to recover source context in one call; cannot combine with JSON selection."},
                "occurrence": {"type":"integer","minimum":1,"description":"1-based non-overlapping match to return (default 1). Requires a non-empty query."},
                "context_chars": {"type":"integer","minimum":0,"description":"Characters before and after the query match (default 400). Requires a non-empty query."},
                "json_source": {"type": "string", "description": "RFC 6901 pointer to a JSON body inside the stored result, e.g. /stdout. A string body is decoded as JSON once. Default: stored result itself."},
                "json_pointer": {"type": "string", "description": "RFC 6901 pointer within the JSON body, e.g. /entries. Empty string selects the root. Use instead of start/end/query."},
                "match_pointer": {"type": "string", "description": "RFC 6901 pointer within each selected array item, e.g. /reference/id. Keep all items whose field equals match_value; missing fields do not match."},
                "match_value": {"type": ["string", "number", "boolean", "null"], "description": "Exact JSON scalar to match. Numbers and booleans remain distinct. Requires match_pointer."},
            },
            "required": ["artifact_id"],
        },
    },
}


def _pointer_tokens(pointer: str) -> list[str]:
    if not isinstance(pointer, str) or (pointer and not pointer.startswith('/')):
        raise ValueError('JSON pointer must be empty or begin with /')
    if re.search(r'~(?![01])', pointer):
        raise ValueError('JSON pointer escape must be ~0 or ~1')
    return [part.replace('~1', '/').replace('~0', '~')
            for part in pointer[1:].split('/')] if pointer else []


def _resolve_pointer(value: Any, tokens: list[str]) -> Any:
    for token in tokens:
        if isinstance(value, dict):
            value = value[token]
        elif isinstance(value, list):
            if not re.fullmatch(r'0|[1-9][0-9]*', token):
                raise KeyError('invalid array index')
            value = value[int(token)]
        else:
            raise KeyError('pointer cannot traverse a scalar')
    return value


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError('JSON number is outside the supported finite range')
    return value


def _reject_constant(text: str) -> Any:
    raise ValueError('Non-finite constant is not a JSON number: ' + text)


def _load_json(text: str) -> Any:
    return json.loads(text, parse_float=_finite_float, parse_constant=_reject_constant)


def _json_selection(text: str, args: dict, artifact_id: str) -> dict:
    if 'json_pointer' not in args:
        raise ValueError('json_pointer is required for JSON selection')
    if any(key in args for key in ('start', 'end', 'query', 'occurrence', 'context_chars', 'text_pointer')):
        raise ValueError('JSON selection cannot be combined with start, end, query, occurrence, context_chars or text_pointer')
    value = _load_json(text)
    source = args.get('json_source', '')
    value = _resolve_pointer(value, _pointer_tokens(source))
    if 'json_source' in args and isinstance(value, str):
        value = _load_json(value)
    value = _resolve_pointer(value, _pointer_tokens(args['json_pointer']))
    total_items = len(value) if isinstance(value, list) else None
    if ('match_pointer' in args) != ('match_value' in args):
        raise ValueError('match_pointer and match_value must be supplied together')
    if 'match_pointer' in args:
        if not isinstance(value, list):
            raise ValueError('array matching requires an array selection')
        expected = args['match_value']
        if expected is not None and not isinstance(expected, (str, int, float, bool)):
            raise ValueError('match_value must be a JSON scalar')
        if isinstance(expected, float) and not math.isfinite(expected):
            raise ValueError('match_value must be finite')
        tokens = _pointer_tokens(args['match_pointer'])
        matches = []
        for item in value:
            try:
                field = _resolve_pointer(item, tokens)
            except (KeyError, IndexError):
                continue
            if (isinstance(field, bool) == isinstance(expected, bool)
                    and field == expected):
                matches.append(item)
        value = matches
    payload = {'artifact_id': artifact_id, 'json_source': source,
               'json_pointer': args['json_pointer'], 'value': value,
               'total_items': total_items,
               'matched_items': len(value) if isinstance(value, list) else None,
               'total_chars': len(text)}
    return {'output': json.dumps(payload, ensure_ascii=False, allow_nan=False), 'exit_code': 0,
            'artifact_id': artifact_id}


class ReadArtifactTool:
    async def execute(self, content: Any, ctx: Optional[dict] = None) -> dict:
        from src.owner_identity import effective_storage_owner
        from src.tool_result_offload import ArtifactAccessDenied, read_artifact_range

        raw = content if isinstance(content, str) else json.dumps(content or {})
        try:
            args = json.loads(raw) if raw.strip() else {}
        except (TypeError, ValueError):
            args = {"artifact_id": raw.strip()}
        if not isinstance(args, dict):
            args = {"artifact_id": str(args)}
        artifact_id = str(args.get("artifact_id") or "").strip()
        if not artifact_id:
            return {"error": "read_artifact requires `artifact_id`.", "exit_code": 1}
        ctx = ctx or {}
        owner = effective_storage_owner(ctx.get("owner"), auth_is_disabled=None) or ""
        if not owner:
            return {"error": "artifact not found", "exit_code": 1}
        json_mode = any(key in args for key in ("json_source", "json_pointer", "match_pointer", "match_value"))
        query = args.get("query")
        try:
            start = int(args.get("start") or 0)
        except (TypeError, ValueError):
            start = 0
        end = args.get("end")
        try:
            end = int(end) if end not in (None, "") else None
        except (TypeError, ValueError):
            end = None
        try:
            if json_mode:
                stored = read_artifact_range(artifact_id, owner=owner)
                try:
                    return _json_selection(stored["text"], args, artifact_id)
                except (ValueError, TypeError, KeyError, IndexError) as exc:
                    return {"error": "JSON selection: " + str(exc), "exit_code": 1}
            query_options = {key: args[key] for key in ("occurrence", "context_chars") if key in args}
            if "text_pointer" in args:
                stored = read_artifact_range(artifact_id, owner=owner)
                try:
                    text = _resolve_pointer(_load_json(stored["text"]),
                                            _pointer_tokens(args["text_pointer"]))
                    if not isinstance(text, str):
                        raise ValueError("text_pointer must select a string")
                except (ValueError, TypeError, KeyError, IndexError) as exc:
                    return {"error": "Artifact text: " + str(exc), "exit_code": 1}
                from src.tool_result_offload import read_text_range
                result = read_text_range(
                    text, artifact_id, start=start if query is None else 0,
                    end=end if query is None else None,
                    query=str(query) if query is not None else None,
                    **query_options,
                )
                result["text_pointer"] = args["text_pointer"]
                if "matches" in result:
                    for match in result["matches"]:
                        context_start = max(0, match["offset"] - args.get("context_chars", 400))
                        match["line"] = text.count("\n", 0, match["offset"]) + 1
                        match["context_start_line"] = text.count("\n", 0, context_start) + 1
                else:
                    result["start_line"] = text.count("\n", 0, result["start"]) + 1
            else:
                result = read_artifact_range(
                    artifact_id, owner=owner,
                    start=start if query is None else 0,
                    end=end if query is None else None,
                    query=str(query) if query is not None else None,
                    **query_options,
                )
            if query_options and not query:
                raise ValueError("occurrence and context_chars require a non-empty query")
            return result
        except ValueError as exc:
            return {"error": "Artifact query: " + str(exc), "exit_code": 1}
        except ArtifactAccessDenied:
            # Same words for "not yours" and "does not exist" (A13).
            return {"error": "artifact not found", "exit_code": 1}


ARTIFACT_SEARCH_TOOL_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "artifact_search",
        "description": "Search across offloaded tool results to discover which artifact contains a term. Scoped to your own artifacts in the current session by default; artifact_id restricts discovery to one result. For a known artifact, read_artifact(artifact_id=..., query=..., context_chars=...) retrieves the passage directly in one call; text_pointer selects an original source string such as /full_text. Hits also provide start/end ranges for range reads.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Term or phrase to search for."},
                "artifact_id": {
                    "type": "string",
                    "description": "Restrict the search to one artifact id instead of every offloaded result in this session.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max hits to return (default 8, max 20).",
                },
            },
            "required": ["query"],
        },
    },
}


class ArtifactSearchTool:
    async def execute(self, content: Any, ctx: Optional[dict] = None) -> dict:
        from src.owner_identity import effective_storage_owner
        from src.offload_search import search as search_offloaded

        raw = content if isinstance(content, str) else json.dumps(content or {})
        try:
            args = json.loads(raw) if raw.strip() else {}
        except (TypeError, ValueError):
            args = {"query": raw.strip()}
        if not isinstance(args, dict):
            args = {"query": str(args)}

        query = str(args.get("query") or "").strip()
        if not query:
            return {"error": "artifact_search requires `query`.", "exit_code": 1}

        ctx = ctx or {}
        owner = effective_storage_owner(ctx.get("owner"), auth_is_disabled=None) or ""
        if not owner:
            return {"hits": [], "count": 0}

        session_id = str(ctx.get("session_id") or "") or None
        artifact_id = str(args.get("artifact_id") or "").strip() or None
        try:
            limit = int(args.get("limit") or 8)
        except (TypeError, ValueError):
            limit = 8
        limit = max(1, min(limit, 20))

        hits = search_offloaded(
            owner, query, session_id=session_id, artifact_id=artifact_id, limit=limit,
        )
        return {"hits": hits, "count": len(hits), "output": render_hits(query, hits)}


def render_hits(query: str, hits: list) -> str:
    """The hits as readable text for the model: the structured result was
    handed over as JSON, so every snippet's line breaks arrived as literal
    ``\\n`` escapes."""
    if not hits:
        return f"artifact_search: no stored result contains {query!r}."
    lines = [f"artifact_search: {len(hits)} hit(s) for {query!r}. "
             "For a known id, read_artifact(artifact_id, query, context_chars) returns context directly; ranges remain available."]
    for n, hit in enumerate(hits, 1):
        snippet = str(hit.get("snippet") or "").strip()
        lines.append(f"\n[{n}] {hit.get('artifact_id')} ({hit.get('tool') or 'tool'}) "
                     f"chars {hit.get('start')}-{hit.get('end')}:\n{snippet}")
    return "\n".join(lines)
