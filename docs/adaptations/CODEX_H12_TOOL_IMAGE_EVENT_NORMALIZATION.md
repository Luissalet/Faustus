# H12 parcial: normalización del evento de imagen de herramienta

## Reproducción y alcance

El consumidor de screenshots del evento en vivo, en `src/agent_loop.py`, toma
directamente `result["images"][0]` y exige `mimeType` y base64 sin prefijo. El
contrato existente de `src/tool_images.py:normalize_result_images` admite también
`mime_type`, URL de datos dentro de `data` y entradas inválidas antes de una
imagen utilizable. El helper `screenshot_data_url` ya aplica esa autoridad.

Con una imagen PNG sintética, el bloque real del consumidor produce un KeyError
para `mime_type`, duplica el prefijo de una URL de datos y produce TypeError
cuando la primera entrada es inválida aunque la siguiente sea utilizable. El
bloque está antes de `tool_events.append` y de la creación del registro que
alimenta `_append_tool_results`; no tiene un try envolvente en la función del
agente. Las excepciones impiden alcanzar esas operaciones en esta ejecución.

El incremento sustituye esa construcción por el helper existente y añade el
campo screenshot sólo cuando éste devuelve una URL no vacía. También respeta
el screenshot explícito que el helper existente reconoce.
No cambia generación de imágenes, interfaz, asociación de captions, política
de confianza ni rehidratación del historial. La persistencia de screenshots ya
normaliza y reduce la primera imagen utilizable mediante su helper específico.
La persistencia de fotos del usuario conserva referencias y descripción en
lugar de duplicar los bloques con bytes; es una política distinta y deliberada.

## Pruebas antes del cambio

`tests/test_tool_image_event_normalization.py` compila el segmento real entre
los comentarios del consumidor y la vista del navegador. No modifica el AST
ni reemplaza su lógica por una implementación de prueba. Una comprobación
estructural exige que la extracción contenga el acceso al campo screenshot.
Se omite importar el agente completo y sus servicios; el helper común es real.

El PNG se genera en un directorio temporal. Se comprueban forma canónica,
alias MIME, URL de datos con y sin MIME explícito, primera entrada inválida,
ausencia de imágenes y entradas inutilizables. Los resultados deben conservar
el objeto original y permitir continuar el consumidor sin excepciones. Esta
prueba del segmento no certifica por sí sola una ronda completa ni reapertura
SQLite. No se invocan modelos ni se utilizan datos personales.

Validación inicial: `venv/Scripts/python.exe -m pytest
tests/test_tool_image_event_normalization.py -q`: **7 fallos esperados y 3
correctas en 2,92 s**, exit 1. Los fallos corresponden al alias, las dos formas
URL de datos, primera entrada inválida y tres formas inutilizables. La forma
canónica y las dos ausencias ya funcionan.

Tras liberar el archivo compartido, se cambió únicamente el bloque del
consumidor. No se modifica `tool_images.py`.

Validación final: las 10 pruebas nuevas junto con `test_tool_result_images.py`,
`test_agent_loop_tool_images.py`, `test_tool_image_budget.py` y
`test_vision_routing.py`: **73 correctas en 27,30 s**, exit 0. Se verifica la
normalización, entrega al modelo, presupuesto y proyección por ruta existentes.

## Fuente y límites

Incremento propio de H12 del [análisis fijado](CODEX_HARNESS_ANALISIS_2026-09-29.md),
con referencia conceptual original
[Codex b1e72963](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9).
La autoridad concreta de formas admitidas es el normalizador local existente;
no se introduce un nuevo esquema multimodal ni se certifican proveedores.
Sólo se proyecta la primera imagen utilizable en el evento, como antes; el
historial del modelo conserva su tratamiento independiente de todas las imágenes.
