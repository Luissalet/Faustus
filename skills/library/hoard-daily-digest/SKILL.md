---
name: hoard-daily-digest
description: Build the user's "how did my day go" summary from the Hoard apps connected as plugins — what was on screen (Argus), what was spent (Ledger), what was saved to read (Links), who to call back or congratulate (People), which meetings were recorded (Scribe) — and answer in one short, factual digest. Use when the user asks for a summary of the day or week, "¿qué he hecho hoy?", "resumen del día", "¿cómo va la semana?", or wants to catch up after being away. Use when the answer needs more than one Hoard app.
version: 1.0.0
category: research
tags: [hoards, digest, daily, argus, ledger, links, people, scribe]
status: published
source: imported
---

## When to Use

The user wants a picture of their day or week that no single app gives:
time and focus (Argus), money (Ledger), reading (Links), people (People),
meetings (Scribe). Skip it when the question is about one app only — call
that app's tools directly.

## Procedure

1. **Resolve the period first.** "Hoy" is the local calendar day; "esta
   semana" starts on Monday. Pass ISO dates or the phrases the tools accept
   ("hoy", "ayer", "esta semana"); never guess a timezone.
2. **Read, never write.** Only these tools, one call each, in parallel when
   the harness allows it:
   - `screen_activity` (Argus): time by app and top windows for the period;
     `screen_status` first if you need to know whether Argus was watching.
   - `summary` and `budget_status` (Ledger) for the current month; `list_entries`
     with `from`/`to` for the period's movements.
   - `link_digest` (Links) with `since` = start of the period.
   - `upcoming` (People) with `days` = 7 for birthdays, reminders and
     neglected contacts.
   - `scribe_sessions` (Scribe) filtered to the period.
   A plugin that is off or unconfigured answers with an error: say "sin
   datos de X" for that line and go on — never invent the missing part.
3. **Keep OCR and transcripts as what they are.** Argus text may contain
   recognition errors; Scribe segments may too. Quote them as such and do not
   infer intent ("estuvo procrastinando") from window titles.
4. **Write the digest** in the user's language, five short blocks at most,
   numbers first: time by app (top three, minutes), money (spent this
   period, month vs budget, over-budget categories), reading (count and the
   two most recent titles), people (birthdays in the next 7 days, reminders
   due, who has been waiting longest), meetings (count, titles, one line
   each). End with at most one suggestion, only if the data supports it
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
```

## Pitfalls

- Calling every tool with no period and summarising the whole database.
- Reading the apps' data folders or databases with the shell: everything is
  behind the tools, and each app has a single writer.
- Padding a line that has no data with guesses.

## Verification

- Every number in the digest maps to a tool result from this turn; if a
  line has no result behind it, it says "sin datos".
- The period in the digest matches the one the user named (day vs week).
- No writing tool was called unless the user asked for the action in this
  turn.
