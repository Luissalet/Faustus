# Estado del lote Z — Galería (gestión y visor)

Fecha: 05-09-2026. Rama `feat/studio-ui`.

## Brief (modo Experience para la rejilla, Operate para el visor)

**Trabajo.** Encontrar una imagen, decidir qué hacer con ella (guardar,
etiquetar, mandarla a una conversación, girarla, borrarla) y organizar
muchas a la vez. Las imágenes se juzgan mirando: la rejilla lleva.

**Lo que hacía mal la anterior.** Un modal con pestañas (imágenes,
álbumes, borradores del editor), filtros repartidos entre cabecera y
píldoras, la ficha de la imagen sustituía a la rejilla y cada acción era
un botón `gallery-detail-back` más; el editor compartía DOM con la ficha.

**Dirección.** La rejilla con miniaturas 4:3 recortadas (nada de tarjetas
que crecen con la foto), favoritas y álbumes como chips con recuento y
un chip punteado para crear álbum, etiquetas como filtro apilable, orden
y subida en la barra, arrastrar fotos sobre la rejilla. El visor es un
lightbox: la imagen en un escenario oscuro con flechas y teclado, y a la
derecha un inspector callado con las acciones arriba y los datos abajo.
La selección convierte la rejilla en bandeja (álbum, favorita, zip,
borrar). Un solo movimiento: el visor entra con `fs-fade-in`.

## Qué hay

`adapters/gallery.ts` (todo `/api/gallery/*`), `screens/library/Gallery.tsx`,
`screens/library/Viewer.tsx`, `library.css` (bloque Z), `Library.tsx` (modo
imágenes y las tarjetas de imagen del «Todo» abren el visor), `Studio.tsx`
(`?image=&name=` adjunta una imagen al compositor). `Button` e `IconButton`
aceptan `ref` y el resto de atributos: los triggers `asChild` de Radix
(menús) no abrían porque el botón se tragaba `onPointerDown`.

## Verificado en el 7001

Tres imágenes subidas por la API; crear álbum «Studio tests» → chip
activo; selección → Añadir al álbum (menú) → 3; visor: favorita,
etiquetas «test, studio» (aparecen como filtro), álbum, girar, flecha →
2/3, «A una conversación» → `/studio` con la imagen adjunta en el
compositor; zip por la API (15 KB, application/zip); borrar desde el visor
con diálogo; limpieza de imágenes y álbum.

## Crítica

- P1 corregido: las miniaturas crecían con la foto (el atributo `height`
  ganaba a `aspect-ratio`); ahora `block-size: auto`.
- P1 corregido: los menús Radix no abrían con `Button` como trigger.
- P2 abierto: el editor (Z2); el botón Editar del visor está oculto
  hasta entonces.
