# Estado del lote J - Notas como pantalla, grupo «Herramientas»

Fecha: 05-09-2026. Rama `feat/studio-ui`. Verificado en el 7001.

## Qué entra

- **Grupo «Herramientas»** en la barra lateral (debajo de los seis destinos,
  sin ser nodos de la línea) y en la paleta Ctrl+K: Notas, Calendario,
  Correo, Memoria, Cookbook. Lo migrado es ruta de Studio; lo demás abre la
  interfaz anterior en su enlace profundo (`/calendar?shell=legacy`), con
  «↗» para decirlo. Se define en `shell/routes.ts` (`TOOLS`, `toolHref`).
- **Notas** (`/notes`, chunk propio de 24 KB; adaptador `adapters/notes.ts`
  sobre `/api/notes` sin cambios en el servidor):
  - Añadir rápido: nota, o lista con un elemento por línea; Intro crea.
  - Tarjetas de nota, lista y objetivo: color (los seis nombres de la
    anterior; un `bg:` propio se respeta y se dice de dónde viene),
    etiquetas `#`, fijada, enlace pulsable, casillas con progreso, paso
    siguiente del objetivo en negrita, imagen si la tiene.
  - Editor: título, tipo, texto o elementos (Intro añade, Retroceso vacío
    quita, subir/bajar), color, etiquetas, recordatorio con fecha y hora
    («Más tarde», «Mañana», «Semana que viene»), repetición (día, semana,
    mes, año; se guardan y avanzan las formas `weekly:wd`, `monthly:day:N`,
    `monthly:nth:N:wd`, `monthly:last:wd` de la anterior), archivar, borrar.
  - Menú por tarjeta: editar, fijar, copiar el texto, resolver con el
    agente, subir/bajar, archivar, borrar (con confirmación).
  - Archivar deja «Deshacer» 7 s; vista de archivadas con recuperar.
  - Orden manual: arrastrar (HTML5, solo sin filtros) o subir/bajar;
    `POST /api/notes/reorder`.
  - Filtros: «Hoy» (vencidas o de hoy), objetivos, por etiqueta. Lista o
    cuadrícula (misma clave `odysseus-notes-view`).
  - «Resolver con el agente»: `/studio?draft=…&mode=agent&send=1&note=<id>`.
    Studio lo envía en cuanto hay ruta y enlaza la conversación a la nota
    (`agent_session_id`); la tarjeta muestra «agente» y abre ese chat.
    La anterior lo corría en segundo plano; aquí se abre en primer plano.
  - Recordatorios: bucle cada 30 s que dispara `POST /api/notes/fire-reminder`
    (síntesis, correo, ntfy, webhook según ajustes), `Notification` del
    navegador si hay permiso (botón de campana para pedirlo), y avanza la
    fecha si repite. **Apagado mientras `#notes-pane` de la anterior exista**
    (su `notes.js` sigue cargado debajo del piloto y ya dispara): así nada
    suena dos veces. Clave propia `faustus_studio_notes_fired`.
  - `/notes?n=<id>` abre esa nota (el `openNote` de la anterior).
- Studio: `?draft=` admite `mode`, `send` y `note`; el atajo `open_notes` y
  el comando `/notes` van a la pantalla.
- `QuickMenu` pasa a estilos compartidos (`styles/components.css`): lo usan
  pantallas de chunks distintos.

## Verificado en el navegador

- Crear nota rápida con enlace (pulsable) y lista de tres elementos.
- Marcar un elemento (optimista, confirmado por el servidor).
- Editor: título «Casa», color amarillo, `#casa`, «Mañana» + cada semana;
  la tarjeta muestra «mañana 09:00 · Cada semana · #casa» y el filtro
  `#casa` aparece.
- Fijar (sube al principio), archivar → «Nota archivada. Deshacer» →
  vuelve; vista de archivadas vacía; cuadrícula y lista.
- «Resolver con el agente»: Studio abre en modo agente con el prompt, crea
  la sesión «Casa» y la nota queda con la etiqueta «agente».

## Sin verificar

- Recordatorio disparado por Studio (la anterior sigue debajo).
- Arrastrar tarjetas con el ratón real (el puente no arrastra).
- Foto y dibujo: siguen en la anterior (PARIDAD).

## Trampas

- Un menú que cuelga por debajo de su tarjeta recibía el clic del elemento
  siguiente: la animación de entrada (`fs-rise … both`) deja a cada hijo
  de `.fs-screen` en su propio contexto de apilamiento. La rejilla lleva
  `position: relative; z-index: 1` y la tarjeta con menú abierto
  `:has(.fs-qmenu__list)` sube por encima de sus hermanas.
- Las capturas del puente van a 1568×749 sobre una ventana de 1920×917:
  `elementFromPoint` con coordenadas de captura engaña; el tool `computer`
  ya escala.
