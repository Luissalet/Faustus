# H03 parcial: persistir intención antes del correo de un run trazado

Estado: **implementado el tramo seleccionado**, H03 global pendiente.
Continúa [H04](CODEX_H04_EFFECT_RESULTS.md) y el
[diseño H03/H04](CODEX_HARNESS_ANALISIS_2026-09-29.md).
Referencia original preservada:
[Codex normalización](https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core/src/context_manager/normalize.rs#L21)
y [reintentos](https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core/src/responses_retry.rs#L31).
El registro previo obligatorio es una adaptación propia del análisis local,
no una afirmación de que Codex implemente una outbox SMTP.

## Cobertura concreta

Protege `send_email`, `reply_to_email` y sus aliases
`mcp__email__send_email` / `mcp__email__reply_to_email` cuando se invocan por
`execute_tool_block` con `call_id` dentro de un run activo de `agent_runs`.
Se reutiliza `email_tool_policy_names`, que ya identifica ambos nombres como
el mismo servicio. No cambian los permisos ni las aprobaciones existentes.

El servidor `mcp_servers/email_server.py` también expone despacho directo fuera
del wrapper. Esa ruta y las invocaciones foreground sin run existen y conservan
compatibilidad; exigirles un run detached artificial rompería usos legítimos.
Se mantienen explícitamente fuera de este entregable, igual que llamadas sin
`call_id`, envíos masivos, integraciones y correo desde otras herramientas.

## Qué garantiza este tramo

Antes de invocar el handler se escribe un evento pendiente con run/call, nombre
de herramienta, owner y SHA-256 de los argumentos. No se duplica el cuerpo del
correo en esta nueva metadata. La ruta estricta confirma `write`, `flush` y
`os.fsync` del archivo antes de añadir el evento al buffer o avisar suscriptores.
El resto del replay mantiene su persistencia best-effort previa.

Si el log del run activo está ausente, cerrado, huérfano o el sistema operativo
no confirma la sincronización, se devuelve `EFFECT_INTENT_NOT_PERSISTED` y
`effect_not_dispatched: true`; el handler no se llama. Esto es un fallo de
admisión por persistencia, no una aprobación nueva. Las lecturas no se bloquean.
Una caída de clasificación de capacidades no permite saltarse esta selección
explícita de herramientas de envío.

La clave histórica `idempotency_key` del evento identifica run+call; **el
receptor no la hace cumplir**. No se afirma idempotencia SMTP ni exactamente una
entrega. Una intención escrita antes de un crash puede no haber sido despachada:
su incertidumbre se conserva para comprobarla, no se reenvía automáticamente.

## Pruebas

Receptor sintético en proceso, sin red ni correo real:

- Comprueba dentro del handler que el evento ya está en disco y sincronizado.
- Comprueba que el fanout y buffer siguen vacíos mientras ocurre `fsync`.
- Ejecuta ambos nombres nativos y ambos aliases MCP.
- Falla `fsync`, cierra/retira log y provoca error de clasificación: cero despachos.
- Confirma que una lectura y una llamada foreground sin run siguen funcionando.
- Mantiene pruebas H04 de efecto ya realizado con resultado desconocido y
  recuperación sin segundo despacho.

La selección ampliada del ledger, observabilidad, recuperación y reemplazo de log
pasó **64 pruebas en 37,37 s**. Encontró una regresión durante desarrollo en la
asignación de `step_id`; se corrigió conservando el orden previo de observación
para eventos ordinarios y la repetición completa pasó. Tras añadir el último
caso de fallo de capacidades, **29 pruebas H03/H04/A05 pasan en 1,74 s**.

```powershell
venv/Scripts/python.exe -m pytest tests/test_codex_h03_durable_email_intent.py tests/test_codex_h04_effect_results.py tests/acceptance/test_a05_unknown_effect.py tests/test_agent_runs_queue_persist.py tests/test_l67_call05_agent_runs_normalize.py tests/test_obs_agent_runs.py tests/test_obs_call_id.py tests/test_agent_run_log_replacement.py -q
```

## Límites que no se cierran

No hay outbox independiente, identidad de intento duradera, reconciliación SMTP,
deduplicación del receptor ni cobertura de llamadas no trazadas. El log sigue
el ciclo de vida actual por sesión y puede reemplazarse por un run posterior;
la retención causal independiente es H08. `fsync` confirma la solicitud al
sistema operativo, sin simular aquí pérdida de alimentación, fallo físico del
disco o persistencia del directorio. Los resultados posteriores siguen la
ruta H04; sus errores genéricos no prueban que no hubiese efectos.
