# Comprobaciones de UI pendientes

Actualizado: 08-09-2026. Quitar las entradas al verificarlas.

- **QA final de los cambios recientes:** escritorio y móvil, teclado/foco, errores de red y restauración al recargar. Una configuración de viewport no cuenta como prueba visual por sí sola.

Voz, objetivos y separadores del chat se siguen en
[PENDIENTES.md](../../PENDIENTES.md), sin duplicarlos aquí.
Funciones de interfaz incompletas: [OBJETIVOS_UI.md](OBJETIVOS_UI.md).

Biblioteca comprobada con cien documentos sintéticos: el renderizado diferido nativo evita dibujar 82 subárboles fuera de pantalla (antes, ninguno), mantiene las filas en el DOM y permite seleccionar la fila 99. Las tarjetas de imagen conservan ahora su tamaño medido al desplazarse. El modo de impresión muestra todas las filas.

Resultados: pruebas de exclusión de escrituras fallidas, pendientes y canceladas; persistencia y conflictos del panel correctos. Galería→chat: rechazo probado de URL externas, errores HTTP y contenido que no sea imagen; en Brave una descarga fallida muestra su estado y no crea un adjunto. Protegida la llegada tardía de una imagen cuando el usuario cambia de conversación. TypeScript y compilación correctos.

Referencias de imagen: selector y foco comprobados en escritorio y móvil de 390 px con el compositor real y una captura sintética. Ajustado el ancho móvil; el mensaje de prueba conserva el papel elegido como texto visible. Pruebas de orden de imágenes, adjuntos no visuales y nombres con saltos de línea correctas. Todos los scripts `studio/checks/*.check.mjs` pasan.

Creación de chat: comprobada la aplicación de producción contra un fallo HTTP 503 simulado, sin reenviar escrituras a la aplicación real. Muestra que está creando la conversación, desactiva el doble envío y conserva el borrador al fallar. Los adjuntos se retiran sólo después de crear el chat; las altas tardías no fuerzan el regreso a una conversación abandonada.
