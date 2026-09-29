# H09 parcial: identidad de reutilización del paquete live

Implementado en el motor opcional de contexto, que continúa desactivado por
defecto. Antes, `deliver_round` reutilizaba el paquete anterior si cabía en el
presupuesto, aunque la nueva solicitud deshabilitara memoria personal, excluyera
una fuente o perteneciera a otro ámbito. Se reprodujeron esos tres casos con
compilador simulado y el código real de entrega: una única compilación y el mismo
contenido privado en las respuestas posteriores.

Ahora un SHA-256 privado representa ejecución (owner/proyecto/sesión/workspace/
run/turn/branch/council), actor, tarea, política, referencias explícitas y consumidor.
Solo se reutiliza el paquete si coincide esa identidad y sigue cabiendo. Los
paquetes anteriores sin identidad se recompilan. La identidad queda en el objeto
interno de continuidad, fuera del mensaje, metadatos y reporte público; no duplica
consultas ni otros contenidos sensibles en logs.

`request_id`, `created_at` y presupuesto solicitado no forman parte de la identidad:
los primeros describen el intento y el presupuesto efectivo se comprueba por
separado antes de reutilizar. El timeout de compilación tampoco cambia el snapshot.
Se mantienen las optimizaciones de reutilizar los mismos bytes cuando la solicitud
relevante no cambia, incluso en varias rondas consecutivas.

## Evidencia

**53 pruebas aprobadas** en `test_context_delivery_reuse_scope.py` y
`test_context_engine_wiring.py`: cambios de ámbitos, actor/modelo, exclusiones,
memoria/proyecto deshabilitados, frescura, tarea y referencias; compatibilidad sin
identidad; continuidad ante IDs/fecha/timeout/presupuesto; ajuste de presupuesto
insuficiente, aislamiento y fallbacks existentes. Sin servicios externos.

## Límites

Es validación de la identidad declarada en la solicitud, no un mecanismo de
invalidación por versiones de fuentes o epochs de revocación. Un contenido
actualizado con idéntica solicitud puede seguir siendo el snapshot deliberado
del turno. No cambia el fallback heredado del caller ni permite retirar información
que el modelo ya recibió. Se comprobó el desajuste de propietario en la API interna;
no se demostró que un usuario pueda cambiar de propietario dentro del loop real.
No completa H09 ni cambia permisos.

Referencia conceptual ya visitada: [backlog H09](CODEX_HARNESS_ANALISIS_2026-09-29.md)
y [Codex fijado b1e72963](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9).
Implementación propia; sin nueva revisión upstream.
