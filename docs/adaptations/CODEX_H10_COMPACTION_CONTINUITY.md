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

## 2. Persistencia y reapertura

Implementado: el plan de compactación diferida y la escritura inmediata conservan
el bloque literal y sus metadatos `compaction_preserve`. Antes solo se añadían al
contexto del turno en curso: el historial guardaba el resumen del modelo, perdiendo
las restricciones que este hubiera omitido. La comprobación de correspondencia
con los mensajes originales sigue ocurriendo antes de reemplazar filas.

La prueba usa SQLite temporal y el `SessionManager` real: cierra la lectura con
un gestor nuevo, verifica restricciones y metadatos en ambas rutas, y comprueba
que un historial editado impide aplicar la sustitución diferida. Tres compactaciones
consecutivas por `maybe_compact`, con resumen simulado y reapertura entre controles,
mantienen intacto el primer bloque protegido. Esto funciona porque los mensajes
`system` previos se conservan fuera del tramo que se resume; no se vuelven a interpretar
sus palabras como consentimiento nuevo.

Validación final: **99 pruebas aprobadas** entre los dos nuevos módulos,
`test_context_compactor.py`, `test_context_compactor_regressions.py` y aceptación A14.

Límite: las menciones históricas de aprobaciones pendientes son contexto fechado,
no autorización ejecutable. El almacén de aprobaciones decide su vigencia; este
cambio no revalida automáticamente el texto histórico, que podría seguir diciendo
«pendiente» después de una resolución. No crea recibos portables ni amplía permisos.

Referencia conceptual ya visitada: [análisis H10](CODEX_HARNESS_ANALISIS_2026-09-29.md)
y [Codex fijado b1e72963](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9).
Implementación propia, sin copiar código de terceros.
