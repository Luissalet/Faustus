# H08 parcial: compactar progreso por identidad de llamada

Estado: **corrección de replay implementada**, no ledger causal independiente.
Origen: [H08 y métodos 27.3/27.8 del análisis local](CODEX_HARNESS_ANALISIS_2026-09-29.md),
con referencia conservada al
[recorder de Codex](https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/rollout/src/recorder.rs).
Se inspeccionó el código local vigente; no se repitió revisión upstream.

## Fallo observado

El productor principal incluye `call_id` en `tool_progress`. Sin embargo,
`agent_runs._compact_key` solo usaba herramienta, ronda y aprobación. Dos
eventos consecutivos de llamadas diferentes a `bash` en ronda 1 compartían
clave. `_publish` sustituía el primer evento por el segundo y `_read_log`
también conservaba solo el último evento del mismo slot. En el piloto aislado,
`trace_for_call("call-a")` devolvía `found=False`, mientras `call-b` sobrevivía.

La corrección incluye `call_id` en la clave cuando está presente. Usa una
representación estructurada para evitar ambigüedad de separadores. Dos ticks de
la misma llamada siguen ocupando un slot y mantienen secuencia estable; eventos
antiguos sin ID conservan su comportamiento. La identidad no se reconstruye a
partir del contenido de salida ni del texto del comando.

## Evidencia y validación

Pruebas con buffer real, archivo JSONL temporal, lectura del log y trazabilidad
tras retirar el run de memoria. Verifican dos IDs de la misma herramienta/ronda,
IDs con separadores, coalescencia dentro de una llamada y eventos legacy.
Otra prueba intercala progreso con intención durable y efecto desconocido:
los dos eventos de efecto permanecen y recuperación sigue indicando incertidumbre.

Selección inicial: **52 pruebas pasan en 3,37 s**, incluyendo H03/H04,
observabilidad y reemplazo del log. Tras endurecer la aserción de traza solo en
disco, las **4 pruebas propias pasan en 0,75 s**.

```powershell
venv/Scripts/python.exe -m pytest tests/test_codex_h08_replay_call_identity.py tests/test_codex_h03_durable_email_intent.py tests/test_codex_h04_effect_results.py tests/test_obs_agent_runs.py tests/test_agent_run_log_replacement.py -q
```

## Lo que sigue pendiente

No se observó que `tool_effect` se compactara: el fallo concreto afectaba
identidad/traza de progreso. No se conserva cada tick histórico de una misma
llamada ni se separa almacenamiento causal de representación visual.
El log sigue siendo por sesión y puede reemplazarse entre runs. La proyección
de progreso de subagentes inspeccionada omite `call_id` antes de llegar aquí;
su rama no se modifica ni se presenta como resuelta. Estos son entregables
separados de H08, no garantías implícitas de este cambio.
