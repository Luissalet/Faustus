# H16 parcial: resultados del worker desde SSE hasta historial

El loop ya enviaba `result_status` e incertidumbre, pero `_run_subagent` decidía
éxito solo por código de salida y descartaba esos campos al guardar el historial.
Reproducción: `web_fetch` con resultado desconocido y código cero emitía `ok=True`;
su historial sin estado tipado volvía a contar como página consultada en el harness.

Ahora el worker reutiliza el criterio de evidencia del harness, conserva el estado
canónico del SSE y transmite/persiste incertidumbre y `call_id`. La incertidumbre
admite solo `reason` y `reconcile_action`, de hasta 512 caracteres; la identidad de
llamada también queda acotada. No se transforma un evento normalizado en un resultado
heredado ignorando su estado. Estados canónicos malformados quedan desconocidos.
Eventos heredados sin estado mantienen la compatibilidad del normalizador compartido.

Una declaración `succeeded` contradictoria con error, bloqueo, aprobación pendiente,
código no cero o estado crudo inválido/no exitoso se persiste como `outcome_unknown`.
Así descartar atributos crudos al compactar el evento no puede convertir evidencia
no válida en verificación al reabrir el chat. Las señales contradictorias no prueban
ausencia de efectos ni permiten repetir automáticamente.

## Evidencia y alcance

Pruebas del camino real de consumo SSE, `_save_transcript`, SQLite temporal,
reapertura con un `SessionManager` nuevo y `TurnLedger.note_prior_message`. Stream
del modelo simulado, ninguna llamada a proveedores. La fixture conecta explícitamente
el singleton del gestor usado por `Session.add_message`, como hace la aplicación.

Se comprueban siete estados con código cero/ausente, identidad, límites de
incertidumbre, eventos heredados y seis combinaciones contradictorias. La selección
incluye además board de workers, contabilidad de retries y evidencia del harness.
Resultado final: **120 pruebas aprobadas**.

`failed_calls` es el contador existente de llamadas no exitosas; incluir resultados
inciertos no significa que no hayan tenido efecto. No se rediseña el estado global
del worker ni se completa H16: verificadores de CodeMode y otros consumidores siguen
pendientes. No cambian permisos, política global de reintentos ni contrato de herramientas.

Fuente conceptual ya visitada: [H14/H16](CODEX_HARNESS_ANALISIS_2026-09-29.md) y
[Codex fijado b1e72963](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9).
Implementación propia; sin repetir revisión upstream.
