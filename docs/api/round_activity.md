# Per round model activity

The chat activity disclosure combines model round events, persisted usage buckets, worker events, advisor receipts, and tool steps. It shows only fields reported by the backend or measured by the client.

`round_info` may include `model`, `input_tokens`, `output_tokens`, `cached_tokens`, `engine_timings`, `request_duration_ms`, `request_duration_source`, `finish_reason`, and `usage_source`. Engine fields such as `prompt_ms`, `predicted_ms`, and `cache_n` remain absent when the runtime did not report them. `request_duration_ms` is the observed client stream duration and is shown separately from engine phases.

Subagent `round_info` is retained as `round_activity` in the worker report and forwarded in the worker's round event. At most 100 per-worker rounds are retained. Older records without this field continue to show their aggregate model, status, rounds, tokens, and tools.

The interface never infers a GPU or node from a model name or actor. Device attribution is only meaningful when a runtime event supplies it; the current round schema does not claim that GPUs are shared or identify a remote node.

The activity view is observational. It does not add completion gates, approval steps, or model configuration changes.
