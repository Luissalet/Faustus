# CMP-08 — Estimador de coste y recursos con supuestos explícitos

- **Lote:** W2-B (`CONTRATO_CMP_W2.md`, ola 2 de CMP).
- **Versión base:** `master` en `95747d9`; sin git en el entorno de trabajo
  (repo montado sin `.git`), así que este documento y el propio código son
  el recorrido — no hay commit que citar más allá del punto de partida.
- **Ficha origen:** `INFORME_COMPARATIVO_V2.md` §3.7 (CMP-08).
- **Solapa con ADP-16** (`docs/adaptations/baseline.md`): `ADP-16` ya
  registraba `workflow_cost_estimate.py::estimate()` como "parcial" —
  precio min/max, ciclos no acotados, modelo sin precio → `unknown` — y
  `preflight.py`/`interchange.py` como el resto de su alcance, ya hechos en
  W1-G. Este lote NO repite ese trabajo: añade `estimate_detailed()` y
  `plan_compare.py` al lado de lo que ya existía, sin tocar el contrato de
  `estimate()` que `preflight.py` y `/api/workflows/estimate` (sin
  `?detail=1`) ya usan.

## Qué pedía el informe

Cuentas SEPARADAS (`node_activations`, `model_calls`, `external_ops`,
`tokens {in,out}`, `cost_known_usd`, `cost_unestimable: [motivos]`) en vez
de un `calls_min/max` que mezcla activaciones estructurales con llamadas a
modelo; precio siempre estructurado y con procedencia
(`{amount, unit: per_1M_tokens, currency, source, as_of}`), nunca
hardcodeado; una skill compuesta con perfil declarado o histórico
compatible, de modo que dos skills con el mismo modelo no reciban la misma
estimación y una skill de tres llamadas no se presente como una;
`structural_bounds` / `forecast_with_assumptions` / `measured` como tres
cosas distintas; en local, `latency_estimate` con carga/cola/prefill-
generación/memoria por servidor real, nunca sumada; `plan_compare.compare`
para una tabla "por qué este plan" con `computed`/`estimated`/`unknown` por
celda; ruta `POST /api/workflows/compare-plans`.

## Solución actual (antes de este cambio)

`src/workflow_cost_estimate.py::estimate()` (ADP-16, ya existente):
`calls_min`/`calls_max` por nodo (gating por `condition`, ciclos acotados
por `assumed_iterations`), precio solo desde `prices` inyectado (nunca
hardcodeado — ya cumplía esa regla), `unpriced_models`. Pero:
- Un nodo `deliver`/`artifact_store` cuenta en `calls_min/max` con la misma
  vara que un nodo `skill` — no hay forma de preguntar "¿cuántas de estas
  activaciones son llamadas a modelo de verdad?" sin leer `per_node[i].type`
  a mano.
- Un `skill` node siempre se trata como 1 llamada por activación —
  correcto para una skill simple, pero una skill compuesta (una que por sí
  misma dispara varias llamadas) se estimaba exactamente igual que una de
  una sola llamada.
- El precio es `ModelPrice(prompt_usd_per_1k, completion_usd_per_1k)` — un
  número con procedencia implícita (el caller lo puso), no una estructura
  con `source`/`as_of` visibles para la UI.
- No existía ningún concepto de "estructural" vs "con supuestos" vs
  "medido" — solo `calls_min`/`calls_max`, que ya son el resultado de
  aplicar `assumed_iterations`.
- No existía `plan_compare.py` ni `POST /compare-plans`.

`src/workflows/preflight.py::preflight()` (ADP-16, W1-G) ya envolvía
`estimate()` añadiendo conexiones/herramientas/permisos/esperas y una regla
más estricta ("unknown" si CUALQUIER skill invocada no tiene precio, no
solo total parcial) — su `cost` queda intacto por este lote.

## Solución de referencia (INFORME §3.7)

Un estimador que separa qué-tipo-de-cosa ocurre (activación estructural /
llamada a modelo / operación externa) de cuánto cuesta, que nunca colapsa
una skill compuesta a "una llamada", que distingue explícitamente lo que el
grafo garantiza de lo que es una previsión con supuestos listados y de lo
que un run real midió, y que compara varios planes para el mismo objetivo
en una tabla con procedencia por celda en vez de exigir leer tres
estimaciones por separado.

## Mecanismo concreto de diferencia

- **`estimate_detailed()`** (`src/workflow_cost_estimate.py`, añadido al
  lado de `estimate()` — nada que ya llame a `estimate()` cambia de forma):
  recorre los mismos nodos y ciclos que `estimate()` pero clasifica cada
  activación en `node_activations` (todas), `model_calls` (solo `skill`
  con llamada resuelta) y `external_ops` (`deliver`/`artifact_store`, más
  `calls_profile.external_ops` de una skill compuesta). `tokens{in,out}`
  se acumulan solo desde llamadas a modelo caracterizadas.
- **Precio estructurado y con procedencia** — `StructuredPrice
  {amount_prompt_per_1m, amount_completion_per_1m, unit: "per_1M_tokens",
  currency, source, as_of}`. `price_from_openrouter_raw(raw)` lee
  `raw["pricing"]["prompt"/"completion"]` (el payload real de OpenRouter —
  USD por token, convertido a por-millón) de un `capability_pricing`
  INYECTADO por el caller — `estimate_detailed()` nunca hace red (ver
  siguiente punto); `capability_pricing[model]` gana sobre
  `prices[model]` (el `ModelPrice` de siempre, `source: "caller_prices"`)
  SOLO cuando el primero trae un `pricing` de verdad, nunca por defecto.
- **`CallsProfile`** — `{model_calls, external_ops, tokens_in, tokens_out,
  source, samples}`. Una skill compuesta (`config.skill` en un nodo
  `skill`) se busca primero en `skill_calls_profiles` (declarado —
  manifiesto de la skill, leído por el caller) y si no, en
  `skill_call_history` (`DATA_DIR/skill_call_history.json`, leído por la
  RUTA — `routes/workflows_routes.py::_skill_call_history_from_disk`, no
  por el módulo puro). Ninguno de los dos encontrado →
  `calls_profile_source: "unknown"`, `model_calls: {min:0, max:0}`, y el id
  de la skill nombrado en `cost_unestimable` — nunca `1` por defecto. Una
  skill sin `config.skill` (solo `config.model`) SÍ se asume de una
  llamada — asunción nombrada en `assumptions`, no oculta.
- **`structural_bounds` / `forecast_with_assumptions` / `measured`**:
  `structural_bounds.node_activations.max` es la cadena `"unbounded"`
  cuando hay un ciclo (nunca un número inventado);
  `forecast_with_assumptions` aplica `assumed_iterations` y lo declara
  junto al resto de supuestos; `measured` es `None` salvo que el caller
  pase `run_measured` (la ruta lo llena desde
  `WorkflowStore.usage_so_far(run_id)` cuando `run_id` existe) — y aun así
  solo trae `tool_calls`/`active_seconds`: `tokens_in/out`/`cost_usd`
  quedan `None` con una nota explícita, porque `store.usage_so_far` (y
  `engine.py`) no guardan ningún ledger de tokens/coste por nodo hoy
  (comprobado antes de escribir esto, no supuesto).
- **`local_latency` nunca se calcula dentro del módulo puro** — es un
  `Mapping[model_id, {...}]` que el CALLER ya calculó desde
  `resource_admission.status()` (cola), `llm_core.local_speed()`
  (prefill/generación, tok/s aprendido, `None` hasta que se observa una
  respuesta real), `vram_admission.reservations_snapshot()` (memoria
  reservada por servidor real, nunca sumada entre servidores) y
  `gpu_policy.model_sizes()` (tamaño en disco — la única de las cuatro que
  hace una petición de red, precisamente por lo que `estimate_detailed`
  nunca la llama: un estimador tiene que seguir siendo "cero LLM/efectos").
  Este lote NO cablea esa llamada real en la ruta (ver Cobertura) — el
  parámetro existe y se enhebra correctamente sobre `per_node[i]
  .latency_estimate`, pero nadie en este lote calcula los cuatro números
  reales todavía.
- **`plan_compare.compare(goal, plans, ...)`** (`src/plan_compare.py`,
  NUEVO): llama a `estimate_detailed()` una vez por plan con los MISMOS
  `prices`/`capability_pricing`/`skill_calls_profiles`/`local_latency`, y
  arma una tabla de 6 filas (`node_activations_max`, `model_calls_max`,
  `external_ops_max`, `tokens_in_max`, `tokens_out_max`,
  `cost_known_usd_max`) × N planes. Cada celda lleva `basis`:
  `"computed"` (nada de ciclo/precio-faltante/skill-no-caracterizada toca
  esa métrica para ese plan), `"estimated"` (el plan tiene un ciclo — el
  número depende de `assumed_iterations`), `"unknown"` (la métrica está
  tocada por un modelo sin precio o una skill compuesta sin perfil). Un
  plan cuya `definition` no parsea se reporta en `errors` y se excluye de
  `rows`/`detail` — nunca aborta la comparación entera.
- **Rutas** (`routes/workflows_routes.py`, anclas propias — W2-E toca
  `/simulate` y `topology.ts` para otras funciones, sin solape): `POST
  /api/workflows/estimate?detail=1` (query param `detail`, por defecto 0 —
  el shape de siempre sin él) y NUEVA `POST /api/workflows/compare-plans`.
  `_detail_inputs_from_payload` centraliza cómo se leen
  `capability_pricing`/`skill_calls_profiles`/`local_latency` del body y
  siempre intenta `skill_call_history.json` en disco (nunca falla la
  petición si el fichero no existe o está corrupto — `{}` y listo).
- **`preflight()` (edición mínima)**: añade el campo `cost_detail` (nuevo,
  con `default_factory=dict` para no romper construcción posicional
  existente) computado con `estimate_detailed()` usando los MISMOS
  `prices`/`assumed_iterations` que ya recibía — ningún caller nuevo
  requerido, `cost` (el campo de siempre) intacto.
- **Studio**: `studio/src/adapters/topology.ts` gana los tipos/decode de
  `DetailedEstimate`, `StructuredPrice`, `PlanComparison` y las funciones
  `workflowEstimateDetailed`/`comparePlans` — deliberadamente SIN importar
  los tipos de W2-E (`SimulationResult`) ni al revés, tal como esa sección
  del fichero ya anotaba antes de este lote ("los dos lotes tocando este
  fichero... se mantienen desacoplados a propósito"). `WorkflowEstimateView`
  se movió de `studio/src/screens/Activity.tsx` a NUEVO
  `studio/src/screens/activity/EstimateView.tsx` (primer paso de este lote,
  antes de tocar nada más de Studio, con un `Edit` anclado y releyendo el
  fichero primero) — mismas props, mismo `data-testid`, mismo comportamiento;
  `Activity.tsx` ahora solo importa `{ WorkflowEstimateView } from
  './activity/EstimateView'`. No se construyó una UI nueva para las cuentas
  separadas/`plan_compare` en este lote (ver Cobertura) — el objetivo
  explícito del movimiento era dejar `Activity.tsx` disponible para W2-C sin
  tocar nada más de ese fichero.

## Cobertura

**Presente** (implementado y verificado en este lote):
- `estimate_detailed()` con las 4 cuentas separadas, precio estructurado y
  con procedencia (`caller_prices` / `openrouter_pricing_field`, nunca
  hardcodeado), perfiles de llamada declarados/históricos/desconocidos que
  distinguen dos skills con el mismo modelo, `structural_bounds` /
  `forecast_with_assumptions` / `measured` como campos distintos.
- `plan_compare.compare()` con tabla de 6 métricas × N planes y `basis` por
  celda; tolera un plan que no parsea sin abortar los demás.
- Rutas `POST /api/workflows/estimate?detail=1` y `POST
  /api/workflows/compare-plans`, más `cost_detail` en `preflight()`.
- Movimiento de `WorkflowEstimateView` a
  `studio/src/screens/activity/EstimateView.tsx`, con `Activity.tsx`
  reducido a un import — verificado con `tsc --noEmit` y `vite build`.
- Tipos TS (`DetailedEstimate`, `PlanComparison`, etc.) y funciones de
  adaptador (`workflowEstimateDetailed`, `comparePlans`) en `topology.ts`.

**Parcial** (limitación conocida, documentada, no bloqueante):
1. **`local_latency` no se calcula en ningún sitio todavía** — el
   parámetro existe end-to-end (módulo puro → ruta → tipo TS), pero
   ningún código de este lote llama de verdad a
   `resource_admission.status()`/`llm_core.local_speed()`/
   `vram_admission.reservations_snapshot()`/`gpu_policy.model_sizes()`
   para rellenarlo — hoy siempre llega vacío salvo que un caller externo
   lo calcule y lo pase. Documentado como punto a cablear (ver abajo), no
   fingido como hecho.
2. **`skill_calls_profiles` "declarado" no se descubre automáticamente**
   desde el manifiesto real de una skill — `src/skills_runtime/discovery.py`
   /`bridge.py` no están en la lista de ficheros de este lote (posesión de
   otros CMP), y hoy tampoco existe un campo `calls_profile` en el formato
   de manifiesto que esos módulos producen. La ruta acepta
   `skill_calls_profiles` del body de la petición (el caller ya lo tiene o
   lo calculó) y siempre intenta el historial en disco; el descubrimiento
   automático desde el manifiesto queda para quien posea
   `skills_runtime/` añadir el campo y una llamada de glue en la ruta.
3. **No hay UI nueva para las cuentas separadas ni para `compare-plans`**
   — `EstimateView.tsx` sigue mostrando el shape de `estimate()` (min/max
   de coste y llamadas, sin desglosar `model_calls`/`external_ops`). El
   adaptador (`workflowEstimateDetailed`/`comparePlans`) y los tipos están
   listos para que una vista los consuma; construir esa vista no estaba en
   la lista de ficheros que este lote posee para `Activity.tsx` (solo
   `WorkflowEstimateView`, y el objetivo de tocarlo era el traslado, no
   ampliarlo).

**No verificado**: comportamiento de `capability_pricing` contra un
payload real de OpenRouter (`price_from_openrouter_raw` se probó contra el
shape documentado por OpenRouter — `{"prompt": "0.0000008", ...}` — no
contra una respuesta real capturada; sin red en este entorno).

## Estado comparativo

**Ventaja propia** en la separación estructural/previsión/medido y en el
rechazo explícito a inventar una llamada de modelo para una skill
compuesta sin perfil (el informe pide el resultado — nunca "1 llamada por
defecto" — y aquí queda forzado por código: `calls_profile_source ==
"unknown"` bloquea la cuenta en 0, no en 1, y se nombra en
`cost_unestimable`). **Hipótesis de mejora** en el resto: el informe
describe el comportamiento deseado, no una implementación externa
concreta que auditar línea a línea, así que no hay "paridad demostrada" ni
"ventaja externa documentada" que evaluar aquí — es una hipótesis medida
contra los criterios de aceptación explícitos del propio informe (cuentas
separadas, precio con procedencia, skills compuestas distinguibles,
`structural/forecast/measured`, tabla de planes con `basis` por celda),
no contra un producto de terceros.

## Decisión

**Extender** `src/workflow_cost_estimate.py` con `estimate_detailed()` en
vez de sustituir o mutar `estimate()` — todo lo que ya llama a `estimate()`
(incluido `preflight()`'s `cost` de siempre) sigue leyendo exactamente el
mismo shape. **Componer**: `plan_compare.py` no reimplementa ninguna lógica
de estimación, solo llama a `estimate_detailed()` N veces con los mismos
insumos y arma la tabla — cero duplicación de la parte difícil.
**Conservar** la regla de "cero red/LLM en una estimación": `local_latency`
se enhebra pero nunca se calcula dentro del módulo puro, precisamente
porque una de sus cuatro fuentes (`gpu_policy.model_sizes`) es una llamada
de red real.

## Pruebas ejecutadas

Desde `/home/claude/faustus`:

```
$ python3 -m pytest tests/test_cmp08_estimate.py -q -p no:cacheprovider -W ignore
15 passed in 5.62s

$ python3 -m pytest tests -q -p no:cacheprovider -W ignore -k "workflow or topology or estimate"
373 passed, 2 skipped, 3 failed in 80.68s
  (los 3 fallos son tests/test_workflow_credential_execution.py — no
   importan nada de este lote: execution_router/approval_store/secret_storage/
   credentials, sin relación con workflow_cost_estimate.py, plan_compare.py,
   preflight.py ni las rutas /estimate y /compare-plans. No investigados a
   fondo por estar fuera del alcance de W2-B — ver "Puntos a cablear".)

$ python3 -m pytest tests/test_studio_guards.py -q -p no:cacheprovider -W ignore
11 passed in 1.14s

$ ./node_modules/.bin/tsc --noEmit -p tsconfig.json
(0 errores en los ficheros de este lote — 2 errores preexistentes en
 studio/src/screens/studio/model.ts y studio/src/shell/AppShell.tsx,
 propiedad de otros lotes en curso en paralelo, no de W2-B)

$ ./node_modules/.bin/vite build
✓ built in 21.68s   (solo el aviso preexistente de tamaño de chunk > 500kB)
```

## Puntos a cablear por el orquestador

1. **`local_latency` sin cableado real**: alguien con acceso de escritura a
   la ruta (o un lote posterior) debe calcular
   `{model_id: {load, queue, prefill_tps, generation_tps, memory_server}}`
   desde `resource_admission.status()`/`llm_core.local_speed()`/
   `vram_admission.reservations_snapshot()`/`gpu_policy.model_sizes()` y
   pasarlo como `local_latency` a `estimate_detailed()` — hoy solo llega
   si el caller HTTP ya lo trae en el body.
2. **`skill_calls_profiles` declarado, automático**: si se añade un campo
   `calls_profile` al formato de manifiesto de skills
   (`src/skills_runtime/bridge.py::manifest_from_skill`, fuera de la
   posesión de este lote), la ruta puede empezar a construir
   `skill_calls_profiles` automáticamente para los `skill` nodes de la
   definición en vez de depender de que el caller lo mande.
3. **`DATA_DIR/skill_call_history.json` no lo escribe nadie todavía** —
   este lote solo lo LEE (con manejo de ausencia/corrupción). Falta el
   productor: algo que, tras cada ejecución real de una skill compuesta,
   acumule `{model_calls, external_ops, tokens_in, tokens_out, samples}`
   por skill id.
4. **UI de las cuentas separadas y de `compare-plans`**: los adaptadores
   (`workflowEstimateDetailed`, `comparePlans`) y tipos TS están listos;
   falta una vista en Studio que los consuma — natural candidato:
   `EstimateView.tsx` (su nueva ubicación,
   `studio/src/screens/activity/EstimateView.tsx`, deliberadamente aislada
   para esto) o una pestaña nueva en la pantalla `/workflows` que W2-E está
   construyendo.
5. Confirmar contra un payload real de OpenRouter capturado (no solo el
   shape documentado) que `price_from_openrouter_raw` interpreta
   `pricing.prompt`/`pricing.completion` correctamente — no hay red en
   este entorno para probarlo aquí.
