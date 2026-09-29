# H05/H06 parcial: esquemas independientes al capturar el catálogo

Hallazgo nuevo, reproducido: modificar una propiedad anidada en la exportación
de `read_file` cambiaba tanto el fingerprint del snapshot como el schema nativo
global. Una actualización anidada del schema MCP cambiaba retrospectivamente el
catálogo ya capturado. Las copias superficiales conservaban listas y diccionarios
compartidos.

`ToolDescriptor` ahora copia en profundidad los schemas de entrada/salida al
parsear y exportar. Así una exportación modificada no reescribe el catálogo ni
el origen, y una actualización del proveedor aparece en el siguiente snapshot.
La forma serializada y los fingerprints para contenido sin cambios se conservan.

Tres regresiones fallaron antes y pasaron después: exportación nativa, actualización
MCP anidada y parseo/exportación de ambos schemas. Selección inicial: **46 pruebas
correctas** de aislamiento, round-trip y registro; la selección ampliada con
contratos y fixtures terminó con **118 pruebas correctas**.

Alcance: evita aliasing accidental entre origen, descriptor parseado y exportación.
No hace inmutables los diccionarios expuestos por el propio descriptor, no captura
una política por paso ni sustituye la comprobación de revocaciones al despachar.
H05/H06 completos siguen pendientes. No requiere servicio MCP real.

Fuente conceptual ya visitada: [diseño H05/H06](CODEX_HARNESS_ANALISIS_2026-09-29.md)
y [Codex b1e72963](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9).
Implementación propia; no copia de código de terceros.
