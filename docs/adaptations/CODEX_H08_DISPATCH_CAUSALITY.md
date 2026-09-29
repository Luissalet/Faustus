# H08 parcial — origen causal estable durante despacho

Fecha: 2026-09-30. Continúa [identidad de workers](CODEX_H08_SUBAGENT_CAUSAL_IDENTITY.md)
y [H06 recibos](CODEX_H06_SCHEMA_RECEIPTS.md). Referencia original fijada:
[Codex b1e72963c3b71a9265a551e54beff078384efed9](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9).
Implementación propia; no se repitieron revisiones upstream ni usaron modelos.

## Pérdida reproducida

Una fixture con `_direct_fallback` real llevaba `old-detached-run` en las opciones
del caller, pero `_RUNS[session]` ya identificaba `new-detached-run`. El handler
veía el run antiguo en `ctx.run_id`, ningún call_id, y el helper de delegación
seleccionaba el run nuevo. El parámetro `call_id` de `execute_tool_block` existía,
pero no llegaba al handler. Las opciones/trazas del loop permiten fallback a
sesión; el UUID de seguridad tiene otra finalidad. Ninguno sustituye al origen
real del run detached. El binding PDF/H06 sólo aporta contratos y hashes.

## Cambio

`src/run_causality.py` es un módulo stdlib con records frozen `RunOrigin` y
`CallOrigin` y una ContextVar server-owned. `_drain` vincula su propio
`run.run_id` y sesión antes de consumir el generador y resetea el token en finally,
incluidos error y cancelación. No consulta el registro mutable de sesiones.

Antes del primer await, `execute_tool_block` captura el origen y su parámetro
call_id existente. Lo transmite mediante keyword privado al impl y al fallback,
que lo expone como `_causal_call` en el contexto del handler. El branch especial
de `delegate_agents` también lo transmite. No cambia el `run_id` legacy, políticas,
aprobaciones ni el registro H03 de intención/efecto.

La delegación acepta exclusivamente un CallOrigin cuya sesión coincide con el
padre y fija parent_run_id/parent_call_id/delegation_id una sola vez para workers
y reviewer. Retries conservan ese snapshot. El transcript añade parent_call_id.
Si falta el snapshot, parent_run_id queda None aunque haya otro run en `_RUNS`;
si falta el call_id explícito queda None. Una sesión hija no hereda el run padre
desde la ContextVar, pero conserva el call_id explícito real de su propia llamada.
No guarda argumentos, cuerpos, tokens ni nuevas credenciales.

## Evidencia

`tests/test_subagent_dispatch_causality.py` recorre drain, dispatcher real,
delegate real con executor sintético y SQLite temporal con reapertura. Cubre
caller antiguo/registro nuevo, dos tareas concurrentes con misma sesión y distintos
run/call, mismatch de sesión hija, ausencia de origen, ID nativo y reset tras
error/cancelación y fallo de finalizador restaurando un contexto exterior. El
reset es la primera acción de finally, antes de limpieza que pueda fallar. Se adapta la suite anterior
para transmitir snapshot en vez de simular una consulta al registro actual.

Selección inicial: dispatch+identidad+H03 email+reemplazo de log: **29 pasan
en 15,42 s**. Comprobación ampliada de colas, H08 por run, retries y recibos H06:
**80 pasan en 20,64 s**. Tras añadir el control de fallo de finalizador, los
**7 casos** de la suite de despacho pasan en **6,06 s**.
`git diff --check` limpio salvo avisos CRLF.

## Límites

No vincula aprobación reanudada con llamada original: ese caller usa su approval_id
existente como call_id. No crea IDs para callers directos sin snapshot/call_id.
Workers anidados no tienen aquí su propio run detached: la comprobación de sesión
impide atribuirlos falsamente al coordinador, pero falta su origen run propio.
Los hijos async de una tarea heredan su contexto: una tarea separada que sobreviva
a su run conserva el origen de ese run, no acredita que continúe autorizado.

No añade autoridad, ledger independiente, outbox, idempotencia externa ni garantías
durables del transcript best effort. El registro de intenciones H03 aún tiene su
propia selección de run; este incremento sólo corrige procedencia en delegaciones.
