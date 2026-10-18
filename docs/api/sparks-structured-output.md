# Structured output on managed Sparks

Callers already passing `response_schema` to `llm_call` or `llm_call_async`
can now send the existing strict OpenAI `response_format` envelope to a
managed Sparks endpoint whose served OpenAPI declares the supported request
contract. This includes the existing extraction and workflow model-call paths.
The endpoint must still be enabled/configured and published by native Sparks
sync. Capability is independent of host address and model name.

`src.sparks.sync()` refreshes this capability as part of its existing metadata
work. Startup sync and the existing 20-second sync loop, explicit sync, and
Sparks preferences all use that path. Verification reads `/openapi.json`;
it never generates a prompt. The typed chat request's response-format field,
local references and nullable alternatives must admit a string selector,
name, arbitrary schema object and boolean strict flag. Unknown or incompatible
constructions remain unverified. This is a conservative protocol check,
not a general OpenAPI validator or proof of actual grammar execution.

The capability cache is keyed by configured endpoint identity and normalized
API base, bounded to 128 entries, and expires after 60 seconds. Positive and
negative checks share the TTL. Changed model lists and URLs invalidate old
entries before refresh, and retired identities are removed. Generation only
reads this cache; this feature adds no OpenAPI request per completion.
An endpoint may therefore use the existing tolerant parsing path before its
first metadata sync, after expiry, or when metadata is unavailable.

`local_structured_output=off`, hosted/unmanaged providers, unsupported
endpoints and calls carrying tools keep their existing behavior. Native
Ollama and managed llama.cpp retain their fields and policies. llama.cpp's
schema thinking suppression remains local to that backend; Sparks keeps the
caller's High/Max effort, clear-thinking setting and explicit budgets,
including 0, -1 and 128. The normal generation adapter still validates and
translates those controls. No new default reasoning budget is introduced.

After the format is attached, an upstream HTTP rejection remains a failure;
this change does not silently retry it without the schema. Response caches,
usage accounting, returned model metadata and tracing remain in their normal
paths. The separate constrained-choice backend gate is unchanged.

A schema controls shape, not arithmetic, program correctness, grounding or
model stability. Existing extraction validation and evidence checks still
run, including rejecting a number whose quote is absent from the document.

Validation uses real loopback HTTP with fixed synthetic replies, native
endpoint/settings/SQLite creation, the existing extraction REST route,
production workflow model calls, real stdio REST MCP and a browser click.
Authentication/session ownership is synthetic in that private QA surface.
No real model or grammar ran in these CPU checks; live Sparks validation is
separate. Regression tests also cover Ollama/llama.cpp, malformed metadata,
cache invalidation, settings-off/tools exclusion, reasoning, usage and traces.
