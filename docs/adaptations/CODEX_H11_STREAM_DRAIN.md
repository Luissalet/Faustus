# H11 parcial: drenar stdout/stderr independientemente de sus líneas

Fuente ya visitada: [H11 del análisis](CODEX_HARNESS_ANALISIS_2026-09-29.md),
[procesos exec de Codex, revisión fijada](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core/src/unified_exec).
Implementación propia; no se repite revisión upstream. El piloto de reinicio
`403756a9` conserva su alcance separado: recuperación de resultados de PowerShell.

## Fallo observado el 29-09-2026

Un proceso Python temporal propio escribe 1.000.000 bytes sin salto de línea y
termina. Con captura normal: **0,07 s, código 0, 1.000.000 bytes**.
Con `_run_subprocess_streaming` anterior: **2,11 s, timeout, código 15, salida
vacía**. Se observó además una advertencia de transporte sin cerrar al terminar
el intérprete de la reproducción. No se tocaron servicios ajenos ni modelos.

El lector usaba `StreamReader.readline()`: superar su límite de línea hacía
fallar la tarea lectora. La tubería dejaba de drenarse, bloqueaba al escritor y
acababa activando el timeout. Además, las listas de salida crecían sin límite
hasta truncarse una vez terminado el proceso.

## Cambio y compatibilidad

- Lectura por bloques de 8192 bytes, drenando hasta EOF aunque ya se haya llenado
  la retención de salida. Cada stream conserva como máximo `MAX_OUTPUT_CHARS`
  (10.000 caracteres); el progreso conserva 4096 caracteres y hasta 12 líneas.
- Decodificador incremental UTF-8: un carácter dividido entre lecturas conserva
  su representación. Bytes malformados mantienen la política `replace` previa.
- Cualquier byte recibido renueva actividad, incluso sin LF o con un carácter
  todavía incompleto. No se cambia la duración de los timeouts.
- El prefijo retenido termina con `[output truncated while draining process]`
  cuando hubo pérdida. La salida pequeña conserva separación, CR y comportamiento
  anterior de quitar exactamente el LF final; stdout/stderr y exit code siguen
  separados en la misma tupla de cuatro campos.
- Cancelación sigue matando únicamente el árbol comprobado del proceso propio.
  La espera final de lectores cancelados acepta `CancelledError`, permitiendo
  liberar el registro de ownership y propagar la cancelación original.

## Pruebas

Diez casos nuevos: stdout y stderr reales de 1 MB sin LF, separados y simultáneos; UTF-8 incompleto
a EOF con replacement; salida pequeña exacta
con código 7; UTF-8 forzado a fragmentos de un byte; heartbeats sin LF durante
1,5 s con idle de 0,6 s; cancelación de proceso temporal y liberación de ownership;
progreso acotado; y límite retenido tras 8 MB sintéticos.

```text
venv/Scripts/python.exe -m pytest tests/test_subprocess_stream_drain.py tests/test_command_timeout_outcomes.py tests/test_subprocess_hardening.py tests/test_process_ownership.py tests/test_process_tree_ownership.py tests/test_process_ownership_rights.py tests/test_sandbox_policy_metadata.py tests/test_sandbox_required_mode.py tests/test_execution_cancellation.py -q
```

Resultado: **91 correctas, 3 omitidas** por semántica POSIX en Windows y fixture
Docker no disponible. Los procesos reales nuevos se ejecutaron y terminaron.

## Pendiente de H11

No se introduce un manager unificado, handle durable, stdin interactivo ni
migración de bg_jobs/tmux/contenedores. El límite expresado en caracteres no es
una medición estricta de RSS. Los errores de lectura distintos del límite de
línea y procesos descendientes que mantienen pipes abiertos requieren un
incremento específico; no se declara resuelto todo el lifecycle.


### VISITADO / IMPLEMENTADO — H11 lectura acotada del log en segundo plano

`dad52508`: bg_jobs._read_output usa un descriptor con tamaño inicial y ventanas de principio/final; conserva límite16000caracteres y marcador. Antes Path.read_bytes decodificaba archivo completo en cada get/followup. ChildPython temporal real escribió8MiB/exit0: salida16015caracteres con peak16.778.061bytes antes. Después misma salida,2lecturas total<=128033bytes/max64017 y peak<1MiB. Mantiene _read_job_text para código de salida.

Final70 correctas7,13s en5suites,36nuevas; coordinador36 correctas1,33s. UTF8sig/UTF16LE-BE, BOM integrado como contenido en tail, emoji pequeño15999caracteres, fronteras/surrogates, malformed y crecimiento/reducción durantelectura. Errores iniciales de nombres de fixtures demasiado largos en Windows corregidos con IDs cortos; no bugdelproducto. SinLLM/GPU/procesosajenos. No limita archivo en disco ni garantiza snapshot atómico frente escritor externo, no stdoutdrain nuevo ni solución stdin/handles durables.

Fuente original https://github.com/autonomous-ai/openharness y https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9, métodos/localcontratos previamentevisitados; adaptación propia, no revisión documental repetida. Piloto visitado/cerrado, H11global parcial. Scope2filespropios, cambiosajenos preservados, uso permitido y automatización activa.
