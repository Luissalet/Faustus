# H12 parcial: reparar llamadas sin respuesta conservando contenido multimodal

Fuente del patrón: [Codex b1e72963](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9), análisis local H12/H13: distinguir historia causal y proyección al proveedor. Implementación propia; no se volvió a revisar el catálogo de 41 proyectos.

## Hallazgo y corrección

`_sanitize_llm_messages` retira llamadas sin respuesta de la proyección enviada al proveedor. Cuando el mismo mensaje assistant conserva contenido en bloques (texto o imagen), llamaba `.strip()` sobre una lista y fallaba antes de la siguiente petición. Seis regresiones reprodujeron el fallo antes del cambio.

Ahora reconoce contenido en bloques y lo conserva sin convertirlo a texto ni inventar un resultado para la llamada pendiente. El historial suministrado no se modifica; sus IDs y llamadas pendientes siguen intactos. Texto vacío, espacios y listas vacías mantienen el comportamiento anterior. La proyección resultante es idempotente en los casos probados.

## Evidencia y límites

94 pruebas correctas: regresiones nuevas (10), saneamiento de llamadas, mezcla multimodal, reasoning, replay de aprobaciones y compactor. Sin servicios remotos ni modelos reales.

Este cambio no crea una historia canónica nueva, recibos de reparación ni un renderer común para todos los protocolos. Tampoco demuestra que cada proveedor acepte imágenes producidas por assistant: solo evita el fallo y conserva los bloques en esta etapa. La generación/edición de imágenes en el chat sigue siendo una prioridad posterior, no una capacidad completada aquí.


## Segundo hallazgo visitado: marcador de contexto junto a una imagen

La revisión posterior encontró otro caso en la misma proyección: si el texto de
un mensaje assistant era un marcador legacy (`Reference context received.` o
`<<faustus_ctx_ack>>`) y coexistía con una imagen, el detector extraía solo el
texto y reemplazaba la lista completa por una cadena. Sin tool_calls perdía la
imagen inmediatamente; con una llamada pendiente, la primera reparación la
conservaba y una segunda llamada al saneador la eliminaba. El original permanecía
intacto, pero la proyección no era idempotente y dejaba de ser multimodal.

Se limita esa reescritura a contenido de tipo string: su propósito original era
reparar una respuesta entera de prosa legacy. Las listas de bloques se conservan
sin reescribirlos; tampoco cambia la condición relativa a tool_calls. No modifica
renderers de proveedor, permisos ni generación de imágenes.

Antes: **12 casos nuevos fallan** por imagen eliminada (dos marcadores, con/sin
llamada pendiente, provider None/Ollama/Anthropic). Después: **128 pruebas pasan**,
4,37 s. Se comprueba también que el marcador legacy en string puro sigue
normalizándose y la historia original permanece intacta. Comando:

`venv/Scripts/python.exe -m pytest tests/test_sanitize_unanswered_multimodal.py
tests/test_sanitize_multimodal_merge.py tests/test_llm_core_sanitize_tool_calls.py
tests/test_sanitize_preserves_reasoning.py tests/test_context_compactor.py
tests/test_context_compactor_nonstring.py tests/test_context_compactor_regressions.py -q`

Sigue siendo una corrección local de proyección. No demuestra idempotencia de
todos los formatos ni todos los proveedores; unificar historia/recibos de
reparación y política shadow de continuación H13 continúa pendiente. Misma fuente
fijada e implementación propia; no hubo nueva revisión upstream.
