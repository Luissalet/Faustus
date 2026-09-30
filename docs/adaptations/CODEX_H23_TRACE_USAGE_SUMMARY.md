# H23 parcial: uso y ejecución en el listado de trazas

La llamada completa ya guardaba `run_id`, uso observado y duración, pero
`list_calls` descartaba identidad y uso. El listado del chat no permitía comparar
el gasto registrado entre ejecuciones sin abrir cada llamada completa.
Tres pruebas nuevas fallaban antes por campos ausentes.

El resumen incorpora el run registrado, sin inferirlo desde la sesión, y una
lista explícita de métricas escalares de uso: contadores, coste informado,
fuente y estado del coste. Conserva cero y dimensiones parciales. No calcula
totales, precios ni costes ausentes. Rechaza booleanos, negativos, valores no
finitos y campos arbitrarios; un valor inválido no elimina los demás.
Las filas antiguas quedan con run desconocido y uso vacío cuando no lo guardaban.
No incorpora request ni thinking al listado. La autorización de la ruta existente
no cambia.

Cuatro casos nuevos cubren runs distintos del mismo chat, uso parcial/cero,
filtrado de datos inválidos y lectura de JSONL temporal real con comparación
contra el detalle completo. Selección final de summary, llm_trace y trazas
auxiliares: **53 correctas en 5,49 s**. Sin modelos ni bases personales.
Una primera invocación de selección usó un nombre de archivo inexistente y no
ejecutó pruebas; la selección final utilizó `test_aux_llm_usage_trace.py`.

Fuente conceptual ya visitada: [Codex fijado](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9),
H23 del [análisis](CODEX_HARNESS_ANALISIS_2026-09-29.md). Implementación propia,
sin nueva revisión upstream ni radar.

H23 sigue parcial: no añade interfaz ni agrupación por fase. No inventa la fase
de llamadas históricas ni representa duración de llamadas como latencia causal
total del turno. La cobertura de auxiliares y la exactitud del coste dependen de
los recibos realmente disponibles; sin métricas permanecen desconocidas.


### VISITADO / IMPLEMENTADO — fase explícita de auxiliares en trazas

`8889446f`: ContextVar limitado a compaction/recovery y step2/3 identifica únicamente llamadas efectivamente realizadas. Scope alrededor de inferencia; reset ante excepción/cancel, anidación y concurrencia. JSONL, listado y detalle conservan phase/step opcionales. Históricos sin fase quedan desconocidos; no se infiere foreground ni se inventan llamadas para compactación determinista. Uso/coste no se suman aquí ni se copian arrays por turno a todas las trazas.

13 nuevas correctas1,02s; selección final65 correctas43,52s con las13; coordinador13 correctas1,23s. Selección previa168 correctas78,64s incluía11 nuevas antes de las2 últimas: no se declara final congelada. JSONL flush/reopen real; compactador/ladder reales con inferencias sintéticas. Evaluación previa SQLite confirma que recovery_usage/compaction_usage persisten en metadata del chat; no se encontró truncamiento de esos arrays. Vista/agrupación causal y facturación universal siguen pendientes. Fuente original ya visitada: https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9; adaptación propia.


### VISITADO / IMPLEMENTADO — fase de trazas con cierre diferido

Nueva pregunta tras8889446f: wrapper streaming podía cerrar generador en shutdown_asyncgens después de salir scope recovery. Repro real helper/error503 con usage7/3 retenía tokens pero phase/step ausentes. `19167960`: ambos wrappers llm_core capturan tupla inmutable al entrar y la pasan explícita a record_call. Sentinel privado conserva compatibilidad registro directo, snapshot(None,None) no adopta fase de otro contexto. Sin cambio de uso/coste.

Final127 correctas44,89s en8 suites,7 nuevas (selección previa42 correctas2,18s). Helper pasos2/3+asyncgenfinalizer, earlyclose en contextoB conservaA; llamada iniciada desconocida sigue desconocida; nonstream cambia contexto dentroimpl; JSONL actualflush/list/detail y uso observado. Coordinador selección48 correctas2,72s con7 nuevas+recall+fetch12. No modelo/provider real ni UI; agrupación causal/factura general pendiente. Fuente original ya visitada https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9; adaptación propia. Matiz diferido visitado/cerrado, no repetir sin regresión/version.
