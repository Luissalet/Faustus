# Decisiones tipadas — `/api/typed-decision`

Una decisión tipada responde una pregunta cerrada sobre un texto eligiendo
entre respuestas fijas **sin generar razonamiento**: una sola pasada de
prefill, y se leen las probabilidades del siguiente token para las letras de
las respuestas permitidas. Implementación: `src/typed_decision.py`. Ruta:
`routes/typed_decision_routes.py`. Ver FAUSTUS.md §177 para el porqué, dónde
se usa y la verificación.

## La técnica

1. El prompt lleva unas instrucciones fijas (mensaje de sistema), el
   contexto entre `"""` y, **al final**, la pregunta con las opciones
   etiquetadas con letras de un solo token (`A) …`, `B) …`; una pregunta
   sí/no es `A) yes`, `B) no`).
2. Se pide exactamente **un** token de salida, temperatura 0, con
   `top_logprobs`.
3. De las alternativas del primer token se suman las probabilidades de cada
   letra permitida (`"A"` y `" A"` cuentan igual; minúsculas no, porque «a»
   es más a menudo un artículo que una respuesta).
4. `mass` = probabilidad total sobre letras permitidas (sin renormalizar);
   `distribution` = esas probabilidades renormalizadas; `value` = la de
   mayor probabilidad; `confidence` = su probabilidad renormalizada.
5. `mass < typed_decision_min_mass` → desconocido (el modelo quería decir
   otra cosa); `confidence < typed_decision_min_confidence` → desconocido.
   `best` conserva el argmax aunque no se haya aceptado, para auditar.

Varios campos sobre el mismo contexto comparten un prefijo **idéntico byte a
byte** (instrucciones + contexto) y sólo cambian en la cola: un servidor con
caché de prompt sólo rellena la cola a partir del segundo campo.

### Forma en el cable

| Servidor | Petición | Dónde se lee la respuesta |
| --- | --- | --- |
| OpenAI-compatible (`/v1/chat/completions`, p. ej. un `llama-server` en loopback) | `max_tokens: 1, temperature: 0, logprobs: true, top_logprobs: N`; en loopback además `cache_prompt: true` y `chat_template_kwargs: {"enable_thinking": false}` | `choices[0].logprobs.content[0].top_logprobs[] = {token, logprob}` |
| Ollama nativo (`/api/chat`) | `think: false, options: {num_predict: 1, temperature: 0}, logprobs: true, top_logprobs: N`; si el modelo rechaza `think`, se repite sin él | `logprobs[0].top_logprobs[] = {token, logprob}` |

Una URL `/v1` de un Ollama local se mueve a `/api/chat`: en la superficie
compatible, un modelo que piensa gasta el único token en razonar. Si el
servidor no devuelve log-probabilidades, se lee la letra generada
(`method: "letter"`, sin confianza); si tampoco hay letra, desconocido.

### Etiqueta con los modelos

Nunca carga ni descarga un modelo. Antes de cualquier petición pregunta a
`src.background_job_guard` qué modelos tiene el servidor en memoria (Ollama
`/api/ps`; `llama-server` en loopback `/health` + `/v1/models`) y si está
ocupado (`/slots`); si el modelo configurado no está residente, no se sabe,
o está ocupado, responde `method: "unavailable"` **sin llamar al modelo**.
`typed_decisions_may_load` lo anula (apagado por defecto).

### Es consultiva

Nunca concede un permiso, nunca aprueba nada y nunca sustituye a una regla
que ya está segura. No se usa en `src/user_request_gate.py` ni en ninguna
decisión de seguridad o aprobación.

## API de Python

```python
from src.typed_decision import Field, decide, decide_sync

fields = [
    Field("needs_web", "Does answering this need current information?", "bool"),
    Field("kind", 'What kind of thing is "Cordera Labs"?',
          ["person", "organization", "place", "other"],
          descriptions={"organization": "a company, team or institution"}),
]
decisions = await decide(context, fields, owner=user, purpose="utility",
                         timeout_s=1.5, min_confidence=0.7, caller="mine")
d = decisions["kind"]
d.value, d.confidence, d.mass, d.distribution, d.method, d.reason, d.ms, d.best
```

`decide` nunca lanza. `timeout_s` es el presupuesto de reloj de TODA la
llamada (resolución del endpoint y sondeo de residencia incluidos); los
campos que no caben vuelven `unavailable`/`timeout`. `decide_sync` es el
envoltorio síncrono (seguro dentro de un bucle de eventos: corre en un hilo).

`reason` cuando `value` es `null`: `disabled`, `no_endpoint`,
`unsupported_endpoint`, `model_not_resident`, `residency_unknown`,
`model_busy`, `timeout`, `error`, `low_mass`, `low_confidence`, `unparsed`.

## Rutas HTTP

Autenticación: `require_user`; el endpoint se resuelve para quien llama.

### `POST /api/typed-decision`

```json
{
  "context": "Cordera Labs abrió una oficina en Villanueva.",
  "fields": [
    {"name": "kind", "question": "¿Qué es Cordera Labs?",
     "choices": ["person", "place", "organization"],
     "descriptions": ["una persona", "un lugar", "una empresa u organismo"]},
    {"name": "is_company", "question": "¿Es una empresa?", "choices": "bool"}
  ],
  "purpose": "utility",
  "timeout_ms": 1500,
  "min_confidence": 0.7
}
```

- `fields`: 1 a 8; cada uno con `name` único, `question` y `choices` (lista
  de 2 a 20 respuestas, o `"bool"`); `descriptions` opcional (lista en el
  mismo orden u objeto por respuesta).
- `purpose`: `utility` (por defecto), `task`, `research` o `default` — qué
  ajuste de endpoint/modelo se usa.
- `timeout_ms`: 50–10000 (por defecto `typed_decision_timeout_ms`).
- `context`: hasta 20.000 caracteres (se recorta a 6.000 en el prompt).

Respuesta `200`:

```json
{"decisions": {
  "kind": {"field": "kind", "value": "organization", "confidence": 0.97,
           "mass": 0.99, "distribution": {"person": 0.01, "place": 0.02, "organization": 0.97},
           "method": "logprobs", "ms": 412.0, "reason": "", "best": "organization",
           "model": "helper-3b"},
  "is_company": {"...": "..."}
}}
```

`400` con entrada inválida, `413` con un contexto demasiado largo.

### `GET /api/typed-decision/stats`

Contadores del proceso: `calls`, `fields`, por método (`logprobs`,
`letter`, `unavailable`), `unknown`, `fallbacks` (= respuestas por letra),
`reasons`, `p50_ms`, `p95_ms`, `settings` y `recent` (las últimas 50
decisiones **sin su contexto**: quién preguntó —`caller`: `freshness`,
`brain_entity_type`, `memory_conflict`, `api`—, campo, valor, `best`,
confianza, masa, método, motivo y milisegundos).

## Ajustes

| Clave | Por defecto | Qué hace |
| --- | --- | --- |
| `typed_decisions_enabled` | `true` | Interruptor general. |
| `typed_decision_timeout_ms` | `1500` | Presupuesto duro de una llamada en el camino caliente (las pasadas de fondo usan al menos 8 s). |
| `typed_decision_min_confidence` | `0.7` | Por debajo, desconocido. |
| `typed_decision_min_mass` | `0.5` | Por debajo, desconocido. |
| `typed_decisions_may_load` | `false` | Permite llamar a un modelo no residente. |
| `typed_decision_freshness` | `true` | Usar decisiones en la comprobación de actualidad del chat. |
| `typed_decision_entity_types` | `true` | Tipar entidades «other» del segundo cerebro. |
| `typed_decision_memory_conflicts` | `true` | Sugerencias de conflicto de memoria. |

## Evaluación

`scripts/eval_typed_decision.py` — ver [docs/evals/typed-decisions.md](../evals/typed-decisions.md).
