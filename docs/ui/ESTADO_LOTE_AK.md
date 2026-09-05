# Estado del lote AK — Notas: dibujar, fotos y varias a la vez

Fecha: 05-09-2026. Rama `feat/studio-ui`. Cierra la fila «Notas» de PARIDAD
§1, que era **Parcial** por cinco cosas: dibujar en lienzo, foto adjunta,
fondo de imagen propio, selección múltiple y edición a pantalla completa.
Quedan dos filas Parcial (Calendario y Memoria).

## Brief (banco de trabajo)

**Trabajo.** Apuntar algo que no son palabras: un croquis, una flecha sobre
una foto, un plano de la estantería. Y, cuando hay veinte notas, poder
hacerle lo mismo a seis sin abrirlas una por una.

**Lo que había.** El lienzo de la anterior era bueno de verdad —lápiz,
goma, texto en tres tamaños, línea, círculo, deshacer de 30, y una foto
debajo— pero vivía dentro de `notes.js` (5.368 líneas) con `eraser`,
`text` y `line` como booleanos separados. Su propio comentario reconoce el
resultado: «Single source of truth for what clicks/drags do… (the bug that
made T appear broken after using the eraser)». Y la barra era una fila de
botones diminutos sin etiqueta, con el selector de color nativo parcheado
en caliente.

**Dirección.** Un `tool`, no tres booleanos, así que hay una sola verdad.
Ocho tintas fijas en vez de un selector de color (una nota no necesita
16 millones), tres grosores, y la barra con etiquetas de verdad. El resto
—foto, fondo, selección múltiple— sigue el patrón que ya usan Galería y
Biblioteca, para que se aprenda una vez.

## Lo que se hizo

### `studio/src/lib/paint.ts` (nuevo)

La parte pura: el tamaño del lienzo, las tintas, los grosores y los tamaños
de texto (literales de píxeles, que en un `.ts` es donde la guarda de
colores los espera), `pointIn` (dónde cae el dedo en un lienzo que se
dibuja a 600×320 pero se muestra al ancho que le den), `radius`, `isDrag`
(un toque no es un arrastre, así que una figura de un toque no deja nada),
`pushUndo` (pila acotada a 24), `isBlank`, el sentinela `bg:<url>` con
`backgroundOf`/`asBackground`, y `safeImage`, que solo deja pasar subidas
del propio servidor y `data:image/`.

### `studio/src/screens/notes/Draw.tsx` (nuevo)

El lienzo. Lápiz, goma (que pinta papel, no borra de verdad, igual que la
anterior), línea y círculo con vista previa desde una instantánea —así el
arrastre no deja rastro—, y texto: se hace clic donde va y aparece un campo
en ese punto, con el tamaño que tendrá; Enter lo escribe, Esc lo descarta.
Deshacer con botón y con Ctrl+Z. Si hay foto, se pinta debajo escalada
«contain», de modo que dibujo y foto son una sola imagen, que es lo que se
guarda.

**El fallo que costó encontrar**: el lienzo se le pasaba al padre con un
callback `onReady`. Un callback es una función nueva en cada render, el
efecto de montaje se volvía a ejecutar, y el lienzo se limpiaba **en cuanto
escribías el título**. Ahora el padre pasa su propio `ref`, que es estable.

### `Notes.tsx`

- Cuarta píldora de tipo, **Dibujo**, con su lienzo en el editor.
- Botón **Foto** (que en modo dibujo dice «Dibujar sobre una foto»), con
  vista previa y quitar. Sube a `/api/upload` y guarda la URL en
  `image_url`, exactamente como la anterior, así que una nota con foto
  hecha allí se ve aquí y al revés.
- **Una imagen** como fondo de la tarjeta, junto a los puntos de color, con
  miniatura y quitar. Es el mismo `bg:<url>` de siempre; lo que faltaba era
  poder elegirla.
- **Selección múltiple** con `useSelection`/`BulkBar` (los mismos de la
  Biblioteca): casilla por tarjeta, «Todas», recolorear, archivar y borrar
  en bloque, con confirmación que dice la consecuencia («no van al archivo:
  desaparecen»).
- En móvil el diálogo del editor es la pantalla entera, y los botones de la
  barra de dibujo crecen a 34 px.
- De paso: «Etiquetas», «Recordar» y «Añadir» estaban escritos a pelo en
  castellano dentro del componente; ahora pasan por `t()`.

### `adapters/notes.ts`

`uploadNoteImage(blob)` y `uploadCanvas(canvas)`, ambos sobre el
`uploadFiles` que ya existía para el compositor.

## Pruebas

- `studio/checks/paint.check.mjs` (nuevo) + `tests/test_studio_paint_js.py`:
  la aritmética del puntero (incluido un lienzo con caja de tamaño cero, que
  no puede dividir entre cero), radio, toque contra arrastre, la pila de
  deshacer acotada por el extremo correcto, papel en blanco, el sentinela
  `bg:` y qué URLs pueden ser una imagen (`javascript:` y `data:text/html`
  no).
- `tests\test_studio*.py`: 22 en verde (eran 21).

## Verificado en el 7001

Nota de dibujo: trazo a lápiz, círculo azul y línea, guardada como
`/api/upload/…png` y visible en su tarjeta. Foto adjunta subida y mostrada
en el editor y en la tarjeta. Fondo de imagen propio elegido desde el
editor y pintado en la tarjeta. Selección múltiple: casillas, contador,
recolorear, y borrado en bloque con su confirmación, que dejó la pantalla
fuera del modo selección.

## Lo que queda

- El texto del lienzo no se puede mover ni editar una vez escrito: se
  escribe y ya es píxeles (como en la anterior). Deshacer lo quita.
- La goma pinta blanco, así que sobre una foto «borra» a blanco en vez de
  destapar la foto. Es lo que hacía la anterior.
- Una nota de dibujo no se puede reabrir para seguir dibujando encima con
  el trazo anterior separado: se reabre con la imagen debajo, que es lo
  mismo que hacía la anterior.
