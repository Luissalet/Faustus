# Tablero de trabajo por proyecto (Lote 92, OBJ-6) — `/api/projects/{project_id}/board/*`

Un tracker de issues por proyecto: id legible y estable (`FAU-12` = clave del
proyecto + secuencial), vocabulario fijo de tipo/estado/prioridad (no
configurable — ver `BOARD_RESEARCH.md` para el porqué), enlaces simples entre
issues (grafo, nunca jerarquía forzada) y `GET .../summary`, la proyección
compacta que `src/agent_loop.py::_project_board_block` inyecta en el prompt
del sistema en vez de que el agente relea un backlog en markdown entero cada
turno.

Implementación: `src/project_board.py` (modelo, SQLite propio) y
`routes/board_routes.py` (rutas). Sin SQLAlchemy — sigue el mismo patrón que
`src/question_store.py`/`src/chat_outbox.py`: fichero SQLite propio bajo
`DATA_DIR` (`DATA_DIR/board.sqlite3`), sin tabla compartida en
`core/database.py`, una transacción de escritura por llamada
(`BEGIN IMMEDIATE`).

## Modelo de datos

```
issues(id TEXT PK "FAU-12", project_id, seq INT, type, title, body_md,
       status, priority, assignee, labels_json, created_at, updated_at,
       closed_at, created_by)
issue_counters(project_id, key, next_seq)     -- PK (project_id, key)
comments(id, issue_id, author, body_md, created_at)
events(id, issue_id, kind, payload_json, actor, created_at)
links(id, issue_id, kind, target_issue_id, created_at)
refs(id, issue_id, kind, value, label, created_at)
```

**Vocabulario fijo** (`src/project_board.py`):

- `type`: `bug` · `idea` · `feature` · `task` · `chore`
- `status`, con categoría fija detrás (igual que Linear — la categoría, no el
  nombre, mueve la lógica de "ready"/"done"):
  - `open` → *unstarted*
  - `in_progress` / `blocked` → *started*
  - `done` → *completed*
  - `wontfix` / `duplicate` → *cancelled*
- `priority`: `P0` · `P1` · `P2` · `P3` (la misma escala que ya usa
  `docs/spec/v2/backlog.json`)
- `links.kind`: `blocks` · `blocked_by` · `relates_to` · `duplicate_of` ·
  `discovered_from`. `blocks`/`blocked_by` y `relates_to` se guardan en
  **ambas direcciones** (crear uno inserta también su espejo) — así "qué me
  bloquea" es una consulta directa sobre el issue que se pregunta, nunca un
  escaneo de los enlaces de todos los demás. `duplicate_of`/
  `discovered_from` son direccionales, sin espejo.
- `refs.kind`: `commit` · `session` · `file` · `url`

**Transiciones de estado**: cualquier estado no terminal puede moverse a
cualquier otro (incluido terminal). Un estado terminal (`done`, `wontfix`,
`duplicate`) solo puede reabrirse a `open`/`in_progress` — cualquier otro
cambio se rechaza con `error_class` `board.invalid_transition`. Mover a
terminal fija `closed_at`; reabrir lo limpia.

**Clave del proyecto** (`services/projects.py`): campo `board_key` en la fila
del proyecto (2-5 mayúsculas, único entre los proyectos del mismo owner).
`ProjectStore.board_key(project)` devuelve el valor guardado o, si no hay
ninguno, un valor derivado del nombre con `project_board.derive_key`
("LocalAI"→"LOC", "Writer's Hoard"→"WH", "Faustus"→"FAU") — nunca se
persiste el derivado hasta que el usuario lo cambia explícitamente. Cambiar
la clave (`PUT .../board/key`) **no renumera** nada: los ids antiguos se
conservan tal cual y los issues nuevos usan la clave nueva
(`ProjectStore.set_board_key`).

**Ids**: `issue_counters` lleva un `next_seq` por `(project_id, key)`, leído
y escrito dentro de la misma transacción `BEGIN IMMEDIATE` que crea el
issue — el lock de fichero de SQLite serializa a los escritores concurrentes,
así que dos agentes creando issues a la vez no chocan sin necesitar ids con
hash (a diferencia de Beads: aquí hay un backend único con SQLite local, así
que un secuencial simple basta y es más legible).

**`ready`**: issues `open`/`in_progress` sin ningún `blocked_by` que siga en
estado no terminal, ordenados por prioridad y luego antigüedad — el
equivalente a `bd ready` de Beads: el llamador no razona sobre todo el
backlog, se lo dice la consulta.

## Rutas

Prefijo `/api/projects/{project_id}/board`. Todas requieren `require_user`
(owner-scoped, igual que el resto de rutas de proyecto) — **incluidas las
mutaciones**: a diferencia del panel de git, un issue del tablero es dato
propio del usuario sin efecto externo (nada de host remoto, nadie más se
entera), así que va en la misma clase de gate que `manage_documents`/
`manage_notes`, no en la de `require_human` que usan los `POST` del panel de
git.

```
GET    /issues?status=&type=&assignee=&q=&priority=&label=&limit=&cursor=
GET    /ready
GET    /summary
GET    /export.md
POST   /issues            {type,title,body_md?,priority?,assignee?,labels?,links?:[{kind,target}]} → 201
GET    /issues/{id}
PATCH  /issues/{id}       {title?,body_md?,type?,status?,priority?,assignee?,labels?}
POST   /issues/{id}/claim {assignee}
POST   /issues/{id}/comments {body_md}     → 201
POST   /issues/{id}/links    {kind, target} → 201
DELETE /issues/{id}/links/{link_id}
POST   /issues/{id}/refs     {kind, value, label?} → 201
DELETE /issues/{id}
POST   /import  {"sources":["objetivos","pendientes","backlog"], "dry_run":bool}
PUT    /key     {"key":"FAU"}
```

`GET .../summary` es el objeto que se inyecta en el prompt:
`{"key", "counts":{status:n}, "ready":[...8], "in_progress":[...], "recent_done":[...5]}`.

**Objeto compacto** (listados, `ready`, `summary`):
`{id,type,title,status,priority,assignee,labels,updated_at,blocked_by:[ids]}`
— `blocked_by` lleva solo los bloqueadores que SIGUEN abiertos (un
bloqueador ya cerrado no bloquea nada).

**Detalle completo** (`GET /issues/{id}`, y lo que devuelven `create`/
`update`/`claim`): todos los campos anteriores más `body_md`, `comments`,
`events`, `links`, `refs`.

**Errores**: cuerpo plano con `error_class` en el nivel superior, junto a
`detail` (el mensaje) -- mismo patrón que `routes/git_routes.py::_error()`
(Lote 94: la primera versión de estas rutas envolvía el código en
`HTTPException(status, {"code": ..., "message": ...})`, que FastAPI serializa
como `{"detail": {"code": ..., "message": ...}}` -- `error_class` quedaba
entonces enterrado en `detail.code`, donde el middleware OBS-03 nunca lo ve y
rellena en su lugar una clase genérica derivada solo del código HTTP. Los
cuatro tests de `tests/test_l92_board_routes.py` que comprobaban
`resp.json()["detail"]["code"]` se corrigieron a `resp.json()["error_class"]`
al arreglarlo; `tests/test_l94_board_contract.py` cubre el cuerpo real de
extremo a extremo):

| `error_class`               | HTTP | Cuándo |
|---|---|---|
| `board.not_found`           | 404  | issue/link inexistente o de otro proyecto |
| `board.invalid_transition`  | 400  | cambio de estado no permitido desde un estado terminal |
| `board.claimed`             | 409  | `claim` sobre un issue ya reclamado por otro assignee |
| `board.invalid` / `board.invalid_key` / `board.invalid_link` / `board.invalid_ref` / `board.too_large` | 400 | validación de entrada |

## `POST /import`

Migra desde los ficheros que ya describe `BOARD_RESEARCH.md`, buscados
relativos al `workspace` del proyecto:

- **`OBJETIVOS.md`**: cada `## OBJ-N · título` (o variantes `-`/`:`) se
  convierte en un issue `feature`; un título tachado (`~~...~~`) o un cuerpo
  que menciona "done/completed/completado/cerrado" entra como `done`, el
  resto como `open`. El cuerpo de la sección se guarda en `body_md`.
- **`PENDIENTES.md`**: líneas de checklist `- [ ]`/`- [x]` y líneas
  tachadas `- ~~...~~` se convierten en issues `task` (`x`/tachado → `done`,
  el resto → `open`). Las secciones cuyo encabezado contiene "regla" se
  **saltan enteras** — una regla permanente no es una tarea (heurística por
  encabezado; no es infalible, ver "Riesgos" en el informe del lote).
- **`docs/spec/v2/backlog.json`** (bajo el `workspace` del proyecto): cada
  item se convierte en issue `feature` con label `spec:<id-original>` y
  `priority` tomada del item si es válida; `depends_on` se traduce a enlaces
  `blocked_by` una vez creados todos los issues del lote.

Idempotente: cada issue importado lleva un label `import:<origen>:<ext_id>`
(hash estable del texto para PENDIENTES.md, `OBJ-N` para OBJETIVOS.md, el id
original para backlog.json); una segunda pasada con el mismo origen omite lo
que ese label ya marca como importado. `dry_run` (por defecto `true`) hace
todo el trabajo de parseo y devuelve `preview` sin escribir nada;
`dry_run:false` escribe y suma `created`.

## Tools del agente (`src/agent_tools/board_tools.py`)

`board_list`, `board_ready`, `board_get`, `board_create`, `board_update`,
`board_comment`, `board_link`, `board_claim`. El proyecto sale siempre de
`ctx["project_id"]` — el mismo mecanismo que `git_tools.py::_resolve_repo_root`
ya usa para "el repo del proyecto" — nunca se pasa como argumento; sin
proyecto en el chat, cada tool se niega con `error_class` `board.no_project`.

Sin gate de aprobación ni de política: un issue del tablero es dato propio
del usuario, misma clase que `manage_documents`/`manage_notes`
(`src/tool_capabilities.py`: lecturas `READ_PRIVATE`, escrituras
`WRITE_PRIVATE`). `board_create` devuelve el id creado; el agente debe
citarlo al usuario ("Apuntado como FAU-14"), nunca inventar uno.

## Conexión con git

`git_panel.commit(repo_path, message, project_id=...)` — parámetro opcional
nuevo, con valor por defecto `None` (todo lo que ya llamaba a `commit()` sin
él sigue exactamente igual). Cuando se pasa `project_id`, tras un commit con
éxito se llama a `project_board.link_commit(project_id, sha, message)`:

- por cada `[A-Za-z]{2,5}-\d+` en el mensaje que sea un issue de ESE proyecto
  → fila nueva en `refs` (`kind=commit`, `value=sha`) — "mencionado", sin
  cambiar el estado;
- si el id va precedido de una palabra de cierre (`fixes`, `fix`, `closes`,
  `close`, `resolves`, `resolve`, `implements`, `completes`, y en español
  `cierra`, `arregla`, `resuelve`...) y el issue no está ya en un estado
  terminal → pasa a `done` + evento `commit_linked` con el sha.

Nunca lanza: un fallo de `project_board.link_commit` (base bloqueada,
proyecto inexistente, mensaje sin ids) se registra con `logger.debug` y el
commit sigue siendo un éxito — este hook corre en la ruta caliente de
`git commit` y no puede convertir un commit correcto en un error.

`src/agent_git_policy.py::after_turn` (el auto-commit del agente al final de
un turno) resuelve el `project_id` con `services.projects.project_for_session
(session_id, owner)` y se lo pasa a `git_panel.commit(...)` — así un mensaje
de auto-commit que mencione `FAU-3` con una palabra de cierre lo marca
`done` sin ninguna llamada explícita a `board_update`.

**Nota de integración** (fuera del alcance de ficheros de este lote): el
commit manual desde el panel (`routes/git_routes.py::post_commit`) y el
tool `git_commit` del agente (`src/agent_tools/git_tools.py::GitCommitTool`)
también llaman a `git_panel.commit(...)` pero SIN `project_id` todavía — el
mecanismo (el parámetro en `git_panel.commit`, y `project_board.link_commit`)
ya existe y está probado; falta una línea en cada uno de esos dos sitios
(`project_id=meta["project_id"]` en el primero, `project_id=str(ctx.get
("project_id") or "") or None` en el segundo) para que también disparen el
enlace. Ver el informe del Lote 92, sección "Cambios necesarios en ficheros
ajenos".

## Contexto del agente

`src/agent_loop.py::_project_board_block(owner, project_id)`, inyectado en
`_build_system_prompt` justo después del bloque de repos
(`_project_repos_block`), con el mismo patrón: caché de 20 s por
`(owner, project_id)`, tope de 600 caracteres. Formato:

```
## Project board (FAU)
3 ready — FAU-12 [P1] bug: el retry no limpia sessionId; FAU-9 [P2] feature: exportar a CSV (+1 more)
in progress: FAU-7 (agent)
done recently: FAU-5, FAU-6
```

Vacío (no añade nada al prompt) cuando el proyecto no tiene issues
`ready`/`in_progress`/`done` recientes, o si algo falla — nunca bloquea la
construcción del prompt por esto.

## Qué NO hace (BOARD_RESEARCH.md "Qué NO hacer")

Sin workflows configurables por proyecto, sin permisos por transición, sin
campos custom ilimitados, sin lenguaje de consulta propio. Un solo
vocabulario, compartido por todos los proyectos.
