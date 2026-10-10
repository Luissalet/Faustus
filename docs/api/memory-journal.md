# Memory journal

`GET /api/memory-journal/changes` · `POST /api/memory-journal/changes/{id}/undo` · MCP `memory_changes` (memory server)

Every memory created, edited or deleted in one of four stores is recorded with the item as it was
before and after, and the turn that did it:

| store | what | hooked at |
|-------|------|-----------|
| `facts` | personal facts (`memory.json`) | `MemoryManager.save` (every writer) |
| `rules` | learned memory (`memory_engine.db`) | `add_item`, `delete_item` (`forget`/`correct` = delete + create) |
| `instincts` | trigger -> action instincts | `instincts._save_raw` (trigger, action, domain, scope, project) |
| `notes` | second-brain notes | `notes.write_note`, `notes.delete_note` (trash) |

Bookkeeping (use counters, confidence, access stamps) is not a change.

## List

Query: `session_id`, `run_id`, `turn_id`, `store`, `limit` (1-500, default 100), `include_undone` (default true).
Only the caller's own changes.

```json
{"changes": [{"id": "a6c3...", "ts": 1791644650.1, "owner": "admin", "store": "facts", "op": "create",
  "item_id": "5f1e...", "label": "Luis prefers green tea in the afternoon", "before_label": "",
  "session_id": "dce9fd75-...", "run_id": "...", "turn_id": "dce9fd75-...:1791644649000",
  "source": "ai_agent", "undone": false}], "count": 1, "stores": ["facts", "rules", "instincts", "notes"]}
```

## Undo

`POST /api/memory-journal/changes/{id}/undo` puts that one change back: removes what was created, restores the
edited text, brings back what was deleted (a forgotten learned memory comes back without its tombstone; a deleted
note leaves the trash). `409` when the item changed again after the change, `404` for an unknown change or someone
else's, `400` when it was already undone.

Each assistant reply stores the turn's changes in `metadata.memory_changes` (`id`, `store`, `op`, `label`).
