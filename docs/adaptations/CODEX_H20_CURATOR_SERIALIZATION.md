# H20: serialización de curaciones dentro de un proceso

## Problema y cambio

La reproducción de `CODEX_H20_CURATOR_SCOPE.md` mostró dos curaciones con
snapshots distintos que elegían supervivientes opuestos. Cada una guardaba
su superviviente y borraba el de la otra: SQLite reabierto quedaba sin filas,
aunque ambos informes declaraban una memoria activa.

`memory_curator.curate` ahora adquiere un único `threading.RLock` compartido
antes de seleccionar filas y lo mantiene hasta terminar todas las pasadas y
el informe. El contexto libera el bloqueo también si una pasada falla;
`safe_curate` conserva su contrato de devolver el error. La lógica de ámbitos,
vigencias, ranking, fusión, maduración, inversión y poda no cambia.

El bloqueo es global para las curaciones del módulo, no por propietario:
dos ámbitos visibles distintos pueden seleccionar las mismas filas globales.
La conexión y los commits individuales de `memory_engine` permanecen intactos.

## Evidencia

`tests/test_memory_curator_concurrency.py` usa dos threads, SQLite temporal
real y vectores desactivados. La sincronización observa el intento efectivo
de adquirir el bloqueo, sin sleeps ni barreras que requieran la entrada del
segundo curator para liberar al primero. Los waits tienen límite y la pausa
del primero se libera en `finally`, incluso al fallar una assertion.

Antes del cambio, los dos controles iniciales (mismo ámbito y ámbitos
distintos con filas globales comunes) fallaron por selección concurrente;
el control de error pasó. Después se comprueban ambas fronteras: snapshot
seleccionado y save real completado antes de delete. El segundo curator no
selecciona hasta terminar el primero. Al reabrir SQLite queda un superviviente
con ambas evidencias; los informes indican deduped 1/0 y total_active 1/1.
Errores en selección y después de save también liberan el bloqueo y permiten
que otro thread complete una curación posterior.

Verificación: selección de concurrencia, ámbitos, adversariales temporales y
memory_engine: 118 pasan en 16,35 s. Tras ajustar únicamente el hilo de prueba
de liberación para que un bloqueo regresivo no cuelgue el cierre de pytest,
la suite nueva final: 6 pasan en 0,99 s. Sin modelos, servicios ni datos personales.

## Límites y procedencia

Esto evita el intercalado entre llamadas a `curate`/`safe_curate` del mismo
proceso que comparten este módulo. No es una transacción de lote, no coordina
varios procesos, no bloquea writers externos ni snapshots anteriores usados
por otras APIs, y no añade rollback ante fallos parciales o de indexación.
Las llamadas directas a helpers privados tampoco son una API serializada.
El coste es esperar una curación previa, incluso en ámbitos sin filas comunes.

Referencia del análisis H20: fuente Codex fijada en
https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9.
El mecanismo concreto se adapta al curator local; no afirma implementar
leases ni atomicidad durable de esa fuente. Coordinación multiproceso y
transacciones selección/fusión/borrado siguen pendientes.
