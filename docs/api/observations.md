# Observations (`src/observations`, `/api/observations/*`, MCP `observations`)

A note per useful tool call, written by the small model in the background, and a
progress summary per turn. Stored in `OBSERVATIONS_DB` (SQLite, WAL, FTS5 with a
`LIKE` fallback). Code: `src/observations/`; routes: `routes/observations_routes.py`;
MCP server: `mcp_servers/observations_server.py`. Tests: `tests/test_observations.py`.

## What is stored

Observation: `{id, created, type, title, subtitle, facts[], narrative, concepts[],
files_read[], files_modified[], session, project}`. `type` is one of `bugfix`,
`feature`, `refactor`, `change`, `discovery`, `decision`, `other`. Turn summary: same
idea, referenced as `S<n>`.

Capture: the lifecycle listeners `post_tool`, `turn_start`, `turn_end` and
`session_start` (`src/lifecycle_hooks.py::register_listener`). A deterministic
pre-filter drops noise and repeats, secrets are redacted, and a bounded queue feeds
one daemon thread. The worker defers while a turn is running, the model is not
resident, no slot is free or the unattended-jobs breaker is open. Nothing is captured
or injected in incognito chats.

## Progressive retrieval

| Step | HTTP | MCP tool | Returns |
|---|---|---|---|
| 1 | `GET /api/observations/search?q=&limit=&type=&project=&since=` | `obs_search` | `{results: [{id, created, type, title}]}`, limit <= 20 |
| 2 | `GET /api/observations/timeline?anchor=12,S3&before=2&after=2` | `obs_timeline` | `{entries: [{ref, kind, created, type, title, anchor}]}`, <= 5 anchors, <= 10 before/after |
| 3 | `GET /api/observations/details?ids=12,13` | `obs_details` | `{records: [...]}`, <= 5 ids (400 above) |

Also:

- `GET /api/observations/recent?limit=&project=&type=` (MCP `obs_recent`): newest first, index shape.
- `GET /api/observations/for-file?path=&workspace=&limit=`: notes that touched a file.
- `GET /api/observations/context?project=&max_chars=`: the recap and how it was fitted. Trim order: narratives, summary, older days, titles.
- `GET /api/observations/status`: counts and the worker's counters.
- `DELETE /api/observations/{id}`: forget one note (404 if it is not yours).

`since` is epoch seconds or an ISO date. Signed-in callers see only their own notes;
single-user installs see everything.

## Python

```python
from src import observations
observations.search("fts index", 20, owner="")
observations.timeline(["12"], 2, 2, owner="")
observations.details(["12", "13"], owner="")
observations.observations_for_file("src/foo.py", 5, owner="")
observations.context_block("my-project", max_chars=6000)
```

## Settings (Settings > Agent Tools > Observations)

| Key | Default | Meaning |
|---|---|---|
| `agent_observations_enabled` | true | write notes |
| `agent_observations_summaries` | true | one summary at the end of a turn |
| `agent_observations_inject_session_start` | false | inject a recap when a chat starts |
| `agent_observations_inject_max_chars` | 6000 | recap budget |
| `agent_observations_file_context` | false | inject notes about a file when it is read |
| `agent_observations_queue_max` | 100 | pending calls kept |
| `agent_observations_max_per_turn` | 40 | most notes per turn |
| `agent_observations_model` | "" | model for the notes; empty = the utility endpoint |
| `agent_observations_retention_days` | 90 | days kept |

## MCP

Built-in server `observations`, registered at runtime by `listeners.register_mcp_server()`.
All four tools are read-only. `ODYSSEUS_MCP_OBSERVATIONS_OWNER` scopes whose notes the
server reads; without it reads are install-wide.

## Limits

`post_tool` only fires on the normal tool path (approval-resume, prefetch and
pre-approved runs may skip it). The turn summary only sees notes already processed.
Sub-agents that share a `session_id` share a turn state.
