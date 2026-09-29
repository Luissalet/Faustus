# H08 parcial — origen propio de cada invocación de worker

Fuente conceptual ya visitada: [H08 del análisis local](CODEX_HARNESS_ANALISIS_2026-09-29.md), [despacho causal previo](CODEX_H08_DISPATCH_CAUSALITY.md) y [Codex fijado b1e72963](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core). Implementación propia en Faustus; no equivalencia completa con Codex ni nueva revisión upstream.

## Fallo reproducido

Fixture temporal con _run_subagent, stream_agent_loop, execute_tool_block y DelegateAgentsTool reales, respuesta nativa sintética y SQLite real. El coordinador tenía un origen válido, pero el worker heredaba esa ContextVar con la sesión del padre. El guard de sesión descartaba correctamente ese origen: el dispatcher veía CallOrigin(child_session, run_id=None, call_id=nested-native-call). El transcript del nieto conservaba la llamada nativa, pero parent_run_id era None. Una assertion de origen propio falló antes del cambio. No se enviaron correos ni se consultaron modelos o HTTP externos.

## Cambio y lifetime

_run_subagent genera un UUID server-owned por consumo de stream, después de preparar opciones y antes de iniciar la inferencia. Ese mismo UUID sobrescribe harness_options.run_id y se vincula a la sesión del worker mediante bind_run: las trazas y el harness utilizan la identidad efectiva del mismo intento. No usa sesión, worker_id, delegación o identificador recibido del modelo como alias. Cada retry obtiene un UUID nuevo; los campos lógicos parent_session, parent_run_id, parent_call_id y delegation_id existentes conservan su significado y snapshot original.

El token causal se restaura como primera acción del finally, antes de clear_busy, guardar transcript y emitir done. Error, cancelación y fallos de finalizador no filtran el origen al coordinador. Los workers concurrentes tienen contextos separados. invocation_run_ids conserva sólo los últimos ocho UUID en el objeto y transcript best effort: incluye el máximo de dos intentos del retry actual y evita crecimiento sin límite ante reutilizaciones manuales repetidas. No es un journal durable ni permite recuperar todos los intentos antiguos.

## Correo sin recorder

El binding del worker no hereda el recorder del coordinador. Lleva un requisito privado server-owned require_durable_email_intent=True, cuyo valor por defecto en otros bindings es False. El dispatcher captura el requisito antes del primer await, con el origen y recorder. Si una invocación de worker solicita send_email/reply_to_email o sus aliases MCP sin recorder durable o sin call_id, devuelve EFFECT_INTENT_NOT_PERSISTED y effect_not_dispatched=True antes de ejecutar el receptor. No fabrica un recorder para hacer pasar el guard ni copia esta política desde opciones públicas del modelo.

El foreground legacy conserva su contrato previo; el gate del detached root con recorder real conserva fsync/intent antes del despacho. Lecturas y otros efectos conservan sus contratos existentes. El cambio no concede permiso para enviar: incluso granted o una identidad causal válida no sustituyen al recorder requerido. El journal/outbox propio del worker sigue pendiente.

## Validación y límites

El nuevo test_worker_run_origin.py recorre worker y nieto reales con SSE nativo simulado, dispatcher real y transcript SQLite con reapertura. Verifica parent_run_id/call_id y que el run_id del contexto real llm_trace coincide con el origen del intento. Otros casos verifican retries con opciones recibidas inválidas sobrescritas, UUID nuevos, metadata limitada, reset en éxito/error/cancelación/finalizador/emisor fallido, concurrencia y rechazo del correo con receptor sintético prohibido. Los tests no envían mensajes externos.

H08 continúa parcial: no crea managed_run ni cambia drain, cuentas de presupuesto, límites/configuración o políticas de delegación. No ofrece durabilidad, replay de todos los workers, checkpoint portable o outbox por el hecho de crear un UUID. El transcript es best effort. Las tareas async que sobrevivan a su worker conservan el contexto heredado de esa invocación; el origen no acredita autorización vigente.


Selección final: **243 pruebas aprobadas**, 60,81 s. Incluye el caso que comprueba el contexto real de llm_trace tras el cambio; el lote inicial de 59 se solapa y no se suma.

```text
venv/Scripts/python.exe -m pytest -q tests/test_worker_run_origin.py tests/test_subagent_dispatch_causality.py tests/test_subagent_causal_identity.py tests/test_codex_h03_durable_email_intent.py tests/test_effect_recorder_causality.py tests/test_subagent_retry_usage.py tests/test_subagent_outcome_history.py tests/test_worker_steering_lifecycle.py tests/test_agent_harness.py tests/test_chat_team.py
```
