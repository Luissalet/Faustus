# Pendientes de cierre

Actualizado: 08-09-2026. Sólo trabajo vigente; quitar cada entrada al cerrarla.

## Comprobaciones pendientes

- **Batería completa:** repetir al cerrar los últimos cambios de implementación.
- **Voz física:** conversación completa con micrófono en español e inglés. No activar grabación ni permisos para cerrar esta casilla sin intervención del usuario.
- **Entrega:** revisar diff, arranque y recorrido principal. README EN/ES, portfolio y CV ya están actualizados; sólo sincronizar cambios posteriores.

Las carencias de backend del índice anterior están implementadas; se ha eliminado ese índice vacío.
Comprobaciones específicas de interfaz: [PENDIENTES_UI.md](docs/ui/PENDIENTES_UI.md).
No contar planes de inspiración o notas de implementación como otra cola de tareas.

## Última evidencia

- Clientes oficiales en Brave (08-09, 02:07): Codex y Claude responden con sus sesiones de suscripción; Claude delega a un worker Codex, con resultado registrado, aprobación y cero cambios de archivos. Corregidos esquemas MCP incompatibles y descripciones ausentes de herramientas textuales. Bloque de **234 pruebas correctas**, controles de transporte de memoria y etiquetas de modelo, TypeScript y compilación correctos.

- Suite completa: **13.160 correctas, 81 omitidas y tres fallos de aislamiento de ajustes** (`logs/checkpoint-verified-clients-20260908.xml`). Los tests de clientes externos leían la configuración real de la máquina; ahora usan los valores de fábrica sin modificar las conexiones del usuario. Bloque posterior: **54 correctas**, incluyendo regresiones del catálogo de skills desactivado. Sin errores de teardown. TypeScript y compilación de producción correctos.
- Navegador Brave y Qwen local: objetivo OBJ-1 creado en proyecto sin carpeta, conservado tras recarga; trabajo continuado fuera del chat. Respuesta inglesa tras herramienta y texto entre rondas completos tanto en vivo como tras recargar (08-09, 01:15).
- Objetivos y aislamiento: **225 correctas** (`logs/astra-objective-scope-focused.xml`).
- Integración: **203 correctas** (`logs/astra-objective-scope-integration.xml`).
- Servidor reiniciado el 08-09: HTTP 200.
- TypeScript y compilación de producción correctos. Separada la caché de React sin adelantar la carga del editor; ya no aparece el aviso de tamaño del bundle.
