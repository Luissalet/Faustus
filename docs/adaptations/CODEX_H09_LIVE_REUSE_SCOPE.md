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

Checkpointcb9ce030 registraH17a1b488e1 y alinea tabla: último drain steering evaluado con descarte explícito, objetivos sin fallo de caché reproducido. Chroma1B segunda omisión exacta: tests/test_rag_remove_directory_scope.py::test_vectorrag_remove_is_path_bounded — POSIX-only: fixture uses POSIX-shaped absolute paths. No ocultar esa omisión ni atribuirla al nuevo strict path.


### VISITADO / IMPLEMENTADO — H09 recibos de consulta Documents y adaptador vigente

`0f710cf9`: compiler captura en task padre el resultado normalizado de consultas DocumentSource estrictas reales, incluyendo vacío; recibo privado liga request/query/top/owner/proyección al manager y runtime de lanes/colecciones/clientes. Validación usa el manager capturado sin RAGconstructor/migración/reindex/uses. Captura identidad antes/después: URL/credenciales sólo hashesprivados, model/dim/bounds y referencias del transporte/colección materializadas; no getters que infieran/carguen modelos. Error/degraded/timeout/cuerpo manual sin backing/overflow>16 ⇒ unknown/recompila. Fuera de capture legacy igual; initial strictfailure puede render fallback existente degradado y sin recibo válido. Gates política/owner siguen antesstore. Tresfixtures fakecompiler explicitan fuente no planeada por hook real.

Before: compilador+SQLiteledger+Chroma real con wiring4c69f252 conservaba documento viejo tras cambiar cuerpo mismoID. Final286 correctas/2omitidas16,04s (ambas pruebas Chroma full no disponibles en Faustusenv thin); QA40 correctas1,42s. Coordinador40 correctas1,41s. Consulta real prueba reuse sin cambios/0→nuevo/update y borrado colección; unit/integración añade topmembership/otroowner/fallo parcial/configidentidad/timeouts/legacy/noIO/manual/concurrencia.

Pregunta nueva y followup `169c5bd2`: cambiar originaladapter._manager podía revalidar manager antiguo aún intacto. Reprocompiler+SQLite confirmado stale reuse. Recibo ahora conserva adaptador original; verifica su manager antesparent/workerquery/afterquery/aftergather sin abrir store para guard. Reemplazo manager por otro (mismoowner/otroowner), cambio antesencode y despuésquery ⇒ unknown/no oldquery o resultado descartado. Final133 correctas/1omitida5,79s; QA44 correctas1,47s; coordinador44 correctas1,45s/2avisos de opciones timeoutplugins no cargados. 25Documentcases+19strictRAG en QA. Script D:/LocalAI/tmp/chroma-strict-qa-20260930/run_document_qa.py y document-qa-result.json, repro previo test_document_before.py/document-before-result.json. Scope8filesprimerlote+3followup sólopropios.

Límites: snapshot de consulta y selección, no versión universal de toda colección. Documento fueraquery/top/owner puede no invalidar. Pool/factories reemplazados y planificación/configDB no recargada siguen sin epoch universal; no atomicidad entre checks y encode/consulta ni proveedorinternals. Owner-only de Documents conservado, projectid sólo scopecompilador, no filtrotabla añadido. Sin servicios HTTP ni GPU, QAfinal vectors3D explícitos. Fuente https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9 y contratoslocales, upstream no re-revisado. Pilotos visitados/cerrados, H09MemEngine y entrega/transversal pendientes. Uso95% permitido y automatización activa.


### VISITADO / IMPLEMENTADO — H09 prerequisito MemoryEngine standing read-only

`7e13c7f6`: engine.context_snapshot(owner,project,statuses,now) abre SQLite mode=ro/query_only/BEGIN y obtiene publicprojections de filas y conflictos en una misma transacción. Strict conflicto usa conexión del caller y propaga errores; defaultslegacy conservados. SchemaSELECT incluso vacío evita certificar tabla ausente/incompatible. No _db/_open/_connect de writer, mkdir,DDL/DML,migrate,quarantine,uses ni vector enstrict. Filtrosactuales owner=? OR global'' y project=? OR global'' conservados; no filtro de sesión inventado.

Final211 correctas18,63s en6módulos,15nuevas; coordinador15 correctas1,19s. SQLite real: DELETEfixture bytes/archivosintactos; corrupt/missingstore/tabla/conflicts/errores noemptycertification; fila/conflicto coherentes bajo otrocommit durante lectura. WALwriter abierto con nuevo texto commit sincheckpoint: reader veúltimocommit, DBprincipalbytesintactos. WALwriter cerrado sin sidecars: reader puedecrear operativos -wal/-shm; se ajusta requisito anteriornojournals a evidencia. No immutable=1, que podría omitir WAL y leer estado obsoleto. Garantía de no escrituras de estado de app, NO ceroactividadfilesystem/archivosauxiliares. Sin limpiar sidecars personales manualmente.

Repro anterior de staleMemoryEngine válido: fuente standing, compilador/SQLite reales, regla procedural original→changed mismoID/updated_at; second reused=True/texto viejo. Intento previo code_change sin fuente renderizada descartado como prueba inválida. Este commit implementa sólo APIprimitiva; adaptador/recibo standing/hybrid freshness NOcerrados. Siguiente standingreceipt en evaluación, hybridestricto vectorstore pendiente; cliente fullChromaQAaislado ya disponible no servidoHTTP. Fuente https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9 y memoryengine/conflictslocales; no revisión upstream repetida. Piloto prerequisito visitado/cerrado, globalH09parcial. Checkpoint8629ce96 recovery28385388/cancelda7412f6, uso96% permitido y automatización activa, ajenos preservados.


### VISITADO / IMPLEMENTADO — H09 vigencia y secretos en memoria standing

`c4de4930`: selección de reglas/anti-patterns sin consulta ya excluye sensitivity=secret y filas fuera de valid_from/valid_until, con instanteUTC actual compartido por selección y score. Sigue los guardas de la ruta híbrida y conserva anti-patterns sin filtro de score, ownerblank global-only y políticaoff antesDB. Beforeactualgather+SQLite:3fallos secret/expired/future entraban en contexto y2positivos correctos; final183 correctas17,68s en5módulos,5nuevas. Coordinador5nuevas0,88s correctas. No reloj congelado para revalidation, no cambio de permisos del usuario ni filtro de sesión inventado.

Este lote2filesadapter/test cierra guard de selección, NO standing freshness aún: recibo enimplementación. Fuente https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9 y contratos MemoryEngine híbridoslocales; no revisión upstream repetida.


### VISITADO / IMPLEMENTADO — recibo de consulta MemoryEngine standing

`99d27ee5`: la rama standing captura el resultado normalizado de la consulta (incluido vacío) después del worker y revalida filas/conflictos con context_snapshot estricto antes de reutilizar. Cambio de texto conservando ID/updated_at y vacío→nuevo ya fuerzan recompilación. Captura y valida ruta de DB antes/después; vigencia usa reloj actual. No writer, vector, migración, reindexación ni uses durante revalidación. Fallo inicial puede conservar respuesta legacy degradada, pero el recibo queda desconocido; manual mem, error, timeout y >16 consultas no certifican reutilización.

Final del agente: 300 correctas y 1 omisión en18,37s, 11 módulos, 16 nuevas. Coordinador: standing16 + mainretry18, **34 correctas en29,57s**. SQLite/compilador reales temporales; sin modelos ni DB personal. Tres fixtures declaran fuentes no planificadas y referencias genéricas usan fixture: para no fingir recibos mem:. Fuente normativa/default MemoryEngineSource, sin promesa para subclases arbitrarias ni epochs globales de pools/configuración. SQLite puede usar sidecars WAL/SHM; no escritura de estado de aplicación.

Piloto standing cerrado; **consulta híbrida sigue desconocida**. Nueva evaluación real: MemoryVector devuelve parcial/vacío ante error de una lane; petición lanes=('lexical',) consulta vector store igualmente. Primitiva strict opt-in en implementación separada; política lexical-only pendiente. H09 general parcial (versiones/configuración universales y entrega efectiva pendientes). Fuente original ya visitada: https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9; adaptación propia, sin repetir radar41.

Precisión del lote99d27ee5: única omisión test_document_reuse_freshness.py::test_real_chroma_compiler_freshness (requires isolated full Chroma QA runtime; entorno principal chromadb-client). Ninguna prueba standing omitida. QA documental real ya44 correctas enruntime aislado tras169c5bd2. Checkpoint documental69b1918b.


### VISITADO / IMPLEMENTADO — MemoryVector strict opt-in

`c0514371`: MemoryVectorStore.search(strict=True) reutiliza query_lanes_strict: count una vez por lane capturada, vacío sólo si count conocido0; cualquier error count/encode/query/shape rechaza resultado entero en vez de certificar parcial. Query/k válidos requeridos incluso vacío. Distancia→score, prioridad custom, ranking/dedupe y defaultlegacy conservados. Sin initialize/reconnect/migrate/reindex durante consulta strict.

Final126 correctas/1 omitida15,23s en6módulos. 24 nuevas:23 enruntime principal y1 Chroma real omitida por cliente thin (test_memory_vector_strict_queries.py::test_real_chroma_query_absence_and_partial_failure). QA aisladafullChroma1.5.9:24 correctas0,69s; coordinador24 correctas0,67s con3 warnings config asyncio_mode/timeout/timeout_method por plugins no cargados. PersistentClient temporal real, vectors3D explícitos/encoder determinista, consulta/vacío/colección borrada y lane válida junto lane fallida. Sin modelos, servicio ni DB personal. Script/artifact D:/LocalAI/tmp/chroma-strict-qa-20260930/run_memory_vector_qa.py y memory-vector-qa-result.json.

Primitiva cerrada; aún no conectada a recibo híbrido ni lectura SQLite estricta híbrida. Política lexical-only en incremento separado. Fuente original ya visitada: https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9; implementación propia, no revisión upstream/radar repetida.


### VISITADO / IMPLEMENTADO — ámbito y vigencia al reabrir memoria por ID

Nueva pregunta by-ID frente a búsqueda: fetch_ref real→MemoryEngineSource.fetch→SQLite permitía owner vacío leer privado, proyecto diferente/vacío recuperar fila de otro workspace, secret y ventana no vigente. Ranking posterior no recupera scope: candidate usa proyecto del request y no lleva sensibilidad/ventanas completas. Repro corregido de fixture:6 fallos reales y6 positivos antes cambio; dos intentos previos usaron nombre de columna inexistente last_used_at/last_used y no certifican esos positivos.

`9dc7eeed`: _fetch rechaza storedowner no vacío distinto del request (también ownerblank), storedproject no vacío distinto de scope actual, secret y !is_valid_now. Globales blank siguen permitidos; workspace y fallback project_id conservados. Gate incognito antes get_item. Sin touch ni cambio acceso/last_accessed. Lote2filespropios,12nuevas; selección98 correctas3,73s (sources/standing/freshness), segunda selección con recall real + deferredphase **48 correctas2,72s**. Una selección inicial tenía test_context_engine_expand.py inexistente:0 tests, sustituida por recall existente. DB temporales reales, sin GPU/DB personal.

Se cierra guard de reabrir por ID, NO lectorreadonly de get_item (usa _db legacy), atomicidad snapshot ni epochs universales. Fuente original: https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9, contratos locales de memoria/contexto; nueva pregunta no repetición de revisión cerrada.
