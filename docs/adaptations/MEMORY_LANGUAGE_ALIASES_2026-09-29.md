# Memoria: abreviaturas de lenguajes en recuperación

Continuación del hallazgo `q_indent` en
[la evaluación de recuperación](RETRIEVAL_EVALUATION_2026-09-29.md).
Método original que motiva medir por pregunta:
[Agent Memory Benchmark](https://github.com/vectorize-io/agent-memory-benchmark)
y [Hindsight](https://github.com/vectorize-io/hindsight).

Implementado: BM25 canonicaliza las palabras completas `JS` → `JavaScript` y
`TS` → `TypeScript` cuando el propio texto contiene una pista explícita de
programación. Se aplica simétricamente a documentos y consultas. No cambia el
tokenizador hash, los embeddings persistidos, el corpus ni los pesos de ranking.
Nombres de archivos, identificadores y subcadenas siguen siendo literales.
Iniciales sin contexto, timestamps TS y los términos ambiguos Go/R no se expanden.
Una abreviatura sola o un texto sin las pistas reconocidas permanece literal.

Resultado del mismo corpus de 36 consultas, embeddings hash y reloj fijo:

| Métrica | Antes | Después |
|---|---:|---:|
| Posición del recuerdo esperado en `q_indent` | 6 | 2 |
| MRR global | 0,890625 | 0,901042 |
| Acierto@3 | 0,968750 | 1 |
| Acierto@5 | 0,968750 | 1 |
| Recall@5 | 0,953125 | 0,984375 |
| Fugas prohibidas | 0 | 0 |

Ninguna consulta pierde rango recíproco ni recall@5. El
[JSON completo](memory-language-aliases-2026-09-29.json) conserva las consultas.
105 pruebas pasan en 33,27 s: alias bidireccionales JS/TS, casos ambiguos,
identificadores, benchmark completo y motor de memoria.

```powershell
venv/Scripts/python.exe -m pytest tests/test_memory_search_language_aliases.py tests/test_bench_memory_recall.py tests/test_memory_engine.py -q
```

**Matiz resuelto en un cambio posterior:** Python todavía ocupaba la primera
posición porque la carga del corpus penalizaba la preferencia JavaScript.
La inspección de la fila de conflicto precisó que el otro recuerdo era
«Alice prefers pytest over unittest for Python tests», no la regla de sangría
Python. El BM25 corregido puntuaba mejor JavaScript, pero persistía esa
penalización. La corrección separada se documenta en
[ámbitos de preferencias](MEMORY_PREFERENCE_SCOPES_2026-09-29.md).
