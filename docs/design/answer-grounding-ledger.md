# Answer grounding ledger

`src/grounding_ledger.py` labels every significant figure of a tool-backed final answer as
`observed`, `cited`, `derived` (with its formula), `count` or `unsupported`. It is off by default
(setting `agent_answer_grounding_ledger`, Settings > Agent loop).

## Behaviour

1. The ledger is skipped when the turn used no tool.
2. With unsupported figures and no rewrite used yet: one correction request through the existing
   rewrite message (`answer_checks.rewrite_note(..., grounding=note)`).
3. When the correction was already used, or did not help: each unsupported figure is struck through
   (`~~12 %~~`) and a short note is appended (Spanish or English, following the answer).
4. The ledger is returned as a compact trace entry (`trace_entry`) to record with the turn.

## Wiring in the agent loop

Both call sites below are wired in `src/agent_loop.py` (the answer-check block and the step right
after it); `tests/test_grounding_ledger_loop.py` drives a whole turn through them.

`answer_checks.grounding_review(answer, question, tool_outputs, retry_used=..., lang=...)` returns
`{"action": "none" | "retry" | "mark", "note", "answer", "ledger", "trace"}` and is always `none`
while the setting is off. The caller needs two places:

* where the harness decides on the single answer rewrite: include `review["action"] == "retry"` in
  the condition, pass `grounding=review["note"]` to `rewrite_note`, and add a `"ungrounded_figure"`
  reason to the `harness_check` event;
* where the final answer is settled: when the answer was already rewritten (or the rewrite was not
  available), call `grounding_review(..., retry_used=True)` and, when `action == "mark"`, replace the
  answer with `review["answer"]` and emit `response_replace`.

In both places append `review["trace"]` to the turn ledger notes (for example as compact JSON) so the
ledger is recorded with the trace.

## Limits

* At most 30 distinct evidence values feed derivations (sums of two or three, differences, products,
  ratios, percentages, percentage changes, the total and the mean). A coincidental derived match is
  possible, so the formula is always kept in the entry.
* At most 60 figures are listed and evidence is read up to 200 000 characters.
* Small bare integers, years, dates, times, versions, list markers, code and URLs are not figures.
