# Estado del lote M - Correo como pantalla

Fecha: 05-09-2026. Rama `feat/studio-ui`. Verificado en el 7001 con el modo
de fixtures del servidor (`DATA_DIR/fixture_email_messages.json`: tres
mensajes de prueba; el 7001 no tiene cuenta de correo).

## Qué entra

- **Correo** (`/email`, chunk propio de 22 KB; `adapters/email.ts` sobre
  `/api/email`, las mismas rutas de `emailInbox.js` / `emailLibrary.js`):
  - Tres columnas: cuenta y carpetas con filtros (todos, sin leer, sin
    contestar, con estrella); lista paginada de 40 (remitente, hora o
    fecha, asunto con estrella y clip, extracto si el servidor lo da, no
    leídos en negrita); mensaje.
  - Búsqueda en la carpeta (`/search`, 350 ms de espera, dos letras mínimo).
  - Lectura (`/read`): asunto, remitente, fecha, para/cc, adjuntos con
    tamaño (`/attachment/{uid}/{i}`), cuerpo HTML en un `iframe` con
    `sandbox` y `srcdoc` (estilos base propios, enlaces a pestaña nueva,
    altura ajustada al contenido); abrir marca como leído (el servidor
    también).
  - Acciones: responder (cita en texto plano, `In-Reply-To` y `References`,
    `source_uid` para que el servidor lo marque contestado), responder a
    todos, reenviar, archivar, borrar, mover a carpeta (menú), estrella,
    marcar como no leído.
  - Redactar: desde (si hay varias cuentas), para, CC/CCO plegados,
    asunto, cuerpo (Markdown, el servidor lo renderiza), enviar y guardar
    borrador; los errores del servidor (`success:false`) se enseñan tal
    cual.
  - Actualizar (`_=` cache-bust) y acceso a cuentas y reglas en la
    anterior.
- **`Toast` compartido** (`components/Toast.tsx`, portal al overlay root):
  los avisos de Notas, Memoria, Calendario y Correo pasan por él. Motivo en
  PENDIENTES §56.

## Verificado en el navegador

- Bandeja con tres mensajes, «3 en Bandeja de entrada · 3 sin leer».
- Abrir «Comida del viernes» → cuerpo, remitente, fecha; la fila deja de ir
  en negrita («2 sin leer»).
- Responder → diálogo con «Re: Comida del viernes», la cita «El 4/9/2026,
  10:15:00, Marta Ruiz … escribió:» y Enviar → aviso «No SMTP-capable email
  account configured» (correcto: no hay cuenta), el diálogo se queda.
- Estrella → la fila muestra la estrella y el aviso «Con estrella.».

## Sin verificar

- Con IMAP real: paginación, carpetas del servidor, mover, archivar y
  borrar de verdad (el fixture solo simula bandeja, archivo y enviados),
  adjuntos y HTML de correos reales, búsqueda del servidor.
- Envío y borrador con una cuenta SMTP.
