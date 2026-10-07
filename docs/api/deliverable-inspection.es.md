# Inspección de entregables

`inspect_deliverable` es una herramienta de workspace de solo lectura para revisar archivos de salida reales. Acepta una ruta local `path`, un `max_content_chars` opcional entre 1.000 y 80.000 (24.000 por defecto) y, en archivos de PowerPoint, una plantilla de referencia opcional `compare_with`. Devuelve ruta resuelta, nombre, extensión, tipo, tamaño en bytes, SHA-256, datos del formato, contenido acotado, límites del parser y el alcance explícito de la inspección.

Extracción disponible:

- PDF: número de páginas, dimensiones, rotación, texto extraído por página, metadatos y número de campos AcroForm.
- PPTX: texto por diapositiva y forma, notas del presentador, medios relacionados —incluidas dimensiones/viewBox de SVG— y títulos, series, categorías y valores en caché de gráficos; también `.pptm`, `.potx` y `.potm`, con la estructura de plantilla que se describe más abajo.
- DOCX: párrafos, tablas e inventario de medios incrustados.
- XLSX: orden/nombres de hojas, celdas, cadenas compartidas/en línea, fórmulas, valores en caché y fórmulas sin valor en caché. Nunca recalcula fórmulas.
- CSV/TSV: delimitador detectado, encabezado y filas, con límite de lectura de 20.000 filas.
- Imágenes raster/SVG: metadatos del decodificador (dimensiones, formato, modo/número de fotogramas o dimensiones/viewBox SVG).
- Audio/vídeo: metadatos del inspector multimedia existente; algunos formatos comprimidos necesitan FFprobe.
- DesignCraft `.designcraft`: número de páginas/pliegos, elementos de texto/gráfico/grupo, texto de historias, registros de recursos y número/tamaño de partes del paquete que no son metadatos. Solo lee `document.json`; cuenta las demás partes sin asumir que sean recursos, no decodifica sus bytes ni sigue rutas enlazadas.
- VectorCraft `.vectorcraft`: número de mesas de trabajo y nodos de capa/grupo/trazado/texto/imagen, además del número de registros de imagen. Lee el JSON acotado, pero no decodifica píxeles ni rasteriza la geometría.

La herramienta no modifica archivos. Trata las relaciones de paquetes Office como datos, ignora relaciones externas y nunca ejecuta macros. También trata las referencias de proyectos nativos como datos: nunca abre archivos enlazados ni ejecuta código incrustado. El tamaño del archivo y de las partes, el número de entradas y elementos nativos y el contenido devuelto tienen límites. Calcula el hash del archivo inspeccionado y compara sus metadatos antes y después; rechaza archivos que cambien durante la operación.

Esto aporta evidencia estructural y de contenido extraíble, no un control de calidad. No renderiza documentos ni proyectos nativos, no verifica su diseño visual, no juzga la corrección semántica ni su completitud, no recalcula hojas, no escucha medios, no transcribe audio ni interpreta píxeles. Contar un marco, nodo, historia, registro de recurso o parte del paquete no demuestra que sea visible, editable, completo ni fiel al encargo. Una inspección correcta confirma que se extrajeron los datos indicados; no certifica que el entregable cumpla el encargo.

## Estructura de plantilla de PowerPoint

También lee la estructura de plantilla de los paquetes `.pptx`, `.pptm`, `.potx` y `.potm`, además
del texto, las notas, las imágenes y los gráficos. `facts.template` incluye:

- `slide_size`: ancho y alto en EMU y en pulgadas, el atributo `type`, un nombre común (`16:9`, `4:3` o `16:10`)
  si el tamaño coincide con uno estándar con un margen del 0,5 %, y `aspect`, la proporción de cualquier lienzo (una
  diapositiva de 20 × 11,25 pulgadas también es `16:9`).
- `masters`: cada patrón con su nombre, su tema, sus diseños y su huella.
- `layouts`: cada diseño con su nombre, tipo, patrón, marcadores (`type`, `idx`), huella y las diapositivas que lo
  usan. `unused_layouts` lista los que no usa ninguna.
- `themes`: el nombre, la paleta (de `dk1` a `accent6`, `hlink` y `folHlink`), las fuentes (titulares y cuerpo) y
  la huella.
- `template_fingerprint`: un hash que reúne las huellas de todos los patrones, diseños y temas y el grafo que los une (diseño con patrón, patrón con tema).
- `broken_links`: enlaces que no se resuelven: una diapositiva sin diseño o que apunta a uno inexistente, un diseño sin enlace a su patrón o que apunta a otro, un patrón sin tema (`from`, `kind`, `target`, `error`). Con cualquiera de ellos `template_fingerprint` es nulo.
- `unreadable_parts`: partes de la plantilla que faltan o están dañadas (`part`, `kind`, `error`). Se dejan fuera en lugar de abortar la inspección; entonces `template_fingerprint` es nulo y la comparación nunca da la plantilla por conservada.

Cada diapositiva indica además su `layout`, su `layout_name` y su `master`.

La **huella** es el SHA-256 del XML canónico de la parte (C14N, sin espacios irrelevantes). Cada identificador de
relación dentro de la parte se sustituye por aquello a lo que apunta: el tipo de relación y, en imágenes y otras partes
binarias, el hash de su contenido. Si una herramienta vuelve a serializar una parte sin cambiarla, renumera sus
relaciones o mueve partes dentro del paquete, la huella se mantiene; un cambio de contenido o una imagen cambiada da
otra.

Las partes se siguen por sus relaciones y sus tipos, no por las carpetas habituales, así que los paquetes con destinos
absolutos (`/ppt/...`) o con los temas junto a los patrones se leen igual. Los patrones y diseños sin nombre se
emparejan por la ruta de su parte.

`compare_with` (opcional) es la ruta de una plantilla de referencia (`.pptx` o `.potx`). Con ella, el resultado añade
`template_comparison`:

- `template_preserved`: verdadero si el tamaño es el mismo, ningún tema, patrón o diseño del archivo difiere de la
  referencia y ninguna diapositiva usa un diseño ajeno a ella. Que el archivo no lleve diseños de la referencia que
  no usa no cuenta en contra.
- `identical_template_fingerprint`: todos los patrones, diseños y temas son idénticos, también los que no se usan.
- Para temas, patrones y diseños: `identical`, `changed_same_name` (copia editada de una parte de la referencia),
  `missing_from_deck` y `not_in_reference`.
- `theme_changes`: diferencias de color y fuente de cada pareja de patrones (emparejados por huella, luego por nombre y luego por ruta). `theme_color_changes` y `theme_font_changes` repiten la pareja del primer patrón.
- `arcs_outside_reference`: enlaces del grafo del archivo que la referencia no tiene, como un diseño sin cambios colgado de otro patrón.
- `broken_links` y `unreadable_parts` de los dos archivos; cualquiera de ellos en el archivo o en la referencia hace falso `template_preserved`.
- `slides_on_layouts_outside_reference`: diapositivas sobre un diseño editado o que no está en la referencia.

Límites: son hechos sobre las partes del paquete. Que las huellas coincidan no demuestra que las diapositivas se vean
bien, que los marcadores estén bien rellenos ni que el texto sea correcto. El renderizado y la revisión visual son
pasos aparte.

Ejemplo:

```json
{"path":"data/output/final.pptx","max_content_chars":24000}
{"path":"data/output/final.pptx","compare_with":"data/templates/marca.potx"}
```

Los errores incluyen códigos estables como `path_not_allowed`, `invalid_pdf`, `encrypted_pdf`, `invalid_package`, `file_too_large`, `part_too_large`, `file_changed` y `probe_unavailable`.
