# Comprobación de clientes oficiales

Agentes → Runners muestra «Tus clientes oficiales» antes del catálogo existente.
Codex y Claude Code se comprueban por separado, sólo al pulsar el botón. La
consulta no envía tareas al modelo ni cambia instalación, cuenta o configuración.

## Contrato

- `RunnerConnections.tsx` consume el adaptador `runner-connections.ts` y
  `GET /api/agent-runners/{codex|claude}/connection`, protegido para administradores.
- Distingue suscripción, API, falta de instalación/sesión, conflicto y estado no
  reconocido. Un resultado desconocido nunca aparece como conexión confirmada.
- Muestra cuando los trabajos externos permanecen desactivados. Detectar una
  sesión no equivale a habilitarla, comprobar cuota ni garantizar cómo se cobrará
  una ejecución posterior. La integración como orquestador del chat sigue pendiente.
- El cliente cancela a los 12 segundos; el servidor limita la comprobación a 8.
  Cancelar y desmontar abortan la petición. Una respuesta antigua no cambia el
  estado de una nueva consulta. Ninguna consulta se ejecuta automáticamente.
- Errores 401/403 explican el requisito de administrador; 404 pide reiniciar el
  backend actualizado. Otros errores ofrecen reintentar sin mostrar salida cruda.
- Sólo se muestran nombres validados de variables relevantes, nunca sus valores.
  Ayuda con enlaces oficiales fijos, sin lanzar login/logout desde el navegador.

## Diseño y validación

Impeccable guio una extensión pequeña del sistema existente: filas ligeras, texto
de estado explícito, acciones de al menos 44 px y mensajes en español e inglés.
No cambia el diseño global ni añade tarjetas decorativas. En móvil, textos largos
y acciones pasan a varias líneas; los estados usan texto además del color.

Pruebas de adaptador en `studio/checks/runner-connections.check.mjs`, incorporadas
al test de contratos JavaScript. TypeScript y build pasan. Pruebas de navegador
con el componente real y respuestas sintéticas locales: suscripción/API, conflicto
en tema claro a 390 px (sin desbordamiento horizontal, acciones de 44 px), tema
oscuro de escritorio, cancelación sin respuesta tardía que cambie el estado,
errores 403/404 y límite de espera de 12 segundos. No se usaron cuentas reales desde esta prueba.
La comprobación local de CLI real se documenta por separado en el registro de cambios.

Detector manual ejecutado una vez: seis avisos de bordes laterales pertenecen a
reglas anteriores de `agents.css`, fuera de esta superficie. Ninguno en las nuevas
filas/componente. No es una auditoría ni certificación de toda la pantalla de agentes.

La vista de prueba aislada está en `studio/checks/runner-connections-preview.mjs`;
no forma parte de la aplicación publicada y no realiza llamadas a proveedores.
