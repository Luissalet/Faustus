# Estado del lote AJ — los tours guiados

Fecha: 05-09-2026. Rama `feat/studio-ui`. Cierra lo último que quedaba de
PARIDAD §3: `/demo` y los once `tour-*`, más el autoplay que los disparaba.
Con esto §3 y §4 están enteras.

## Brief (banco de trabajo)

**Trabajo.** Que alguien que abre una pantalla por primera vez entienda
para qué sirve sin leer documentación: cinco frases, cada una señalando la
cosa de la que habla.

**Lo que había.** Once funciones `async` copiadas y pegadas dentro de
`slashCommands.js`, de unas 150 líneas cada una. Cada copia volvía a
inyectar el mismo `<style id="tour-styles">` en el `head`, volvía a
escribir su `_makeHalo`, su `_positionTooltip` y su `_showStep`, y cada una
tenía su propio bucle a mano esperando a que un modal abriera o cerrara
(`for (let i = 0; i < 20; i++) await new Promise(r => setTimeout(r, 80))`).
Y `tourAutoplay.js` te lanzaba un tour encima la primera vez que abrías
cada herramienta: un paseo guiado que empieza solo, que es justo lo que la
gente odia de los paseos guiados.

**Dirección.** Un motor y los tours como datos. Y el autoplay se convierte
en un ofrecimiento: una línea abajo a la derecha, con «Hacerlo» y una cruz,
que si dices que no no vuelve.

## Lo que se hizo

### `studio/src/lib/tours.ts` (nuevo)

Doce tours (el general más once) como datos. Un paso es
`{ route?, target, text }`: dónde vive, qué señala y qué dice. Todos los
selectores son `data-testid` o clases `.fs-` que ya existían, así que no
hubo que tocar ninguna pantalla para que se dejaran señalar. También:
`tourById`, `tourForPath` (qué tour ofrece una ruta —la galería y la
apariencia se distinguen por su query— y ninguno para `/studio`, porque el
tour general no debe ofrecerse solo), `seenTours`/`markTourSeen`/`resetTours`
(`faustus_studio_tours` en `localStorage`), y `placeTooltip`, que es la
aritmética de colocación: debajo si cabe, si no encima, si no al lado, y
luego recortada contra el viewport. Pura, y por tanto con test.

### `studio/src/shell/Tour.tsx` (nuevo) y `tour.css`

El motor. Por cada paso: navega si el paso vive en otra ruta, espera hasta
2,6 s a que el objetivo aparezca (las pantallas son chunks perezosos),
lo trae a la vista y lo rodea con un halo que oscurece el resto —salvo que
el objetivo sea medio viewport, en cuyo caso oscurecer no ayuda a nadie—.
Si el objetivo no aparece nunca, **se salta el paso** y el tour sigue: una
pantalla que cambia no rompe su tour. La tarjeta lleva título, `n/total`,
puntos de progreso, atrás/siguiente/terminar, y teclado (←, →, Esc). Nada
hace clic en nombre de nadie: el tour enseña, la persona conduce.

`shell/store.ts` gana `tourId` y `startTour(id)`, así que cualquier pantalla
o comando puede levantar uno. El componente se monta en `AppShell` como
chunk perezoso.

### Comandos

`/demo` (alias `/tour`) y los once `tour-*` con todos los alias que tenía la
anterior (`compare-tour`, `tour-doc`, `tour-memory`, `tasks-tour-1`…), más
uno nuevo: `/tours` lista los que hay con cuántos pasos tiene cada uno y
marca los ya vistos, y `/tours reset` vuelve a ofrecerlos.

## Pruebas

Ampliado `studio/checks/commands.check.mjs`: ningún id repetido, todos los
tours con título y pasos, todo paso con objetivo y frase, todo tour dice
dónde empieza, todo id tiene su `/comando`, `tourForPath` para `/compare`,
la galería, la apariencia, `/studio` (ninguno) y una ruta cualquiera; y
`placeTooltip` en los cuatro casos (debajo, encima, al lado, y recortado
contra los dos bordes). 21 tests en verde.

## Verificado en el 7001

- `/demo`: los doce pasos, con las navegaciones de `/studio` a `/projects`,
  `/library`, `/agents`, `/memory`, `/cookbook` y `/settings`, y el halo
  encontrando el objetivo en cada una.
- `/cookbook`: el ofrecimiento aparece solo al llegar; «Hacerlo» arranca el
  tour y los seis pasos recorren las seis pestañas (`?t=fit` → `?t=servers`),
  cada una con su halo.
- `/compare`: el ofrecimiento, y los cuatro pasos con la tarjeta
  colocándose sola encima o debajo según dónde esté el objetivo.
- Teclado: ← y → mueven, Esc termina y marca el tour como visto.

## Lo que queda

- Los pasos que señalan algo que solo existe después de una acción (el
  panel de resultados de Compare antes de comparar) se saltan solos. Es lo
  correcto, pero significa que un tour puede ser más corto de lo que dice su
  contador si lo haces con la pantalla vacía.
- El ofrecimiento sale una vez por pantalla y por navegador (vive en
  `localStorage`, no en el servidor). No hay ajuste para volver a activarlos
  que no sea `/tours reset`.
