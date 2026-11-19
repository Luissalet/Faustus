---
name: studio-writer-production-brief
description: Compile a production brief for a character, a scene or a place from a Writer project (look, what they carry, place, date, hour, weather, moon, style and an explicit list of details to keep) and hand it to the next station. Use when a production order starts from a story in Writer and the next station draws, films, models or narrates it.
version: 1.0.0
category: writing
tags: [studio, production, brief, writer, worldbuilding, character, handoff, expediente, produccion, ficha, personaje, escena, ilustracion, relevo]
apps: [writer]
accepts: [production-brief]
produces: [scene-brief]
status: published
source: imported
---

## When to Use

The first station of a visual or narrated production order whose source is a Writer project: an illustrator, a video shot, a 3D model or a narration needs to know who, where, when and what must not change.

- **Receives:** a Writer project, the chapter where the scene happens, and the names of the character, the objects and the place.
- **Delivers:** one brief (JSON plus a short Markdown copy) with fixed fields and a numbered list of details to keep, each with the sentence of the manuscript it comes from; optionally a draft character in Prospero.

## Procedure

1. **Reach the station.** `hub_app_status {app:"writer"}`; if it is down, `hub_start_app {app:"writer"}` and stop it (`hub_stop_app`) at the end. `hub_app_tools {app:"writer"}` gives the live catalogue; calls are `hub_call_app {app:"writer", tool, arguments, reason, timeout_s}`. Without the Hub's tools, the same call is `POST http://127.0.0.1:8810/api/apps/writer/call` with `Authorization: Bearer <token>` (the Hub's `data/mcp-token`; on this machine `E:\Development\IA\Hoards\Faustusables\Hoard Hub\data\mcp-token`) and body `{"tool", "arguments", "timeout_s"}`; the reply carries `ok` and `result`. With the same header, `GET /api/apps/writer` says whether it runs (its `state` field: `running` or `down`), `POST /api/apps/writer/start` and `/stop` start and stop it and `GET /api/apps/writer/tools` lists its tools. Keep each call's arguments and reply as a receipt in the order folder.
2. **Check the build.** If the catalogue has `wh_production_dossier` and `wh_create_project`, it is the current build from the repository. If it has about six hundred tools and lacks `wh_character_state`, `wh_get_world_weather` or `wh_state_ledger*`, it is an old installed build: tell the director and ask for Writer started from the repository; do not compile from an incomplete catalogue.
3. **Project.** For a test order, create a project with `wh_create_project` (undoable). Never write test material into one of the person's real projects.
4. **One call when it exists.** `wh_production_dossier` for the scene, then for the character and the place in that chapter. It returns sheet, voice, state ledger (what they carry), place, world date, scene hour, weather, moon, world style, approved reference images and the details to keep.
5. **Otherwise, the twelve calls that worked:** `wh_get_codex_entry` for the character, each important object and the place; `wh_get_writing` for the chapter; `wh_character_state` (state at the end of the chapter); `wh_voice_profile`; `wh_world_date`; `wh_list_places`; `wh_get_world_weather` (needs world coordinates: take them from the place, or choose a cell and say so in the brief); `wh_get_editorial_context`; `wh_build_world_bible` in sheet mode.
6. **Write the brief.** Fields: fixed look traits (in English too if the next station prompts an image model), clothing and colours, what they carry, place and its layout (count the steps, which side the lantern is on), date, hour, weather, light, moon, style, framing. Then **details to keep**, numbered, written so they cannot be misread: sides relative to the character and to the picture ("tube at HER left hip = right side of the picture when she faces the camera"), and every state change ("the bottom-right corner is missing; upside down the gap is at the top left").
7. **Hand off.** If Writer offers a hand-off to Prospero in its catalogue, use it and read the draft back from Prospero. Otherwise pass the brief file to the sheet station (`studio-prospero-character-sheet`), which imports the character with `cast_import_character`.

## Pitfalls

- The state ledger can disagree with the prose (in the probe it said "carries nothing" while the chapter gave her the tube and the map). Reconcile, and cite which source you used.
- The scene hour may live only in the prose, and the world style may be empty; write "missing" rather than inventing it silently.
- In browser mode Writer has no image studio and no Prospero hand-off ("needs the desktop app").
- A generated island may have no settlements; weather then needs coordinates chosen by hand.
- `wh_voice_profile` normalises the punctuation of quoted lines; quote dialogue from `wh_get_writing`.
- "Left" without a reference frame flips between stations.

## Verification

- Read the brief against the chapter: every detail to keep has its source sentence, and nothing in the brief contradicts the text.
- Hour, style and carried objects are filled in or explicitly marked missing.
- If a draft was handed to Prospero, read it back there and confirm the details to keep arrived word for word.
- Stop Writer with `hub_stop_app` if you started it.
