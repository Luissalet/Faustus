# H14 parcial: uso observado del compactor en el presupuesto del turno

Fuente conceptual ya visitada: [H14/H16 del análisis local](CODEX_HARNESS_ANALISIS_2026-09-29.md) y [Codex fijado b1e72963](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core). Implementación propia sobre el [recibo auxiliar previo](CODEX_H14_AUX_USAGE_TRACE.md); sin nueva revisión upstream ni del catálogo de 41 proyectos.

## Fallo reproducido y cuenta elegida

En el circuito raíz real, la preparación de rutas ejecutó dos compactaciones con POST simulado de 80 tokens de entrada y 20 de salida cada una. El uso observado alcanzó 200, pero `_budget_ledger.tokens` permaneció en cero y el agente admitió su inferencia principal bajo un presupuesto sintético de 100. Un sentinel interrumpió esa inferencia antes de cualquier modelo o red real. No se leyó una traza para reconstruir consumo ni se usó una sesión como identidad de presupuesto.

La cuenta corregida es el objeto `autonomy_budget.Ledger` que el propio turno ya había creado, capturado por una función local. No es `budget_account`, cuya cuenta de delegaciones sigue siendo independiente. Un observador privado conecta el recibo de `llm_call_async`, `summarize_rows`, `maybe_compact` y la preparación de rutas. Cada respuesta fresca con métricas válidas llega una vez al ledger, directamente desde la recepción del proveedor.

## Conteo, gasto y procedencia

Para tokens se prefiere `total_tokens` válido informado. En su ausencia se suman sólo dimensiones válidas conocidas, conservando la marca `tokens_lower_bound`: no se estima una dimensión ausente ni se fuerza a coincidir un total inconsistente con su reparto. El validador de contadores existente rechaza booleanos, negativos, fracciones, no finitos y valores fuera de rango. Un recibo sin tokens válidos ni coste no genera un cargo artificial de cero.

Para gasto se reutiliza la política vigente `autonomy_budget.remote_spend_units`: un `cost_usd` realmente informado reemplaza el proxy de tokens. Sin coste monetario se usan exclusivamente contadores observados como proxy de unidades del presupuesto remoto, identificado como `remote_spend_source=observed_token_proxy`. Esto no convierte el coste desconocido en USD conocidos ni consulta precios. Un recibo con coste y sin tokens puede agotar el gasto manteniendo los tokens desconocidos; su evidencia no incluye `charged_tokens` inventados.

El observador recibe sólo un booleano privado `endpoint_local`, calculado a partir del `target_url` efectivo de la petición del resumidor. No infiere ese carácter desde el modelo/endpoint principal ni guarda una URL adicional. Un resumidor local queda excluido del cargo aunque el modelo principal sea remoto. Un resumidor remoto puede aportar consumo aunque el principal sea local, pero el principal local mantiene su política previa de presupuesto ilimitado y bypass: este incremento no le crea límites nuevos.

Los recibos se muestran separadamente en `metrics.compaction_usage`, con fuente `reported_engine`, cargo de tokens cuando se conocen, cargo de unidades remotas y procedencia de ese cargo. Los buckets del stream principal permanecen intactos; no se suman estos recibos por segunda vez en sus totales ni se mezclan con sus estimaciones.

## Parada y compatibilidad

El chequeo existente al inicio de cada ronda ve ahora el consumo de preparación y puede impedir la inferencia principal que el circuito anterior permitía. No reserva ni comprueba admisión antes de cada auxiliar: la preparación inicial puede realizar más de un POST antes de llegar a ese chequeo. Se inicializan respuesta/reasoning vacíos antes de las rondas para que esta salida previa a la primera inferencia complete su finalización sin acceder a variables inexistentes.

Una preparación fallback puede compactar después de ese chequeo. Por eso `_candidate_request` comprueba el mismo ledger y presupuesto después de preparar el contexto y antes de inferir el candidato. Una excepción privada de preparación con `fallback_eligible=False` hace volver al ensamblador; éste traduce su flag local a `budget_exhausted` con la razón existente y sale del turno antes de recuperaciones, retries o nuevas rondas. No muestra un error del proveedor para ese agotamiento. La precedencia de un override booleano explícito se conserva también cuando la política genérica de fallback no trae lista de statuses; sin override, su política anterior no cambia. Los fixtures incluyen un tercer candidato que no llega a prepararse ni inferirse.

El retorno público de `llm_call_async` sigue siendo string o texto/modelo. El observador es privado, local a la invocación y sus errores quedan contenidos. Una caché no llama al observador con uso fresco; un error de schema después de recibir métricas sí cuenta. La cancelación anterior a recibirlas no fabrica consumo y una cancelación posterior conserva el cargo ya observado. No se modifican límites, configuraciones, permisos ni política de inferencia local.

## Evidencia y límites

La selección amplia de integración aprobó **283 pruebas**, 146,06 s, antes de incorporar el cargo de coste sin tokens. Incluye presupuesto de autonomía, traza auxiliar, streaming/retries/fallbacks y compactación. El caso adicional de coste aislado reprodujo el fallo antes del ajuste y pasó después. La selección focal posterior aprobó **45 pruebas**, 32,48 s: todos los fixtures de cargo del compactor, recibos auxiliares y política de coste de autonomía. Estas selecciones se solapan; no son 328 casos distintos.

Los fixtures usan el turno raíz real y POST simulado. Para probar admisión se prohíbe iniciar la inferencia principal, o se simula sólo la disponibilidad del stream primario mientras se conserva el wrapper real de fallback. Las rutas de compactación, normalización, ledger y parada son código de producción. Se cubren remoto inicial, fallback con dos políticas y tres candidatos, utility local, bypass principal local, totales/partial, caché, schema inválido, cancelación antes/después, observador fallido, coste sin tokens y suma independiente con stream principal. No se cargaron modelos ni se usaron servicios o DB personales.

```text
venv/Scripts/python.exe -m pytest tests/test_compaction_budget_receipts.py tests/test_aux_llm_usage_trace.py tests/test_autonomy_budget.py tests/test_autonomy_budget_cost_usd.py tests/test_llm_core_fallback.py tests/test_llm_core_streaming_retries.py tests/test_context_compactor.py tests/test_context_compactor_regressions.py tests/test_llm_trace.py -q --tb=short
```

Selección posterior al ajuste de coste aislado:

```text
venv/Scripts/python.exe -m pytest tests/test_compaction_budget_receipts.py tests/test_aux_llm_usage_trace.py tests/test_autonomy_budget_cost_usd.py -q --tb=short
```

H14 continúa parcial: esto cubre la compactación llamada desde preparación de rutas del agente, no todos los revisores, síntesis finales, manual condense, workers u otros auxiliares. No unifica cuentas de delegación, no reserva antes de inferencia, no inventa consumo sin métricas y no convierte unidades aproximadas en un precio exacto. Compactación extractiva/determinista no produce gasto LLM; métricas no recibidas permanecen desconocidas.


### VISITADO / IMPLEMENTADO — H14 consumo de recuperación

`28385388`: observer privado opcional step_completion→ladder→ambos callers captura último valor válido por campo/step, sin sumar snapshots acumulativos repetidos. Error/cancel conserva observación en finally; partial/cost-only no borra tokens previos ni coste conocido. Retorno4tuple legacy intacto. Recovery_usage separado de buckets principales/compactor y cargo al ledger presupuestario del turno; coste desconocido no se inventa. Gate antes de same-model/utility usa política vigente y presupuesto observado; agotamiento termina caller/round sin tools ni nueva inferencia. ASTlifecycle incorpora dos nuevas emisiones (6total) conservando4anteriores y comprueba latch antesfanout/outerbreak.

Repro real loop/ladder/helper con proveedor sintético: usage800input/200output descartado antes (ledger0), ahora ledger1000/receipt1000. Ambos callers degenerate/ctx_ack con step2 de100tokens alcanzan cuota: utilidad/roundposterior/tools no despachan. Final53 correctas17,25s (30nuevas+lifecycle21+cost2); coordinador30 nuevas14,37s. Amplia previa98 correctas/1 fallo197,85s: único fallo era assert estructural de4emisiones desactualizado, corregido y repetido final; selección71 correctas103,29s tenía26nuevas antescontrolesfinales, no freeze global. Scope3filespropios.

Límites: recuperación local excluida de cargo/receipt según política existente; turno originalmente local conserva grant ilimitado aunque utility sea remota (su consumo remoto sí observado/cargado, no límiteinventado). No reserva previa, unión de cuentas ni factura universal para retries internos sin identidad. Métricas principales mantienen su significado, consumo recovery aparte. Fuente https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9 y auxiliareslocales ya visitados. Nuevo timing de uso principal antes recovery sólo evaluación, no cerrado. Sin modelos/GPU reales ni cambios personales.
