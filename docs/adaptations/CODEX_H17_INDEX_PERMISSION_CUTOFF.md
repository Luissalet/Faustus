# H17 parcial: permisos antes del corte final del índice

## Cambio y autoridad

El [piloto del catálogo](CODEX_H17_LOOKUP_BEFORE_LIMIT.md) dejaba un corte previo:
retrieve devolvía top-k antes del filtro. Se reproduce con read_file primero,
ask_user segundo, k=1 y ToolPolicy que bloquea read_file: el handler real devolvía
vacío aunque el índice había recibido ambos candidatos dentro de su ventana.

ToolIndex.retrieve acepta ahora candidate_filter opcional. lookup lo pasa cuando
la firma del índice admite ese argumento, utilizando el is_permitted existente.
Los adapters antiguos o sin firma inspeccionable mantienen su llamada anterior;
el filtro del catálogo sigue siendo el último control para todos los resultados.
No se construye otra política ni se modifica su comportamiento ante errores.

En el vector directo se estrecha el orden existente antes del corte final. En
la fusión se conservan los inputs originales y sus contribuciones RRF; se filtra
el orden resultante antes de top-k. Un anchor bloqueado no sustituye una plaza
permitida. En el fallback lexical, two_tier_search acepta el mismo argumento y
lo aplica después de calcular scores, promociones y rerank, antes de hits[:k].
El corpus completo sigue participando en BM25/hash; filtrar primero el corpus
habría alterado IDF y scores. Los rangos de presentación de hits se renumeran.

No se cambian pesos, score floor, desempates, tamaño k, consulta de embeddings,
anchor permitido, número de candidatos solicitados ni contratos sin filtro.
El argumento es opcional también en el buscador compartido: sus otros callers
siguen por la ruta previa. El cambio no modifica curator, capabilities o masters.

## Límite real conservado

La ventana vectorial sigue siendo min(max(k*3,24),count). No se repiten queries
para recuperar un candidato permitido que esté fuera de esa ventana. La fusión
también conserva sus pools previos: un candidato escondido tras el corte lexical
del pool de fusión puede seguir ausente. Filtrar los inputs antes de RRF o ampliar
los pools habría cambiado votos/scores y queda fuera de este piloto.

Los adapters antiguos que sólo aceptan query/k tampoco pueden reponer candidatos
que ya ocultaron. La prueba residual del piloto anterior conserva precisamente
ese contrato legacy; no certifica el nuevo ToolIndex. No se promete llenar k
si faltan candidatos permitidos dentro de los pools realmente observados.

## Verificación

Pruebas sintéticas con ToolIndex real construido sin inicializar servicios,
lanes y distancias deterministas, scoring lexical real y handler lookup real.
Se comprueban top1 permitido, denegación total, anchor bloqueado, mismos inputs y
scores RRF, mismo corpus y scores lexical, queries con la misma profundidad,
adapter legacy y candidato 25 fuera de la ventana 24 todavía ausente. No se
invocan modelos, endpoints ni datos personales.

Validación: suite nueva, lookup antes del límite/coherencia, floors, idiomas,
two_tier_search, rerank, embedding lanes, tool_serve y MCP stale:
**185 correctas en 8,51 s**, exit 0. Incluye dos casos de excepción del
predicado: vector propaga el error y el buscador lexical devuelve vacío conforme
a su contrato nunca-raises, sin repoblar candidatos sin filtro. El comportamiento
ante errores de la autoridad is_permitted sigue siendo el previo.

Fuente conceptual del [análisis H17 fijado](CODEX_HARNESS_ANALISIS_2026-09-29.md):
[Codex registry.rs b1e72963](https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core/src/tools/registry.rs).
Implementación propia sobre las rutas actuales, sin repetir revisiones upstream.

## Incremento posterior: ventana de candidatos permitidos (30-09-2026)

El residual del primer piloto se reproduce con24 candidatos vectoriales
bloqueados delante de ask_user permitido: la consulta anterior nunca lo veía.
Con candidate_filter, retrieve amplía geométricamente la ventana por lane,
reutilizando un único embedding. Termina al obtener suficientes nombres únicos
permitidos, agotar la colección, recibir respuesta corta, llegar al score floor
o al techo absoluto256. Sin filtro conserva su consulta y profundidad previas.
En fusión evita recortar el pool vectorial antes de considerar permisos y pasa
candidate_filter al buscador lexical antes de su corte; mantiene corpus BM25.

El techo256 es un límite deliberado: candidatos posteriores pueden seguir
ocultos. El freno por floor supone ranking ordenado de distancias del backend.
Adapters legacy siguen conservando su contrato; no se promete completar k.
Es lógica de recuperación, sin inferencia GPU ni tráfico a modelos reales.
Revisión independiente sin bloqueos; coordinador22 pruebas focales correctas
en1,27s (overfetch y cutoff), incluyendo query legacy, denegación total,
cap con k100/count400, encode único, respuesta corta, floor y filtro lexical.
Fuente original sigue registry.rs fijado arriba, sin revisión repetida.
Commit del incremento: `17b6bc8a`. Selección amplia:231 correctas/2 fallos de categorías MCP, reproducidos también en baseline previo cargado sólo en proceso; no suite amplia verde. Fixtures de categorías pendientes fuera de este lote.

Fixtures de categorías reparadas en `2cc9f2ab`: manager MCP sintético con
schemas y listado coherentes, comprobado por tool_registry.snapshot real.
Pruebas adicionales preservan rechazo por disabled/policy, usuario no admin
y nombre anunciado sin schema. Sentinel confirma que lookup no ejecuta
conectores. Sin cambios producción. Categorías10 correctas1,05s;
misma selección amplia H17 ahora237 correctas10,77s. Coordinador32 correctas
1,73s (categorías+overfetch+cutoff). Los2fallos previos de fixture quedan
resueltos; no implica suite completa de Faustus ni prueba con LLM/GPU.
