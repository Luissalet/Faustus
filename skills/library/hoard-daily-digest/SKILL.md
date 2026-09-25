---
name: hoard-daily-digest
description: One short digest of the user's day or week from the Hoard plugins: screen time (Argus), money (Ledger), reading (Links), people (People), meetings (Scribe), study (Hypatia); en español, un resumen del día o de la semana: pantalla, dinero y gastos, lectura, gente, reuniones y estudio. Use when the user asks "¿qué he hecho hoy?", "¿cómo ha ido mi día?", "how did my day go", "resumen del día/semana", or wants to catch up after being away.
version: 1.0.0
category: research
tags: [hoards, digest, daily, argus, ledger, links, people, scribe, hypatia, pantalla, dinero, gastos, lectura, gente, reuniones]
status: published
source: imported
---

## When to Use

The user wants the whole picture of a day or week, not one app's answer. Skip it for a single-app question — call that app's tools directly.

## Procedure

1. **Resolve the period first.** "Hoy" is the local calendar day; "esta
   semana" starts on Monday. Pass ISO dates or the phrases the tools accept
   ("hoy", "ayer", "esta semana"); never guess a timezone.
2. **Read, never write.** Only these tools, one call each, in parallel when
   the harness allows it:
   - `screen_activity` (Argus): time by app and top windows for the period;
     `screen_status` first if you need to know whether Argus was watching.
     This is the screen line. Funes's `activity_summary` measures something
     else (input activity: active vs away); use it only for an "activo/ausente"
     note, never as the time by app.
   - `summary` and `budget_status` (Ledger) for the current month; `list_entries`
     with `from`/`to` for the period's movements.
   - `link_digest` (Links) with `since` = start of the period.
   - `upcoming` (People) with `days` = 7 for birthdays, reminders and
     neglected contacts.
   - `scribe_sessions` (Scribe) filtered to the period.
   - `study_stats` (Hypatia): due today per subject, streak.
   Plugin tools are exposed as `mcp__<connector>__<name>`; one `lookup_tools`
   call naming all of them loads their schemas.
   A plugin that is off or unconfigured answers with an error: say "sin
   datos de X" for that line and go on — never invent the missing part.
3. **Keep OCR and transcripts as what they are.** Argus text may contain
   recognition errors; Scribe segments may too. Quote them as such and do not
   infer intent ("estuvo procrastinando") from window titles.
4. **Write the digest** in the user's language, six short blocks at most,
   numbers first: time by app (top three, minutes), money (spent this
   period, month vs budget, over-budget categories), reading (count and the
   two most recent titles), people (birthdays in the next 7 days, reminders
   due, who has been waiting longest), meetings (count, titles, one line
   each), study (cards due now, reviewed today, streak). End with at most
   one suggestion, only if the data supports it
   (an over-budget category, a neglected contact, an unread pile).
5. **Offer, don't do.** If the user wants an action (log a contact, tag a
   link, note an expense), ask before calling any writing tool.

## Output shape

```
Hoy (mar 23 sept)
· Pantalla: 3 h 10 en Code, 1 h 05 en el navegador, 40 min en Claude.
· Dinero: 57,50 € hoy · septiembre 57,50 € de 600 € · sin categorías pasadas.
· Lectura: 2 enlaces guardados (último: «…»).
· Gente: cumpleaños el jueves (M.), 1 recordatorio vencido, 3 personas sin contacto desde hace más de un mes.
· Reuniones: 1 grabada (12 min, «Entrevista …»).
· Estudio: 3 tarjetas pendientes · 7 repasadas hoy · racha 2 días.
```

## Pitfalls

- Calling every tool with no period and summarising the whole database.
- Reading the apps' data folders or databases with the shell: everything is
  behind the tools, and each app has a single writer.
- Padding a line that has no data with guesses.
- Mixing sources in one line: a window can stay open for hours while the
  user is away. Time by app comes from Argus; "activo X, ausente Y" from
  Funes, labelled as such, or left out.

## Verification

- Every number in the digest maps to a tool result from this turn; if a
  line has no result behind it, it says "sin datos".
- The period in the digest matches the one the user named (day vs week).
- No writing tool was called unless the user asked for the action in this
  turn.
