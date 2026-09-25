# Reasoning mode per turn (Auto / Fast / Think / Deep)

Spanish UI: Auto / Rápido / Pensar / A fondo. Code: `src/think_mode.py`, wired in
`routes/chat_routes.py` (`_resolve_think_mode`) and `src/agent_loop.py` (watchdog).

## Modes and what they send

| Mode  | `gen_overrides`                                                                 |
|-------|---------------------------------------------------------------------------------|
| fast  | `think: false`                                                                  |
| think | `think: true`, `reasoning_budget` = `think_mode_budget_think` (4096)            |
| deep  | `think: true`, `reasoning_budget` = `think_mode_budget_deep` (16384), `reasoning_effort: "high"` |
| auto  | whatever `think_mode.decide()` picks for the message                            |

`reasoning_budget` only reaches llama-server and other self-hosted OpenAI-compatible
engines (next to `chat_template_kwargs.enable_thinking`). Ollama's native `/api/chat`
has no budget field: there `deep` is thinking on, the same as `think`.
`reasoning_effort` is only sent to a local endpoint or Mistral; a strict remote
provider does not get the field.

A model without a thinking mode (`_supports_thinking` false) is left alone: no
change, no event.

## Precedence

1. An explicit mode in the `think_mode` form field (`fast`/`think`/`deep`).
2. An explicit `think` in `gen_overrides` (`/think on|off`, the Generation switch).
3. The Auto rule (`think_mode` = `auto`, or the field absent and
   `think_mode_default` = `auto`).
4. Existing defaults (thinking models off; local agent coding turns on).

In Auto, an agent turn on a coding task (workspace bound, or a coding request) never
drops below `think` unless the message is plain small talk, so the agent's
"local coding turns think by default" is never made worse. A bare go-ahead ("sí",
"adelante") in a running task counts as `think`, not small talk.

A budget the client pinned itself in `gen_overrides.reasoning_budget` still wins over
the Auto rule's budget.

## The Auto rule

Deterministic, ES + EN, never raises. In order:

- `deep`: an explicit ask for depth ("a fondo", "en profundidad", "exhaustivo",
  "paso a paso", "demuestra", "think hard", "step by step", "prove", "thorough"…);
  a very long brief (250+ words) with work in it; 120+ words with 3+ list items;
  3+ attachments plus analysis or comparison.
- `fast`: an explicit ask for speed ("rápido:", "responde rápido", "en una palabra",
  "sí o no", "quick question", "tl;dr"…) — phrase-shaped, so "el algoritmo más
  rápido" is not one; small talk (`src/turn_effort.is_small_talk`).
- `think`: code, debugging, implementation, maths, planning, comparisons, analysis,
  or several stacked constraints; attachments; a coding turn.
- otherwise `fast` for a short lookup or short turn, `think` for a long one.

The result carries `reasons` (the signal families that matched) and `confidence`.

## Thinking watchdog

When the think flag comes from the reasoning mode (`source` `rule` or `explicit`),
it is not a user pin: the local runaway watchdog (`agent_local_think_budget_seconds`)
stays on. For `deep` it is multiplied by `think_mode_deep_watchdog_factor` (2.0).
`/think on` keeps disabling the watchdog as before.

## SSE

`think_mode` (catalogued in `docs/api/sse_events.json`), once at the top of the
stream: `{mode, requested, source, reasons, budget}` plus `why`/`confidence` for
the rule. The Studio chip shows "Auto · Pensar" from it.

## Settings

| Key                               | Default |
|-----------------------------------|---------|
| `think_mode_default`              | `auto`  |
| `think_mode_budget_think`         | 4096    |
| `think_mode_budget_deep`          | 16384   |
| `think_mode_deep_watchdog_factor` | 2.0     |

## Studio

A compact chip in the composer (only for a thinking model) picks the mode per chat,
kept in localStorage (`faustus_studio_think_mode_<session>`); a chat with no pick
uses `think_mode_default`. `/think auto|fast|think|deep` (also rápido / pensar /
a fondo) does the same; `/think on|off` still pins the Generation switch.
Generation settings and the mode picked in a brand-new chat carry over to the
session id the first send creates (before, they were reset after the first turn).
