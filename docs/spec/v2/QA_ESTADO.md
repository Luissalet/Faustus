# Estado de los 48 escenarios QA como regresiones permanentes

Generado a mano (Lote 11) a partir de `docs/spec/v2/acceptance_scenarios.json` y
coherente con el marker `qa_state` que cada fichero en `tests/qa/` declara —
`tests/test_qa_index.py` compara esta tabla contra esos markers y falla si
alguna fila se desincroniza. **verde**: el comportamiento existe y el test lo
prueba contra el codigo real. **xfail**: el mecanismo no existe todavia
(`xfail(strict=True)`, con el motivo en el propio test). **manual**: requiere
hardware/navegador real (microfono, zoom interactivo) y no puede ejecutarse en
pytest; el test esta `skip` con los pasos manuales documentados.

Resumen: **27 verde**, **19 xfail**, **2 manual** — 48 de 48.

| ID | Estado | Test | Que falta |
|---|---|---|---|
| QA-01 | verde | test_qa_01_carpeta_sin_git.py | — |
| QA-02 | xfail | test_qa_02_proyecto_grande.py | Indice de simbolos (definicion/callers) por nombre, IDX-02/IDX-03 |
| QA-03 | verde | test_qa_03_diseno_ambiguo.py | — |
| QA-04 | xfail | test_qa_04_no_preguntar_lo_obvio.py | Guion e2e (fake_llm) que pruebe que un edit literal nunca dispara ask_user |
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
| QA-17 | xfail | test_qa_17_lote_parcialmente_aplicado.py | Journal/compensacion por archivo dentro de un lote multiarchivo (EDIT-02) |
| QA-18 | xfail | test_qa_18_rollback_fuera_de_alcance.py | workspace_checkpoints.status() no reporta exclusiones/efectos irreversibles (EDIT-04) |
| QA-19 | verde | test_qa_19_no_todo_verde_es_correcto.py | — |
| QA-20 | verde | test_qa_20_fallo_preexistente.py | — |
| QA-21 | verde | test_qa_21_citas_vacias.py | — |
| QA-22 | verde | test_qa_22_brief_largo.py | — |
| QA-23 | verde | test_qa_23_buscadores_degradados.py | — |
| QA-24 | xfail | test_qa_24_modelos_simultaneos.py | Reserva atomica (Lock) entre admit() concurrentes de VRAM (HW-01) |
| QA-25 | verde | test_qa_25_kv_desconocida.py | — |
| QA-26 | xfail | test_qa_26_presion_de_commit.py | Vigilante de presion de RAM/commit del propio proceso (EXEC-04/PERF-04) |
| QA-27 | xfail | test_qa_27_caida_de_nodo.py | remote_worker no es un backend implementado (HW-06) |
| QA-28 | xfail | test_qa_28_cambio_de_modelo.py | Recalculo de capacidades + reconstruccion de estado al cambiar de modelo (MOD-06) |
| QA-29 | xfail | test_qa_29_privacidad_transitiva.py | Perfil local-only unico que cubra reranker/resumen remoto (SEC-04/MOD-05) |
| QA-30 | verde | test_qa_30_inyeccion_en_documento.py | — |
| QA-31 | verde | test_qa_31_ssrf_y_redireccion.py | — |
| QA-32 | verde | test_qa_32_preview_malicioso.py | — |
| QA-33 | verde | test_qa_33_aislamiento_de_dueno.py | — |
| QA-34 | xfail | test_qa_34_olvido_e_incognito.py | Tombstones que sobrevivan a un reindex/import (MEM-02) |
| QA-35 | xfail | test_qa_35_borrador_persistente.py | Persistir attachments del composer junto al texto del borrador (UX-01) |
| QA-36 | xfail | test_qa_36_regenerar_con_efectos.py | Endpoint de regenerar dedicado que no repita efectos externos (UX-03) |
| QA-37 | xfail | test_qa_37_lectura_bajo_stream.py | Virtualizacion de listas largas en Studio (PERF-01) |
| QA-38 | verde | test_qa_38_historia_y_tarjeta.py | — |
| QA-39 | xfail | test_qa_39_export_defectuoso.py | Validacion visual/calculo tras exportar DOCX/XLSX, estado generado vs revisado (ART-03/ART-04) |
| QA-40 | verde | test_qa_40_pdf_escaneado.py | — |
| QA-41 | manual | test_qa_41_microfono_y_autoescucha.py | Microfono/altavoz reales; permisos de navegador interactivos |
| QA-42 | verde | test_qa_42_render_ya_aceptado.py | — |
| QA-43 | xfail | test_qa_43_cambio_horario.py | Politica explicita de ambiguedad DST / misfire (AUTO-01) |
| QA-44 | manual | test_qa_44_teclado_y_zoom.py | Navegador real a 200% de zoom, navegacion solo teclado |
| QA-45 | xfail | test_qa_45_actualizacion_fallida.py | Backup antes de migrar schema + rollback transaccional (OPS-02) |
| QA-46 | xfail | test_qa_46_plugin_problematico.py | Modo seguro / desactivacion automatica de un MCP que se cuelga (OPS-05) |
| QA-47 | xfail | test_qa_47_continuidad_de_novela.py | Distincion canon vs alternativa descartada (WRITE-02/WRITE-04) |
| QA-48 | verde | test_qa_48_migracion_de_carpeta.py | — |

## Como se regenera

`tests/test_qa_index.py::test_qa_estado_doc_lists_every_scenario_with_its_declared_state`
lee el marker `qa_state` de cada fichero en `tests/qa/` y comprueba que la fila
de esta tabla para ese ID menciona el mismo fichero y el mismo estado (en
espanol: verde/xfail/manual). Si un test cambia de estado, esta tabla debe
actualizarse a mano en la misma fila — el test dira exactamente cual quedo
desincronizada.
