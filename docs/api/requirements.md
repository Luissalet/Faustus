# Requisitos versionados por proyecto (ADP-18/19/20) — `/api/projects/{project_id}/requirements/*`

Un requisito por proyecto con identidad legible y estable (`REQ-3`, secuencial
por proyecto — nunca globalmente único como `FAU-12` del tablero, porque
"REQ-1" se espera que se repita en proyectos distintos), revisiones
inmutables en cada cambio (el pasado no se edita), enlaces tipados a
código/tests/runs/issues y una matriz de cobertura con cuatro dimensiones
independientes (`linked`/`implemented`/`tested`/`verified`) más un indicador
`stale` transversal.

Implementación: `src/requirements/{store,context,evidence}.py` (modelo,
SQLite propio, sin SQLAlchemy — mismo patrón que `src/project_board.py`:
fichero propio bajo `DATA_DIR` (`DATA_DIR/requirements.sqlite3`), una
transacción de escritura por llamada `BEGIN IMMEDIATE`) y
`routes/requirements_routes.py` (rutas). Ver también
`docs/requirements-format.md` para el esquema completo y el sidecar
`.faustus/requirements.yaml`.

Este módulo NO toca `src/project_board.py`: un requisito no es un issue
(el vocabulario del tablero es cerrado por diseño). Un requisito puede
enlazarse DESDE un issue del tablero (link `kind='issue'`), pero
`requirements.sqlite3` y `board.sqlite3` son ficheros independientes.

## Modelo de datos

```
requirements(req_id TEXT PK "proj1:REQ-3", project_id, key "REQ-3", title,
             text, source, acceptance_json, status, proposed_by,
             current_revision INT, created_at, updated_at, created_by)
             UNIQUE(project_id, key)
req_counters(project_id PK, next_seq)
requirement_revisions(id, req_id, revision INT, title, text, source,
                       acceptance_json, status, changed_by, change_note,
                       created_at)                -- INMUTABLE, solo INSERT
requirement_links(id, req_id, kind, target, revision, req_revision_at_link,
                   content_hash, state, created_by, created_at, updated_at)
```

**Vocabulario fijo** (`src/requirements/store.py`):

- `source`: `doc` · `issue` · `url` · `human`
- `status`: `proposed` · `accepted` · `rejected` · `superseded`
- `proposed_by`: `human` · `model` — una propuesta de modelo nace SIEMPRE
  `status: proposed`, y ningún caller `by="model"` puede moverla a
  `accepted`/`rejected` (`requirements.model_cannot_decide`). Solo un humano
  decide.
- Enlaces (`kind`): `implements` · `tests` · `evidences` · `issue`
- Estado de enlace (`state`): `linked` · `needs_review` · `stale` · `unknown`

## Identidad y versionado

Cada `POST /requirements` asigna el siguiente `REQ-N` **por proyecto** (un
contador independiente en `req_counters`, igual que `project_board`'s
`issue_counters` pero sin necesidad de una "key" configurable — el prefijo
es siempre `REQ-`). Dos proyectos distintos con `REQ-1` cada uno nunca
colisionan: la clave primaria real es `project_id + key`.

Cada `PATCH` que cambia `title`/`text`/`source`/`acceptance`/`status`
inserta una fila nueva en `requirement_revisions` y sube
`current_revision` — la fila anterior en `requirement_revisions` nunca se
toca. Un `PATCH` que no cambia nada (mismo valor) no crea revisión.

## Enlaces y validación de rutas

`POST /{key}/links` con `kind` en `implements`/`tests` valida que la mitad
de ruta del `target` (`path[@symbol]` o `path::symbol`) resuelva DENTRO del
workspace del proyecto — una ruta absoluta o que escape con `../` se
rechaza con `409 requirements.path_outside_workspace`, nunca se acepta en
silencio para descubrirse "unknown" más tarde. El hash del fichero se
guarda en el momento del enlace para detectar cambios después.

`kind='evidences'`/`kind='issue'` no tocan el sistema de ficheros: el
`target` es un run/test-result id o una clave de issue del tablero.

## Matriz de cobertura (`GET /matrix`, `GET /{key}/matrix`)

Cuatro hechos independientes, nunca fundidos en una puntuación:

- **`linked`**: existe al menos un enlace de cualquier tipo.
- **`implemented`**: un enlace `implements` resuelve AHORA MISMO — la ruta
  existe y, si se dio un símbolo, sigue encontrándose con
  `src/repo_map.py::symbol_lines` (el mismo extractor que usa el outline de
  `read_plan`, nunca un segundo parser).
- **`tested`**: lo mismo para un enlace `tests`.
- **`verified`**: existe un enlace `evidences` registrado contra la
  **revisión actual** del requisito. Un enlace `implements` — incluido uno
  detectado vía comentario `@implements REQ-N`
  (`evidence.scan_implements_comments`) — NUNCA activa `verified` por sí
  solo; es, por construcción, un enlace, no una verificación.
- **`stale`** (transversal): algún enlace resuelto cambió desde que se
  registró — el fichero cambió de contenido (símbolo renombrado, por
  ejemplo) o el requisito ganó una revisión nueva después de que se
  registrara el `evidences`. Un objetivo que simplemente DESAPARECIÓ
  (fichero/test borrado) no es "stale" — es `unknown`: la evidencia se fue,
  no cambió.

`GET /{key}/matrix`/`GET /matrix` recalculan el estado de cada enlace en
vivo y lo persisten en `requirement_links.state` como efecto secundario de
lectura (igual que la caché por fichero de `repo_map`); nunca se recalcula
al pedir solo el requisito (`GET /{key}`).

## Contexto para una tarea (`POST /context`)

`src/requirements/context.py::for_task(project_id, *, files, keys,
budget_chars)`: prioriza los `keys` pedidos directamente, luego los
requisitos enlazados (`implements`/`tests`) a los `files` citados. Un
`key` pedido que no existe va a `unknown`, nunca se inventa. Un requisito
que no cabe en `budget_chars` va COMPLETO a `omitted` — nunca se recorta un
criterio de aceptación en silencio para que quepa a medias.

## Sidecar (`.faustus/requirements.yaml`)

`GET /sidecar` — proyección de solo lectura de
`<workspace>/.faustus/requirements.yaml` si existe, con un parser tolerante
de un subconjunto pequeño de YAML (sin dependencia nueva: pyyaml no está
declarada en `requirements*.txt`/`pyproject.toml` de este repo). Formato
documentado en `docs/requirements-format.md`. Los elementos del sidecar NO
se importan automáticamente a `requirements.sqlite3` — son el registro de
un humano, no una decisión de base de datos por sí solos.

## Rutas

| Método | Ruta | Qué |
|---|---|---|
| GET | `/requirements` | Lista, filtrable por `status`/`source`/`q` |
| GET | `/requirements/matrix` | Matriz de cobertura de TODO el proyecto |
| GET | `/requirements/sidecar` | Vista de solo lectura del sidecar |
| GET | `/requirements/{key}` | Un requisito completo |
| GET | `/requirements/{key}/revisions` | Historial inmutable |
| GET | `/requirements/{key}/links` | Enlaces del requisito |
| GET | `/requirements/{key}/matrix` | Matriz de cobertura de un requisito |
| POST | `/requirements` | Crear |
| PATCH | `/requirements/{key}` | Editar (crea revisión); `by: human\|model` |
| POST | `/requirements/{key}/links` | Enlazar evidencia |
| DELETE | `/requirements/{key}/links/{link_id}` | Quitar un enlace (404 si el enlace no es de ese requisito) |
| POST | `/requirements/context` | `for_task` presupuestado |

Owner-scoped en todas las rutas vía `require_user` + el mismo
`_project_or_404` que copia `routes/board_routes.py` — un `project_id` que
no es del owner responde exactamente igual que uno que no existe.

## Tools del agente

`src/agent_tools/requirement_tools.py`: `req_list`/`req_get`/`req_matrix`
(lectura) y `req_propose`/`req_link` (escritura). Sin `req_accept`: aceptar
o rechazar un requisito es una decisión humana que solo se toma vía
`PATCH .../requirements/{key}` con `by: human` desde la UI —
`Store.create`/`Store.update` lo hacen cumplir de forma independiente de
este fichero, así que no basta con que el prompt del agente "se le olvide"
llamar a la tool equivocada.
