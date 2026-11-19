---
name: studio-drucker-engagement-brief
description: Register a client order as an engagement in Drucker with its scope items and assumptions, read it back, and keep a Markdown brief that the next stations quote. Use when a production order does not start from a story (a report, a presentation, a web) and needs a brief with scope, client and assumptions before work begins.
version: 1.0.0
category: planning
tags: [studio, drucker, brief, engagement, scope, assumptions, client, expediente, encargo, alcance, supuestos, cliente]
apps: [drucker]
accepts: [production-order]
produces: [production-brief]
status: published
source: imported
---

## When to Use

The first station of a presentation, report or web order. For the full consulting cycle (proposal, review, delivery package) see `engagement-proposal-and-delivery`.

- **Receives:** the order as written by the person or the director (text of the task), its deadline and its audience.
- **Delivers:** `encargo.md` (the order interpreted: goal, audience, deliverables, deadline, assumptions, acceptance criteria) and a Drucker engagement id whose reread matches it.

## Procedure

1. **Write `encargo.md` first.** Goal, audience, deliverables with their formats, deadline, length, what is assumed, and how each deliverable will be accepted. Mark test clients and data as fictitious.
2. **Reach the station.** `hub_app_status {app:"drucker"}`; if it is down, `hub_start_app {app:"drucker"}` and stop it (`hub_stop_app`) at the end. `hub_app_tools {app:"drucker"}` gives the live catalogue; calls are `hub_call_app {app:"drucker", tool, arguments, reason, timeout_s}`. Without the Hub's tools, the same call is `POST http://127.0.0.1:8810/api/apps/drucker/call` with `Authorization: Bearer <token>` (the Hub's `data/mcp-token`; on this machine `E:\Development\IA\Hoards\Faustusables\Hoard Hub\data\mcp-token`) and body `{"tool", "arguments", "timeout_s"}`; the reply carries `ok` and `result`. With the same header, `GET /api/apps/drucker` says whether it runs (its `state` field: `running` or `down`), `POST /api/apps/drucker/start` and `/stop` start and stop it and `GET /api/apps/drucker/tools` lists its tools. Keep each call's arguments and reply as a receipt in the order folder. If the Hub cannot list Drucker's catalogue (it answered 502 while the bridge itself worked), call the app directly: `GET http://127.0.0.1:8810/api/apps/drucker` gives `url` and `token_file`; then `POST <url>/api/agent/call` with `Authorization: Bearer <token>` and body `{"tool","arguments"}`.
3. **Save.** `drucker_engagement_save` with: `name`, `client_name`, `origin` (the Agora task), `status:"active"`, `kind` (`"report"` for a report or a deck), `currency:"EUR"`, `notes` (the whole `encargo.md`), `lang:"es"`, and `items`: `{"scope":[{"title":"..."}, ...], "assumption":[{"text":"..."}]}`. Scope items take `title`; assumptions take `text`. Getting either wrong returns 422 with the missing field.
4. **Read back.** `drucker_engagement_get {engagement_id, lang:"es"}` and save it as `drucker-expediente.json`.
5. Put the engagement id and the path of `encargo.md` in the Agora task, so every later station quotes the same brief.
6. Stop Drucker if you started it.

## Pitfalls

- The tool schema does not say which item kind uses `title` and which uses `text`.
- Direct bridge calls and Hub calls take different body shapes; keep one helper per route.
- A fictitious client written as if real ends up in a real engagement list: prefix test names (`[ESTUDIO #<task>]`).
- The brief is not a plan: deliverables without acceptance criteria cannot be checked later.

## Verification

- The reread engagement has every scope item and assumption of `encargo.md`, with the client and origin right.
- Each deliverable in `encargo.md` has a format and an acceptance criterion that a later station can test.
