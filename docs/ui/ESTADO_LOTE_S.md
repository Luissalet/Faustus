# Estado del lote S — Ajustes: Integraciones

Fecha: 05-09-2026. Rama `feat/studio-ui`.

## Qué hay

`adapters/integrations.ts` + `screens/settings/{Integrations,IntegrationForms,
IntegrationsMore}.tsx`. La lista unificada de la anterior
(`initUnifiedIntegrations`, ~1.700 líneas de settings.js) con las mismas
rutas y cuerpos: `/api/auth/integrations` (+ presets, test),
`/api/calendar/config/accounts` (+ `/api/calendar/test`), `/api/contacts/*`
(config, list, add, import, export, clear, PUT/DELETE por uid),
`/api/email/accounts` (+ test, OAuth de Google), `/api/mcp/servers` (+
reconnect, toggle, tools, oauth/exchange), `/api/tokens` (Codex/Claude por
prefijo de nombre, como la anterior), `/api/vault/*`.

Una lista con la clase de cada conexión, nombre, detalle y punto de estado;
«Añadir» abre las ocho clases; cada fila abre su ficha. Las fichas guardan
y refrescan la lista (`onChanged`) sin cerrarse cuando el flujo sigue
(prueba tras guardar, token recién creado, bóveda).

## Verificado en el 7001

API (plantilla ntfy → guardar → probar contra el servidor: el error real de
conexión se muestra → quitar), token de Claude (crear, 14 alcances, revocar),
formularios de correo (Gmail rellena hosts y avisa de la contraseña de
aplicación), contactos y bóveda renderizados con el estado del servidor.
OAuth de Google y MCP con OAuth no se han ejercitado (no hay proveedor
configurado en el 7001): los flujos son los mismos que la anterior
(guardar → redirigir; sondeo de `auth_url` → pestaña → pegado manual).

## Pendiente

- Modelos locales (lote T), editor de Tema (lote U).
- La nota de seguimiento tras guardar una cuenta de correo («ajustes de
  correo: respuesta automática, estilo») vive en la ventana de Correo de la
  anterior; entra con la paridad de Correo (IA y programación).
