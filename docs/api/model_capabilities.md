# Capacidades → selección explicable (CMP-11) — `GET /api/models/fit-explain`

No confundir con `GET /api/models/fit` (`docs/api/model_router.md` no lo
cubre; vive en `routes/model_routes.py` también, pero es un veredicto de
**VRAM** — ¿cargan los pesos?) ni con `GET /api/models/{name}/capabilities`
(`routes/local_models_routes.py`, MOD-01/MOD-02 — el manifiesto crudo
`announced`/`tested` de un modelo Ollama). Esta ficha es la tercera pregunta,
distinta de ambas: dado lo que una tarea necesita (`needs`), ¿este modelo en
este endpoint lo cumple, y si no, por qué y con qué alternativa?

INFORME V2 §3.10: «admite herramientas pero no esta salida estructurada;
acepta imágenes pero no edición; el modelo anuncia X pero la conexión no lo
ha verificado» — cada incompatibilidad se explica, nunca se elimina un
requisito en silencio; las alternativas son modelos del mismo endpoint (o
instalados) que sí cumplen.

## El vocabulario: `explain_fit` (`src/model_capabilities.py`)

```python
def explain_fit(
    requirements: Any,           # iterable de tokens/alias de capacidad
    *, model: str, endpoint: str = "",
    assertions: Any = None,      # evidencia YA resuelta por el caller
    candidates: Any = (),        # otros modelos ya evidenciados (FitCandidateModel)
) -> Fit: ...
```

`Fit{ok: bool, model, endpoint, reasons: [FitReason]}`. Un `FitReason` por
cada capacidad de `requirements` — nunca menos, nunca fusionadas — con:

| Campo | Qué es |
|---|---|
| `capability` | Token canónico (`tool_call`, `json_mode`, `vision`, `image_editing`, …). |
| `state` | `tested` \| `announced` \| `unknown` \| `missing` (ver abajo). |
| `message` | La frase en castellano que explica el estado — la CAUSA, no solo el veredicto. |
| `alternatives` | Ids de otros modelos ya evidenciados (`candidates`) que SÍ cumplen esa capacidad. Vacío cuando no hay ninguno. |

### Los cuatro estados

Reutilizan el vocabulario ya existente de `CapabilityAssertion.status`
(`claimed`/`verified`/`unsupported`/`unknown`) — `explain_fit` no inventa una
segunda taxonomía, la traduce a los nombres que CMP-11 pide:

| `Fit` state | `CapabilityAssertion.status` | Significa |
|---|---|---|
| `tested` | `verified` | Este modelo+conexión exactos fueron probados y funcionó. |
| `announced` | `claimed` | El proveedor/modelo lo anuncia; nada aquí lo ha verificado. |
| `unknown` | `unknown` | Ni anunciado ni probado — ausencia de evidencia, nunca un "no". |
| `missing` | `unsupported` | Probado y falló, o una lista explícita de capacidades remotas no lo incluye. |

`Fit.ok` es `False` **solo** cuando alguna capacidad requerida está en
`missing` — igual que el invariante 3 de `src/provider_policy.py`: `unknown`
se reporta, nunca bloquea en silencio ni se trata como un `True`.

`explain_fit` es puro (sin I/O, sin red): quien la llama ya resolvió
`assertions`/`candidates`, desde `src.model_calibration` (modelo local) o la
lista de capacidades declarada de un endpoint remoto. Dos ayudantes hacen esa
traducción sin duplicar lógica:

- `assertions_from_calibration_manifest(manifest)` — el manifiesto de
  `model_calibration.get_manifest` (`{"announced":…, "tested":…}`) → un
  `CapabilityAssertion` por capacidad. `tested[...]=True` → `verified`;
  `tested[...]=False` → `unsupported` (nunca al revés: una capacidad
  probada y fallida nunca queda tapada por lo anunciado); solo
  `announced.capabilities[...]=True` sin probar → `claimed`; el resto,
  `unknown`.
- `assertions_from_endpoint_capabilities(capabilities, requirements=...)` —
  una lista explícita de capacidades remotas (el mismo hecho ya resuelto por
  el caller que `provider_policy._check_required_parameters` trataba como
  definitivo, no como una prueba): lo listado es `claimed`; lo que
  `requirements` pide y NO está en la lista es `unsupported` — una lista
  explícita es exhaustiva para lo que se comprobó, así que el silencio ahí
  es una prueba, no una incógnita.

## `GET /api/models/fit-explain`

```
GET /api/models/fit-explain?model=<id>&endpoint_id=<id>&needs=tools,json,vision,images_edit
```

Autenticado (`require_user`), scoped por owner igual que `GET /api/models`
(un admin ve cualquier endpoint; un usuario normal, los suyos + los de
owner nulo). `needs` acepta cualquier alias de
`src.model_capabilities.normalize_capability` (`tools`→`tool_call`,
`json`→`json_mode`, `images_edit`→`image_editing`, …).

```json
{
  "ok": false,
  "model": "qwen2.5:7b",
  "endpoint": "local-ollama",
  "endpoint_name": "Ollama",
  "reasons": [
    {"capability": "tool_call", "state": "tested", "message": "Admite herramientas: verificado en esta conexión.", "alternatives": []},
    {"capability": "json_mode", "state": "missing", "message": "El modelo no admite salida estructurada (JSON) en esta conexión.", "alternatives": ["qwen3.5:9b"]},
    {"capability": "vision", "state": "unknown", "message": "No hay evidencia de visión (imágenes de entrada) en esta conexión: ni anunciado ni probado.", "alternatives": []}
  ]
}
```

`404` (`endpoint {id} not found`) si el endpoint no existe o no es del
owner; `400` si `model` viene vacío.

### De dónde sale la evidencia, según el endpoint

- **Ollama local** (`_is_ollama_base`): el manifiesto de calibración
  (`src.model_calibration`, MOD-01/MOD-02), keyeado por digest cuando
  `/api/tags` responde (mismo criterio que
  `routes/local_models_routes.py::_digest_for` — un re-pull que cambia el
  digest empieza a explicarse contra un manifiesto nuevo, no uno obsoleto).
  Las alternativas son los demás modelos visibles en el mismo endpoint
  (hasta 25), cada uno con su propio manifiesto — nunca el mismo veredicto
  copiado.
- **Endpoint remoto**: la única señal declarada que este esquema tiene hoy
  es `ModelEndpoint.supports_tools` (booleano, admin-editable) — cubre
  `tool_call` únicamente. El resto de `needs` en un endpoint remoto vuelve
  honestamente `unknown`, nunca inventado. Cuando `supports_tools` no es
  `True`, la ruta busca hasta 5 endpoints habilitados y visibles para el
  owner con `supports_tools=True` y ofrece un modelo representativo de cada
  uno como alternativa de `tools` — la única capacidad para la que este
  esquema tiene evidencia cruzada entre endpoints.

### Límite conocido / pendiente

No hay hoy un lector de capacidades declaradas por proveedor para endpoints
remotos (algo como `src/model_capability_readers/openrouter.py`, que ya
sabe leer `pricing` pero no está cableado a esta ruta). Mientras no se
cablee, `vision`/`json_mode`/`image_editing` en un endpoint remoto son
siempre `unknown` — nunca se inventa un "sí" ni un "no". Cablear ese lector
es el siguiente paso natural de esta ficha (ver
`docs/adaptations/decisions/CMP-11.md`).

## `src/provider_policy.py`: invariante 3, ahora explicado

`resolve_route`'s invariante 3 (un parámetro requerido probado no soportado
es un error, nunca un requisito descartado en silencio) usa `explain_fit`
en vez de un chequeo binario. El `ProviderPolicyError` sigue siendo
`provider.parameter_unsupported`, pero el `detail` ahora trae, por cada
capacidad en `missing`, el mensaje de causa y — cuando el caller pasó
`endpoint["capability_alternatives"]` (una lista opcional de
`{"model_id":, "capabilities": [...]}` o `{"model_id":, "manifest": {...}}`
que el caller ya resolvió) — los ids de los modelos que sí la cumplen. Sin
ese campo, `provider_policy` no inventa alternativas: sigue sin hacer I/O
propio, tal como documenta su cabecera de módulo.

## Studio: badges del selector de modelo

`studio/src/adapters/fit.ts::fetchFitExplain` (una lectura por fila, en
hover/foco — igual disciplina que `fetchModelCapabilities`, que sigue
existiendo para `studio/src/screens/settings/LocalModels.tsx`) pide
`PICKER_CAPABILITY_NEEDS = ['tools', 'json', 'vision', 'images_edit']` —
exactamente el conjunto de INFORME V2 §3.10 — y `ModelPalette.tsx` pinta un
badge por capacidad (`.fs-palette__cap[data-state=…]`, `studio/src/shell/palette.css`):
`tested` en verde, `announced` en ámbar, `missing` en rojo, `unknown` sin
color especial (la misma regla que el veredicto de VRAM: nunca inventar un
color para "no lo sabemos" — la diferencia es que aquí el badge SÍ se
dibuja, porque quedarse callado sobre un requisito es justo lo que esta
ficha prohíbe). El tooltip lleva la frase del backend y, si hay alternativa,
los ids separados por comas.
