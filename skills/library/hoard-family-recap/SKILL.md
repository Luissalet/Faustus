---
name: hoard-family-recap
description: A short recap of what the Hoard family did by itself since a moment: watched releases and feed entries, rules and jobs, backups, apps started or stopped, incidents. Use when the user asks "¿qué ha pasado en las hoards?", "what came in from my watches", "did the backup run".
version: 1.0.0
category: research
tags: [hoards, hub, events, watches, rules, backups, recap]
status: published
source: imported
---

## When to Use

The user wants what the apps did on their own — arrivals, automations, copies, outages — not what they did themselves. For "what did I do today" use `hoard-daily-digest`; for one failure, `hoard-what-happened`.

## Procedure

1. **Resolve the period.** "Desde ayer", "esta semana", "since I left": turn it
   into epoch seconds for the hub (`since`) and into the same moment for
   Cassandra (`since` accepts ISO, a clock time or an age like `6h`). Default
   to the last 24 hours and say so.
2. **Read the bus, never the databases.** The hub keeps the last events; the
   tools are `mcp__<connector>__<name>` (one `lookup_tools` call naming them
   loads their schemas). One call per line, in parallel when possible:
   - `hub_event_stats` with `since`: totals by type and by source — the
     skeleton of the recap.
   - `hub_events` with `type: "digest.item"` and `since`: what the watches
     brought in (each carries `title`, `url`, `watch`); if there is none,
     try `links.watch.new` and `links.watch.changed` directly.
   - `hub_events` with `type: "hub.rule.ran|hub.job.ran"`: which automations
     fired, ok or failed, and `results` (one per action).
   - `hub_events` with `type: "hub.backup.*"`: snapshot id, apps, files, new
     bytes, and any `hub.backup.failed`.
   - `hub_events` with `type: "hub.app.*|cassandra.incident.*"`: starts,
     stops, incidents opened and closed with their `probable_cause`.
   - `audit_stats` (Cassandra) with the same `since`: which tools the
     assistant called, how many failed, from which caller — the "what the
     assistant did" line.
   The hub keeps only the last 20 000 events; for anything older ask
   Cassandra (`audit_search` with `since`/`until`), which mirrors the bus.
3. **Group by what the user cares about**, not by event type: arrivals
   (watched releases, feed entries, page changes), automations (rules and
   jobs, with failures first), copies (last backup, size, anything skipped
   as "locked by the app"), availability (incidents, restarts), and the
   assistant's own activity (calls, failures). A group with nothing gets
   one line: "sin novedades".
4. **Write it** in the user's language, numbers first, newest first inside
   each group, at most five items per group with "y N más". Quote titles as
   they came; a URL only when the user will want to open it.
5. **Offer, don't do.** If something failed (a rule, a backup, an app), say
   what and offer `hoard-what-happened` or the specific tool; never restart,
   re-run or install anything unless asked in this turn.

## Output shape

```
Desde ayer a las 22:00
· Llegadas: 3 novedades vigiladas — release v0.4.0 de <repo>, 2 entradas del feed «…».
· Automatizaciones: 4 reglas ejecutadas, 1 fallida (Transcript → cards: hypatia no respondía).
· Copias: nocturna a las 04:00, 18 apps, 5,1 GB, 1 fichero saltado (workbench.duckdb, en uso).
· Disponibilidad: 1 incidencia — argus caído 03:12–03:14, causa probable «reinicio del proceso».
· Asistente: 41 llamadas, 2 fallidas (links/read_link: enlace sin texto).
```

## Pitfalls

- Summarising the whole bus with no `since`: the default is the last day.
- Reading `data/events.db` or `rules.json` with the shell: everything is
  behind the tools.
- Calling `hub_rule_run`, `hub_backup_run` or `hub_start_app` from a recap:
  those are actions, and they wait for the user's word.
- Treating a `hub.rule.ran` with `ok: true` as "nothing to report": its
  `results` may carry one failed action among several.

## Verification

- Every number maps to a tool result from this turn; a group without a
  result says "sin datos", never a guess.
- The period in the recap matches the one the user named.
- No writing or starting tool was called in this turn.
