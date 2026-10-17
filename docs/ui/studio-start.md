# Studio start

Studio is the single destination for starting and continuing conversations.
The former Home URL (`/`) redirects to `/studio` with its query and fragment
intact, including session, project and draft links. Desktop navigation has
six primary destinations; mobile has Studio, Projects, Library and More.

An empty, ordinary Studio conversation shows the existing personal assistants,
pending decisions, pinned automation results and recent projects above the
same composer used for every conversation. Quick actions append to the actual
draft. Personal tools still open mail, calendar, notes and messaging. Recent
conversations remain in the desktop sidebar and also appear in the mobile
start content. The existing welcome preference controls the introductory
text and quick actions. Incognito and group starts retain their own surfaces.

Workspace overview discloses the existing repository radar, fleet and usage
recap. Those components load only when expanded. Failed start requests offer
retry without blocking the conversation. Empty pinned-card lists take no
space. Existing assistant refresh, create, adopt, edit and removal controls
are reused without changing their ownership or chat identity.

There is no data migration. Home cards retain their existing `home_cards.json`
store, `/api/home/cards` API and `manage_tasks.pin_to_home` tool field. The UI
calls the destination Studio. Unpinning removes the shortcut, preserving the
scheduled task and its runs. Assistants retain the native mobile API and
`personal_assistants` MCP tool, canonical session IDs, project and messages.
The assistant profile/scheduling work in task 724 owns its existing component
and backend; this change mounts that component rather than duplicating it.

Verification: `studio/checks/studio-start.check.mjs` mounts real components,
exercises route compatibility, draft appending, lazy secondary requests,
assistant navigation, retry, forced refresh and confirmed unpin transport.
Existing navigation, home-card and recap checks, TypeScript, translation
consistency, production build and backend card/assistant tests remain required.
Browser and native MCP evidence uses an isolated data directory; a simulated
provider can verify chat transport without loading a model.
