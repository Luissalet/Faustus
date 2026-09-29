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
