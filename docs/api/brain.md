# Segundo cerebro — `/api/brain/*`

El segundo cerebro es una bóveda markdown que Faustus crea y mantiene por su
cuenta bajo `DATA_DIR/brain/vault/<owner>/` (o `brain_vault_dir` si se fija):
memorias, notas personales y entidades tipadas espejadas en ficheros
editables a mano, más notas libres, un grafo de notas y relaciones de entidad
con ventanas de validez. Implementación: `src/brain/` (`db.py`,
`frontmatter.py`, `wikilinks.py`, `render.py`, `vault.py`, `notes.py`,
`temporal.py`, `entities.py`, `extract.py`, `wiki.py`). Esta página describe
las tres superficies que exponen esas mismas funciones: las rutas HTTP
(`routes/brain_routes.py`), el servidor MCP `brain`
(`mcp_servers/brain_server.py`) y la herramienta de agente `brain`
(`src/agent_tools/brain_tools.py`). Ver FAUSTUS.md §176 para el porqué y la
verificación en vivo.

**Autenticación.** Como el resto de los datos propios de una persona: toda
ruta exige `require_user` y trabaja sobre la bóveda de quien llama
(`owner = effective_user(request)`), salvo `PUT /settings`, que cambia un
ajuste de instalación (admin, `require_admin`). Un `ValueError` de
`src.brain.*` (una ruta inválida, un id de entidad o de papelera
desconocido, una fecha mal formada) se traduce a `400`; una nota inexistente,
a `404`.

## Rutas HTTP

Prefijo `/api/brain` en todas.

### Estado y sincronización

| Método | Ruta | Parámetros | Respuesta |
| --- | --- | --- | --- |
| `GET` | `/status` | — | `{enabled, vault_dir, notes, entities, relations, last_sync, extraction:{pending, llm_enabled}, wiki:{enabled}}` |
| `POST` | `/sync` | — | el informe de `vault.sync` (ver abajo) |

`last_sync` es el último informe de `vault.sync` guardado (`vault.last_sync`)
o `null` si nunca se sincronizó. El informe de sincronización, en ambas
rutas:

```json
{"at": "2026-09-23T10:00:00Z", "exported": 12, "imported": 3, "created": 1,
 "suppressed": 0, "conflicts": [], "guard_tripped": false, "errors": [],
 "duration_ms": 134, "notes": []}
```

`conflicts` y `errors` son listas de objetos/cadenas descriptivas; `notes`
es una lista de anotaciones de la propia pasada (p. ej. que el guardián de
borrado saltó); `guard_tripped: true` significa que una sincronización tenía
más ficheros espejados desaparecidos de golpe de los que
`brain_vault_delete_guard_ratio` permite, y no actuó sobre ninguno de ellos
en esa pasada.

### Notas

| Método | Ruta | Parámetros | Respuesta |
| --- | --- | --- | --- |
| `GET` | `/tree` | — | `{"folders": [str], "notes": [{"path","title","kind","source","updated_at","size"}]}` |
| `GET` | `/note` | `path` (vault-relativa) | `notes.read_note` (ver forma abajo) |
| `PUT` | `/note` | body `{"path","content"}` | `notes.write_note` → `{"note": <read_note>, "applied": {...}}` |
| `POST` | `/note` | body `{"title","folder"?,"content"?}` | `read_note` de la nota creada (`folder` por defecto `"Notes"`) |
| `POST` | `/note/rename` | body `{"path","new_title","update_links"?}` | `{"note": <read_note>, "updated_links": int}` |
| `DELETE` | `/note` | `path` | `{"trash_id", "effect"}` (`effect`: `suppressed`\|`removed`\|`hidden`) |

`read_note` (forma compartida por `GET /note`, `PUT /note`, `POST /note`,
`GET /daily` y la restauración de papelera):

```json
{"path": "Notes/Ideas de café.md", "title": "Ideas de café", "kind": "note",
 "source": null, "frontmatter": {"tags": ["cafe"]},
 "user_zone": "texto editable…", "generated": "", "content": "texto completo del fichero",
 "links": [{"target": "Otra nota", "path": "Notes/Otra nota.md", "resolved": true, "label": null}],
 "backlinks": [{"path": "Daily/2026-09-23.md", "title": "2026-09-23", "context": "…"}],
 "tags": ["cafe"], "updated_at": "2026-09-23T10:00:00Z", "editable": true}
```

Una nota espejada (`kind` `memory`/`personal`/`entity`/`project`/
`objective`/`concept`/`daily`/`home`, `source` no nulo) tiene `generated`
relleno con las secciones que la última sincronización escribió; `PUT /note`
sólo puede cambiar la zona editable — el título de una nota `mem:` deriva de
su propio texto y renombrarla se rechaza (`notes.rename_note` lanza
`ValueError`, traducido a 400) salvo cambiando el propio texto.

### Papelera

| Método | Ruta | Parámetros | Respuesta |
| --- | --- | --- | --- |
| `GET` | `/trash` | — | `{"items": [{"id","path","title","source","effect","deleted_at"}]}` |
| `POST` | `/trash/{trash_id}/restore` | — | `read_note` de la nota restaurada |

Restaurar deshace el efecto según la fuente: una memoria vuelve a estar
`suppressed=False`, una entrada personal se vuelve a añadir a `memory.json`,
una entidad se vuelve a mostrar (`hidden=False`) y una nota libre borrada se
vuelve a escribir tal cual estaba.

### Búsqueda, grafo, etiquetas, enlaces sin resolver

| Método | Ruta | Parámetros | Respuesta |
| --- | --- | --- | --- |
| `GET` | `/search` | `q`, `limit` (por defecto 20, tope 200) | `{"results": [{"path","title","kind","snippet","score"}]}` |
| `GET` | `/graph` | `center?`, `depth` (0-5, por defecto 1), `kinds?` (coma), `scope` (`notes`\|`entities`, por defecto `notes`) | `{"nodes":[…], "edges":[…]}` |
| `GET` | `/tags` | — | `{"tags": [{"tag","count"}]}` |
| `GET` | `/unresolved` | — | `{"links": [{"target","from":[paths]}]}` |

`GET /search` usa FTS5 (bm25 + impulso por título), insensible a acentos.
Con `scope=entities`, `/graph` devuelve `entities.graph(owner)` (ignora
`center`/`depth`/`kinds`); con `scope=notes` (por defecto), el grafo local o
global de notas — `center` es una ruta de nota, `depth` cuántos saltos desde
ella (0 = sólo el nodo), y un enlace `[[destino]]` que no resuelve a ninguna
nota aparece como un nodo de tipo `"unresolved"` en vez de desaparecer.

### Nota diaria

| Método | Ruta | Parámetros | Respuesta |
| --- | --- | --- | --- |
| `GET` | `/daily` | `date?` (`YYYY-MM-DD`; hoy si se omite) | `read_note` (se crea si no existía) |

### Entidades

| Método | Ruta | Parámetros | Respuesta |
| --- | --- | --- | --- |
| `GET` | `/entities` | `q?`, `type?` | `{"entities": [EntitySummary, …]}` (tope 200) |
| `GET` | `/entities/{entity_id}` | `as_of?` (ISO) | `profile(entity_id, as_of)` + `path` (ver abajo) |
| `PATCH` | `/entities/{entity_id}` | body: cualquiera de `name`, `type`, `aliases`, `summary`, `hidden` | la entidad actualizada, enriquecida |
| `POST` | `/entities/merge` | body `{"keep","merge"}` | la entidad resultante (`keep`), enriquecida |

Cada fila de `/entities` y la entidad devuelta por `GET /entities/{id}`,
`PATCH` y `merge` llevan `mentions` (lista de `source_ref`) y `relations`
(las vigentes, `include_closed=False`) además de los campos propios de
`entities.get_entity`. `GET /entities/{id}` añade además `"path"`: la ruta
de la nota de la bóveda que espeja esa entidad (`Entities/<Tipo>/<Nombre>.md`)
o `null` si aún no se ha exportado. `profile(id, as_of)`:

```json
{"entity": {"id","name","type","aliases",…},
 "facts": [{"source_ref","text","valid_from","valid_until","valid_now","created_at"}],
 "relations": [{"rel","dst_name","valid_from","valid_until","valid_at": true, …}],
 "history": [ /* relaciones cerradas, con sus ventanas */ ],
 "timeline": [{"at","kind","text","source_ref"}],
 "summary": "…", "summary_sources": ["mem:ab12cd34", …]}
```

`PATCH`/`merge` re-exportan la nota de la entidad afectada a la bóveda de
inmediato (best-effort — un fallo de exportación no rompe la respuesta).

### Línea de tiempo

| Método | Ruta | Parámetros | Respuesta |
| --- | --- | --- | --- |
| `GET` | `/timeline` | `entity?`, `q?`, `limit` (por defecto 200, tope 1000) | `{"events": […]}` |

Con `entity` (un id), la línea de tiempo es la de `profile(entity)["timeline"]`
recortada a `limit` y en orden descendente; sin `entity`, la línea de tiempo
cruzada de `temporal.timeline(owner, query=q, limit=limit)` — memorias
creadas, validadas desde/hasta, corregidas u olvidadas, y cambios de
relación, con `q` filtrando por texto.

### Pasadas en segundo plano, disparadas a mano

| Método | Ruta | Parámetros | Respuesta |
| --- | --- | --- | --- |
| `POST` | `/extract` | body `{"limit"?}` | `{"entities","relations","checked","llm_used","llm_skipped","errors": [str]}` |
| `POST` | `/wiki/refresh` | body `{"entity_id"?}` | `{"refreshed","skipped","errors": [str], ...}` |

`POST /extract` corre primero `entities.revalidate_if_needed` (silenciosa
ante error) y luego `extract.extract_pending(owner, limit=limit)`; el `errors`
de la capa de datos es un entero — la ruta lo traduce a la lista de cadenas
que espera el adaptador de Studio (`["N item(s) failed to extract"]` o `[]`).
`POST /wiki/refresh` con `entity_id` refresca sólo esa entidad
(`wiki.refresh_entity`, un resumen: `refreshed: 1` si `updated`/`fallback`,
`skipped: 1` si `locked`/`unchanged`/`disabled`/`deferred`); sin `entity_id`,
`wiki.refresh_stale(owner)` sobre todas las que lo necesiten, con `checked` y
`deferred` (cuántas se saltaron porque el modelo de utilidad estaba ocupado o
no residente — `background_llm_gate`, nunca carga ni descarga un modelo).

### Ajustes

| Método | Ruta | Body | Respuesta |
| --- | --- | --- | --- |
| `GET` | `/settings` | — | instantánea de los ajustes de abajo |
| `PUT` | `/settings` (admin) | cualquiera de los ajustes de abajo | la instantánea actualizada |

```json
{"memory_temporal_parse": true, "memory_temporal_supersede": true,
 "brain_enabled": true, "brain_vault_dir": "", "brain_vault_sync_seconds": 120,
 "brain_entity_extraction": true, "brain_llm_extraction": true,
 "brain_wiki_summaries": true, "brain_context_source": true,
 "owner_display_name": ""}
```

`brain_vault_delete_guard_ratio` (0.3) y `brain_llm_extraction_batch` (12)
existen como ajustes (`src/settings.py`) pero no se leen ni escriben por
estas rutas — se cambian con el mecanismo genérico de ajustes.

## Servidor MCP `brain`

`mcp_servers/brain_server.py`. La bóveda sobre la que trabaja la fija la
variable de entorno `ODYSSEUS_MCP_BRAIN_OWNER` (o `ODYSSEUS_BRAIN_OWNER`);
sin ella, las herramientas de lectura caen a nivel de instalación pero
`brain_write_note`, `brain_append_note` y `brain_sync` se rechazan en vez de
escribir en la bóveda de nadie en particular.

| Herramienta | Argumentos | Qué hace |
| --- | --- | --- |
| `brain_search` | `query`, `limit?` (1-50) | Notas libres + entidades que casan el texto. Sólo lectura. |
| `brain_read_note` | `path` | Una nota completa: frontmatter, etiquetas, enlaces, backlinks. Sólo lectura. |
| `brain_write_note` | `path?`, `title?`, `folder?`, `content` | Con `path`, reemplaza la zona editable de una nota que existe (la sección generada se conserva); sin `path`, crea una con `title` bajo `folder` (por defecto `Notes`). |
| `brain_append_note` | `path`, `content` | Añade texto al final de la zona editable de una nota, sin borrar lo que ya había. |
| `brain_entity` | `entity_id?` \| `name?`, `as_of?` | El perfil completo de una entidad (hechos, relaciones, línea de tiempo). Sólo lectura. |
| `brain_timeline` | `entity_id?` \| `name?`, `query?`, `limit?` (1-500) | La historia de una entidad, o la línea de tiempo cruzada si no se da entidad. Sólo lectura. |
| `brain_graph_neighbors` | `path?`, `depth?` (1-4), `scope?` (`notes`\|`entities`) | El grafo local alrededor de una nota, o el grafo entero de entidades si no se da `path`. Sólo lectura. |
| `brain_sync` | — | Sincroniza la bóveda ahora mismo (importa ediciones manuales, reexporta, reindexa). |

`brain_entity`/`brain_timeline` aceptan `name` como alternativa a
`entity_id`: se resuelve con la primera coincidencia de
`entities.list_entities(owner, q=name, limit=1)`, y si no hay ninguna la
herramienta devuelve un mensaje de error nombrado (`no entity matches
'<name>'`) en vez de un perfil vacío.

## Herramienta de agente `brain`

`src/agent_tools/brain_tools.py`. Una sola herramienta, `action` como
discriminador; el dueño viene siempre del contexto de ejecución del turno,
nunca de un argumento del modelo (misma disciplina que `context_recall` y
`manage_memory`).

| `action` | Argumentos | Resultado |
| --- | --- | --- |
| `search` | `query`, `limit?` | notas + entidades que casan |
| `read` | `path` | una nota, en `output` su zona editable |
| `write` | `path?`, `title?`, `folder?`, `content` | crea o edita una nota |
| `append` | `path`, `content` | añade sin borrar lo que había |
| `entity` | `entity_id?` \| `name?`, `as_of?` | el perfil de una entidad |
| `timeline` | `entity_id?` \| `name?` \| `query?`, `limit?` | la historia de una entidad o la cruzada |
| `neighbors` | `path?`, `scope?`, `depth?` | el grafo local de notas o el de entidades |
| `daily` | `date?` | la nota diaria de hoy o de `date` |

Las acciones de sólo lectura (`search`, `read`, `entity`, `timeline`,
`neighbors`) pasan la puerta de contexto externo (`src/context_tool_gate.py`,
tabla de reglas de §175) sin pedir tarjeta de aprobación; `write`, `append` y
`daily` sí la piden, con el mismo trato que `manage_memory`. Editar una nota
espejada por `write`/`append` sólo toca su zona editable — la sección
generada de la última sincronización se conserva igual que en las rutas HTTP
y en el servidor MCP.

## Fuente del Context Engine

`src/context_engine/adapters/brain.py` (`BrainSource`, `source_id="brain"`,
sección `retrieved_memory`) no es una superficie que se llame directamente:
compone, para el paquete de contexto de un turno, una tarjeta por cada
entidad mencionada por nombre o alias en el mensaje
(`entities.entities_in_text`) y las mejores notas libres para la consulta
(`notes.search`). Apagada del todo bajo `allow_personal_memory=False`
(Incógnito) y, además, tras su propio ajuste `brain_context_source`
(independiente de `brain_enabled`). `source_ref`: `ent:<entity_id>` o
`note:<ruta>`.
