# Connected Blender scenes

Faustus uses Blender Lab's official MCP extension and Python tools, extended
with native asset jobs, recipes, installed RNA queries, node editing, animation
inspection, review contact sheets and clip-scoped exports. Blender 5.1 or later
is required; real verification used the installed Blender 5.2.2 LTS.

## Install

Run `Install-BlenderBridge.ps1`. It selects Blender 5.2 when installed, verifies
the official 1.0.3 extension ZIP and MCPB hashes, installs the official Python
distribution into Faustus's own environment and enables the extensions through
Blender's extension manager. `-BlenderPath` chooses another supported executable.
`-PackagesPath` selects downloaded release packages for installation without
network access. No unqualified package with a similar PyPI name is installed.

The official packages are available at
<https://projects.blender.org/lab/blender_mcp/releases/tag/v1.0.3>.
The extension ZIP is `mcp-1.0.3.zip` (SHA-256
`a7a9da816192502e5a0a202a396444e266b47d8fc4f74ad4698048bd43040707`);
the MCPB is `blender-1.0.3.mcpb` (SHA-256
`d6fe04dd17767f7c7fb452142f1f0e6787db575db49f3976a166142683b5b97e`).

The Faustus sidebar opens the shared copilot using its canonical icon. Its
connection starts the official socket engine on loopback, including when
Blender's Internet access is disabled. It reuses the official polling and
execution modules; it has no second HTTP server or bpy execution queue.
No hosted API, asset subscription or external model is required. Text reasoning
uses the configured global Sparks service; this extension does not load a text
model on the PC.

## Tools and routes

The existing five typed scene tools remain available. The same built-in MCP
server also exposes four native live tools and all 26 tools from the installed
official distribution under `blender_official_…`, preserving their input and
structured output schemas. Official tools cover scene/object summaries, missing
resources, linked libraries, API/manual search, screenshots, navigation, renders,
and Python in the connected instance or a background Blender process.

The agent's `blender_scene` tool gains `official_tools` and `official_call`
(`name`, `arguments`) plus `live_probe`, `live_submit`, `live_job` and
`live_cancel`. Discover the real official schemas before calling a tool.

| Route | Purpose |
| --- | --- |
| `GET /api/blender/official/tools` | Installed official schemas |
| `POST /api/blender/official/call` | `{name, arguments}`; text and optional images |
| `GET /api/blender/live/probe` | Connected version, file and execution mode |
| `POST /api/blender/live/jobs` | `{operation, arguments, port?}` |
| `GET /api/blender/live/jobs` | Job history |
| `GET /api/blender/live/jobs/{id}` | State, partial results and diagnostics |
| `POST /api/blender/live/jobs/{id}/cancel` | Request cancellation |

Native operations:

| Operation | Arguments/result |
| --- | --- |
| `scene` | Optional object names; selection, transforms, materials, modifiers, bones, actions |
| `execute` | Python `code` with bpy; assign `result`; stdout/stderr retained |
| `api` | `identifier`: `bpy.types.Name`, `bpy.ops.category` or `bpy.ops.category.operator`; installed RNA, defaults, enums, context availability |
| `nodes` | `owner`: group/material/world/light/compositor/modifier; `name`, optional `modifier`; optional `ops` add/remove/set/link/unlink; stable socket identifiers, links and interface returned |
| `animation` | `object`; actions with slots, layers, curves and keys, active action, NLA and drivers |
| `asset_check` | Optional selected object names; base-mesh topology, UV coverage/degenerate faces, rig weight totals, materials, shape keys and units, without mutation |
| `views` | Object names, angles, frames and dimensions; rendered PNGs and contact sheet; restores camera, frame, selection and render settings |
| `save`, `load` | Explicit local `path`; editable native `.blend` |
| `export` | `path`, format GLB/glTF/FBX/STL/OBJ, object names, selection; optional `actions: {object: [clip names]}` |
| `recipe_save` | Stable `name`, distinct named steps containing Python code |
| `recipe_list` | Saved recipes and their digests |
| `recipe_run` | Name and optional stage names; frozen recipe and per-stage output |

Explicit export clips are temporarily bound to their owner through Blender's
NLA tracks. Original actions, slots, track mute states and selection are restored
even after an error. Exporting one rig does not scan every compatible action
belonging to another rig. Existing active actions and NLA tracks remain usable
when no explicit clip mapping is supplied.

## Jobs and resources

Each job has its own local worker, independent of the originating chat/MCP
connection. Records, recipes and review files live under
`%LOCALAPPDATA%/Faustus/blender_bridge`, or `FAUSTUS_BLENDER_DATA_DIR`.
The official TCP server executes bpy on Blender's main thread. Cancellation
markers are readable between recipe stages and review views even while that
server is busy. An individual synchronous script/export/render must finish
before cancellation takes effect. There is no forced termination of a user's
Blender process and no automatic replay after loss of the worker.

Review rendering requests and renews a shared Hub GPU memory lease, releasing
it on completion or failure. If the Hub is unavailable the job reports that
resource coordination was unavailable. Free Python can perform other Blender
operations; callers remain responsible for requesting the appropriate resources
when their own script runs a heavy render or bake.

Official background tools use the supported installed Blender generation and
closed stdin, so a child Blender cannot consume the MCP JSON-RPC input stream.
The typed scene pipeline retains its existing validation, private output paths
and timeout settings. Its restrictions do not describe the full live bpy API.

## References and verification

Official distribution 1.0.3, source commit
`2cea8d566dde07fbac28a61d698909d69724e853`, GPL-3.0-or-later; subsequent main
commit `dbbf836ad4b1025f14a2b3b504c43903f39e0b04` includes the license text.
No official engine code is copied into the Faustus repository; it is installed
as the selected local engine dependency.

Other copilots studied: ageless-h/blender-mcp
`19bfd0f06bebc23a06a240ac9ae731081abec9f9` (MIT), dwgx/blender-copilot
`ca345c1aea977d9103df0ceca6fcff9fe3865951` (MIT), RFingAdam/mcp-blender
`71724fc63d14bef6a08a4ac6dc6361b474b6fd27` (AGPL-3.0-or-later), and
bpy-dev/blender-mcp `e8ac4088d4a3e0469d3eff9f9bb2dd717e899219` (GPL-3.0).
Workflow ideas include deep node/animation readback, declarative edits and
installed API discovery. Their published tool counts are not parity claims.
Their transports, cloud asset integrations and hosted generators are not used.
Source review found direct legacy `action.fcurves` access in the pinned
dwgx interpolation helper; the native action reader instead uses Blender 5.2
layers, strips and channel bags, verified in the installed engine. Node/socket
IDs and runtime RNA discovery avoid relying on translated display names or
remembered operator signatures. Mesh/UV/weight observations were added as
native asset checks; no pixel score from another copilot is treated as proof
of modeling quality.

Real MCP verification exercised both typed tools and official scene/docs/CLI
tools, plus two original articulated assets, three distinct clips each, selection
preservation, editable save/reopen, nine-view review, GLB skins/materials/clips,
Geometry Nodes, action inspection, selective recipe replay, cancellation and
retained failure diagnostics. The official screenshot tool captured the actual
Blender window and Faustus panel while `bpy.app.online_access` was false.
Synthetic assets verify operations, not artistic quality or autonomous modeling
quality from a live Sparks evaluation.

The live global Sparks evaluation attempted a complete articulated robot and
three clips three times, using JSON and plain Python response formats. The long
generation requests reached their 600-second
timeout without returning code; a separate short request returned coherent
text. These attempts do not establish autonomous asset quality. They did not
load a fallback text model or change the existing QA scene.
