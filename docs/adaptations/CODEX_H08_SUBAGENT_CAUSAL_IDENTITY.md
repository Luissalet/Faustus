# H08 parcial — identidad causal de workers en el historial

Fecha: 2026-09-30. Continúa [H08 por run](CODEX_H08_PER_RUN.md) y el
[análisis original](CODEX_HARNESS_ANALISIS_2026-09-29.md), basado en
[Codex b1e72963c3b71a9265a551e54beff078384efed9](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9).
No repite la inspección upstream ni modifica el replay de outcomes.

## Pérdida reproducida

`src/agent_tools/subagent_tools.py::_save_transcript` persistía nombre/rol y
sesión padre, pero descartaba el identificador del worker, el run padre y la
delegación. Dos invocaciones con workers/runs distintos y el mismo chat hijo
reanudado producían metadatos causales idénticos tras reabrir SQLite. La prueba
de lectura previa usó sólo un almacén temporal y objetos sintéticos, sin modelos.

## Cambio acotado

Antes de despachar workers se captura el run **activo** del registro runtime
mediante `get_active_run`; `get_run_id` también devuelve runs terminales y no
sirve para esta captura. Si no hay run activo se conserva `None`, sin usar la
sesión como fallback presupuestario. La delegación conserva su identificador
existente de ocho caracteres para mantener compatibilidad con la board.

Cada worker recibe una sola vez `parent_run_id` y `delegation_id`; el reviewer
recibe el mismo snapshot aun si el run padre cambia durante el trabajo. Los
retries conservan el objeto/identidad; reanudar el mismo chat en otra delegación
produce una nueva identidad de invocación. Los metadatos `subagent` del mensaje
assistant añaden `worker_id`, `session_id`, `parent_run_id` y `delegation_id`.
Son identificadores, sin nuevos argumentos, cuerpos ni tokens almacenados.
No cambia el schema SQLite ni exige migración de mensajes legacy.

## Verificación

`tests/test_subagent_causal_identity.py` usa SQLite temporal real y reabre otro
SessionManager. Cubre despacho real con worker/reviewer usando executor fixture,
reemplazo de run padre durante ejecución, dos delegaciones concurrentes del
mismo padre, mismo chat reanudado entre runs, mensajes legacy, ausencia de run
activo, runs terminales y retry real con cambio del registro padre.

Comando: `venv/Scripts/python.exe -m pytest tests/test_subagent_causal_identity.py
tests/test_subagent_retry_usage.py tests/test_subagent_outcome_history.py -q`.
Resultado final: **52 pruebas pasan en 32,62 s**; la suite nueva aporta 11 casos.
`git diff --check` limpio, salvo el aviso habitual de conversión CRLF.

## Límites pendientes

No captura `parent_call_id`: el contexto inspeccionado no proporciona esa
identidad. No inventa una llamada a partir del nombre, orden o presupuesto.
La captura del run activo identifica el run registrado al entrar en delegación;
no acredita por sí sola que la llamada pertenece a ese run si un caller antiguo
continúa después de que se reemplace el registro. Un contexto causal del caller
resolvería esa carrera en otro incremento.

No añade ledger independiente, índice entre parent replay y child historial,
recuperación de workers tras crash, lease, outbox ni recibos durables de mensajes.
`_save_transcript` conserva su persistencia best effort y límite de tool_events.
Los mensajes legacy siguen sin identidad recuperable; los identificadores
cortos existentes no ofrecen una garantía universal de ausencia de colisiones.
