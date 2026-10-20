# Stateless global model inference

The family can use Faustus's exact configured global OpenAI-compatible model
without creating a conversation, running an agent or receiving provider keys.
The router is included in the existing model router; no app startup change is
required. Browser sessions follow their existing authentication. Bearer API
tokens require the existing `chat` scope and a real token owner. An endpoint
must be visible to that owner (or their administrator); shared endpoints remain
visible. There is no fallback to a different endpoint or model.

## Descriptor

`GET /api/global-model` returns:

```json
{
  "endpoint_id": "configured-endpoint-id",
  "model": "configured-model-id",
  "connection_revision": "server-owned-connection-revision",
  "chat_path": "/api/global-model/v1/chat/completions",
  "protocol": "openai",
  "vision": true
}
```

This reads `default_endpoint_id` and `default_model`, checks the selected row
and its ownership/enabled/hidden state. For vision, an existing durable supported
or unsupported observation wins only when vendor, endpoint, connection revision,
protocol and model match exactly. Unknown, malformed, legacy and mismatched
evidence retain the existing cache/name fallback. Reading the descriptor does
not run a capability probe. It does not return a backend URL, credentials or
headers. It does not claim the provider is online. It never chooses a first
available endpoint, another vision model, a hidden-model replacement or a
model adopted by a sibling instance. An unconfigured/disabled/hidden selection
returns a structured 503; an unavailable owner-visible row returns 404.

Read this descriptor once when a native job starts. Reuse the same three pin
fields for every call in that job. After a configuration change, a new job can
read a new descriptor; an old job fails explicitly instead of switching models.

## Chat completions

`POST /api/global-model/v1/chat/completions` takes the ordinary OpenAI payload
plus these **required** fields:

```json
{
  "expected_endpoint_id": "configured-endpoint-id",
  "expected_model": "configured-model-id",
  "expected_connection_revision": "server-owned-connection-revision",
  "model": "configured-model-id",
  "messages": [{"role": "user", "content": "Example"}],
  "stream": false
}
```

`model` can be omitted; if present it must match the pin. The three expected
fields are removed before forwarding. All other OpenAI payload fields,
including multimodal message blocks, JSON schema/response_format, tool
definitions, reasoning and template options, pass through unchanged. Caller
URL/base URL/header/API-key overrides are rejected; only the configured row
can select the destination.

Faustus resolves provider credentials internally with
`resolve_endpoint_runtime` and `build_headers`, then rechecks both the global
selection and private connection snapshot immediately before sending. A stale
endpoint/model/connection pin or a credential-resolution conflict returns
409 with `detail.error=global_model_changed`, with no inference dispatched.
Configuration changes after dispatch cannot redirect that already-open call.
Secrets are neither returned in descriptors nor included in generated error
messages. Cookie/API-token headers from the caller are not forwarded to the
provider.

This is an **OpenAI chat-completions** transport. Configured native Anthropic,
Ollama-chat, Responses-only and CLI destinations are not translated through a
different protocol; their non-chat-completions URL returns 503
`global_model_protocol_unsupported`. The router never loads, downloads,
unloads or swaps a model. It does not follow redirects or retry generation,
including a 400/422 rejecting the JSON schema. It preserves the provider's
HTTP status/body and content type, Retry-After and request-ID headers; upstream
cookies, redirects' Location and unrelated transport headers are not relayed.

For `stream=true`, SSE bytes are forwarded incrementally, preserving the
provider's own usage, finish reason, errors and `[DONE]` frames. The proxy
does not synthesize a completion or reinterpret incomplete generation as
success. Consumers must validate completion and structured results. A caller
disconnect or stream cancellation closes the provider response and client;
disconnects while waiting for headers or a non-stream body also cancel and
join that operation. The internal 499 response denotes an already-disconnected
caller. Connection establishment has a 15-second timeout; there is no new
read-duration cap on a long generation. Transport failures before response
are structured 502/504 without exception text or secret-bearing URLs.
Closing this request's HTTP connection does not certify that a provider has
stopped its remote generation. It never invokes another run's cancellation,
an API to stop the global service or an automatic replay after disconnect.

No session/history, prompt persistence, fallback, capability probing, free URL
proxy, automatic retries or model process management is added. Provider auth
refresh follows the existing credential resolver; no new credential store is
created. Only configured LLM endpoints use the existing private-CA TLS helper.

## Verification and delivery

Success criteria: pin/config changes dispatch zero requests; authenticated
synthetic provider receives only its own key and the unchanged OpenAI payload;
schema errors dispatch exactly once; SSE is byte exact; retained upstream is
closed before cancellation returns; token scopes/owner visibility are honored;
no Main, product DB, provider, Hub, model or installed app is accessed by tests.

```powershell
.venv/Scripts/python.exe -m unittest discover -s tests -p test_global_model_routes.py -v
.venv/Scripts/python.exe -m pytest --noconftest -q tests/test_tls_overrides_scope.py
```

Task938 (Fisiones) implementation on isolated public base
`dbe2569527c4553646d7e91f49b607124fcee0ff`: 16 focused inert tests pass in0.799s;
four existing TLS scope tests pass in19.89s. The latter emits three warnings
for pytest config options whose optional plugins are intentionally absent in
the minimal own venv. The first test run had13 passes/one test-fixture import
error: importing `core.authz` through `core/__init__` pulled in bcrypt outside
the minimal runtime. The fixture now loads the real pure authz module directly;
no implementation change or permission relaxation was used to fix it.

Dependencies used in that venv: FastAPI0.146.0, Starlette1.9.0, httpx0.28.1,
AnyIO4.15.1, Pydantic2.14.1, Python3.13. Tests use a mock HTTP transport and
synthetic endpoint/credential rows, with real network connects prohibited.
They do not certify installed Main, live provider auth, native binaries,
visual/MCP integration or a deployed route. Independent CAS review and the
root owner's controlled integration validation remain required. No live API
call, app restart, model request, integration or publication was performed.
