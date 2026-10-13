# Ágora spaces and working modes

This guide describes the completed source implementation. Deployment and
production activation are separate from the private interface verification.

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

The Rooms browser lists conversations and deliberations for the selected
space. Search by room title or create a room with **New room**. On mobile,
open **Rooms** to browse; choosing a room closes the browser. The task view
also offers **Open in a separate window**.

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
