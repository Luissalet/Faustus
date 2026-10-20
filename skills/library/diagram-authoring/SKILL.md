---
name: diagram-authoring
description: Create, edit and explain revisioned diagrams and paginated infographics through Vitruvius's native editor. Use when the user asks for an editable flowchart, architecture, concept map, organisation chart or «infografía editable».
version: 1.1.0
category: engineering
tags: [hoards, vitruvius, diagram, diagrama, flowchart, esquema, organigrama, editable]
status: published
source: imported
---

## When to Use

Use when relationships and editable objects matter: a process, decisions,
architecture or a concept map. Vitruvius → Design → Diagrams owns this
semantic document, independently of a software project. Daguerre prepares
free vector illustrations; Cicero consumes figures in presentations.
An exported bitmap is an illustration, not the graph's editable source.

## Procedure

1. Discover `diagram_document_list/get/save/edit/undo/history/compare/import`
   on the configured Vitruvius owner. Discovery leaves modules stopped.
   Enable Design only as part of the requested work through the available
   native module operation. If the installed catalogue lacks these tools,
   report that build's limitation; do not invent a tool or start a retired app.
2. Read the current document and its revision before editing. In the common
   Faustus panel, use the selected document, selected node/edge and current
   draft supplied by the editor. For a new diagram, save `content.diagram`
   with its existing type, title, nodes and edges. Give nodes and edges
   stable IDs; connections name their `from` and `to` node IDs. Blank graphs
   are valid starting documents. Authored arrows express the supplied
   relationships; they are not proof of measured runtime dependencies.
3. Keep structure separate from presentation. `view.nodes` holds geometry,
   dimensions and pinning by node ID; `view.edges` holds ports and intermediate
   points by edge ID. Preserve manual positions and pinned objects unless
   the requested edit requires changing them. Edit specific objects with
   the revision actually read, rather than regenerating the entire drawing.
   For example, after reading revision3:

   ```json
   {"id":"012345abcdef","base_revision":3,"edits":[{"collection":"nodes","id":"review","values":{"label":"Comprobar la fuente"}},{"collection":"view_nodes","id":"review","values":{"x":260,"pinned":true}}]}
   ```

   For editorial composition, retain the same graph and set
   `view.kind:"infographic"`. `view.pages` holds objects with stable `id`,
   `title`, `order`, `width` and `height`; each `view.nodes[id].page_id`
   selects its page. Node view supports `font_size`, `font_weight`, `align`,
   `ink`, `border` and `show_label`. `view.assets` embeds PNG/JPEG/WebP
   data URIs by asset ID, referenced from a block's `asset_id`. Object-edit
   collections include `pages`, `assets` and `view`; view properties use
   action=set. Move or remove a page's blocks before removing it through
   native edits. The UI also offers page duplication and four editorial
   templates: comparison, timeline, process and relationships. The common
   panel includes the current draft and selected page. Saved renders report
   text/bounds findings without refusing the save.

4. Independent fields merge. On a same-field conflict, retain the person's
   draft and its base, read the conflicting current field and reconcile
   that field according to the request. Do not retry blindly using the
   newest revision or replace the whole document. `undo` targets a published
   revision selectively; the visual editor also maintains local undo/redo.
5. Open the returned `editor_url` for human editing. Check labels, endpoints,
   groups and manual geometry in the actual diagram; inspect a small and
   large viewport when sharing. Use history and compare for the requested
   revisions. The returned viewer is self-contained HTML with keyboard/click
   selection and authored upstream/downstream traversal.
6. Deliver the requested export at an explicit revision and retain native
   JSON `vitruvius.diagram/1`. SVG/PNG/PDF freeze a figure; draw.io and Mermaid
   preserve only the documented subset. Read import warnings and retain the
   imported original. Opening the exported source as a new document does
   not import server-side history. Do not claim linked insertion into another
   Hoard until that consumer's operation has actually been exercised.
   For editorial documents, SVG/PNG stack pages vertically and PDF retains
   each page's own dimensions. draw.io preserves the documented page/style/
   raster subset; cross-page semantic links use a native extension. Mermaid
   retains graph relationships rather than the editorial page composition.

## Pitfalls

- An empty canvas, an automatic layout and a saved revision are different states.
- A local draft in the panel may be newer than the server revision.
- Changing node IDs breaks references; copying pixels does not preserve relations.
- Full Canva/Visio fidelity and authored isometric state simulation are not
  implied by the native editor. Approximate character wrapping is not a
  desktop-publishing text-layout engine.

## Verification

- Read back the saved revision and exact requested fields, including unrelated edits.
- Verify the visible figure and actual exported file, not only a successful tool response.
- Return editor/source/viewer links from the native result, with their revision.
- Name untested imports or consumers accurately; do not infer compatibility.

## Provenance

Original procedure for the native editor. Research comparisons: Archify
`25cf3708` (MIT), diagram-design `f2f4d49e` (MIT) and Anatomy `1482e846`
(MIT). No upstream authoring service or CLI is required. The native tools
and their documented exchange subset are the operational contract.
