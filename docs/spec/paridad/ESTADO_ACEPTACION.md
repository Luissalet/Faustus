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

Resumen tras fusionar T1+T2+T3+S3 (2026-09-13): **8 verde** (A01–A07, A20), 28 `pendiente`. En la entrega de T1 (PR1) era **1 verde** (A03),
**0 xfail**, **35 pendiente**. De las 35 pendientes, 6 están asignadas por
`CONTRATO_PARIDAD_1.md` a los lotes T2 (A01, A04, A07) y T3 (A02, A05,
A06) de este mismo encargo; las 29 restantes (A08-A36 salvo las listadas)
quedan fuera del alcance de T1/T2/T3 y abiertas para un PR posterior.

| ID | Área | Estado | Test | Qué falta |
|---|---|---|---|---|
| A01 | turns | verde | `tests/acceptance/test_a01_turn_admission.py` | — (T2: lock de admisión por sesión en `routes/chat_routes.py::chat_stream` que envuelve solo la mutación hasta `agent_runs.start()`; un POST inválido nunca toca el run válido en curso; dos POST válidos concurrentes no se entrelazan; `client_message_id` en carrera sigue siendo un solo turno) |
| A02 | approvals | verde | `tests/acceptance/test_a02_approval_race.py` | — (T3: la misma decisión desde dos clientes concurrentes contra la ruta real → una fila decidida y el perdedor recibe 409 con el recibo del ganador; `tool_approvals.consume_with_reason` devuelve `consumed`/`already_consumed`/`expired`/`not_found`/`owner_mismatch`/`invalid_decision`) |
| A03 | approvals | verde | `tests/test_tool_approvals.py::test_dispatcher_rejects_modified_approved_action` | — (digest canónico de `src/tool_approvals.py:120-171,293-330` comprobado en `src/tool_execution.py:1179-1195`; test marcado `@pytest.mark.acceptance("A03")` en este lote, sin más cambios) |
| A04 | events | verde | `tests/acceptance/test_a04_replay_cursor.py` | — (T2: corte tras k eventos y reconexión real por `GET /api/chat/resume` con cursor → sin hueco ni duplicado, `[DONE]` una vez, LLM falso llamado una vez) |
| A05 | recovery | verde | `tests/acceptance/test_a05_unknown_effect.py` | — (T3: evento `tool_effect` `pending|confirmed|failed` por `call_id` con flush inmediato para tool-calls no `read`; `recover_interrupted_runs` marca `unknown_effects` en el parcial y en la nota; el siguiente turno recibe un bloque de sistema que prohíbe repetirlas sin comprobar) |
| A06 | recovery | verde | `tests/acceptance/test_a06_lease_fencing.py` | — (T3: `lease_generation` en `scheduled_tasks` y `workflow_node_runs` con migración idempotente; `still_owner` antes de cada efecto en el scheduler → estado `fenced`; handlers de workflow con `_check_fenced` — devuelve `failed` + `fenced: True` porque el engine solo admite cuatro estados de handler) |
| A07 | cancel | verde | `tests/acceptance/test_a07_cancel_cascade.py` | — (T2: `stop_workers_of_parent_by_level` transitivo; `chat_stop` retira aprobaciones de toda la jerarquía y cancela sus preguntas; `cleanup.workers_stopped` por nivel y `cleanup.approvals_retired`) |
| A08 | tools | pendiente | — | Fuera del alcance de T1/T2/T3 de este contrato (`CONTRATO_PARIDAD_1.md` cubre solo A01-A07) |
| A09 | tools | pendiente | — | Fuera del alcance de T1/T2/T3 |
| A10 | code_mode | pendiente | — | Fuera del alcance de T1/T2/T3; sin puente Code Mode↔`tool_execution` encontrado en el árbol auditado (ver `MATRIZ_PARIDAD.md` fila 10) |
| A11 | code_mode | pendiente | — | Fuera del alcance de T1/T2/T3 |
| A12 | artifacts | pendiente | — | Fuera del alcance de T1/T2/T3 |
| A13 | artifacts | pendiente | — | Fuera del alcance de T1/T2/T3 |
| A14 | context | pendiente | — | Fuera del alcance de T1/T2/T3 |
| A15 | context | pendiente | — | Fuera del alcance de T1/T2/T3 |
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
| A29 | loops | xfail | `tests/acceptance/test_a29_loop_breaker.py::test_twenty_identical_tool_calls_stop_the_turn_deterministically` | `src/loop_breaker.py` (`LoopPolicy`) is implemented and unit-tested (`tests/test_loop_breaker.py`, all green) but not wired into `src/agent_loop.py`'s round loop — that file is not owned by lot T7; exact diff in `T7_wiring.md` §2. Today's inline `_loop_recovery_*` mechanism (FAUSTUS.md §87) redirects/blocks a repeated call but never stops the turn with `non_progressing_loop`. |
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
