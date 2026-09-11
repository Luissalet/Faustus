# CMP-13 — Alternativas aisladas comparables

**Versión y recorrido.** `master` en `95747d9` al empezar W2-G; este lote
añade `src/alternatives.py`, `src/git_panel.py::worktree_add/
worktree_remove/worktree_list`, `routes/alternatives_routes.py`,
`src/agent_tools/alternatives_tools.py` (+ registro en los seis catálogos
de tools), `studio/src/screens/alternatives/{AlternativesScreen,
CompareView}.tsx` + `alternatives.css`, `studio/src/adapters/
alternatives.ts`, ruta `/alternatives` en `app.py`/`routes.ts`/
`AppShell.tsx`, `tests/test_cmp13_alternatives.py` +
`tests/test_cmp13_alternatives_js.py` + `studio/checks/alternatives.check.mjs`,
`docs/api/alternatives.md`.

## Solución actual (antes de este lote)

Dos piezas reales, ninguna cubriendo lo que pide INFORME §3.12:

1. **`src/branching_futures/`** (424 líneas en `service.py` + contratos
   estrictos en `contracts.py`): un motor de "futures" MUY completo en el
   nivel de orquestación — `N` estrategias con criterios de puntuación
   ponderados (`max`/`min`, `blocking`/`scored`/`metric`), presupuestos
   (`max_branches`, `max_total_cost`, `max_total_tokens`,
   `max_wall_seconds`), modos (`plan_only`/`simulate`/`prototype`/
   `isolated_execute`/`shadow`/`canary`) y selección automática opcional.
   Pero su propio `isolation.py` lo dice sin rodeos: `BranchIsolator` es un
   `Protocol` (`fork`/`inspect`/`cleanup`) y la ÚNICA implementación que
   existe es `InMemoryIsolator`, cuyo docstring dice literalmente *"no
   filesystem or network capability; lets lifecycle and fairness tests
   prove that branches cannot share mutable outputs before a real isolator
   is trusted"* — es decir: el aislamiento REAL de ficheros nunca se
   implementó, solo el contrato que un isolador de verdad debería cumplir.
   `grep` de `InMemoryIsolator`/`BranchIsolator` en todo el repo confirma
   que no hay una segunda clase que lo implemente. Tampoco hay pantalla de
   Studio que consuma `branching_futures`/`BranchingService` (`grep` en
   `studio/src` → 0 resultados); su única superficie es
   `routes/branching_futures_routes.py`, gateada por `require_admin` y por
   el ajuste `agent_branching_futures` (`False` por defecto).
2. **`src/git_panel.py`**: sin `git worktree` en absoluto (`grep worktree`
   → 0 resultados antes de este lote) — la única forma de tener dos
   copias de trabajo de un mismo repo era clonar aparte a mano.

Ninguna de las dos da lo que pide la ficha: un experimento concreto,
orientado a fichero, con N alternativas que un USUARIO (no solo el motor de
estrategias) puede crear, comparar por diff y aplicar con una fusión a tres
vías que nunca destruye su propia edición manual.

## Solución de referencia (INFORME §3.12)

`Experiment{id, project_id, owner, goal, base_ref, alternatives: [...]}`;
aislamiento por `worktree`/`snapshot_dir`/`doc_version` según lo que sea el
workspace; `run_tests`, `compare` (diffs contra base y entre alternativas),
`apply` (fusión a tres vías explícita, nunca destruye edición manual, falla
en conflicto), `combine` (aplicar por fichero); herramientas
`alt_start/alt_compare/alt_apply` con `alt_apply` gateada por aprobación
humana como `git_merge`; hooks de git nunca se ejecutan al crear worktrees.

## Mecanismo concreto de diferencia

* **Aislamiento real, no un contrato sin implementar**: `git_panel.
  worktree_add` (`-c core.hooksPath=` en el propio comando — un solo
  proceso `git`, hooks neutralizados por diseño, no `--no-checkout` +
  checkout manual) para repos git; copia congelada de directorio
  (`shutil.copytree`) para un workspace sin git; texto en el propio
  registro del experimento para un documento suelto. Las tres formas
  QUEDAN implementadas y probadas — a diferencia de `InMemoryIsolator`.
* **`apply`/`combine` con garantía todo-o-nada verificada**:
  `_build_plans` calcula el plan de CADA fichero (incluida la fusión a
  tres vías vía `git merge-file -p --diff3`) antes de escribir nada; un
  solo conflicto aborta el conjunto entero. `branching_futures` no tiene
  operación de "aplicar sobre la copia principal" en absoluto — sus
  branches se seleccionan (`selected`/`committed`) pero no hay código que
  fusione contenido de fichero alguno de vuelta a un workspace real,
  porque no hay workspace real que tocar sin un isolador real.
* **Superficie de usuario, no solo de motor**: pantalla `/alternatives`
  con lista de experimentos, comparación por fichero, banner de conflicto
  explícito y botón de aplicar con confirmación — `branching_futures` no
  tiene equivalente en Studio.

## Cobertura

| Pieza del contrato | Cobertura |
|---|---|
| `Experiment`/`Alternative`, aislamiento worktree/snapshot_dir/doc_version | **presente** |
| `run_tests` (comando real, sin shell, dentro del aislamiento) | **presente** |
| `compare` (diff contra base + `contested_files`) | **presente**; diff POR PARES entre alternativas — **ausente**, alcance declarado en `docs/api/alternatives.md` (el conjunto `contested_files` ya dice dónde comparar dos alternativas a mano) |
| `apply` (tres vías, nunca destruye edición manual, falla en conflicto) | **presente**, con test decisivo pasando |
| `combine` (aplicar por fichero) | **presente** |
| Hooks nunca se ejecutan al crear worktrees | **presente** (`-c core.hooksPath=`, elegido y documentado) |
| Tools `alt_start/alt_compare/alt_apply`, `alt_apply` con aprobación humana | **presente**, registrado en los seis catálogos |
| Integración con `src.branching_futures.isolation.BranchIsolator` (el Protocol que este lote técnicamente podría satisfacer) | **no verificado / no hecho** — ver "Decisión" |
| `doc_version` cableado al almacén real de documentos de Studio | **ausente**, alcance declarado |

## Estado comparativo

**Ventaja propia** para el caso que pide la ficha (aislamiento real +
aplicar con fusión a tres vías + UI): `branching_futures` no lo intenta —
es un motor de puntuación de estrategias de alto nivel con un aislador que
su propio autor dejó como fixture. **No hay solapamiento de producto**
real hoy (uno decide QUÉ estrategia de agente ganó por puntuación; el otro
deja a un humano o al agente COMPARAR y APLICAR cambios de fichero
concretos) — pero sí hay un solapamiento de INTERFAZ: el `Protocol
BranchIsolator` (`fork`/`inspect`/`cleanup`) es casi exactamente lo que
`add_alternative`/`apply_alternative` de este módulo ya hacen con nombres
distintos.

## Decisión: **componer** (no integrar en este lote; dejar el punto de
integración documentado y trivial de cablear)

No se fusionan los dos módulos en W2-G: `branching_futures` vive en un
plano de abstracción distinto (selección automática por criterios
ponderados sobre ejecuciones completas de agente) y su propio esquema de
contratos (`FUTURE_STATUSES`, `criteria`, `budget`) no tiene un análogo
limpio en `Experiment`/`Alternative` sin inventar una traducción con
pérdida — hacerlo bien es un lote propio, no una nota al margen de este.
Lo que SÍ se deja hecho y documentado, para que el orquestador (o un lote
futuro) lo cablee sin rediseñar nada:

* `git_panel.worktree_add/worktree_remove` son EXACTAMENTE la forma que un
  `BranchIsolator.fork`/`cleanup` real necesitaría para repos git — el
  mismo mecanismo, con nombres de módulo distintos. Un adaptador delgado
  (`WorktreeIsolator` implementando el `Protocol` de
  `src/branching_futures/isolation.py` sobre `git_panel.worktree_*`) cerraría
  el hueco que ese módulo lleva declarado como "adapters plug in later"
  sin tocar `src/alternatives.py` en absoluto.
* No se descarta `branching_futures` ni se le quita superficie: sigue
  existiendo, gateado por admin, tal cual estaba.

## Pruebas ejecutadas

* `python3 -m pytest tests/test_cmp13_alternatives.py -q -p no:cacheprovider -W ignore`
  → **16 passed** (creación git_sha/snapshot, aislamiento worktree
  verificado como independiente, scoping por owner, `compare` con
  `contested_files`, fast-forward, fusión a tres vías real sobre líneas no
  solapadas, **la prueba decisiva** —dos alternativas tocan la misma línea
  mientras el usuario edita esa misma línea en la copia principal: aplicar
  cualquiera de las dos se rechaza y la edición manual sobrevive byte a
  byte, comprobado dos veces—, conflicto en un fichero no aplica
  parcialmente otro fichero sin conflicto, `combine` por fichero,
  `doc_version` ida y vuelta, `run_tests` éxito/fallo con `shlex.split`
  (nunca shell), borrado de experimento limpia el worktree, coste nace
  `"unknown"`).
* `python3 -m pytest tests -q -p no:cacheprovider -W ignore -k "git_panel or alternatives or tool_registry"`
  → **80 passed, 0 failed** (tras añadir `docs/api/alternatives.md`) — sin
  regresión en `git_panel.py` existente ni en el resto del registro de
  tools.
* `python3 -c "import ast; ast.parse(...)"` sobre los once ficheros
  Python tocados/nuevos → todos parsean.
* Import en caliente de `src.agent_tools` → `TOOL_HANDLERS`/`TOOL_TAGS`
  contienen `alt_start`/`alt_compare`/`alt_apply`; `tool_schemas.
  FUNCTION_TOOL_SCHEMAS` (132 entradas), `tool_capabilities.
  TOOL_CAPABILITIES`, `tool_index.BUILTIN_TOOL_DESCRIPTIONS` y
  `tool_security.NON_ADMIN_BLOCKED_TOOLS` — los tres nombres presentes en
  los cinco. (Nota aparte, no causada por este lote: `import src.
  tool_schemas` en frío, ANTES de `src.agent_tools`, falla por un ciclo de
  importación preexistente entre ambos módulos — reproducido también sin
  ninguno de los cambios de este lote; la app real importa `agent_tools`
  primero, así que no es una regresión.)
* `node studio/checks/alternatives.check.mjs` → **`ok alternatives`**.
* `python3 -m pytest tests/test_cmp13_alternatives_js.py -q -p no:cacheprovider -W ignore`
  → **3 passed**.
* `./node_modules/.bin/tsc --noEmit -p tsconfig.json` (desde la raíz) → UN
  solo error en todo el árbol, en `studio/src/screens/studio/model.ts:665`
  (`Function lacks ending return statement`) — fichero que este lote NO
  toca; no hay ningún error en `studio/src/adapters/alternatives.ts` ni en
  `studio/src/screens/alternatives/*`. Probablemente de otro lote en
  paralelo tocando `model.ts`; el orquestador debe confirmarlo al fusionar.
* `python3 -m pytest tests/test_studio_guards.py -q -p no:cacheprovider -W ignore`
  → **11 passed** sobre TODO el árbol de Studio (sin `outline:none`, sin
  colores literales, sin svg inline, sin `transition: all`, sin div
  interactivo) — los ficheros nuevos de este lote no violan ningún guard.
* `./node_modules/.bin/vite build` — NO ejecutado en este lote (build
  completo de producción, coste alto; los tres pasos anteriores ya cubren
  tipado, wiring y guards de los ficheros propios). El orquestador debe
  correrlo sobre el árbol final tras fusionar los nueve lotes.

## Límites y validaciones pendientes

* Ver "Límites conocidos" en `docs/api/alternatives.md` (diff por pares
  entre alternativas no implementado; `doc_version` no cableado al
  almacén real de documentos; sin seguimiento de coste real de ejecución).
* `vite build` no ejecutado en este lote (ver arriba); `tsc --noEmit` y
  los guards de Studio SÍ se corrieron y pasan para los ficheros propios
  de este lote — el orquestador debe repetir los tres (incluido `vite
  build`) sobre el árbol final tras fusionar los nueve lotes, ya que
  varios tocan `studio/` a la vez y el error preexistente de
  `model.ts:665` (ajeno a este lote) debe resolverse antes de esa
  fusión final.
* La composición con `src.branching_futures` (adaptador `WorktreeIsolator`
  sobre `git_panel.worktree_*`) queda como trabajo futuro documentado, no
  como código sin probar en este lote.
