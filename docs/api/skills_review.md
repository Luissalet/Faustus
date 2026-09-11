# Skills review (ADP-25) and browser snapshot scoping (ADP-29)

## ADP-25 — manifest, hash and diff before a discovered skill runs

`src.skills_runtime.discovery`/`.bridge` turn any `SKILL.md` found under a
project workspace's `.odysseus/skills`, `.agents/skills` or `.claude/skills`
folder into a `SkillManifest`. That is a **read** — listing and describing a
skill costs nothing and grants nothing. What must not follow automatically
is treating the skill as **runnable**: the masterplan's rule is that a
skill's own words never grant it more than a human explicitly approved, and
"explicitly" means pinned to exact bytes, not to a folder or a name.

`src/skill_import_review.py` is that pin. It tracks, per skill id
(`SkillManifest.id`, i.e. `category.name`), three independent facts:

| Field           | What it is                                                                  |
|-----------------|------------------------------------------------------------------------------|
| `digest`        | sha256 over the skill's `SKILL.md` **and every sibling file in its folder** (`src.skills_runtime.discovery.skill_digest`) — any byte anywhere in the folder changing produces a new digest. |
| `tools_required`| `manifest.permissions.backends`, checked as its own field even though the digest already covers it, so a caller can tell "prose edited" from "backend widened" without re-diffing bytes. |
| `by` / `approved_at` | who approved it, and when.                                              |

Stored at `DATA_DIR/skill_approvals.json`:

```json
{"category.name": {"digest": "…", "approved_at": 1732000000.0,
                    "tools_required": ["docker_workspace"], "by": "alice",
                    "version": "1.0.0"}}
```

A copy of the exact approved `SKILL.md` text is kept alongside, under
`DATA_DIR/skill_approvals/`, so `diff()` can show *what* changed rather than
only "the hash no longer matches".

### Gate states (`skill_import_review.status_of`)

- **`unreviewed`** — no approval on file for this skill id.
- **`needs_review`** — an approval exists but either the digest or the
  `tools_required` list no longer matches. The stale approval record is
  still returned (for the UI to show "was approved as of …"), never used to
  let the skill run.
- **`approved`** — current digest and tools match the last approval exactly.

### Privilege requests are refused, not reviewed

A skill's frontmatter asking to weaken the **approval system around it**
(`disabled_tools`, `tool_approval_mode`, `disable_approval`, `skip_approval`,
`bypass_approval`, `approval_mode`, `auto_approve`, `require_admin`,
`tool_approval`, `approval_required`, `skip_review`) is refused outright by
`approve()` — `error_class: "skills.privilege_request"` — at any digest.
This is different from `permissions_*` keys (network, backends, secrets,
filesystem, host access, …), which are normal, reviewable capability
requests handled by `src.skills_runtime.bridge`/`src.contracts.skill`. The
distinction: `permissions_*` says what the skill needs; the keys above say
"change how approval works for tools in general", which a skill's own text
never gets to ask for.

### HTTP API (`routes/skills_routes.py`, prefix `/api/skills`)

All three resolve the skill **inside one project's workspace**, owner-scoped
exactly like `src/workflows/skills.py::run` (`services.projects.get_store()
.get(project_id, owner=<caller>)`) — a project the caller does not own reads
as `404 skills_review.project_not_found`, never as someone else's skill.

- `GET /api/skills/{id}/review?project_id=<id>` — origin, version, digest,
  `tools_required`, declared `permissions`, any `privilege_request` flags,
  and the current gate `status`. Any signed-in caller who owns the project.
- `POST /api/skills/{id}/approve` — body `{"project_id": "<id>"}`. Admin
  only (`core.middleware.require_admin`), same tier as this file's existing
  built-in-tool mutations. Computes the current digest, refuses a privilege
  request (`409 skills.privilege_request`), otherwise records the approval
  and a text snapshot, and returns it.
- `GET /api/skills/{id}/diff?project_id=<id>` — unified diff of the current
  `SKILL.md` text against the last approved snapshot. `has_approved: false`
  with a `reason` (never an error) when nothing has been approved yet, or
  the snapshot file is missing (re-approve to fix).

Not found in either project or skill folder → `404 skills_review.not_found`.

### What still needs wiring (out of this change's file ownership)

`src/workflows/skills.py::run` is today's only caller that turns a
discovered skill into an effect (`bridge.manifest_from_markdown` → build a
bundle → `execution_router.execute`). That file is not part of this
change's file list. The hook it still needs, once wired:

```python
from src import skill_import_review
digest = discovery.skill_digest(found)
status = skill_import_review.status_of(
    manifest.id, digest=digest,
    tools_required=skill_import_review.tools_required_of(manifest))
if status.state != "approved":
    return {"status": "failed", "reason": f"skill needs review: {status.reason}",
            "error_class": "skills.needs_review"}
```

Place it right after `manifest = match[1]` / before the bundle is built (the
existing digest computed a few lines later in that function can be reused —
see `_bundle`'s own return value — instead of computing it twice).

## ADP-29 — subtree and search over a browser accessibility snapshot

`src/browser_view.py` already parses a `browser_snapshot` result into
`{ref, role, name}` rows (`parse_snapshot_elements`, WEB-04) and checks a
`ref` against a **fresh** snapshot by exact identity (`element_present`),
tolerant of both real `@playwright/mcp` textual shapes. This adds two pure
functions on top, so an agent that wants one part of a page does not have to
pay the context budget for the whole dump:

- **`subtree(snapshot, ref, depth=2) -> str`** — the lines rooted at the
  element whose ref matches `ref` exactly, plus up to `depth` further levels
  of descendants, derived from the snapshot's own indentation (not a
  hard-coded step size). `depth=0` returns just the matched line. Returns
  `""` for an absent ref — a caller cannot widen "not found" into "the whole
  page" by asking for a subtree of it. On the flat/relay snapshot shape
  (no indentation to derive children from) it degrades to just the matched
  line, same as `depth=0`.
- **`search(snapshot, query, limit=20) -> list[{ref, role, name}]`** — every
  parsed element whose role or name contains `query` (case-insensitive
  substring), in snapshot order, capped at `limit` (≤ 200). Built on
  `parse_snapshot_elements`, so a hit is always a real, freshly parsed
  element — never a phrase found in prose the parser did not recognise as
  one.

Both are pure (no I/O, no MCP calls) and operate on text the caller already
has — they do not fetch a new snapshot. Refs returned by either are only
ever valid against the snapshot they came from, same as the rest of this
module (`element_present`/`browser_actions.run_with_precondition` still
decide whether a ref may be acted on).

Not wired as agent-invokable tools (`browser_subtree`/`browser_search`) in
this change: that registration lives in `src/agent_tools/tool_schemas.py` +
`tool_capabilities.py` + `tool_index_examples.py`, none of which are in this
change's file list this round (see `docs/adaptations/baseline.md`'s ADP-29
row for context — a separate wave that touches `src/agent_tools/` should add
two thin tool wrappers that pass the agent's last-known snapshot text and
these functions' arguments straight through).
