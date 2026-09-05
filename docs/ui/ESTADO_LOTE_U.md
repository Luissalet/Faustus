# Estado del lote U — Apariencia (el editor de Tema)

Fecha: 05-09-2026. Rama `feat/studio-ui`.

## Qué hay

`shell/appearance.ts` (el tema: mismo almacenamiento y misma forma que
`static/js/theme.js`, `--bg/--fg/--panel/--border/--red` en `<html>` y
`data-theme-source="faustus"` en las raíces para que `legacy-bridge.css`
lo lea; `legacy-bridge.css` ahora restata también los tokens derivados
—papel, aurora, cristal, sombras— para que el tema llegue al fondo),
`shell/effects.ts` (los siete efectos de canvas portados tal cual, con el
lienzo dentro del shell y un contador de generación para que el bucle
viejo se apague al cambiar), `shell/display.ts` (lo que enseña la
transcripción: razonamiento, emojis, difuminar secretos con los mismos
patrones que `censor.js`, bienvenida, ancho completo) y
`screens/settings/Appearance.tsx`, que sustituye a la sección General.
Fuentes personalizadas por `/api/fonts/custom`; OpenDyslexic declarada en
`fonts.css`. Densidad y tamaño de texto como atributos en `<html>` que
reescalan los tokens de espacio.

Ya no queda ninguna pestaña «en la interfaz anterior» en Ajustes.

## Verificado en el 7001

Ocean (oscuro, constelaciones) y Paper (claro, puntos) con el shell
entero recoloreado; lluvia sobre Paper; vuelta a la paleta de Studio;
`data-theme` sigue a la luminosidad del fondo de la paleta y vuelve a la
elección del usuario con la de Studio.

## Trampas

- El lienzo del efecto se inserta como primer hijo del shell (un grid): va
  `position:absolute` y las reglas de z-index solo tocan `.fs-nav` y
  `.fs-main`; poner el tirador de la barra en `relative` lo convertía en
  celda del grid y rompía la disposición.
- Los tokens derivados con `color-mix` se resuelven donde se declaran: un
  puente que solo cambia `--fs-canvas` no cambia `--fs-paper`.

## Pendiente

PENDIENTES 80–81.
