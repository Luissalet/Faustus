# H12 parcial: reparar llamadas sin respuesta conservando contenido multimodal

Fuente del patrón: [Codex b1e72963](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9), análisis local H12/H13: distinguir historia causal y proyección al proveedor. Implementación propia; no se volvió a revisar el catálogo de 41 proyectos.

## Hallazgo y corrección

`_sanitize_llm_messages` retira llamadas sin respuesta de la proyección enviada al proveedor. Cuando el mismo mensaje assistant conserva contenido en bloques (texto o imagen), llamaba `.strip()` sobre una lista y fallaba antes de la siguiente petición. Seis regresiones reprodujeron el fallo antes del cambio.

Ahora reconoce contenido en bloques y lo conserva sin convertirlo a texto ni inventar un resultado para la llamada pendiente. El historial suministrado no se modifica; sus IDs y llamadas pendientes siguen intactos. Texto vacío, espacios y listas vacías mantienen el comportamiento anterior. La proyección resultante es idempotente en los casos probados.

## Evidencia y límites

94 pruebas correctas: regresiones nuevas (10), saneamiento de llamadas, mezcla multimodal, reasoning, replay de aprobaciones y compactor. Sin servicios remotos ni modelos reales.

Este cambio no crea una historia canónica nueva, recibos de reparación ni un renderer común para todos los protocolos. Tampoco demuestra que cada proveedor acepte imágenes producidas por assistant: solo evita el fallo y conserva los bloques en esta etapa. La generación/edición de imágenes en el chat sigue siendo una prioridad posterior, no una capacidad completada aquí.
