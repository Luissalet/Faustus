# H04 parcial: conservar resultados e incertidumbre al recuperar efectos

Estado: **implementado y probado este tramo**, H04 completo sigue pendiente.
Parte del [backlog local H03/H04](CODEX_HARNESS_ANALISIS_2026-09-29.md).
Referencia original fijada del análisis:
[normalización de Codex](https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core/src/context_manager/normalize.rs#L21)
y [reintentos](https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core/src/responses_retry.rs#L31).
Se adopta el invariante de no inventar éxito ni rollback al proyectar el resultado;
no se copia código ni se añade dependencia del runtime Codex.

## Fallo reproducido y corrección

El contrato existente acepta siete estados. El adaptador convertía `partial`,
`cancelled`, `denied` y `failed` sin campos adicionales de error en `succeeded`.
Además, el wrapper convertía cualquier resultado distinto de éxito en efecto
`failed`; la recuperación borraba el pendiente ante cualquier estado diferente
de `pending`. Una respuesta perdida después del efecto podía desaparecer del
aviso de incertidumbre al reiniciar.

Ahora la normalización conserva los siete estados, genera los errores que exige
el contrato para rechazo/fallo y conserva el motivo y acción de reconciliación
del productor. `timeout`/`timed_out` explícitos (o `timed_out: true`) se consideran
`outcome_unknown`: recibir ese estado no demuestra que el efecto no ocurrió.
La incertidumbre explícita prevalece sobre un indicador contradictorio `blocked`.

| Resultado normalizado | Evento de efecto | Tras reinicio interrumpido |
|---|---|---|
| succeeded | confirmed | Resuelto |
| partial | partial | Conservado; comprobar antes de repetir |
| outcome_unknown | unknown | Conservado con reconciliación |
| cancelled | unknown | Conservado; cancelar no demuestra rollback |
| denied / conflict / failed | failed | Resolución heredada |

El evento incluye el `result_status` original para no convertir cancelación o
parcialidad en un fallo genérico. El `call_id` disponible llega al normalizador.
Una clasificación fallida conserva incertidumbre; un estado de evento futuro o
desconocido ya no borra el pendiente. Una confirmación posterior sí lo resuelve.

## Prueba del recorrido

Un handler sintético deja un marcador de efecto en disco y responde `timed_out`.
La prueba usa `execute_tool_block`, el log real del run y su recuperación real:
conserva el log previo a la interrupción, descarta el run en memoria, recupera la
sesión y verifica que el prefacio siguiente exige comprobar antes de repetir.
El marcador sigue presente y el contador de despacho queda en uno. Se repite
con resultado parcial, desconocido, cancelado y controles de fallo/rechazo.
No se contacta ningún servicio externo ni se envía ningún mensaje.

Validación: **55 pruebas pasan en 3,22 s**.

```powershell
venv/Scripts/python.exe -m pytest tests/test_codex_h04_effect_results.py tests/test_l62_call05_tool_result.py tests/acceptance/test_a05_unknown_effect.py tests/test_agent_runs_queue_persist.py tests/test_tool_outcome.py -q
```

## Límites pendientes

- No completa la proyección de todos los eventos `tool_output` ni la UI. El
  recorrido de recuperación probado es el de un run interrumpido; una respuesta
  parcial en un run normalmente terminado requiere revisar sus consumidores.
- Un error genérico sin señal de incertidumbre conserva `failed` por compatibilidad.
  **No demuestra ausencia de efecto.** Falta inventariar productores que convierten
  timeouts de transporte en simples cadenas `error` y aportar certeza explícita.
- El registro previo sigue siendo best-effort y solo existe con `call_id` y run
  activo. No satisface todavía H03 (persistencia obligatoria antes de efectos).
- No crea un `attempt_id` duradero, no implementa reconciliación externa ni
  garantiza idempotencia en proveedores. Conserva el aviso existente contra
  reintentos ciegos; no introduce reenvíos automáticos.
- No migra logs anteriores: si un efecto antiguo ya se guardó erróneamente como
  confirmado/fallido, este cambio no reconstruye una certeza que se perdió.
