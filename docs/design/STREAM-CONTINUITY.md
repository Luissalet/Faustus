# Response continuity after transport interruption

Normal chat and agent turns continue an unfinished response after an HTTP
stream ends early, resets or times out after partial output. They keep the
visible prefix and append the remaining answer in the same detached run.
Native copilot MCP calls use that same route and execution identity.

An OpenAI-compatible socket EOF without either `[DONE]` or a provider finish
reason is `transport.incomplete_stream`, rather than a completed answer.
Unfinished native tool calls are not dispatched. Completed tool results from
earlier agent rounds stay in the transcript, with their returned identifiers.
The continuation asks for the remaining text and keeps the original request,
language and output format. It does not change reasoning preferences.

Chat continues while the interrupted attempt added visible text. If another
attempt adds nothing, its real failure is surfaced; an earlier prefix does
not count as progress in the current attempt. Transport recovery adds no
counter limit, approval or setting. Agent recovery uses its existing round
budget and does not consume the separate token-length continuation allowance.
Stop still cancels execution. Compare panes retain their single-generation
behavior and disconnect cancellation.

Validation uses synthetic providers and private storage. Tests cover clean EOF,
reset, timeout, four successive interruptions, preserved prior tool results,
single persistence, request identity, Stop and definite failures. The normal
HTTP route, native stdio `hoard_turn_start`, browser and reconnect path were
also exercised with an actual HTTP provider fixture. These checks prove the
transport behavior; they do not benchmark autonomous model quality.

Group participant turns benefit because the Group UI uses normal chat mode.
Scheduling the next participant remains browser-driven; a durable group
coordinator is tracked separately in Agora task 443.
