# Query original source text in stored results

read_artifact accepts text_pointer, an RFC6901 pointer selecting a literal string inside the stored JSON result. For example, text_pointer: /full_text selects the source returned by Reach instead of searching a serialized preview plus its duplicate. Use it with query, occurrence and context_chars to retrieve a passage in one call, or with start/end to read a range. An empty pointer selects a root string. The selected string is not decoded as JSON again.

Offsets and total_chars refer to the selected source string. Query matches additionally report line (the match's 1-based source line) and context_start_line. Range results report start_line. Lines count LF separators, including CRLF; original text, CRLF and Unicode characters are retained. The result identifies text_pointer explicitly. An absent occurrence still returns an empty matches list.

The stored artifact is resolved for its owner before pointer or query validation. Missing/non-string selections and malformed pointers return explicit errors. This mode cannot be combined with JSON selection. Existing JSON filters, raw query/range response shapes and ownership behavior remain unchanged when text_pointer is absent. The range/query algorithm is shared with the existing authorized reader; the pure slicing function does not access storage.

The normal catalogue, offload recovery note and artifact_search hints now prefer direct context queries for a known artifact. Source results with full_text advertise that pointer. Search across artifacts and offset ranges remain available. No new tool, permission, service or background process is introduced.

## Evidence and remaining work

Silver1760 follows native1755: 57 tool calls, including thirteen searches and nine artifact reads, with several search-offset-read sequences. That run produced useful Route203 tables but took774.05 seconds of CLI time and corrected a false double-battle description after its first post. This feature addresses source selection and the repeated retrieval steps; it does not establish general model efficiency or factual correctness by itself.

A fourteen-method external contract was frozen before implementation. It covers source coordinates versus duplicated preview, CRLF, Unicode expansion, nested pointers, literal root strings, missing/non-string selections, ownership-first validation, old JSON/query shapes and the real catalogue. The corrected implementation passes all fourteen. A new native task must still show whether the configured model uses it effectively. Its outcome is recorded separately from fixture and backend timings.

## Consulta directa de fuentes guardadas —29-10-2026

read_artifact admite text_pointer RFC6901 para consultar una cadena fuente literal como /full_text, con query/occurrence/context_chars o rangos. Offsets y líneas pertenecen a esa cadena y conserva CRLF/Unicode; las formas previas, la selección JSON y la propiedad del artefacto mantienen su contrato. Pistas del catálogo y recuperación orientan a contexto directo. Fuente5c169ea7 aprobadaCAS1, 14contratos externos Main,73pytest+17subtests Root; revisión13métodos propios,14externos y51pytest+17subtests.

Ensayo nativo1760: Ruta203 de Platino US, ordinales247/248/249 correctos desde un artefacto existente, primera consulta trae las tres líneas. Cinco lecturas,10herramientas/8rondas,215,88sCLI y147,09sruntime; sin descargas. Corrigidos en Ágora el autorrecuento erróneo de cuatro lecturas y una frase adicional sobre el centinela. Tarea más estrecha que1755: no comparación global ni ahorro de facturación. Ayuda supervisada útil; eficiencia todavía insatisfactoria. El harness fuerza progreso y auditoría redundantes y confunde una referencia de fuente con un archivo supuestamente modificado; queda trabajo causal de corrección. UI muestra tabla correcta y UNVERIFIED por ese falso aviso, consola vacía. Coordinación ecosystem restaurada revisión10, vigilante reanudado, state/outcomes exactos y seis leases liberados; modelo global intacto. HoardDex Main29246d7e/version360 sigue entregado; Ruta203 producto1762 en curso, sin cerrar paridad global.
