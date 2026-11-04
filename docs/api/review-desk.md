# Review desk (`review_desk`)

One desk per deliverable: versions as snapshots, comments anchored to the version they were made on, a state per correction and one delivered version. Hangar proposal E32; anchors are the first part of E22.

| Surface | Where |
|---|---|
| Chat tool | `review_desk {action, ...}` |
| MCP | built-in server `delivery`, tool `review_desk` |
| HTTP (admin) | `GET/POST /api/review-desks`, `GET /api/review-desks/{id}`, `GET /api/review-desks/{id}/compare?a=&b=`, `GET /api/review-desks/{id}/versions/{n}`, `POST /api/review-desks/{id}/{add_version,comment,import_feedback,set_status,deliver}` |
| Studio | Delivery → **Reviews** |

## Actions

- `create {title, path?, commission?, milestone?}` – a desk, optionally with v1.
- `add_version {id, path, note?}` – copies the file to `DATA_DIR/review_desks/<owner>/<id>/vN.<ext>` (up to 512 MB), hashes it and reopens it with the delivery readers. Identical content is refused (`desk.same_content`).
- `comment {id, text, version?, anchor?, author?}` – the version is the one given, the one named in the text (`v2`, `versión 2`, a version's file name) or the latest. `about_older` is true when it is not the latest. Each comment gets `ref` = `review:<id>@v<n>#<anchor>`.
- `import_feedback {id, text, about?, author?}` – one comment per line; a heading line ending in `:` that names a version applies to the lines below.
- `set_status {id, comment, status, fixed_in?, note?}` – `open`, `in_progress`, `fixed` (records `fixed_in`, default latest), `wontfix`, `answered`.
- `compare {id, a?, b?}` – reader facts, size, unified text diff for text, `.docx`, `.xlsx` and `.pdf`, and the comments made on `a`.
- `deliver {id, version?, force?, note?}` – the one delivered version; refused with open comments (`desk.open_comments`) unless `force`.
- `show` / `status` – status: latest, counts per state, open comments on the latest and from older versions, the deliverable (`v2 name sha256:…`) and whether the delivered snapshot and the source still match.

## Anchors (`src/anchors.py`)

| Kind | Canonical | Everyday wording |
|---|---|---|
| page | `p3`, `p3@x,y,w,h` | `página 3`, `pág. 3`, `page 3 (x,y,w,h)` |
| time | `t=00:01:10-00:01:25` | `min 1:10`, `1:10-1:25` |
| cell | `Sheet!B4`, `cell:B4` | `celda B4` |
| object | `obj:Tapa` | `pieza Tapa`, `part Lid` |
| line | `L120`, `L120-140` | `línea 42`, `lines 10-20` |
| symbol | `sym:compute_total` | `función compute_total` |

`anchors.ref(desk, version, anchor)` and `anchors.parse_ref(ref)` turn an anchor into a stable identifier and back.
