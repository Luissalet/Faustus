# H14 parcial: consumo acumulado de intentos de un worker

Implementado: `_run_subagent` conserva los tokens acumulados antes de cada
invocación. Las métricas finales sustituyen las estimaciones por ronda de ese
intento, manteniendo el consumo de intentos anteriores. Una segunda emisión de
las mismas métricas no vuelve a sumar; los eventos de ronda posteriores a métricas
finales siguen sin contarse dos veces. Entrada y salida se contabilizan por
separado: una métrica parcial no borra la dimensión omitida. Valores negativos,
no finitos, booleanos o tipos inválidos no cuentan como tokens observados.

Fallo reproducido con el worker real y stream simulado: un primer intento de
100 tokens de entrada y 20 de salida seguido de un retry de 50 y 10 acababa
declarando solo 60 tokens en el ledger SQLite. Ahora conserva **180**. La misma
identidad `SubagentRun` es la que utiliza el retry existente de respuestas vacías
o meros acuses; no se añade una política nueva de reintentos.

## Validación

**49 pruebas aprobadas** entre `test_subagent_retry_usage.py`,
`test_subagent_board_events.py` y `test_budget_account.py`.

- Reconciliación real en SQLite temporal: 180 consumidos, reserva liberada y coste
  desconocido preservado como `unknown`.
- Rondas frente a totales finales corregidos y métricas duplicadas.
- Segundo intento sin métricas finales, interrumpido por excepción o cancelación:
  mantiene el uso observado sin borrar el primer intento.
- Tres intentos mezclando estimaciones, métricas finales y consumo cero.
- Métricas con una dimensión ausente y valores inválidos en rondas/totales.

No se ejecutaron modelos ni se modificaron sesiones o presupuestos reales.
Las pruebas invocan el worker dos/tres veces sobre el mismo objeto; no afirman
una prueba del ciclo completo de decisión automática del coordinador.

## Límites

No completa H14: `budget_account` y `autonomy_budget` no se unifican aquí, ni se
incorporan automáticamente compactaciones/revisores o consumo sin métricas.
No introduce precios ni transforma coste desconocido en cero. El ledger recibe
el acumulado corregido a través de la reconciliación existente del coordinador.

Fuente conceptual ya visitada: [H14/H16](CODEX_HARNESS_ANALISIS_2026-09-29.md) y
[Codex fijado b1e72963](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9).
Implementación propia; sin repetir la revisión upstream.
