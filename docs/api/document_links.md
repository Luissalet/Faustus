# Wiki-links, backlinks y comentarios anclados en Documents (W1-E: ADP-06 + ADP-05)

Dos piezas independientes sobre los mismos `Document`/`DocumentVersion`
(`core/database.py`) que ya usa la Library del Studio — ninguna crea un
segundo almacén de documentos, ambas solo proyectan lo que hay en
`Document.current_content`.

- **`src/document_links.py`** — enlaces `[[Título]]` / `[[doc:<id>]]` /
  `[[Título|alias]]` entre documentos, con backlinks, sobre un índice SQLite
  propio y reconstruible.
- **`src/document_comments.py`** — comentarios anclados a una cita +
  contexto dentro de un documento, con reubicación al guardar y aceptación
  de una propuesta `find → replace` acotada al propio anclaje.

Ambas se enganchan en el mismo punto de guardado:
`routes/document/document_routes.py::update_document` (el `PUT
/api/document/{id}` existente) llama, tras el commit del contenido, a
`document_links.rebuild_for_document` y a
`document_comments.relocate_comments_for_document`. Un fallo ahí nunca hace
fallar el guardado del documento — ambas proyecciones son reconstruibles, así
que en el peor caso quedan desactualizadas hasta el siguiente guardado o una
llamada explícita de rebuild/relocate.

## `Document` no tiene `project_id`

`core/database.py::Document` solo tiene `owner` y `session_id` (nullable,
`SET NULL` si la sesión se borra) — nunca `project_id`. Por eso las rutas de
este lote son `/api/documents/...`, owner-scoped (igual que
`routes/document/document_helpers.py::_owner_session_filter`), **no**
`/api/projects/{id}/documents/...` como sugería el boceto de la ficha
ADP-06. Confirmado también en `ADP01_INVENTARIO.md` (fila ADP-06: "sin
`[[`/wikilink" y sin concepto de proyecto en documentos). Puntos para el
orquestador si en otro lote se añade `project_id` a `Document`: las
consultas de `resolve()`/backlinks tendrían que añadir ese filtro además del
de `owner`.

## Wiki-links (ADP-06)

### Sintaxis

- `[[Título]]` — resuelve por título, case-insensitive, **exacto** (nunca
  "parecido").
- `[[doc:<id>]]` — resuelve por id estable. Sobrevive a un renombrado del
  documento destino; un enlace por título al título antiguo se rompe (es lo
  esperado: un título no es una identidad estable).
- `[[Título|alias]]` — el alias es solo texto de presentación, no cambia la
  resolución.

### Resolución (`resolve(sa_db, owner, token)`)

Cuatro estados, nunca una adivinanza:

| status | significado |
|---|---|
| `resolved` | exactamente un documento del mismo `owner` coincide |
| `ambiguous` | más de un documento del mismo `owner` coincide por título; `candidates` lista TODOS los ids |
| `broken` | cero coincidencias (o el id/título pertenece a OTRO owner — indistinguible de "no existe": nunca se filtra existencia entre owners) |
| `rejected` | el destino contiene un segmento `..` (intento de traversal); nunca se resuelve |

La resolución siempre filtra por `Document.owner == owner` del documento
ORIGEN — un enlace nunca cruza owner ni "proyecto" (que aquí es lo mismo,
ver arriba).

### Índice (`DATA_DIR/document_links.sqlite3`)

Mismo patrón que `src/project_board.py`/`board.sqlite3`: fichero SQLite
propio, `BEGIN IMMEDIATE` por escritura, `PRAGMA user_version` para
versionado de esquema. Tabla `links(source_doc_id, owner, raw, target_type,
target_value, alias, status, resolved_doc_id, candidates_json, position,
updated_at)`. **Reconstruible**: `rebuild_for_document`/
`rebuild_all_for_owner` recomputan siempre desde `Document.current_content`
— nunca hay una escritura incremental que pueda derivar del contenido real.

### Rutas (`routes/document_links_routes.py`)

- `GET /api/documents/{doc_id}/links` — enlaces salientes de ese documento
  (`{document_id, links: [...]}`, cada uno con `status`/`resolved_doc_id`/
  `candidates`).
- `GET /api/documents/{doc_id}/backlinks` — documentos que enlazan a este
  (solo enlaces `resolved`, scoped al owner del documento consultado).
- `POST /api/documents/links/rebuild` — recomputa el índice completo de
  TODOS los documentos del owner autenticado. Pensado para recuperación
  (índice borrado/corrupto) o para documentos guardados antes de que este
  índice existiera.

## Comentarios anclados (ADP-05)

### Modelo (`core/database.py::DocumentComment`)

`id, document_id, owner, base_version, quote, before_ctx, after_ctx,
structural_pos, body, author ∈ {human, model}, state ∈ {open, resolved,
orphan}, proposal_find, proposal_replace`. Tabla nueva, creada por
`Base.metadata.create_all()` en `init_db()` (mismo mecanismo que
`ArtifactRow` — ver `core/database.py::_migrate_create_artifacts_table` —
por eso este lote NO añadió una función `_migrate_*` explícita: una tabla
que nunca existió no necesita `ALTER TABLE`, solo aparecer en
`Base.metadata`).

### Anclaje: cita + contexto, nunca offset

Un comentario no apunta a un índice de carácter (se queda obsoleto en la
siguiente edición) ni "a la primera aparición de este texto" (falla en
cuanto el texto se repite). Apunta a `quote` + `before_ctx`/`after_ctx` (40
caracteres a cada lado, capturados al crearlo). `document_comments.relocate`
vuelve a buscar ese mismo anclaje contra el contenido ACTUAL:

- la cita aparece una sola vez → reubicado ahí, sin ambigüedad.
- aparece más de una vez pero `before_ctx`/`after_ctx` acotan a una sola
  ocurrencia → reubicado ahí.
- la cita desapareció, o sigue repetida y el contexto no desambigua →
  `orphan`. Un comentario huérfano nunca se pega a otro párrafo solo porque
  tiene que apuntar a algún sitio; un huérfano puede sanar más tarde si el
  contexto vuelve a ser único.

`structural_pos` (índice de bloque markdown, bloques separados por línea en
blanco — `document_comments.block_spans`) es una pista barata para la UI
("salta aproximadamente aquí"), nunca la fuente de verdad: la reubicación
siempre deriva la posición de la cita, jamás de `structural_pos` guardado.

### Aceptar una propuesta (`accept`)

`accept(sa_db, document, comment)`:

1. Reubica el anclaje contra `document.current_content` **ahora mismo**
   (nunca confía en `structural_pos` ni en `base_version` guardados).
2. Si no se puede reubicar (cita perdida o ahora ambigua) → falla cerrado:
   `document_comments.base_changed` (409). Una edición humana posterior al
   comentario que tocó justo el texto citado nunca se pisa en silencio.
3. Si se reubica, busca `proposal_find` **dentro de esa ventana reubicada**
   (nunca en todo el documento) y sustituye por `proposal_replace` solo ahí
   — el mismo texto repetido en otra parte del documento no se toca.
4. Persiste como una nueva `DocumentVersion` (`source="user"`,
   `version_count` incrementado) — misma forma que un guardado manual desde
   `update_document`.

Una edición humana en una parte NO relacionada del documento sobrevive
intacta a un `accept` posterior, porque el reemplazo nunca sale de la
ventana del anclaje.

### Rutas (`routes/document_comments_routes.py`)

- `GET /api/documents/{doc_id}/comments?state=` — lista (filtro opcional
  por estado).
- `POST /api/documents/{doc_id}/comments` — crea; `400
  document_comments.quote_not_found` / `document_comments.quote_ambiguous`
  si la cita no ancla de forma inequívoca.
- `PATCH /api/documents/{doc_id}/comments/{comment_id}` — edita `body`/
  `state` manualmente (p. ej. descartar sin aplicar la propuesta).
- `DELETE /api/documents/{doc_id}/comments/{comment_id}`.
- `POST /api/documents/{doc_id}/comments/{comment_id}/accept` — aplica la
  propuesta; `409 document_comments.base_changed` si el anclaje ya no es
  válido.

### Límite explícito

Ni `body` ni `proposal.find/replace` se interpretan nunca como instrucción:
este módulo y sus rutas tratan ese texto como dato opaco — no se inyecta en
ningún prompt ni se ejecuta (límite de la ficha ADP-05, "no convertir
comentarios en instrucciones privilegiadas").
