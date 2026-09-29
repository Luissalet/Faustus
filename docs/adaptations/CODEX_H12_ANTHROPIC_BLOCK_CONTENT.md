# H12 parcial: contenido assistant en bloques junto a llamadas Anthropic

Patrón original: [Codex b1e72963](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9), análisis local H12/H13: separar historia causal y proyección por proveedor. Implementación propia sobre Faustus; no se reexaminó el catálogo de 41 proyectos.

## Contrato consultado y alcance

La nueva pregunta de protocolo se comprobó en documentación primaria de Claude el 2026-09-30. [Handle tool calls](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls) muestra respuestas assistant con bloques de texto y `tool_use`, y pide devolver la respuesta en la conversación al enviar sus resultados. [Tool use overview](https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview) utiliza `response.content` como contenido assistant. [Messages API](https://platform.claude.com/docs/en/api/http/messages) exige conservar orden y firma de los bloques thinking; los datos de redacted thinking son opacos y deben mantenerse intactos.

Esto justifica conservar bloques de texto y reasoning en lugar de envolver una lista completa en un campo textual. La [guía de visión](https://platform.claude.com/docs/en/build-with-claude/vision) ejemplifica imágenes en mensajes user y no establece soporte de imagen producida por assistant. Este cambio no declara ese soporte, generación de imágenes ni aceptación remota de todos los bloques.

## Fallo y cambio

`_sanitize_llm_messages` conservaba correctamente un mensaje assistant con contenido en bloques y una llamada con respuesta adyacente. Sin embargo, `_build_anthropic_payload` trataba ese contenido como prosa y generaba `{"type":"text","text":[{"type":"text","text":"Inspecting file"}]}` antes del `tool_use`. El campo `text` dejaba de ser string; el fallo se reproduce sin imágenes ni modelos.

La rama assistant con `tool_calls` ahora distingue listas: añade los bloques mediante `_convert_openai_content_to_anthropic`, igual que la rama sin llamadas. Para prosa conserva el comportamiento anterior. Mantiene thinking recuperado de metadata antes del contenido y llamadas, conserva IDs y argumentos, y no modifica la historia original.

No introduce un validador nuevo ni una lista de tipos permitidos. Bloques desconocidos siguen el passthrough del conversor existente; el test correspondiente comprueba paridad y conservación de datos, no soporte del proveedor. Tampoco cambia los errores existentes para formatos arbitrarios, las respuestas tool, permisos, ledger, compactor o la política de continuación. Si reasoning está duplicado entre contenido y metadata, continúa sin deduplicarlo: ese problema requiere su propio contrato.

## Evidencia

La nueva suite usa imports y renderer reales, con fixtures sintéticos. Antes del cambio: **5 fallan y 4 pasan**. Después: **100 pasan**, 4,49 s, incluyendo las 9 nuevas regresiones, bucle thinking firmado, cache Anthropic, temperatura y saneamiento multimodal/llamadas/reasoning.

Se cubren bloques textuales múltiples con y sin cache, citas, string tradicional, contenido ausente o vacío, IDs y argumentos de llamadas/resultados, thinking firmado y redacted en metadata y en contenido, campos opacos, repetibilidad del payload e historia intacta. No hubo peticiones reales, servicios remotos ni modelos cargados.

Comando:

```text
venv/Scripts/python.exe -m pytest tests/test_anthropic_assistant_block_content.py tests/test_anthropic_thinking_tool_loop.py tests/test_llm_core_anthropic_cache.py tests/test_llm_core_anthropic_temp_clamp.py tests/test_llm_core_anthropic_temp_omit.py tests/test_llm_core_temperature_anthropic.py tests/test_sanitize_unanswered_multimodal.py tests/test_sanitize_multimodal_merge.py tests/test_llm_core_sanitize_tool_calls.py tests/test_sanitize_preserves_reasoning.py -q
```

H12 sigue parcial: faltan historia canónica, recibos de reparación y renderer común. H17 no queda resuelto por esta corrección.
