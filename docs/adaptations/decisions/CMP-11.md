# CMP-11 — Capacidades → selección explicable

**Versión y recorrido:** repo en `757262e` (master) al arrancar la Ola 3;
sin git disponible en este entorno, así que esta ficha documenta el trabajo
directamente sobre el árbol de ficheros, no sobre un commit.

## Solución actual (antes)

El selector de modelo (`studio/src/screens/ModelPalette.tsx`) ya mostraba un
badge por capacidad, pero solo para las TRES claves probadas de
`GET /api/models/{name}/capabilities` (MOD-01/MOD-02,
`routes/local_models_routes.py`) y solo `pass`/`fail`: una capacidad
`announced` pero nunca calibrada no se distinguía de una desconocida, y
ninguna de las dos se distinguía de una probada y fallida salvo por el
color. `src/provider_policy.py`'s invariante 3
(`_check_required_parameters`) ya rechazaba una conexión que no cumplía un
parámetro requerido, pero el error solo nombraba la capacidad
(`"does not support required parameter(s): tool_call"`) — nunca decía POR
QUÉ (¿nunca anunciada? ¿probada y falló?) ni ofrecía una alternativa. Un
endpoint remoto sin `capabilities` explícito reportaba el requisito como
"unverified" en `RouteDecision.reason`, correcto pero mudo sobre qué hacer
al respecto. `docs/adaptations/baseline.md` no tenía fila para esto: nace
en el informe comparativo V2, no en ADP.

## Solución de referencia (INFORME_COMPARATIVO_V2 §3.10)

«admite herramientas pero no esta salida estructurada; acepta imágenes pero
no edición; el modelo anuncia X pero la conexión no lo ha verificado» —
cada incompatibilidad explicada, nunca eliminar requisitos en silencio;
alternativas = modelos del mismo endpoint (o instalados) que sí cumplen.
Prueba decisiva: un modelo con capacidades generales pero un endpoint
incompatible con un parámetro obligatorio produce un rechazo explicado o
una ruta alternativa autorizada — nunca una «ejecución equivalente»
silenciosa.

## Mecanismo concreto de diferencia

* `src/model_capabilities.py` (añadido, módulo existente): `explain_fit(requirements, *, model, endpoint, assertions=None, candidates=()) -> Fit`,
  puro (sin I/O, sin red — coherente con la cabecera del módulo). Cuatro
  estados (`FIT_TESTED`/`FIT_ANNOUNCED`/`FIT_UNKNOWN`/`FIT_MISSING`),
  traducidos 1:1 desde el vocabulario `CapabilityAssertion.status` que el
  módulo ya tenía (`verified`/`claimed`/`unknown`/`unsupported`) — no una
  segunda taxonomía paralela. Cada requisito produce exactamente un
  `FitReason{capability, state, message, alternatives}`; nunca se
  descartan requisitos sin evidencia (`unknown`) ni se funden dos
  capacidades en un solo veredicto. `Fit.ok` es `False` únicamente cuando
  hay un `missing` — `unknown` se reporta, nunca bloquea, igual que el
  invariante 3 ya hacía. Dos traductores nuevos, también puros:
  `assertions_from_calibration_manifest` (el manifiesto de
  `model_calibration.get_manifest` → assertions, sin duplicar
  `model_router._capability_status` gracias a una importación perezosa
  cuando hace falta y, si no, a una réplica mínima de sus dos tablas
  capacidad→clave para evitar el ciclo `model_capabilities → model_router →
  model_calibration → model_capabilities`) y
  `assertions_from_endpoint_capabilities` (una lista explícita remota →
  assertions, mismo criterio "silencio = prueba" que ya tenía
  `provider_policy._check_required_parameters`).
* `src/provider_policy.py`: el invariante 3 (`resolve_route`) sustituye su
  chequeo binario por una llamada a `explain_fit` (vía el nuevo
  `_build_fit`/`_fit_candidates_from_endpoint`); el `ProviderPolicyError`
  sigue siendo `provider.parameter_unsupported`, pero `detail` ahora trae
  el mensaje de causa por capacidad y, cuando el caller resolvió
  `endpoint["capability_alternatives"]` (opcional, nunca I/O propio de este
  módulo), los ids de los modelos que sí cumplen. Las 33 pruebas previas de
  `tests/test_adp22_provider_policy.py` siguen pasando sin cambios — la
  forma exterior (`error_class`, `"tool_call" in detail`, el `reason` de
  "unverified") es un superconjunto de la anterior, no una ruptura.
* `routes/model_routes.py`: NUEVA `GET /api/models/fit-explain?model=&endpoint_id=&needs=`
  (distinta de `/api/models/fit`, el veredicto de VRAM que ya vivía en este
  mismo fichero — ancla distinta, mismo router). Local Ollama: manifiesto de
  calibración keyeado por digest (mismo criterio que
  `routes/local_models_routes.py::_digest_for`), alternativas = otros
  modelos visibles del mismo endpoint, cada uno con su propio manifiesto.
  Endpoint remoto: la única señal declarada en este esquema hoy es
  `ModelEndpoint.supports_tools` — cubre `tool_call` únicamente; el resto
  de `needs` vuelve honestamente `unknown` (límite documentado, no
  inventado); cuando `supports_tools` no es `True`, se ofrece como
  alternativa un modelo de otro endpoint habilitado con `supports_tools=True`.
* `studio/src/adapters/fit.ts`: `fetchFitExplain` + tipos (`FitExplain`,
  `FitReason`, `FitExplainState`) + `PICKER_CAPABILITY_NEEDS` (el conjunto
  exacto de INFORME V2 §3.10). `fetchModelCapabilities` (MOD-01/MOD-02, la
  lectura más antigua) se conserva sin tocar: `studio/src/screens/settings/LocalModels.tsx`
  sigue dependiendo de ella.
* `studio/src/screens/ModelPalette.tsx`: el badge de capacidades pasa de
  tres claves pass/fail a un badge por requisito de `PICKER_CAPABILITY_NEEDS`,
  con los cuatro estados y un tooltip que lleva la frase del backend más,
  si hay alternativa, sus ids. La petición ya no se salta un endpoint
  remoto (antes gateada a `profile?.isLocal`): un endpoint remoto tiene
  alguna evidencia (`supports_tools`) y reporta `unknown` con honestidad
  para el resto, en vez de no preguntar nada.
* `studio/src/shell/palette.css`: `.fs-palette__cap[data-state=…]` con los
  tres colores con evidencia (`tested`/`announced`/`missing`, tokens
  `var(--fs-*)`); `unknown` se queda con el estilo neutro por defecto — a
  diferencia del veredicto de VRAM, aquí el badge SÍ se dibuja siempre,
  porque callarse sobre un requisito es justo lo que esta ficha prohíbe.
* `docs/api/model_capabilities.md` (nuevo): contrato completo, tabla de
  estados, ejemplo de respuesta, límite conocido documentado.

## Cobertura

**Presente**: `explain_fit` y sus dos traductores, la ruta HTTP, el cableado
de `provider_policy` y los badges de Studio existen y están probados (ver
Pruebas ejecutadas). **Parcial**, documentado como tal: la única señal de
capacidad declarada para un endpoint REMOTO es `supports_tools` — vision,
salida estructurada y edición de imagen en un endpoint remoto son siempre
`unknown` hasta que se cablee un lector de capacidades declaradas por
proveedor (existe `src/model_capability_readers/openrouter.py`, que ya lee
`pricing`, pero no expone capacidades hoy y no está mandatado por este
lote). Nunca se rellenó ese hueco con una suposición.

## Estado comparativo

**Ventaja propia extendida**: el informe pide explicar la incompatibilidad
y ofrecer alternativas de "modelos instalados"; esta implementación además
(a) reutiliza el vocabulario `CapabilityAssertion` ya existente en vez de
crear una segunda taxonomía de estados, así que un futuro lector de
capacidades remoto solo necesita producir `CapabilityAssertion`s para
heredar automáticamente los cuatro estados y sus mensajes, y (b) mantiene
`unknown` sin bloquear (mismo invariante que ya regía `provider_policy`),
de modo que la nueva explicación es un superconjunto estricto del
comportamiento anterior, no una segunda vía paralela.

## Decisión

**Extender** `src/model_capabilities.py` (módulo ya existente, contrato
"shape and normalization only" respetado: `explain_fit` sigue sin hacer
I/O) e **integrar** el resultado en `src/provider_policy.py` (invariante 3),
`routes/model_routes.py` (ruta nueva) y el selector de Studio (badges
sustituidos por su superconjunto).

## Pruebas ejecutadas

```
python3 -m pytest tests/test_cmp11_fit_explain.py tests/test_cmp11_fit_explain_js.py \
  tests/test_adp22_provider_policy.py tests/test_model_picker_vram_fit.py \
  tests/test_model_capabilities.py tests/test_model_capability_readers.py \
  tests/test_model_calibration.py tests/test_model_routes.py \
  tests/test_model_defaults.py tests/test_endpoint_probing.py \
  tests/test_studio_guards.py \
  -q -p no:cacheprovider -W ignore
```
→ ver el informe final para el número de tests y el resultado exacto.

```
./node_modules/.bin/tsc --noEmit -p tsconfig.json
./node_modules/.bin/vite build
node studio/checks/fit_explain.check.mjs
```

## Límites / pendientes

* Endpoints remotos: solo `tool_call` tiene evidencia declarada
  (`ModelEndpoint.supports_tools`); vision/salida estructurada/edición de
  imagen quedan `unknown` hasta cablear un lector de capacidades por
  proveedor (candidato natural: extender
  `src/model_capability_readers/openrouter.py`).
* Las alternativas cruzadas entre endpoints (no solo dentro del mismo
  endpoint local) solo existen hoy para `tool_call`, por la misma razón.
* `provider_policy._build_fit` solo ofrece alternativas cuando el caller
  pasa `endpoint["capability_alternatives"]` explícitamente — el módulo
  sigue sin hacer I/O propio (contrato del módulo), así que ese campo debe
  poblarlo quien resuelve el endpoint (p. ej. `routes/chat_routes.py`), no
  cableado en este lote.
