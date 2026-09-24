---
name: hoard-what-happened
description: Reconstruct what happened around one moment or failure — which app went down, what the assistant called before, which rule ran, what logs and GPUs said — from Cassandra's audit and the hub's bus. Use when the user asks "¿qué pasó a las 4?" or "why is X down".
version: 1.0.0
category: research
tags: [hoards, cassandra, hub, incidents, audit, forensics]
status: published
source: imported
---

## When to Use

One moment or one failure needs a cause, in order, with evidence. For a general recap of the day use `hoard-family-recap`; for "is X up right now" call `svc_status` directly.

## Procedure

1. **Pin the moment.** Take the time the user gives ("a las 04:00", "esta
   madrugada", "when Borges stopped answering") and, if it is an app, its
   id. Cassandra's tools take `at` with a `window_min` half-width (15 by
   default) or `since`/`until`; the hub takes epoch seconds. Tools are
   `mcp__<connector>__<name>`; one `lookup_tools` call loads their schemas.
2. **Start from the incident, if there is one.** `svc_incidents` with the
   window (or `service`): opened/closed times, `to_state`, `probable_cause`.
   For the one that matters, `svc_why_down` with `service` (and
   `incident_id` when several): it already gathers what else changed in
   ±3 minutes, the GPU right before, the log tail, whether the process is
   still alive, the machine's boot time, and `bus_events_around` — the
   assistant's calls and the hub's rules around that minute.
3. **Then the bus around it.** `audit_search` with `at` and the same window:
   every `agent.call` (tool, ok, ms, caller, error), `hub.rule.ran` /
   `hub.job.ran` (which automation fired and what each action returned),
   `hub.app.started/stopped`, `hub.backup.*`, `hub.lease.*`. Add
   `failed: true` for a first pass when the list is long, and `tool` or
   `source` to narrow. `hub_events` with `since`/`until` gives the same
   window from the hub itself when Cassandra was not running yet.
4. **Then the logs and the GPUs**, only for what the previous steps point
   at: `logs_search` with the service and the window for tracebacks, OOM,
   "Killed", a port already in use; `gpu_timeline` if VRAM or a model load
   is a suspect. Do not read every log of every app.
5. **Write the timeline** in the user's language: one line per fact, in
   time order, with its source in brackets (`[cassandra]`, `[bus]`,
   `[log]`), and end with the cause you can defend and the ones you
   cannot rule out. A rule that fired right before is a fact, not a
   verdict. If nothing in the window explains it, say so and name what is
   missing (Cassandra not running then, hub events already pruned, no log).
6. **Offer the fix, do not apply it.** Restarting (`svc_restart`,
   `hub_start_app`), disabling a rule (`hub_rule_update`) or changing a job
   wait for the user's word in this turn.

## Output shape

```
Alrededor de las 04:00 (03:45–04:15)
03:58:10 [bus] hub.job.ran «Nightly backup» → hub_backup_run ok (174 s, 18 apps).
03:59:40 [bus] agent.call scribe/scribe_transcribe por la regla «Transcript → cards», falló: «CUDA out of memory».
04:00:02 [cassandra] llama-server 8081 caído (pid 103248 desaparecido); VRAM al 98 % 40 s antes.
04:00:05 [log] llama-server: «Killed».
Causa defendible: whisper cargó en la GPU compartida durante la copia; no descartable: el reinicio del sistema (arranque 03:57 no, la máquina lleva 3 días).
```

## Pitfalls

- Answering from the probable cause alone: it is a heuristic; the timeline
  is what the user asked for.
- Searching logs before knowing which minute: a dozen `logs_search` calls
  around midnight is the failure mode this skill exists to prevent.
- Confusing "the assistant called X" with "X caused it": the bus shows
  order, not blame.
- Opening `data/*.db` or `logs/` with the shell.

## Verification

- Each line of the timeline cites a tool result from this turn and its
  source tag.
- The window in the answer is the one the user named (or the stated default).
- Nothing was restarted, disabled or re-run in this turn.
