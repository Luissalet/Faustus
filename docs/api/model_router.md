# Router medido para modelos locales (MOD-05) — `/api/model-router/*`

Elige QUÉ modelo local responde un turno, a partir de evidencia que esta
instalación ha recogido realmente — nunca de un nombre o una lista fija.
Tres fuentes, nunca mezcladas:

- **Capacidad probada** — `src/model_calibration.py` (`tested`): un probe
  real contra un modelo residente. Peso alto.
- **Capacidad declarada** — el mismo manifiesto (`announced.capabilities`,
  construido con el vocabulario de `src/model_capabilities.py`). Solo se usa
  cuando no hay nada probado para esa capacidad. Peso bajo.
- **Historial propio de esta instalación** — `DATA_DIR/model_router_stats.json`
  (`record_outcome`): aciertos/fallos y una latencia media (EWMA) por modelo.

Implementación: `src/model_router.py` (puro salvo lecturas de disco/manifest)
y `routes/model_router_routes.py` (HTTP, admin-only). **Apagado por
defecto** (`RouterConfig.enabled = False`): mientras no se active
explícitamente, nada en el resto del sistema cambia de comportamiento.

**Nunca pasa a pago silenciosamente**: `choose()` solo marca
`escalated: true` cuando se cumplen las tres condiciones a la vez —
`allow_paid_escalation` activo, ningún modelo local cumple los requisitos, y
el perfil de privacidad activo (`src/privacy_policy.py`) no es
`local_only`. Incluso así, `choose()` **nunca elige un modelo de pago**:
devuelve `escalated: true` y un objeto `escalation` con el motivo, y es el
caller quien debe leerlo y decidir — la función no puede por sí sola
terminar un turno en un modelo de pago.

## Endpoints

Todos admin-only (`core.middleware.require_admin` — igual que
`routes/config_routes.py`/`routes/command_guard_routes.py`). Error plano
`{"error_class": "...", "detail": "..."}` en 4xx, misma convención que
`routes/git_routes.py::_error`.

| Método | Ruta | Qué hace |
|---|---|---|
| `GET` | `/api/model-router/config` | Config actual (`RouterConfig.to_dict()`). |
| `PUT` | `/api/model-router/config` | Parchea la config (solo las claves presentes en el body). `400 model_router.invalid_config` si algo no valida. |
| `POST` | `/api/model-router/preview` | Ejecuta `choose()` sin persistir nada en el log (`log=False`) — para ver qué elegiría el router antes de activarlo. |
| `GET` | `/api/model-router/log?limit=` | Últimas `limit` (por defecto 50, tope 2000) decisiones, más reciente primero. |
| `GET` | `/api/model-router/stats` | Historial ok/fail/latencia por modelo (`model_router_stats.json` completo). |

### `GET /api/model-router/config`

```json
{
  "enabled": false,
  "prefer_local": true,
  "max_latency_s": null,
  "allow_paid_escalation": false,
  "candidates": [],
  "min_capabilities": []
}
```

### `PUT /api/model-router/config`

Body = parche (cualquier subconjunto de los campos de arriba). Ejemplo:

```
PUT /api/model-router/config
{"enabled": true, "min_capabilities": ["tool_call"]}
```

`candidates` vacío = "todos los locales instalados" (el caller decide qué
hay instalado y se lo pasa a `choose(..., installed=[...])`; este módulo
nunca hace su propio `/api/tags`). `min_capabilities` es un subconjunto de
`"tool_call"`, `"json_mode"`, `"vision"`, `"reasoning"` — un modelo que no
las cumpla queda fuera de los candidatos "usables" sea cual sea su score.

### `POST /api/model-router/preview`

```json
// request
{
  "requirements": {"capabilities": ["tool_call"], "max_latency_s": 8.0},
  "installed": ["qwen2.5:7b", "llama3.1:8b"]
}
// response
{
  "decision": {
    "model": "qwen2.5:7b",
    "reason": "mejor candidato local: tool_call probado, 41 tok/s medidos, 12 ok / 0 fallos recientes",
    "alternatives": [
      {"model": "qwen2.5:7b", "score": 5.82, "why": ["tool_call probado", "41 tok/s medidos", "12 ok / 0 fallos recientes"], "meets_requirements": true},
      {"model": "llama3.1:8b", "score": -996.1, "why": ["tool_call desconocido", "velocidad desconocida", "sin historial"], "meets_requirements": false}
    ],
    "escalated": false,
    "escalation": null
  },
  "explain": "Modelo elegido: qwen2.5:7b (tool_call probado, 41 tok/s medidos, 12 ok / 0 fallos recientes)"
}
```

### `GET /api/model-router/log?limit=20`

Cada línea de `DATA_DIR/model_router_log.jsonl` (rotado a 2000 líneas):
`{ts, session_id, requested, chosen, reason, escalated, candidates: [{model, score, why}]}`.

### `GET /api/model-router/stats`

```json
{"stats": {"qwen2.5:7b": {"ok": 12, "fail": 0, "ewma_latency_s": 3.2, "last_error_class": null, "updated_at": "2026-09-11T10:00:00+00:00"}}}
```

## Uso programático (`src/model_router.py`)

```python
from src import model_router

config = model_router.get_router_config()
requirements = model_router.Requirements(capabilities=("tool_call",))
decision = model_router.choose(
    requirements,
    installed=["qwen2.5:7b", "llama3.1:8b"],
    config=config,
    requested="auto",
    session_id="sess-123",
)
if decision.model:
    ...  # usa decision.model
elif decision.escalated:
    ...  # el caller decide explícitamente si continúa en un modelo de pago
model_router.record_outcome(decision.model, ok=True, latency_s=3.2)
```

## Integración con el turno (ADP-22, lote W1-F)

`routes/chat_routes.py` ahora cablea `choose()` en el punto donde se fija
el modelo del turno, en `chat_endpoint` (`/api/chat`) y en `chat_stream`
(`/api/chat_stream`) — inmediatamente antes de
`_recover_empty_session_model`, no después: esa recuperación ya rellena un
`sess.model` vacío desde la caché del endpoint, y si corriera primero no
dejaría nada que resolver para el router. Ambos puntos llaman a la misma
función privada, `routes.chat_routes._resolve_auto_model_route(sess,
session_id, owner=...)`:

1. Si el modelo pedido de la sesión (`sess.model`, tras strip) no es
   literalmente `"auto"` (case-insensitive) ni una cadena vacía → no hace
   nada. Este es el guard que hace que **todo lo demás sea irrelevante
   cuando la sesión ya tiene un modelo concreto** — el caso de casi todas
   las sesiones existentes hoy, porque el picker de Studio nunca escribe el
   literal `"auto"` (no se tocó Studio en este lote; ver «Límites»).
2. Si `RouterConfig.enabled` es `False` (el valor guardado por defecto) →
   no hace nada. Con la config de fábrica esta función es un no-op completo
   para cualquier sesión, cualquier modelo pedido — el mismo
   `enabled=False` de siempre.
3. Si el endpoint actual de la sesión no es el propio Ollama local de esta
   instalación (`src.vram_admission.ollama_root(sess.endpoint_url)`) → no
   hace nada. MOD-05 solo elige entre modelos locales instalados
   (`installed_local_models()`, que pregunta al mismo Ollama vía
   `_ollama_base()`); una sesión apuntando a un endpoint remoto/API se deja
   intacta en vez de adivinar a qué otro endpoint debería moverse — ver
   «Límites» para el porqué exacto de este corte de alcance.
4. Si no hay ningún modelo instalado ahora mismo → no hace nada.
5. En cualquier otro caso: llama a `installed_local_models()` +
   `choose(Requirements(), installed=..., config=..., requested="auto",
   session_id=..., owner=...)`. Si `decision.model` es verdad, lo escribe en
   `sess.model` (en memoria y en la fila `Session` de la base de datos, igual
   que hace `_reconcile_selected_route_from_request` para una ruta elegida a
   mano) y clasifica esa ruta con `src.provider_policy.resolve_route(...)`
   (contrato completo en el docstring de ese módulo — ver la sección
   «Límites» más abajo). Si `decision.model` es
   `None` y lo pedido era literalmente `"auto"`, `sess.model` se deja en
   `""` — nunca en el literal `"auto"` — para que el 400 de "No hay modelo
   seleccionado" ya existente dispare con su mensaje habitual en vez de que
   el turno intente llamar río arriba a un modelo llamado, de verdad,
   `auto`.

El resultado (`model_router.Decision` + el `provider_policy.RouteDecision`
opcional) se guarda en una variable local (`_model_router_auto` en
`chat_stream`) y viaja por clausura hasta `stream_with_save`, donde:

- Se emite un evento SSE `model_router` justo después de
  `workspace_rejected` (mismo sobre `{"type": ..., "data": ...}` que ya usa
  `git_policy`), con `model_router.explain_event(decision, route=route)` —
  **solo cuando el router realmente actuó**; un turno con un modelo ya
  concreto no añade ningún evento nuevo al stream.
- En el modo `agent` (bucle de herramientas), los dos puntos terminales que
  `chat_routes.py` ya reconocía del stream de `src/agent_loop.py`
  (`{"type": "metrics"}` para éxito, `{"type": "agent_terminal"}` para
  fallo) llaman a `_record_model_router_outcome(_model_router_auto, ok=...,
  latency_s=..., error_class=...)`, que a su vez llama a
  `model_router.record_outcome(decision.model, ...)` — de nuevo, solo
  cuando `_model_router_auto` no es `None` y trae un modelo. Así el
  historial ok/fail/latencia de MOD-05 (`model_router_stats.json`) se
  alimenta con datos reales del turno, sin que `agent_loop.py` sepa nada de
  esto.

### Qué se dejó fuera de esta llamada de integración (auditoría de `sess.model`)

`grep -n "sess\.model\b" routes/chat_routes.py` antes de este lote daba 21
resultados; cada uno se revisó:

- **Construcción de `route_descriptors`/`foreground_candidates`**
  (`chat_endpoint` ~L1535, `chat_stream` ~L2740) y el resto de lecturas
  dentro de `stream_with_save` (sesión de generación
  de imágenes, admisión VRAM, mensajes de error, el modo `chat` sin
  herramientas) — todas leen `sess.model` **después** del punto de
  resolución, así que ven el modelo concreto ya elegido por el router (o el
  original, si el router no actuó). No necesitaron cambios.
- **`_enforce_chat_privileges` (`routes/chat_helpers.py`)** — el gate de
  `allowed_models`/`block_all_models` corre después de la resolución y
  valida el modelo YA elegido por el router contra la lista blanca del
  operador. Si un admin restringe modelos y el router elige uno fuera de
  esa lista, el turno se rechaza con 403 igual que si un humano lo hubiera
  tecleado — comportamiento correcto sin cambio de código.
- **`normalize_model_id`/`_normalize_model_id_from_cache`
  (`routes/chat_helpers.py::build_chat_context`)** — normaliza el modelo
  contra la caché del endpoint; corre después de la resolución, así que
  normaliza el modelo elegido por el router, no `"auto"`. Si el modelo
  recién elegido aún no está en la caché del endpoint (una caché que se
  refresca por separado de `installed_local_models()`), se deja el tag tal
  cual — Ollama acepta un tag directo igual que si un humano lo hubiera
  escrito a mano, así que esto degrada con seguridad, no con error.
- **`regenerate_chat_response` / `rewrite_message` (endpoints separados,
  `chat_routes.py` ~L3915/4301)** — leen `sess.model` de una sesión ya
  existente, sin pasar por `_reconcile_selected_route_from_request` /
  `_recover_empty_session_model` / `_resolve_auto_model_route`. Como este
  lote muta `sess.model` en memoria y en la fila de base de datos la
  primera vez que el router actúa, un regenerate/rewrite posterior ve
  directamente el modelo concreto ya resuelto — nunca `"auto"` — así que no
  necesitan su propio cableado.
- **Caché de capacidades / calibración (`src.model_calibration`,
  `src.model_capabilities`)** — se leen por nombre de modelo concreto; nunca
  ven `"auto"` porque solo se consultan después de la resolución (incluido
  dentro de `provider_policy._check_required_parameters`, que este mismo
  lote añade).
- **`routes/session_routes.py`, `routes/history/history_routes.py`,
  `routes/research/research_routes.py`, `routes/memory/memory_routes.py`,
  `routes/webhook/webhook_routes.py`, `routes/email_routes.py`,
  `routes/task/task_routes.py`** — cada uno lee `session.model`/`sess.model`
  de su propia sesión, en su propio endpoint, sin pasar por
  `chat_endpoint`/`chat_stream`. Ninguno queda en el alcance de este lote
  (archivos fuera de la lista de ficheros de W1-F) y ninguno puede observar
  el literal `"auto"` salvo que un caller externo lo escriba directamente en
  la fila de sesión sin pasar por el chat — un caso no cubierto hoy, igual
  que no lo estaba antes de este lote.

### Límites de esta integración

- **No se movió el cableado a `src/agent_loop.py`.** Ese módulo no está en
  la lista de ficheros de este lote. El "registro explícito de por qué se
  eligió la ruta" que pide ADP-22 (`provider_policy.RouteDecision`) viaja
  hoy en el evento SSE `model_router` y en los logs de
  `record_outcome`/`choose` — **no** en `_usage_bucket` (`src/agent_loop.py`
  ~L4452), que tiene una firma de kwargs fija sin hueco genérico para campos
  adicionales. Añadir `billing`/`network`/`fallback_scope` ahí es una tarea
  pequeña y concreta para quien posea `agent_loop.py`: pasar el
  `RouteDecision.to_dict()` (o los campos sueltos que hagan falta) como
  kwargs nuevos de `_usage_bucket`, con default `None`/omitido para no
  romper las llamadas existentes — igual que ya hace con
  `cost_usd`/`cached_tokens`/`reasoning_tokens`.
- **Solo cablea el modo `agent` para `record_outcome`.** El modo `chat`
  (sin herramientas, `stream_llm_with_fallback` directo) tiene sus propios
  puntos terminales (`{"type": "usage"}` y el bloque `chat_terminal` de
  `event: error`) que no se tocaron — el historial de MOD-05 no se alimenta
  con turnos "auto" que caen en modo `chat` puro. El evento SSE
  `model_router` sí se emite igual para ambos modos (se emite antes de la
  bifurcación `chat_mode`), así que la UI vería la elección pero
  `model_router_stats.json` no acumularía ese resultado. Cablear los dos
  puntos que faltan es mecánicamente idéntico a los dos ya hechos (ver
  `_record_model_router_outcome` en `routes/chat_routes.py`).
- **`chat_endpoint` (`/api/chat`, no-streaming) solo resuelve el modelo.**
  No emite ningún evento (no hay stream) ni llama a `record_outcome` — ese
  endpoint no tiene un punto único de cierre de turno tan claro como el SSE
  del streaming. El modelo elegido sí queda persistido en `sess.model`, así
  que una llamada streaming posterior de la misma sesión ve el resultado.
- **No se tocó Studio.** No existe hoy ningún control en
  `studio/src/screens` que escriba el literal `"auto"` en `sess.model` — la
  única forma de activar esta ruta hoy es una llamada API directa que fije
  `model` en `"auto"` (o lo deje vacío) con el router activado por
  `PUT /api/model-router/config`. Añadir un control de UI ("Auto") es
  trabajo de Studio, fuera de este lote de backend.
- **`src/provider_policy.py` no tiene documento propio en `docs/api/`** —
  su contrato completo (las cuatro invariantes, el vocabulario de
  `billing`/`network`/`fallback_scope`) está en su propio docstring de
  módulo y en `tests/test_adp22_provider_policy.py`; esta página solo cubre
  cómo se usa desde el turno.

### Actualización (lote de integración) — `_usage_bucket` amplía su firma

`src/agent_loop.py::_usage_bucket` acepta ahora `billing=None, network=None,
fallback_scope=None, route_reason=None` (los mismos nombres que
`provider_policy.RouteDecision`), persistidos solo cuando el caller pasa una
cadena no vacía — igual regla que `cost_usd`/`cached_tokens`/
`reasoning_tokens`; ninguna llamada existente cambia. Sigue sin haber
cableado real: se auditó de nuevo `_usage_bucket(` en `agent_loop.py` y cómo
llega `requested_route` (~L4924, construido por
`src.foreground_model_routing.build_foreground_route_descriptors` /
`src.endpoint_resolver.resolve_route_descriptor`) — ese descriptor es la
metadata de fallback *original* de ADP-22 (solo `endpoint_id`/
`endpoint_label`/`endpoint_cost_tracked`), no tiene relación con
`provider_policy.RouteDecision`, y `_resolve_auto_model_route` (donde SÍ se
construye un `RouteDecision`, para el evento SSE `model_router` y
`record_outcome`) no pasa por ningún punto que llegue a `_usage_bucket`. No
hay atajo corto sin añadir plumbing nuevo entre `routes/chat_routes.py` y
`agent_loop.py`, así que se deja la firma ampliada más un test directo
(`tests/test_l95_openrouter_usage.py::test_usage_bucket_persists_route_fields_when_given`
y sus vecinos) en vez de inventar un cableado no pedido por ningún fichero
de este lote.

## Reconciliación con `src/execution_router.py`

`src/execution_router.py` **no es un router de modelos**: enruta el
*backend de ejecución de una skill* (sandbox Docker vs. host) a partir de
un `SkillManifest`, con la regla "el host nunca es un fallback silencioso"
como único invariante duro. No elige entre modelos ni entre endpoints LLM en
ningún punto de su código — su docstring de módulo lo dice explícitamente
("capability → backend"). No se ha tocado en este lote y no hace falta
reconciliarlo con MOD-05: son dos preocupaciones distintas (qué modelo
responde un turno vs. dónde corre el código de una skill) que compartían
solo el nombre "router" en el informe externo que originó ADP-22, no una
responsabilidad solapada real.
