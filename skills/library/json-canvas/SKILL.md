---
name: json-canvas
description: Create, edit or check a .canvas file in the open JSON Canvas 1.0 format (text, file, link and group nodes with integer geometry; edges with sides, ends, colours and labels) so it opens in Obsidian and other canvas editors, and move boards in and out of the Cerebro with brain_canvas_export and brain_canvas_import. Use when the user asks for a canvas, a board, a mind map or a visual map of notes, mentions a .canvas file or JSON Canvas, or wants the knowledge graph exported to or imported from Obsidian.
version: 1.0.0
category: writing
tags: [json-canvas, canvas, obsidian, mind-map, board, diagram, knowledge-graph, cerebro, lienzo, mapa-mental, grafo]
status: published
source: imported
---

## When to Use

A `.canvas` is a JSON board: cards (nodes) placed on an infinite plane and arrows (edges) between them. Use this skill to write one by hand, to fix one that will not open, or to turn the Cerebro (Faustus's knowledge graph) into one and back. For the Markdown inside text nodes see `obsidian-markdown`.

Adapted from kepano/obsidian-skills, skill `json-canvas` (MIT, © 2026 Steph Ango, pinned 3ccff5338ea7), and the specification in obsidianmd/jsoncanvas (MIT, © 2024 Obsidian.md, spec 1.0 of 2024-03-11, pinned 456f843cb293). See THIRD_PARTY_NOTICES.md.

## Procedure

1. **From the Cerebro, use the tools instead of writing JSON.**
   - `brain_canvas_export`: whole graph, or `center` (note path, note title or entity) with `depth` hops, `kinds`, `tags`, `limit`; `scope` notes or entities; `mode` file (nodes point at the vault notes, opens in Obsidian from inside the vault) or text (excerpts copied, the file travels alone). It lays nodes out without overlaps, groups them by kind/folder/tag and saves `Canvas/<name>.canvas` in the vault (`save_path` for another place).
   - `brain_canvas_import`: `path` to a `.canvas` or the document in `canvas`; notes go to `Canvas/<name>/`, edges become `[[wikilinks]]`, groups become `canvas/<group>` tags. Run `dry_run` first on a large board; run it again after editing the board (it is idempotent by node id); `prune` removes notes whose card is gone; `undo` removes the whole import.
   - The Cerebro screen has the same two buttons over the graph: «Exportar a Canvas» and «Importar Canvas».
2. **Writing one by hand: the shape.** `{"nodes": [...], "edges": [...]}`. Nodes are drawn in array order (first is lowest), so put groups first.
3. **Every node:** `id` (unique string; 16 lowercase hex characters is the convention), `type` (`text`, `file`, `link`, `group`), `x`, `y`, `width`, `height` as integers (pixels; `x` grows right, `y` grows down; position is the top-left corner; negatives are fine), optional `color`.
   - text: `text` with Markdown. Line breaks are `\n` inside the JSON string.
   - file: `file` (a path inside the vault, such as `Notes/Plan.md` or `img/map.png`), optional `subpath` starting with `#` (`#Heading` or `#^block`).
   - link: `url`.
   - group: optional `label`, `background` (image path) and `backgroundStyle` (`cover`, `ratio`, `repeat`). A node belongs to a group when it sits inside the group's rectangle; there is no membership list.
4. **Every edge:** `id`, `fromNode`, `toNode`; optional `fromSide`/`toSide` (`top`, `right`, `bottom`, `left`), `fromEnd`/`toEnd` (`none`, `arrow`; defaults none and arrow), `color`, `label`.
5. **Colours:** a preset `"1"`–`"6"` (red, orange, yellow, green, cyan, purple; the app picks the exact shade) or a hex string `"#RRGGBB"`. Always a string.
6. **Layout by hand:** sizes around 250–400 × 80–300; 60–100 px between cards; 20–50 px of padding inside a group; snap to multiples of 20. Connect left-to-right with `right`→`left`, top-to-bottom with `bottom`→`top`.
7. **Validate before handing it over.** `POST /api/brain/canvas/validate` with `{"canvas": …}` returns every problem; or check by reading: parseable JSON, ids unique across nodes and edges, every `fromNode`/`toNode` exists, required field per type present, enums spelled exactly, geometry integers.

## Pitfalls

- Floats in `x`/`y`/`width`/`height` (some generators write `120.5`): the spec says integers. The importer rounds them; other editors may not.
- A raw newline inside a JSON string makes the whole file invalid; write `\n`.
- Duplicate ids between a node and an edge are also invalid: the id space is shared.
- File nodes resolve relative to the vault root, not to the canvas file's folder.
- A group does not move its cards in the JSON: moving a group means adding the same offset to every node inside it.
- Importing into the Cerebro never rewrites memories, entities or project notes; arrows that leave them are listed in the import's index note instead. That is expected, not a failure.
- Exports of very large graphs are hard to read: narrow with `center` and `depth`, `kinds` or `tags`, or lower `limit`.

## Verification

- The validator returns no errors, or `brain_canvas_export` reports `errors: []`.
- The file opens in Obsidian (or any JSON Canvas viewer) with no overlapping cards and every arrow attached.
- After an import, the new notes are under `Canvas/<name>/`, the graph shows the expected links, and a second import of the same file reports `created: 0, updated: 0`.
