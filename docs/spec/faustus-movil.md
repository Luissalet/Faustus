# Faustus en el móvil: mandar, mirar y recibir avisos con todo el cómputo en el PC

Pedido (17-09-2026): «tenerlo en el móvil y controlar el del PC: que me notifique al móvil cuando acaba cosas, pueda comprobar el progreso, mandarle otras, preguntar desde el móvil… pero todo el procesamiento en el PC».

Principio: el móvil es un **mando**, nunca un cerebro. Nada de modelos en el teléfono, nada de copiar datos fuera del PC; el PC sigue siendo el único sitio donde viven sesiones, ficheros, credenciales y modelos.

## Lo que ya hay y se reutiliza

- Servidor web con login y sesiones (`/api/auth`), Studio como SPA, SSE de turnos (`docs/api/sse_events.json`), tarjetas de permiso (`/api/approvals`), tareas programadas y `TaskRun` con resultado, cola `/api/queue`, `dispatch_reminder` (avisos internos), `companion/` (ping/info/pair con token de emparejamiento de un solo uso), puente de WhatsApp (`bridges/whatsapp`, envío `require_human`).
- Lo que falta: un canal de **push** hacia el teléfono, una **entrada** desde el teléfono que cree turnos/tareas, y una UI usable con el pulgar.

## Diseño en dos capas

### Capa 1 — Canal de mensajería (rápido, lo primero): un bot en el móvil

Un bot de Telegram (BotFather, gratis, push nativo, botones inline, ficheros) — o, como alternativa, el propio WhatsApp de Luis a través del puente que ya existe. Telegram primero: no compite con la sesión de WhatsApp Web, tiene botones y no depende del emparejamiento por QR.

- `src/mobile_channel.py` + `bridges/telegram/` (long polling desde el PC, sin puerto abierto ni dominio; el PC sale, nada entra). Ajustes: `telegram_bot_token` (secreto), `telegram_allowed_chat_ids` (solo Luis; cualquier otro chat se ignora y se registra).
- **Avisos** (PC → móvil): fin de turno de agente (resumen de 3 líneas + qué tocó), fin de `TaskRun` (tarjetas de Inicio: tiempo, correo, noticias, WhatsApp digest), errores/abortos, y **tarjetas de permiso** con botones «Aprobar / Denegar» (mapean a `/api/approvals`); mensajes largos se recortan y llevan botón «Ver en Faustus». Se engancha a los mismos puntos que `dispatch_reminder` y al final de `agent_loop` (evento `turn_finished`).
- **Órdenes** (móvil → PC): texto normal → nuevo turno en una sesión «Móvil» (modo agente) sobre el modelo por defecto, respuesta de vuelta al chat; `/status` (qué corre: turnos activos, cola, tareas, apps), `/tasks`, `/run <tarea>`, `/stop` (parar el turno), `/apps` (arrancar/parar por botón), `/wa <contacto> <texto>` (pasa por la tarjeta de permiso), `/ask …` en modo chat (sin herramientas). Fotos/audio recibidos → se guardan en el workspace de la sesión Móvil (audio transcrito con `services.stt`).
- Seguridad: solo `chat_id` en la lista; los textos del móvil entran como **usuario**, nunca como instrucciones a otras herramientas; todo envío externo sigue exigiendo persona (en este canal, «persona» = botón pulsado desde el `chat_id` autorizado); rate limit y registro en Activity.

### Capa 2 — Studio en el móvil (para mirar y aprobar con detalle)

- **Red**: Tailscale en PC y móvil (WireGuard, sin abrir puertos, sin dominio). `tailscale serve --https=443 http://127.0.0.1:7000` da HTTPS con certificado válido → necesario para PWA, service worker y Web Push. Alternativa: Cloudflare Tunnel.
- **PWA**: `manifest.webmanifest` + service worker en Studio (icono Faustus, standalone), «Añadir a pantalla de inicio».
- **Web Push** (VAPID): `src/push.py` (`pywebpush`), tabla `push_subscriptions` por usuario, `POST /api/push/subscribe`; mismos eventos que la capa 1. Es el sustituto del bot cuando Luis prefiera no usar Telegram; ambas capas comparten `src/notifications.py` (un solo «evento → mensaje» con destinos: Telegram, Web Push, WhatsApp-a-mí-mismo, Inicio).
- **UI con el pulgar**: vista `/m` (o Studio con `data-compact`): lista de sesiones, turno en curso con progreso (SSE), tarjetas de permiso grandes, compositor con dictado (ya existe `startDictation`), tarjetas de Inicio, Apps (Start/Stop). Nada de paneles laterales; navegación por pestañas inferiores.

## Orden de trabajo

1. `src/notifications.py` (eventos → destinos) + hook en fin de turno, fin de TaskRun, aprobación pendiente, error. Tests.
2. Bot de Telegram: long polling, allowlist, avisos con botones, `/status`, texto → turno, `/stop`, aprobar/denegar. Verificar en vivo con el bot de Luis (token en ajustes, nunca en el repo).
3. Tailscale + `tailscale serve` (Luis instala; Faustus solo documenta y detecta `https` para activar PWA/push).
4. PWA + Web Push + vista móvil de Studio.
5. Después: fotos desde el móvil al workspace, audio → transcripción, «modo remoto» que apaga el navegador integrado y las capturas de pantalla cuando no hay nadie delante.

## Riesgos

- Telegram pasa por servidores de Telegram: solo resúmenes y botones; nunca contenido de correo/WhatsApp completo salvo que Luis lo pida explícitamente en ese chat. Web Push (capa 2) es cifrado extremo a extremo y no depende de un tercero más que para el transporte.
- Un móvil robado = acceso al bot: `/revoke` desde Studio y expiración del emparejamiento; el bot no puede cambiar ajustes ni credenciales.
