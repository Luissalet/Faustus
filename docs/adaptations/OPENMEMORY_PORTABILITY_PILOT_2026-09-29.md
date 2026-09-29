# Portabilidad de conversaciones: piloto propio

Referencia de la exploración: [OpenMemory](https://github.com/mem0ai/openmemory). Se aprovecha el exportador e importador existentes de Faustus; no se instala ni ejecuta OpenMemory.

El recorrido probado usa `chat_export.transcript_to_dict`, vista previa de `history_import.import_path`, importación real a un archivo SQLite temporal, lectura, búsqueda y segunda importación idempotente. El texto contiene una instrucción de recordar y una llamada a herramienta como datos exportados. El importador procesa texto pasivo: no llama al ejecutor de herramientas ni al extractor de recuerdos.

| Campo | Resultado |
|---|---|
| ID de sesión de origen | Identidad externa del archivo; repetir la importación actualiza la misma conversación |
| Texto, roles, fechas | Conservados en mensajes ordenados |
| Título y modelo de conversación | Representados en el archivo |
| Adjuntos | No restaurados; el original JSON sigue siendo necesario |
| Llamadas, argumentos y resultados de herramientas | No restaurados como acciones o ledger de ejecución |
| Proyecto, workspace, modelo por mensaje, bloques de presentación | No reconstruidos como estado de sesión activa |
| Autorizaciones y memoria personal | No concedidas ni promovidas por importar el archivo |

El archivo se puede buscar; no es una migración reversible de toda la sesión. Una futura reanudación editable requiere un contrato propio para campos perdidos, referencias a medios y política de instrucciones importadas.

El piloto descubrió una colisión en las subidas: una vista previa del mismo nombre podía sobrescribir y después borrar un archivo anterior. Corregida en `9214976f`: directorio único por subida y basename preservado para mantener identidad de los formatos que lo usan.

Pruebas: `venv/Scripts/python.exe -m pytest -q tests/test_history_import.py tests/test_history_portability_pilot.py`. Son fixtures locales, sin datos privados ni llamadas a proveedores externos. La suite de importación pasó 99 pruebas y el nuevo recorrido pasó su prueba integral.
