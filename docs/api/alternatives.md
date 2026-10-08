# Alternativas aisladas comparables (CMP-13, W2-G) — `/api/projects/{project_id}/alternatives/*`

Un "experimento" agrupa un `goal`, un `base_ref` compartido y N
`alternatives`, cada una en su propio aislamiento — un `git worktree`, una
copia congelada de directorio, o una instantánea de texto para un solo
documento — para que dos alternativas y la copia principal del usuario
puedan tener contenido sin confirmar DISTINTO en los mismos ficheros al
mismo tiempo, sin pisarse.

Implementación: `src/alternatives.py` (modelo + tres-vías + aislamiento),
`src/git_panel.py::worktree_add/worktree_remove/worktree_list` (el
aislamiento `worktree`), `routes/alternatives_routes.py` (rutas),
`src/agent_tools/alternatives_tools.py` (`alt_start`/`alt_compare`/
`alt_apply`), `studio/src/screens/alternatives/{AlternativesScreen,
CompareView}.tsx` + `studio/src/adapters/alternatives.ts` (UI).

La regla que no se negocia (INFORME §3.12): **`apply`/`combine` nunca
destruyen una edición manual hecha en la copia principal después de crear el
experimento, y nunca mezclan una alternativa que no se pidió.** Cada fichero
tocado se fusiona a tres vías (base / copia principal ahora / la
alternativa elegida) con `git merge-file` — el propio algoritmo de fusión de
git, no una reimplementación — y si CUALQUIER fichero produce un conflicto,
el `apply`/`combine` ENTERO se aborta antes de escribir un solo byte
(`src/alternatives.py::_build_plans` calcula todos los planes primero; solo
si ninguno es un conflicto se escribe algo). Ver el test decisivo abajo.

## Aislamiento

| `isolation`     | Cuándo | Mecanismo |
|---|---|---|
| `worktree`       | `workspace` está dentro de un repo git (`git_panel.repo_toplevel`) | `git worktree add` en `DATA_DIR/alternatives/<exp>/<alt>`, HEAD desacoplado en `base_ref` — comparte el object store del repo, así que es barato y cualquier commit que la alternativa haga dentro sigue siendo alcanzable mientras el worktree exista. |
| `snapshot_dir`   | `workspace` es un directorio normal, sin git | Copia (`shutil.copytree`) de una instantánea base congelada en `DATA_DIR/alternatives/<exp>/_base/`, tomada UNA vez al crear el experimento (sin git no hay otra forma de recuperar "cómo era al principio" una vez el directorio sigue cambiando). |
| `doc_version`    | Un solo documento, sin workspace de fichero alguno | Mientras se edita: texto guardado en el propio registro del experimento (`create_doc_experiment`, `set_doc_version_content`) — la MISMA garantía de aislamiento que `worktree`/`snapshot_dir` dan a un workspace de fichero, nunca visible como contenido vivo del documento hasta que se aplica. **W3-D**: cuando `create_doc_experiment` recibe `document_id`, `apply_alternative` SÍ escribe el resultado fusionado en el almacén real de Studio (`core.database.Document`/`DocumentVersion`) — ver «Cableado a `core.database.Document`» abajo. |

Hooks de repo (`.git/hooks`) **nunca** se ejecutan al crear un worktree:
`git_panel.worktree_add` pasa `-c core.hooksPath=` (vacío) en el propio
comando de `git worktree add`, en vez de `--no-checkout` + un `checkout`
manual — un único proceso `git`, y cualquier hook (incluido
`post-checkout`) resuelve a un directorio que no existe. Documentado y
elegido explícitamente, per el contrato de este lote.

## Tres vías (`base`/`mine`/`theirs`) por fichero

Para cada fichero que la alternativa cambió respecto a `base_ref`
(`src/alternatives.py::_alt_changed_files`), el plan es:

* `theirs == mine` → nada que hacer (ya coincide).
* `mine == base` (el usuario no ha tocado el fichero) → toma la versión de
  la alternativa tal cual, incluida una eliminación (fast-forward).
* `theirs == base` (la alternativa en realidad no cambió este fichero —
  apareció en el conjunto solo porque OTRA alternativa sí lo tocó) → nada
  que traer.
* `theirs is None` y `mine` cambió, o `mine is None` y `theirs` cambió →
  **conflicto** (borrado contra modificación) — nunca se adivina.
* Ambos cambiaron, ninguno de los casos anteriores → `git merge-file -p
  --diff3 mine base theirs`; `returncode == 0` → fusión limpia,
  `returncode == 1` (o mayor) → **conflicto**.

`apply_alternative(owner, exp_id, alt_id)` mezcla UNA alternativa entera;
`combine(owner, exp_id, {ruta: alt_id, ...})` mezcla, POR FICHERO, la
alternativa elegida para cada uno — misma garantía de todo-o-nada, solo que
repartida por fichero en vez de por alternativa completa.

## `compare(owner, exp_id)`

Diff de cada alternativa contra `base_ref` (`diff_summary`:
`files_changed`/`additions`/`deletions`, y por fichero `change ∈
{added,modified,deleted,none}`), más `contested_files`: qué rutas toca MÁS
DE UNA alternativa — el conjunto que un `apply`/`combine` posterior
necesitará fusionar o que colisionará si se combinan sobre el mismo
fichero. **Alcance**: `compare` es "cada alternativa contra la base y qué ficheros
se disputan"; la diferencia de una alternativa contra OTRA es
`compare_pair`, justo debajo. Un fichero que la alternativa creó sin
`git add` cuenta como `added` (antes `git diff <base>` no lo veía), y un
renombrado se informa como borrado + alta con rutas reales (con `-M` la
clave salía como `old => new`, que no nombra ningún fichero que `apply`
pueda escribir).

## `compare_pair(owner, exp_id, alt_a, alt_b, *, max_file_bytes, max_diff_lines, max_files)` (OBJ-47)

Una alternativa contra otra, fichero a fichero, en sentido **A → B**:
`added` existe solo en B, `removed` solo en A, `renamed` se movió (ruta en B,
`old_path` en A), `changed` difiere. Los ficheros idénticos en ambas no se
listan (se cuentan en `summary.identical_files`). Solo lectura: no escribe
nada, ni siquiera el registro del experimento.

* **Diff**: `diff` es un diff unificado (`--- a/<ruta>` / `+++ b/<ruta>`,
  `/dev/null` en el lado ausente; una última línea sin salto lleva
  `\ No newline at end of file`). `additions`/`deletions` cuentan TODO el
  diff, no solo la parte conservada.
* **Binarios y grandes**: un fichero con NUL, UTF-8 inválido o más de
  `max_file_bytes` (por defecto 512 000) se lista con `binary` / `too_large`,
  sin diff y con `additions`/`deletions` a `null`; nunca se diffa como vacío.
* **Topes, siempre declarados** (`limits` y `truncation`): `max_diff_lines`
  por fichero (1 500; `diff_truncated`, `omitted_lines`, `diff_total_lines`),
  `max_files` (400; `truncation.files`, `files_omitted`) y un presupuesto
  total de 600 000 caracteres de diff (`diff_budget_exhausted`). Los
  conflictos van primero, así que un tope nunca esconde uno.
* **Renombrados**: una ruta solo de A y otra solo de B son un renombrado si
  su contenido es idéntico o, en texto pequeño, se parece al menos un 60 %.
* **Solape contra la base** (`touched`, y por fichero `touched_by`,
  `base_change`, `overlap`): para lo que AMBAS tocaron, `identical` (misma
  edición), `mergeable` (fusión a tres bandas limpia) o `conflict` - el mismo
  `git merge-file` que usa `apply`, así que la vista previa y la fusión real
  no pueden discrepar. `unknown` solo si git no está.
* Errores: la misma alternativa dos veces o mezclar una alternativa de
  documento con una de ficheros es `400 alternatives.invalid_request`; una
  alternativa inexistente, `404 alternatives.alt_not_found`.

## Cableado a `core.database.Document` (W3-D, CMP-13 seguimiento)

`docs/adaptations/decisions/CMP-13.md` dejó esto declarado como alcance
pendiente: *"`doc_version` cableado al almacén real de documentos de
Studio | ausente"*. `src/alternatives.py` lo cierra sin tocar ningún otro
fichero de este módulo más allá de las funciones `doc_version`:

* `create_doc_experiment(owner, project_id, goal, base_content, *,
  document_id=None)` — con `document_id`, el texto base del experimento es
  el `current_content` LIVE de ese `Document` (owner-scoped, nunca el
  `base_content` que el caller haya pasado a la vez, que podría estar
  obsoleto); sin `document_id`, se comporta exactamente igual que antes
  (texto autónomo, `apply_alternative` nunca escribe en ningún sitio).
* `apply_alternative`: cuando la alternativa es `doc_version` y el
  experimento tiene `document_id`, `mine_doc_content` (si el caller no lo
  pasa explícitamente) se lee del documento LIVE en vez de la instantánea
  de creación — la misma regla "mine es siempre el contenido actual" que
  ya rige para `worktree`/`snapshot_dir`. Una vez el plan de fusión está
  limpio (nunca si hay conflicto: `ApplyConflictError` no escribe nada, ni
  en el JSON del experimento ni en el documento real), el resultado se
  escribe como una `DocumentVersion` NUEVA e inmutable — el mismo mecanismo
  que usa cualquier otra edición (`src/document_comments.py
  ::_apply_document_edit`: `version_count` incrementado + una fila nueva),
  nunca una segunda fuente de verdad — etiquetada `source="alternative:
  <exp_id>"` y con `summary` nombrando la alternativa elegida
  ("procedencia del fragmento elegido" queda consultable, no solo en un
  comentario). La respuesta añade `document_version: {document_id,
  version_number, unchanged}`.
* Owner-scoped igual que el resto del módulo: un `document_id` que no
  existe o pertenece a otro owner responde `alternatives.document_not_found`
  (mapeado a 400 genérico por `_alt_error` — ese error_class no tiene un
  código propio en la tabla de `_alt_error`, así que cae en el `else 400`
  general, igual que cualquier otro `alternatives.*` no listado).

### Cableado en las rutas (W3-INT)

`routes/alternatives_routes.py`'s `POST /doc` acepta ahora `document_id`
opcional en el cuerpo (`{goal, base_content, document_id?}`) y lo pasa tal
cual a `alternatives.create_doc_experiment(..., document_id=...)` — la
función ya lo aceptaba y lo probaba end-to-end a nivel de módulo
(`tests/test_w3d_doc_alternatives.py`); ese fichero suma ahora un par de
pruebas por `TestClient` contra el router real que confirman el paso a
través de la ruta HTTP. `apply`/`combine` ya recorrían el camino que
escribe `DocumentVersion` desde que el experimento tiene `document_id` (ver
arriba) — no necesitaron ningún cambio de ruta adicional. `AlternativesScreen.tsx`
sigue sin una UI de creación propia para `doc_version` (ver «Límites
conocidos» abajo): la ruta está cableada, la pantalla no.

## Rutas

Todas bajo `/api/projects/{project_id}/alternatives`. Lectura
`require_user`; toda mutación `require_human` — igual que
`routes/git_routes.py`, cuyo propio docstring explica por qué crear un
worktree, ejecutar un comando dentro de uno, y sobre todo escribir en la
copia principal del usuario son la misma clase de sorpresa que
`require_human` ya existe para frenar.

| Método y ruta | Qué hace |
|---|---|
| `GET /` | Lista los experimentos del proyecto (del owner). |
| `POST /` | `{goal, workspace?}` — crea un experimento; `workspace` por defecto es el del proyecto. |
| `POST /doc` | `{goal, base_content, document_id?}` — experimento `doc_version`, sin workspace de fichero; con `document_id` (W3-INT), el texto base es el `current_content` LIVE de ese documento del owner (ver «Cableado a `core.database.Document`» arriba). |
| `GET /{exp_id}` | Un experimento completo. |
| `DELETE /{exp_id}` | Borra el experimento y limpia sus worktrees/directorios. |
| `GET /{exp_id}/compare` | Ver arriba. |
| `GET /{exp_id}/compare/{alt_a}/{alt_b}` | `?max_file_bytes&max_diff_lines&max_files` - `compare_pair`, ver arriba. Solo lectura (`require_user`). |
| `POST /{exp_id}/alternatives` | `{label, isolation?}` — añade una alternativa. |
| `PUT /{exp_id}/alternatives/{alt_id}/content` | `{content}` — solo `doc_version`. |
| `POST /{exp_id}/alternatives/{alt_id}/tests` | `{command, timeout?}` — corre `command` (siempre `shlex.split`, nunca shell) dentro del propio directorio aislado de la alternativa. |
| `POST /{exp_id}/apply/{alt_id}` | `{mine_doc_content?}` — fusiona la alternativa entera. |
| `POST /{exp_id}/combine` | `{choices: {ruta: alt_id}, mine_doc_content?}` — fusiona por fichero. |

Errores: `error_class` en el nivel superior del cuerpo (nunca anidado bajo
`detail`), copiado del propio `_error()` de `routes/git_routes.py`/
`routes/board_routes.py`. Un conflicto de `apply`/`combine` responde `409
alternatives.apply_conflict` con `"conflicts": [ruta, ...]`.

## Herramientas del agente

`alt_start`/`alt_compare`/`alt_apply` — ejecutores finos sobre este módulo,
registrados como `git_tools` en los seis ficheros de catálogo
(`agent_tools/__init__.py`, `tool_schemas.py`, `tool_capabilities.py`,
`tool_index.py`, `tool_index_examples.py`, `tool_security.py`; misma clase
de privilegio que `git_*` en `NON_ADMIN_BLOCKED_TOOLS`).

`alt_apply` exige aprobación humana explícita — EXACTAMENTE el mismo
mecanismo que `git_merge` (`src/agent_tools/git_tools.py::_human_approved`,
reimplementado idéntico aquí en vez de inventar un segundo sistema de
aprobaciones): o bien `ctx["human_approved"]` (una tarjeta de aprobación ya
sellada para esta llamada exacta), o bien `args["user_confirmed"] == true`
después de que el modelo haya preguntado y el usuario haya dicho que sí.
Sin ninguna de las dos, la llamada se rechaza con `error_class
alternatives.approval_required`. `alt_start`/`alt_compare` no están
cableados a esa puerta: crear un aislamiento y comparar no lee nada fuera
de sí mismo ni escribe nada que el usuario no pidiera ya al invocar la
herramienta.

## Coste

Cada alternativa nace con `cost: {"known_usd": "unknown", "unestimable":
[...]}` — regla transversal del contrato ("precio desconocido = unknown",
nunca cero). Este lote no cablea seguimiento real de coste de LLM (eso es
CMP-08/W2-B); lo que SÍ hace es no fingir un número que nadie midió.

## Prueba decisiva (INFORME §3.12)

`tests/test_cmp13_alternatives.py::test_apply_conflict_never_destroys_manual_edit_or_mixes_other_alternative`:
dos alternativas cambian la MISMA línea del mismo fichero; mientras tanto el
usuario edita esa misma línea en la copia principal. Aplicar CUALQUIERA de
las dos alternativas se rechaza (`ApplyConflictError`, nada se escribe) y la
edición manual del usuario sobrevive byte a byte — comprobado después de
intentar aplicar la primera alternativa, y otra vez después de intentar la
segunda, para probar que ningún intento mezcló nada.

## Límites conocidos / pendiente de cablear

* `doc_version` SÍ está integrado con el almacén real de documentos de
  Studio desde W3-D (ver «Cableado a `core.database.Document`» arriba), y
  desde W3-INT la ruta `POST /doc` también acepta `document_id` en su
  cuerpo (ver «Cableado en las rutas» arriba) — solo falta
  `AlternativesScreen.tsx`: no tiene UI de creación propia para
  `doc_version` (solo `worktree`/`snapshot_dir` desde un workspace) —
  `set_doc_version_content` y `apply_alternative` están probados end-to-end
  por el backend y ahora también la ruta `POST /doc`, listos para que un
  lote de documentos/pantalla los cablee a una pantalla.
* `compare_pair` compara de dos en dos; no hay una matriz de las N alternativas
  entre sí (sería O(n²) sin que decidir qué aplicar lo necesite). El agente
  (`alt_compare`) sigue usando `compare`.
* No hay seguimiento de coste real de ejecución (`run_id` queda `None`
  salvo que un futuro cableado del motor de agentes lo rellene).
* `combine` (fusión por fichero) no tiene el mismo cableado a
  `core.database.Document` que `apply_alternative` — su bucle de escritura
  asume `exp["workspace"]` (una ruta de fichero), que un experimento
  `doc_version` no tiene; en la práctica `combine` no se ha usado nunca con
  una alternativa `doc_version` (solo hay UNA entrada posible, `"(document)"`),
  así que no es una regresión de este lote, pero queda como hueco conocido
  para quien cablee `combine` a documentos reales.
* `src/branching_futures/isolation.py` (W3-D) añade `WorktreeIsolator`/
  `SnapshotDirIsolator` reales sobre el mismo `git_panel.worktree_*` que
  este módulo usa, e `isolator_for(workspace)`/`BranchingService(isolator=
  ...)` para inyectarlos — pero `branching_futures.service.py::create`/
  `start_branch` todavía NO llaman a `fork`/`inspect`/`cleanup`: siguen
  tratando `namespace` como una etiqueta lógica, no un aislamiento real.
  Cablear eso es trabajo futuro documentado, no código sin probar en este
  lote (ver `tests/test_w3d_isolators.py`, que prueba los isoladores en
  aislamiento, no un futuro fork real).
