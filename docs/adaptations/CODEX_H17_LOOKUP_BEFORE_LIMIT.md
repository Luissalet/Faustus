# H17 parcial: permisos antes del límite del catálogo

El handler real lookup_tools con nombres ordenados `[read_file, ask_user]`,
`k=1` y ToolPolicy que bloquea read_file devolvía una lista vacía. El catálogo
recortaba primero y serve eliminaba después el candidato bloqueado. El mismo
fallo se reprodujo con hints y con un pool recuperado que incluía ambos nombres.

Ahora serve pasa a search_catalog un filtro de candidatos basado en el mismo
is_permitted existente. `_add` lo aplica antes de añadir candidatos y antes
del corte final. Conserva orden, scores externos, schemas, pesos, k y autoridad
de permisos. El argumento opcional no cambia los callers directos del catálogo
que no lo suministran. No amplía permisos ni cambia el selector global.

El índice tiene otro corte: retrieve(query,k) puede devolver sólo el candidato
bloqueado y ocultar el permitido que viene después. Su API no acepta allowed_names
ni predicate. Cambiarlo exigiría tratar lanes vectoriales, lexical fallback,
fusión RRF y anchor reservado; el tamaño del pool y anchor dependen de k.
Solicitar un k mayor no preserva necesariamente el mismo ranking. Este incremento
no infla k ni reescribe el índice: ese corte interno permanece pendiente.

Las pruebas usan el handler y ToolPolicy reales con índices/hints sintéticos.
Comprueban nombres, hints, candidatos explícitos y pool recuperado, orden con
política ausente, contrato directo previo y denegación total. Una prueba residual
exige vacío con índice top1 bloqueado y ask_user con top2, verificando también
solicitudes exactamente k=1 y k=2. No invocan modelos, servicios ni datos personales.

Validación: suite nueva más `test_lookup_policy_coherence.py`, `test_tool_serve.py`,
`test_tool_serve_bare_names.py` y `test_mcp_stale_tools.py`: **50 correctas en
3,43 s**, exit 0. Un primer intento situó por error el argumento opcional en
serve; se corrigió a search_catalog antes de esta validación. El iterable disabled
sigue congelado en serve antes de crear el predicado.

Continuación propia del [análisis H17 fijado](CODEX_HARNESS_ANALISIS_2026-09-29.md)
y [piloto anterior](CODEX_H17_LOOKUP_POLICY.md), con referencia conceptual
[Codex registry.rs b1e72963](https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core/src/tools/registry.rs).
No certifica cambios de catálogo después de lookup ni disponibilidad universal.
