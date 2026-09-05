# Estado del lote P — Idioma, apariencia y barra lateral

Fecha: 05-09-2026. Rama `feat/studio-ui`.

## Qué hay

Tres cosas que la anterior no tenía o tenía a medias, todas en Ajustes →
General (la sección que se abre por defecto) y en la propia barra:

- **Idioma de la interfaz** (`studio/src/i18n/`). Inglés por defecto,
  español completo. Cada cadena de Studio está escrita en inglés en el
  punto de uso (`t('Save')`) y el diccionario español la traduce
  (`es.ts`, 1413 entradas generadas desde `docs/ui/i18n/es.tsv` por
  `scripts/i18n_es.py`; `--check` avisa de claves sin traducir). Una clave
  sin entrada se muestra en inglés, nunca en blanco ni como código.
  Plurales con `tn(n, one, other)`; interpolación `{name}`; fechas, horas,
  meses y días con `locale()` (`en-GB` / `es-ES`), también el STT/TTS. La
  elección va a `localStorage` (instantánea) y a la preferencia
  `ui_language` del servidor (sigue al usuario a otro navegador); el shell
  reconcilia ambas al montar. Cambiarla re-monta el árbol (`key={lang}` en
  `AppShell`), que es la forma honesta de refrescar etiquetas estáticas.
- **Apariencia** (`shell/theme.ts`): sistema / claro / oscuro. Un atributo
  `data-theme` en `<html>` que `tokens.css` ya resolvía antes que
  `prefers-color-scheme`; «sistema» quita el atributo. Preferencia
  `ui_theme` + `localStorage`, misma pareja que el idioma.
- **Barra lateral redimensionable y plegable** (`shell/navSize.ts`): el
  borde de la barra es un separador (`role="separator"`) con cursor de
  doble flecha; arrastrar cambia el ancho (168–400 px), comprimir por
  debajo de 120 px pliega a carril de iconos, arrastrar de vuelta o doble
  clic reabre, teclado ←/→/Inicio/Fin. Se recuerda en
  `faustus_studio_nav` (`{mode: auto|wide|rail, width}`); en <1280 px se
  pliega sola salvo elección explícita. Las reglas del carril pasaron de
  `@media` a `.fs-shell[data-nav='rail']`. El tirador vive en la raíz del
  shell, no dentro del `nav` (que recorta su desbordamiento).

## Verificado en el 7001

- Español e inglés en Inicio, Studio, Proyectos, Biblioteca,
  Automatizaciones, Actividad, Notas, Calendario, Correo, Memoria, Agentes
  y Ajustes; el cambio aplica sin recargar y sobrevive a la recarga
  (`localStorage`) y a un navegador limpio (`ui_language`).
- Apariencia: las tres opciones; «sistema» sigue al SO.
- Barra: arrastre real con eventos de puntero (guardado
  `{"mode":"wide","width":300}`), plegado al comprimir, doble clic a 236.
- `tests/test_studio_guards.py` y los `test_studio_*` (19) en verde;
  `scripts/i18n_es.py --check` sin claves perdidas.

## Arreglos que salieron

- **El shell se montaba dos veces** (PENDIENTES 68): la primera vez que un
  cambio de idioma quitó nodos, React tiró todo el árbol. Ahora `app.tsx`
  monta una sola vez.
- La hoja antigua se colaba en Studio por reglas de elemento (PENDIENTES
  63), lo que descentraba Memoria y dejaba el compositor flotando en
  pantallas estrechas: el panel lateral de Studio es `aside.fs-panel`
  (la `.fs-panel` genérica es la tarjeta).
- `model.check.mjs` comparaba con una cadena en español; ahora con la clave.

## Pendiente

PENDIENTES 68–72.
