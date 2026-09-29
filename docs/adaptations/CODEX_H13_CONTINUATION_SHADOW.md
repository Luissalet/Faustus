# H13/H07 parcial: observar la ampliación de rondas

Fuente ya visitada: [H07 y H13 del análisis](CODEX_HARNESS_ANALISIS_2026-09-29.md),
[bucle Codex en revisión fijada](https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core/src/codex.rs).
Implementación propia, sin nueva revisión upstream.

## Alcance implementado

`src/continuation_decision.py` es puro: dataclasses inmutables, sin imports de
Faustus, configuración, I/O ni autorización. Describe el gate de ampliación al
superar el presupuesto de rondas. Recibe progreso, ciclos restantes, techo y
estado de recuperación; devuelve acción, causa, rondas adicionales, ciclos
restantes y racha de falta de progreso.

No decide por el bucle. `agent_loop` conserva exactamente su condición y las
mutaciones originales. Después de extender o entrar en la rama de cierre,
compara la predicción con la acción real, delta real de rondas, ciclos reales,
racha y gate de progreso. No emite SSE, añade mensajes, registra eventos del
ledger ni cambia el recuento de progreso.

El diagnóstico se limita a tres coincidencias en DEBUG y tres discrepancias o
fallos por turno (discrepancias en WARNING; fallos del observador en DEBUG).
No se registra contenido de usuario: solamente estado numérico y causa.
Un error de cálculo, comparación o decisión malformada no cambia el control
original. Es observación en memoria/logs; no un registro durable de decisiones.

## Evidencia

Los tests ejecutan mediante AST las sentencias reales del gate del bucle,
aislando efectos ajenos a este presupuesto. No duplican a mano su algoritmo
para compararlo consigo mismo. Una matriz de **432 combinaciones** contrasta
el presupuesto, ciclos y racha obtenidos con las predicciones. Casos explícitos
cubren frontera de presupuesto, ciclos 0/1/-1, evento nuevo, racha 2→3,
recuperación y techo de progreso.

El wiring real aislado se ejecuta con predicción deliberadamente equivocada:
el bucle sigue concediendo sus ciclos originales. Fallos forzados del cálculo,
comparador y objetos malformados en ambas ramas conservan las transiciones y
el límite de logs. No se arrancan modelos ni turnos largos.

```text
venv/Scripts/python.exe -m pytest tests/test_continuation_decision_shadow.py tests/test_plan_coverage.py tests/test_completion_gate.py tests/test_p1_plan06_completion_gate.py tests/test_completion_engine_wiring.py -q
```

Resultado: **75 pruebas correctas**, incluidas 16 nuevas y el wiring de
completion/plan existente.

## Discrepancia de cierre identificada, pendiente

La rama de completion engine admite dos continuaciones (`_CE_MAX_ROUNDS=2`).
Si su nueva decisión todavía contiene `continue_with` con ese cupo agotado,
ya no continúa; la misma lista impide entrar en `plan_coverage`. Además, puede
perdurar una razón `completion_continue` previa. Se observó en lectura de las
condiciones de cierre; este incremento no modifica esa política ni declara
resuelto el caso. Requiere caracterización separada de la precedencia de cierre.

## Lo que falta

No existe todavía controlador único de retry/completion/plan ni catálogo ligado
al paso. El shadow solo observa este gate cuando se supera el presupuesto; su
resultado `not_due` se prueba como función pura. No se ha activado como autoridad
ni se ha demostrado equivalencia de todo `agent_loop`. Los gates de autonomía
anteriores y las políticas de herramientas permanecen fuera de este módulo.
