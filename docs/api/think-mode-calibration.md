# Auto reasoning calibration

`GET /api/diagnostics/think-mode?days=30` · MCP `think_mode_calibration` (server `harness`)

Read-only report of how the Auto reasoning mode's choices held up. Every reply records the
mode its turn ran with in `metadata.think_mode` (`mode`, `requested`, `source`, `reasons`,
`budget`, `effort`). The report pairs each recorded turn with the next user message in the
same session.

## Parameters

| Name | Default | Notes |
|------|---------|-------|
| `days` | 30 | Look-back window, clamped to 1–365. |

Only the caller's own sessions are read. The MCP tool uses `ODYSSEUS_MCP_HARNESS_OWNER`
(or `ODYSSEUS_MCP_CONTEXT_OWNER` / `ODYSSEUS_MCP_BRAIN_OWNER`); without one it uses the only
owner in the database, and refuses when there are several.

## Response

```json
{
  "turns":  {"turns": 4, "followed": 3, "corrected": 1, "empty": 0, "correction_rate": 0.333, "empty_rate": 0.0},
  "auto":   {"...": "same counts, Auto (source=rule) turns only"},
  "by_mode":   {"fast": {}, "light": {}, "think": {}, "deep": {}},
  "auto_by_mode": {"fast": {}, "think": {}},
  "by_source": {"rule": {}, "explicit": {}, "override": {}},
  "by_reason": {"small_talk": {}, "correction": {}, "short": {}},
  "flags": [{"kind": "reason", "key": "small_talk", "followed": 12, "correction_rate": 0.5,
             "baseline": 0.12, "hint": "..."}],
  "battery": {"turns": {"turns": 20, "passed": 17, "pass_rate": 0.85}, "by_mode": {}, "by_reason": {}},
  "min_followed": 8, "days": 30, "since": "2026-09-10T12:00:00", "rows_read": 812, "truncated": false
}
```

- `followed`: turns the person answered; only these enter `correction_rate`.
- `corrected`: the next message matched the correction rule Auto itself uses
  (`think_mode.is_correction`: "no, eso está mal", "try again", ...).
- `empty`: the reply had no text.
- A `think` with low effort is counted as `light`.
- `flags`: reasons or modes with at least `min_followed` followed turns whose correction rate
  is at least twice the Auto average and at least 15 points above it; modes are flagged from Auto's own decisions (`auto_by_mode`), never from modes picked by hand: candidates for a higher mode.
- `battery`: turns from daily-battery sessions (`scripts/daily_eval.py` writes each task's
  session to `logs/daily_eval/*.json`), with the battery's pass rate per mode and reason.

Nothing here changes a decision; the thresholds stay in `src/think_mode.py`.
