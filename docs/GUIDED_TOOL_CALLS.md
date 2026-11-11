# Constrained native tool calls

The normal agent stream adds `function.strict=true` to function tools on managed Sparks endpoints whose served chat OpenAPI declares a boolean `function.strict`. The server compiles each original parameters schema into its native tool grammar. `tool_choice` retains automatic selection, allowing the model to answer directly, return a batch, and finish after receiving tool results. Existing self-hosted `parallel_tool_calls=true`, streaming call assembly and resource-keyed dispatch handle the batch.

The capability is discovered by the existing Sparks metadata refresh and returned as `schema_capabilities[endpoint_id].strict_tools` by the sync API and `sparks_sync` MCP tool. Generation reads the endpoint/base-keyed cache. The cache expires after 60 seconds and follows endpoint retirement, URL changes and model-list changes. Unverified metadata uses the existing generation path. `local_structured_output=off` selects the existing behavior.

Only request-local function wrappers are copied. Parameter schemas retain their optional fields, required lists, references, enums and additional-property rules. An explicit per-function strict value takes precedence. The existing argument validation, truncated-output handling, tool receipts and dispatch resource claims continue to apply.

Strict native tools and tool-less JSON response schemas use their respective decoding paths. The current server implements strict automatic tool selection using structural grammar. Completed calls must match the declared schema. Tool results and source evidence establish the meaning and correctness of their values. [Server implementation](https://github.com/sgl-project/sglang/blob/main/python/sglang/srt/function_call/function_call_parser.py).

## Measurement on 12 November 2026

Local log inspection counted 20 JSON argument parse errors, 12,004 logged native calls and 1,509 multi-call round summaries. November logs contained 718 native calls, 56 multi-call rounds and zero recorded JSON parse errors. These are log occurrences across historical runs. Model-specific error rates require model identity and complete call coverage.

The live server advertised `deepseek-v4.1-flash`, parser `deepseekv41` and grammar backend `xgrammar`. Its current model list contained one model. GLM evaluation awaits a served GLM endpoint.

Two live request batteries used the same server, tool schemas and inputs.

| Battery | Current requests | Strict requests |
| --- | --- | --- |
| Ordinary and conflicting user requests, 24 requests per arm | 24 complete calls, 0 invalid | 24 complete calls, 0 invalid |
| Deliberately conflicting literal-data instructions, 18 paired requests per arm | 18 complete calls, 12 invalid | 18 complete calls, 0 invalid |

The stress battery alternated arm order with a fixed random seed and repeated six cases three times. The invalid baseline calls violated the numeric minimum, enum or string pattern. This measures robustness under conflicting instructions. Both ordinary arms returned independent batches of three and two calls and answered greetings directly.

An isolated normal Faustus turn started through stdio MCP returned three `read_file` calls in one inference. Resource-keyed dispatch ran all three reads in parallel with successful results. The second inference produced the exact values `alpha=17`, `beta=29` and `gamma=43`. Both real requests carried 24 strict tools and `parallel_tool_calls=true`. Studio displayed two model rounds, three tools and a completed answer. The screenshot was opened and inspected.

Reproducible probes, complete responses, payloads, MCP receipts and screenshots are retained in `E:/Development/IA/Research/faustus-gramatica2784`. Focused regression tests cover metadata types and references, expiry and retirement, changed models, explicit choices, optional arguments and split streaming batches.
