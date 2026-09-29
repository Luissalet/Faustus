# H03 parcial — fence del retry automático ante incertidumbre del worker

Fuente conceptual ya visitada: [H03/H08 del análisis local](CODEX_HARNESS_ANALISIS_2026-09-29.md), [origen propio del worker](CODEX_H08_WORKER_RUN_ORIGIN.md) y [Codex fijado b1e72963](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core). Implementación propia, sin equivalencia completa con Codex ni revisión upstream nueva.

## Circuito y cambio

La condición existente en one() reintentaba una tarea de escritura una vez si el resultado era empty/ack_only y no todas las herramientas habían fallado. Esa condición no distinguía una acción incierta de falta de trabajo. Una lectura conocida exitosa seguida de una herramienta outcome_unknown, sin mutations y con «Done.», cumplía el retry: tool_calls=2, failed_calls=1, por tanto el guard _all_refused no lo evitaba. El segundo stream recibe un UUID distinto; esa identidad no proporciona idempotencia de la acción externa.

_worker_retry_is_uncertain consume únicamente result_status canónico de tool_events, ya normalizado por el worker. partial/outcome_unknown bloquean el retry automático. cancelled también lo bloquea: el helper existente effect_state lo proyecta a efecto unknown. failed, denied y conflict conocidos no equivalen a incertidumbre y mantienen el comportamiento de retry previo. Una señal contradictoria/malformada que el normalizador proyecta a outcome_unknown también bloquea; ninguna frase en output o en la respuesta del modelo decide el fence.

Se añade el guard antes de emitir retry y antes de reemplazar la instrucción o empezar otro stream. El reporte y el transcript marcan retry_blocked_uncertainty cuando hay esa evidencia; los eventos originales conservan resultado y call_id. No se borran ni reinterpretan resultados. La señal considera todos los eventos ya observados en el objeto de ese worker, incluidos intentos previos; no depende de un texto libre de la delegación.

## Validación y límites

El test nuevo recorre DelegateAgentsTool.one() y _run_subagent reales, con SSE de outcomes sintéticos y SQLite de sessions real, sin ejecutar herramientas ni llamar a modelos/servicios. Se comprueba una lectura exitosa seguida de unknown/partial/cancelled en formatos actual y legacy, ack_only con tarea de escritura y exactamente un UUID/intento. La lectura evita que _all_refused o un error del worker expliquen la parada. También comprueba clean ack_only y fallos conocidos con dos intentos y UUID distintos, prosa que afirma éxito sin neutralizar incertidumbre y resultado canónico malformado convertido a unknown.

Alcance conservador: bloquea también ante resultados inciertos de lecturas. No determina si una acción tuvo efecto ni concede autorización. Cubre la condición de retry automático de una delegación viva; no es un fence persistente universal para nuevas delegaciones, resume manual o retries internos de otra capa.

Pendiente explícito: tool_start anunciado sin tool_output requiere un mapa causal de inicios/resultados por identidad real. El worker aún no lo conserva; este incremento no protege ese caso ni deduce el resultado ausente. Tampoco impide por sí solo repetir una acción conocida como exitosa, ni ofrece deduplicación SMTP/outbox. No conecta un journal/recorder al worker ni habilita correo: el rechazo previo cuando falta intent durable permanece igual.


Selección final después del guard conservador: **97 pruebas aprobadas**, 56,82 s. El lote anterior de 69 se solapa y no se suma. Incluye 12 casos nuevos, receipts/retry H3, persistencia de outcomes, origen propio, chat team y consumo de reintentos.

```text
venv/Scripts/python.exe -m pytest -q tests/test_worker_retry_uncertainty.py tests/test_h3.py tests/test_subagent_outcome_history.py tests/test_worker_run_origin.py tests/test_chat_team.py tests/test_subagent_retry_usage.py
```
