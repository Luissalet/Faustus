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
