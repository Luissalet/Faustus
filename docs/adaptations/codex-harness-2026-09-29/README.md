# Evidencias del análisis del harness

- `sources.json`: 88 fuentes locales citadas, huellas SHA-256 y commits de referencia. La inclusión no implica revisión exhaustiva de todas las líneas.
- `code-mode-probe.json`: ejecución aislada del guest sobre un marcador sintético fuera de su cwd. No prueba el flujo completo de aprobación del chat.
- `tool-result-probe.json`: tabla reproducida del normalizador puro. No ejecuta herramientas ni demuestra impacto de extremo a extremo.
- La selección existente de 8 ficheros de tests dio **131 passed, 5 skipped in 21.49s**. Es un resumen del resultado observado, no un log bruto archivado ni una ejecución de toda la suite.

El informe principal detalla los límites de cada evidencia. No se modificó código de producción ni configuración. No se portó código de Codex en este trabajo.
