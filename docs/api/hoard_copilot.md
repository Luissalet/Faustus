# Shared Hoard copilot

Faustus owns the chat history and agent execution. A Hoard supplies its origin,
data-space identity, native project, active objects and selection. Domain data
and tools remain in that application. The shared panel is the existing
`<faustus-chat>` component, extended rather than copied into every Hoard.

## HTTP surface

The normal local authentication and Full configuration apply. The new facade
does not create another account or approval flow.

| Operation | Route |
| --- | --- |
| Discover contract and panel | `GET /api/hoard-copilot/capabilities` |
| Load the shared bundle | `GET /api/hoard-copilot/panel.js` |
| Resolve the stable project link | `POST /api/hoard-copilot/project-links/resolve` |
| Create a canonical conversation | `POST /api/hoard-copilot/conversations` |
| List by Hoard/data space/native project | `GET /api/hoard-copilot/conversations` |
| Read history and context | `GET /api/hoard-copilot/conversations/{session_id}` |
| Import an existing conversation | `POST /api/hoard-copilot/imports` |

Creation takes `{context, name?}` and returns `session_id`, `project_id`,
`project_link_id`, context and selected global model. Context includes
`origin: {app_id, data_space_id, surface?}`, optional
`project: {external_project_id, title?}`, `active_objects`, `selection`, `view`
and attachment references. The originating conversation stays stable when
another surface continues it. A new turn captures its selection once.

Send through the normal `/api/chat_stream` route with `hoard_context` and
`client_message_id`. The SDK exposes these as `hoardContext` and
`clientMessageId`. Resume and stop use the existing detached-run service.
Closing the panel disconnects its observer without stopping the remote turn.
Integrated conversations inherit the configured global model for each turn.

The references database contains external project links, origins, context and
import mappings. Chat messages are stored only in Faustus's canonical database.
Apps without projects do not need a workspace. Same-title projects in different
data spaces do not merge.

Import takes `{context, source_thread_id, name?, messages}`. Each message has
`source_id`, `role`, `content`, optional `created_at` and metadata/tool/attachment
references. Source IDs make repeated imports idempotent. Dates and undo receipts
are preserved. The canonical message transaction rolls back on invalid dates.
Original Hoard history remains available for migration and recovery.

## Shared component

Build once with `node studio/embed/build.mjs`. Serve the resulting bundle from
the panel route. Hosts provide `.server`, `.fetch` for their existing transport,
`.contextProvider` (synchronous or asynchronous), `.locale` and `.session`.
The component owns conversation selection, creation, deletion, history,
streaming, retries and detached observation.

`.draft`, `.appendDraft(text)` and `.loadHistory()` support editor handoff without
sending a message automatically. `faustus:session-change`, `faustus:event` and
`faustus:turn-end` allow the editor to open or refresh its native objects.
`.onToolUndo(receipt)` invokes the host's native undo when a result supplies an
`_undo` receipt. Tool output is available under a collapsed details element.

## MCP

Run `python -m mcp_servers.hoard_copilot_server` with `FAUSTUS_URL` and, when
needed, `FAUSTUS_TOKEN_FILE` or `FAUSTUS_TOKEN`. The server forwards to the
running app and never owns a second harness. Tools cover project resolution,
conversation list/open/get/import and native turn start/status/stop.

## Scope

This is the shared service and panel foundation. Writer's client integration is
tracked separately in Agora 340. Natural-language project search, global Studio
sidebar filters and the complete family migration remain separate tasks
337, 336 and 343. The facade currently reuses native turn routes instead of
implementing a duplicate execution API. Transport and native tool registration
continue to use the established family/connector capabilities.
