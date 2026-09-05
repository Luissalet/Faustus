# Estado del lote AB — Correo completo

Fecha: 05-09-2026. Rama `feat/studio-ui`. Cierra la fila «Correo» de
PARIDAD (era **Parcial** desde el lote M). Nada del DOM ni del CSS de
`emailInbox.js` / `emailLibrary.js` / `document.js` sobrevive; la lógica
pura (saneado del HTML, direcciones, meta de los turnos, plegado de firmas
y citas) vive en `studio/src/lib/mail.ts`.

## Brief (modo Operate)

**Trabajo.** Vaciar la bandeja con criterio: ver rápido qué hay, decidir
(hecho, estrella, archivar, recordar, borrar), responder con ayuda de la IA
y sin perder el hilo, mandar cosas con adjuntos ahora o más tarde, y que lo
automático (etiquetas, urgencia, resúmenes, bajas) esté a la vista sin
molestar.

**Persona.** Luis: tema oscuro, teclado, quiere «eye candy» pero la función
manda; correos en español e inglés; usa la IA local (qwen 27b) para
resumir, traducir y redactar.

**Lo que hacía mal la anterior.** Un modal de 8.771 líneas con tarjetas
en rejilla, tres menús anidados (tarjeta, lector, «más»), un submenú de
recordatorios dibujado a mano, la búsqueda como «píldoras» con sugerencias
que escondían los filtros, los ajustes del correo en una «página» dentro
del modal, el redactor metido en el editor de documentos del panel lateral
(300px con destinatarios, adjuntos, programar e IA apretados), lectores en
pestañas propias, colores literales para la urgencia, y `alert`/`confirm`
nativos.

**Dirección.** Tres columnas: el carril dice dónde estás (cuenta, carpetas
con el recuento, bandeja de salida, triaje y etiquetas como filtros
visibles con nombre), la lista es la cola de trabajo (agrupada por día,
con estrella arriba, avatar con color estable, urgencia como borde y punto,
acciones rápidas al pasar, selección múltiple con bandeja de acciones), y
la tercera columna es el correo **o el redactor**: responder no tapa lo que
estás contestando. El lector se lee en el tema (colores del correo fuera,
imágenes remotas retenidas) con los paneles de IA arriba, plegables y con
el modelo que los hizo; los hilos largos se ven como conversación. Todo lo
raro va a «Más»; lo frecuente está en la barra con icono y nombre
accesible. Atajos de una tecla como Gmail (j/k, e, #, s, d, u, r, a, f, c,
/) y una chuleta en el marcador de posición. La pantalla es «ancha»
(`data-screen='wide'`, 1480px) porque tres columnas a 960 no respiran.

**Revisión contra los defaults.** Sin `alert`/`confirm`: diálogos con la
consecuencia («Enviar sin adjunto?»); la fila es un `<li>` con un botón
principal y acciones aparte (no botones dentro de botones); colores por
token (`--fs-danger` urgente, `--fs-warning` pronto, siete tonos de avatar
del propio sistema); nada crítico en hover (las acciones rápidas repiten lo
que ya está en el lector y en el teclado); estados vacíos que dicen qué
hacer («All done.», «Nothing waiting»); los errores del servidor se enseñan
tal cual y **solo** cuando el servidor dice `success:false` (la anterior
celebraba estrellas que no se habían puesto).

## Qué entra

- `adapters/email.ts` reescrito: cuentas con alias, lista con `from`,
  `has_attachments` y los filtros del servidor (`undone`, `reminders`,
  `pending_30d`, `stale_30d`, `tag:x`), búsqueda con ámbito, `unread-state`,
  `urgency-state` aplanado por uid, lectura con `thread_turns`,
  `related_attachments`, `cached_summary`, `sender_signature`, adjuntos
  diferidos, zip, `inline-image`, `attachment-as-doc`; hecho / no hecho,
  no es spam, borrar definitivamente, recordatorios de Faustus; resumir,
  traducir, respuesta con IA (rápida / completa, indicación), estilo
  (leer, guardar, extraer), configuración (respuesta de ausencia,
  automáticas, idioma de traducción); subir adjunto, desde la biblioteca
  (documento / imagen / zip), desde un correo recibido, descartar; enviar
  con `attachments` y `body_html`, borrador, programar (ISO UTC; la lista
  vuelve con `Z` para que las horas lean en local), programados y
  cancelar, pendientes de aprobación con aprobar / descartar; contactos
  (buscar, recordar); revisión de bajas (escanear, ejecutar, limpiar).
- `lib/mail.ts`: `splitAddresses`/`parseAddress`/`joinAddresses`,
  `displayName`, `initials`, `hueIndex`, `isValidEmail`, etiquetas visibles,
  `sanitizeMailHtml` (portado de `utils.js`, con `keepStyles`, reescritura
  de `cid:` y retención de imágenes remotas), `textToHtml`, `parseTurnMeta`,
  `splitQuotedText`, `foldSignature`, `foldQuotes`, `mentionsAttachment`.
- `screens/email/`: `Email.tsx` (pantalla), `Reader.tsx`, `Compose.tsx`
  (fichas de destinatarios con autocompletado, IA, adjuntos, selector de la
  biblioteca, programar), `Outbox.tsx`, `Unsubscribe.tsx`,
  `MailSettings.tsx`, `parts.tsx` (avatar, chip de etiqueta, fechas,
  `useMailKeys`). `email.css` reescrito.
- Global: `.fs-notice` pasa de `home.css` a `components.css`; `.fs-muted`
  nuevo; `data-screen='wide'` en `AppShell` + `shell.css` (1480px);
  Calendario acepta `?event=<uid>` y abre el evento (etiqueta «calendar» del
  correo).
- i18n: 204 filas nuevas en `es.ts`.

## Verificado en el navegador (7001, modo fixture)

- Lista en tres grupos (Hoy, Ayer, Esta semana), avatares, puntos de no
  leído, cabecera «3 in Inbox · 3 unread on this page», carril con triaje
  y etiquetas, pantalla ancha.
- Abrir → lector con acciones; Responder → redactor en la tercera columna
  con «Marta Ruiz» como ficha, asunto «Re:», cita; «Redactar con IA» →
  Quick draft → «Hi Marta, El viernes a las 14 en La Tasca me viene
  perfecto…» con el modelo (`qwen3.8:27b-q8_0`) y la cita intacta.
- «Enviar más tarde» → Mañana 9:00 → aviso «Scheduled for 6 Sept 2026,
  09:00», contador 1 en Bandeja de salida; Bandeja de salida lista el
  programado (hora local correcta tras el arreglo `Z`) y Cancelar lo quita.
- Resumir → panel «Summary» con dos viñetas y el modelo; Traducir →
  inglés por defecto → panel «Translated to English».
- Recordarme responder → Mañana → nota creada con `due_date` 2026-09-06T09:00
  y enlace `/email?folder=INBOX&uid=…` (borrada después).
- Menú «Más» (resumir, abrir en chat, guardar remitente, solo este
  remitente, HTML original, mover a spam, borrar definitivamente); «HTML
  original» → marco aislado en papel.
- Selección múltiple → Seleccionar todo → Marcar como leído → «0 of 3
  done; the rest failed.» (el fixture no tiene IMAP: PENDIENTES 103).
- Ajustes del correo (respuesta de ausencia, automáticas, estilo) y
  «Extraer de los enviados» → «IMAP is not configured for account
  'legacy'»; revisión de bajas → «Nothing to unsubscribe from».
- Móvil 420px: carril en fila desplazable, lista, lector a toda anchura con
  Volver; sin marcador de posición cuando no hay correo abierto.

## Sin verificar

- Todo lo que necesita IMAP/SMTP real (PENDIENTES 103 y 107): estrella,
  hecho, archivar, mover, borrar, no leído, adjuntos reales, imágenes
  `cid:`, conversación en burbujas, firma plegada, envío, borrador en el
  buzón, bajas con candidatos, reenviar con los adjuntos del original,
  aprobación de borradores de agentes (no hay ninguno pendiente).
