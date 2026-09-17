# Faustus en el móvil: la PWA (lot P-B)

Implementado el 17-09-2026. Convierte Studio en una app instalable —
`static/manifest.json` servido en `GET /manifest.webmanifest` y
`static/sw.js` servido en `GET /sw.js`, ambos públicos
(`routes/pwa_routes.py`, exentos de auth en `app.py`) porque el primer
registro del service worker ocurre antes de que exista una cookie de
sesión.

## Barra inferior a 767px

Por debajo de 767px (o una PWA instalada igual de estrecha —
`data-platform="mobile"`, ver `studio/src/shell/platform.ts`) la barra
lateral de seis destinos se convierte en cinco pestañas horizontales:
Inicio, Studio, Calendario, Notas, Ajustes
(`studio/src/shell/routes.ts::MOBILE_DESTINATIONS`). El grupo de
herramientas (`.fs-nav__tools`) y el pie de la barra (Ajustes + la pista
"Ctrl+K") desaparecen — Ajustes ya es una de las cinco pestañas, y el resto
de herramientas siguen alcanzables desde la paleta de comandos. El tour y
la pista de teclado (chrome de escritorio) tampoco se montan en
`data-platform="mobile"`.

Objetivos táctiles: cada pestaña mantiene los 44px mínimos ya establecidos
para el resto de la interfaz (`studio/src/styles/shell.css`); las tarjetas
de aprobación/pregunta del chat (`.fs-studio__ask-option`,
`.fs-studio__ask-actions`) suben a 44px por debajo de 767px
(`studio/src/screens/studio.css`).

## Instalar y notificar

`studio/src/lib/installPrompt.ts` captura `beforeinstallprompt` desde
`main.tsx`, antes de que el bundle de la app termine de cargar — si se
capturara más tarde, el evento (que el navegador sólo entrega una vez) ya
habría pasado. `studio/src/screens/settings/ThisDevice.tsx` (Ajustes →
Este dispositivo) ofrece el botón "Instalar Faustus" cuando ese evento
llegó, el aviso manual de iOS ("Compartir → Añadir a pantalla de inicio")
cuando no puede llegar nunca, y el panel de notificaciones push
(`studio/src/adapters/push.ts`, sobre `/api/push/*` — ver
`docs/api/mobile.md`).

## Verificación

- `node studio/checks/pwa.check.mjs` — enlaces del manifest/service worker
  en `index.html`, los manejadores push/notificationclick/
  pushsubscriptionchange en `sw.js`, los iconos del manifest existen en
  disco, los exports del adaptador y el montaje de `ThisDeviceSection` en
  Ajustes.
- `python3 -m pytest tests/test_pwa_routes.py -q` — `/sw.js` y
  `/manifest.webmanifest` responden 200 sin ninguna cabecera de auth,
  contra la `app` real.
- `npx tsc --noEmit -p tsconfig.json` limpio.
