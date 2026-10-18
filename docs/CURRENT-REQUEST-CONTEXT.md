# Current request and the input budget

For a known self-hosted OpenAI-compatible serving window, the agent preserves
the latest human request and the subsequent tool/runtime messages when they
fit together with the resident instructions, protected context, candidate tool
schemas and resolved output allocation. It drops older history first. AUTO
still targets the configured amount of ordinary history; its default remains
6000 with a 200000-token history ceiling. A large current request can use the
remaining serving window without changing those settings.

The final candidate preparation uses the actual offered schemas, after schema
slimming and context rebuilding. Candidate fallback is checked independently;
a request that cannot fit a smaller candidate can still use a larger configured
candidate. Explicit input caps are never widened. Unknown windows and other
provider protocols retain their previous policy.

Output reservation uses the stream builder's existing output-field resolver.
An omitted output argument can resolve to the configured native fallback; zero
and negative values omit the output cap and are recorded as an unknown output
reserve, rather than an invented allocation. Message and schema token counts
are estimates. This preparation does not certify tokenizer accuracy or semantic
quality at any context size.

If the intact request cannot fit a known window or explicit cap after the
estimated reserves, the stream reports a context capacity error before sending
that candidate. It does not dispatch a shortened version or compact and replay
the same impossible preparation. Ordinary provider errors keep their existing
handling and do not expose source content in the preparation error message.

The opt-in `preserve_current_request` option on `trim_for_context` is used only
for this supported path. The current human anchor skips injected runtime,
harness and untrusted tool-result user messages; the subsequent messages remain
in their original order.

## Verification

`tests/test_current_request_context.py` covers long and small requests,
explicit caps, insufficient/unknown windows, resident instructions, injected
messages, independent fallback windows and output fields over private HTTP.
The existing budget, compactor, routing, interleaved history and native sampling
tests cover compatibility. Native API, MCP and Studio checks require the
separate long-message transport change (task 866) composed in a private worktree.
Private CPU fixtures establish transport and request preservation, not model
quality or activation of the production runtime.
