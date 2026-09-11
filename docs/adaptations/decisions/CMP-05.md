# CMP-05 — Supervisión: de estado técnico a atención útil

**Versión y recorrido.** Base `95747d9` (master, sin git en este entorno de
ejecución). Ola 1 ya había producido `src/attention.py` +
`routes/attention_routes.py` + `studio/src/adapters/attention.ts` (ADP-11:
`classify()` con ocho estados de prioridad fija, lectura por owner). Este
lote (W2-C, CMP-05) profundiza esa base — no la reemplaza; los 28 tests de
`tests/test_adp11_attention.py` siguen pasando sin modificar una sola
aserción.

## Solución actual (antes de este lote)

`classify(RunInfo) -> Attention{kind, reason, priority, since, detail}` — un
único eje (`kind`) que mezclaba a la vez ciclo de ejecución, causa de
espera y salud de conexión (p. ej. `disconnected` era simultáneamente "va a
más de 120s sin evento" Y "por eso no confíes en que siga trabajando"). La
UI (`Activity.tsx`) ya tenía: pestaña "Needs action" con las seis/ocho
categorías, contador de no-leídos, orden por prioridad. No había vista por
proyecto, ni congelación de orden durante interacción, ni MRU, ni protección
explícita contra una respuesta tardía sobrescribiendo la selección.

## Solución de referencia (INFORME §3.4)

Herdr Studio: una autoridad de estado por agente (hooks completos cuando
los hay, manifestaciones de pantalla en otro caso), que expone POR QUÉ
clasificó un estado y documenta los límites del fallback; proyecta ese
estado en navegación, switcher e inspector. No es código adoptado — Herdr no
es un repositorio al que este lote tenga acceso ni licencia que citar; es un
patrón de diseño usado como referencia conceptual, tal como el propio
INFORME lo presenta ("Referencia externa"). El cliente HTTP real hacia un
runtime Herdr (solo lectura, `certainty ∈ {structured, heuristic}`) es
CMP-06 — explícitamente fuera de este lote (W2-H).

## Mecanismo concreto de diferencia

1. **Cuatro ejes separados**, nunca refundidos entre sí:
   - `lifecycle` (`queued`/`running`/`waiting`/`finished`/`failed`/`cancelled`) —
     estado propio de la ejecución.
   - `wait_cause` (`approval`/`question`/`gpu_queue`/`dependency`/`none`) —
     por qué no avanza, si acaso.
   - `connection_health` (`live`/`stale`/`disconnected`) + `signal
     {source, age_s, last_event_at}` — si la señal se ve sana, de dónde
     viene y qué antigüedad tiene. `source` es siempre `"events"` para todo
     lo que este módulo clasifica hoy (nada heurístico se cuela); el campo
     acepta `"heuristic"`/`"heartbeat"` como hueco tipado para CMP-06.
   - `next_action` (`approve`/`answer`/`open`/`retry`/`reconnect`) —
     función determinista de `kind` (y de `lifecycle` para
     `finished_unreviewed`: `failed` → `retry`).
   `kind`/`reason`/`priority` (ADP-11) quedan intactos como el eje de
   urgencia que ya eran.
2. **Marcador durable de "terminado"** (`src/agent_runs.py`,
   `_record_finished_marker`/`finished_marker`/`finished_markers`,
   `DATA_DIR/finished_runs.json`) — escrito una sola vez, en el `finally` de
   `_drain`, el único sitio donde `run.status` se asienta a un valor
   terminal. Cierra el hueco que `docs/api/attention.md` documentaba como
   límite conocido: antes, TODA sesión terminada se presentaba como
   `status="done"` adivinado; ahora `lifecycle`/`next_action` reflejan el
   desenlace real cuando el marcador existe, con fallback exacto al
   comportamiento anterior cuando no.
3. **`project_id` por fila** (`_default_project_ids`, lee
   `Session.project_id` — la misma columna que ya usa
   `routes/session_routes.py`, nunca un segundo lugar).
4. **Frontend (`Activity.tsx`):**
   - Insignias de lifecycle/wait-cause/conexión+edad en cada tarjeta y en el
     panel de detalle (`AttentionFacts`), nunca refundidas en la línea de
     `reason`.
   - Chip `next_action` visible por fila — abre el mismo panel de detalle
     donde ya se decide (nunca aprueba/responde en un clic: ese paso sigue
     exigiendo el campo de motivo existente).
   - Vista "por proyecto" (`?view=project`, `adapters/activity.ts::
     groupByProject`, puro — reagrupa la lista ya cargada, sin red nueva) y
     vista "por atención" (la prioridad de `classify()` que ya existía).
   - Orden estable mientras se interactúa
     (`adapters/activity.ts::stableAttentionOrder` + `onMouseEnter/Leave`/
     `onFocus/Blur` en el contenedor de la lista): una fila nueva se añade al
     final, nunca reordena lo que ya estaba bajo el puntero.
   - MRU "último agente usado" (`localStorage`, best-effort, por
     navegador) con botón "Continue: …" en la cabecera.
   - Guardia contra respuesta tardía (`adapters/activity.ts::nextRunParam`
     + `closeIfStillOpen`): la clave del run se captura en el momento del
     clic, no cuando la promesa resuelve; si la persona ya navegó a otro
     run mientras tanto, la resolución tardía no lo cierra ni lo sustituye.

## Cobertura

| Punto de la ficha | Cobertura |
|---|---|
| Ejes separados (lifecycle/wait_cause/connection_health/atención) | **presente** |
| Cada tarjeta responde qué intenta hacer / qué lo frena / desde cuándo / siguiente acción | **presente** |
| Vista por proyecto | **parcial** — cubre `chat`/`question` (vía `Session.project_id`) y `workflow` (vía su propio `project_id`); `task`/`render`/`approval` caen en "Sin proyecto" (no tienen esa columna hoy). |
| Vista por atención | **presente** (ya existía en ADP-11; conservada) |
| Orden estable mientras se interactúa | **presente** |
| Navegación al último agente usado (MRU) | **presente** |
| "Comparación de varias tareas" | **parcial** — la vista agrupada muestra el estado de varias tareas a la vez sin abrir cada una; no hay una vista de comparación lado a lado dedicada (diff de dos runs). No inflar: esto es una lectura conservadora de una frase corta del INFORME, no una funcionalidad explícitamente especificada con criterio de aceptación propio. |
| Continuidad de selección al volver | **presente** — estructural (la selección se busca por id en la lista fresca en cada poll, nunca por posición) + explícita para el caso de carrera (`nextRunParam`). |
| Identidad y antigüedad de la señal visibles | **presente** (`signal.source`/`signal.ageS`, fila + detalle) |
| Eventos estructurados para ejecuciones propias; heurística solo para runtimes externos | **presente** — `signal_source` es siempre `"events"` en este módulo; el hueco `"heuristic"` queda tipado y documentado para CMP-06, no implementado aquí. |
| No degradar estado nativo a heurística para unificar colores | **presente** — ninguna heurística de color sustituye una señal estructurada; `connection_health` deriva de `last_event_at` real, no de una inferencia visual. |
| Marcador durable "terminado" (encontrado como límite en la ola 1) | **presente** — cerrado en `src/agent_runs.py`, edición mínima anclada en `_drain`. |

## Estado comparativo

**Ventaja propia** en el núcleo (clasificación con fuente y motivo, cero
heurística de color, aprobación/pregunta siempre resueltas por la ruta
real, nunca un clic directo desde la tarjeta) — Faustus ya tenía la base
(ADP-11) y este lote la profundiza con el mismo rigor de no inventar señal
donde no la hay. **Hipótesis de mejora, no medida**, en la vista por
proyecto y la congelación de orden: no hay una prueba de usuario real que
compare "antes/después" en productividad — las pruebas ejecutadas (abajo)
verifican COMPORTAMIENTO correcto, no percepción de utilidad.

## Decisión

**Extender.** ADP-11 quedó intacto en su contrato público; se añadieron
cuatro ejes, un marcador durable y la UI correspondiente sin sustituir ni
duplicar nada existente (una segunda lista de runs, que el propio INFORME
avisa de no crear, no se creó — todo es una re-proyección de los mismos
datos ya cargados).

## Pruebas ejecutadas

- `python3 -m pytest tests/test_cmp05_attention.py -q -p no:cacheprovider -W ignore`
  → 33 passed (incluye la prueba decisiva: aprobación + cola de GPU +
  conexión caída + run sano, un solo `attention_for_owner()`, sin abrir
  chats — `test_decisive_several_runs_one_approval_a_gpu_queue_and_a_dropped_connection`).
- `python3 -m pytest tests/test_adp11_attention.py -q -p no:cacheprovider -W ignore`
  → 28 passed, sin cambios (compatibilidad hacia atrás).
- `python3 -m pytest tests -q -p no:cacheprovider -W ignore -k "activity or attention or agent_runs"`
  → 113 passed.
- `python3 -m pytest tests/test_studio_guards.py -q -p no:cacheprovider -W ignore`
  → 11 passed (sin `outline:none`, sin color literal, sin `transition: all`).
- `node studio/checks/attention-cmp05.check.mjs` → ok (mergeAttention con
  los campos nuevos, `groupByProject`, `stableAttentionOrder`,
  `nextRunParam`, `project_id` de workflow, parseo de fila con campos
  ausentes — fallback seguro).
- `node -e "esbuild.build(...Activity.tsx...)"` → bundle correcto (aviso de
  clave duplicada `"Recipe"` en `es.ts` es de OTRO lote paralelo — ver
  "Límites" abajo, no se tocó ese contenido).
- `./node_modules/.bin/tsc --noEmit -p tsconfig.json` desde la raíz → **no
  concluyente**: falla por dos causas ajenas a este lote (ver abajo), no
  por nada en `src/attention.py`/`agent_runs.py`/`Activity.tsx`/
  `adapters/attention.ts`/`adapters/activity.ts`/`activity.css`.

## Límites y validaciones pendientes

- **`tsc --noEmit` global no verificado limpio** — bloqueado por dos cosas
  de otros lotes paralelos, encontradas durante la verificación de este
  mismo: (1) `studio/src/i18n/es.ts:3719` clave `"Recipe"` duplicada (dos
  filas `Recipe\tReceta` en `docs/ui/i18n/es.tsv`, líneas 3245 y 6692 —
  otro agente añadió una que ya existía; la regeneración final del
  orquestador debería deduplicar el TSV antes de regenerar `es.ts`); (2)
  `studio/src/screens/studio/model.ts:665` "Function lacks ending return
  statement" — fichero ajeno a este lote, no tocado aquí. El `esbuild`
  aislado de `Activity.tsx` (arriba) sí compila limpio, que es la señal más
  cercana a "sintaxis y tipos locales correctos" disponible sin depender de
  que el resto del árbol compile.
- Vista por proyecto: `task`/`render`/`approval` no llevan `project_id`
  hoy — documentado, no oculto (ver tabla de cobertura).
- `wait_cause == "gpu_queue"` sigue siendo la señal de cola de carril de
  siempre (`agent_runs.queued_positions()`), sin cruzar contra
  `src/resource_admission.py` — dejado así deliberadamente (terreno de
  W2-B/CMP-08).
- "Comparación de varias tareas" — cobertura parcial, ver tabla arriba.
- MRU y congelación de orden son mecanismos de cliente (localStorage /
  estado de React) — nunca verificados con un test de interacción real de
  ratón (jsdom no está en el harness de este repo); cubiertos por unit
  tests de la lógica PURA que los sostiene (`stableAttentionOrder`,
  `nextRunParam`) más inspección de que los manejadores DOM están cableados
  (`test_activity_screen_uses_the_new_exports`).
- **Punto a cablear por el orquestador:** deduplicar `docs/ui/i18n/es.tsv`
  (fila `Recipe` repetida, ver arriba) antes de la regeneración final de
  `es.ts`; ninguna fila de ESTE lote está duplicada (verificado: `No
  project`, `By project`, `Continue: {label}`, `GPU queue`, `Dependency`,
  `Lifecycle`, `Waiting on`, `Connection`, `{n}s old`, `Signal: {source},
  session {id}` — una sola vez cada una).

## Ficheros

- `src/attention.py` — cuatro ejes nuevos en `Attention`/`RunInfo`,
  `_lifecycle_of`/`_wait_cause_of`/`_connection_of`/`_next_action_of`,
  `_default_finished_statuses`/`_default_project_ids`, `attention_for_owner`
  extendido (parámetros `finished_statuses`/`project_ids`, nuevos campos en
  cada fila).
- `src/agent_runs.py` — `_record_finished_marker`/`finished_marker`/
  `finished_markers`/`_finished_marker_path` + una llamada anclada en el
  `finally` de `_drain`.
- `routes/attention_routes.py` — sin cambios (ya reenviaba lo que
  `attention_for_owner` devolviera).
- `studio/src/adapters/attention.ts` — tipos `Lifecycle`/`WaitCause`/
  `ConnectionHealth`/`SignalSource`/`NextAction`/`AttentionSignal`, parseo
  con fallback seguro.
- `studio/src/adapters/activity.ts` — `ActivityRun.attention` extendido,
  `ActivityRun.projectId`, `groupByProject`/`stableAttentionOrder`/
  `nextRunParam`/`runProjectId` (puras, exportadas).
- `studio/src/screens/Activity.tsx` — todo salvo `WorkflowEstimateView`
  (ya movido por W2-B a `screens/activity/EstimateView.tsx` antes de que
  este lote tocara el fichero; no se tocó ese componente).
- `studio/src/screens/activity.css` — insignias, chip de siguiente acción,
  conmutador de vista, cabecera de grupo por proyecto.
- `tests/test_cmp05_attention.py` (33 tests) +
  `studio/checks/attention-cmp05.check.mjs`.
- `docs/api/attention.md` — sección CMP-05 añadida.
- `docs/ui/i18n/es.tsv` — 10 filas nuevas al final (ver arriba).
