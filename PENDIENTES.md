# Pendientes de cierre

Actualizado: 08-09-2026. Sólo trabajo vigente; quitar cada entrada al cerrarla.

## Comprobaciones pendientes

- **Batería completa:** repetir al cerrar los últimos cambios de implementación.
- **Integraciones reales:** generación con Codex/Claude usando una cuenta real y equipo mixto. Las pruebas con proveedores simulados no cubren esto.
- **Voz física:** conversación completa con micrófono en español e inglés. No activar grabación ni permisos para cerrar esta casilla sin intervención del usuario.
- **Entrega:** revisar diff, arranque y recorrido principal. README EN/ES, portfolio y CV ya están actualizados; sólo sincronizar cambios posteriores.

Las carencias de backend del índice anterior están implementadas; se ha eliminado ese índice vacío.
Comprobaciones específicas de interfaz: [PENDIENTES_UI.md](docs/ui/PENDIENTES_UI.md).
No contar planes de inspiración o notas de implementación como otra cola de tareas.

## Última evidencia

- Suite completa: **13.120 correctas, 81 omitidas y una expectativa antigua fallida** (`logs/checkpoint-full-20260908.xml`): el test exigía rechazar objetivos sin carpeta. Corregido y verificado en el bloque posterior de **134 pruebas correctas**. No hubo errores de teardown.
- Navegador Brave y Qwen local: objetivo OBJ-1 creado en proyecto sin carpeta, conservado tras recarga; trabajo continuado fuera del chat. Respuesta inglesa tras herramienta y texto entre rondas completos tanto en vivo como tras recargar (08-09, 01:15).
- Objetivos y aislamiento: **225 correctas** (`logs/astra-objective-scope-focused.xml`).
- Integración: **203 correctas** (`logs/astra-objective-scope-integration.xml`).
- Servidor reiniciado el 08-09: HTTP 200.
- Control previo al checkpoint: **147 correctas** (`logs/checkpoint-20260908.xml`), TypeScript y compilación de producción correctos. Persiste el aviso de tamaño del bundle.
