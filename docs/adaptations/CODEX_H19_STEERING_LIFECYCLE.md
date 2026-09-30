# H19 parcial — recepción de instrucciones durante un intento de worker

Fecha: 2026-09-30. Continúa [H19 ámbito](CODEX_H19_STEER_SCOPE.md) y el
[análisis original](CODEX_HARNESS_ANALISIS_2026-09-29.md), con referencia fijada
a [Codex b1e72963c3b71a9265a551e54beff078384efed9](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9).
Adaptación local, sin repetir el análisis de padre sin resolver ni el recorder.

## Fallo reproducido

Durante `await emit(done)` de `_run_subagent`, el run ya tiene `finished` pero la
tarea asyncio aún no terminó. La admisión comprobaba solamente `task.done()`.
Con stream sintético, `steer_worker` devolvió True en ese punto, steered quedó
en cero y la cola tenía una entrada. La limpieza del coordinador eliminó el
registro; la instrucción no se podía recuperar. No se enviaron mensajes reales.

## Cambio

`SubagentRun.accepts_steers` comienza False. Cada intento lo abre inmediatamente
antes de consumir el loop y lo cierra en finally antes de emitir done o guardar
el transcript. El error de ejecución y los eventos terminales `agent_terminal`
y `event: error` cierran también antes de emitir su error;
cancelación cierra en finally. `steer_worker` exige esta puerta además de los
controles de tarea y registro existentes. El siguiente intento/retry la abre de
nuevo, sin depender del timestamp `finished` que puede pertenecer al anterior.

No vacía ni reescribe entradas pendientes al cerrar; un retry puede consumirlas
por la misma callback existente. Tampoco cambia resolución del padre, permisos,
destinatarios ni formato/normalización de mensajes. True sigue significando
**encolado**, no leído, inyectado ni persistido.

## Pruebas

`tests/test_worker_steering_lifecycle.py` usa `_run_subagent` real con loop fixture,
cola real y `_apply_steers_to_messages` real, sin modelos. Nueve casos cubren
done con tarea viva, instrucción aceptada e inyectada, retry con finished previo,
error, cancelación, registro todavía no iniciado, conservación de cola para retry
y ambos formatos de error terminal durante fanout con tarea aún viva y retry posterior.

La fixture `fake_worker` de `tests/test_subagent_steer_routes.py` marca su puerta
abierta porque representa un worker dentro del loop; no se debilitan asserts.
La suite board usa el loop real del worker y no requirió cambios.
La primera selección de lifecycle/board/rutas pasó 43 pruebas en 30,36 s;
la selección ampliada con el control de cola/retry, retries y causal identity
pasó **70 pruebas en 40,65 s**.
Tras cerrar los dos formatos de error terminal antes de fanout, lifecycle/board/rutas
pasaron **46 pruebas en 30,69 s**.

## Límites

Una instrucción aceptada justo antes de la última ronda puede quedar sin consumir.
No hay recibo individual queued/applied/dropped, persistencia durable de la cola,
reconciliación tras reinicio ni recuperación de un worker eliminado. Se mantiene
el contador steered como señal de inyección observada, sin confundirlo con éxito
de la tarea ni lectura del modelo. La ruta HTTP conserva su respuesta existente
de aceptación/404; no se añade un estado durable ni un canal de notificaciones.

## Cierre de recepción antes de guards terminales, 30-09-2026

Nuevo residual reproducido con _run_subagent real: durante fanout de un guard
rounds_exhausted, la tarea sigue viva y steer_worker aceptaba una instrucción
que nunca se consumía (steered0 y cola1). La recepción ahora se cierra antes
del await de guard para rounds_exhausted, budget_exceeded e intent_nudge_exhausted.
loop_breaker_triggered conserva recepción: la recuperación puede continuar.
No borra la cola previa y el siguiente intento/retry vuelve a abrirla.

Coordinador:16 pruebas lifecycle correctas en2,16s; incluyen rechazo durante
fanout terminal con tarea viva, conservación de cola, retry y recuperación
no terminal con inyección. Usa worker/cola reales, LLM fixture; no inferencia
GPU ni servicios personales. Scope acotado: no arregla toda entrada aceptada
antes del último drain, recibos durables, reinicio ni guards distintos como
budget_exhausted/cancelled. No concede rondas ni cambia presupuestos/permisos.
Fuente original del análisis sigue https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9,
sin revisión repetida; adaptación propia al lifecycle actual de Faustus.
Commit `2b5b6a3f`; selección final86 correctas46,87s lifecycle/rutas/board/retries/causal/dispatch. No sumar suites focales solapadas.
