# Daily paid spend cap

A hard cap on one day's spend with PAID remote providers, on by default. It
bounds the day across every turn, task, sub-agent, advisor and utility call; the
per-turn ceilings (`agent_turn_max_cost_usd`, the autonomy budget) bound only one
turn.

Code: `src/spend_cap.py`. The single choke point is `src/llm_core.py`:
`llm_call`, `llm_call_async` and `stream_llm` call `spend_cap.gate(url, model)`
immediately before EVERY billable HTTP attempt (the first send, each retry, each
fallback candidate, the empty-completion re-send) and `spend_cap.record(...)` with
the provider-reported usage as soon as a response is in.

## Rules

- "Paid" means the provider/model is actually billed per call; a remote URL alone
  is not proof. `spend_cap.classify(url, model)` decides, in order: empty or
  `faustus-cli://` (flat-fee CLI) is not paid; a local endpoint (configured
  `endpoint_kind` `local`, or loopback/private/tailscale that is not declared
  `api`/`proxy`: llama-server, Ollama, Sparks) is not paid; a flat-rate
  subscription provider (ChatGPT subscription, GitHub Copilot) is not paid; a
  model id ending in `:free` (the OpenRouter convention) is not paid; everything
  else that is remote (`endpoint_kind` `api`/`proxy`, or a public host) is paid.
  Not-paid calls are never counted or refused.
- Faustus has no price table, so dollars are only the provider's reported
  `cost_usd`. A response that reports `cost_usd: 0` is free by the provider's own
  statement: it adds $0 and its tokens do NOT count toward the token limit, so it
  is never blocked. A paid call that reports no cost has an unknown price: it
  counts its tokens (the token limit is the protection for exactly this case) and
  is tallied as `unknown_cost_calls`; a price is never guessed. If the dollar
  limit is `0` (off), the token limit covers every paid token.
- The gate runs after the response cache: a cache hit is not a new paid request
  and is neither refused nor counted. A refused retry never goes out; an attempt
  already accepted is never cut.
- Usage is booked per HTTP attempt, also when the response fails (a 503 that
  carries a valid `usage` receipt is booked; a failure without a receipt books
  nothing). In a stream, repeated or cumulative `usage` frames of one response
  overwrite each other instead of adding up; a genuinely new attempt (retry) is
  booked separately.
- The accumulator is safe across processes sharing `DATA_DIR`: every
  load-modify-save holds an exclusive lock file (`spend_cap.json.lock`,
  `msvcrt.locking` on Windows, `fcntl.flock` elsewhere).
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
today's usage. The card refetches after every settings save, on focus and every
15 s, so it never shows an old limit next to the control.

Only a person can change them. `PATCH`/`POST /api/spend-cap` use `require_human`;
`POST /api/auth/settings` refuses a change to these keys (403) when it arrives
with the internal tool token; and the native `manage_settings` tool refuses
`set`, `reset` and `delete` on them. Nothing else is blocked globally.

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
direct provider probes in routes) are neither counted nor refused. A streamed
attempt that fails with an error body is not parsed for a usage receipt. The cap
is a check before each send, not a reservation: concurrent calls accepted before
the limit is reached can overshoot it by their own cost. A backup restore or an
edit of `settings.json` by a person (or a shell tool) can still change the
settings.
