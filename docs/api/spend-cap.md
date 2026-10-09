# Daily paid spend cap

A hard cap on one day's spend with PAID remote providers, on by default. It
bounds the day across every turn, task, sub-agent, advisor and utility call; the
per-turn ceilings (`agent_turn_max_cost_usd`, the autonomy budget) bound only one
turn.

Code: `src/spend_cap.py`. The single choke point is `src/llm_core.py`:
`llm_call`, `llm_call_async` and `stream_llm` call `spend_cap.gate(url)` before
sending and `spend_cap.record(url, usage)` with the provider-reported usage.

## Rules

- A call is "paid" when its endpoint is not local: not a loopback, private or
  tailscale address, not a configured `local` endpoint (llama-server, Ollama,
  Sparks), not a `faustus-cli://` client. Local models are never counted or
  refused.
- Dollars come from the provider's real `cost_usd`. A paid call without one
  counts its tokens only and is tallied as `unknown_cost_calls`; a price is never
  guessed. The token limit therefore still stops providers that report no price.
- The account is keyed by the LOCAL calendar date and persisted in
  `DATA_DIR/spend_cap.json`; a new date starts from zero.
- Once the day's USD or tokens reach the limit, the next paid call is refused
  before it is sent: `SpendCapReached` (HTTP 429, `error_class`
  `spend_cap.reached`), or for a stream an `event: error` chunk with the same
  fields. Calls already in flight finish and are booked.
- One notification (`turn_error`, `data.kind = "spend_cap"`) is sent per day.

## Settings

| Key | Default | Meaning |
|---|---|---|
| `paid_spend_cap_enabled` | `true` | Master switch. |
| `paid_spend_cap_daily_usd` | `5.0` | Dollar limit; `0` = none. |
| `paid_spend_cap_daily_tokens` | `2000000` | Token limit; `0` = none. |

They appear in Settings > Agent as "Daily paid spend cap", with a card showing
today's usage.

## API

`GET /api/spend-cap` (admin)

```json
{"enabled": true, "limit_usd": 5.0, "limit_tokens": 2000000, "day": "2026-10-09",
 "spent_usd": 0.42, "tokens_today": 81234, "paid_calls": 17,
 "unknown_cost_calls": 3, "reached": false, "reached_by": null,
 "resets_at": "2026-10-10T00:00:00+02:00",
 "by_provider": [{"provider": "openrouter", "usd": 0.42, "tokens": 81234,
                  "calls": 17, "unknown_cost_calls": 3}]}
```

`PATCH /api/spend-cap` (also `POST`, human only: an agent token cannot raise or
disable its own cap) takes any subset of `{enabled, daily_usd, daily_tokens}`,
writes the settings above and returns the new status.

## Error

```json
{"error": "Daily paid-provider spend cap reached ($5.0021 of the $5.00 daily limit). ...",
 "status": 429, "error_class": "spend_cap.reached", "retryable": false,
 "limit_kind": "usd", "spent_usd": 5.0021, "limit_usd": 5.0,
 "spent_tokens": 912000, "limit_tokens": 2000000,
 "resets_at": "2026-10-10T00:00:00+02:00"}
```

## MCP

The built-in `harness` server exposes the read-only tool `spend_cap_status`
(same body as `GET /api/spend-cap`).

## Not covered

Paid calls that bypass `src/llm_core.py` (image/video generation APIs, a few
direct provider probes in routes) are neither counted nor refused.
