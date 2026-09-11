# CMP-04 — Conocimiento que explica por qué importa

- **Lote:** W2-D (`CONTRATO_CMP_W2.md`, ola 2 de CMP).
- **Versión base:** `master` en `95747d9`; sin git en el entorno de trabajo
  (repo montado sin `.git`), así que este documento y el propio código son
  el recorrido — no hay commit que citar más allá del punto de partida.
- **Ficha origen:** `INFORME_COMPARATIVO_V2.md` §3.3 (CMP-04).
- **Depende de la ola 1 de hoy** (`docs/adaptations/baseline.md`, ADP-18/19/20):
  `src/requirements/` (store, `context.for_task`, `evidence.matrix`) y
  `src/project_board.py` ya existían enteros antes de este lote; este lote
  no repite ninguno de los dos, solo los LEE y los conecta a
  `src/context_engine/code_index.py` (también preexistente, ADP-28) en un
  módulo nuevo.

## Qué pedía el informe

`neighborhood(project_id, owner, *, path=None, req_key=None, issue_key=None,
depth=1)` con vecinos TIPADOS `requirement → decision(issue) → symbol →
test → run`, cada arista con `relation ∈ {declared, located, verified}` y
`stale: bool`; selección por unidades semánticas con presupuesto (no
truncado por caracteres); prueba decisiva: requisito cambiado + test
antiguo en verde + código sin actualizar → el vecindario marca la evidencia
como `stale` y el contexto del turno lo dice ("evidencia caducada"), y NO
afirma verificado.

## Solución actual (antes de este cambio)

Tres piezas reales y maduras, sin conectar entre sí:

- `src/requirements/evidence.py::matrix()` — ya calcula
  linked/implemented/tested/verified/stale por requisito, con
  `resolve_link_state` distinguiendo `stale` de `unknown` y las cuatro
  razones de staleness. No expone nada tipado como "vecindario"; un caller
  tiene que pedir el matrix de UN requisito a la vez y no sabe qué símbolo
  de código ni qué issue del tablero se relacionan salvo que ya conozca el
  `target` de cada link.
- `src/project_board.py` — issues/comentarios/links/refs, con
  `list_issues(project_id, q=…)` como búsqueda de texto ya existente
  (`title`/`body_md`). Ninguna noción de "issues que mencionan este
  fichero" salvo pasarle el path como `q`.
- `src/context_engine/code_index.py::neighbors()` — ya hace BFS tipado por
  `kind`/`certainty`/`direction` sobre el grafo de símbolos
  (`imports`/`calls`/`defines`/`tests`/`registers`), con
  `symbols_in(path)` para arrancar desde un fichero. Ninguna relación con
  requisitos ni con el tablero.
- El turno del agente (`src/agent_loop.py`) ya compila y hasta OBSERVA
  paquetes de `context_engine` (`_ce_live_wiring.deliver_round` +
  `observe_receipt`, sección "Receipts are recorded only after
  verification…") pero solo registra el receipt en el store del context
  engine — nada de eso llega al metadata persistido del mensaje ni a un
  evento SSE que la Transcript pueda mostrar.

## Solución de referencia (INFORME §3.3)

Una vista que, dado un fichero o un requisito, responda en una sola
llamada qué debe cumplir, qué decisión lo puso ahí, qué código/test lo
implementa y qué evidencia lo verificó — con cada arista honesta sobre si
fue declarada a propósito, encontrada mecánicamente, o verificada de
verdad, y sin poder afirmar "verificado" cuando la evidencia quedó
caducada.

## Mecanismo concreto de diferencia

- **`src/knowledge_neighborhood.py::neighborhood()`** (NUEVO): para cada
  requisito resuelto (por `req_key` directo, o por `path` vía
  `_requirement_keys_for_path` — el mismo match por prefijo que
  `requirements/context.py::_linked_keys_for_files` usa, reimplementado
  aquí porque ese helper es privado a su módulo y este necesita también
  los links, no solo las keys), llama a `evidence.matrix()` UNA vez y
  traduce cada link ya resuelto (`live_state`/`live_meta`) a un nodo+arista
  tipados — **nunca recalcula linked/implemented/tested/verified/stale**,
  los lee tal cual del matrix (ver "Contratos que no se negocian": una
  sola fuente de verdad sobre verificación).
- **`relation` nunca se inventa**: `declared` para todo link explícito del
  requisito (`implements`/`tests`/`issue`), `located` para lo encontrado
  mecánicamente (vecinos del code index, o un issue que menciona el
  path/key por búsqueda de texto sin link), `verified` SOLO para un link
  `kind="evidences"` — igual que exige el docstring de `evidence.py`.
- **`stale` nunca se recalcula**: se delega a
  `evidence.resolve_link_state()` (vía `matrix()`) y las cuatro razones ya
  existentes (`content_changed`, `symbol_not_found`, `target_missing`,
  `requirement_changed_since_verification`) se traducen UNA vez a un `why`
  corto en español (tabla completa en `docs/api/knowledge.md`); la de
  verificación caducada usa literalmente "evidencia caducada" — la frase
  que la ficha pide y que `tests/test_cmp04_knowledge.py` comprueba.
- **Decisiones**: `project_board.get(issue_id)` para un link declarado
  (`kind="issue"`), y `project_board.list_issues(project_id, q=path)` para
  encontrar issues que MENCIONAN el fichero sin enlace explícito — estas
  últimas SIEMPRE `relation="located"`, `kind="mentions"`, nunca
  presentadas como si fueran una decisión declarada.
- **Código/tests**: `code_index.symbols_in(path)` + `code_index.neighbors`
  para los vecinos estructurales del fichero (profundidad acotada por
  `depth`, clamped a `MAX_DEPTH=3`), todos `relation="located"`. Un nodo se
  tipa `test` en vez de `symbol` por una heurística de ruta
  (`test_`/`/tests/`/`_test.py`), reutilizada tanto para los links
  declarados del requisito como para los vecinos del code index.
- **Selección por unidades, no por caracteres**: cada nodo/arista es una
  unidad semántica completa (un link, un símbolo, un issue) — nunca se
  corta un `why`/`label` a la mitad para caber en un presupuesto; el único
  límite es de CANTIDAD de filas (`MAX_NODES=400`/`MAX_EDGES=800`, mismo
  patrón que `context_engine/wiring.py::MAX_REPORT_ROWS`), nunca de
  caracteres por fila.
- **Ruta**: `GET /api/projects/{project_id}/knowledge/neighborhood`
  (`routes/knowledge_routes.py`, NUEVO), owner-scoped con el mismo
  `_project_or_404` que `requirements_routes.py`/`board_routes.py` ya
  usan; registrada en `app.py` junto a `board_routes`/`requirements_routes`
  (ancla propia, W2-F toca `agent_loop.py` en la región de estrategia, sin
  solape).
- **Recibos de contexto** (pieza relacionada, misma ficha del contrato):
  `src/agent_loop.py` ya compilaba paquetes de contexto y los OBSERVABA
  (`_ce_live_wiring.observe_receipt`) sin exponer nada al metadata
  persistido ni al stream. Este lote añade, en la MISMA región donde ya se
  acumulan `_context_packets_delivered` (edición mínima, ancla propia — W2-F
  toca el bloque de estrategia de `agent_loop.py`, región distinta): un
  acumulador `_context_receipts_summary` que dedup por `(kind, ref)` cada
  `sources` row que `deliver_round`'s report ya trae, un evento SSE
  `context_receipts` (mismo patrón que `web_sources`/`harness_summary`
  existentes) y `metrics["context_receipts"]` (mismo patrón que
  `metrics["harness"]`, por lo que llega al metadata persistido del mensaje
  sin tocar `routes/chat_helpers.py` — `last_metrics` ya se copia entero a
  `md`).
- **Studio**: `studio/src/adapters/knowledge.ts` (NUEVO, el único adaptador
  de esta vista); `studio/src/adapters/chat.ts` gana `ContextReceipt` +
  el caso `context_receipts` en `decode()`; `Transcript.tsx` gana
  `ContextReceiptCard` (tarjeta compacta plegable, "Contexto usado (n) —
  por qué", colapsada por defecto, con enlace cuando `ref` es una URL);
  `Project.tsx` gana `FileNeighborhood` ("Vecindario de un fichero") dentro
  de la pestaña Context, con cuatro listas agrupadas.

## Cobertura

**Presente** (implementado y verificado en este lote):
- `neighborhood()` con las cinco categorías de nodo
  (`requirement`/`decision`/`symbol`/`test`/`run`), `relation` en
  {`declared`,`located`,`verified`} nunca inventada, `stale` delegado
  siempre a `evidence.py`.
- La prueba decisiva exacta de la ficha: requisito cambiado tras verificar
  + código/test sin tocar → arista `evidences` `stale=True` con
  "evidencia caducada" en `why`, `verified=False` en el nodo del
  requisito, `implemented`/`tested` siguen `True`.
- Ruta `GET .../knowledge/neighborhood`, owner-scoped, registrada en
  `app.py`.
- `context_receipts`: acumulado en `agent_loop.py`, persistido en
  `metrics["context_receipts"]`, emitido por SSE, decodificado en
  `chat.ts`, y una tarjeta en `Transcript.tsx` que lo consume — ver
  limitación 1 abajo sobre por qué la tarjeta no se activa todavía en un
  turno en vivo.
- `FileNeighborhood` en la pestaña Context de `Project.tsx`, leyendo por
  `adapters/knowledge.ts`, sin `fetch()` directo.

**Parcial** (limitación conocida, documentada, no bloqueante):
1. **`ContextReceiptCard` no tiene todavía su dato de entrada en vivo.**
   El evento `context_receipts` se decodifica correctamente en
   `adapters/chat.ts` (`ChatEvent`), pero el reductor que convierte cada
   `ChatEvent` en el estado por turno (`Turn`) vive en
   `studio/src/screens/studio/model.ts`, un fichero que NO está en la
   lista de ficheros de W2-D (ni de ningún otro lote de esta ola, por lo
   que se pudo comprobar). `ContextReceiptCard` lee
   `turn.contextReceipts` con un cast defensivo (`unknown`) precisamente
   para no fingir un campo que no existe todavía en `Turn` — hasta que
   `model.ts` añada el campo y el `case 'context_receipts':` en su
   reductor (mismo patrón que ya tiene `case 'web_sources':` → `sources`,
   `Transcript.tsx` línea ~795 de `model.ts`), la tarjeta se queda oculta
   (`receipts.length === 0`) en cualquier build real, exactamente como se
   comporta ante un servidor que no manda el evento. Ver "Puntos a
   cablear" abajo — es la única pieza de este lote que no queda
   end-to-end dentro de su propio alcance de ficheros.
2. **El histórico (recargar una conversación) no muestra la tarjeta.** Por
   la misma razón: `context_receipts` sí queda en `metrics` → metadata
   persistido del mensaje, pero nada en este lote lee
   `message.metadata.context_receipts` al reconstruir el historial (esa
   lectura también vive en `model.ts`/`adapters/chat.ts`'s `historyFrom`-
   equivalente, no tocado aquí). El dato está guardado; falta el mismo
   cableado de lectura histórica que el de arriba.
3. **`run` nodes no se resuelven más allá del id del link.** Un
   `evidences` link solo guarda el `target` como string libre
   (`run_123`); este módulo lo muestra tal cual, sin cruzarlo contra
   `agent_runs.py`/`changeset_store.py` para traer estado/fecha real del
   run — la ficha no lo pedía explícitamente y `evidence.py` tampoco lo
   hace, así que no se inventó ese cruce.
4. **La búsqueda de decisiones por texto (`located`/`mentions`) es
   literal**, vía `project_board.list_issues(q=…)` — no hay ranking
   semántico ni normalización más allá de lo que esa búsqueda ya hacía
   (case-fold simple). Suficiente para el criterio de aceptación
   ("decisiones del tablero enlazadas") pero no pretende ser búsqueda
   inteligente.

**No verificado**: comportamiento con un `code_index` grande y `depth=3`
en un repo real (el test de este lote usa un workspace de dos ficheros);
los topes `MAX_NODES`/`MAX_EDGES` existen mecánicamente pero no se
ejercitaron contra un grafo que realmente los alcance.

## Estado comparativo

**Ventaja propia** en la prueba decisiva concreta (evidencia caducada
nunca reportada como verificada) y en la composición sin duplicar store de
verdad — el informe describe el comportamiento deseado en términos
genéricos ("qué debe cumplir, qué decisiones lo condicionan…"), no una
implementación externa concreta que auditar línea a línea, así que aquí
tampoco hay "paridad demostrada" ni "ventaja externa documentada" que
evaluar: es una **hipótesis de mejora** medida contra los criterios
explícitos de la propia ficha (vecinos tipados, `relation` de tres
valores, `stale` honesto, prueba decisiva), no contra un producto de
terceros.

## Decisión

**Componer**, no duplicar: `neighborhood()` no reimplementa ninguna lógica
de `evidence.py`/`code_index.py`/`project_board.py` — cada nodo/arista es
una traducción directa de algo que esos tres módulos ya calcularon o
almacenaron. **Extender** `agent_loop.py` con el mínimo necesario
(un acumulador + una línea de metadata + un yield SSE) en la MISMA región
donde ya vivía la lógica de recibos de contexto observados, sin tocar el
contrato de `context_engine/` (lectura pura, cero escritura nueva ahí).
**Conservar** el límite "una sola fuente de verdad sobre verificación":
`verified`/`stale` de un requisito siempre proceden de `evidence.matrix()`,
nunca de un cálculo paralelo en este módulo nuevo.

## Pruebas ejecutadas

Desde `/home/claude/faustus`:

```
$ python3 -m pytest tests/test_cmp04_knowledge.py -q -p no:cacheprovider -W ignore
9 passed in 2.62s

$ python3 -m pytest tests/test_cmp04_knowledge_js.py -q -p no:cacheprovider -W ignore
3 passed in 0.82s

$ python3 -m pytest tests -q -p no:cacheprovider -W ignore -k "requirements or knowledge or context_engine or transcript"
572 passed, 2 skipped in 89.14s

$ python3 -m pytest tests/test_agent_loop.py tests/test_context_engine_wiring.py -q -p no:cacheprovider -W ignore
86 passed in 1.16s

$ python3 -m pytest tests/test_studio_guards.py -q -p no:cacheprovider -W ignore
11 passed in 1.12s

$ node studio/checks/knowledge.check.mjs
ok knowledge

$ python3 scripts/i18n_es.py && python3 scripts/i18n_es.py --check
es.ts: 6850 strings
(missing: … — todo de otros lotes en curso en paralelo; ninguna clave de
 este lote aparece en la lista de faltantes)

$ ./node_modules/.bin/tsc --noEmit -p tsconfig.json
(0 errores en los ficheros de este lote — 1 error preexistente en
 studio/src/screens/studio/model.ts, propiedad de otro lote en curso en
 paralelo, no de W2-D)

$ ./node_modules/.bin/vite build
✓ built in 25.21s   (solo el aviso preexistente de tamaño de chunk > 500kB)
```

## Puntos a cablear por el orquestador

1. **`studio/src/screens/studio/model.ts`** (fuera de la lista de ficheros
   de W2-D): añadir `contextReceipts: ContextReceipt[]` a la interfaz
   `Turn` (default `[]`) y, en el reductor de eventos, un caso
   `case 'context_receipts': return { ...turn, contextReceipts: event.receipts };`
   — mismo patrón que el `case 'web_sources':` ya existente en la línea
   ~795 del fichero (`return { ...turn, sources: event.sources, … }`). Sin
   este cambio, `ContextReceiptCard` (`Transcript.tsx`) nunca recibe datos
   en un turno en vivo, aunque el evento SSE ya llega correctamente
   decodificado.
2. **Lectura histórica**: el mismo campo debería poblarse también al
   reconstruir una conversación desde `message.metadata.context_receipts`
   (el dato ya está persistido, ver Cobertura #2) — el punto exacto es
   donde `model.ts`/`adapters/chat.ts` ya leen `metadata.harness` al
   restaurar el historial.
3. **`run` nodes**: si se quiere mostrar estado/fecha real de un run en
   vez de solo su id, cruzar `target` de un link `evidences` contra
   `agent_runs.py`/`changeset_store.py` — fuera del alcance de este lote
   (`evidence.py` tampoco lo hace).
4. **Enlace desde `AlternativesScreen`/`WorkflowsScreen`** (W2-E/W2-G): si
   esas pantallas quieren ofrecer "ver el vecindario de este fichero"
   desde un diff o un nodo de workflow, `adapters/knowledge.ts` ya expone
   `getNeighborhood` listo para reutilizar — no se cableó proactivamente
   para no invadir ficheros de otros lotes.
