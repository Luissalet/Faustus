# Delivery packages (Hangar E33)

`src/delivery_package.py` builds a reproducible delivery for a client: the editable
sources, the exports, the data sources the owner explicitly authorised to share,
instructions, examples and the real limitations, in one folder plus a zip with
`ENTREGA.md` and `manifest.json` (SHA-256 per file).

A package is **ready** only when:

1. every editable and every export opens with a reader for its format, read from
   the source before copying;
2. the zip, extracted into a scratch folder, gives the same SHA-256 and the same
   reader verdict for every item (the package exactly as the client receives it);
3. instructions exist (text or an `instructions` file) and limitations are stated;
4. no named file was missing or refused.

Readers are structural: STL (binary/ASCII, triangle count and bounding box), OBJ
(face indices in range), GLB/glTF (header, JSON chunk, BIN length, external
resources present), 3MF (model part, objects, triangles, unit), PLY, STEP
(ISO-10303-21 sections and entities), PDF/PPTX/DOCX/XLSX/CSV and images through
`src/deliverable_inspection.py`, HTML, JSON, Jupyter notebooks, Python (compiles),
text and ZIP. `.blend`, FBX, video and audio are checked by container signature
only and say so (`level`). A format without a reader is `not_checked`, which keeps
an editable or export from being ready. Readers never judge visual quality or
correctness.

Data items need `authorized: true` (or `(authorized)` at the end of a text line);
otherwise they stay out and are listed under *excluded*. A software repository
(`software_root`) contributes the verified client handoff that
`code_graph_handoff` already wrote (`.handoff/HANDOFF.md` and the report); this
module never runs a repository's commands.

Storage: `DATA_DIR/delivery_packages/<owner>/<id>/` (folder, zip, manifest).

## Surfaces

| Surface | Name |
|---|---|
| Chat tool | `delivery_package` (`action` build, list, show, reverify, readers); paths confined to the active workspace like `read_file` |
| HTTP (admin) | `POST /api/delivery-packages`, `GET /api/delivery-packages`, `GET /api/delivery-packages/readers`, `GET /api/delivery-packages/{id}`, `POST /api/delivery-packages/{id}/reverify`, `GET /api/delivery-packages/{id}/download`, `GET /api/delivery-packages/{id}/document` |
| MCP | built-in server `delivery` (`mcp_servers/delivery_server.py`), tool `delivery_package`; owner from `ODYSSEUS_MCP_DELIVERY_OWNER` |
| Studio | **Delivery** (`/delivery`, also in Project → Project tools): new package form (role per file, "May be shared" for data, instructions, limitations, software repository, document language), list, detail with the check from the source and from the zip per file, gaps, excluded files, download, ENTREGA.md and "Reopen the zip again" |

HTTP and MCP accept any local path except the sensitive ones `read_file` refuses.

## Example

```json
{"name": "Caja sensor - ficha 3D",
 "items": [{"path": "E:/encargo/enclosure.cad.json", "role": "editable"},
           {"path": "E:/encargo/enclosure.stl", "role": "export"},
           {"path": "E:/encargo/circuito.json", "role": "data", "authorized": true}],
 "instructions": "Abrir las recetas en Vulcan; STL en milímetros.",
 "limitations": ["Sin prototipo físico."]}
```

## Limits

- Structural reading only: a PDF that parses may still look wrong.
- No conversion or export is produced here; the package carries what exists.
- Files are copied, so very large deliveries take disk space under `DATA_DIR` until removed.
