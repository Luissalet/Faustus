# Evaluación offline: recuperar solo cuando aporta y medir por pregunta

Estado: **evaluado** el piloto local OpenViking y el desglose del benchmark existente.
No se modifica producción. No se ejecutan modelos ni proveedores externos.

Fuentes originales conservadas: [OpenViking](https://github.com/volcengine/OpenViking),
[Hindsight](https://github.com/vectorize-io/hindsight) y
[Agent Memory Benchmark](https://github.com/vectorize-io/agent-memory-benchmark).
Esta ejecución aplica los métodos ya inspeccionados en el radar; no constituye una
nueva revisión de sus versiones ni reproduce sus benchmarks oficiales.

## OpenViking: saludo frente a pregunta sobre memoria

Se ejecuta el compilador real de Faustus, con sus adaptadores `MemoryEngineSource`
y `SessionSource`. SQLite temporal, corpus fijo existente y embeddings hash.
Se registra el manifiesto real del paquete. Cada muestra usa una caché de trabajo
nueva; el proceso y los imports están calientes. Un calentamiento por caso se excluye.
El reloj del compilador y de la búsqueda se fija al del corpus para evitar caducidades
accidentales. No se simulan demoras ni se sustituyen resultados de búsqueda.

| Caso | Muestras | Búsquedas memoria por turno | p50 local | Rango local | Tokens estimados del paquete |
|---|---:|---:|---:|---:|---:|
| `¡Hola!` | 12 | 0 | 6,985 ms | 6,435–10,071 ms | 177 |
| `Hello, what editor does Alice prefer?` | 12 | 1 | 42,360 ms | 41,224–275,489 ms | 371 |

Las 26 compilaciones (incluidos calentamientos) conservan los cinco bloques
obligatorios y ambos mensajes del historial. La pregunta recupera la referencia
exacta de la preferencia Neovim. Ningún paquete declara degradación.
El saludo evita trabajo opcional; el prefijo de saludo en una petición real no
suprime memoria necesaria. Los tokens son los estimados por el compilador.

Estos tiempos pertenecen a este proceso y corpus pequeños. El máximo observado
de 275 ms muestra variabilidad local; no se oculta ni se convierte la mediana en
un compromiso de rendimiento. **No son latencia de respuesta del chat, del modelo
ni de embeddings neuronales**. No se recorren aquí rutas HTTP, preámbulos previos,
otras fuentes o proveedores; por tanto no se afirma cero recuperación extremo a
extremo. Queda separada una medición con el modelo/proveedor real del usuario.

## Hindsight / benchmark: resultados por tipo de pregunta

Se reutiliza `src.bench.memory_recall.run()` sin alterar corpus, resultados o
umbrales. 36 consultas: 32 positivas y 4 sondas negativas. Las negativas se evalúan
por fugas y no se contabilizan como fallos de acierto. La categorización se deriva
de los ítems esperados; los hechos corregidos y las sondas tienen grupos propios.

| Tipo | Consultas | MRR | Acierto@5 | Recall@5 | Fugas prohibidas |
|---|---:|---:|---:|---:|---:|
| Preferencias | 6 | 0,777778 | 0,833333 | 0,833333 | 0 |
| Hechos | 7 | 0,904762 | 1 | 1 | 0 |
| Procedimientos | 7 | 0,857143 | 1 | 1 | 0 |
| Decisiones | 7 | 1 | 1 | 1 | 0 |
| Hechos corregidos | 3 | 0,833333 | 1 | 1 | 0 |
| Equivalentes de tipos mixtos | 2 | 1 | 1 | 0,75 | 0 |
| Caducidad, sondas negativas | 2 | — | — | — | 0 |
| Aislamiento, sondas negativas | 2 | — | — | — | 0 |

Global: MRR 0,890625; acierto@5 0,968750; recall@5 0,953125; cero fugas
entre los resultados examinados (hasta 30 por consulta). Corrección, bloqueo de
resurrección, olvido y vigencia futura: 7/7 comprobaciones del ciclo pasan.

**Hallazgo pendiente de mejora:** `q_indent`, “How many spaces does Alice use for
JS indentation?”, coloca la preferencia JavaScript en sexto lugar. El primer
resultado habla de sangría Python. Es una pregunta nueva y concreta para el siguiente
piloto: desambiguar lenguajes y abreviaturas conservando aislamiento y sin ajustar
el corpus para ocultar el fallo. No se declara resuelta la recuperación semántica
real ni se interpreta este corpus hash como precisión de un modelo generativo.

## Reproducción y evidencia

Desde la raíz de Faustus:

```powershell
venv/Scripts/python.exe -m scripts.eval_retrieval_pilot --output docs/adaptations/retrieval-pilot-2026-09-29.json
venv/Scripts/python.exe -m pytest tests/test_context_engine_compiler.py tests/test_bench_memory_recall.py -q
```

El [JSON íntegro](retrieval-pilot-2026-09-29.json) conserva resultados por consulta,
rangos de tiempo, contadores, referencias esperadas y fugas. El script contiene
aserciones de preservación, recuperación necesaria y ausencia de degradación;
termina con error si falla una de ellas. Solo usa almacenes temporales.

Validación registrada: script completo correcto; **86 pruebas pasan en 27,85 s**.
