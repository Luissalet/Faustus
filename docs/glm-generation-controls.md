# GLM generation controls

For a GLM-5.3-Flash endpoint whose vLLM schema exposes native thinking budgets,
Faustus normalizes generation controls after applying the generic fast switch.
Fast mode requests Low reasoning effort and keeps the thinking template enabled;
it does not force an empty reasoning block. Explicit high/max reasoning choices
remain available when thinking is enabled.

`reasoning_budget` is translated to `thinking_token_budget` without inventing a
cap. Explicit `0`, `-1` (unlimited) and positive values are preserved. An existing
native budget survives when no generic override was supplied. The adapter removes
llama-server's spelling before sending the request. Other models retain their
existing fast-mode behavior.

The streaming path still honors the existing
`local_openai_reasoning_budget_default` setting, whose fallback is 4096, when
thinking is enabled and no per-request budget was supplied. Setting that default
to zero continues to omit it. Tool-less asynchronous helpers do not acquire this
streaming default merely by passing through the GLM adapter.

Reasoning effort is sent at the top level only. An explicitly supplied template
effort is used as a fallback if the top-level field is absent, then removed from
template kwargs to avoid stale values during retries. A reasoning-related HTTP
400 can step max down to high; the final generic fallback removes native budget
fields as well as effort. `clear_thinking` defaults to true, preserving an
explicit false choice.

Tests exercise both public async and streaming calls through HTTP MockTransport,
including 0/-1/128, fast/high, native settings and reasoning/transport retries.
The isolated MCP proof uses the production `hoard_turn_start` stdio facade, an API
stub, real llm_core and a mocked model HTTP transport. This verifies those seams,
not the complete chat route, agent harness or model quality. No real model
inference, global Faustus instance or GPU is required for these checks.

Sampler correctness and persistent engine state are separate concerns. A valid
control payload does not certify semantic output or automatic recovery.
