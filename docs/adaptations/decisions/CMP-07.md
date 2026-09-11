# CMP-07 — Workflows: diseñar, simular y depurar un plan

- **Lote:** W2-E (`CONTRATO_CMP_W2.md`, ola 2 de CMP).
- **Versión base:** `master` en `95747d9`; el entorno de trabajo no tiene
  `.git` montado (repo sin historial accesible), así que este documento y
  el propio código son el recorrido — no hay commit que citar más allá del
  punto de partida declarado por el orquestador.
- **Ficha origen:** `INFORME_COMPARATIVO_V2.md` §3.6 (CMP-07).
- **Solapa con ADP-14/ADP-16/ADP-17/ADP-31** (`docs/adaptations/baseline.md`):
  - `ADP-14` (parcial) ya registraba `topology_export.py` (Mermaid) y
    `MermaidView.tsx` como la "alternativa legible sin canvas", y señalaba
    que faltaba un canvas interactivo tipo React Flow. Este lote construye
    ESE canvas (`PlanGraph.tsx`) — SVG propio, capas por profundidad, sin
    dependencia nueva — cerrando el delta que ADP-14 dejó abierto, sin
    tocar `topology_export.py`/`MermaidView.tsx`.
  - `ADP-16`/`ADP-17` (W1-G, ya hechos) dieron `preflight.py` e
    `interchange.py`. Este lote los REUTILIZA en la UI
    (`adapters/topology.ts::workflowPreflight`/`importWorkflowDefinition`/
    `exportWorkflowDefinition`) en vez de duplicarlos — no hay segunda
    implementación de dry-run ni de import/export.
  - `ADP-31` (nueva, P3/XL por diseño) pedía exactamente el documento que
    este lote entrega: `docs/design/bounded-workflow-iterations.md` +
    `src/contracts/workflow_iteration.py` — un PROPOSAL, no una
    implementación; `_find_cycle` intacto, nada cableado al motor.

## Qué pedía el informe (§3.6)

`simulate(definition, *, choices={node_id: branch}, rounds_max)` — un
recorrido estructural por rondas SIN ejecutar nada: qué nodos se
activarían, esperas humanas, ramas no tomadas, y una advertencia explícita
de que las dependencias AND (`needs`) no son ramas de control — un camino
visual que no pasa por una aprobación no demuestra que el nodo pueda
ejecutarse sin ella si la exige como dependencia. Una pantalla `/workflows`
con lista de definiciones (runs recientes + importar/pegar JSON), un grafo
SVG propio (capas por profundidad, navegación por teclado, alternativa
legible en lista), tres modos explícitos — Diseño / Simulación estructural
/ Ejecución real autorizada —, un `NodeInspector` (contrato, herramienta/
modelo, `needs`, `config`; cambiar tool/modelo re-lanza preflight y lint),
un aviso de lint que abre el nodo y propone la corrección (`hint`), y un
`RunOverlay` que superpone el estado de un run real sobre el mismo grafo.
Chat y canvas deben modificar la MISMA definición.

## Solución actual (antes de este cambio)

Nada de esto existía como superficie de usuario ni como endpoint:
- `POST /api/workflows/validate`/`/plan` ya parseaban y mostraban qué
  nodos partirían primero, pero sin recorrido por rondas, sin `choices`,
  sin distinguir "esperas humanas" de "ramas no tomadas".
- `topology_export.py::workflow_to_mermaid` ya dibujaba el grafo, pero
  como texto Mermaid (`MermaidView.tsx`), no como un canvas interactivo con
  `NodeInspector` ni con estado de simulación/ejecución superpuesto.
- No existía ninguna pantalla `/workflows`; `Automations.tsx` es
  deliberadamente otra cosa (recetas, no el runtime de workflows) — ya
  distinguido por ADP-14, no tocado aquí.

## Solución de referencia (INFORME §3.6)

Un simulador estructural puro (sin LLM/efectos), una pantalla con tres
modos explícitos sobre la misma definición, un inspector de nodo que
re-valida al cambiar `config`, y un overlay de ejecución real sobre el
mismo grafo — sin segundo formato "solo canvas".

## Mecanismo concreto de diferencia

- **`src/workflows/simulate.py::simulate`**: recorrido por rondas donde
  cada ronda solo resuelve nodos cuyas dependencias quedaron TODAS
  resueltas al FINAL de la ronda anterior (capas reales, no un contador de
  iteraciones arbitrario). Un `condition`/`human_approval` sin entrada en
  `choices` queda `awaiting_choice` — nunca se asume que pasa. Un
  `human_approval` alcanzado se registra en `human_waits` exista o no una
  elección para él (un run real siempre pausa ahí). Nada de
  `src/workflows/handlers.py` se importa ni se llama (pinned por
  `test_simulate_never_calls_a_real_handler`, con spy).
- **`routes/workflows_routes.py::POST /simulate`**: reutiliza
  `_definition_or_400` (mismo mensaje de refusal que `/validate`), valida
  `choices`/`rounds_max`, delega en `simulate()`. Ancla distinta de
  `/estimate` (W2-B) y de `/compare-plans` (W2-B) — ninguna se tocó.
- **`studio/src/screens/workflows/PlanGraph.tsx`**: SVG propio por capas
  de profundidad (BFS sobre `needs`), sin librería de grafos; cada nodo es
  un `<g role="button" tabIndex={0}>` con navegación por flechas entre
  capas y una tabla `<details>` como alternativa legible/accesible. Ninguna
  dependencia nueva (verificado: `reactflow`/`d3`/`cytoscape`/`vis-network`
  ausentes de `package.json`).
- **`NodeInspector.tsx`**: edición de `config` como JSON; "Apply and
  re-check" delega en `WorkflowsScreen`, que vuelve a llamar
  `workflowPreflight` (que a su vez re-ejecuta `agent_profile_lint
  .lint_workflow`) — un solo mecanismo de lint, no dos. Los avisos del
  panel de Diseño abren el nodo correspondiente parseando `subject` (
  `"node:<id>"`).
- **`RunOverlay.tsx`**: el MISMO `PlanGraph` con `marks` derivados del
  estado real de un run (`GET /api/workflows/runs/{id}`); iniciar un run
  nuevo pasa por un `Dialog` de confirmación explícita antes de llamar
  `POST /api/workflows/runs` (existente, sin ruta nueva); avanzar/
  cancelar/reanudar reutiliza `adapters/activity.ts::changeWorkflow`, la
  misma función que ya usa `Activity.tsx` — sin segunda implementación.
- **`src/contracts/workflow_iteration.py`**: PROPUESTA de `loop` — dataclasses
  y validación únicamente, sin import de `workflows/engine.py` ni
  `workflows/handlers.py` (pinned por
  `test_module_never_imports_the_engine_or_handlers`), sin entrada `"loop"`
  en `NODE_TYPES` (pinned). Ver
  `docs/design/bounded-workflow-iterations.md` para el razonamiento
  completo — `max_iterations` siempre requerido y nunca "0 = ilimitado"
  (eso reabriría exactamente el ciclo sin cota que `_find_cycle` ya
  rechaza), `IterationState` reutiliza `NODE_STATUSES`/`TERMINAL_NODE` de
  `contracts/workflow.py` en vez de un segundo vocabulario, y
  `loop_effect_idempotency_key` fold-ea la iteración en el fingerprint para
  que dos intentos de la MISMA iteración colisionen pero dos iteraciones
  distintas nunca lo hagan.

## Cobertura

| Pieza | Cobertura |
|---|---|
| `simulate()` estructural, sin efectos | **presente** |
| Distinción AND-deps vs ramas de control, con advertencia explícita | **presente** |
| Esperas humanas separadas de ramas no tomadas | **presente** |
| Pantalla `/workflows`, tres modos explícitos | **presente** |
| Grafo SVG por capas, navegación por teclado, alternativa en lista | **presente** |
| `NodeInspector` con re-lint al cambiar config | **presente** |
| Aviso de lint que abre el nodo correspondiente | **presente** (vía `subject` parseado; no reubica el cursor dentro del textarea de config) |
| `RunOverlay` sobre el mismo grafo, ejecución confirmada | **presente** |
| Chat/canvas sobre la misma definición (sin segundo formato) | **presente** (un solo `definition` en estado; `layout` se exporta aparte vía `interchange.export_canonical`, nunca fusionado) |
| Contrato de iteración acotada (diseño) | **presente como documento + dataclasses**, explícitamente **no cableado al motor** |
| Wiring del `"loop"` real al engine/scheduler | **ausente** (fuera de alcance de este lote por contrato) |
| Importación real de un export de aigraphstudio (formato exacto) | **no verificado** (hereda la misma reserva ya documentada en `docs/api/topology.md`'s Interchange: forma asumida, no contrastada contra el exportador real) |

## Estado comparativo

**Ventaja propia** en el núcleo del simulador: `simulate()` es más estricto
que lo que el INFORME pedía en un punto — nunca asume que un `condition`/
`human_approval` sin elección pasa, y separa explícitamente "esperas
humanas" de "ramas no tomadas" de "pendiente de elección" (tres cubos, no
dos). **Hipótesis de mejora, no medida**, en la usabilidad del grafo SVG
frente a un canvas dedicado tipo React Flow (ADP-14 ya lo señalaba): sin
comparación de uso real, solo construcción y tests estáticos. **Comparación
pendiente** en el contrato de iteración acotada: es un diseño propio, no
contrastado contra una implementación de referencia externa (el INFORME no
detalla un contrato exacto de aigraphstudio para `Loop`, solo lo nombra
como tipo de nodo `design_only` — confirmado en
`src/workflows/interchange.py`).

## Decisión

- `src/workflows/simulate.py` / `POST /api/workflows/simulate`: **conservar
  tal cual** — cubre el criterio de aceptación del INFORME sin necesitar
  ampliación inmediata.
- `PlanGraph`/`NodeInspector`/`RunOverlay`/`WorkflowsScreen`: **conservar y
  extender** — el núcleo (tres modos, grafo por capas, re-lint) está
  completo; un futuro lote podría añadir persistencia de `layout` (hoy
  `exportWorkflowDefinition` existe pero ningún botón de la pantalla lo
  invoca todavía — ver Límites) y una vista de "por qué este camino" que
  cruce con `simulate()`'s `awaiting_choice`.
- `src/contracts/workflow_iteration.py` /
  `docs/design/bounded-workflow-iterations.md`: **componer** — queda como
  pieza lista para que un lote futuro decida cablearla (o no) al motor;
  este lote no toma esa decisión por sí mismo, tal como pedía el encargo.

## Pruebas ejecutadas

```
python3 -m pytest tests/test_cmp07_simulate.py tests/test_cmp07_workflow_iteration.py -q -p no:cacheprovider -W ignore
# 39 passed

python3 -m pytest tests/test_cmp07_workflows_js.py -q -p no:cacheprovider -W ignore
# 5 passed (node presente en el entorno)

node studio/checks/workflows.check.mjs
# ok workflows

python3 scripts/i18n_es.py && python3 scripts/i18n_es.py --check
# es.ts: 6850 strings; sin claves de este lote pendientes
# (queda "The server did not return a plan comparison." — es de W2-B, no de este lote)

./node_modules/.bin/tsc --noEmit -p tsconfig.json
# limpio salvo studio/src/screens/studio/model.ts(665) — fichero ajeno a
# este lote, no tocado aquí; ver informe final para el detalle
```

Vecinos (`-k "workflow or topology"`): ver informe final — ejecutado junto
al resto de la superficie de workflows tocada por W2-B en paralelo.
