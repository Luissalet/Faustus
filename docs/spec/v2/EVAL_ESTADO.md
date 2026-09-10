# Estado de la evaluación (lote 36): EVAL-01/03/04, BENCH-02, BASE-02, ARCH-02

Generado a mano en el lote 35 y actualizado en el lote 36 (integración de la
ola 5), que corrigió el bug real que EVAL-04 había descubierto. Coherente con
`docs/spec/v2/MAPA_REUTILIZACION.md` (filas EVAL-01, EVAL-03, EVAL-04,
BENCH-02, BASE-02, ARCH-02), que sigue siendo la fuente de verdad para el
resto de filas — este documento solo profundiza en las seis que tocaron
estos dos lotes.

| ID | Estado | Dónde vive | Qué falta / nota |
|---|---|---|---|
| EVAL-01 | verde | `tests/eval/` (harness, tasks, tests), `scripts/eval_run.py`, `tests/eval/baseline.json` | `--live` (modelo real) está escrito y probado en su forma (`test_run_live_is_wired_but_not_exercised`) pero no ejecutado: este sandbox no tiene un endpoint de modelo real. |
| EVAL-03 | verde | `tests/eval/test_chaos_protocols.py` | Cubre los 4 estímulos que no estaban ya cubiertos (`tests/qa` ya cubría el JSON fragmentado y el reinicio en research, marcados "(ya)" en el lote). |
| EVAL-04 | parcial | `scripts/ui_smoke.py` | Real, con Chromium, contra el build servido (`static/studio/`): login/nueva conversación/Settings (Effective config, Tools, System-Diagnóstico) verdes. El bug real que dejaba en rojo el envío de mensaje (ver abajo) **se corrigió en el lote 36**; el paso sigue en rojo por un hallazgo NUEVO y distinto, también documentado abajo. |
| BENCH-02 | verde | `src/auto_review.py::review_gate` + `tests/test_bench_review.py`; la parte "medir" la cubre EVAL-01 (`baseline.json` + `scripts/eval_run.py --live`) | — |
| BASE-02 | verde | `src/state_mirror/divergence.py` + `tests/test_state_mirror_divergence.py` | — |
| ARCH-02 | verde | `src/process_ownership.py` (`can_stop`, `owner_of`, `note_started(..., owner=)`) + `tests/test_process_ownership_rights.py` | `can_stop` no está todavía llamado desde ningún sitio de producción (`server_runtime.py`/`desktop/main.cjs` no están en el PROPIOS de este lote) — es la pieza que un futuro lote puede enchufar donde hoy solo se llama `check()`. |

## El hallazgo de EVAL-04 (lote 35) — CORREGIDO en el lote 36

`scripts/ui_smoke.py` es, a propósito, el primer test de este repositorio
que dirige un Chromium real contra `/api/chat_stream`. Al hacerlo encontró
un defecto real que ningún test anterior podía ver:

`routes/chat_routes.py::chat_stream` (y `chat_endpoint`, el mismo patrón)
calculaban `owner = effective_user(request)` — y esa función devuelve `None`
en crudo (no `""`) cuando `AUTH_ENABLED=false`, porque el middleware de auth
que rellenaría `request.state.current_user` ni siquiera se ejecuta en ese
modo (a diferencia de `require_user()`, que sí tiene el `return ""` explícito
para ese caso). Cuando el cliente manda `client_message_id` — el id de
reintento idempotente que el navegador real siempre manda (TASK-03) —
`chat_outbox.record_intent(owner=None, ...)` reventaba el `NOT NULL` de
`outbox.owner` y la ruta respondía 500.

Reproducido dos veces, con y sin login real (`/api/auth/setup` +
`/api/auth/login` con una cuenta admin real, no solo `LOCALHOST_BYPASS`), así
que no era un artefacto del modo de pruebas.

**Corregido en el lote 36**: `routes/chat_routes.py` normaliza
`owner = effective_user(request) or ""` (igual que `require_user()`) en
`chat_stream` y en `chat_endpoint`; `src/chat_outbox.py` normaliza
`None -> ""` como defensa adicional en `record_intent`/`get`/`mark_running`/
`mark_finished`. `src/question_store.py` ya normalizaba con `owner or ""` al
escribir (`Store.open`), así que no necesitó cambio. Prueba de regresión:
`tests/test_l36_chat_outbox_owner_none.py` — monta la función real de
`/api/chat_stream` en un `TestClient` (sin middleware de auth, para que
`request.state.current_user` esté genuinamente ausente) y comprueba 200;
revertido el `or ""` el mismo test falla con el `sqlite3.IntegrityError`
original propagado a través del stack real de FastAPI (confirmado a mano).
`scripts/ui_smoke.py` ya no quita `client_message_id` de la petición.

## Hallazgo NUEVO (lote 36): la tarjeta de pregunta sigue sin aparecer, por otra razón

Con el bug de `owner=None` corregido, `python scripts/ui_smoke.py` ya no
revienta con 500: login, nueva conversación y los tres paneles de Settings
quedan en verde. El paso 3 (enviar mensaje + tarjeta `ask_user`) sigue en
rojo, pero por un motivo distinto y no relacionado con el bug anterior.

Diagnóstico (reproducido sin navegador, llamando a `/api/chat_stream`
directamente vía `tests/eval/harness.py::EvalApp` con el mismo guion que usa
`ui_smoke.py`): el turno SÍ llega al modelo y el modelo SÍ responde con el
bloque` ```ask_user ... ``` ` exacto, pero el evento `metrics` de esa
respuesta trae `"direct_low_signal": true, "agent_rounds": 0,
"tool_calls": 0` — el texto se streamea tal cual (se ve literalmente
` ```ask_user\n{"question": ...} ``` ` en el transcript) en vez de pasar por
el analizador de tool-calls de `src/agent_loop.py` que reconocería el fence
y emitiría el evento `ask_user`. Es decir: algún heurístico de enrutamiento
("low signal") está clasificando este mensaje concreto como
chat-directo-sin-herramientas incluso con `mode=agent`, y por tanto el bloque
` ```ask_user``` ` nunca se interpreta.

**No corregido aquí**: diagnosticar y arreglar ese heurístico de
enrutamiento vive dentro de `src/agent_loop.py`, y el `PROPIOS` de este lote
para ese fichero es explícitamente limitado ("SOLO unificar
`_AGENT_PREAMBLE`/`_API_AGENT_RULES`, y —si es barato— un evento
`verification_summary`"); no incluye tocar la lógica de enrutamiento
directo/agente. Queda para un lote futuro con ese fichero en su PROPIOS.
`logs/ui_smoke/result.json` (regenerado en este lote) trae el detalle real
del fallo (`TimeoutError` esperando `[data-testid="studio-question"]`) sin
que `known_issues` lo disfrace de otra cosa — ese campo queda vacío a
propósito porque el hallazgo ya no es del script, sino de agent_loop, y no
hay workaround aplicado.

## Ejecutar

```
python scripts/eval_run.py                 # EVAL-01, sin modelo
python scripts/eval_run.py --live --endpoint-url URL --model NAME   # EVAL-01, con modelo real
python scripts/ui_smoke.py                  # EVAL-04, navegador real (Playwright + Chromium)
python scripts/ui_smoke.py --dry-run        # EVAL-04, sin navegador: valida selectores contra static/studio/
```
