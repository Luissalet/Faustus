# Complete-response timeouts

`llm_call_async` receives the complete non-streaming response. It cannot measure
time to first token. The Sparks initial-response setting and request override
therefore do not impose an additional deadline on this complete response.
The caller's existing transport timeout, retry policy, cancellation and outer
turn deadline continue to apply. Defaults and request payloads are unchanged.
Usage, actual-model metadata and call traces still come from the same response.

For example, an initial-response budget of 300 seconds must not cancel a
421-second complete response when the caller allowed 900 seconds. A caller that
allows only 300 seconds still gets its existing transport timeout. HTTP read
timeouts bound inactivity while reading; they do not become a new total wall
deadline. An outer caller or turn deadline remains independent.

Use the normal streaming path when the caller needs progress and an observable
first content, reasoning or tool event. Its initial-response deadline remains
in place. This change neither converts helper callers to streaming nor changes
model recovery behavior.

The legacy plain-chat route still bounds context preparation separately before
the model call. Streaming admission and context preparation can occur before
the SSE response opens; those stages do not gain heartbeats from this change.
Their route behavior is outside this correction.

The regression uses a real loopback HTTP server and scales 421/300/900 timing by
1:1000 at the transport clock only. It checks complete-response timeout,
cancellation, retries with identical request bytes, usage and model metadata.
It performs no model inference or lifecycle operation.
