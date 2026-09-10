# Estado de los 48 escenarios QA como regresiones permanentes

Generado a mano (Lote 11) a partir de `docs/spec/v2/acceptance_scenarios.json` y
coherente con el marker `qa_state` que cada fichero en `tests/qa/` declara —
`tests/test_qa_index.py` compara esta tabla contra esos markers y falla si
alguna fila se desincroniza. **verde**: el comportamiento existe y el test lo
prueba contra el codigo real. **xfail**: el mecanismo no existe todavia
(`xfail(strict=True)`, con el motivo en el propio test). **manual**: requiere
hardware/navegador real (microfono, zoom interactivo) y no puede ejecutarse en
pytest; el test esta `skip` con los pasos manuales documentados.

Resumen: **44 verde**, **3 xfail**, **1 manual** — 48 de 48.

| ID | Estado | Test | Que falta |
|---|---|---|---|
| QA-01 | verde | test_qa_01_carpeta_sin_git.py | — |
| QA-02 | verde | test_qa_02_proyecto_grande.py | L38: `src/code_index.py` cierra IDX-02/IDX-03 - indice incremental (hash por fichero) con `find_definition`/`find_callers`/`tests_for`, que respeta `.gitignore`/`.faustusignore` y excluye vendor/dist/build/binarios/>2MB sin abrirlos (probado con spy sobre `open`) |
| QA-03 | verde | test_qa_03_diseno_ambiguo.py | — |
| QA-04 | verde | test_qa_04_no_preguntar_lo_obvio.py | L40: guion e2e con `tests.eval.harness.EvalApp` (fake_llm) contra un servidor y una ruta reales prueba que una sustitucion de texto exacto ejecuta `edit_file` directamente y nunca dispara `ask_user`; no prueba que un modelo real, sin guion, elegiria igual (esa decision vive en el prompt, fuera del PROPIOS de este lote) |
| QA-05 | verde | test_qa_05_json_utf8_fragmentados.py | — |
| QA-06 | verde | test_qa_06_tool_falsa_en_texto.py | — |
| QA-07 | verde | test_qa_07_schema_peligroso.py | — |
| QA-08 | verde | test_qa_08_reenvio_de_mensaje.py | — |
| QA-09 | verde | test_qa_09_replay_de_stream.py | — |
| QA-10 | xfail | test_qa_10_reinicio_en_research.py | Resume por rondas/secciones confirmadas (RES-05); solo hay recover_interrupted + reintento desde cero |
| QA-11 | verde | test_qa_11_efecto_remoto_incierto.py | — |
| QA-12 | verde | test_qa_12_cancelar_proceso.py | — |
| QA-13 | verde | test_qa_13_pregunta_abandonada.py | — |
| QA-14 | verde | test_qa_14_respuesta_vieja.py | — |
| QA-15 | verde | test_qa_15_edicion_concurrente.py | — |
| QA-16 | verde | test_qa_16_dos_agentes_escritores.py | — |
| QA-17 | verde | test_qa_17_lote_parcialmente_aplicado.py | — |
| QA-18 | verde | test_qa_18_rollback_fuera_de_alcance.py | — |
| QA-19 | verde | test_qa_19_no_todo_verde_es_correcto.py | — |
| QA-20 | verde | test_qa_20_fallo_preexistente.py | — |
| QA-21 | verde | test_qa_21_citas_vacias.py | — |
| QA-22 | verde | test_qa_22_brief_largo.py | — |
| QA-23 | verde | test_qa_23_buscadores_degradados.py | — |
| QA-24 | verde | test_qa_24_modelos_simultaneos.py | — |
| QA-25 | verde | test_qa_25_kv_desconocida.py | — |
| QA-26 | verde | test_qa_26_presion_de_commit.py | — |
| QA-27 | xfail | test_qa_27_caida_de_nodo.py | remote_worker no es un backend implementado (HW-06) |
| QA-28 | verde | test_qa_28_cambio_de_modelo.py | L40: `src/agent_loop.py::recompute_capabilities_on_model_switch` + `src/model_calibration.py::diff` (nuevos) recalculan capacidades cuando el modelo que responde cambia a mitad de tarea (fallback en la misma llamada) y emiten `capabilities_changed` con `lost`; `_resolve_tool_blocks`/`_render_tool_result_content` ya reconstruian nativo->fence y "imagen descrita, no fingida" por-ronda (verificado, no reescrito). El cambio de modelo ENTRE turnos ya lo cerraba `routes/session_routes.py`'s `PATCH /session/{sid}` (lote 20) |
| QA-29 | verde | test_qa_29_privacidad_transitiva.py | L41: `src/privacy_policy.py` cierra el escenario para embeddings/chroma/resumidor remoto; el reranker real vive en `src/rerank.py` (fichero ajeno a L41, ver su informe) y sigue sin llamar a `assert_outbound` |
| QA-30 | verde | test_qa_30_inyeccion_en_documento.py | — |
| QA-31 | verde | test_qa_31_ssrf_y_redireccion.py | — |
| QA-32 | verde | test_qa_32_preview_malicioso.py | — |
| QA-33 | verde | test_qa_33_aislamiento_de_dueno.py | — |
| QA-34 | verde | test_qa_34_olvido_e_incognito.py | — |
| QA-35 | verde | test_qa_35_borrador_persistente.py | L29: attachments del composer persistidos y restaurados por sesion (readAttachmentsFor/writeAttachmentsFor, Studio.tsx), excluyendo incognito (UX-01) |
| QA-36 | verde | test_qa_36_regenerar_con_efectos.py | L40: `POST /api/chat/regenerate/{sid}` (routes/chat_routes.py, nuevo) reinyecta la evidencia (`tool_events`) del turno anterior como contexto y bloquea toda herramienta no probadamente de solo lectura para esa llamada (reusa `src.tool_security.PLAN_MODE_READONLY_TOOLS`, la misma autoridad de modo plan); el mensaje guardado lleva `regenerated_from` |
| QA-37 | verde | test_qa_37_lectura_bajo_stream.py | Lote 39: `Transcript.tsx` virtualiza la lista de turnos (`useVirtualizer`) y repinta el turno en streaming como maximo una vez por frame (`useFrameBatched`/`lib/frame-batch.ts`); prueba mecanica de que esta cableado, no una medicion de seleccion pixel a pixel en navegador real |
| QA-38 | verde | test_qa_38_historia_y_tarjeta.py | — |
| QA-39 | verde | test_qa_39_export_defectuoso.py, test_l36_session_export_artifact_manifest.py | Cerrado de punta a punta (lote 36): `artifact_identity.validate_artifact_bytes()` sigue detectando tabla DOCX que excede pagina y formulas XLSX sin `fullCalcOnLoad`, y ahora `routes/session_routes.py::export_session` tambien enruta cada export por `artifact_store.collect()`/`persist()` (best-effort, nunca bloquea la descarga), asi que el DOCX/XLSX exportado hereda manifiesto y esa validacion en vez de servirse como bytes sueltos |
| QA-40 | verde | test_qa_40_pdf_escaneado.py | — |
| QA-41 | manual | test_qa_41_microfono_y_autoescucha.py | Microfono/altavoz reales; permisos de navegador interactivos |
| QA-42 | verde | test_qa_42_render_ya_aceptado.py | — |
| QA-43 | verde | test_qa_43_cambio_horario.py | — |
| QA-44 | xfail | test_qa_44_teclado_y_zoom.py | Lote 44 (integracion): re-ejecutado `scripts/ui_a11y.py` tras `studio/src/components/Dialog.tsx` (cabecera titulo+cerrar acotada con `min-inline-size:0`/`max-inline-size:100%` para que un titulo largo no empuje el boton de cerrar fuera de la caja a 200% zoom) + `npx vite build`; resultado real, variable entre 34/38 y 35/38 (logs/ui_a11y/result.json, varias corridas en esta misma ronda), NO 38/38. Persisten los mismos 2-3 huecos: (1) "in viewport: dialog control @ 200%" — re-investigado con instrumentacion directa (no solo el JSON del informe): el elemento con foco en ese instante es el CHIP `data-testid="studio-workspace"` del COMPOSER (`fs-studio__chip--folder`), no un control dentro de `WorkspaceDialog.tsx` — la atribucion anterior ("WorkspaceDialog.tsx desborda") era una hipotesis, no una comprobacion; la causa real esta en `Studio.tsx` L451-469 `pickWorkspace()` (intenta `pickNative` de forma asincrona antes de `setWsOpen(true)`) + el `lazy(() => import('./studio/WorkspaceDialog'))`/`<Suspense fallback={null}>` de L93/L2420-2424: el foco entra brevemente en el dialogo (el propio test lo confirma con "opening the workspace dialog moves focus INTO it" en verde) y vuelve al chip del disparador poco despues — todos ficheros ajenos a este lote (Composer.tsx/Studio.tsx/WorkspaceDialog.tsx no estan en su vale-libre); (2) "in viewport: diff region @ 200%" reaparece en varias corridas con el mismo rect exacto pese al `scrollIntoView` del Lote 39 — no se ha investigado a fondo, ficheros ajenos (Transcript.tsx/studio.css); (3) "Escape closes the dialog" flaquea de forma intermitente (100% unas veces, 200% otras) en este entorno sandboxeado — sin evidencia de ser un bug de UI real y no de temporizacion del entorno (Playwright + servidor real bajo carga). Ver el informe del lote para el fichero/linea exactos de (1) |
| QA-45 | verde | test_qa_45_actualizacion_fallida.py | — |
| QA-46 | verde | test_qa_46_plugin_problematico.py | — |
| QA-47 | verde | test_qa_47_continuidad_de_novela.py | L41: `src/branching_futures/narrative_canon.py` distingue draft/discarded/promoted sobre `BranchingService`; `canon_state()` aun no esta enganchado en `src/memory_engine.py`/`src/context_engine/` (ficheros ajenos a L41, ver su informe) |
| QA-48 | verde | test_qa_48_migracion_de_carpeta.py | — |

## Como se regenera

`tests/test_qa_index.py::test_qa_estado_doc_lists_every_scenario_with_its_declared_state`
lee el marker `qa_state` de cada fichero en `tests/qa/` y comprueba que la fila
de esta tabla para ese ID menciona el mismo fichero y el mismo estado (en
espanol: verde/xfail/manual). Si un test cambia de estado, esta tabla debe
actualizarse a mano en la misma fila — el test dira exactamente cual quedo
desincronizada.
