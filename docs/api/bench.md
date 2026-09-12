# Banco de pruebas de inferencia local — INF-04 §09/§10

INF-04 (Lote A backend). Contratos en `src/contracts/inference.py`
(`InferenceProfile`, `BenchmarkCase`, `BenchmarkRun`, `Comparison`), lógica en
`src/bench/{suites,runner,profiles}.py`, rutas en `routes/benchmark_routes.py`.
Construido sobre INF-02 (`EngineIdentity`, `ModelDescriptor`,
`launch_receipts.identity_for_endpoint`) e INF-03 (`ExecutionMetrics`,
`engine_timings` en el evento `usage` de `llm_core.stream_llm`).

Regla que no se negocia: **abrir la pantalla no ejecuta nada**. `plan()` es
puro — calcula cuántos casos caben en el presupuesto y, si se conoce la
velocidad de decodificación de este modelo exacto, una estimación de
duración — y nunca toca un modelo, un proceso ni la puerta de admisión.
Solo `start()`, una acción explícita posterior, llama al motor.

## Qué mide cada capa

Dos capas, no una:

- **Rendimiento** (`ExecutionMetrics` por muestra): carga, cola, prefill,
  generación y tokens, exactamente el mismo contrato que ya llena INF-03
  para un turno de chat normal — nunca un cronómetro paralelo con sus
  propias reglas.
- **Tarea** (`quality` por muestra, `src/bench/suites.py::run_checks`): el
  recorrido real de Faustus (`llm_core.stream_llm` con la puerta de
  `vram_admission.admit` delante, igual que un chat) contra comprobaciones
  deterministas — `contains`, `regex`, `json_valid`, `json_has_keys`,
  `max_words`, `language_es` (heurístico, documentado como tal), y
  `no_tool_leak`. Ningún juez de lenguaje decide si una respuesta es
  "mejor"; cada comprobación es una función pura que un lector puede
  auditar leyendo `src/bench/suites.py`.

Tres suites deterministas (`config/benchmark_suites/*.json`, 6-8 casos cada
una), una por objetivo de producto (§09): `es_conversation` (interactive),
`tools_json` (coding_agent — formato JSON estricto, la forma que necesita un
turno de agente con herramientas), `long_documents` (long_documents —
recuperación de un dato exacto situado al principio, en medio y al final de
un documento de ~3-4k tokens embebido en el propio caso, sin ficheros
externos).

## Qué NO demuestra un resultado de este banco

- **Una muestra pequeña no prueba equivalencia de calidad.** El punto de
  partida son 3 repeticiones exploratorias; `compare()` exige `n >= 3` en
  ambos lados y, aun así, publica siempre el tamaño de muestra
  (`Comparison.sample_sizes`) junto al veredicto — nunca un `p95` fiable
  con pocas ejecuciones disfrazado de precisión.
- **Un solo motor no es una comparación de motores.** Cada `BenchmarkRun`
  lleva la `EngineIdentity` exacta que respondió (INF-02); `compare()`
  exige el mismo `suite_id`+`suite_version`, el mismo `model.artifact_id` y
  el mismo `objective` — comparar `llama-server` contra `ollama`, o dos
  versiones distintas del mismo motor, requiere una comparación de
  variantes explícita que este lote no fabrica por su cuenta
  (`comparable=False` con el motivo exacto en `reasons`).
- **Una mejora de velocidad que no supera el ruido no cuenta.** El umbral
  de mejora (mediana de `gen_tps` +10%) tiene que superar además la
  dispersión observada (`p95 - median` como proxy de variabilidad,
  documentado explícitamente como NO un test estadístico) — una ganancia
  dentro de esa banda es `inconclusive`, nunca `improvement`.
- **Agotar el presupuesto no es "optimizado".** Un run que se queda sin
  `max_seconds`/`max_generated_tokens` a mitad de camino termina en
  `partial`, con la interrupción y su motivo (`budget_seconds`/
  `budget_tokens`) registrados en `BenchmarkRun.interruptions` — nunca se
  presenta como un resultado completo.
- **El banco de rendimiento no certifica el arnés de producción por sí
  solo.** Reutiliza el mismo camino que un chat real (`stream_llm` +
  `vram_admission.admit`, sin atajos que se salten permisos o el Context
  Engine), pero solo ejerce las tres suites de este lote — el resto de la
  matriz de INF-05/§17 (programación con proyecto temporal, documentos con
  Context Engine real) queda fuera de este banco tal como está hoy.

## La regla de promoción (§13, aplicada a `InferenceProfile.evaluation`)

`src/bench/profiles.py::promote(profile_id, comparison)`:

1. Un perfil candidato solo llega a `evaluation="recommended"` cuando
   `Comparison.verdict == "improvement"` — nunca por velocidad sola, nunca
   por una comparación en caché (`compare()` se recalcula siempre en el
   momento de promover; no existe un `comparison_id` persistido que se
   pueda reutilizar más tarde con datos desactualizados).
2. Cualquier otro veredicto (`regression`, `inconclusive`, `no_change`)
   actualiza igualmente la `evaluation` del perfil para reflejar lo que de
   verdad se observó, pero **nunca lo asciende** — una regresión se
   registra como `regression` y queda como una propuesta de volver al
   perfil anterior, nunca como un cambio automático a mitad de turno.
3. Guardar un perfil (`save_profile`) y activarlo (que el chat lo use
   realmente) son decisiones distintas: `activate_profile` queda fuera de
   este lote a propósito — ver el docstring de `src/bench/profiles.py` para
   el porqué y qué necesitaría el siguiente lote.
4. Una comparación no comparable (`comparable=False`) nunca promueve ni
   degrada nada: no hay evidencia aplicable a ese perfil concreto.

## Por qué no hay juez LLM

Ningún componente de este lote usa un modelo para decidir si una respuesta
es correcta. Las comprobaciones de `run_checks` son deterministas y
auditable una por una; la puerta de calidad de `compare()` es aritmética
sobre `quality_pass_rate` y `gen_tps`. Esto es intencional y sigue la regla
del espec (§10, §13): "un juez LLM es auxiliar, no reemplaza tests ni
evidencia" — y este lote no introduce ninguno, auxiliar o no. Una
preferencia estética ("¿qué respuesta suena mejor?") no se convierte en un
hecho medible solo porque otro modelo la puntúe; ese tipo de comparación
queda fuera del alcance de "optimizar para mi equipo".

## Qué queda para INF-05/LAB

- **Candidatos con reinicio del motor o descarga de artefactos.** Este
  lote solo mide configuraciones de modelos ya instalados, sin instalar,
  descargar ni reiniciar nada (regla no negociable del contrato). Comparar
  una cuantización distinta, un binario distinto o una versión distinta
  del motor requiere una decisión explícita de instalación fuera de este
  banco.
- **Especulación (draft models, MTP, EAGLE).** §13 del espec la describe
  como P1/LAB aparte; este banco no evalúa perfiles especulativos todavía
  — `InferenceProfile` no distingue hoy un perfil especulativo de uno que
  no lo es.
- **Reparto entre GPUs/nodos remotos.** Fuera de alcance por la misma razón
  que §11/§12: la topología puede orientar candidatos, pero una
  recomendación de reparto necesita su propia medición y su propio
  conjunto de casos de aceptación.
- **`activate_profile`.** Cambiar lo que un chat en curso realmente usa es
  una decisión de producto distinta de guardar/evaluar un perfil — ver
  arriba.
- **La pantalla "Optimize for my machine" en Cookbook (Lote B/Studio).**
  Este documento cubre solo el backend; la interfaz que confirma el plan y
  el presupuesto antes de pulsar *Start benchmark* es un lote aparte sobre
  el mismo contrato.
