# Memory journal

`GET /api/memory-journal/changes` · `POST /api/memory-journal/changes/{id}/undo` · `DELETE /api/memory-journal/changes` · MCP `memory_changes` (memory server)

Every memory created, edited or deleted in one of four stores is recorded with the item as it was
before and after, and the turn that did it:

| store | what | hooked at |
|-------|------|-----------|
| `facts` | personal facts (`memory.json`) | `MemoryManager.save` (every writer) |
| `rules` | learned memory (`memory_engine.db`) | `add_item`, `delete_item` (`correct` = delete + create; `forget` also erases the journal text) |
| `instincts` | trigger -> action instincts | `instincts._save_raw` (trigger, action, domain, scope, project) |
| `notes` | second-brain notes | `notes.write_note`, `notes.delete_note` (trash) |

Bookkeeping (use counters, confidence, access stamps) is not a change. That includes the automatic lease closes and
reopens of the facts store (`closed_at` / `superseded_by`, `memory_leases`): a newer fact of the same kind closing an
older one is not journaled, and undoing an add makes the fact it had superseded current again (the save that removes the
superseder reopens it).

Stored text is capped per field (`MAX_FIELD_CHARS` = 20 000): a longer field keeps its start plus a `_truncated` marker
(`chars`, `sha256`), and a change whose copy was cut cannot be undone (`truncated: true`). A deleted note's text lives in
the notes trash, not again in the journal. Rows older than 90 days are pruned at startup and at most once a day after
that (the last pass is stored in `memory_journal.db`); big removals also compact the file and deletes use `secure_delete`.

## List

Query: `session_id`, `run_id`, `turn_id`, `store`, `limit` (1-500, default 100), `include_undone` (default true).
Only the caller's own changes. Each row also says `undone`, `purged` (the text was erased by a definitive forget) and
`truncated`. Changes made in one turn share `turn_id`; the post-turn extractors are filed under the turn that triggered
them (`metadata.turn_id` of the reply).

```json
{"changes": [{"id": "a6c3...", "ts": 1791644650.1, "owner": "admin", "store": "facts", "op": "create",
  "item_id": "5f1e...", "label": "Luis prefers green tea in the afternoon", "before_label": "",
  "session_id": "dce9fd75-...", "run_id": "...", "turn_id": "dce9fd75-...:1791644649000",
  "source": "ai_agent", "undone": false}], "count": 1, "stores": ["facts", "rules", "instincts", "notes"]}
```

## Undo

`POST /api/memory-journal/changes/{id}/undo` puts that one change back: removes what was created, restores the
edited text, brings back what was deleted (a forgotten learned memory comes back without its tombstone; a deleted
note leaves the trash). A refusal answers `detail = {"code", "message", ...}`, so Studio can word it in the interface
language:

| status | `code` | when |
|--------|--------|------|
| 409 | `later_change` | a later change to the same item exists; `blocking` = `{id, op, label, ts}` is the one to undo first |
| 409 | `changed_elsewhere`, `conflict` | the item changed outside the journal |
| 409 | `duplicate` | undoing a delete would duplicate the same text added again under another id (`existing_id`) |
| 410 | `store_wiped` | the store was wiped after the change |
| 410 | `purged` | the memory was definitively forgotten; its text is erased |
| 410 | `trash_gone` | the deleted note is no longer in the trash |
| 400 | `truncated`, `already_undone`, ... | cannot be undone |
| 404 | `unknown` | no such change, or someone else's |

## Erasing

* `DELETE /api/memory-journal/changes` clears the caller's history (the memories are not touched) -> `{"deleted": n}`.
* `DELETE /api/admin/wipe/memory` also drops every `facts` row and records the wipe, so nothing can be undone into wiped data.
* A definitive forget of a learned memory (`DELETE /api/memory-engine/items/{id}/forget`, tombstone) blanks the text of that
  item's rows; they stay as stubs that cannot be undone. `correct` keeps its rows: the correction is undoable.

## MCP

`mcp__memory__memory_changes` is catalogued in `src/tool_capabilities.py`: `action=list` is a private read, `action=undo`
a destructive private write that needs approval after outside content; any other action fails high. The tool only sees
the owner configured with `ODYSSEUS_MCP_MEMORY_OWNER`.

Each assistant reply stores the turn's changes in `metadata.memory_changes` (`id`, `store`, `op`, `label`).
