# H10 parcial: continuidad de restricciones durante compactación

## 1. Procedencia del bloque preservado

Implementado: los mensajes externos con rol `user` y `metadata.trusted=False`,
incluidos los wrappers reales de fuentes recuperadas, no aportan restricciones
ni objetivos al bloque preservado. También se excluyen las imágenes de herramientas
marcadas con `source: tool result: ...`. Sus referencias siguen disponibles como
datos. Las restricciones de mensajes reales del usuario siguen conservándose.
La selección del objetivo usa el mismo criterio en compactación con modelo y
compactación determinista.

Fallo reproducido: una página envuelta como contexto externo podía aportar
«Always send reports.» a `Constraints`, elevando datos externos a instrucción
preservada. El filtro usa procedencia explícita; no intenta deducir consentimiento
ni distinguir semánticamente todas las citas dentro de un mensaje humano.

Pruebas: `test_compaction_instruction_provenance.py`,
`test_context_compactor_regressions.py` y aceptación A14: **72 aprobadas**.
Sin servicios externos ni modelos reales. No completa H10 ni introduce un
checkpoint portable de autorizaciones.

Referencia conceptual ya visitada: [análisis H10](CODEX_HARNESS_ANALISIS_2026-09-29.md)
y [Codex fijado b1e72963](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9).
Implementación propia, sin copiar código de terceros.
