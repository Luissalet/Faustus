# Excursos (side threads) — `/api/session/{id}/side-threads`, `/thought-map`, `/context-preview`, `/references*`

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
historia propia de la sesión.

## Modelo de datos

Una única tabla, `session_wires`, para los dos tipos de cable:

```
id, owner, kind ('branch'|'reference'),
source_session_id, target_session_id,   -- FK sessions.id ON DELETE CASCADE
anchor_index, anchor_passage,           -- branch
depth ('quote'|'full'), context_order, archived, source_fingerprint,  -- reference
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

Ambas FK tienen `ON DELETE CASCADE`: un cable nunca sobrevive a ninguna de
las dos sesiones que nombra, sin limpieza específica en
`SessionManager.delete_session`.

## El hook: `inherited_context`

`build_chat_context` construye `messages = preface + inherited_context(...) +
_history_messages`. `inherited_context(session_manager, owner, session_id)`
concatena dos capas independientes:

1. Un bloque `[Reference: ...]` por cada `reference` no archivada que
   apunta a esta sesión, en `context_order`.
2. Si esta sesión es un excurso (tiene un cable `branch` entrante): la
   transcripción de la madre hasta el ancla (`parent.history[:anchor_index+1]`,
   sin mensajes `slash`), precedida RECURSIVAMENTE por lo que la madre misma
   heredó si ella también es un excurso (profundidad máx. 8), y seguida del
   aviso `[Regarding this passage: "..."]` si el cable llevaba un pasaje.

Si el ancla ya no existe (`anchor_index >= len(parent.history)` — la madre se
truncó o compactó por debajo del turno de origen), se declara
`anchor_state: "missing"` y se hereda la transcripción COMPLETA de la madre
más una nota explícita — nunca se inventa ni se calla el hecho.

**Invariante decisivo**: cada mensaje que devuelve `inherited_context` es un
diccionario NUEVO, nunca un `ChatMessage.to_dict()` que la madre ya haya
sellado con `src.context_compactor.HISTORY_INDEX_KEY`. Eso significa que la
compactación puede resumir estos mensajes heredados en el prompt del turno
actual, pero **nunca puede borrar una fila de NINGUNA sesión a partir de
ellos** — el borrado real de filas solo ocurre sobre mensajes sellados con
esa clave, y estos nunca la llevan. Sin cables, la función no toca la BD más
de una consulta y devuelve `[]`.

Sin replay automático de excursos hacia la madre, sin "materiales" o
adjuntos especiales: la única forma en que un excurso influye en otra
conversación es un cable `reference` explícito.

## Rutas

Owner-scoped en todas: una sesión ajena responde exactamente igual que una
inexistente (404), nunca un 403 que permita sondear qué ids existen. Errores
planos: `{"error": str, "error_class": "excursos.<motivo>"}` — nunca
anidados bajo `detail`.

| Método | Ruta | Body | Respuesta |
|---|---|---|---|
| POST | `/api/session/{id}/side-threads` | `{anchor_index, passage?, question?}` | 201 `{"session_id","wire","question"}` |
| GET | `/api/session/{id}/side-threads` | — | `wires_for`: `{"parent","children","references_in","references_out"}` |
| GET | `/api/session/{id}/thought-map` | — | árbol `{"id","name","message_count","anchor_index","children":[...]}`, `"current": true` en el nodo pedido |
| GET | `/api/session/{id}/context-preview` | — | `{"layers":[references, inherited, own], "total_tokens"}` |
| POST | `/api/session/{id}/references` | `{source_session_id, depth?}` | `{"wire","tokens"}` |
| PATCH | `/api/session/{id}/references/{wire_id}` | `{depth?, archived?, context_order?, refresh?}` | `{"wire"}` |
| DELETE | `/api/session/{id}/references/{wire_id}` | — | `{"removed": true}` |
| GET | `/api/side-threads/parents` | — | `{"parents": {child_id: parent_id}}` |

`error_class` conocidos: `excursos.not_found` (404), `excursos.
anchor_out_of_range` (400), `excursos.self_reference` (400), `excursos.
bad_depth` (400).

`POST .../side-threads` **no copia mensajes** (a diferencia de
`/api/session/{id}/fork` en `routes/history/history_routes.py`): el excurso
nace vacío y hereda por cable. `question`, si viene, no se envía a ningún
modelo aquí — se devuelve tal cual para que el cliente la ponga en el
compositor del nuevo excurso.

`POST .../references` es idempotente: cablear dos veces el mismo par
`(source, target)` no duplica el bloque, devuelve el cable existente.

## Límites

- Profundidad de herencia recursiva (`inherited_context`, excurso de
  excurso de excurso...): 8.
- Profundidad del mapa (`thought-map`, subiendo a la raíz y bajando por los
  hijos): 32 — más allá, el resultado lleva `"truncated": true`.
- `anchor_passage`: 2000 caracteres, truncado en la creación.
- Títulos del trail (`Trail (upstream questions): q1 → q2 → q3` en un
  bloque `reference` con `depth="quote"`): 60 caracteres cada uno.
