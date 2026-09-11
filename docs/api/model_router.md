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

## Integración pendiente (deliberada)

Este lote deja `choose()`/`explain()` listos pero **no los cablea** en
`routes/chat_routes.py`/`src/agent_loop.py`. El único punto de esta forma
(el turno decide su modelo antes de armar `route_descriptors`) lee y usa
`sess.model` directamente en cientos de líneas de
`routes/chat_routes.py::stream_chat_response` más allá de la construcción de
route descriptors que este lote tenía permiso para tocar (headers de
caché/capacidad, métricas, estado de sesión persistido...). Reescribir esas
lecturas para que resuelvan un alias `"auto"` a través del router es un
cambio de forma distinta y de más alcance que "una llamada de integración" —
ver el informe final del lote para el detalle exacto.
