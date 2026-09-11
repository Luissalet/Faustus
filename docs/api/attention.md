# Atención en Activity (ADP-11) — `GET /api/attention`, `POST /api/attention/read`

Prioriza, de entre todo lo que corre, qué necesita realmente a la persona
detrás. Implementación: `src/attention.py` (clasificación + marcas de
lectura), `routes/attention_routes.py` (transporte). No sustituye nada: las
aprobaciones y preguntas se siguen resolviendo por las rutas de siempre
(`/api/approvals/*`, `sendTurn` con `questionId`) — este módulo solo dice
*cuáles* y *por qué*, ordenadas.

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

## Límites / siguiente paso

- No cubre sesiones "busy" sin `_Run` propio (`agent_runs._EXTERNAL_BUSY`,
  workers de `delegate_agents` sin su propio detached run).
- `finished_unreviewed` es heurístico (ver arriba) — un marcador durable en
  `src/agent_runs.py` (fuera de este lote) lo haría preciso.
- `attention_reads.json` es un único fichero JSON con lock en proceso: correcto
  para "una marca de tiempo por sesión que alguien abrió", no pensado para
  escalar más allá de eso.
