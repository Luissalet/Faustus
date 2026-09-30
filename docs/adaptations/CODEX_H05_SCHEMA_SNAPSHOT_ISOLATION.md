# H05/H06 parcial: esquemas independientes al capturar el catálogo

Hallazgo nuevo, reproducido: modificar una propiedad anidada en la exportación
de `read_file` cambiaba tanto el fingerprint del snapshot como el schema nativo
global. Una actualización anidada del schema MCP cambiaba retrospectivamente el
catálogo ya capturado. Las copias superficiales conservaban listas y diccionarios
compartidos.

`ToolDescriptor` ahora copia en profundidad los schemas de entrada/salida al
parsear y exportar. Así una exportación modificada no reescribe el catálogo ni
el origen, y una actualización del proveedor aparece en el siguiente snapshot.
La forma serializada y los fingerprints para contenido sin cambios se conservan.

Tres regresiones fallaron antes y pasaron después: exportación nativa, actualización
MCP anidada y parseo/exportación de ambos schemas. Selección inicial: **46 pruebas
correctas** de aislamiento, round-trip y registro; la selección ampliada con
contratos y fixtures terminó con **118 pruebas correctas**.

Alcance: evita aliasing accidental entre origen, descriptor parseado y exportación.
No hace inmutables los diccionarios expuestos por el propio descriptor, no captura
una política por paso ni sustituye la comprobación de revocaciones al despachar.
H05/H06 completos siguen pendientes. No requiere servicio MCP real.

Fuente conceptual ya visitada: [diseño H05/H06](CODEX_HARNESS_ANALISIS_2026-09-29.md)
y [Codex b1e72963](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9).
Implementación propia; no copia de código de terceros.


### VISITADO / IMPLEMENTADO — H05/H06 argumentos del candidato que respondió, 30-09-2026

`1c854de1`: preparación copia profundamente las herramientas aun con slimming desactivado y captura parámetros serializados inmutables por candidato/ronda. Validación y reparación de builtins usan esa misma proyección preparada para el candidato que respondió; eliminar o cambiar su definición en el catálogo live no cambia el contrato de argumentos. Captura ausente, obsoleta, textual, duplicada o herramienta no ofrecida deja recibo explícito de compatibilidad legacy, sin sustituir candidato cero. PDF y MCP mantienen sus contratos separados; schemas suministrados para nombres desconocidos no crean autoridad ni un nuevo validador.

Repro: write_file ofrecía content:string, una mutación live durante espera lo convertía en boolean; antes se intentaba reparar 'false' a False y el convertidor fallaba. El test de fallback usa stream_agent_loop, preparación y handler real de escritura: candidato1 con contrato string escribe el texto literal false pese a mutaciones del origen preparado y catálogo global. Además cubre aislamiento anidado, definición live eliminada, round/candidato errado, duplicate/text-only/legacy y propagación del recibo a tool_output y metrics.tool_events sin parámetros, argumentos ni owner.

Final: 126 pruebas correctas en 11,42 s en 12 suites, 16 nuevas; coordinador repite 16 nuevas correctas en 3,09 s. LLM sintético/archivo temporal real, sin GPU ni proveedor externo. Recibo stage=candidate_prepared, scope=argument_validation: llm_core puede adaptar el protocolo después; no prueba universal del schema visto por el proveedor, lease de ejecutor ni autorización general por paso. No se encontró escritor UI actual del catálogo builtin: el repro ejercita mutabilidad expuesta y actualización durante await, no se atribuye un incidente UI observado. Referencia https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core/src/tools/registry.rs, ya visitada. Piloto cerrado; H05/H06 globales parciales.


### VISITADO / EVALUADO — H05 prepared→builder de proveedor

Builders reales con schema nested boolean/array/required/additionalProperties: Harmony conserva parameters y nombres con roundtrip; Ollama conserva contrato; Anthropic input_schema igual; OpenAIchat tools sin rewrite. Original sin mutación. No se encontró bug nuevo de tipo/required para corregir. Smalltalk puede omitir tools; subscriptionResponses también: omisión deliberada, no wireauthority certificada. Recibo actual declara candidate_prepared/shadow/notcomparable; no promesa request_sent. Sin fakePOST benchmark ni nueva implementación. Autoridad de schemas efectivamente enviados requiere piloto distinto y sigue pendiente. Fuente https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9 y builderslocales ya visitados. No repetir esta comparación sin nueva pregunta/version.
