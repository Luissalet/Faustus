# Paridad de aceptación — manifiesto (PR1 / Lote T1)

## Qué es este paquete

Este directorio recoge, dentro del árbol de Faustus, el resultado del primer
incremento de un encargo mayor: cerrar la brecha de producto entre Faustus y
un harness de agentes de referencia (no se nombra aquí a propósito) sin
adoptarlo como motor. La fuente es el dictamen externo del 13-09-2026
(copia de trabajo en
`/tmp/.../scratchpad/paridad/` durante la sesión que generó este lote; no
forma parte del árbol de Faustus). Documentos citados en esta carpeta:

- `04_COMPARATIVA_Y_DECISION.md` — la matriz de 22 razones para elegir
  el harness de referencia y el dictamen de que Faustus conserva su núcleo y cierra paridad
  de producto, no al revés.
- `16_BLUEPRINT_IMPLEMENTACION_V2.md` §9 — la secuencia de PRs (PR1..PR9);
  este lote es PR1.
- `evidence/acceptance_cases.json` — las 36 recetas de aceptación A01–A36,
  copiadas sin cambios de contenido en `acceptance_cases.json` de esta
  carpeta.
- `09_BACKLOG.csv` — el backlog TF01–TF26, copiado con una columna
  `estado_2026-09-13` reauditada en `backlog.csv` de esta carpeta.
- `10_LICENCIA_Y_ADOPCION.md` — la decisión de licencia (AGPL-3.0-or-later
  de Faustus frente a MIT del harness de referencia), que queda **abierta — decisión del
  propietario** en la matriz (fila TF17). Ningún fichero de licencia se ha
  tocado en este lote.

## Snapshot analizado

- Repositorio: `/home/claude/faustus`, rama de trabajo de este lote
  `lot/T1` en el worktree `/home/claude/faustus-T1`.
- Commit base de la reauditoría de A01–A07 y de las citas de código de esta
  carpeta: `5b72467` (`master`), según lo fijado en
  `CONTRATO_PARIDAD_1.md`.
- El baseline general de capacidades (`01_FAUSTUS_BASELINE.md` del paquete)
  fue auditado contra un checkout distinto (commit `98b9f17...`, ruta
  `D:/LocalAI/odysseus`); sus citas de fichero:línea se han vuelto a
  comprobar de forma puntual contra este árbol antes de reutilizarlas en
  `MATRIZ_PARIDAD.md` — donde no se pudieron recomprobar todas, la fila lo
  dice explícitamente en vez de heredar una cita sin verificar.

## Regla del encargo: ninguna fila se cierra por existir

Una fila de `MATRIZ_PARIDAD.md` o una fila de `ESTADO_ACEPTACION.md` **no
se marca cerrada/verde porque exista una clase, una ruta o un test que
simula el mecanismo bajo prueba**. Cierra una fila únicamente un test que
ejercita código real de Faustus (TestClient contra rutas reales,
`agent_runs`/`approval_store`/`tool_approvals`/workflows reales; fakes solo
para el LLM y para procesos externos) y que aparece enlazado en la columna
`Test de aceptación` (matriz) o en la columna `Test` (estado de
aceptación). Lo que no tiene ese enlace se declara `abierta`/`pendiente`,
nunca `cerrada`/`verde`, aunque el mecanismo exista parcialmente en otro
subsistema (p. ej. workflows ya resuelve `unknown_effect` para nodos, pero
eso no cierra A05, que pide lo mismo para tool-calls de un turno de chat).

## Qué contiene esta carpeta

| Fichero | Contenido |
|---|---|
| `README.md` | este documento |
| `MATRIZ_PARIDAD.md` | las 22 filas de `04_COMPARATIVA_Y_DECISION.md` con columnas Razón / Evidencia upstream / Faustus hoy (reauditado) / Cambio-PR / Test de aceptación / Estado / Fase |
| `acceptance_cases.json` | copia sin cambios de `evidence/acceptance_cases.json` (36 casos A01–A36, `status: PROPOSED_NOT_EXECUTED`) |
| `ESTADO_ACEPTACION.md` | las 36 filas A01–A36 con su estado (`verde`/`xfail`/`pendiente`), su test y qué falta; instrucciones de cómo T2/T3 actualizan sus filas |
| `backlog.csv` | copia de `09_BACKLOG.csv` (TF01–TF26) con la columna `estado_2026-09-13` |

Convención de aceptación (marcador pytest, layout de tests, ejecutor):
ver la cabecera de `ESTADO_ACEPTACION.md` y `docs/api/acceptance_runner.md`.
