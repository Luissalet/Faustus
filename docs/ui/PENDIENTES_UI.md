# Comprobaciones de UI pendientes

Actualizado: 08-09-2026. Quitar las entradas al verificarlas.

- **QA final de los cambios recientes:** escritorio y móvil, teclado/foco, errores de red y restauración al recargar. Una configuración de viewport no cuenta como prueba visual por sí sola.

Voz, objetivos y separadores del chat se siguen en
[PENDIENTES.md](../../PENDIENTES.md), sin duplicarlos aquí.
Funciones de interfaz incompletas: [OBJETIVOS_UI.md](OBJETIVOS_UI.md).

Biblioteca comprobada con cien documentos sintéticos: el renderizado diferido nativo evita dibujar 82 subárboles fuera de pantalla (antes, ninguno), mantiene las filas en el DOM y permite seleccionar la fila 99. Las tarjetas de imagen conservan ahora su tamaño medido al desplazarse. El modo de impresión muestra todas las filas.
