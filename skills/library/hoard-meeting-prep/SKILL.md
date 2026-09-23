---
name: hoard-meeting-prep
description: Prepare the user for a meeting, call or reunion with a specific person or on a specific topic by gathering what their Hoard apps already know — who the person is and what was said last time (People), recorded conversations that mention them (Scribe), pages saved about the topic (Links), documents in the personal library (Borges) and what was on screen when it last came up (Argus) — into one short brief. Use when the user says "tengo una reunión con X", "prepárame la llamada con", "qué sé de X", "antes de hablar con", "resúmeme lo que tengo sobre <tema>", or asks what to bring up with someone.
version: 1.0.0
tags: [hoards, meeting, brief, people, scribe, links, borges, argus]
category: research
status: published
source: imported
---

## When to Use

The user is about to talk to someone or about something and wants the
context they already own but cannot remember: the person's facts and
open reminders, the last conversation with them, what they saved to
read about the subject, the documents that mention it. Skip it for a
single-app question ("¿cuándo cumple años X?" is `get_person` alone).

## Procedure

1. **Pin down who and what.** Extract the person's name and the topic
   from the request. Call `find_people` with the name; if more than one
   candidate is plausible, ask which one before anything else. A topic
   with no person is fine (then skip the People and Scribe-by-person
   steps).
2. **Read, never write.** Plugin tools are exposed as
   `mcp__<connector>__<name>`; one `lookup_tools` call naming all of them
   loads their schemas. Then call, in parallel where the harness allows:
   - `get_person` (People): facts, last interactions, open reminders,
     days since last contact.
   - `scribe_search` (Scribe) with the person's name and again with the
     topic; for the best hit, `scribe_transcript` with a small window
     around the match — never the whole session.
   - `search_links` (Links) with the topic (and the person's name or
     company if any); `read_link` only for the one or two most relevant,
     limited to a few thousand characters.
   - `library_search` (Borges) with the topic; quote the exact passage
     the tool returns, with file and page.
   - `screen_search` (Argus) with the topic or name, last 30 days, to say
     when it last came up on screen — titles only, no OCR text unless it
     answers a question.
   A plugin that is off or empty answers with an error or zero hits: say
   "sin datos de X" for that line and go on; never invent the missing part.
3. **Keep sources as what they are.** Transcripts and OCR carry
   recognition errors; a saved page is someone else's opinion; a library
   passage is a quote, not a conclusion. Attribute each item to its app
   and date.
4. **Write the brief** in the user's language, six short blocks at most:
   who (one line from People, plus open reminders), last time (date, what
   was agreed or pending, from Scribe or People interactions), what they
   saved (two titles with one line each), what the library says (one
   quote with file and page), when it last came up on screen (one line),
   and three suggested talking points that follow from the data. Every
   point must trace to a tool result from this turn.
5. **Offer, don't do.** If the user wants an action (log the meeting, add
   a reminder, tag a link), ask before calling any writing tool.

## Output shape

```
Reunión con M. (jueves)
· Quién: vecina, le gustan los gatos; cumple el 14 de marzo · 2 recordatorios abiertos.
· Última vez: 12 sept, llamada de 18 min (Scribe): quedó pendiente enviarle el presupuesto.
· Guardado: «…» (site, 10 sept) · «…» (site, 3 sept).
· Biblioteca: «cita exacta…» — apuntes.pdf, p. 14.
· En pantalla: el tema apareció por última vez el 20 sept (título de la ventana).
· Para hablar: 1) el presupuesto pendiente 2) … 3) …
```

## Pitfalls

- Searching every app with no name or topic and summarising everything.
- Reading whole transcripts or whole articles when a window or an excerpt
  answers the question.
- Guessing a person when `find_people` returned several candidates.
- Reading the apps' data folders or databases with the shell: everything
  is behind the tools, and each app has a single writer.

## Verification

- Every line of the brief names the app and date it came from, and maps
  to a tool result from this turn; a line with nothing behind it says
  "sin datos".
- The person in the brief is the one the user named (or the one they
  picked when asked), not a fuzzy match taken silently.
- No writing tool was called unless the user asked for the action in this
  turn.
