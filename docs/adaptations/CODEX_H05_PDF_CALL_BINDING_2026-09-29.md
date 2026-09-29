# H05/H06 parcial: captura PDF por llamada

Continuación de [contrato PDF de argumentos](CODEX_H05_PDF_ARGUMENT_CONTRACT_2026-09-29.md),
commit `3bfc22e2`. Fuente ya visitada:
[orquestación Codex, revisión fijada](https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core/src/tools/orchestrator.rs).
Implementación propia; no se repite revisión upstream.

## Comportamiento implementado

`execute_tool_block` captura, antes del primer await, el callable registrado en
`TOOL_HANDLERS` y el descriptor/contrato de argumentos de esa herramienta PDF.
Se resuelve una sola entrada; no se construye el catálogo de 220 herramientas.
El binding conserva contratos serializados, y cada consumidor recibe una copia.
La llamada transporta explícitamente ese binding hasta el handler; no existe
estado compartido de «última llamada». El parser del handler consume la misma
definición capturada para validar y completar defaults.

Una sustitución del handler o del contrato central mientras la llamada está
suspendida afecta a llamadas posteriores. La llamada iniciada conserva su par.
Eliminar la inscripción, deshabilitar la herramienta o denegarla por política
antes de despachar sigue impidiendo su ejecución. Una inscripción ausente al
capturar no puede habilitarse retroactivamente mediante el catálogo ni mediante
una inscripción tardía. Los demás controles del dispatcher se conservan.

Los resultados del handler incluyen `pdf_call_contract`: nombre, versión,
`scope=call` y SHA-256 del descriptor serializado. Este fingerprint identifica
el descriptor, **no el código del callable**, sus efectos ni una autorización.
No se añade en rechazos anteriores a la ejecución.

## Evidencia

Siete pruebas nuevas verifican llamadas concurrentes con handler/default distintos,
revocación por registro/deshabilitado/política, registro inicialmente ausente,
copias sin alias y consumo del default capturado por el handler PDF real con un
backend sintético. El test concurrente prohíbe construir `tool_registry.snapshot`.
Se conservan los fixtures PDF reales y los recorridos directo, nativo y Code Mode
del incremento anterior.

```text
venv/Scripts/python.exe -m pytest tests/test_pdf_call_binding.py tests/test_pdf_tool_contracts.py tests/test_pdf_tree.py tests/test_pdf_tree_argument_errors.py tests/test_tool_index_schema_parity.py tests/test_tool_registry.py tests/test_tool_registry_roundtrip.py tests/test_codex_h03_durable_email_intent.py tests/test_l62_call05_tool_result.py tests/test_teach_capture_result_status.py tests/test_command_timeout_outcomes.py -q
```

Resultado: **172 pruebas correctas**, incluidos intención durable H03,
normalización H04, timeouts y TeachMode.

## Límites pendientes

- Captura coherente sin await en el event loop; no es publicación atómica de
  handler/schema para hot-reload concurrente desde otros hilos.
- El contrato anunciado al modelo al principio del paso todavía no queda ligado
  a esta captura. H06 global sigue pendiente; esto es únicamente por llamada PDF.
- La captura no certifica que un handler arbitrario añadido por código de terceros
  respete el parser. La garantía se prueba para los tres handlers PDF propios.
- Invocar directamente un handler fuera del dispatcher usa el contrato central
  actual y no añade metadata de captura ni nuevos permisos.
- Los `30.000 ms` genéricos del descriptor siguen siendo metadata, sin timeout
  duro aplicado a PDF. No se anuncia aislamiento adicional ni cancelación nueva.

