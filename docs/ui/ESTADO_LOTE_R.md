# Estado del lote R — Ajustes: Cuenta, Usuarios, Herramientas, Sistema (admin)

Fecha: 05-09-2026. Rama `feat/studio-ui`.

## Qué hay

`adapters/account.ts` + `screens/settings/{Account,Users,Tools,SystemExtras}.tsx`
(los primitivos de Ajustes viven ahora en `screens/settings/fields.tsx`).
Cuatro pestañas de la anterior que eran de `settings.js` y `admin.js`, con
las mismas rutas: `/api/auth/*` (status, policy, change-password, 2fa/*,
logout, users, open-signup), `/api/tools`, `/api/diagnostics/logs`,
`/api/export`, `/api/import`, `/api/admin/wipe/{kind}`. Las secciones de
admin (Herramientas, Usuarios, los extras de Sistema) solo aparecen si
`/api/auth/status` dice `is_admin` (o el acceso está apagado).

La pestaña «Email» de la anterior eran tres enlaces (Correo, Integraciones,
Tareas): ya no hace falta como pestaña.

## Verificado en el 7001

Con sesión de admin (`/api/auth/login`): Cuenta (nombre, rol, formulario de
contraseña con mínimo de la política, tarjeta 2FA), Usuarios (registro,
compartir, lista con admin, alta), Herramientas (54 etiquetas del servidor,
todo/una a una), Sistema (registro en vivo con niveles y búsqueda, copia,
zona de peligro). Sin sesión: «Solo administradores» y el 401 del 2FA se
muestra como aviso, no como error.

## Pendiente

- Integraciones (lote S), Modelos locales (lote T), editor de Tema (lote U).
- Los `TOOL_TAGS` del servidor no coinciden con la tabla de nombres
  (`bash`, `python`…) que la anterior copiaba; ambas enseñan la etiqueta
  cruda. Cuando el servidor devuelva familia y descripción, la tabla sobra.
