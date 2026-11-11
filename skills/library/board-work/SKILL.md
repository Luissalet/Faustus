---
name: board-work
description: Build and edit a whiteboard (Pizarra) the person sees and edits beside the chat: pizarra_create, pizarra_read, pizarra_edit, pizarra_arrange. Use when the person wants something drawn, mapped or organised on a board ("hazme un tablero con el plan de lanzamiento", "pon esto en la pizarra", "organiza estas notas por temas", "dibuja el flujo", "make a mind map of this").
version: 1.0.0
category: productivity
tags: [whiteboard, pizarra, canvas, mindmap, flow, notes, layout, pizarra_create, pizarra_edit]
status: published
source: imported
---

## When to Use

The task is spatial: a plan with branches, a flow, a timeline, sticky notes to group, an architecture sketch, a brainstorm to sort. If the output is prose or a table, write a document or a sheet instead. A board is a JSON Canvas file saved with the chat; the person sees your changes as they land and can move or edit anything.

## Procedure

1. Start with `pizarra_create` and fill it in the same call: `items` (nested `text` / `children`) plus `layout` (`tree`, `mindmap`, `flow`, `timeline`, `grid`) lets the server place everything. If a board is already open, use `pizarra_read` first: it lists ids, positions, the person's selection and what is in view.
2. Never compute coordinates yourself unless the person asked for exact positions. Use `outline` or `layout` ops, or add items with no `x`/`y` and let free-space placement put them near an anchor (`near`: an item id).
3. Change with `pizarra_edit` in one batch (`add`, `update`, `move`, `delete`, `connect`, `group`, `layout`, `align`, `order`). Connect items with `connect` (label optional) instead of drawing lines. Put a set of related items in a frame with `group`.
4. After many edits, `pizarra_arrange` tidies a set: all, the person's selection, a frame or a list of ids.
5. Work with what the person selected when they say "this" or "these": the selection comes with `pizarra_read` and the turn context.

## Pitfalls

- The person edits the same board at the same time. Re-read before changing items they may have touched; edits merge per item, the last write wins.
- Keep text short: a sticky holds a few words, not a paragraph. Put detail in a document and link it with a `link` item.
- Do not delete or rewrite items the person made unless asked; add beside them.
- Batches stop at the first op that fails and say which; fix that op and send the rest again.
- Colours are the presets 1-6 or a hex value; use them to mean something (status, owner), not decoration.

## Verification

`pizarra_read` once after the last edit and check the counts and the layout summary match what you meant (the items, the connectors, no overlaps). Tell the person what is on the board and where, not what you meant to do.
