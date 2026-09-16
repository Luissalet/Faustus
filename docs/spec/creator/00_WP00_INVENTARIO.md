# WP00 — Inventario contra HEAD real

Baseline: `fa567977a0` (13-09). HEAD auditado: `3a7d4a3d` (`feat/close-the-loop-audit`). Sólo lectura; no se descargaron modelos ni se ejecutó inferencia.

## 1. Magnitud baseline→HEAD

`git log --oneline fa567977a0..HEAD | wc -l` = **132 commits**. `git diff --stat` = **478 archivos, +59 835/-2 996**. El plan Creator (vendorizado en `docs/spec/creator/plan/`) fue escrito contra la baseline; el HEAD añadió subsistemas enteros no contemplados por el plan y que lo tocan directamente:

| Módulo nuevo (HEAD) | Afecta a |
|---|---|
| `src/budget_account.py` + `routes/budget_routes.py` (sqlite bajo `DATA_DIR`, runs/reservations) | WP09 (presupuesto/aprobaciones): ya hay una autoridad de "cuenta de presupuesto" por run; Creator no debe crear una segunda. |
| `src/artifact_store.py` offload (`src/tool_result_offload.py`, `src/artifact_read_tool.py`) | WP03 (artefactos/ArtifactOccurrence): el offload de resultados de tool ya usa `artifact_store` como backend; Creator debe registrar occurrences ahí, no en un store propio. |
| `src/fanout/*` + `routes/fanout_routes.py` | WP29 (orquestación multiagente/paralelismo): fanout ya resuelve scoring, merge y runner paralelo con scope propio; cualquier "producción con especialistas" de Creator debería apoyarse en este runner en vez de crear otro. |
| `src/harness_evolution/*` (sqlite `harness_evolution.db`) | WP41 (o equivalente de auto-mejora/evaluación): patrón de referencia de CAS con sqlite ya probado en producción (ver §3). |
| `src/code_graph/*`, `src/code_mode/*` | Tocan WP31 (concurrencia) tangencialmente vía `code_mode/runner.py` (sandbox de ejecución) pero no son media; no requieren coordinación directa con WP01-10. |
| `src/reach/*` + `routes/reach_routes.py` | Fuera del alcance Creator (fuentes externas de investigación), sin conflicto de archivos. |
| `src/personas/*` | Fuera de alcance Creator directo, pero podría dar perfiles reutilizables para "casting" de voz (WP06) si se decide en fase de producto, no en WP00. |

Ningún módulo nuevo redefine `services/projects.py`, `src/media_runs.py`, `src/media_scheduler.py`, `src/media_workflows.py`, `src/media_capabilities.py`, `src/media_edit_projects.py`, `src/model_capabilities.py`, `src/model_calibration.py`, `src/artifact_identity.py` ni `src/approval_store.py`: siguen siendo las autoridades citadas por el plan. `src/model_load_options.py` (605 líneas) y `src/vram_fit.py` (384 líneas) también existen tal como el plan los referencia.

## 2. Clasificación por capacidad

Estado: **documentada** (el plan la cita) / **localizada** (el archivo/función existe) / **integrada** (tiene ruta HTTP y/o consumidor en Studio) / **probada** (tiene test dedicado). Lo no verificado queda "no localizada".

| Capacidad | Archivo · función | Ruta HTTP | Pantalla Studio | Test | Estado |
|---|---|---|---|---|---|
| Identidad de proyecto | `services/projects.py::ProjectStore` (1648 líneas), file-backed JSON + `atomic_write_text` | — (consumida internamente) | `studio/src/screens/Projects.tsx`, `Project.tsx` | `test_project_identity.py`, `test_idx_project_identity.py` | Probada |
| Marcador corroborativo | `src/project_identity.py` (257 líneas) | — | — | `test_project_identity.py` | Probada |
| Runs de media (outbox) | `src/media_runs.py` (864 líneas) | `/api/media/runs`, `/api/media/runs/{id}`, `/poll`, `/cancel` (`routes/media_routes.py`) | `studio/src/screens/studio/LocalVideo.tsx`, `MediaRecipes.tsx` vía `adapters/activity.ts`, `adapters/media-recipes.ts` | `test_media_runs.py`, `test_media_runs_outbox.py` | Probada e integrada |
| Reconciliación tras reinicio | `src/media_scheduler.py` (65 líneas) | implícito en `/api/media/runs` (poll) | — | `test_media_scheduler.py` | Probada |
| Recetas (workflows tipados) | `src/media_workflows.py::MediaWorkflow.fingerprint()` (603 líneas) | `/api/media/workflows`, `/api/media/plan` | `MediaRecipes.tsx` | `test_media_workflows.py` | Probada e integrada |
| Probes de capacidad multimedia | `src/media_capabilities.py::_run_version, probe_stt` (177 líneas) | no expuesta directamente en routes revisadas | — | `test_media_capabilities.py` | Localizada/probada a nivel unidad; sin ruta HTTP confirmada |
| Editor de imagen no destructivo | `src/media_edit_projects.py::export()` (257 líneas) | `/api/media/edit/projects`, `/layers`, `/ops`, `/export` (`routes/media_edit_routes.py`) | no localizada pantalla dedicada (LocalVideo/Composer no cubren edición de imagen por capas) | `test_p1_media_edit_projects.py` | Integrada y probada en backend; **no localizada** en Studio |
| Subtítulos SRT/VTT | `src/media_subtitles.py` (84 líneas) | no localizada ruta HTTP propia | — | (sin test dedicado propio distinto de los de video local) | Localizada, no integrada por ruta propia confirmada |
| ComfyUI backend | `src/media_backends/comfyui.py`, `src/media_backends/pool.py` | sin ruta propia; se sirve vía `/api/media/*` genérico (`engine` param) | `LocalVideo.tsx`/`MediaRecipes.tsx` indirecto | `tests/test_comfyui_backend.py` | Integrada y probada |
| FFmpeg | dentro de `media_capabilities.probe_ffmpeg` vía `_run_version` | ídem capacidades | — | `test_media_capabilities.py` | Localizada; el propio código NO comprueba `returncode` (ver §4) |
| ASR (STT) | `services/stt/` | no confirmada ruta HTTP en este barrido | — | no localizado test específico en este barrido | Localizada, resto no localizado |
| TTS | `services/tts/` | no confirmada ruta HTTP en este barrido | — | no localizado test específico | Localizada, resto no localizado |
| Música/vídeo generativo dedicados (ACE-Step, Wan, LTX) | no localizado ningún módulo `src/*music*`, `src/*wan*`, `src/*ltx*` | — | — | — | No localizada (confirma gap del plan; son introducción candidata real) |
| Vocabulario de modelos | `src/model_capabilities.py` (1277 líneas) | no confirmada ruta HTTP directa en este barrido | `cookbook/Models.tsx`, `ModelPicker.tsx`, `ModelPalette.tsx` vía `adapters/cookbook.ts` | `test_model_capabilities.py` | Integrada y probada |
| Calibración por digest | `src/model_calibration.py::manifest_key()` (606 líneas) | — | — | `test_model_calibration.py` | Probada; confirma clave `ollama\|digest:<digest>` (§4) |
| Opciones de carga de modelo | `src/model_load_options.py` (605 líneas) | — | `settings/LocalModels.tsx` | `test_model_load_options.py`, `test_model_load_options_extra.py` | Probada e integrada |
| Ajuste VRAM | `src/vram_fit.py` (384 líneas) | — | `ModelPicker.tsx`, `VramAdmissionDialog.tsx` | `test_vram_fit.py`, `test_model_picker_vram_fit.py` | Probada e integrada |
| Admisión de recursos (pools/leases) | `src/resource_admission.py::acquire()` (383 líneas) | usada por `routes/ops_routes.py`, `src/workflow_cost_estimate.py` | — | **no localizado test dedicado** (`tests/test_resource_admission*.py` no existe) | Localizada e integrada parcialmente; **sin test propio**; docstring de `acquire()` dice explícitamente *"out of scope: nothing wires to this gate yet"* — confirma que el propio código declara la integración con el lock global como pendiente |
| GPU (placement/policy/topology/shared memory) | `src/gpu_placement.py`, `src/gpu_policy.py`, `src/gpu_topology.py`, `src/gpu_shared_memory.py` | usados por `ops_routes.py` (no auditado en detalle) | `settings/LocalModels.tsx` probablemente | tests existentes con prefijo `gpu_` (no enumerados exhaustivamente aquí) | Localizada |
| Artefactos | `src/artifact_store.py` (433 líneas), `src/artifact_identity.py` (994 líneas) | consumidos por `src/artifact_read_tool.py`, `src/tool_result_offload.py` | `Library.tsx` (galería) | `test_artifact_store.py`, `test_artifact_identity.py`, `test_l29_artifact_store_call_id.py` | Probada e integrada; ahora también autoridad del offload de resultados de tool (nuevo desde baseline) |
| Workflows durables | `src/workflows/{engine,scheduler,store,runtime,simulate}.py` | rutas bajo `routes/` (no listadas aquí completas) | `screens/workflows/WorkflowsScreen.tsx`, `PlanGraph.tsx`, `RunOverlay.tsx`, `NodeInspector.tsx` | tests dispersos con prefijo `workflow` | Integrada y probada |
| Aprobaciones | `src/approval_store.py` (448 líneas) | `/api/approvals/pending`, `/active`, `/request`, `/check`, `/{id}/grant`, `/{id}/deny` (`routes/approvals_routes.py`) | `activity/`, `Home.tsx` vía `adapters/approvals.ts`, `adapters/activity.ts` | `test_approval_store.py` | Probada e integrada |
| Presupuesto por run | `src/budget_account.py` (368 líneas, nuevo desde baseline) | `/api/runs/{run_id}/budget` (`routes/budget_routes.py`) | no localizada pantalla dedicada | `test_budget_account.py` | Probada e integrada en backend; **no localizada** en Studio |
| Plugins/MCP | `src/mcp_manager.py`, `src/builtin_mcp.py`, `routes/mcp_routes.py`, `mcp_servers/` | `/api/mcp/*` | `screens/Settings.tsx` (sección MCP, no confirmada por archivo dedicado) | ~25 archivos `test_*mcp*.py` | Probada e integrada; no se localizó un `PluginManager` de plugins de terceros con manifest/sandbox equivalente al citado (`chat-on-steroids`) — el modelo actual es MCP puro |
| Exportación | no se localizó un endpoint `/api/creator/exports` ni equivalente genérico de "export audiovisual"; existe `export()` de imagen (`media_edit_projects.py`) y exportadores SRT/VTT (`media_subtitles.py`) | parcial | — | parcial | Parcial: exportación por dominio sí, exportación unificada de producción no localizada |

## 3. Mapa de autoridades a conservar (reverificado)

| Área | Autoridad confirmada en HEAD |
|---|---|
| Proyecto | `services/projects.py::ProjectStore` (file-backed JSON, `atomic_write_text`); `src/project_identity.py` sigue siendo marcador corroborativo, no autoridad paralela. |
| Artefacto | `src/artifact_store.py` + `src/artifact_identity.py`; ahora también backend del offload de resultados de tool grandes (`src/tool_result_offload.py`), lo que refuerza — no debilita — su rol de autoridad única de bytes. |
| Run (efecto de ejecución multimedia) | `src/media_runs.py` sigue siendo la única autoridad de estado de un run multimedia; `src/media_scheduler.py` sólo reconcilia, no reenvía grafos. |
| Aprobación | `src/approval_store.py`. Coexiste con la nueva `src/budget_account.py`, que es autoridad de **presupuesto** (tokens/gasto por run), no de aprobación: son responsabilidades distintas y no deben fusionarse. |
| Modelo/deployment | `src/model_capabilities.py` (vocabulario), `src/model_calibration.py` (evidencia por digest/deployment), `src/model_load_options.py` (opciones aplicadas), `src/vram_fit.py` (ajuste de recursos). Ninguno fue sustituido; siguen siendo cuatro capas separadas tal como describe el plan. |
| Admisión de recursos | `src/resource_admission.py` (pools/leases) sigue siendo la autoridad declarada, pero su propio docstring confirma que aún no está conectada al `single global lock` de `llm_core._gate_workload`. Cualquier Creator que dependa de "paralelismo real" en GPU debe tratar esto como no resuelto, no como ya integrado. |

## 4. Observaciones estáticas reverificadas

| Observación del plan | Sigue vigente en HEAD |
|---|---|
| `fingerprint()` de `MediaWorkflow` no incluye `models`, `requires_nodes` ni `outputs` | **Sí, sigue igual.** Campos actuales: `id, version, engine, inputs, computed, graph, requires_consent, consent_subject_input` (línea 178-185 de `src/media_workflows.py`). |
| `_run_version()` no comprueba `returncode` antes de declarar éxito | **Sí, sigue igual.** La función sólo distingue `TimeoutExpired`/`OSError`; cualquier proceso que termine (incluso con código de error) se declara `installed=True` (líneas 47-63 de `src/media_capabilities.py`). |
| Orden de composición del exportador de imagen (base del historial, capas después) | **Sin cambio observable** en la firma/docstring de `export()`; sigue componiendo sobre una copia y nunca sobre el original salvo flag explícito. No se verificó el detalle interno del orden capa-por-capa más allá del docstring. |
| Identidad de calibración por digest de Ollama | **Sí, sigue igual.** `manifest_key()` usa `f"ollama|digest:{digest}"` cuando hay digest (línea 154 de `src/model_calibration.py`). |
| Docstring de `resource_admission.py` sobre integración pendiente con el lock global | **Confirmado y ahora más explícito.** El docstring de `acquire()` dice literalmente *"out of scope: nothing wires to this gate yet"* (líneas 270-273). Los únicos consumidores localizados son `src/workflow_cost_estimate.py` y `routes/ops_routes.py`, ninguno de los cuales es el pipeline de generación multimedia real. La observación del baseline se confirma, no se refuta. |

## 5. Seam de almacenamiento recomendado para documentos Creator

Dos patrones conviven en el HEAD:

1. **File-backed + JSON atómico** (`services/projects.py`): adecuado para un registro pequeño, de baja concurrencia, sin necesidad de consultas relacionales.
2. **CAS con sqlite bajo `DATA_DIR`** (`src/budget_account.py`, `src/harness_evolution/store.py`): un archivo `.sqlite3`/`.db` por dominio, `sqlite3.Row`, `CREATE TABLE IF NOT EXISTS`, `timeout=` en la conexión para colas de escritura, y (en `budget_account.py`) un comentario explícito de diseño: *"DATA_DIR rather than a table bolted onto core/database.py"*.

**Recomendación:** los documentos Creator (`CreatorDocument`: canvas, timeline, transcript, song, storyboard) deben usar el patrón (2), replicando `harness_evolution/store.py` como plantilla: un `creator_documents.sqlite3` bajo `DATA_DIR`, con tabla de documentos (id, project_id, schema_version, revision, content, updated_at) y tabla de comandos aplicados (command_id, expected_revision) para deduplicación/concurrencia optimista, tal como pide `03_ARQUITECTURA_Y_CONTRATOS.md`. El patrón (1) de `projects.py` no ofrece transacciones ni `expected_revision` atómico sin reimplementar locking manual, y el propio plan pide "transacciones pequeñas" — sqlite ya resuelve esto en dos módulos nuevos del HEAD, por lo que no es una elección nueva sino continuar un patrón ya en producción.

## 6. Orden recomendado WP01–WP10 dado el HEAD real

| Orden | WP | Write set probable (archivos nuevos, no tocar autoridades) | Conflictos con otros WP |
|---|---|---|---|
| 1 | WP01 (CreatorDocument + store) | `src/creator/documents.py`, `src/creator/store.py` (nuevo, patrón sqlite §5), `routes/creator_document_routes.py` | Ninguno; base para el resto. |
| 2 | WP02 (CreatorProfile sobre ProjectStore) | `src/creator/profile.py` (lee/escribe vía `ProjectStore.context_items`, no nueva tabla de proyectos) | Debe coordinarse con cualquier lot que toque `services/projects.py` directamente (ninguno detectado en `lot/*` actuales salvo posibles cambios de contexto). |
| 3 | WP03 (ArtifactOccurrence + relaciones derived_from) | `src/artifact_store.py` (extensión aditiva), `src/creator/occurrences.py` | Alto: `src/artifact_store.py` también es tocado por el offload de tools (`tool_result_offload.py`, `artifact_read_tool.py`); cualquier cambio debe ser aditivo y no debe repetirse en dos lotes a la vez — un único integrador para este archivo. |
| 4 | WP04 (Puerto de adapters: describe/plan/submit/status/cancel/collect/reconcile) | `src/creator/adapter_port.py`, `src/media_backends/comfyui.py` (adaptar a interfaz nueva sin romper firma actual) | Medio: comparte `src/media_backends/comfyui.py` y `src/media_runs.py` con cualquier trabajo de paralelismo (fanout) que decida usar media runs; coordinar con quien toque `src/fanout/runner.py`. |
| 5 | WP05 (ProductionPlan → compilación a `workflows/`) | `src/creator/production_plan.py`, extensión de `src/workflows/engine.py` (aditiva) | Alto: `src/workflows/engine.py` es núcleo compartido; un solo lote debe tocarlo por ciclo. |
| 6 | WP06 (Voice/TTS casting) | `services/tts/` (extensión), `src/creator/voice.py` | Bajo; aislado si no se toca `media_capabilities.py`. |
| 7 | WP07 (Subtítulos multihablante) | `src/media_subtitles.py` (versión editorial nueva, sin romper exportadores SRT/VTT legacy) | Medio: mismo archivo que cualquier fix de subtítulos en curso; verificar `lot/*` activos antes de tocar. |
| 8 | WP08 (Model Explorer / evidencia por campo) | `src/model_capabilities.py` (lectura only + nuevo `src/creator/model_explorer.py`), pantalla `cookbook/Models.tsx` | Alto: `model_capabilities.py` es 1277 líneas y probablemente objeto de otros lotes de paridad; confirmar con `git log -- src/model_capabilities.py` antes de asignar en paralelo. |
| 9 | WP09 (Presupuesto/aprobación de producción) | usa `src/budget_account.py` y `src/approval_store.py` tal cual (no reescribir); sólo `src/creator/budget_policy.py` nuevo | Bajo si es sólo consumidor; alto si alguien más tiene intención de modificar `budget_account.py` en el mismo ciclo — coordinar. |
| 10 | WP10 (Studio: pantalla Creator unificada) | `studio/src/screens/creator/*` (nuevo), `studio/src/adapters/creator.ts` (nuevo) | Bajo; namespace nuevo, sin colisión con `screens/studio/*` existentes salvo si se decide extender `Composer.tsx`/`LocalVideo.tsx` en vez de crear pantalla nueva (a decidir en WP10, no aquí). |

Regla de reparto: WP01, WP02, WP06, WP10 son disjuntos entre sí y paralelizables de inmediato. WP03, WP05 y WP08 tocan archivos núcleo compartidos (`artifact_store.py`, `workflows/engine.py`, `model_capabilities.py`) y requieren un integrador único por archivo, tal como exige `README_START_HERE.md`. WP04, WP07 y WP09 dependen de que WP01/WP03 hayan fijado sus contratos primero.

## 7. Limitaciones de este inventario

No se ejecutó la aplicación, su suite ni inferencia. Las rutas HTTP se localizaron por grep de decoradores `@router.*`, no por arranque del servidor. "No localizada" en la tabla de §2 (música/vídeo dedicados, pantalla de edición de imagen, pantalla de presupuesto, exportación unificada) son gaps reales frente al plan, no errores de búsqueda, pero una búsqueda más amplia (p. ej. rutas registradas dinámicamente) podría revelar integraciones no capturadas por grep estático.
