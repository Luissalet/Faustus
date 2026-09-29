# H03/H08 parcial — recorder de efectos ligado al run original

Fecha: 2026-09-30. Continúa [H03 intención durable](CODEX_H03_DURABLE_EMAIL_INTENT.md)
y [H08 despacho](CODEX_H08_DISPATCH_CAUSALITY.md). Fuente original fijada:
[Codex b1e72963c3b71a9265a551e54beff078384efed9](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9),
incluidas [normalización](https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core/src/context_manager/normalize.rs#L21)
y [reintentos](https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core/src/responses_retry.rs#L31).
La durabilidad de intención y el recorder son adaptaciones locales, no una outbox SMTP upstream.

## Cruce reproducido

Dispatcher real con handler dummy sin efectos externos: el run antiguo escribía
pending en su log temporal; el dummy sustituía `_RUNS[session]`; confirmed se
escribía en el run nuevo y llegaba a sus suscriptores, con otra idempotency_key.
`_publish(run)` ya usa el objeto exacto; la consulta mutable repetida de
`record_tool_effect` causaba el cruce.

## Implementación

`_drain` vincula un `_EffectRecorder` privado al objeto `_Run` original junto al
RunOrigin. El holder queda fuera de repr/comparación, CallOrigin, ctx del handler,
metadata y SSE. El dispatcher captura el recorder antes del primer await y usa
esa misma referencia para pending y terminal. No vuelve a consultar `_RUNS`.
La sesión debe coincidir: un worker hijo no adopta el recorder del padre.

La intención durable exige que el run capturado siga running y que su log
acepte write/flush/fsync. Si el run fue detenido antes de despachar, su log está
cerrado/orphaned/ausente o fsync falla, el correo seleccionado no se despacha;
nunca se muda la intención al run nuevo. Si el handler ya actuó, el terminal
best effort puede seguir registrándose en el escritor antiguo abierto aunque
el run esté stopped. Un escritor cerrado no recibe ese terminal ni lo deriva
al nuevo. Sin log, se conserva el fanout/buffer best effort previo para efectos
ordinarios; el correo durable sigue bloqueado por falta de log.

Sin recorder captured, el dispatcher no adopta un run registrado vecino y no
impone intención artificial a llamadas foreground. `record_tool_effect` conserva
su API legacy para callers directos que explícitamente soliciten esa selección
por sesión; esa API no promete el snapshot causal del dispatcher.
No cambia permisos, revocación de herramientas, políticas ni aprobaciones.

## Pruebas y adaptación de fixtures

Nueva suite `tests/test_effect_recorder_causality.py`: 11 casos con `_drain`,
dispatcher real, handlers dummy y logs temporales. Incluye reemplazo dentro del
handler, detención anterior, fsync, log closed/missing, cierre posterior,
concurrencia misma sesión/call_id, foreground, sesión hija, API legacy y
persistencia off conservando memoria del run antiguo y vacío el nuevo.

Antes de adaptar el contexto de la fixture H03: **8 fallos, 31 pasan**. La fixture
`tests/test_codex_h03_durable_email_intent.py::tracked_run` registraba `_RUNS`
pero no vinculaba origen/recorder. Ahora vincula el recorder explícito del mismo
run y resetea el token en finally. No cambian los asserts de fsync, rechazo,
hash de argumentos ni ausencia de secretos. H04 y A05 no necesitaron cambios.

Selección integrada de recorder, H03/H04/A05, despacho causal, colas,
reemplazo de log y observabilidad: **74 pasan en 11,02 s**.
No hay correo, modelos ni DB personal. `git diff --check` limpio salvo avisos CRLF.

## Límites

No añade outbox independiente, idempotencia externa, recibos SMTP, retry automático
ni recuperación de workers. Terminal sigue siendo best effort; cerrar el escritor
antes del retorno puede conservar sólo pending y requerir reconciliación posterior.
Las tareas async heredan recorder del run que las originó: no acredita que mantengan
autorización. La intención durable comprueba el estado running; acciones ordinarias
conservan los gates existentes. API legacy y callers directos quedan fuera de la
garantía del snapshot. Persistencia off sólo conserva memoria de proceso.
