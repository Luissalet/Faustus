# Automatic sampling and speculative decoding

Faustus adds a conservative `min_p` default to local OpenAI-compatible servers.
Some speculative decoders reject a positive `min_p` with the explicit message
“The min_p and logit_bias sampling parameters are not yet supported with speculative decoding.”

For that confirmed rejection, Faustus makes one compatibility request with
`min_p=0`. The correction applies only when Faustus injected that value itself;
a value supplied by the caller, saved model options, or `extra` remains explicit
and its rejection is returned. A supplied `logit_bias` also prevents negotiation.
The model, messages, output limit, reasoning effort and other settings stay the same.
The change is logged. It does not change the saved sampler defaults or device settings.
Explicit sampler overrides also reach configured servers on the LAN. Streaming
closes the rejected response before retrying, and an independent retry flag
prevents repeated negotiation even if an override is lost.

Both synchronous and asynchronous completion helpers handle HTTP 400 this way.
Streaming handles HTTP 400 and an SSE error with status 400 before any content,
reasoning or tool call has arrived. Partial output is never replayed. Other errors,
hosted endpoints and repeated rejections keep their ordinary error handling.

Existing API and MCP model calls use the same transport. There are no special
model names, Spark addresses or benchmark-only adapters in this behavior.
