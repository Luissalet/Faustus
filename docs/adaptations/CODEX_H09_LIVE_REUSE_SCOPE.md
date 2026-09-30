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


### VISITADO / IMPLEMENTADO — H09 consulta documental estricta, 30-09-2026

`4c69f252`: API opcional strict en RAGManager/VectorRAG y query_lanes_strict. Captura referencias de lanes/clientes/colecciones por llamada, count directo una vez por lane y formato de respuesta validado. Count cero conocido omite query; ausencia de lanes, fallo count/encode/query o respuesta mal formada de cualquiera lanza error, incluso si otra lane respondió. No interpreta excepción como colección vacía ni recurre al fallback keyword en strict. Conserva los defaults legacy y cálculo de límite/fusión/owner vigentes; el wrapper no añade nuevo keyword en llamadas legacy.

Selección Faustus: 67 correctas/2 omitidas en7,17s (una requiere Chroma full frente al cliente HTTP local; otra omisión legacy preexistente). QA aislada Chroma1.5.9 full: 19 correctas en0,65s, con PersistentClient temporal y RAGManager/VectorRAG reales, propietario/ausencia/edición/borrado, colección inválida y fallo de segunda lane tras respuesta real válida. Coordinador repite script:19 correctas en0,66s, tres avisos de opciones pytest de plugins no cargados; no fallos. No servicio HTTP ni cambios en Faustusenv.

QA reproducible: D:/LocalAI/tmp/chroma-strict-qa-20260930/run_strict_qa.py con venv/Scripts/python.exe de esa carpeta; evidencia strict-qa-result.json fija runtime/version/ruta y resultados. Final usa vectors explícitos3D y encoder determinista. Un intento anterior de fixture omitió embeddings en update, ejecutó MiniLM por defecto con cache existente y falló dimensión384vs3; corregido antes resultado final. No afirmar ausencia total de inferencia en aquel intento; no progreso de descarga observado.

Límites: referencias capturadas no convierten count→query en transacción atómica ni sellan namespace global. Metadata None se considera desconocida conservadoramente. DocumentSource todavía NO usa recibos de freshness ni strict por defecto; este tramo es prerequisito implementado/probado, no cierre documental universal. Referencias https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9 y código local lanes/RAG; no revisión upstream repetida. Scope4files sólo3fuentes+testnuevo, cambios ajenos preservados. Checkpoint361effbc registra objetivos evaluados y188integradas. Automatización activa mientras uso permitido.
