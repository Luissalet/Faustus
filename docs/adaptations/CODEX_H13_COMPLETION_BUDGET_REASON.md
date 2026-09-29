# H13: razón de cierre al agotar continuaciones de completion engine

Continúa el hallazgo registrado en
[H13 shadow](CODEX_H13_CONTINUATION_SHADOW.md). Fuente ya visitada:
[bucle Codex, revisión fijada](https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core/src/codex.rs).
No se repite investigación upstream ni se adopta una política de continuación nueva.

## Reproducción y corrección

Ejecutando por AST las ramas reales de cierre con decisión CE live y propuesta
pendiente, dos continuaciones usadas (máximo dos) y un plan pendiente:
no se emitía otra instrucción, no se evaluaba el plan y el ledger mantenía
`completion_continue`. El evento anunciaba `would_continue=1`, sin distinguir
propuesta y ronda concedida. No se usaron modelos en esta reproducción.

Se conserva el cierre y la exclusión del plan: no hay rondas extra ni bypass de
la cuota CE. Cuando existe propuesta y la cuota está agotada, el ledger registra
`completion_budget_exhausted`. El SSE `completion_decision` añade:

- `continuation_proposed`: existe una propuesta live en `continue_with`.
- `continuation_granted`: el gate CE admite esa continuación.
- `continuation_block_reason`: `completion_round_limit`, `shadow`, `no_proposal`
  o vacío si se admite.
- `completion_rounds_used` y `completion_rounds_limit`: estado antes de conceder
  la eventual ronda. No representan presupuesto global de autonomía.
- `engine_stop_reason`: razón original del motor. `stop_reason` comunica el
  límite efectivo del bucle cuando impide continuar.

`would_continue` mantiene el recuento previo de propuestas live. En shadow,
el servicio entrega `continue_with=[]` y los candidatos evaluados internamente
no se presentan como propuestas concedibles. Shadow y ausencia de propuesta
conservan el gate previo de revisión del plan.

## Consumidores y límites

`TurnLedger.stop_reason` es texto libre y su summary conserva el valor.
`tool_outcome.classify_status` lo clasifica como interrupción esperada, no éxito;
se comprueba por test. El vocabulario cerrado de `CompletionDecision.stop_reason`
no se modifica: la nueva razón pertenece al runtime y al SSE, no se introduce
en el contrato persistido del motor. No se modifica su decisión ni presupuesto.

Esto no añade una tarjeta UI o auto-reanudación. Tampoco garantiza que no haya
otros caminos de cierre con razones obsoletas; queda acotado a propuesta CE
bloqueada por su cuota.

## Pruebas

Fixtures guionizadas ejecutan las ramas reales vía AST: cuotas usadas 0/1/2/3,
con y sin plan, shadow sin propuesta, propuesta vacía live, cuota del plan
disponible/agotada y clasificación de resultado. Verifican mensajes, contadores,
ausencia de comprobación nueva del plan y campos del evento.

```text
venv/Scripts/python.exe -m pytest tests/test_completion_budget_reason.py tests/test_continuation_decision_shadow.py tests/test_plan_coverage.py tests/test_p1_plan06_completion_gate.py -q
```

Resultado: **41 pruebas correctas**, 11 nuevas. Verificación adicional con
`tests/test_completion_budget_reason.py tests/test_completion_engine_wiring.py
tests/test_completion_gate.py`: **56 correctas**. No modelos ni llamadas externas.
