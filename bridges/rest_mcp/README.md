# bridges/rest_mcp — generic REST → MCP stdio adapter

Turns a local HTTP API into MCP tools without any app-specific code. Faustus
spawns `server.py` as a stdio MCP server (`src/connectors.py`'s `gepetto`
and `platos` presets are the two current users), the same way it spawns any
other stdio MCP server.

## Configuration (environment variables)

| Variable            | Required | Meaning |
|----------------------|----------|---------|
| `REST_BASE_URL`      | yes | The app's base URL. Loopback only (`127.0.0.0/8`, `::1`, `localhost`) — anything else refuses to start. |
| `REST_OPENAPI_URL`   | no  | An OpenAPI 3.x document fetched at startup; one tool per operation. |
| `REST_MANIFEST`      | no  | Path to a JSON manifest — the fallback/explicit tool listing (see below). |
| `REST_ALLOW`         | no  | Regex over `"METHOD /path"` filtering which **OpenAPI** operations become tools. Default `^GET ` (read-only only). Manifest tools are always included. |
| `REST_TOKEN_FILE`    | no  | Path to a bearer token, sent as `Authorization: Bearer <token>` on every request. |
| `REST_NAME`          | no  | Display name for the MCP server. |
| `REST_HEALTH_PATH`   | no  | Path for the always-present `app_status` tool. Default `/api/health`. |

## Manifest shape

```json
{
  "name": "Some App",
  "instructions": "Shown to the model as the server's instructions.",
  "tools": [
    {
      "name": "get_thing",
      "description": "One sentence.",
      "method": "GET",
      "path": "/api/things/{id}",
      "readonly": true,
      "params": {
        "id": {"in": "path", "type": "string", "required": true, "description": "Thing id."}
      }
    }
  ]
}
```

`params[name].in` is `"path"`, `"query"`, or `"body"` (the whole JSON body as
one object argument). Tool names must match `[a-zA-Z0-9_-]{1,64}`.

## OpenAPI operations → tools

One tool per operation. Path and query parameters become tool arguments
(path params always required). A JSON request body becomes a single `body`
object argument, or is flattened into top-level arguments when its schema is
a plain object with at most 12 properties. A multipart or otherwise
non-JSON body is skipped — its operation is listed as a note in the server's
instructions instead, never silently dropped. `readOnlyHint` is set on every
`GET` tool.

## Responses

Bodies are pretty-printed JSON, truncated at 20 kB with a trailing note. A
binary body (by `Content-Type`) is never returned as bytes — only a note
naming its content-type and size.

## `app_status`

Always present: `GET REST_HEALTH_PATH` against `REST_BASE_URL`. Lets an
agent check the underlying app is actually up without guessing which other
tool is safe to call first.
