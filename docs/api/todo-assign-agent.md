# Asignar una tarea a un agente (`/api/todos/{id}/assign-agent`)

Una "tarea" es una nota de tipo `todo`/`checklist` de Notas. Asignarla a un agente abre una conversación nueva con la tarea como encargo, la ejecuta en segundo plano y enlaza conversación y tarea en los dos sentidos. Implementación: `src/todo_agent.py` y `routes/todo_agent_routes.py`.

## Rutas

| Ruta | Qué hace |
| --- | --- |
| `POST /api/todos/{id}/assign-agent` | Cuerpo `{mode: "agent", project_id?, autonomy_preset?, model?}`. Devuelve `{ok, todo_id, run}`. |
| `GET /api/todos/{id}/agent` | El registro de la ejecución (`run`) o `null`. |

Solo el dueño de la nota ve la tarea (404 para otro usuario). Con token de API hacen falta `todos:write` **y** `agents:dispatch` (perfil `todo_agent`) para asignar, y `todos:read` para leer el estado.

## El registro `run`

`run_id`, `status` (`running` | `done` | `needs_review` | `failed`), `session_id`, `started_at`, `finished_at`, `summary` (hasta 1800 caracteres de la respuesta), `error`, `project_id`, `autonomy_preset` (el efectivo), `autonomy_requested`, `autonomy_clamped`, `already_running`. Se guarda en `notes.agent_run` y también aparece como `agent_run` en `GET /api/notes`. La nota guarda además `agent_session_id`.

## Reglas

- **Autonomía nunca ampliada.** La base es el `autonomy_preset` guardado del proyecto (`supervised` si no hay). Lo pedido solo puede igualar o endurecer esa base; si pide más, se aplica la base y `autonomy_clamped` es `true`. Orden: `read_only` < `supervised` < `bounded_autonomous`. Con proyecto, la ejecución usa su carpeta de trabajo.
- **El encargo entra como texto no fiable**: una herramienta que cambia algo se detiene en una tarjeta de aprobación exacta; la ejecución desatendida no se aprueba sola.
- **Nunca `done` sin una ejecución completada.** `done` exige respuesta, sin error, sin aprobaciones pendientes y sin tiempo agotado. Parada en una aprobación, respuesta vacía o tiempo agotado: `needs_review`. Excepción o error sin respuesta: `failed`. Una ejecución que figura `running` sin tarea viva (Faustus se reinició) se repara a `failed` al leerla. La ejecución no marca los elementos de la lista: eso lo hace la persona.
- **Idempotente.** Asignar de nuevo mientras corre devuelve la misma ejecución (`already_running: true`) sin crear otra conversación. Terminada, una nueva asignación crea una ejecución nueva.
- Errores: 400 (modo distinto de `agent`, preajuste desconocido, tarea vacía, proyecto desactivado), 404 (tarea o proyecto inexistentes), 503 (sin modelo por defecto).

## Studio y MCP

- Notas: acción "Asignar a un agente" en el menú de cada nota y una etiqueta de estado ("En curso por agente", "Agente terminó", "Agente: revisar", "El agente falló") que abre la conversación; mientras corre, la lista se refresca cada 5 s.
- MCP integrado `faustus-workers`: herramienta `todo_assign_agent` (`todo_id`, `project_id?`, `autonomy_preset?`, `model?`, `wait?`).

## Verificación

`tests/test_todo_assign_agent.py` (sin llamadas a modelos: sesión y turno sustituidos) y `studio/checks/todo-agent.check.mjs`.
