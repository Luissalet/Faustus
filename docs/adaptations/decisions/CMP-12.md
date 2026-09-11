# CMP-12 — Recetas de trabajo

**Versión y recorrido:** repo en `95747d9` (master) al arrancar la Ola 2;
sin git disponible en este entorno — ficha escrita directamente sobre el
árbol de ficheros.

## Solución actual (antes)

Nada en el repo tenía el concepto de "receta": la única forma de repetir
un procedimiento era o bien re-explicarlo cada vez en el mensaje, o bien
apoyarse en una skill completa de `src/skills_runtime/` (frontmatter +
markdown largo) — "la montaña de skills" que el propio informe señala como
lo que una receta debe evitar para una tarea puntual y pequeña. No hay fila
`docs/adaptations/baseline.md` para esto (nace en la Ola 2).

## Solución de referencia (INFORME_COMPARATIVO_V2 §3.11)

`{id, title, inputs[], steps[], tools[], success_conditions[],
optional_resources[], license?}`; el turno con receta activa inyecta un
procedimiento estructurado (no la montaña de skills); `from-run` convierte
una tarea exitosa en receta REVISABLE (`status: draft`, nunca secretos: pasa
por `core/log_safety` o el redactor existente).

## Mecanismo concreto de diferencia

* `src/recipes.py` (NUEVO): `Recipe` (dataclass, `to_dict`/`from_dict`),
  `list_recipes(owner=None)` (built-ins de `docs/recipes/*.json` + drafts
  privados del owner en `DATA_DIR/recipes/<owner>/*.json`),
  `get_recipe`, `procedure_block` (el texto corto que se inyecta en el
  prompt), y `from_run(run_id, owner)`.
* `docs/recipes/*.json` (NUEVO, 4 built-ins): `review-changes.json`,
  `sources-to-report.json`, `design-function-and-tests.json`,
  `edit-passage-keep-tone.json` — exactamente las cuatro que pide el
  contrato («revisa estos cambios», «convierte estas fuentes en un
  informe», «diseña una función y genera sus pruebas», «edita este pasaje
  sin cambiar el tono»).
* `from_run`: lee `DATA_DIR/runs/<run_id>.jsonl` (la forma documentada por
  `src/agent_runs.py`, reimplementada aquí como lectura de un formato
  estable en vez de importar su `_log_path` privado), pasa el fichero
  ENTERO por `core.log_safety.redact_secrets` antes de extraer nada, exige
  un estado terminal `done` (si no, `400 recipes.run_not_finished`),
  construye `steps`/`tools` a partir de los `tool_start` reales (nombres
  distintos, en orden de aparición — nunca inventados) y usa el `label`
  propio del run como título. Guarda siempre `status: "draft"` — nunca
  publica ni sustituye un built-in.
* `src/strategy_policy.py::choose_strategy(..., context={"recipe_id": ...})`
  sustituye los `steps` genéricos del método por los de la receta activa —
  una receta no es un concepto paralelo, es un `Strategy` con los mismos
  campos.
* `src/agent_loop.py::_strategy_block` inyecta `recipes.procedure_block`
  cuando hay receta activa (ver CMP-09.md para el resto del cableado del
  turno).
* `routes/strategy_routes.py`: `GET /api/recipes`,
  `POST /api/recipes/from-run/{run_id}`.
* `studio/src/screens/studio/Composer.tsx::RecipeSelector` (popover, mismo
  patrón que `AutonomyPresetSelector`/`StrategyProfileSelector`) +
  `studio/src/adapters/strategy.ts::loadRecipes/createRecipeFromRun`.

## Cobertura

**Presente**: shape de receta, 4 built-ins, inyección en la estrategia,
`from_run` con redacción de secretos y rechazo de runs sin terminar/no
encontrados, ruta HTTP, UI de selección — todo probado.

## Estado comparativo

**Ventaja propia**: la ficha de referencia no especifica CÓMO se
reconstruye una receta desde un run real; esta implementación deja
explícito el límite (solo `tool_start` distintos, sin inputs
reconstruidos) en vez de fingir una reconstrucción completa — ver Límites.

## Decisión

**Integrar** (nuevo módulo).

## Pruebas ejecutadas

```
python3 -m pytest tests/test_cmp09_strategy.py -q -p no:cacheprovider -W ignore
# 28 passed (incluye los tests de recipes.py — built-ins, drafts privados
# por owner, from_run con redacción de secretos, rechazo de run no
# terminado/no encontrado, y las rutas GET /api/recipes /
# POST /api/recipes/from-run/{run_id})

python3 -m pytest tests -q -p no:cacheprovider -W ignore -k "agent_loop or composer or strategy or recipe"
# 399 passed, 5 skipped, 16451 deselected (281.58s)
```

## Límites / validaciones pendientes

* `from_run`'s `inputs` es un placeholder genérico (`["task description"]`)
  — el log del run solo tiene el lado del ASISTENTE (eventos de
  herramienta + estado), no el mensaje original del usuario, así que no se
  puede reconstruir de forma fiable solo con ese fichero.
* Ninguna promoción automática de draft → built-in: deliberadamente fuera
  de alcance; `status` es el punto de enganche para una revisión humana
  futura.
* `_run_log_path` reimplementa la forma documentada de
  `src/agent_runs.py` (`DATA_DIR/runs/<session>.jsonl`) en vez de importar
  su helper privado — acoplamiento documentado, no oculto (ver
  `docs/api/strategy.md`, Límites).
* Sin wiring de `studio/src/adapters/chat.ts` para el evento SSE
  `strategy` en vivo (compartido con CMP-09.md) — la UI usa el estado
  persistido, no el evento en vivo.
