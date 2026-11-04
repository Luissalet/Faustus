#!/usr/bin/env python3
"""bridges/rest_mcp/server.py — a generic REST -> MCP stdio adapter.

Turns a local HTTP API (a domain app with no native MCP server of its own)
into MCP tools, without any app-specific code. Configuration is entirely by
environment variable — Faustus spawns one of these per connector, the same
way it spawns any other stdio MCP server (see `src/mcp_manager.py`,
`src/connectors.py`).

Env vars
--------
REST_BASE_URL       Required. The app's own base URL. Loopback only
                     (127.0.0.0/8, ::1, "localhost") — anything else refuses
                     to start (this process would otherwise be a generic
                     proxy an agent could point at a stranger's server).
REST_OPENAPI_URL     Optional. An OpenAPI (3.x) document to fetch at startup;
                     one tool is built per operation (see `_openapi_to_tools`).
REST_MANIFEST        Optional. Path to a JSON manifest (see `_read_manifest`)
                     — the fallback/explicit tool listing used when the app
                     has no OpenAPI document, or to declare write operations
                     an OpenAPI-only build would otherwise skip.
REST_ALLOW           Optional regex over "METHOD /path", filtering which
                     OpenAPI operations become tools (manifest tools are
                     always included — a human already chose them by writing
                     the manifest). Default: "^GET " (read-only only).
REST_TOKEN_FILE      Optional path to a bearer token, sent as
                     "Authorization: Bearer <token>" on every request.
REST_HEADERS         Optional JSON object of extra request headers (e.g. {"X-Api-Key": "..."}), sent on every
                     request including the GraphQL introspection; merged over the bearer token.
REST_NAME            Optional display name for the MCP server.
REST_GRAPHQL_PATH    Optional. Path of a GraphQL endpoint under REST_BASE_URL (e.g. /graphql). Its schema is
                     read by introspection at startup and every first-level Query field becomes a read-only tool
                     (`query_<field>`), with the selection set derived from the schema.
REST_GRAPHQL_SCHEMA  Optional path to a saved introspection result (JSON), used instead of the live call.
REST_GRAPHQL_MUTATIONS  Optional. "1" also exposes the Mutation fields (`mutation_<field>`); off by default.
REST_HEALTH_PATH     Optional path for the always-present `app_status` tool.
                     Default "/api/health".

Responses are pretty-printed JSON, truncated at 20 kB with a note. A binary
body (by content-type) is never returned as bytes — only a note naming its
content-type and size.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit

DEFAULT_HEALTH_PATH = "/api/health"
DEFAULT_ALLOW = r"^GET "
TRUNCATE_BYTES = 20_000
_TEXT_CONTENT_TYPES = ("text/", "application/json", "application/xml", "application/javascript")
_TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_IDENT_RE = re.compile(r"[^a-zA-Z0-9_]")

_JSON_TYPE_TO_PY = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "object": "dict",
    "array": "list",
}


# ── startup validation ─────────────────────────────────────────────────────

def is_loopback_base_url(base_url: str) -> bool:
    """True when `base_url`'s host is 127.0.0.0/8, ::1 or "localhost"."""
    try:
        host = urlsplit(base_url).hostname or ""
    except ValueError:
        return False
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return addr.is_loopback


# ── manifest / OpenAPI -> canonical tool specs ─────────────────────────────
# A canonical tool spec:
#   {"name": str, "description": str, "method": "GET", "path": "/x/{pid}",
#    "readonly": bool,
#    "params": {py_name: {"orig": str, "in": "path"|"query"|"body"|"body_field",
#                          "type": "string", "required": bool, "description": str}}}

def _sanitize_ident(name: str) -> str:
    ident = _IDENT_RE.sub("_", name).strip("_") or "param"
    if ident[0].isdigit():
        ident = f"p_{ident}"
    return ident


def _path_slug(path: str) -> str:
    s = re.sub(r"[{}]", "", path.strip("/"))
    s = re.sub(r"[^a-zA-Z0-9]+", "_", s)
    return s.strip("_") or "root"


def _dedupe_ident(base: str, used: set) -> str:
    name = base
    n = 2
    while name in used:
        name = f"{base}_{n}"
        n += 1
    used.add(name)
    return name


def read_manifest(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def manifest_to_tools(manifest: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Manifest ``tools`` entries -> canonical specs. Second element: notes
    (skipped entries, e.g. a bad tool name)."""
    specs: List[Dict[str, Any]] = []
    notes: List[str] = []
    for entry in manifest.get("tools", []):
        name = str(entry.get("name") or "")
        if not _TOOL_NAME_RE.match(name):
            notes.append(f"manifest tool skipped: invalid name {name!r}")
            continue
        method = str(entry.get("method") or "GET").upper()
        path = str(entry.get("path") or "/")
        params: Dict[str, Any] = {}
        used_idents: set = set()
        for orig_name, meta in (entry.get("params") or {}).items():
            meta = meta or {}
            py_name = _dedupe_ident(_sanitize_ident(orig_name), used_idents)
            params[py_name] = {
                "orig": orig_name,
                "in": meta.get("in", "query"),
                "type": meta.get("type", "string"),
                "required": bool(meta.get("required", meta.get("in") == "path")),
                "description": meta.get("description", ""),
            }
        specs.append({
            "name": name,
            "description": entry.get("description") or f"{method} {path}",
            "method": method,
            "path": path,
            "readonly": bool(entry.get("readonly", method == "GET")),
            "params": params,
        })
    return specs, notes


def _openapi_param_schema(schema: Dict[str, Any]) -> str:
    return schema.get("type", "string") if isinstance(schema, dict) else "string"


def openapi_to_tools(spec: Dict[str, Any], allow_regex: Optional[str]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """One tool per OpenAPI operation. Filtered by `allow_regex` (default
    read-only: GET only) over "METHOD /path". Multipart request bodies are
    skipped (noted, never turned into a tool — no file upload over stdio)."""
    pattern = re.compile(allow_regex or DEFAULT_ALLOW)
    specs: List[Dict[str, Any]] = []
    notes: List[str] = []
    used_names: set = set()
    paths = spec.get("paths") or {}
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        shared_params = path_item.get("parameters") or []
        for method in ("get", "post", "put", "patch", "delete"):
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            method_upper = method.upper()
            key = f"{method_upper} {path}"
            if not pattern.search(key):
                continue
            op_params = shared_params + (operation.get("parameters") or [])
            params: Dict[str, Any] = {}
            used_idents: set = set()
            for p in op_params:
                if not isinstance(p, dict) or p.get("in") not in ("path", "query"):
                    continue
                orig_name = p.get("name")
                if not orig_name:
                    continue
                py_name = _dedupe_ident(_sanitize_ident(orig_name), used_idents)
                params[py_name] = {
                    "orig": orig_name,
                    "in": p["in"],
                    "type": _openapi_param_schema(p.get("schema") or {}),
                    "required": bool(p.get("required", p["in"] == "path")),
                    "description": p.get("description", ""),
                }

            request_body = operation.get("requestBody")
            skip_multipart = False
            if isinstance(request_body, dict):
                content = request_body.get("content") or {}
                if "application/json" in content:
                    body_schema = (content["application/json"] or {}).get("schema") or {}
                    props = body_schema.get("properties") if isinstance(body_schema, dict) else None
                    required_props = set((body_schema or {}).get("required") or [])
                    if body_schema.get("type") == "object" and isinstance(props, dict) and len(props) <= 12:
                        for orig_name, prop_schema in props.items():
                            py_name = _dedupe_ident(_sanitize_ident(orig_name), used_idents)
                            params[py_name] = {
                                "orig": orig_name,
                                "in": "body_field",
                                "type": _openapi_param_schema(prop_schema or {}),
                                "required": orig_name in required_props,
                                "description": (prop_schema or {}).get("description", "") if isinstance(prop_schema, dict) else "",
                            }
                    else:
                        py_name = _dedupe_ident("body", used_idents)
                        params[py_name] = {
                            "orig": "body", "in": "body", "type": "object",
                            "required": bool(request_body.get("required")),
                            "description": "JSON request body.",
                        }
                elif content:
                    skip_multipart = True

            if skip_multipart:
                notes.append(f"skipped {key}: request body is not JSON (multipart/binary upload)")
                continue

            op_id = operation.get("operationId")
            if op_id and _TOOL_NAME_RE.match(str(op_id)):
                base_name = str(op_id)
            else:
                base_name = f"{method}_{_path_slug(path)}"[:64]
            name = _dedupe_ident(base_name, used_names)
            if not _TOOL_NAME_RE.match(name):
                notes.append(f"skipped {key}: could not derive a valid tool name")
                continue
            specs.append({
                "name": name,
                "description": operation.get("summary") or operation.get("description") or key,
                "method": method_upper,
                "path": path,
                "readonly": method_upper == "GET",
                "params": params,
            })
    return specs, notes


# ── the HTTP call every tool makes ─────────────────────────────────────────

def _is_text_content_type(content_type: str) -> bool:
    ct = (content_type or "").split(";")[0].strip().lower()
    return ct.startswith("text/") or ct in ("application/json", "application/xml",
                                             "application/javascript") or ct.endswith("+json") or ct.endswith("+xml")


def _render_response(status_code: int, content_type: str, raw: bytes) -> str:
    if not _is_text_content_type(content_type):
        return f"[binary response: {status_code} {content_type or 'unknown content-type'}, {len(raw)} bytes]"
    text = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text)
        text = json.dumps(parsed, indent=2, ensure_ascii=False)
    except (json.JSONDecodeError, ValueError):
        pass
    encoded = text.encode("utf-8")
    if len(encoded) > TRUNCATE_BYTES:
        text = encoded[:TRUNCATE_BYTES].decode("utf-8", errors="ignore")
        text += f"\n... [truncated, {len(encoded)} bytes total, showing first {TRUNCATE_BYTES}]"
    prefix = "" if 200 <= status_code < 300 else f"HTTP {status_code}\n"
    return prefix + text


def auth_headers(token: Any) -> Dict[str, str]:
    """Request headers for the credential: a bearer token string, or a ready ``{header: value}`` dict
    (REST_HEADERS), or nothing."""
    if isinstance(token, dict):
        return {str(k): str(v) for k, v in token.items()}
    return {"Authorization": f"Bearer {token}"} if token else {}


def parse_extra_headers(raw: Any) -> Dict[str, str]:
    """``REST_HEADERS``: a JSON object of extra request headers, e.g. {"X-Api-Key": "..."}."""
    if not raw:
        return {}
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k).strip(): str(v) for k, v in data.items() if str(k).strip() and v is not None}


async def call_rest(base_url: str, token: Optional[str], method: str, path: str,
                     path_values: Dict[str, str], query: Dict[str, Any],
                     body: Optional[Dict[str, Any]], timeout: float = 30.0) -> str:
    import asyncio
    import httpx

    resolved_path = path
    for name, value in path_values.items():
        resolved_path = resolved_path.replace("{" + name + "}", quote(str(value), safe=""))
    url = base_url.rstrip("/") + "/" + resolved_path.lstrip("/")
    headers = auth_headers(token)
    query = {k: v for k, v in (query or {}).items() if v is not None}

    def _do_request() -> Tuple[int, str, bytes]:
        with httpx.Client(timeout=timeout) as client:
            resp = client.request(method, url, params=query or None,
                                   json=body if body else None, headers=headers)
            return resp.status_code, resp.headers.get("content-type", ""), resp.content

    try:
        status_code, content_type, raw = await asyncio.to_thread(_do_request)
    except httpx.RequestError as exc:
        return f"request failed: {exc}"
    return _render_response(status_code, content_type, raw)


def _split_params(params: Dict[str, Any], values: Dict[str, Any]):
    path_values: Dict[str, str] = {}
    query: Dict[str, Any] = {}
    body: Optional[Dict[str, Any]] = None
    body_fields: Dict[str, Any] = {}
    has_body_fields = False
    for py_name, meta in params.items():
        value = values.get(py_name)
        kind = meta["in"]
        if kind == "path":
            path_values[meta["orig"]] = value
        elif kind == "query":
            query[meta["orig"]] = value
        elif kind == "body":
            if isinstance(value, dict):
                body = value
            elif value not in (None, ""):
                body = {"value": value}
        elif kind == "body_field":
            has_body_fields = True
            if value is not None:
                body_fields[meta["orig"]] = value
    if has_body_fields:
        body = body_fields if body is None else {**body_fields, **body}
    return path_values, query, body


# ── dynamic tool functions (FastMCP builds a JSON schema from a real
#    function signature — see mcp.server.fastmcp.utilities.func_metadata) ──

def _build_tool_function(tool_name: str, params: Dict[str, Any], dispatch):
    required = [(n, m) for n, m in params.items() if m["required"]]
    optional = [(n, m) for n, m in params.items() if not m["required"]]

    def _ann(py_type: str) -> str:
        return _JSON_TYPE_TO_PY.get(py_type, "str")

    arg_defs = []
    for n, m in required:
        arg_defs.append(f"{n}: {_ann(m['type'])}")
    for n, m in optional:
        arg_defs.append(f"{n}: Optional[{_ann(m['type'])}] = None")

    src = (
        f"async def _tool({', '.join(arg_defs)}):\n"
        f"    return await _dispatch({tool_name!r}, dict(locals()))\n"
    )
    namespace: Dict[str, Any] = {"_dispatch": dispatch, "Optional": Optional}
    exec(src, namespace)  # noqa: S102 - a fixed, self-authored template; no user input is exec'd
    fn = namespace["_tool"]
    fn.__name__ = _sanitize_ident(tool_name)
    return fn


def register_tools(mcp, tool_specs: List[Dict[str, Any]], base_url: str, token: Optional[str]):
    from mcp.types import ToolAnnotations

    seen_names: set = set()
    for spec in tool_specs:
        if spec["name"] in seen_names:
            continue
        seen_names.add(spec["name"])

        async def dispatch(tool_name: str, values: Dict[str, Any], _spec=spec) -> str:
            if _spec.get("graphql"):
                return await call_rest(base_url, token, "POST", _spec["path"], {}, {},
                                        graphql_request(_spec, values))
            path_values, query, body = _split_params(_spec["params"], values)
            return await call_rest(base_url, token, _spec["method"], _spec["path"],
                                    path_values, query, body)

        fn = _build_tool_function(spec["name"], spec["params"], dispatch)
        mcp.add_tool(
            fn,
            name=spec["name"],
            description=spec["description"],
            annotations=ToolAnnotations(readOnlyHint=bool(spec.get("readonly"))),
        )
    return seen_names


def add_health_tool(mcp, base_url: str, token: Optional[str], health_path: str, skip: bool):
    from mcp.types import ToolAnnotations

    if skip:
        return

    async def app_status() -> str:
        """Check whether the app answers on its health endpoint."""
        return await call_rest(base_url, token, "GET", health_path, {}, {}, None)

    mcp.add_tool(app_status, name="app_status",
                 description=f"Check whether the app is reachable (GET {health_path}).",
                 annotations=ToolAnnotations(readOnlyHint=True))


# ── GraphQL ─────────────────────────────────────────────────────────────────
# A GraphQL endpoint is described by introspection, not by an OpenAPI document.
# Every first-level Query field becomes a read-only tool (`query_<field>`); the
# Mutation fields become tools (`mutation_<field>`) only when REST_GRAPHQL_MUTATIONS
# is set, because a write is something a human opts into. The selection set of a
# tool is derived from the schema (the scalar leaves of the returned type, one
# level of nested objects), so the model never has to write GraphQL.

INTROSPECTION_QUERY = (
    "query IntrospectionQuery { __schema { queryType { name } mutationType { name } types { kind name description "
    "fields { name description args { name description defaultValue type { ...T } } type { ...T } } "
    "inputFields { name type { ...T } } enumValues { name } } } } "
    "fragment T on __Type { kind name ofType { kind name ofType { kind name ofType { kind name ofType { kind name "
    "ofType { kind name } } } } } }"
)
_GQL_SELECTION_FIELDS = 40
_GQL_SELECTION_DEPTH = 2


def _gql_unwrap(t: Optional[Dict[str, Any]]):
    """(kind, name, is_list) of the named type under any NON_NULL / LIST wrappers."""
    is_list = False
    while isinstance(t, dict) and t.get("kind") in ("NON_NULL", "LIST"):
        if t["kind"] == "LIST":
            is_list = True
        t = t.get("ofType")
    if not isinstance(t, dict):
        return "SCALAR", "String", is_list
    return t.get("kind") or "SCALAR", t.get("name") or "String", is_list


def _gql_type_str(t: Optional[Dict[str, Any]]) -> str:
    """The GraphQL spelling of a type reference, e.g. ``[ID!]!``."""
    if not isinstance(t, dict):
        return "String"
    kind = t.get("kind")
    if kind == "NON_NULL":
        return _gql_type_str(t.get("ofType")) + "!"
    if kind == "LIST":
        return "[" + _gql_type_str(t.get("ofType")) + "]"
    return t.get("name") or "String"


def _gql_json_type(t: Optional[Dict[str, Any]]) -> str:
    kind, name, is_list = _gql_unwrap(t)
    if is_list:
        return "array"
    if kind == "INPUT_OBJECT":
        return "object"
    return {"Int": "integer", "Float": "number", "Boolean": "boolean"}.get(name, "string")


def _gql_selection(types: Dict[str, Dict[str, Any]], type_name: str, depth: int) -> str:
    """``{ a b { c } }`` for an object type: its scalar/enum fields that take no required argument and, while
    depth allows, one level of nested objects. ``{ __typename }`` when nothing else is selectable."""
    node = types.get(type_name) or {}
    leaves: List[str] = []
    nested: List[str] = []
    for f in node.get("fields") or []:
        if any((a.get("type") or {}).get("kind") == "NON_NULL" and a.get("defaultValue") is None
               for a in f.get("args") or []):
            continue
        kind, name, _ = _gql_unwrap(f.get("type"))
        if kind in ("SCALAR", "ENUM"):
            leaves.append(f["name"])
        elif kind in ("OBJECT", "INTERFACE") and depth > 1 and name != type_name:
            inner = _gql_selection(types, name, depth - 1)
            if inner != "{ __typename }":
                nested.append(f"{f['name']} {inner}")
    picked = (leaves + nested)[:_GQL_SELECTION_FIELDS]
    return "{ " + " ".join(picked) + " }" if picked else "{ __typename }"


def graphql_to_tools(introspection: Dict[str, Any], path: str,
                     include_mutations: bool = False) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Tool specs from an introspection result (``{"data": {"__schema": ...}}`` or the bare ``__schema``)."""
    schema = introspection.get("data", introspection) if isinstance(introspection, dict) else {}
    schema = schema.get("__schema", schema) if isinstance(schema, dict) else {}
    types = {t["name"]: t for t in (schema.get("types") or []) if isinstance(t, dict) and t.get("name")}
    roots = [("query", (schema.get("queryType") or {}).get("name"))]
    if include_mutations:
        roots.append(("mutation", (schema.get("mutationType") or {}).get("name")))
    specs: List[Dict[str, Any]] = []
    notes: List[str] = []
    used: set = set()
    for operation, root_name in roots:
        for f in (types.get(root_name) or {}).get("fields") or []:
            field = str(f.get("name") or "")
            if not field or field.startswith("__"):
                continue
            tool = f"{operation}_{_sanitize_ident(field)}"[:64]
            if tool in used or not _TOOL_NAME_RE.match(tool):
                notes.append(f"graphql {operation} {field!r} skipped: tool name {tool!r} unusable or taken")
                continue
            used.add(tool)
            params: Dict[str, Any] = {}
            used_idents: set = set()
            for a in f.get("args") or []:
                py = _dedupe_ident(_sanitize_ident(a["name"]), used_idents)
                params[py] = {"orig": a["name"], "in": "graphql_var", "type": _gql_json_type(a.get("type")),
                              "required": (a.get("type") or {}).get("kind") == "NON_NULL" and a.get("defaultValue") is None,
                              "description": a.get("description") or "", "gql_type": _gql_type_str(a.get("type"))}
            kind, ret_name, is_list = _gql_unwrap(f.get("type"))
            selection = _gql_selection(types, ret_name, _GQL_SELECTION_DEPTH) if kind in ("OBJECT", "INTERFACE", "UNION") else ""
            sig = ", ".join(f"{p['orig']}: {p['gql_type']}" for p in params.values())
            specs.append({
                "name": tool,
                "description": (f.get("description") or "").strip() or
                               f"GraphQL {operation} {field}({sig}) -> {_gql_type_str(f.get('type'))}",
                "method": "POST", "path": path, "readonly": operation == "query", "params": params,
                "graphql": {"operation": operation, "field": field, "selection": selection},
            })
    return specs, notes


def graphql_request(spec: Dict[str, Any], values: Dict[str, Any]) -> Dict[str, Any]:
    """The JSON body of the POST for one tool call: only the variables the caller gave are declared."""
    gql = spec["graphql"]
    declared: List[str] = []
    passed: List[str] = []
    variables: Dict[str, Any] = {}
    for py, meta in spec["params"].items():
        value = values.get(py)
        if value is None:
            continue
        declared.append(f"${meta['orig']}: {meta['gql_type']}")
        passed.append(f"{meta['orig']}: ${meta['orig']}")
        variables[meta["orig"]] = value
    head = f"{gql['operation']} Faustus" + (f"({', '.join(declared)})" if declared else "")
    call = gql["field"] + (f"({', '.join(passed)})" if passed else "")
    sel = gql.get("selection") or ""
    return {"query": f"{head} {{ {call} {sel} }}".replace("  ", " "), "variables": variables}


def fetch_graphql_schema(base_url: str, token: Optional[str], path: str, timeout: float = 10.0) -> Dict[str, Any]:
    """Run the introspection query against the local endpoint. Raises on a transport error or GraphQL errors."""
    import httpx
    headers = auth_headers(token)
    resp = httpx.post(base_url.rstrip("/") + "/" + path.lstrip("/"), json={"query": INTROSPECTION_QUERY},
                      headers=headers, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict) or not (data.get("data") or {}).get("__schema"):
        raise ValueError("the endpoint did not return a GraphQL schema (introspection may be disabled)")
    return data


# ── assembly ────────────────────────────────────────────────────────────────

def build_server(env: Dict[str, str]):
    """Build (and populate) a FastMCP instance from an environment mapping.
    Raises `ValueError` for a non-loopback REST_BASE_URL — the caller (tests,
    or `main`) decides what to do with that."""
    from mcp.server.fastmcp import FastMCP

    base_url = env.get("REST_BASE_URL", "")
    if not base_url or not is_loopback_base_url(base_url):
        raise ValueError(
            f"REST_BASE_URL must be a loopback address (127.0.0.1/localhost/::1), got {base_url!r}")

    name = env.get("REST_NAME") or "REST bridge"
    token = None
    token_file = env.get("REST_TOKEN_FILE")
    if token_file and os.path.isfile(token_file):
        with open(token_file, "r", encoding="utf-8") as fh:
            token = fh.read().strip() or None
    extra_headers = parse_extra_headers(env.get("REST_HEADERS"))
    if extra_headers:  # a header map replaces the plain bearer token; keep it unless the map sets its own
        token = {**auth_headers(token), **extra_headers}

    all_specs: List[Dict[str, Any]] = []
    all_notes: List[str] = []

    manifest_path = env.get("REST_MANIFEST")
    manifest_instructions = None
    if manifest_path:
        manifest = read_manifest(manifest_path)
        manifest_instructions = manifest.get("instructions")
        specs, notes = manifest_to_tools(manifest)
        all_specs.extend(specs)
        all_notes.extend(notes)

    openapi_url = env.get("REST_OPENAPI_URL")
    if openapi_url:
        try:
            import httpx
            resp = httpx.get(openapi_url, timeout=10.0)
            resp.raise_for_status()
            spec = resp.json()
        except Exception as exc:  # noqa: BLE001 - startup best-effort, never fatal
            print(f"rest_mcp: could not fetch OpenAPI document ({exc}); "
                  f"using the manifest only", file=sys.stderr)
        else:
            specs, notes = openapi_to_tools(spec, env.get("REST_ALLOW"))
            existing_names = {s["name"] for s in all_specs}
            for s in specs:
                if s["name"] in existing_names:
                    continue  # a manifest entry with the same name wins
                all_specs.append(s)
            all_notes.extend(notes)

    graphql_path = env.get("REST_GRAPHQL_PATH")
    if graphql_path or env.get("REST_GRAPHQL_SCHEMA"):
        graphql_path = graphql_path or "/graphql"
        try:
            if env.get("REST_GRAPHQL_SCHEMA"):
                with open(env["REST_GRAPHQL_SCHEMA"], "r", encoding="utf-8") as fh:
                    introspection = json.load(fh)
            else:
                introspection = fetch_graphql_schema(base_url, token, graphql_path)
        except Exception as exc:  # noqa: BLE001 - startup best-effort, never fatal
            print(f"rest_mcp: could not read the GraphQL schema ({exc}); skipping GraphQL tools", file=sys.stderr)
        else:
            specs, notes = graphql_to_tools(introspection, graphql_path,
                                            env.get("REST_GRAPHQL_MUTATIONS", "").lower() in ("1", "true", "yes"))
            existing_names = {s["name"] for s in all_specs}
            all_specs.extend(s for s in specs if s["name"] not in existing_names)
            all_notes.extend(notes)

    instructions = manifest_instructions or f"REST bridge for {name}."
    if all_notes:
        instructions += "\n\nNotes:\n" + "\n".join(f"- {n}" for n in all_notes)

    mcp = FastMCP(name, instructions=instructions, warn_on_duplicate_tools=False)
    registered = register_tools(mcp, all_specs, base_url, token)
    add_health_tool(mcp, base_url, token, env.get("REST_HEALTH_PATH") or DEFAULT_HEALTH_PATH,
                     skip="app_status" in registered)
    return mcp


def main() -> None:
    env = dict(os.environ)
    try:
        mcp = build_server(env)
    except ValueError as exc:
        print(f"rest_mcp: {exc}", file=sys.stderr)
        sys.exit(2)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"rest_mcp: could not read REST_MANIFEST: {exc}", file=sys.stderr)
        sys.exit(2)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
