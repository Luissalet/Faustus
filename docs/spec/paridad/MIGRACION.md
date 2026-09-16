# Migration — supported project interchange format (A34)

`src/migration/` imports a project exported from an external agent harness
into Faustus, without naming that harness anywhere in code, docs or tests
(rule 4 of the parity contract). This file documents the interchange format
Faustus's importer accepts — a reasonable, self-contained shape, not a copy
of any one product's real export — so a project bundled this way can be
previewed and imported deterministically.

## Supported interchange format

A project is a directory:

```
<project>/
  agents/*.json|*.yaml     # agent/assistant definitions
  models.json               # model configs used by the project
  connectors.json            # external tool/service connectors
  skills/<name>/SKILL.md     # skill packs (frontmatter + Markdown body)
  sessions/*.jsonl            # one JSON object per line: a conversation
  files/**                    # arbitrary attachments referenced by agents/sessions
  schedules.json              # scheduled/recurring actions
```

### `agents/*.json|*.yaml`

One object per agent:

```json
{"name": "reviewer", "model": "gpt-4o", "instructions": "...", "tools": ["read_file"]}
```

Maps to Faustus's own file-based agent definitions
(`src/agent_defs.py`: `DATA_DIR/agents/<slug>/AGENT.md`, frontmatter + body).
Classified `transformed`.

### `models.json`

A list of `{"id", "provider", "params"}`. A `provider` Faustus's model
router recognises (`openai`, `anthropic`, `ollama`, `local`) is
`transformed` into a router-compatible reference; an unrecognised provider
is `unsupported` — Faustus does not invent a mapping for a provider it has
no client for.

### `connectors.json`

A list of `{"id", "type", "config": {...}, "credentials": {...}}`.
**Every connector entry is always `requires_reauthorization`, unconditionally
— never `preserved` or `transformed`.** `credentials` (tokens, API keys,
OAuth secrets) are read only long enough to be dropped; the importer never
writes a credential value to any file under `DATA_DIR` or anywhere else.
The real connection path stays `POST /api/app-connectors` then
`POST /api/app-connectors/{connector_id}/connect`
(`routes/connector_routes.py`) — the report's reason string points there so
the person doing the import knows exactly where to re-enter the secret by
hand.

### `skills/<name>/SKILL.md`

Already Faustus's own skill-pack shape (frontmatter + Markdown, consumed by
`src/workflows/skills.py`). Classified `preserved` — copied byte for byte.

### `sessions/*.jsonl`

One JSON object per line: `{"role", "content", "ts"}` (ordinals implied by
line order). Normalised (role/content shape checked, ordinals assigned) and
classified `transformed` — this lot writes the normalised copy under the
import's own output directory (`DATA_DIR/migrations/<import_id>/sessions/`)
rather than injecting rows into the live session store, which belongs to
the session/chat subsystem, not the migration importer. Wiring that into a
live, browsable Faustus session is a follow-up step, recorded in the
import's `losses.md` as a note, not silently done or silently dropped.

### `files/**`

Copied byte for byte into the import's output directory. Classified
`preserved`.

### `schedules.json`

A list of `{"id", "cron", "action": {"type": ..., ...}}`. An `action.type`
Faustus's own scheduler understands today (`prompt`, `workflow`) is
`transformed`; anything else (webhooks, custom scripts, a type nobody
implements) is `unsupported` — listed with the reason, never dropped
silently. This lot classifies and reports; it does not write scheduler
state (`src/task_scheduler.py` is owned by a different lot in this
contract) — the transformed schedule stays in the import's output
directory pending an explicit activation step.

## Report shape

`preview(path) -> Report` returns one `ResourceEntry` per resource found:

```python
ResourceEntry(kind, source_path, status, reason, target_hint)
```

where `status` is one of `preserved | transformed | unsupported |
requires_reauthorization`. Nothing found in the project is ever left off
the report — an entry the importer does not recognise at all still gets one,
with `status="unsupported"` and a reason saying so, rather than being
skipped in silence.

`apply(path, report, decisions) -> ApplyResult` imports only entries whose
status is `preserved` or `transformed` (or that `decisions` explicitly
force to `import`), writes `DATA_DIR/migrations/<import_id>/` with the
imported files plus `manifest.json`, and always writes `losses.md` listing
every entry that was NOT imported and why (unsupported resource, or
requires re-authorization) — so nothing is silently omitted from the
record even though it was correctly excluded from the import itself.

## Test coverage

`tests/acceptance/test_a34_project_migration.py` builds a fixture project
with one unsupported schedule action and one connector carrying a token,
runs `preview` (asserts both are listed with the right status/reason) and
`apply` (asserts the output tree — searched recursively — never contains
the token string, and that `losses.md` names both excluded entries).
