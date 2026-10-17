# Projects workspace

Projects has six sections: **Overview, Chats, Tasks, Knowledge, Skills,
Settings**. Agent activity opens as a drawer from every section, including
the project's full Agora view.

Overview contains the brief, the project's Agora rooms and recent activity.
Chats retains ordinary project conversations. Tasks contains the existing
issue board, objectives, versioned requirements and coordination tasks.
Knowledge contains editable durable memory, linked sources, the literal
context preview and the file neighbourhood. Settings contains standing
instructions, path-specific rules, repositories, execution/coordination
defaults and the existing project settings.

## Compatibility and preservation

Old `tab` URLs continue to work: `brief`/`agora` go to Overview;
`board`/`objetivos`/`requisitos` to Tasks; `memoria`/`contexto` to Knowledge;
`reglas`/`repos`/`ajustes` to Settings. Objectives, requirements and repositories
open their relevant disclosure. `actividad` opens the activity drawer. Query
details such as issue and room IDs remain usable; browser Back works normally.
The project tools menu retains the existing alternatives/deltas/completion/state
destinations. Source control keeps the project scope and full-screen view.

The presentation change does not rewrite objectives, requirements, memories,
rules, conversations, context links or coordination records. Their IDs,
decisions, revisions and references retain their existing semantics. Tasks
gain editable title, body, assignee, free labels and acceptance criteria. Board
schema v2 adds `acceptance_json` on the first write to a v1 database; read-only
access to v1 remains valid and returns an empty criteria list. Existing rows,
comments, events, links, counters and references are preserved. Export includes
the criteria. Free labels can also filter the board.

## Project skills

Skills can be searched in the bundled library and the user's saved skills,
installed into the project, created as SKILL.md text, imported by file/folder
selection or drag and drop, or imported from supported public GitHub URLs.
A repository URL lists its skill manifests before installation; returned URLs
are pinned to the observed commit. Multiple skill folders are selected one at
a time. Existing local `.agents`, `.claude`, `.faustus` and legacy `.odysseus`
skills remain discoverable. Existing import-review UI remains accessible.

New installations preserve UTF-8 text bundles under `.faustus/skills/`. They
never execute imported scripts. Traversal, drive/alternate-stream paths,
Windows reserved names, case-insensitive collisions, unsupported/binary files,
oversized bundles and overwriting existing skills are refused. A failed
validation cannot leave a partially installed manifest.

Review displays the manifest, supporting files, declared permissions, required
backends and existing static scan findings. Activation uses the existing
review machinery, with approval IDs namespaced by project and exact relative
skill key. The submitted digest must still match. Critical findings retain
their explicit override and privilege-escalation declarations remain refused.
Activation pins are stored outside the skill bundle in
`.faustus/project-skills.json`; a content change invalidates activation.
Deactivation preserves all files. Editing the manifest requires its current
digest, preserves supporting files and never silently renames the skill.
Existing binary resources and large references are listed with their path,
byte size and SHA-256 instead of being decoded as text. Their bytes remain
included in the activation digest. Typing while a manifest save is pending
keeps the newer draft open with the returned digest for its next save.

An activated, unchanged skill is listed in the project's context. Its procedure
is read on demand through the project tool; neither installation nor activation
grants additional runtime permissions or promises a declared backend exists.

## Native tools, API and MCP

The native `project_workspace` tool resolves its project from the chat's trusted
context, ignores model-supplied project IDs and uses the same stores as the UI.
Actions: `instructions_read/write`, `memory_list/read/write`,
`skills_list/catalog/repository`, `skill_read/write/install/import/activate/deactivate`.
`skill_write` needs the digest returned by `skill_read`. The agent can reactivate
a reviewed current digest but cannot approve a new digest through this tool.

`board_create` and `board_update` support `labels`, `assignee` and `acceptance`.
Existing `project_objectives`, `req_*`, `manage_project_context` and ordinary file
tools retain their roles. Spanish/English project management requests discover
the new tool, while chats without a project do not receive project-only tools.

UI/API actions use `POST /api/projects/{id}/workspace/action`; the human review
action uses `/workspace/skills/review-activate` with `{key, digest, override}`.
The registered `project_workspace` MCP server exposes four tools:
`project_workspace`, `project_task_list`, `project_task_create`,
`project_task_update`. All use the normal authenticated project API.

## Verification boundaries

Tests use isolated data and project folders. Store migration, ownership,
native schema/dispatch, digest invalidation, source editing, safe imports and
repository revision pinning have regression coverage. A mounted navigation
check verifies six destinations and all legacy mappings. Browser checks cover
task edits/reload, library/manual/local skill installation, review/activation,
Knowledge, Settings, the activity drawer and responsive layout. MCP validation
uses a real stdio client and the real API. These checks do not claim a live
model's judgement of skill quality or execution of imported third-party scripts.
