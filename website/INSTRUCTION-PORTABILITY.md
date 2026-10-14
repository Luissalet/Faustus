---
layout: default
---
# Portable instructions and project Markdown checks

Studio → Skills → Export previews and exports the user's canonical skills,
project rules and agent definitions. The source and destination can be different
folders. Export preserves existing different files by default and reports
conflicts; the user can explicitly replace destination files. Original skill
files, project instructions and agent definitions are never modified.

## Host formats

- Claude Code: `.claude/skills/<name>/SKILL.md`, `.claude/rules/*.md` and native
  `.claude/agents/*.md`. Rule paths and explicit-only invocation are represented.
- Codex: `.agents/skills/<name>/SKILL.md` with portable frontmatter and
  `agents/openai.yaml` with `interface` and optional invocation `policy`.
  Explicit-only skills set `allow_implicit_invocation: false`. Rules and agent
  instructions become selectable workflows; they do not create a second host
  agent runtime or automatically inject a rule by path.
- Cursor: `.cursor/rules/*.mdc` with `alwaysApply`, `globs` and descriptions.
  Explicit-only instructions remain manual rules. Supporting resources live
  under `.cursor/skills/`; Markdown resource links are adjusted accordingly.

UTF-8 scripts/references and binary resources are copied with the skill. Source
ownership, model/usage counters and unsupported runtime settings are not
mistaken for host instruction metadata. Variables such as
`${CLAUDE_PROJECT_DIR}` become portable path hints for other hosts; warnings
identify transformations and runtime fields that cannot be transferred.
Native project agent inheritance is resolved for its instructions. Export reads
the explicitly selected project definitions as text and does not change runtime
workspace trust or execute those agents.

The current schemas are based on the official host references:
[Codex skills](https://learn.chatgpt.com/docs/build-skills),
[OpenAI skill metadata](https://developers.openai.com/plugins/deploy/submission-errors),
[Claude skills](https://code.claude.com/docs/en/skills),
[Claude rules](https://code.claude.com/docs/en/memory),
[Claude sub-agents](https://code.claude.com/docs/en/sub-agents) and
[Cursor rules](https://cursor.com/docs/rules).
Exported files and their round trips are tested; this does not certify a live
session in every external host or promise identical runtime permissions/models.

## Markdown checks

Rules live in `<workspace>/.faustus/hooks/*.md`:

```markdown
---
name: own-paths
enabled: true
event: bash
conditions:
  - field: command
    operator: contains
    pattern: git add -A
---

Stage the paths belonging to this change.
```

Events are `bash`, `file`, `prompt` and `stop`, adapted to the existing lifecycle
engine's pre-tool, turn-start and turn-end callbacks. The rules emit messages;
they do not block tools, add approvals or execute a supplied shell command.
Existing lifecycle command/injection hooks continue working unchanged.

A simple `pattern` is a regex on command, file content, user prompt or final
answer respectively. Conditions are ANDed; fields include `command`,
`file_path`, `new_text`, `old_text`, `content`, `user_prompt` and
`assistant_message`. Operators: `regex_match`, `contains`, `not_contains`,
`equals`, `starts_with`, `ends_with`. Native file-tool JSON is decoded for body
and path checks. At turn-end, the existing live session cache supplies the final
answer when the lifecycle callback does not include it.

Malformed files and invalid regexes are reported without removing other checks.
An invalid regex is retained for correction and produces no matching hit.
Symlinks outside the selected workspace are not followed for hook writes.

Propose checks analyzes supplied conversation messages or a stored session's
history. Explicit corrections with command/path examples yield deterministic
proposals and evidence. Recommended replacement commands are not warning
triggers. General corrections without a mechanical example are retained as
notes; no invented regex is installed. Proposals are never automatically saved
and no LLM or model reservation is required for this deterministic method.

## API and MCP

The native API prefix is `/api/instruction-portability`:

- `GET /catalog?workspace=...`: canonical source identities.
- `POST /preview`, `/export`: `target`, optional source `workspace`, destination,
  and optional selected `skills`/`rules`/`agents`. Omitted selections include all;
  an empty list includes none. Export accepts explicit `overwrite`.
- `GET /hooks?workspace=...`, `POST /hooks/save` for listing and editing files.
- `POST /hooks/propose`: `messages` or `session_id`; a stored session follows
  the same owner identity as its authenticated client.
- `POST /hooks/evaluate`: workspace, event and context; returns advisory matches.

The product MCP module is
`python -m mcp_servers.instruction_portability.server`, with seven tools:
`instruction_catalog`, `instruction_export_preview`, `instruction_export`,
`markdown_hooks_list`, `markdown_hook_save`, `markdown_hooks_propose` and
`markdown_hooks_evaluate`. It calls the same native API; it has no duplicate
store. `FAUSTUS_INSTRUCTIONS_API_URL` (or `FAUSTUS_API_URL` / `FAUSTUS_URL`)
selects the API. `FAUSTUS_API_TOKEN`, `FAUSTUS_TOKEN` or the existing
`FAUSTUS_TOKEN_FILE` are used when that installation requires bearer
authentication; tokens are not printed in results or logs.
