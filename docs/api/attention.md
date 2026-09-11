# Atención en Activity (ADP-11 + CMP-05) — `GET /api/attention`, `POST /api/attention/read`

Prioriza, de entre todo lo que corre, qué necesita realmente a la persona
detrás. Implementación: `src/attention.py` (clasificación + marcas de
lectura), `routes/attention_routes.py` (transporte). No sustituye nada: las
aprobaciones y preguntas se siguen resolviendo por las rutas de siempre
(`/api/approvals/*`, `sendTurn` con `questionId`) — este módulo solo dice
*cuáles* y *por qué*, ordenadas.

**CMP-05 profundiza ADP-11, no lo sustituye.** Todo lo de más abajo (`kind`,
prioridad fija, `finished_unreviewed`, lectura) sigue exactamente igual —
los 28 tests originales de `tests/test_adp11_attention.py` no se tocaron.
Lo nuevo son cuatro ejes **independientes** que antes estaban aplastados
dentro de la única palabra `kind`:

| Campo | Valores | Qué es |
|---|---|---|
| `lifecycle` | `queued`/`running`/`waiting`/`finished`/`failed`/`cancelled` | Estado propio de la ejecución — nunca confundido con POR QUÉ está parada. |
| `wait_cause` | `approval`/`question`/`gpu_queue`/`dependency`/`none` | Por qué no avanza, si acaso. `gpu_queue` es la MISMA señal de cola de carril que `queued_model` ya leía (`agent_runs.queued_positions()`) — todavía sin cruzar con `src/resource_admission.py` (terreno de W2-B). |
| `connection_health` + `signal` | `live`/`stale`/`disconnected`; `signal: {source, age_s, last_event_at}` | Si la señal de esta ejecución se ve sana. `source` vive en `RunInfo` (por defecto `"events"`); `"heuristic"` es un hueco tipado para el adaptador Herdr de CMP-06 — nada de este módulo lo usa todavía. |
| `next_action` | `approve`/`answer`/`open`/`retry`/`reconnect` | Función determinista de `kind` (y de `lifecycle` para `finished_unreviewed`: `failed` → `retry`, si no → `open`). |
| `project_id` | `Session.project_id` o `null` | Para la vista "por proyecto" del estudio — mismo dato que ya usa `routes/session_routes.py`, nunca un segundo sitio donde vive el proyecto de una sesión. |

Un ejemplo de fila completa:

```json
{"session_id": "s1", "kind": "disconnected", "reason": "No recent events — is it disconnected?",
 "priority": 3, "since": 1699999880.0, "detail": "2 min", "label": "Long research", "unread": true,
 "lifecycle": "running", "wait_cause": "none",
 "connection_health": "disconnected", "signal": {"source": "events", "age_s": 122.0, "last_event_at": 1699999880.0},
 "next_action": "reconnect", "project_id": "proj-42"}
```

**Marcador durable de "terminado" (`src/agent_runs.py`).** Antes,
`finished_unreviewed` siempre presentaba una sesión terminada como
`status="done"` (adivinado), porque la única fuente disponible era
`Session.last_message_at`, que no distingue "el agente acaba de terminar" de
"el humano mandó otro mensaje y no hay respuesta aún", y no sabe si el run
en realidad falló. CMP-05 cierra ese hueco: `_drain`'s `finally` (el único
sitio donde `run.status` se asienta a un valor terminal) escribe
`DATA_DIR/finished_runs.json` (`{session_id: {run_id, status, finished_at,
label, model}}`, escritura atómica, best-effort — nunca rompe el run real).
`agent_runs.finished_marker(session_id)` / `finished_markers()` lo leen;
`attention_for_owner` los prefiere sobre la adivinanza de la base de datos
cuando existen, así que `lifecycle`/`next_action` reflejan el desenlace real
(`error` → `failed`/`retry`, no `finished`/`open`). Una sesión sin marcador
(anterior a este cambio, o que nunca corrió un run detached) sigue cayendo
al mismo fallback de siempre.

## `classify(RunInfo) -> Attention`

Función pura (`src/attention.py::classify`). Ocho estados posibles, en el
orden de prioridad fijo de la ficha:

| `kind`                | Prioridad | Cuándo |
|------------------------|:---:|---|
| `approval`              | 0 | `tool_approvals.py`'s mid-turn gate tiene una aprobación pendiente para esta sesión. |
| `question`               | 1 | `question_store.list_open()` tiene una pregunta abierta para esta sesión. |
| `finished_unreviewed`     | 2 | El run terminó (`done`/`error`/`stopped`) y nadie lo ha marcado leído. |
| `disconnected`            | 3 | `status == "running"` pero `last_event_at` lleva ≥ `STALE_AFTER_S` (120s) sin moverse — **nunca** se lee como `working` por inercia. |
| `queued_model`            | 4 | En cola por su carril (`agent_runs.queued_positions()`), sin estar stale. |
| `dependency`              | 5 | Un worker delegado (`subagent_tools.worker_board()`) todavía no ha respondido. |
| `working`                 | 6 | Corriendo con normalidad. **No** aparece en la respuesta HTTP (no necesita a nadie). |
| `none`                    | 7 | Nada de lo anterior. **No** aparece en la respuesta HTTP. |

El umbral `STALE_AFTER_S = 120.0` está documentado en el propio módulo: no
hay otra constante "cuánto es normal" en el repo de la que heredarlo, así
que se fija muy por encima del heartbeat SSE real, para que una conexión
sana nunca se acerque — no es un ajuste fino de latencia de modelo, es solo
el punto en el que "sin eventos" deja de leerse honestamente como
"trabajando".

`Attention` lleva además `reason` (string en inglés, estable — se traduce
en el cliente con `t()`, igual que el resto de Activity) y `detail` (contexto
sin traducir: posición de cola, nombre de la dependencia).

## `finished_unreviewed` — límite conocido

`src/agent_runs.py` evict-ea el registro en memoria de un run terminal a los
~180s (`_EVICT_GRACE_S`) y este módulo no es dueño de ese fichero (no está en
el lote), así que no puede alargar esa ventana. La fuente real para
`finished_unreviewed` es en cambio `core.database.Session.last_message_at`
(la MISMA columna que ya usa el orden "recientes" del sidebar), acotada a
`FINISHED_LOOKBACK_S` (24h). Es más basta que la de `agent_runs`: no
distingue "el agente acaba de terminar un turno" de "el humano mandó un
mensaje más y aún no hay respuesta". Documentado, no disimulado.

## `GET /api/attention?limit=`

Owner-scoped (`effective_user`, igual que `routes/chat_routes.py`). Devuelve
solo los kinds accionables (`ACTIONABLE_KINDS` — todos menos `working`/`none`),
ordenados por prioridad y, dentro de la misma prioridad, por más reciente:

```json
{
  "runs": [
    {"session_id": "...", "kind": "approval", "reason": "Waiting for your approval",
     "priority": 0, "since": 1699999999.1, "detail": "", "label": "", "unread": true}
  ],
  "unread_count": 1
}
```

## `POST /api/attention/read`

`{"run_ids": ["session-1", "session-2"]}` → `{"ok": true, "marked": 2}`.
Marca `now()` como leído para ese `(owner, session_id)` en
`DATA_DIR/attention_reads.json` (`{owner: {session_id: ts}}`, escritura
atómica vía `core/atomic_io.py`). Leer **nunca** resuelve nada por sí solo:
una aprobación pendiente sigue en la lista tras leerla (solo dejar de estar
`unread`) — la única excepción es `finished_unreviewed`, que no tiene nada
más que resolver y desaparece de la lista al leerse.

`run_ids` que no sea una lista → 400 `attention.invalid_run_ids`.

## Integración en Activity.tsx

`studio/src/adapters/attention.ts` (`loadAttention`/`markAttentionRead`) +
`studio/src/adapters/activity.ts::mergeAttention` (añadida, no reescribe
`ActivityRun`) pegan cada fila de atención a la fila que ya representa esa
sesión (`chat.sessionId` / `question.session`) — salvo `finished_unreviewed`,
que sintetiza una fila mínima porque `conversationRuns()` nunca lista una
sesión terminada. La pestaña «Needs action» pasa a incluir también estos
cuatro estados (antes solo `status === 'waiting'`) y ordena por la prioridad
de `classify()`; abrir un run llama a `markAttentionRead`.

**CMP-05, en `Activity.tsx`:**

- **Tarjeta → siguiente acción útil.** Cada fila y el panel de detalle
  muestran `lifecycle`/`wait_cause`/`connection_health`+edad de la señal
  como insignias separadas (nunca refundidas en la línea de `reason`), y un
  chip `next_action` (aprobar/responder/abrir/reintentar/reconectar) —
  abre el mismo panel de detalle donde ya se decide (el chip nunca aprueba
  ni responde por sí solo; ese paso sigue exigiendo el campo de motivo).
- **Vistas «por proyecto» y «por atención».** «Por atención» es el orden de
  prioridad que ya existía; «por proyecto» es nuevo — un conmutador
  (`?view=project`) que reagrupa la MISMA lista ya cargada con
  `adapters/activity.ts::groupByProject` (puro, sin llamada nueva a red),
  usando `Session.project_id` para `chat`/`question` (vía la fila de
  atención) y el `project_id` que un run de workflow ya trae. Cobertura
  parcial y documentada: `task`/`render`/`approval` no llevan proyecto hoy
  (caen en el cajón «Sin proyecto»).
- **Orden estable mientras se interactúa.** `stableAttentionOrder` congela
  el orden renderizado mientras el ratón o el foco están dentro de la lista
  (`onMouseEnter`/`onMouseLeave`/`onFocus`/`onBlur`) — una fila nueva se
  añade al final en vez de reordenar las que ya estaban, y se reordena de
  verdad en cuanto la persona sale de la lista.
- **MRU «último agente usado».** `localStorage` (best-effort, por
  navegador) recuerda la última sesión de chat/pregunta abierta desde la
  bandeja; un botón «Continue: …» en la cabecera la reabre cuando no es ya
  la seleccionada.
- **Una respuesta tardía no cambia la selección.** `adapters/activity.ts::
  nextRunParam` — un `grant`/`deny`/`answer` captura la clave del run en el
  momento del clic (no cuando la promesa resuelve); si la persona ya abrió
  otro run mientras la petición estaba en vuelo, la resolución tardía no lo
  cierra ni lo sustituye. Cubierto por `studio/checks/attention-cmp05.check.mjs`.

## Límites / siguiente paso

- No cubre sesiones "busy" sin `_Run` propio (`agent_runs._EXTERNAL_BUSY`,
  workers de `delegate_agents` sin su propio detached run) — ni el marcador
  durable nuevo, que solo se escribe en `_drain` (runs propios).
- `wait_cause == "gpu_queue"` todavía es la señal de cola de carril de
  siempre (`agent_runs.queued_positions()`), sin cruzar con
  `src/resource_admission.py` — trabajo de otro lote si hace falta.
- `attention_reads.json` y `finished_runs.json` son ficheros JSON únicos con
  lock en proceso: correcto para su escala actual (una marca por sesión),
  no pensado para crecer más allá de eso.
- La vista «por proyecto» no cubre `task`/`render`/`approval` (sin
  `project_id` hoy) — documentado arriba, no oculto.
- `signal_source == "heuristic"` es un hueco tipado, no cableado: CMP-06
  (adaptador Herdr, solo lectura) es quien lo rellenará.
