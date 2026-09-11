# Excursos (side threads) — `/api/session/{id}/side-threads`, `/thought-map`, `/context-preview`, `/references*`, `/materials*`, `/stale-turns`

Idea de origen: ThoughtDAG (chenxiachan/thoughtdag). La regla, en una frase:
**lo que el modelo ve es exactamente lo que está cableado al nodo**. Un
excurso nace de un pasaje concreto de la conversación principal, crece en su
propio hilo sin ensuciar el original, y solo vuelve a la conversación
principal como un bloque de referencia explícito que el usuario cablea,
ajusta o retira. El humano dibuja el grafo; el modelo va por los cables.
Ningún agente lo redibuja por su cuenta — cada cable nace de una llamada a
esta API hecha por un humano, nunca de una tool.

Implementación: `core/database.py::SessionWire` (modelo), `src/side_threads.py`
(lógica + acceso a BD, sin FastAPI), `routes/side_thread_routes.py` (rutas).
El hook que hace que todo esto importe vive en
`routes/chat_helpers.py::build_chat_context`, justo antes de concatenar la
historia propia de la sesión. CONTRATO_CABLES2 (F1 "replay bajo botón" y F2
"materiales cableados") extiende este mismo módulo — nada aquí abajo lo
sustituye, lo completa.

## Modelo de datos

Una única tabla, `session_wires`, para los cuatro tipos de cable:

```
id, owner, kind ('branch'|'reference'|'document'|'note'),
source_session_id, target_session_id,   -- FK sessions.id ON DELETE CASCADE
anchor_index, anchor_passage,           -- branch
depth ('quote'|'full' en reference; 'selection'|'full' en document),
context_order, archived, source_fingerprint,
document_id,                            -- document
note_text,                              -- note
quotes, ranges,                         -- document, JSON
created_at
```

- **`branch`** (estructural, "sólido"): `source` = sesión madre, `target` =
  excurso. Como mucho uno por `target` — un excurso tiene una única madre —
  garantizado por construcción: solo `create_side_thread` inserta un
  `branch`, y siempre emparejado con una sesión que acaba de crear.
- **`reference`** ("discontinuo"): `source` = el excurso que se trae de
  vuelta, `target` = la sesión que lo recibe, como UN bloque
  `[Reference: ...]`. Pueden coexistir varias apuntando al mismo `target`
  (`context_order` las ordena); `archived` retira una sin borrar el excurso;
  DELETE la borra físicamente sin tocar ninguna de las dos sesiones.
- **`document`** / **`note`** (F2, "materiales cableados"): un fragmento de
  documento o una nota libre, fijados PERMANENTEMENTE en el contexto de UNA
  sesión — no hay una "otra" sesión a la que apunten, así que
  `source_session_id` y `target_session_id` son el mismo id (la columna es
  `NOT NULL` y ya existía; los invariantes `source != target` / sin ciclos
  solo aplican a `branch`/`reference`). `document`: `depth='selection'` fija
  hasta 8 citas literales (`quotes`, JSON, ≤ 4000 caracteres cada una);
  `depth='full'` lee `Document.current_content` EN VIVO al montar el bloque,
  siempre la versión actual. `note`: `note_text` (1-8000 caracteres) es el
  propio texto del usuario.

Ambas FK tienen `ON DELETE CASCADE`: un cable nunca sobrevive a ninguna de
las dos sesiones que nombra, sin limpieza específica en
`SessionManager.delete_session`.

**Migración**: `session_wires` ya existía (CONTRATO_EXCURSOS, `create_all`).
`document_id`/`note_text`/`quotes`/`ranges` se añaden con un
`ALTER TABLE session_wires ADD COLUMN` idempotente
(`core/database.py::_migrate_add_session_wire_material_columns`, registrado
en `_formal_migration_steps()`), guardado por `PRAGMA table_info` — una base
existente los gana una vez; una base nueva ya los trae desde `create_all` y
el `PRAGMA` no encuentra nada que añadir.

## El hook: `inherited_context_with_snapshot`

`build_chat_context` construye `messages = preface + inherited + _history_
messages`, donde `inherited, side_thread_wires =
inherited_context_with_snapshot(session_manager, owner, session_id)`.
`inherited_context(...)` (sin snapshot) sigue existiendo — es esa misma
función quedándose solo con `messages` — para todo caller que no necesita el
snapshot (`context_preview`, la mayoría de tests).

Tres capas, en este orden — **materiales → referencias → cadena heredada**
(ThoughtDAG: *materials → references → mainline*):

1. **Materiales** (F2): un bloque por cada cable `document`/`note` no
   archivado que apunta a esta sesión, en `context_order`.
2. **Referencias**: un bloque `[Reference: ...]` por cada `reference` no
   archivada que apunta a esta sesión, en `context_order`.
3. **Cadena heredada** (`branch`): si esta sesión es un excurso, la
   transcripción de la madre hasta el ancla
   (`parent.history[:anchor_index+1]`, sin mensajes `slash`), precedida
   RECURSIVAMENTE por lo que la madre misma heredó si ella también es un
   excurso (profundidad máx. 8), y seguida del aviso
   `[Regarding this passage: "..."]` si el cable llevaba un pasaje.

Si el ancla ya no existe (`anchor_index >= len(parent.history)` — la madre se
truncó o compactó por debajo del turno de origen), se declara
`anchor_state: "missing"` y se hereda la transcripción COMPLETA de la madre
más una nota explícita — nunca se inventa ni se calla el hecho.

**Invariante decisivo**: cada mensaje que devuelve la función es un
diccionario NUEVO, nunca un `ChatMessage.to_dict()` que la madre ya haya
sellado con `src.context_compactor.HISTORY_INDEX_KEY`. Eso significa que la
compactación puede resumir estos mensajes heredados en el prompt del turno
actual, pero **nunca puede borrar una fila de NINGUNA sesión a partir de
ellos** — el borrado real de filas solo ocurre sobre mensajes sellados con
esa clave, y estos nunca la llevan. Sin cables, la función no toca la BD más
de una consulta y devuelve `([], [])`.

`regenerate_chat_response` (`POST /api/chat/regenerate/{sid}`) construye su
propio prompt sin pasar por `build_chat_context` — llama a
`inherited_context_with_snapshot` directamente (arreglo CONTRATO_CABLES2 F1;
antes de esto, regenerar en una sesión con cualquier cable perdía TODA la
herencia).

### F1 — el snapshot y `stale_turns`

`snapshot` es `[{"wire_id", "kind", "source_session_id", "document_id",
"fingerprint"}]`, una entrada por cada cable `reference`/`document`/`note`
que aportó un bloque (`branch` nunca aparece: siempre refleja el ancla
ACTUAL de la madre, no puede quedarse desactualizado de la misma forma).
`save_assistant_response(..., wires=snapshot)` estampa
`metadata["side_thread_wires"] = snapshot` en la respuesta guardada y llama
a `src.side_threads.note_wires_used(snapshot)`, que actualiza el
`source_fingerprint` de cada cable a la huella con la que se acaba de
responder.

Eso mueve lo que significa `stale` de "la fuente cambió desde que se
cableó" a lo preciso: **"la fuente cambió desde la última respuesta que lo
usó"**. `wires_for` expone, por cada entrada de `references_in`,
`children[].reference` y `materials[]`, un `stale_turns: {"count", "last_
index"}` — cuántas de las respuestas propias de esta sesión se escribieron
contra una versión anterior de ese cable, y el índice de historia (0-based,
el mismo que usa `anchor_index`) de la última. `GET .../stale-turns` da la
vista inversa, por turno: `{"turns": {"<history_index>": [{"wire_id",
"label"}]}}`.

## `context_preview`

`{"layers": [materials, references, inherited, own], "total_tokens"}` — la
capa `materials` va primera (F2), con `items: [{"wire_id","kind","label",
"stale"}]`.

Sin replay automático de excursos hacia la madre: la única forma en que un
excurso influye en otra conversación sigue siendo un cable `reference`
explícito (o, ahora, un material fijado a mano) — nunca algo que un agente
dispare por su cuenta.

## Rutas

Owner-scoped en todas: una sesión ajena responde exactamente igual que una
inexistente (404), nunca un 403 que permita sondear qué ids existen. Errores
planos: `{"error": str, "error_class": "excursos.<motivo>"}` — nunca
anidados bajo `detail`.

| Método | Ruta | Body | Respuesta |
|---|---|---|---|
| POST | `/api/session/{id}/side-threads` | `{anchor_index, passage?, question?}` | 201 `{"session_id","wire","question"}` |
| GET | `/api/session/{id}/side-threads` | — | `wires_for`: `{"parent","children","references_in","references_out","materials"}` |
| GET | `/api/session/{id}/thought-map` | — | árbol `{"id","name","message_count","anchor_index","children":[...]}`, `"current": true` en el nodo pedido |
| GET | `/api/session/{id}/context-preview` | — | `{"layers":[materials, references, inherited, own], "total_tokens"}` |
| GET | `/api/session/{id}/stale-turns` | — | `{"turns": {"<history_index>": [{"wire_id","label"}]}}` |
| POST | `/api/session/{id}/references` | `{source_session_id, depth?}` | `{"wire","tokens"}` |
| PATCH | `/api/session/{id}/references/{wire_id}` | `{depth?, archived?, context_order?, refresh?}` | `{"wire"}` |
| DELETE | `/api/session/{id}/references/{wire_id}` | — | `{"removed": true}` |
| POST | `/api/session/{id}/materials` | `{kind, document_id?, depth?, quotes?, ranges?, note_text?}` | 201 `{"wire","tokens"}` |
| PATCH | `/api/session/{id}/materials/{wire_id}` | `{depth?, note_text?, archived?, context_order?, refresh?}` | `{"wire"}` |
| DELETE | `/api/session/{id}/materials/{wire_id}` | — | `{"removed": true}` |
| GET | `/api/side-threads/parents` | — | `{"parents": {child_id: parent_id}}` |

`error_class` conocidos: `excursos.not_found` (404), `excursos.
anchor_out_of_range` (400), `excursos.self_reference` (400), `excursos.
bad_depth` (400), `excursos.bad_kind` (400), `excursos.quotes_required`
(400), `excursos.empty_note` (400).

`POST .../side-threads` **no copia mensajes** (a diferencia de
`/api/session/{id}/fork` en `routes/history/history_routes.py`): el excurso
nace vacío y hereda por cable. `question`, si viene, no se envía a ningún
modelo aquí — se devuelve tal cual para que el cliente la ponga en el
compositor del nuevo excurso.

`POST .../references` es idempotente: cablear dos veces el mismo par
`(source, target)` no duplica el bloque, devuelve el cable existente.
`POST .../materials` con `kind='document'` es idempotente en (documento,
depth, quotes): la misma selección cableada dos veces devuelve el mismo
cable. `kind='note'` nunca se deduplica — dos notas son dos notas.

**Deviación documentada**: CONTRATO_CABLES2 dejaba abierto si
`PATCH`/`DELETE /references/{wire}` debían aceptar también cables
`document`/`note`, o si materiales merecían su propia ruta. Este módulo optó
por `/materials/{wire_id}` propia — un cable de material carga campos
(`note_text`, un `depth` con vocabulario distinto: `selection`/`full` en vez
de `quote`/`full`) que forzarían a `UpdateReferenceRequest` a describir dos
formas sin relación bajo un mismo nombre.

## Límites

- Profundidad de herencia recursiva (`inherited_context_with_snapshot`,
  excurso de excurso de excurso...): 8.
- Profundidad del mapa (`thought-map`, subiendo a la raíz y bajando por los
  hijos): 32 — más allá, el resultado lleva `"truncated": true`.
- `anchor_passage`: 2000 caracteres, truncado en la creación.
- Títulos del trail (`Trail (upstream questions): q1 → q2 → q3` en un
  bloque `reference` con `depth="quote"`): 60 caracteres cada uno.
- Materiales (F2): hasta 8 `quotes` por cable `document` en `depth=
  'selection'`, ≤ 4000 caracteres cada una; `note_text` 1-8000 caracteres;
  un `document` en `depth='full'` se trunca a 40 000 caracteres con aviso.
