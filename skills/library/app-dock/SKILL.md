---
name: app-dock
description: Work in one of the family's Hoard apps while the person watches and uses the same page beside the chat: open it with app_open, read the screen with app_view, act with app_act, point things out with highlight. Use when the person wants to see an app next to the conversation or asks you to do, show or explain something inside an app ("ábreme Links al lado", "enséñame dónde se cambia eso en Darwin", "pulsa importar por mí", "open the tracker and mark it as delivered").
version: 1.0.0
category: engineering
tags: [apps, hoards, dock, side-panel, cursor, ui, app_open, app_view, app_act]
status: published
source: imported
---

## When to Use

The person wants an app open beside the conversation, or the task is easier to do or to show in the app's own screen than to describe: pressing a button, filling a form, finding a setting, walking through a view. For reading or changing the app's data in bulk, use the app's own MCP tools instead and leave the screen alone.

## Procedure

1. `app_open` with the app's id or name. It starts the app through the Hub when it is down and opens the tab. If it answers `no_host`, the person has no window showing this chat: say so.
2. `app_view` before the first action and again after anything changed the screen (yours or theirs). It gives the person's selection and focus, an outline with a `ref` per control, and the app's own tools.
3. Prefer the app's own tools (`app_act` with `action` = its name and `args`) when one does the job; they are faster and do not depend on the layout. Use the generic actions (`click`, `fill`, `select`, `navigate`, `scroll`, `wait`) for what has no tool. Give `target` as the `ref` from `app_view`, or the visible text or label.
4. Use `highlight` to point at something for the person (a setting, a button, a row) with a short note; it changes nothing. Do it before a risky click, and instead of the click when they only asked where something is.
5. After each action read the result. On `not_found` or `ambiguous` the answer lists the nearest labels or the candidates with their refs: pick one, do not guess. On `no_host`, `not_ready` or `timeout`, run `app_view` once; if it still fails, tell the person what is wrong.

## Pitfalls

- The person's own pointer and typing are never blocked, and they may change the page under you: re-read before acting when time has passed.
- Do what was asked in the app and no more. Do not press delete, send, pay or publish unless the person asked for that exact thing.
- Several agents can act at once, each with its own named cursor; sub-agents act in the same app as the chat that started them.
- If an app's page has no bridge yet, `app_view` answers `not_ready`: the app still works for the person; tell them you cannot act in it yet.

## Verification

After the last action, `app_view` once more and check the screen shows what you were asked for (the new row, the changed setting, the opened view). Report what you did and what you saw, not what you intended.
