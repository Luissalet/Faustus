# Mem0: piloto de atribución de recuerdos en inglés y español

Fecha: 29-09-2026. Fuente original: [mem0ai/mem0](https://github.com/mem0ai/mem0), ya revisada en el radar del día. Este piloto adapta el principio de extraer evidencia atribuible al usuario; no copia código externo ni ejecuta Mem0. No constituye una comparación de calidad con Mem0.

## Cambio probado

Los dos fallbacks deterministas de Faustus comparten ahora una exclusión conservadora de citas y ejemplos: comillas multilínea, guillemets, citas Markdown, bloques de código e inline code. Un pedido inicial de traducción, reescritura, resumen o corrección se omite entero porque su contenido puede ser material de trabajo de otra persona. El extractor de listas exige que la solicitud de recordar esté al inicio: mencionar «remember» o «recuerda» dentro de una tarea no basta. Siguen admitiéndose listas explícitas en inglés y español, con cortesía opcional.

La decisión sacrifica cobertura cuando una tarea también contiene datos personales. No es un analizador de hablantes ni una solución general a la atribución.

## Muestra y resultados

Corpus manual y sintético de **42 mensajes**, ejecutable en `tests/test_memory_attribution_corpus.py`. Mezcla declaraciones propias, citas de terceros, texto bilingüe, código, tareas, listas explícitas y roles assistant/tool. Los negativos españoles que contienen declaraciones inglesas evitan que la ausencia de soporte español oculte un fallo de atribución.

| Resultado sobre recuerdos esperados | Cantidad |
|---|---:|
| Recuerdos correctos emitidos | 12 |
| Recuerdos incorrectos emitidos | 0 |
| Recuerdos esperados omitidos | 4 |

**Precisión observada en esta muestra: 12/12 (100 %); cobertura: 12/16 (75 %).** Los otros 26 mensajes no contienen recuerdos admisibles y no produjeron candidatos. El silencio ante las cuatro declaraciones personales españolas se cuenta como omisión, nunca como acierto: el fallback de prosa libre sigue siendo inglés. Las seis listas explícitas EN/ES sí se recuperan; seis declaraciones propias reconocidas, incluidas dos mezcladas con citas, completan los doce aciertos. La muestra está diseñada para regresiones, no es aleatoria ni independiente del desarrollo; estos porcentajes no estiman precisión global ni real.

## Validación

66 pruebas correctas con `venv/Scripts/python.exe -m pytest` sobre el corpus, listas, preferencias, mensajes malformados, filas del extractor, degradación del índice vectorial, aislamiento de propietarios, hechos volátiles e importaciones. No se utilizó un LLM ni red, ni se modificaron recuerdos reales.

## Límites y continuación

- No se evaluó extracción con LLM, rendimiento real en producción ni idiomas adicionales.
- La prosa libre española necesita un extractor específico o la ruta LLM; esta entrega mide la carencia y no afirma resolverla.
- Atribución indirecta sin comillas, Markdown anidado o malformado y paráfrasis de tareas requieren un corpus adicional. Las exclusiones actuales no ofrecen garantías para esos casos.
- Suprimir citas puede perder preferencias propias expresadas entre comillas; se prioriza evitar atribuciones erróneas en el fallback.
- Un porcentaje sin falsos positivos en 42 mensajes no permite concluir seguridad general. Ampliar con conversaciones anonimizadas y evaluación separada antes de extender los patrones.

Estado: cambio implementado y muestra EN/ES evaluada; limitaciones registradas, sin declarar soporte universal.
