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
`project: {external_project_id, title?}`, `active_objects`, `selection`, `view`, `editor`
and attachment references. The originating conversation stays stable when
another surface continues it. A new turn captures its selection once.

`editor` carries the application's current document revision, unsaved fields and
other editor state. It survives normalization, storage and per-turn freezing,
and reaches the normal agent as domain context. It does not create another
editor or conversation store. App-level views omit `project` rather than sending
`project: null`; new forms do not invent native object identifiers.
Continuing from Faustus without a fresh host snapshot omits old `editor` state,
as it does the previous selection. The original turn keeps its captured context.

Send through the normal `/api/chat_stream` route with `hoard_context` and
`client_message_id`. The SDK exposes these as `hoardContext` and
`clientMessageId`. Resume and stop use the existing detached-run service.
Closing the panel disconnects its observer without stopping the remote turn.
Integrated conversations inherit the configured global model for each turn.

Before a turn, an `origin.app_id` (or `tool_server_id`, or app name) that names an
installed plugin manifest whose tool server is not connected is connected through
the Connectors routes with the in-process token: `POST /api/app-connectors/{id}/connect`
for an existing connector, otherwise `POST /api/app-connectors/adopt` with the port
where discovery finds the running app (`src/hoard_copilot_turn.connect_origin_app`,
one attempt per app every `CONNECT_RETRY_S` = 300 s; it never fails a turn). When no
server of the app is connected, the turn's system hint says so and tells the agent
not to stand in for the app's tools with web searches.

The references database contains external project links, origins, context and
import mappings. Chat messages are stored only in Faustus's canonical database.
Apps without projects do not need a workspace. Same-title projects in different
data spaces do not merge. Native titles may hold any punctuation (`API: /article demo`, a URL used as
the id): only the Faustus sidebar label and folder drop the characters a project name cannot hold
(`<>:"/\|?*` and control characters; an empty result becomes «Untitled»). The context keeps the native
title and the external id unchanged, and identity is the link key, never the label.

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

Hoard panels prepare a canonical conversation when opened, without generating a
turn. Unsent text is saved through the existing session draft API, with independent
drafts per session, flush on detach and retry after connection failures.
`PATCH /api/sessions/{sid}/draft` updates text while preserving Studio attachment
IDs when omitted; the existing PUT replacement and explicit attachment clear remain.

Studio's conversation list includes immutable `hoard_origin`, the family's native
icon through `GET /api/hoard-copilot/apps/{app_id}/icon`, a Hoard filter and grouping
by app identity. Folder grouping and the normal search remain available.

## MCP

Host language changes update the shared composer, conversation controls and
undo labels in place, preserving the current conversation and draft.

`GET /api/hoard-copilot/projects/search` accepts query, app/data-space, explicit
ISO after/before dates and offset/limit pagination. It returns stable native IDs,
matched terms and cited canonical messages. Date ranges filter the actual
messages before matching; evidence outside the range cannot supply a text hit.
`activity_at` retains the latest global activity, while `matched_activity_at`
dates the retrieved work. An empty panel opening does not count. Retrieval uses identity, titles and text; it does not
pretend to be a semantic model confidence estimate.

`GET /api/hoard-copilot/projects/{project_link_id}` recalls chats, recent messages,
the existing project memory scope and optional workspace. Availability refers to
the linked Faustus working project; always read the current native Hoard object
before editing. Missing working projects are reported without recreation; no
historic selection or synthetic Git workspace is restored.

Native agent tools and stdio MCP expose `hoard_project_search` and
`hoard_project_recall`. They use the same storage-owner identity as the API,
including the established local no-login bucket.

Run `python -m mcp_servers.hoard_copilot_server` with `FAUSTUS_URL` and, when
needed, `FAUSTUS_TOKEN_FILE` or `FAUSTUS_TOKEN`. The server forwards to the
running app and never owns a second harness. Tools cover project resolution,
conversation list/open/get/import and native turn start/status/stop.

## Scope

This is the shared service and panel foundation. Writer's client integration is
tracked separately in Agora 340. Global Studio sidebar filters are available;
project recovery is available by identity, titles, text and explicit dates; the
complete family migration remains task343. The facade currently reuses native turn routes instead of
implementing a duplicate execution API. Transport and native tool registration
continue to use the established family/connector capabilities.

## Action completion and failures

The origin hint repeats active object IDs verbatim and asks the agent to execute,
verify and report requested changes. Announced retries participate in the existing
intent supervisor. A Hoard turn can recover from an initial empty provider reply
without requiring a filesystem workspace, using the existing recovery budget.

A terminal SSE error remains a failed run even if the stream subsequently sends
`[DONE]`. The SDK and shared panel preserve that failure. The panel displays the
error and restores the submitted text when the same conversation's composer is
empty, retaining the request identity for an explicit retry. It does not replace
a later draft or automatically resubmit a mutation.
