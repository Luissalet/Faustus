# H14 parcial: recibo observado de uso auxiliar en la traza

Fuente conceptual ya visitada: [análisis local H14/H16](CODEX_HARNESS_ANALISIS_2026-09-29.md) y [Codex fijado b1e72963](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core). Implementación propia a partir del backlog; no se revisaron de nuevo los 41 proyectos ni se ejecutaron modelos o peticiones externas.

## Fallo y alcance del incremento

La respuesta no streaming recibida por `_llm_call_async_impl` podía incluir `usage`, pero el impl devolvía solamente texto/modelo y el wrapper `llm_call_async` no enviaba esas métricas a `llm_trace.record_call`. Una compactación real con POST simulado de 80 tokens de entrada y 20 de salida recibía el JSON correctamente y perdía ese uso observado. Además, `maybe_compact` no pasaba la sesión a `summarize_rows`, por lo que el resumidor no tenía la identidad necesaria para guardar su traza.

Ahora el wrapper crea un recibo local para cada llamada y pasa un callback privado al impl. Al recibir el JSON de una respuesta HTTP exitosa, el impl captura los campos válidos antes de interpretar el contenido de la respuesta. El wrapper adjunta ese recibo a su única traza final, manteniendo el retorno público de string o tupla texto/modelo. No hay estado global nuevo ni contexto compartido entre callbacks. Los errores de normalización o del callback no interrumpen la respuesta.

`summarize_rows` acepta `session_id=None` como parámetro opcional; `maybe_compact` le transmite el ID de su sesión existente. No se obtiene una sesión desde el run ni se asignan llamadas a una cuenta de presupuesto. Quien invoca el resumidor sin sesión conserva explícitamente la ausencia de traza; tracing desactivado mantiene su política previa.

## Qué se conserva y qué permanece desconocido

El normalizador reutiliza `_normalize_usage_counts` por dimensión: números enteros finitos no negativos dentro del límite existente, sin booleanos, cadenas, fracciones ni valores fuera de rango. OpenAI-compatible usa prompt/completion, Anthropic usa input/output y Ollama sus contadores nativos. Un campo ausente o inválido no se transforma en cero ni elimina otro campo válido. El `total_tokens` informado se conserva por separado cuando es válido, incluso si no coincide con la suma; no se deriva su reparto.

La procedencia queda como `usage_source=reported_engine`, junto al modelo reportado y el solicitado cuando difieren. Los extras existentes de coste/cache/reasoning se reutilizan. Un coste válido incluye su fuente de proveedor y `cost_state=known`; cuando no hay coste válido, `cost_state=unknown` y el campo monetario queda ausente. No se consultan precios ni se estiman dólares. El validador existente de extras ahora contiene OverflowError por campo: un entero enorme en coste/cache/reasoning no elimina otros extras válidos. Tres regresiones adicionales fallaron antes de esta corrección; comprueban coste válido con cache/reasoning corruptos y coste corrupto con cache válido. El fallback local del recibo se conserva.

Una respuesta desde caché sigue pudiendo crear una traza de llamada, pero su uso está vacío: no representa una nueva factura ni copia el recibo anterior. Tampoco se fabrican métricas para CLI, cancelación anterior a respuesta, ausencia de usage o timeout con resultado incierto. Una respuesta con usage válido y schema de contenido inválido conserva su recibo en la traza de error; una cancelación posterior a capturarlo tampoco lo borra.

No modifica streaming ni suma métricas entre wrappers/fallbacks. Cada invocación registra su propio recibo; la relación de run conserva exclusivamente el comportamiento de trazado existente. **No se integra aquí con budget_account ni autonomy_budget.** Reservas, consumo compartido, admisión de auxiliares y parada del turno por ese gasto siguen pendientes de H14. La prueba SQLite anterior mostró el hueco transversal, pero esta corrección sólo conserva evidencia y atribución de sesión para compactaciones automáticas.

## Verificación

**307 pruebas correctas**, 24,62 s, con traza JSONL real en directorios temporales. La regresión principal ejecuta `maybe_compact` con sesión sintética, `summarize_rows` y `llm_call_async` reales; únicamente sustituye el POST y la resolución/política de endpoint. La traza contiene exactamente input 80/output 20, modelo reportado, fuente observada y coste desconocido.

También cubre llamadas concurrentes con respuestas intercaladas, dimensiones ausentes/parciales/malformadas, total aislado e inconsistente, coste enorme inválido, modelo y coste reportados, caché sin nuevos recibos, error de schema, callback privado fallido y cancelación antes/después de recepción. Se comprobaron suites existentes de trazado, retries, compactación y condense. Sin cambios a presupuestos/configuraciones del usuario ni uso de DB personal.

```text
venv/Scripts/python.exe -m pytest tests/test_aux_llm_usage_trace.py tests/test_llm_trace.py tests/test_llm_core_retries.py tests/test_context_compactor.py tests/test_context_compactor_regressions.py tests/test_context_compactor_nonstring.py tests/test_condense.py tests/test_l95_openrouter_usage.py tests/test_l96_openrouter_options.py tests/test_llm_core_usage_finish_delta.py -q
```


### VISITADO / IMPLEMENTADO — H14 consumo de recuperación

`28385388`: observer privado opcional step_completion→ladder→ambos callers captura último valor válido por campo/step, sin sumar snapshots acumulativos repetidos. Error/cancel conserva observación en finally; partial/cost-only no borra tokens previos ni coste conocido. Retorno4tuple legacy intacto. Recovery_usage separado de buckets principales/compactor y cargo al ledger presupuestario del turno; coste desconocido no se inventa. Gate antes de same-model/utility usa política vigente y presupuesto observado; agotamiento termina caller/round sin tools ni nueva inferencia. ASTlifecycle incorpora dos nuevas emisiones (6total) conservando4anteriores y comprueba latch antesfanout/outerbreak.

Repro real loop/ladder/helper con proveedor sintético: usage800input/200output descartado antes (ledger0), ahora ledger1000/receipt1000. Ambos callers degenerate/ctx_ack con step2 de100tokens alcanzan cuota: utilidad/roundposterior/tools no despachan. Final53 correctas17,25s (30nuevas+lifecycle21+cost2); coordinador30 nuevas14,37s. Amplia previa98 correctas/1 fallo197,85s: único fallo era assert estructural de4emisiones desactualizado, corregido y repetido final; selección71 correctas103,29s tenía26nuevas antescontrolesfinales, no freeze global. Scope3filespropios.

Límites: recuperación local excluida de cargo/receipt según política existente; turno originalmente local conserva grant ilimitado aunque utility sea remota (su consumo remoto sí observado/cargado, no límiteinventado). No reserva previa, unión de cuentas ni factura universal para retries internos sin identidad. Métricas principales mantienen su significado, consumo recovery aparte. Fuente https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9 y auxiliareslocales ya visitados. Nuevo timing de uso principal antes recovery sólo evaluación, no cerrado. Sin modelos/GPU reales ni cambios personales.


### VISITADO / IMPLEMENTADO — H14 admisión incluye consumo principal pendiente

`2d32dcf8`: gate antes recuperación usa copia del ledger más uso principal observado aún no liquidado, por invocación outerstream/round. Últimos campos válidos acumulados sin duplicar snapshots; max(total,componentes) evita infravalorar total obsoleto. Settlement descuenta sólo créditos efectivamente añadidos por el cargo legacy, por dimensión tokens/spend; total-only1000 con cargo230 deja residual770. No muta ledger/buckets/métricas principales ni fabrica factura de reintentos internos.

Repro loopreal con proveedor sintético en amboscallers: mainusage1000 conbudget500 permitía recuperar viendoledger0; ahora no despachaaux ni efectos. Caso bajo límite admite; roundliquidado30+pending20 bajo65 no duplica30. Final73 correctas20,63s (20nuevas+30observer+21lifecycle+2cost); coordinador20nuevas3,57s. Source sóloagent_loop+newtests, aliases/localgrant vigentes. Sólo gate antes recovery: cargo principal omitido en algunas rutas, admisión de primeros reintentos principales e identidad wrapper interna siguen pendientes/en evaluación. No reserva global ni ledgergeneralcompleto. Fuente https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9 ya visitada; notupstreamrerun.


### VISITADO / IMPLEMENTADO — admisión de reintentos principales

`ff0b10a3`: el gate de inicio de ronda usa ledger más observación principal pendiente. Una sola línea de producción, suite nueva18 y expectativa anterior ajustada de varios intentos a uno al denegarse más pronto. Bloquea retry ordinary/reasoning, nudge ctx_ack y tercer intento tras dos respuestas300tokens con límite500. Respeta bajo límite, cancelación entre evento/retry, local bypass y cargo previo liquidado30 sin duplicarlo.

Final91 correctas47,56s (18nuevas+20pending+30recovery+21lifecycle+2cost); coordinador34 correctas29,57s junto standing16. Sin cambios a métricas/buckets/pricing/grants ni helper de fases. **Cargo persistente principal omitido en algunas rutas e identidad de intentos internos siguen pendientes**, ahora en evaluación distinta; admisión no equivale a factura completa ni reserva global. Fuente original ya visitada: https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9; no nueva revisión upstream.


### VISITADO / IMPLEMENTADO — liquidación y admisión antes de herramientas

`e119cec5`: residual observado principal se liquida una vez en el ledger capturado al cerrar wrapper, y antes de métricas en cierre normal. Créditos tokens/spend separados compensan cargo legacy; fallos por dimensión no duplican otra dimensión ya añadida. Callback en _TURN_FINALIZERS existente, nestedfinally ejecuta todos aun gen.aclose con error ordinary y conserva excepción previa. Final109 correctas60,45s (19nuevas+18retry+20pending+30recovery+21lifecycle+1disconnect); nueva19 también11,86s. Terminal, error, cancel, consumerclose y cost-only/local conocidos. **Ledger sólo memoria del turno**; no journal durable ni cambio de buckets/metrics públicas, ni factura de providerattempts internos. CancelledError secundario al cerrar no garantía universal nueva.

`1f66cd71`: gate antes de cada tool usa ledger más mainobservado pendiente, sin cambiar límites/localbypass ni cargo. Repropytest aislado2FAIL2PASS13,11s: main1000 con límite500 ejecutaba read_file y paraba sólo después; now noefecto/tool_output, una sola llamada main y checkpointvacío. Control100 y local1000 aún ejecutan; snapshotrepetido1000 no duplica. Source1line funcional+comentario, nueva4tests; selección final61 correctas54,13s (4nuevas+19finalización+18retry+20pending). Selección adicional autonomía/preflight/lifecycle aún en curso al registrar: no afirmar final correcta hasta resultado.

Primer prototipo inline anterior fuera pytest intentó inicializar MemoryVector enlocalhost8100 (servicio no disponible); no certifica ausencia de acceso a datos personales. Repro decisivo/finalpytest usa conftest aislado; no herramienta real ejecutada, efectos sintéticos. No revertir datos ajenos por inferencia. Fuente original ya visitada https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9; adaptaciones propias.

Nueva evaluación active_seconds: 3repros pytest/fakeclock realwrapper terminal/warmretry/ctx_ack mantienenledger0 tras120–240s proveedor, retry bajo límite100. Prototipo por-await __anext__ 3correctas2,06s:120bloquea,20+20=40admite; consumerpausas1000entreSSE no añaden gasto (techo pared independiente elevado sólofixture). Piloto timepending implementándose por dimensión con crédito elapsedlegacy, no cargo universalwall ni duraciónrecoveryaux cerrada.

Verificación ampliada final1f66cd71: autonomía + preflight + steeringlifecycle, **94 correctas420,08s (7min)**, sin omisiones/fallos. No repetir tras pasar salvo cambio relevante. Checkpoint475fc3fb.
