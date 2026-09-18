# Estado del lote I - sesiones y compositor

Fecha: 04-09-2026. Rama `feat/studio-ui`. Verificado en el 7001 con
`qwen3.5:9b`.

## Qué entra

- **Sesiones** (`SessionsPane` reescrito):
  - Menú ⇅ (`QuickMenu`, sin dependencias: Radix Menu metería 80 KB de
    floating-ui en el bundle principal): ordenar por última actividad, fecha
    de creación, nombre o por carpeta (grupos plegables con contador); misma
    clave `odysseus-session-sort` que la anterior.
  - «Seleccionar varias»: casillas por fila, «todas», barra con Zip
    (`/api/sessions/export?fmt=zip&ids=`), Archivar y Borrar (con
    confirmación, `/api/sessions/bulk-delete`).
  - «Archivadas…»: lista con búsqueda (`/api/sessions/archived`) y
    «Recuperar» (`/api/session/{id}/unarchive`).
  - «Ordenar en carpetas con IA» y «Limpiar vacías»
    (`/api/sessions/auto-sort[?skip_llm=true]`).
  - Diálogo «…»: además de renombrar, favorita, archivar, exportar y borrar,
    **Duplicar** (`/api/session/{id}/fork`) y **Carpeta** (existente, nueva
    o ninguna; `PATCH /api/session/{id}` con `folder`).
  - En pantallas anchas la columna se oculta con el chip «Conversaciones» o
    Ctrl+B y se recuerda (`faustus_studio_pane`); en estrechas sigue el
    cajón.
- **Fork**: «Bifurcar desde aquí» en el pie de cada mensaje (mantiene hasta
  ese mensaje, `keep_count = historyIndex + 1`) y `/fork`; abre la copia.
- **Incógnito**: chip en el compositor, `/incognito`, Ctrl+Alt+I. La sesión
  se llama «Incógnito», no sale en la lista, se envía `incognito=true`, y se
  borra al salir del modo o al recargar. Clave propia
  (`faustus_studio_incognito`), ver PENDIENTES §38.
- **Presets**: chip con paleta (`PresetPalette`, chunk perezoso): «Sin
  preset», «Nuevo preset…» (nombre, temperatura, máx. tokens, prompt de
  sistema → `POST /api/presets/templates`), los tuyos (con borrar) y los
  incluidos; `/preset nombre`; se envía `preset_id`.
- **Dictado y voz** (`adapters/speech.ts`, chunk perezoso): micrófono en el
  compositor (MediaRecorder → `/api/stt/transcribe`, o `SpeechRecognition`
  del navegador si el servidor devuelve 503); «Leer en voz alta» por
  mensaje, `/tts` y Alt+Shift+T (`/api/tts/synthesize`, o `speechSynthesis`
  si el servidor devuelve 503).
- **Citar selección**: al seleccionar texto de un mensaje aparece «Citar» y
  lo añade al borrador como `> cita`.
- **↑** con el borrador vacío recupera lo último enviado.
- **Refrescar modelos** desde la paleta (`/api/models?refresh=true`).
- **Atajos de la anterior** con sus keybinds guardados
  (`/api/auth/settings`), mismos valores por defecto que `settings.js`:
  búsqueda, barra, nueva, favorita, borrar (dos pulsaciones en 4 s),
  cancelar, TTS, incógnito, ajustes, foco, calendario y los «abrir…». Se
  capturan en `window` antes de que los vea `keyboard-shortcuts.js`
  (PENDIENTES §39).
- Servidor: `/api/sessions/archived` ya funciona con `AUTH_ENABLED=false`
  (PENDIENTES §40).

## Verificado en el navegador

- Ordenar por carpeta («SIN CARPETA 100»), por nombre y por creación.
- Preset Brainstorm desde la paleta y `/preset reason` desde el compositor;
  el chip cambia y se envía `preset_id`.
- Incógnito: sesión «Incógnito» oculta de la lista, respuesta «Hola»,
  borrada al salir del modo.
- Citar: seleccionar texto → «Citar» → `> …` en el borrador y la caja crece.
- Fork: «⫝ Prueba Studio v2» creada y abierta.
- Archivar desde el diálogo → «Archivadas…» la lista → «Recuperar» la
  devuelve («Conversación recuperada»).
- Seleccionar varias → dos casillas → Archivar → «2 conversaciones
  archivadas» y la lista pasa de 100 a 98.
- ↑ recupera «Di solo OK» tras enviarlo.
- Ctrl+Alt+N nueva conversación (y ya no aparece la sesión fantasma de la
  anterior), Ctrl+B oculta y muestra la columna, Ctrl+/ enfoca el
  compositor.

## Sin verificar

- Micrófono y voz con hardware real (PENDIENTES §41).
- «Ordenar en carpetas con IA» con el modelo (llama al endpoint; no se ha
  esperado a que el LLM reparta 98 sesiones).
- Exportar zip de la selección (enlace al endpoint de la anterior).

## Tamaño

`app` 375,0 KB / 117,8 KB gzip (presupuesto 350 / 120). Chunks nuevos:
`speech` 2,3 KB, `presets` 1,4 KB, `PresetPalette` 3,6 KB, `SessionDialog`
3,6 KB. Ver PENDIENTES §42.
