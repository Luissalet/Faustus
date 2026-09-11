# OpenRouter (OBJ-8) — opciones por endpoint y accounting real

Integración de Faustus con [OpenRouter](https://openrouter.ai) más allá de
tratarlo como "un backend OpenAI-compatible más": lee coste REAL por turno
(no solo tokens estimados) y expone las preferencias de enrutado/privacidad
que OpenRouter ofrece por request.

Este documento crece por lotes; cada sección nombra el suyo.

## Usage — coste real por turno (Lote A1)

**Problema que resuelve:** antes de este lote, `autonomy_budget.
remote_spend_units()` (el contador que alimenta el presupuesto
`max_remote_spend` de una sesión — ver `src/autonomy_budget.py`) SIEMPRE
estimaba el gasto sumando `input_tokens + output_tokens`, sin importar lo
que el proveedor hubiera cobrado realmente.

**Qué cambia:** cuando el payload de la request a OpenRouter lleva
`"usage": {"include": true}` (lo añade `src/openrouter_options.py`, Lote
A2 — este lote NO toca el payload), OpenRouter devuelve, dentro del
objeto `usage` de la respuesta:

| Campo OpenRouter | Significado |
|---|---|
| `usage.cost` | Coste real en USD de esta llamada. |
| `usage.cost_details.upstream_inference_cost` | Coste que el proveedor upstream cobró a OpenRouter (puede diferir del anterior). |
| `usage.prompt_tokens_details.cached_tokens` | Tokens de entrada servidos desde caché (prompt caching). |
| `usage.completion_tokens_details.reasoning_tokens` | Tokens de razonamiento (modelos "thinking"). |

`src/llm_core.py::_extract_usage_extras(usage: dict) -> dict` lee esos
campos (solo si existen y son numéricos ≥ 0; nunca inventa ceros) y los
aplica en TODOS los sitios donde el módulo construye un evento SSE
`{"type": "usage", "data": {...}}` — el camino OpenAI-compatible (donde
realmente aterriza el `usage.cost` de OpenRouter), el de Responses API, el
de Ollama nativo y el de streaming nativo Anthropic. Para cualquier
proveedor que no sea OpenRouter (o que no haya pedido `usage.include`), el
resultado es que estas claves simplemente NO aparecen — nunca `null`.

Forma del evento `usage` cuando OpenRouter reporta coste real:

```json
{
  "type": "usage",
  "data": {
    "input_tokens": 100,
    "output_tokens": 20,
    "cost_usd": 0.00123,
    "cost_source": "provider",
    "upstream_cost_usd": 0.0009,
    "cached_tokens": 40,
    "reasoning_tokens": 8
  }
}
```

### Propagación en `src/agent_loop.py`

`_usage_bucket(...)` (el registro no-secreto de uso por ronda) acepta ahora
`cost_usd`, `cached_tokens`, `reasoning_tokens` como kwargs opcionales
(`None` por defecto) y los persiste en el bucket SOLO si vienen —
exactamente el mismo criterio "ausente, no cero" que `_extract_usage_extras`.
`agent_loop` los recoge del evento `usage` del stream (mismo sitio donde ya
acumulaba `input_tokens`/`output_tokens`) y los pasa a cada llamada de
`_usage_bucket`.

`_usage_bucket_summary(...)` añade `cost_usd_total` (suma de `cost_usd` de
todos los buckets que lo tengan) SOLO cuando al menos un bucket del turno
tiene coste real — una sesión que mezcla una ronda OpenRouter con una ronda
local nunca reporta un total falso de $0.

### `autonomy_budget.remote_spend_units` — coste real reemplaza la estimación

```python
remote_spend_units(
    *, endpoint_cost_tracked, input_tokens, output_tokens,
    provider_cost_usd=None,
) -> float
```

- Si el endpoint no está marcado `cost_tracked` (modelo local, o ruta cuyo
  coste este proceso nunca aprendió), devuelve `0.0` — sin cambios.
- Si `provider_cost_usd` es un número finito ≥ 0, ese coste real REEMPLAZA
  la estimación por tokens (no se suman: una vez se conoce el número real,
  la estimación es estrictamente peor).
- En cualquier otro caso (sin coste real, o malformado), sigue estimando
  `input_tokens + output_tokens` — comportamiento idéntico al de antes de
  este lote.

**Unidad de `max_remote_spend`:** históricamente, 1 unidad = 1 token
estimado (la suma anterior). Un coste real en USD se convierte a esa misma
unidad con `spend_units_from_usd(usd)`, usando la constante documentada
`USD_PER_UNIT` (`src/autonomy_budget.py`):

```
USD_PER_UNIT = 2.0 / 1_000_000  # $0.000002 por unidad ($2 / 1M tokens)
```

Es una aproximación (precio OpenRouter medio, no un feed de precios en
vivo) — el punto es que exista UN número documentado y trazable, no que sea
exacto por modelo. `TASK-06` (el bloque en `src/agent_loop.py` que carga la
ronda al ledger de presupuesto tras cada turno) pasa
`provider_cost_usd=bucket.get("cost_usd")` a `remote_spend_units`, así que
un turno en un modelo OpenRouter cost-tracked con `usage.include` activo
consume el presupuesto con el gasto REAL, no una estimación.

### Qué NO hace este lote

- No construye el payload (`"usage": {"include": true}`, `provider`,
  `plugins`, etc.) — eso es `src/openrouter_options.py` (Lote A2, ver
  siguiente sección de este documento cuando exista).
- No expone un endpoint HTTP nuevo para leer el coste — ya viaja dentro de
  la estructura de uso existente (`usage_buckets`/`_usage_bucket_summary`)
  que las rutas de sesión/chat ya serializan; no hizo falta tocar
  `routes/observability_routes.py`.

## Opciones por endpoint (Lote A2)

**Qué resuelve:** antes de este lote, toda llamada a OpenRouter salía con un
payload OpenAI-compatible genérico — ninguna de las preferencias de
enrutado/privacidad/coste que OpenRouter expone por request tenía forma de
configurarse, y `usage.include` (que Lote A1 necesita para leer coste real)
no se pedía nunca.

Implementación: `src/openrouter_options.py` (validación de esquema,
persistencia y construcción del payload) + `routes/openrouter_routes.py`
(CRUD admin-only) + tres puntos de integración en `src/llm_core.py`
(`llm_call`, `llm_call_async`, `_stream_llm_inner` — los tres caminos que
construyen un payload OpenAI-compatible) que llaman a
`apply_openrouter_payload` justo antes de enviar la request, y solo cuando
`provider == "openrouter"`.

### Persistencia — `GET/PUT/DELETE /api/openrouter/endpoints/{endpoint_id}/prefs`

Un documento JSON por `endpoint_id` en
`DATA_DIR/openrouter_endpoints.json` (escritura atómica: fichero temporal +
`os.replace`, mismo patrón que `src/command_guard.py`). Todas las rutas son
admin-only (`core.middleware.require_admin`, igual que
`routes/privacy_routes.py`).

| Método | Ruta | Qué hace |
|---|---|---|
| `GET` | `/api/openrouter/prefs` | Todos los endpoints con prefs guardadas (`{"endpoints": {...}}`). |
| `GET` | `/api/openrouter/endpoints/{endpoint_id}/prefs` | Prefs efectivas de un endpoint (defaults del esquema si no hay nada guardado). |
| `PUT` | `/api/openrouter/endpoints/{endpoint_id}/prefs` | Body = patch parcial; valida y hace merge sobre lo ya guardado (o sobre los defaults). `400` con `{"error_class": "openrouter.invalid_prefs", "detail": "..."}` si algún campo no valida (misma convención que `routes/git_routes.py::_error`). |
| `DELETE` | `/api/openrouter/endpoints/{endpoint_id}/prefs` | Borra las prefs del endpoint (idempotente). |

Esquema (`src/openrouter_options.py::_default_prefs`):

| Campo | Tipo / valores | Default |
|---|---|---|
| `sort` | `""` \| `"price"` \| `"throughput"` \| `"latency"` | `""` (deja que OpenRouter decida) |
| `allow_fallbacks` | bool | `true` |
| `require_parameters` | bool | `false` |
| `max_price` | `{"prompt": float, "completion": float}` (ambos opcionales) o `null` | `null` |
| `zdr` | bool (Zero Data Retention) | `false` |
| `order` | lista de provider ids, ≤ 20 | `[]` |
| `ignore` | lista de provider ids | `[]` |
| `data_collection` | `"auto"` \| `"allow"` \| `"deny"` | `"auto"` |
| `web_search` | `{"enabled": bool, "max_results": 1..10}` | `{"enabled": false, "max_results": 5}` |
| `native_fallback` | bool | `false` |

Ejemplo:

```
PUT /api/openrouter/endpoints/ep1/prefs
{"sort": "price", "zdr": true, "order": ["anthropic", "openai"]}

→ 200
{"sort": "price", "allow_fallbacks": true, "require_parameters": false,
 "max_price": null, "zdr": true, "order": ["anthropic", "openai"],
 "ignore": [], "data_collection": "auto",
 "web_search": {"enabled": false, "max_results": 5}, "native_fallback": false}
```

### Provider preferences

`apply_openrouter_payload` construye `payload["provider"]` incluyendo SOLO
las claves cuyo valor se apartó del default (`sort` no vacío,
`allow_fallbacks` en `false`, `require_parameters`/`zdr` en `true`,
`max_price`/`order`/`ignore` no vacíos) — un endpoint sin prefs guardadas no
añade la clave `provider` en absoluto.

`data_collection` sigue una regla propia cuando está en `"auto"` (su
default): el principio Faustus "nunca pasar a pago silenciosamente / una
autoridad única sobre privacidad" (`src/privacy_policy.py`) exige que, bajo
el perfil de privacidad más estricto (`local_only` — "nada debe salir de
esta máquina"), la llamada se marque explícitamente `deny` en vez de confiar
en la configuración por defecto de la cuenta de OpenRouter. Bajo
`local_preferred` o `cloud_allowed` el campo se deja SIN configurar — el
propio docstring de `privacy_policy` es explícito en que ninguno de los dos
bloquea una llamada saliente aquí, así que el ajuste del panel de OpenRouter
es la fuente de verdad honesta en ese caso. `"allow"`/`"deny"` explícitos en
las prefs del endpoint siempre ganan, sin mirar el perfil de privacidad.

`payload["usage"] = {"include": true}` se añade SIEMPRE que
`provider == "openrouter"`, con o sin prefs guardadas — es lo que Lote A1
necesita leer de vuelta, y no depende de que el endpoint tenga configuración
propia.

### Online (web search)

`payload["plugins"] = [{"id": "web", "max_results": n}]` se añade SOLO
cuando el turno lo pide explícitamente (`web_search_requested=True`, una
petición puntual) o cuando `prefs.web_search.enabled` está activado de
antemano para ese endpoint — nunca por defecto, siguiendo el mismo principio
de opt-in explícito para todo lo que cuesta dinero.

### Fallback nativo

Si `prefs.native_fallback` está activo y el caller pasa `fallback_models`
(lista de ids OpenRouter) no vacía, `payload["models"] = [model] +
fallback_models`, deduplicada y recortada a 8 entradas — es el mecanismo de
fallback de OpenRouter (probado del lado del proveedor), distinto de la
cadena de reintentos propia de Faustus.

### Cache (prompt caching Anthropic vía OpenRouter)

`_build_anthropic_payload` (camino nativo Anthropic) ya marcaba
`cache_control` en el bloque `system` y en el último tool cuando `tools` o
un system prompt largo (> 4000 caracteres) lo justificaban. Ese umbral vive
ahora en un helper compartido, `_anthropic_cache_breakpoint_applies(tools,
system_text)`, para no duplicar el criterio.

Un modelo `anthropic/*` servido a través de OpenRouter usa el camino
OpenAI-compatible (no `_build_anthropic_payload`), pero OpenRouter reenvía
un marcador `cache_control` puesto en un bloque de contenido del mensaje
exactamente igual que la Messages API nativa de Anthropic. Por eso, en los
tres puntos de integración de `llm_core.py`:

- `_openrouter_anthropic_cache_hints_applicable(provider, model)` decide si
  aplica (`provider == "openrouter"` y `model` empieza por `"anthropic/"`);
  ninguna otra combinación toca el mensaje.
- `_apply_openrouter_anthropic_cache_hints(payload, tools=...)` reescribe el
  mensaje `system` (ya consolidado en uno solo por cada call site) de string
  plano a un array de un bloque con `cache_control`, usando el mismo umbral
  compartido — solo cuando de verdad hace falta.

### Qué NO hace este lote

- No cablea `endpoint_id` desde `src/agent_loop.py`/`src/endpoint_resolver.py`
  hasta `llm_core.py`: ninguno de los tres call sites que construyen un
  payload OpenAI-compatible (`llm_call`, `llm_call_async`,
  `_stream_llm_inner`) recibe hoy un `endpoint_id` de verdad — solo una url y
  un model — así que las tres llamadas pasan `endpoint_id=None`, y
  `apply_openrouter_payload` cae en los defaults del esquema (`usage.include`
  sigue aplicándose siempre). Añadir ese cableado es plumbing de
  `endpoint_resolver.py`/`agent_loop.py` fuera del alcance de este lote — el
  punto de integración lo puede tomar cualquier caller que sí conozca un
  `endpoint_id` real, pasándolo a esas mismas tres funciones.
- No añade caching de las definiciones de `tools` (array `payload["tools"]`
  en formato OpenAI) para el camino OpenRouter — el sitio exacto donde
  OpenRouter esperaría ese marcador en formato OpenAI-compatible no está
  documentado con la claridad del caso `system`, así que se dejó fuera en
  vez de adivinar una estructura. El camino nativo Anthropic (`provider ==
  "anthropic"`) no cambia: sigue cacheando el último tool como antes.
