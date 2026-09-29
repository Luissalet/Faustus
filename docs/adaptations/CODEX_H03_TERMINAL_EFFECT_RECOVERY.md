# H03 parcial — efectos inciertos en logs terminales

Fuente conceptual ya visitada: [H03/H08 del análisis local](CODEX_HARNESS_ANALISIS_2026-09-29.md) y [Codex fijado b1e72963](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core). Implementación propia sobre la recuperación existente de Faustus; no equivalencia completa con Codex ni nueva revisión del catálogo upstream.

## Fallo observado

Un journal sintético real registró un tool_effect pending durable y cerró su run como error. _partial_from_events reconocía el efecto incierto, pero recover_interrupted_runs devolvía vacío porque descartaba todos los logs terminales antes de examinar efectos. La reproducción usó directorio temporal y SessionManager/SQLite reales; no ejecutó correo ni HTTP externo.

## Incremento

La recuperación examina efectos de logs done, error, stopped, waiting_user e interrupted antes de descartarlos o aplicar retención. Usa el reducer existente: pending sin resultado final, partial y unknown conservan incertidumbre; confirmed/failed la resuelven dentro del journal. La recuperación de generación interrumpida sigue su camino existente. No se registran runs activos ficticios ni se reescriben headers como running/interrupted para recuperar un efecto terminal.

Cuando hay incertidumbre terminal, se reutiliza persist_recovered_message: upsert SQLite por run_id, con commit confirmado. Si existe un assistant del mismo run, su contenido y metadata previos permanecen; sólo se fusionan unknown_effects por idempotency_key y se añade terminal_effect_recovery con terminal_status y digest. El evento message_saved no sustituye ese commit de metadata ni suprime la actualización. Si no hay assistant previo se guarda un aviso nuevo veraz, sin afirmar que una generación terminada siga interrumpida. El preámbulo unknown_effects_system_block distingue este caso del aviso previo de reinicio.

Sólo después del commit confirmado se añade al mismo JSONL un acuse effect_recovery (run_id y SHA-256 del conjunto causal de efectos inciertos más estado terminal), con flush/fsync. No es un nuevo estado del run ni una resolución del efecto. El sidecar de cache se refresca con el mismo estado terminal real porque la longitud/mtime del log ha cambiado. La recuperación siguiente omite la actualización si ese acuse coincide; un nuevo pending/unknown cambia el digest y exige un nuevo commit. El transcript sigue conteniendo la advertencia, no una autorización de reenvío.

Si falla SQLite, no se añade acuse, no se modifica primero el mensaje en memoria y no se poda el journal. Si falla append/fsync tras el commit, se intenta retirar el append parcial; el siguiente arranque vuelve a hacer upsert por el mismo run_id y no duplica mensajes. El log queda para reintentar el acuse. Una excepción de lookup conserva el log; una sesión confirmada ausente mantiene el purge deliberado existente y no se recrea. Sin un manager que ofrezca commit confirmado, el journal incierto no se acusa ni se poda.

La retención usa el mtime capturado antes de añadir el acuse, conserva keep_hours y se aplica a journals inciertos sólo después de commit y acuse confirmados. Un fallo de recuperación no transforma un log antiguo en candidato seguro a borrar.

## Validación y límites

Fixture de producción con _RunLog/_EffectRecorder, reducer, recuperación, SQLite y reapertura reales, receptor externo inexistente. El archivo nuevo cubre cinco estados terminales por tres estados inciertos, resolved skip, mensaje previo con message_saved y campos ajenos, dedupe por clave y por run, digest actualizado, fallo real del commit SQLite, fallo de lookup, sesión ausente, commit no confirmado, fsync fallido después del commit, reintento y retención. _RUNS permanece intacto.

Selección final: **80 pruebas aprobadas**, 31,02 s, incluyendo recuperación terminal nueva, cola/persistencia, lector reducido, recorder causal, correo con intent durable, despacho causal y observabilidad. Selección adicional de A05, reemplazo de journal, H08 por run, resultados de efecto H04 y normalización: **49 aprobadas**, 5,77 s. Son selecciones separadas; no se suman al lote inicial de 51 porque éste se solapa.

```text
venv/Scripts/python.exe -m pytest -q tests/test_terminal_effect_recovery.py tests/test_agent_runs_queue_persist.py tests/test_run_recovery_skip.py tests/test_effect_recorder_causality.py tests/test_codex_h03_durable_email_intent.py tests/test_subagent_dispatch_causality.py tests/test_obs_agent_runs.py
venv/Scripts/python.exe -m pytest -q tests/acceptance/test_a05_unknown_effect.py tests/test_agent_run_log_replacement.py tests/test_codex_h08_per_run.py tests/test_codex_h04_effect_results.py tests/test_l67_call05_agent_runs_normalize.py
```

H03 sigue parcial. No añade journal propio para workers ni habilita su correo, no reenvía ni reintenta acciones externas, no proporciona deduplicación SMTP/outbox, no crea nueva aprobación y no cambia owner/policy. El acuse confirma únicamente que una advertencia fue persistida, no que la acción ocurrió o dejó de ocurrir. El upsert actual de SQLite corresponde al startup secuencial; no es una nueva garantía de concurrencia entre procesos. Para detectar estos efectos ahora se leen también journals terminales antes omitidos: aumenta el trabajo de lectura al arrancar. La poda deliberada por sesión borrada y logs ya perdidos siguen fuera de recuperación.
