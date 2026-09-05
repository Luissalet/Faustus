# Estado del lote Z2 — Editor de imagen

Fecha: 05-09-2026. Rama `feat/studio-ui`.

## Brief (modo Operate con escenario: banco de trabajo)

**Trabajo.** Retocar una imagen de la biblioteca sin salir de Faustus:
arreglar una zona (inpaint, quitar), recortarla (fondo, selección),
componer otra encima (capas, mover, transformar, armonizar), ajustar
(niveles, desenfoques), cambiar el lienzo y guardar (sobre la original,
como copia, descargar, proyecto). Luis: ratón + teclado, tema oscuro, a
veces el móvil.

**Lo que hacía mal la anterior.** 262 SVG inline y un `ALPHA`; ~40
`getElementById` acoplando cada control; el panel derecho era todos los
controles de todas las herramientas con `display:none`; menús de texto
«Image ▾ Filter ▾ Save ▾»; herramientas muertas (escalar, armonizar y
estilo sin botón: solo llegaban por el cuadro de comandos, y escalar ni
así); pistas a 9px y opacidad 0,4; el cuadro de IA era un widget aparte;
el tamaño del pincel repetido en dos filas; subcapas de máscara y ajuste
apretadas en el panel de capas.

**Dirección.** Un banco de trabajo de tres columnas: carril de
herramientas a la izquierda agrupado por intención (Ordenar · Seleccionar
· Pintar · Arreglar) con ✦ en las que llaman a un modelo; el escenario en
el centro (tablero de ajedrez, zoom con rueda, desplazar con espacio,
pellizco); a la derecha un inspector con dos paneles apilados: solo los
controles de la herramienta activa, y las capas (miniatura, ojo,
opacidad, menú con lo raro; subfilas para máscaras y ajustes). Barra
superior: volver, nombre editable y medidas, deshacer/rehacer/historial,
zoom, el campo **Pide** en el centro (describe el cambio y lo ejecuta:
girar, voltear, quitar fondo, escalar, ruido, caras, enfocar, estilo), y
a la derecha Imagen · Filtro · Importar · atajos · Guardar (menú). Los
diálogos de vista previa (desenfoques, ajustes) quitan el velo y se
apartan a un lado para que se vea el resultado. Un solo momento de
movimiento: la fila de la capa nueva entra deslizándose. La confirmación
de «guardar sobre la original» dice la consecuencia («los píxeles
anteriores desaparecen») y ofrece la copia. En el móvil el carril pasa
abajo en horizontal y el inspector es una hoja con pestañas Herramienta /
Capas, por encima de la barra de navegación.

**Revisión contra los defaults.** Sin tarjetas dentro de tarjetas; la
única sombra es la del papel y la de los overlays; colores solo por
token (los del overlay del lienzo —selección, máscara, guías, asas— se
resuelven desde `--fs-ed-*` con `getComputedStyle`, así siguen al tema);
etiquetas de sección en mayúsculas de 11px como en el resto de Studio;
los sliders son `<input type=range>` nativos con `accent-color` (nada de
píldoras propias); nada depende del hover.

## Qué hay

- `studio/src/lib/pixel/` — la lógica pura del editor anterior, portada
  y tipada: `canvas.ts` (tablero, coordenadas, decodificar, base64),
  `mask.ts` (flood fill, dilatar/erosionar, suavizado chamfer, lazo,
  limpieza de borde, combinar/invertir), `adjust.ts` (brillo/contraste,
  tono/saturación, niveles, equilibrio de color, histograma),
  `filters.ts` (gaussiano con borde extendido, zoom, movimiento),
  `snap.ts` (imán y cursores), `doc.ts` (capas, máscaras, ajustes,
  aplanar, miniatura, instantáneas, proyecto JSON v2), `stroke.ts`
  (pincel/goma/clonar, escala logarítmica), `transform.ts` (girar,
  voltear, redimensionar, escalar, recortar, transformación libre).
- `studio/src/adapters/imageTools.ts` — `/api/image/*`, estilo y escalado
  por multipart, sustituir/subir en la galería, borradores
  (`/api/editor-drafts`), modelos de imagen (`/api/model-endpoints` con
  la heurística de capacidades de la anterior), `packageInstalled`,
  imágenes recientes.
- `studio/src/screens/library/editor/` — `engine.ts` (PixelEditor:
  documento, herramientas, selección, historial, composición en dos
  lienzos; React se suscribe con `useSyncExternalStore`), `Editor.tsx`
  (portada y banco de trabajo, carga, borradores, IA, guardar, atajos,
  Pide), `Stage.tsx` (zoom/pan/puntero/soltar), `ToolPane.tsx`,
  `LayersPane.tsx`, `dialogs.tsx` (tamaño del lienzo, desenfoques,
  ajustes, importar de la biblioteca, atajos), `controls.tsx`, `ask.ts`,
  `runner.ts`; `screens/library/editor.css`.
- Ruta `/library/edit` (routes.ts, app.py, AppShell con `data-screen=
  'editor'` cuando hay `img|draft|new`); el botón Editar del visor ya se
  ve; `Viewer` → `/library/edit?img=`.
- `DECISIONES_UI` §10.1 se cumple: ningún DOM ni CSS de `static/js/editor`
  sobrevive; solo la lógica.

## Verificado en el 7001

Imagen de prueba subida por la API; abrir desde el visor → editor
800×600; pincel (trazo suave), borrador guardado solo (fila en
`/api/editor-drafts` con miniatura) y retomado al recargar («Se retomó tu
edición anterior»); varita → rectángulo verde seleccionado (tinte), Copiar
a capa (recortada a su contorno), Transformar → rotación 60,5° con vista
previa y Aplicar; Recortar → 622×430; Filtro → Brillo/Contraste con vista
previa en directo y subfila de ajuste (ojo, opacidad, fundir, borrar);
Pide «sharp» → Enhance 65 % → capa «Sharpened» del servidor; Quitar fondo
→ aviso rembg no instalado + enlace; Historial → saltar a «Crop» (rehacer
activo); menú de capa (renombrar, duplicar, bloquear, mover, fusionar,
máscara, ajustes, borrar); Guardar como copia → imagen nueva en la
biblioteca (borrador borrado, punto ámbar fuera); inpaint: máscara
pintada (subfila Mask 1), Generar → aviso del servidor «No image
generation endpoint configured…»; portada → Lienzo nuevo 1536×1024
(Fondo + Edición) → Importar desde la biblioteca → capa centrada → mover
con imán; móvil 420px: carril abajo, hoja Pincel/Capas sobre la barra.
19 tests de Studio en verde.

## Crítica

- P1 corregido: el panel de la herramienta salía de 40px (grid con
  `minmax(0,auto)`); el inspector es flex: herramienta hasta el 60 %,
  capas el resto.
- P1 corregido: la lista de sugerencias de Pide quedaba bajo el
  escenario (`backdrop-filter` crea contexto de apilamiento sin z-index).
- P1 corregido: en el móvil la hoja tapaba la barra de navegación y el
  `padding` del carril (0,2,1) volvía a pisar el `padding: 0` de la
  pantalla completa.
- P2 corregido: los diálogos de vista previa con velo no dejaban ver lo
  que previsualizaban.
- P2 corregido: la capa copiada de una selección ocupaba todo el lienzo.
- P2 corregido: los avisos de error llevaban el icono de éxito.
- P3 abierto: «guardando…» explícito (PENDIENTES 95); Cookbook (93).
