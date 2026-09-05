# Estado del lote AA — Biblioteca completa y editor de documentos

Fecha: 05-09-2026. Rama `feat/studio-ui`.

## Brief (modo Operate para la biblioteca; banco de trabajo para el editor)

**Trabajo.** Encontrar lo que ya existe (documentos, chats, informes) y hacer
algo con ello —abrir, duplicar, exportar, archivar, borrar, importar más—, y
trabajar un documento a fondo: dar formato, buscar, comparar versiones,
ejecutarlo, exportarlo, mandarlo por correo, o rellenar y firmar un PDF.

**Lo que hacía mal la anterior.** Un modal con cuatro pestañas y el mismo
patrón repetido cuatro veces (selección, chips, ordenar) con ids distintos;
tarjetas con seis iconos a 11px que aparecían al pasar el ratón; el editor
vivía en el panel lateral del chat con once mil líneas de `document.js` y
todo se apretaba en 300px: barra Markdown con menú «más», buscar, diff,
PDF, correo entero (destinatarios, adjuntos, programar) dentro del panel.

**Dirección.** La biblioteca sigue siendo una pantalla con la búsqueda arriba
y los modos como pestañas en la URL; cada modo es una lista de filas
(icono/lenguaje, título con resaltado, meta, desplegable con la vista previa,
Abrir, menú ⋯) con la misma barra: chips con recuento, ordenar, la acción
propia del modo (Importar/Ordenar, Ordenar con IA, Limpiar fallidos) y
«Seleccionar varios» → bandeja de acciones en lote. Un solo componente de
selección (`parts.tsx`) para los cuatro modos. El editor de documentos es
una pantalla a pantalla completa (`/documents/{id}`): barra superior con
título editable, lenguaje, hechos, vista (Editar/Ambos/Vista previa o
Páginas/Texto), Ejecutar, buscar, versiones, Exportar, Enviar por correo,
Más y Guardar; barra Markdown solo cuando el lenguaje es Markdown; el cuerpo
es editor con números de línea + vista previa a la derecha; las versiones
son un panel lateral y Comparar sustituye el cuerpo por la revisión por
bloques con palabras resaltadas. El correo no se redacta dentro del editor:
«Enviar por correo» entrega el texto a Redactar (Correo) por `sessionStorage`
y navega. Los PDF importados muestran las páginas con los campos encima y
tres modos de colocación (texto, marca, firma); la firma es un diálogo con
las guardadas, un lienzo con suavizado y subir PNG.

**Revisión contra los defaults.** Sin tarjetas con hover-only; las acciones
raras en un menú, las frecuentes visibles; confirmaciones que dicen la
consecuencia («la conversación en sí desaparece para siempre»); colores por
token (la tinta de la firma es una constante en `.ts` porque es pigmento,
no interfaz); `li` con `max-inline-size: none` porque `base.css` limita todo
`li` a 65ch (era la causa de las filas de 615px).

## Qué hay

- `adapters/documents.ts`: biblioteca (`loadDocLibrary`, `setDocArchived`,
  `duplicateDoc`, `exportDocsZip`, `tidyDocuments`, `importDocumentFiles` con
  `/static/lib/xlsx` y `mammoth` cargados solo al usarlos, `docFilename`),
  PDF (`renderPdfPages`, `pdfPageUrl`, `aiFillAnnotations`, `extractPdfText`,
  `prepareSignedReply`, `exportPdfBlob`), `runOnServer`; `Doc.sourceEmail`.
- `adapters/research.ts`, `adapters/signatures.ts`; `adapters/email.ts`
  (`leaveComposeHandoff`/`takeComposeHandoffRaw`, `COMPOSE_HANDOFF_KEY`).
- `lib/diff.ts` (LCS por líneas, bloques, aplicar, diff por palabras),
  `lib/pdfDoc.ts` (marcadores, anotaciones, valores de campos).
- `screens/library/{parts,Documents,Chats,Research}.tsx`, `Library.tsx`
  (modos y Archivo con chips), bloque AA en `library.css`.
- `screens/documents/{Editor,PdfPane,SignatureDialog,DiffView}.tsx`,
  `markdown.ts` (acciones de la barra, CSV), `exports.ts` (HTML con `Rich`
  por `renderToStaticMarkup`, DOCX con `/static/lib/docx`), `documents.css`.
- Ruta `/documents/{id}` (routes.ts, app.py, AppShell con `data-screen=
  'editor'`); las reglas a pantalla completa viven ahora en `shell.css` (las
  compartían el editor de imagen y este). `.fs-seg`, `.fs-spacer`,
  `.fs-inline` en `components.css`.
- Panel lateral de Studio: enlace «Editor completo»; Correo abre Redactar con
  `?compose=handoff`.

## Verificado en el 7001

Documentos: chips por lenguaje, importar soltando tres archivos (Markdown,
CSV, PDF por el servidor) → aparecen con su lenguaje, buscar con resaltado,
menú ⋯ → Archivar → aparece en Archivo → Seleccionar varios → Borrar con
diálogo; selección múltiple → 2 borrados. Chats: 100 chats, desplegable con
los últimos mensajes, Abrir. Investigación: vacío (sin informes; ver
PENDIENTES 99). Editor: abrir desde la biblioteca a pantalla completa,
barra Markdown (H1), vista Ambos con vista previa, Buscar, Versiones → Comparar
→ revisión por bloques (v2 → v1: 1 cambio) → Cancelar, Ejecutar un documento
Python → panel de salida con stdout y stderr, Enviar por correo → Redactar
con asunto y cuerpo. PDF: importado con curl, páginas renderizadas
(PyMuPDF instalado, PENDIENTES 98), colocar texto y firma → diálogo de firma
→ dibujar → Guardar y usar → estampada; guardado como v2/v3 en el markdown
(`## Annotations` con `signature:<id>`); `export-pdf` responde 200 con el PDF
relleno. 19 tests de Studio en verde.

## Crítica

- P1 corregido: las filas medían 615px por el `max-width: 65ch` de `li` en
  `base.css`.
- P1 corregido: el editor de documentos no era a pantalla completa porque las
  reglas de `data-screen='editor'` vivían en el CSS del editor de imagen;
  ahora en `shell.css`.
- P1 corregido: activar la página con Espacio (foco en el botón de colocar)
  creaba anotaciones en (0,0); ahora el teclado coloca en el centro y el
  texto recién colocado recibe el foco.
- P2 corregido: la barra de selección usaba `.fs-gal__check`, que es la
  casilla absoluta de las miniaturas.
- P2 corregido: `render-pages` fallaba con «responded 503» en vez del motivo
  del servidor; el adaptador lee `detail`.
- P3 abierto: paginar chats en el servidor (102); títulos en la vista previa
  (101).
