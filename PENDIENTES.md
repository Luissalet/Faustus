# Pendientes de cierre

Actualizado: 08-09-2026. Sólo trabajo vigente; quitar cada entrada al cerrarla.

## Comprobaciones pendientes

- **Batería completa:** repetirla después de corregir el aislamiento del entorno de pruebas.
- **Objetivos sin carpeta:** terminar el recorrido real de alta mediante el agente, comprobarlo en el proyecto y recargar. Guardado, migración y concurrencia pasan sus pruebas automáticas.
- **Texto entre rondas:** comprobar en navegador que las respuestas no quedan concatenadas, en vivo y tras recargar. La prueba del stream pasa.
- **Integraciones reales:** generación con Codex/Claude usando una cuenta real y equipo mixto. Las pruebas con proveedores simulados no cubren esto.
- **Voz física:** conversación completa con micrófono en español e inglés. No activar grabación ni permisos para cerrar esta casilla sin intervención del usuario.
- **Entrega:** revisar diff, arranque y recorrido principal. README EN/ES, portfolio y CV ya están actualizados; sólo sincronizar cambios posteriores.

Carencias funcionales: [OBJETIVOS.md](OBJETIVOS.md).
Comprobaciones específicas de interfaz: [PENDIENTES_UI.md](docs/ui/PENDIENTES_UI.md).
No contar planes de inspiración o notas de implementación como otra cola de tareas.

## Última evidencia

- Suite completa: **13.052 correctas, 81 omitidas y 14 errores de teardown** (`logs/astra-receipts-full.xml`). La causa de los errores está corregida; falta repetir la suite.
- Objetivos y aislamiento: **225 correctas** (`logs/astra-objective-scope-focused.xml`).
- Integración: **203 correctas** (`logs/astra-objective-scope-integration.xml`).
- Servidor reiniciado el 08-09: HTTP 200.
- Control previo al checkpoint: **147 correctas** (`logs/checkpoint-20260908.xml`), TypeScript y compilación de producción correctos. Persiste el aviso de tamaño del bundle.
