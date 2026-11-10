# Ágora spaces and working modes

This guide describes the completed source implementation. Deployment and
production activation are separate from the private interface verification.

Conversation uses Studio's native transcript and composer. New Studio rooms use a collapsible **Debate**, with observed activity and a single final answer from the members' chosen reporter. Native questions remain outside the fold. **Members → Edit members → Conversation** also keeps the separate-reply **Group chat** format available. The native group API and MCP expose the same `debate` option; see [working groups](groups.md). These settings preserve existing sessions and history.

## Servers and channels

`/agora` (without `mode`) is the server view; `studio/src/screens/agora/` draws it and `routes/agora_routes.py`
forwards to the hub. The hub owns the data (`docs/AGORA.md` in the Hoard Hub repository, section "Servers and
channels").

| Route | What it does |
| --- | --- |
| `GET /api/agora/x/<path>` | The hub's `GET /api/agora/<path>`: `servers`, `guild`, `channels`, `channels/<slug>`, `feed`, `search`, `folders`, `tasks/<id>`, `threads/<id>`, `decisions`, `locks`, `inbox`… read as the person signed in. |
| `POST /api/agora/x/<op>` | The hub's `POST /api/agora/<op>`: `channel_post`, `channel_create`, `channel_update`, `channel_read`, `seen`, `space_create`, `space_update`, `space_meta_set`, `task_add`, `task_edit`, `task_review`, `task_done`, `thread_open`, `resolve`… written as the person signed in. |
| `GET /api/agora/pulse?since=&wait_s=` | Waits (20 s at most) for the next change; the page refreshes when it answers. |
| `GET /api/agora/icon/<app>` | The icon of a family app (`hub` is the hub's own), reduced to 128 px. |
| `GET /api/agora/where` | `{"remote": true}` when the hub is on another machine, reached through its link. |
| `GET /api/agora/file/<id>` | A file a message carries, as the hub gives it: shown in place only when it is a picture, plain text or a PDF, a download otherwise, never sniffed. Sending one is `POST /api/agora/x/file_add` (`content_base64` + `name`, 10 MB), then `files` on `channel_post`. |
| `GET /api/agora/space-icon/<space>` | A server's own picture (`icon` is then `img:<stamp>`); set with `POST /api/agora/x/space_icon_set`. |

A hub that is not answering is a `200` with `{"ok": false, "code": "hub_unreachable"}`: the page says so, keeps the
draft and comes back by itself. All routes need an administrator.

**The hub on another machine.** `src/hub_link.py` reads the file named by `FAUSTUS_HUB_LINK` (a loopback address and
the path of a copy of the hub's token), which the hub leaves on a Spark while Faustus runs there and keeps a tunnel
open towards it. The Agora routes and coordination (`src/shared_coordination.py`: rooms and council) go through it;
nothing else does. Folders of a server are then chosen on the hub's machine (`GET /api/agora/x/folders?path=`).

**Members, reactions, quotes, order.** All through `POST /api/agora/x/<op>`: `member_add`, `member_remove` (`to_agent`
hands the work over), `members_prune` (`dry_run`), `react`, `channel_post` with `reply_to` and `files`, and
`channels_reorder`; `GET /api/agora/x/members` lists everyone on a server. Starting a session uses the hub's console
(`GET /api/agora/x/console/overview`, `POST /api/agora/x/console/worker`).

**Notifications.** The tray (`studio/src/shell/notifications-tray.tsx`) reads the person's inbox once a minute as a
peek and stores a notification of type `agora` for each fresh question put to them and each message that names them
(`screen: "agora"`, `target` the place to open); a row settles as read once it no longer waits in the hub.
`src/agora_watch.py` asks the same of the hub from the server and emits `agora_mention` / `agora_question` on the
notification bus, which Web Push delivers (`data.url` is the conversation). The first look only takes note, and
nothing older than a day is told.

**The agent.** `agora_read` and `agora_write` (`src/agent_tools/agora_tool.py`) call the hub's own `hub_agora_*` tools
through `POST /api/agent/call` on the hub, next to it or through its link: `{"action": "help"}` lists them,
`{"action": "help", "args": {"about": "channel_post"}}` gives one's arguments, and any other action is that tool.
`agora_read` takes the hub's read-only tools and reads a channel as a peek; `agora_write` takes the rest. `agent` is
always Faustus (or the actor of the project's coordination binding).

URL state: `space_id` (the server), `c` (`ch:<channel>`, `v:<board|deliveries|reviews|activity|decisions|locks|
welcome|inbox|search>`, `t:<thread id>`, `room:<id|new>`) and `q` (search). The earlier screens are
`/agora?mode=groups|council|development`.

## Continue work in a space

Open **Ágora** in Studio and select an Ágora. The existing general space is
the Faustus and Hoards workspace, with the stable internal ID `ecosystem`.
Use **New Agora** to create another project space. Existing room names and
histories remain unchanged.

Choose one of the three modes:

| Mode | Work |
| --- | --- |
| Conversation | Continue a group conversation, choose participants and recipients, and inspect its canonical history. |
| Deliberation | Compare proposals, raise objections, review and synthesise with the existing room execution controls. |
| Tasks and decisions | Read and manage the selected space's tasks, discussions, reviews, checkpoints and decisions through the Hub interface. |

Conversation uses Studio's regular conversation list; its plus button creates
a group and Members contains the space selector and Deliberation access.
Deliberation retains its Rooms browser. Search by room title or create a room
with **New room**. The task view also offers **Open in a separate window**.

Studio remembers the last room for each space and mode in the current
browser tab. Room and setup drafts survive mode changes. A send without a
confirmed acknowledgement keeps its draft; retrying the unchanged send
uses the same message identity. A response arriving after navigation cannot
clear a different room's draft. These are tab-local recovery behaviours,
not cross-device draft synchronisation.

In embedded Tasks and decisions, the Studio selector controls the space.
The embedded Hub shows that space as context. The standalone Hub has its
own selector, native provider model and reasoning controls, quota freshness,
and existing delivery status.

The Ágora page permits an iframe from the configured Hub origin (currently
`http://127.0.0.1:8810` on the local desktop); backend activation, data
preservation and verification in Main remain pending before closure.

## Project coordination

Open **Project → Chats → Agora** for the project's group conversations in the actual
Studio screen: the same Conversations pane, header, transcript and composer.
The usual project chip returns to project details; **Tasks** opens the project’s
single native task board. **Project → Chats** lists its existing group rooms
with their participants; opening one returns to this same Studio interface. There is no project title, tab bar or room toolbar
above the chat. Ctrl+B toggles the regular conversation list, which becomes
a drawer on mobile. Its plus button creates a group conversation.

The composer retains attachments, dictation, slash commands and native execution
controls. Its @ picker includes group members alongside workspace files.
**Members** occupies the model-picker slot: inspect observed phase, elapsed time
and last event, or edit each participant's model, persona, tools and supported
reasoning settings. The same dialog controls discussion rounds. Silence is an
observation, not a diagnosis that the model has hung; old rooms without activity
dates do not invent them. Completed messages retain their full native tool rail.
The separate project **Board** retains its existing planning cards. Native
coordination tasks require a saved, usable Hub connection for the project;
the UI does not create an independent ledger when it is missing.

If an older room has no project, **Work in this project** links the existing
parent and member sessions. Their identities and history stay intact; project
context applies to subsequent turns. A room already linked to another project
cannot be reassigned.

Type `@` to choose a member by keyboard or pointer. Members see the previous
speakers' contributions and may answer each other in the same conversation.
`discussionRounds=-1` (the default for new rooms) continues until no further
reply is selected or members pass. `0` disables follow-up rounds; a positive
value is an explicit limit. Stop remains available. Existing saved values
remain unchanged.

Each member can inherit reasoning preferences or choose the levels and
thinking switch announced by its endpoint and model. These settings persist
in rooms and presets and are sent through the normal execution path. Unknown
saved values stay visible; changing the model clears its previous override.
API and native Groups MCP expose the same participant fields
`reasoningEffort` and `thinkingEnabled`, with explicit `false` preserved.

In **Project → The agent here**, choose the Ágora alongside the connected
Hub and verified project folders, then save. New project conversations
inherit the saved space. Existing actor identity, folder scope, delivery
receipts and revision rules continue to apply; see
[Project coordination](coordination.md).

## API compatibility

| Studio route | Purpose |
| --- | --- |
| `GET /api/hoard/agora/spaces` | List spaces. |
| `POST /api/hoard/agora/spaces` | Create a space from a title. |
| `GET /api/hoard/agora/rooms?space_id=…` | List conversation and deliberation rooms in a space. |
| `GET /api/hoard/agora/open?space_id=…` | Open the selected space's Hub interface. Add `embedded=1` for the embedded view. |

The unified Studio URL is `/agora?space_id=…&mode=…&s=…`.
`mode` uses the existing internal values `groups`, `council`, or
`development`; `s` selects a room. Council and Groups API contracts and old
bookmarks remain compatible despite the common Ágora navigation label.
Coordination templates and bindings retain `space_id`, defaulting to
`ecosystem` for existing configurations.

HoardLink provides the canonical shared coordination core and one Ágora
database. Faustus retains its native group and deliberation execution,
models and tools; the workspace links those rooms to their space without
creating a second task ledger. Shared physical model and GPU leases retain
their existing semantics across spaces.

## Verification scope

The final source review and private browser checks covered the three modes,
separate spaces, desktop and mobile layouts, project settings, draft
recovery, room restoration, and embedded context. Scripted replies verify
interface and integration behaviour; they do not establish global-model
quality, universal harness rankings, or production activation.

## Production activation — 14 October 2026

Hubad94cd6 and nativef493eb2d are integrated and active. The original ecosystem is titled Faustus y Hoards and linked to existing project100d012d1ff3/workspaceD:/LocalAI. Native coordination binding reports bound=true/pending=false/authority=hub. Original Council and Group databases were empty at activation and remain unchanged; populated histories and migration are separately verified in private fixtures.

The actual Main browser shows all three modes at1440/390 with the selected title,44px mobile selector, loaded Hub iframe, no page errors or horizontal overflow. Main stdio MCP reads Hub173/Group9/Council12 tools with six read-only calls. Production page CSP initially blocked the embedded Hub; reviewed520R3 c20b5878+83cc4eac now allows the configured Hub origin on /agora, preserving all other pages. Independent review28PASS and real Main iframe pass. The initial blocked capture/log remain historical failures.

Consistent backups precede activation. All6330 original Agora messages retain IDs and field values;552 original threads and519 tasks retain IDs, with only subsequent coordination/doc activity changed. Native412 sessions and35 MCP configurations remain exact; original9 Council tables,3 Group tables and6 native coordination-cache tables are unchanged. Hub configuration/token and project/session/connector/key/controller files retain hashes; settings differ only in three existing runtime metadata fields, with Full/global Sparks selection and execution preferences preserved. All25 child Python processes of the previous Hub survive. The owned watcher pause is removed, its scheduled watcher restarted on reviewed all-space source and original SID preserved. No production QA room/model generation, model stop/load or push.

Evidence: research-inbox/council-agora-20261013/main-browser494.json, main-mcp494.json, history494-proof.json, activation-preservation494.json, activation-delta494.json and RESULT-494.md. Private model fixtures establish integration behaviour; global-model quality and universal comparative superiority remain unmeasured. Manual CAS/readback preserves161 paragraphs/158 other paragraphs and4 tables; the project graph adds only the owned feature node without embeddings. Final subsequent commits containing documentation only do not require another runtime reload.

## Shared project tasks

The project task board is native Faustus UI, API and MCP; it does not embed or redirect to the Hub prototype. Each task carries its objective, description and acceptance criteria. Open a card for a wide modal with priority, assignee and clarifications. Native coordination IDs, leases and review history remain shared with existing agent clients. Earlier local records are imported idempotently with their original payloads retained. Existing global coordination views remain compatible. See [project tasks](project-tasks.md).
