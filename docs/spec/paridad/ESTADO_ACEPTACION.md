# Estado de los 36 casos de aceptación de paridad de aceptación (A01-A36)

Generado a mano (Lote T1, PR1) a partir de `docs/spec/paridad/acceptance_cases.json`
(copia sin cambios de `evidence/acceptance_cases.json` del paquete de
auditoría) y coherente con el marker `acceptance(case_id)` que cada test
declara (`pyproject.toml [tool.pytest.ini_options].markers`) —
`tests/test_acceptance_index.py` lee ese marker de todo `tests/*.py` y falla
si esta tabla se desincroniza con lo que realmente encuentra.

Regla del encargo (ver `docs/spec/paridad/README.md`): **ninguna fila pasa
a `verde` porque exista una clase o un test simulado del mecanismo**. Solo
cierra una fila un test que ejercita código real de Faustus (TestClient +
rutas reales, `agent_runs`/`approval_store`/`tool_approvals`/workflows
reales, fakes solo para el LLM y procesos externos) y que aparece en la
columna `Test` de esa fila.

- **verde**: el test existe, pasa y ejercita código real — la fila lo
  enlaza.
- **xfail**: el mecanismo no existe todavía; el test existe y está marcado
  `xfail(strict=True)` con el motivo en el propio test.
- **pendiente**: sin test todavía. Es el estado por defecto de este lote
  (PR1 solo entrega el manifiesto y el ejecutor, no los mecanismos de
  A01-A36); no implica que el caso esté descartado.

Resumen tras fusionar T1+T2+T3+S3+T4-ola1 (2026-09-16): **10 verde** (A01–A07,
A08, A09, A20), 26 `pendiente`. En la entrega de T1 (PR1) era **1 verde**
(A03), **0 xfail**, **35 pendiente**. De las 35 pendientes, 6 estaban
asignadas por `CONTRATO_PARIDAD_1.md` a los lotes T2 (A01, A04, A07) y T3
(A02, A05, A06); T4 (ola 1 de paridad, `CONTRATO.md`) cierra A08/A09 (tool
discovery); las 27 restantes quedan abiertas para un PR posterior.

| ID | Área | Estado | Test | Qué falta |
|---|---|---|---|---|
| A01 | turns | verde | `tests/acceptance/test_a01_turn_admission.py` | — (T2: lock de admisión por sesión en `routes/chat_routes.py::chat_stream` que envuelve solo la mutación hasta `agent_runs.start()`; un POST inválido nunca toca el run válido en curso; dos POST válidos concurrentes no se entrelazan; `client_message_id` en carrera sigue siendo un solo turno) |
| A02 | approvals | verde | `tests/acceptance/test_a02_approval_race.py` | — (T3: la misma decisión desde dos clientes concurrentes contra la ruta real → una fila decidida y el perdedor recibe 409 con el recibo del ganador; `tool_approvals.consume_with_reason` devuelve `consumed`/`already_consumed`/`expired`/`not_found`/`owner_mismatch`/`invalid_decision`) |
| A03 | approvals | verde | `tests/test_tool_approvals.py::test_dispatcher_rejects_modified_approved_action` | — (digest canónico de `src/tool_approvals.py:120-171,293-330` comprobado en `src/tool_execution.py:1179-1195`; test marcado `@pytest.mark.acceptance("A03")` en este lote, sin más cambios) |
| A04 | events | verde | `tests/acceptance/test_a04_replay_cursor.py` | — (T2: corte tras k eventos y reconexión real por `GET /api/chat/resume` con cursor → sin hueco ni duplicado, `[DONE]` una vez, LLM falso llamado una vez) |
| A05 | recovery | verde | `tests/acceptance/test_a05_unknown_effect.py` | — (T3: evento `tool_effect` `pending|confirmed|failed` por `call_id` con flush inmediato para tool-calls no `read`; `recover_interrupted_runs` marca `unknown_effects` en el parcial y en la nota; el siguiente turno recibe un bloque de sistema que prohíbe repetirlas sin comprobar) |
| A06 | recovery | verde | `tests/acceptance/test_a06_lease_fencing.py` | — (T3: `lease_generation` en `scheduled_tasks` y `workflow_node_runs` con migración idempotente; `still_owner` antes de cada efecto en el scheduler → estado `fenced`; handlers de workflow con `_check_fenced` — devuelve `failed` + `fenced: True` porque el engine solo admite cuatro estados de handler) |
| A07 | cancel | verde | `tests/acceptance/test_a07_cancel_cascade.py` | — (T2: `stop_workers_of_parent_by_level` transitivo; `chat_stop` retira aprobaciones de toda la jerarquía y cancela sus preguntas; `cleanup.workers_stopped` por nivel y `cleanup.approvals_retired`) |
| A08 | tools | verde | `tests/acceptance/test_a08_tool_discovery.py` | — (T4: `lookup_tools` — ya existente en `src/tool_serve.py` — narrowed to the EXACT permitted schema by `src/tool_discovery.py::is_permitted`/`audit_selection` — `tool_policy`/`disabled_tools`/admin denylist AND real-tool existence, not just `disabled_tools` — wired into `agent_loop.py`'s `lookup_tools` promotion step; selection+resolution audited via the new `tool_discovery` SSE event) |
| A09 | tools | verde | `tests/acceptance/test_a09_tool_discovery_no_match.py` | — (T4: `src/tool_discovery.py::audit_selection`'s unknown-tool branch — a name that is not a real tool never resolves/promotes; explicit bounded (N=5) "closest by name/capability" fallback via `nearest_by_name_or_capability`, no fabricated call, no schema dump) |
| A10 | code_mode | verde | `tests/acceptance/test_a10_code_mode_policy_gate.py` | — (T6: `run_code` compone llamadas en `src/code_mode/` — subproceso `python -I` aislado + `guest.py` + `bridge.dispatch_call`, que despacha cada `tools.call` por el mismo `execute_tool_block` real; un comando destructivo obtiene el mismo rechazo del guardia que una llamada directa y un tool en `disabled_tools` es inalcanzable. `security_context` se reconstruye como `ToolRunSecurityContext()` nueva en cada llamada del puente porque `ctx` de `TOOL_HANDLERS` no transporta la instancia real del turno — ver `T6_wiring.md`; no cambia el veredicto para el guardia de comandos destructivos, que corre antes de consultar el estado del contexto) |
| A11 | code_mode | verde | `tests/acceptance/test_a11_code_mode_quotas.py` | — (T6: `src/code_mode/runner.py` — timeout de pared (`agent_code_mode_timeout_seconds`), máximo de llamadas (`agent_code_mode_max_calls`), bytes de salida (`agent_code_mode_max_output_bytes`), RLIMIT_CPU/RLIMIT_AS por `preexec_fn` en POSIX; recibo diagnóstico `{terminated_by, calls_made, elapsed_ms, output_bytes, last_call}`; el pid del subproceso no sobrevive tras el kill (comprobado con `os.kill(pid, 0)`). CPU/memoria vía rlimits implementados pero sin test dedicado en este lote — no exigido por los 4 casos listados en el contrato) |
| A12 | artifacts | verde | `tests/acceptance/test_a12_tool_result_offload.py::test_two_parallel_oversized_results_survive_a_restart_with_matching_integrity` | — (T5: `src/tool_result_offload.offload_if_oversized` persiste el resultado íntegro en el artifact store real — owner, sha256, bytes, retention — antes de sustituirlo por un resumen acotado + `artifact_id`; dos tools en paralelo → dos artefactos distintos; tras recrear el engine/app desde el mismo sqlite en disco, `routes/artifact_routes.py` real + `read_artifact_range` recuperan el owner con sha256 recomputado == guardado. Cableado de la llamada en `src/agent_loop.py` (no es fichero de T5) y del tool `read_artifact` en `src/tool_schemas.py`/`src/agent_tools/__init__.py` (T6) documentado en `T5_wiring.md`, comprobado por `tests/test_t5_wiring.py` `xfail(strict=True)`) |
| A13 | artifacts | verde | `tests/acceptance/test_a13_artifact_tenant_isolation.py::test_wrong_tenant_and_nonexistent_id_get_identical_denials` y `::test_encoded_traversal_ids_never_reach_the_filesystem` | — (T5: `routes/artifact_routes.py` real vía TestClient; otro owner con el mismo id real → 404 byte-idéntico al de un id inexistente, sin metadatos (sha256/owner/tamaño) en el cuerpo, en `metadata`/`download`/`manifest`/`provenance`; ids con traversal codificado (`..%2F`, `%2e%2e/`, `..\\`, `%5c`, id con `/`) nunca resuelven una ruta (404 de enrutado) ni tocan el store; `src.artifact_store.path_of` rechaza la forma decodificada de forma independiente) |
| A14 | context | verde | `tests/acceptance/test_a14_compaction_preserve.py::test_compaction_preserves_pending_approval_constraints_objective_and_sources` | — |
| A15 | context | verde | `tests/acceptance/test_a15_reacquisition.py::test_spilled_body_is_reacquired_intact_and_the_run_report_records_its_cost` | Reacquisition tool (`read_overflow`) not yet wired to the agent's live tool loop — see `T8_wiring.md` |
| A16 | sandbox | pendiente | — | Fuera del alcance de T1/T2/T3 |
| A17 | sandbox | pendiente | — | Fuera del alcance de T1/T2/T3 |
| A18 | mcp | pendiente | — | Fuera del alcance de T1/T2/T3 |
| A19 | mcp | pendiente | — | Fuera del alcance de T1/T2/T3 |
| A20 | sdk | verde | `tests/acceptance/test_a20_external_sdk_consumer.py` | — (S3: `npm pack` + `npm install --offline` en dos proyectos limpios, ESM y CommonJS, contra un servidor real con auth activada y un token `ody_…` real de perfil `sdk`; turno de agente con `workspace`, aprobación `tool_approval` real vía `approve_task`, edición que llega a disco, `sessions.export()` + `artifacts.list/get/download` con `sha256` verificado, `turn.cancel('task')` + `turns.resume()` → `RunNotActiveError`, y un token sin scope `sessions` rechazado con 403 al crear sesión; de paso corrigió un bug real en `routes/chat_routes.py::_resolve_request_workspace` que descartaba `workspace` en toda llamada autenticada por token — ver `MATRIZ_PARIDAD.md` fila 2 para las reservas honestas sobre el propio paquete) |
| A21 | embed | pendiente | — | Fuera del alcance de T1/T2/T3; sin UI embebible en el árbol (ver `MATRIZ_PARIDAD.md` fila 3) |
| A22 | auth | pendiente | — | Fuera del alcance de T1/T2/T3; sin OIDC de aplicación (ver `MATRIZ_PARIDAD.md` fila 7) |
| A23 | machine_auth | pendiente | — | Fuera del alcance de T1/T2/T3 |
| A24 | schedule | pendiente | — | Fuera del alcance de T1/T2/T3 |
| A25 | skills | pendiente | — | Fuera del alcance de T1/T2/T3 |
| A26 | learning | pendiente | — | Fuera del alcance de T1/T2/T3 |
| A27 | learning | pendiente | — | Fuera del alcance de T1/T2/T3 |
| A28 | learning | pendiente | — | Fuera del alcance de T1/T2/T3 |
| A29 | loops | verde | `tests/acceptance/test_a29_loop_breaker.py::test_twenty_identical_tool_calls_stop_the_turn_deterministically` | — (T7 + integración: `src/loop_breaker.py::LoopPolicy` cableada en `src/agent_loop.py` — observa cada (tool, args, resultado) tras ejecutar y cada duplicado que la recuperación de bucles salta; N idénticos → aviso, luego la tool se retira, luego el turno termina con `stop_reason=non_progressing_loop` y evento SSE `loop_breaker_stop`, sin depender de la auto-reflexión del modelo) |
| A30 | learning | pendiente | — | Fuera del alcance de T1/T2/T3 |
| A31 | budget | verde | `tests/acceptance/test_a31_budget_reservations.py::test_parallel_children_reserve_and_reconcile_against_shared_run_budget` | `src/budget_account.py` reservation/reconciliation is wired into `src/agent_tools/subagent_tools.py`'s worker launch (real `delegate_agents`); `GET /api/runs/{run_id}/budget` (`routes/budget_routes.py`) works but is not yet mounted on `app.py` (not T7-owned) — one-line diff in `T7_wiring.md` §1, checked by `tests/test_t7_wiring.py` (xfail). Cost is always `unpriced_usage` today (no per-model price table found) — never a false 0.0. |
| A32 | benchmark | pendiente | — | Fuera del alcance de T1/T2/T3; `scripts/acceptance_run.py` (este lote) ya sienta el formato `cost_status`/`total_cost` por caso como paso previo |
| A33 | benchmark | pendiente | — | Fuera del alcance de T1/T2/T3 |
| A34 | migration | pendiente | — | Fuera del alcance de T1/T2/T3 |
| A35 | distribution | pendiente | — | Fuera del alcance de T1/T2/T3 |
| A36 | product | pendiente | — | Fuera del alcance de T1/T2/T3 |

## Cómo T2 y T3 actualizan sus filas

1. Escribe el test en `tests/acceptance/test_a<NN>_<slug>.py` (o, si el caso
   ya vive en un fichero existente como A03, decóralo ahí) con exactamente
   un `@pytest.mark.acceptance("A0N")` por función de test. Usa
   `tests/acceptance/conftest.py::record_evidence(request, **refs)` para
   adjuntar referencias concretas (session/run ids, nodeids, ficheros) que
   `scripts/acceptance_run.py` recogerá en `evidence_refs`.
2. Actualiza la fila de este fichero: cambia `Estado` a `verde` (mecanismo
   real, test real, pasa) o `xfail` (mecanismo ausente; test
   `xfail(strict=True)` con el motivo dentro del propio test), rellena
   `Test` con `fichero::función` exacto, y `Qué falta` con lo que quede (o
   `—` si nada).
3. Corre `python3 -m pytest tests/test_acceptance_index.py -q` — falla con
   un mensaje exacto si la fila no nombra el fichero real, si el estado no
   coincide con el marker, o si `xfail` se declaró sin `@pytest.mark.xfail`
   en el test.
4. Si tu lote toca una fila que no era tuya (p. ej. porque el orquestador
   fusiona T2 y T3 sobre el mismo `ESTADO_ACEPTACION.md`), edita solo tu(s)
   fila(s) — cada lote es responsable únicamente de A01/A04/A07 (T2) o
   A02/A05/A06 (T3); si el fichero no existe todavía en tu worktree (porque
   T1 aún no se ha fusionado), créalo con estas mismas 36 filas y que el
   orquestador reconcilie el merge.
5. No toques la fila de A03 salvo que cambies el propio mecanismo que
   prueba — sigue siendo responsabilidad de quien la cierre originalmente
   documentar cualquier cambio ahí.
