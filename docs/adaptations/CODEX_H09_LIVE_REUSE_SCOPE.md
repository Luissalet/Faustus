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


### VISITADO / IMPLEMENTADO — H09 permiso de documentos personales, 30-09-2026

`d71a6b10`: DocumentSource comprueba `allow_personal_memory` y propietario antes de abrir/consultar su almacén, tanto en búsqueda async como llamada síncrona directa. Un manager ya abierto tampoco se consulta con política desactivada. Planner clasifica `documents` como fuente personal y la excluye cuando se deniega esa política. Conserva consulta, límite y filtro owner de la búsqueda permitida; no se incorpora filtro de proyecto inexistente.

Antes: 4 regresiones fallaban y una pasaba (apertura denegada/owner vacío y documentos ofrecidos con política personal desactivada). Después: 210 pruebas correctas en 13,67 s, incluyendo 8 nuevas. El fixture anterior que esperaba documentos personales permitidos en incognito se actualiza al contrato de privacidad y conserva asserts de causa y caso positivo. Coordinador: 8 nuevas correctas en 0,79 s. Fuente real adapter/planner con manager sintético; no prueba de Chroma ni inferencia/GPU. `available()` conserva imports previos: la garantía es no abrir/consultar el store, no ausencia universal de IO de imports.

Fuente original https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9 y contratos locales; revisión upstream no repetida. Piloto de permisos visitado/cerrado. Freshness documental y consulta estricta siguen PENDIENTES: collection.count con OSError puede acabar count=0/query_lanes=[] y healthy=True, reproducido con EmbeddingLane/query_lanes reales y collection sintética. Runtime Chroma instalado es cliente HTTP; PersistentClient temporal rechazado. No certificar vacío ni revalidación hasta resolver ruta estricta y QA aislada real. Sin activar servicios personales. Uso 94% permitido, automatización activa; preservar cambios ajenos.
