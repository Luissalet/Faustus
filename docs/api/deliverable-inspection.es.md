# Inspección de entregables

`inspect_deliverable` es una herramienta de workspace de solo lectura para revisar archivos de salida reales. Acepta una ruta local `path` y un `max_content_chars` opcional entre 1.000 y 80.000 (24.000 por defecto). Devuelve ruta resuelta, nombre, extensión, tipo, tamaño en bytes, SHA-256, datos del formato, contenido acotado, límites del parser y el alcance explícito de la inspección.

Extracción disponible:

- PDF: número de páginas, dimensiones, rotación, texto extraído por página, metadatos y número de campos AcroForm.
- PPTX: texto por diapositiva y forma, notas del presentador, medios relacionados —incluidas dimensiones/viewBox de SVG— y títulos, series, categorías y valores en caché de gráficos.
- DOCX: párrafos, tablas e inventario de medios incrustados.
- XLSX: orden/nombres de hojas, celdas, cadenas compartidas/en línea, fórmulas, valores en caché y fórmulas sin valor en caché. Nunca recalcula fórmulas.
- CSV/TSV: delimitador detectado, encabezado y filas, con límite de lectura de 20.000 filas.
- Imágenes raster/SVG: metadatos del decodificador (dimensiones, formato, modo/número de fotogramas o dimensiones/viewBox SVG).
- Audio/vídeo: metadatos del inspector multimedia existente; algunos formatos comprimidos necesitan FFprobe.

La herramienta no modifica archivos. Trata las relaciones de paquetes Office como datos, ignora relaciones externas y nunca ejecuta macros. Los tamaños de archivo y partes, el número de entradas y el contenido devuelto tienen límites. Calcula el hash del archivo inspeccionado y compara sus metadatos antes y después; rechaza archivos que cambien durante la operación.

Esto aporta evidencia estructural y de contenido extraíble, no un control de calidad. No renderiza documentos, no verifica su diseño visual, no juzga la corrección semántica ni su completitud, no recalcula hojas, no escucha medios, no transcribe audio ni interpreta píxeles. Una inspección correcta confirma que se extrajeron los datos indicados; no certifica que el entregable cumpla el encargo.

Ejemplo:

```json
{"path":"data/output/final.pptx","max_content_chars":24000}
```

Los errores incluyen códigos estables como `path_not_allowed`, `invalid_pdf`, `encrypted_pdf`, `invalid_package`, `file_too_large`, `part_too_large`, `file_changed` y `probe_unavailable`.
