# CMP-09 — Estrategia observable

**Versión y recorrido:** repo en `95747d9` (master) al arrancar la Ola 2;
sin git disponible en este entorno, así que esta ficha documenta el trabajo
directamente sobre el árbol de ficheros, no sobre un commit.

## Solución actual (antes)

`src/agent_loop.py` decide implícitamente cuánto esfuerzo dedicar a una
tarea — repartido entre docenas de heurísticas de construcción de prompt
(`_classify_agent_request`, los distintos `*_mode` de ruta, los presupuestos
de `autonomy_budget.py`) sin que exista un único valor inspeccionable que
diga "esto es lo que voy a hacer y por qué". No hay perfil
rápido/equilibrado/revisión-intensiva, ni una razón legible por la que el
turno eligió un camino u otro. `docs/adaptations/baseline.md` no tenía fila
para esto (no es ADP-XX; nace en la Ola 2 del informe comparativo V2).

## Solución de referencia (INFORME_COMPARATIVO_V2 §3.8)

`choose_strategy(task_text, *, profile, context) -> Strategy` con
`method ∈ {direct_edit, plan_then_execute, research, specialised_review,
explore_alternatives}`, `steps`, `budget`, `models_hint`,
`permissions_needed`, `close_criteria`, `reasons[]` — observable y
editable; la escalada solo por fallos observables/requisitos no cubiertos,
nunca por "confianza" del modelo; perfiles con diff visible de qué cambia;
la política nunca lanza un council por defecto.

## Mecanismo concreto de diferencia

* `src/strategy_policy.py` (NUEVO): `Strategy` (dataclass) +
  `choose_strategy` (patrones léxicos legibles para `method`, escalada
  SOLO desde `context["failures_observed"]`/`context["uncovered_requirements"]`,
  nunca desde una puntuación de confianza) + `profile_diff` (puro, sin I/O)
  + persistencia `get_active`/`set_active` por owner (y opcionalmente por
  `session_id`) en `DATA_DIR/strategy_profile.json`, mismo patrón que
  `src/attention.py`'s read marks.
* `routes/strategy_routes.py` (NUEVO): `GET/PUT /api/strategy/profile`,
  `POST /api/strategy/preview`. Registrado en `app.py` junto a
  `board_routes` (misma ancla que pide el contrato).
* `src/agent_loop.py` (edición mínima, SOLO lo que el lote autoriza):
  - `_strategy_block(owner, session_id, task_text)` — bloque de prompt,
    mismo patrón de caché que `_project_board_block` (TTL propio, 5s, más
    corto a propósito: cambiar de perfil en el compositor debe notarse en
    el siguiente turno).
  - Evento SSE `strategy`, emitido una vez por turno (`round_num == 1`)
    dentro del bucle de `_stream_agent_loop_body` — el MISMO generador que
    ya emite `steer`/`harness_check`/`paused`/`plan_update`, así que llega
    al cliente por el mismo passthrough que `chat_routes.py` ya les da a
    esos, sin tocar `chat_routes.py` (fuera del alcance de este lote).
* `studio/src/screens/studio/Composer.tsx`: `StrategyProfileSelector`
  (popover fast/balanced/deep_review, mismo patrón que
  `AutonomyPresetSelector`) + `RecipeSelector` (ver CMP-12.md). Estado
  cargado de `GET /api/strategy/profile` al montar/cambiar de sesión;
  cambios persistidos con `PUT`.
* `studio/src/adapters/strategy.ts` (NUEVO): adaptador tipado.

## Cobertura

**Presente** (no "parcial", no "no verificado"): `choose_strategy`,
`profile_diff`, la persistencia, las dos rutas HTTP, el bloque de prompt y
el evento SSE existen y están probados (ver Pruebas ejecutadas).

## Estado comparativo

**Ventaja propia extendida**: el "informe" pide un valor observable con
`reasons[]`; esta implementación además separa las DOS causas de escalada
(fallos observados vs. requisitos no cubiertos) en mensajes distintos
dentro de `reasons[]`, y hace la propia ausencia de "council" una propiedad
del TIPO (`METHODS` no lo contiene) en vez de una regla en tiempo de
ejecución que pudiera romperse.

## Decisión

**Integrar** (nuevo módulo, ningún camino de estrategia previo que
sustituir — no existía nada equivalente en el repo).

## Pruebas ejecutadas

```
python3 -m pytest tests/test_cmp09_strategy.py -q -p no:cacheprovider -W ignore
# 28 passed

python3 -m pytest tests -q -p no:cacheprovider -W ignore -k "agent_loop or composer or strategy or recipe"
# 399 passed, 5 skipped, 16451 deselected (281.58s)

python3 -m pytest tests/test_studio_guards.py -q -p no:cacheprovider -W ignore
# 11 passed

./node_modules/.bin/tsc --noEmit -p tsconfig.json
# 1 pre-existing error in studio/src/screens/studio/model.ts (not touched
# by this lot — a concurrent lot's file; see the report's Límites)

./node_modules/.bin/vite build
# built in 17.89s, no errors
```

## Límites / validaciones pendientes

* La clasificación de `method` es por patrones léxicos deliberadamente
  simples (legibles, no un modelo de clasificación) — un texto de tarea
  ambiguo en un idioma no cubierto por los patrones cae al fallback
  `plan_then_execute`, documentado como "el default seguro y sin opinión".
* El evento SSE `strategy` se emite y llega al navegador, pero
  `studio/src/adapters/chat.ts` no tiene todavía un `case 'strategy':` que
  lo decodifique — fuera del alcance de fichero de este lote (`chat.ts` es
  de W2-D). El compositor refleja el perfil/receta activos vía
  `GET /api/strategy/profile`, no vía el evento en vivo. **Punto a cablear
  por el orquestador.**
* `models_hint`/consultas de hardware son de solo lectura y best-effort
  (`model_router.get_router_config`, `vram_admission.pending`) — nunca
  bloquean la elección de estrategia si fallan.
