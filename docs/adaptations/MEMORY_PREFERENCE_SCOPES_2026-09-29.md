# Preferencias compatibles en ámbitos explícitos distintos

Método que motiva el piloto: evaluación por pregunta de
[Hindsight](https://github.com/vectorize-io/hindsight) y
[Agent Memory Benchmark](https://github.com/vectorize-io/agent-memory-benchmark).
Continúa el [piloto de alias](MEMORY_LANGUAGE_ALIASES_2026-09-29.md), sin repetir
la revisión de los proyectos originales.

La fila real de conflicto en el corpus unía estas dos preferencias:

- Alice prefers 2-space indentation for JavaScript and TypeScript.
- Alice prefers pytest over unittest for Python tests.

Ambas tenían el sujeto «Alice» y el predicado «prefers». El detector trataba todo
el objeto como un valor único, por lo que marcaba la primera como contradicha y
le aplicaba una penalización. Eso permitía que una regla de sangría Python
quedase por delante aunque la coincidencia léxica JavaScript fuera superior.

Implementado: para `prefers`/`prefiere`, cuando ambos objetos tienen un
calificador final explícito `for`/`para` y estos son distintos, no se abre un
conflicto determinista por valor diferente. Se comparan literalmente tras
normalizar mayúsculas y alias completos JS/JavaScript, TS/TypeScript. No se
infieren atributos desde frases sin calificador. Así funciona también con
preferencias de trabajo/viajes; no depende del texto ni del nombre del corpus.
Si los calificadores coinciden, la contradicción sigue detectándose. El cambio
de lenguaje en «Atlas uses Python» frente a «Atlas uses JavaScript» conserva la
detección previa porque el lenguaje ahí es el valor, no un calificador.

La regla es conservadora: ámbitos escritos de forma diferente pueden solaparse;
la desigualdad textual deja ese caso sin conflicto determinista, elegible para
revisión consultiva. No significa demostrar que ambos sean compatibles ni
resolver toda extracción de atributos. Solo afecta a conflictos nuevos; no
borra ni reinterpreta automáticamente filas históricas existentes.

## Resultado y comprobación

Mismo corpus, reloj, ranking y embeddings hash, sin editar fixtures:

| Métrica | Tras alias | Tras respetar ámbito |
|---|---:|---:|
| Posición correcta `q_indent` | 2 | 1 |
| MRR | 0,901042 | 0,916667 |
| Acierto@1 | 0,812500 | 0,843750 |
| Acierto@3 / @5 | 1 / 1 | 1 / 1 |
| Recall@5 | 0,984375 | 0,984375 |
| Fugas prohibidas | 0 | 0 |

Ninguna de las 36 consultas empeora rango recíproco ni recall@5.
[Resultados completos](memory-preference-scopes-2026-09-29.json).
45 pruebas pasan en 21,57 s: clasificación bilingüe, alias de ámbito,
contradicciones reales del mismo ámbito, escritura sobre SQLite aislado,
detector previo y benchmark completo.

```powershell
venv/Scripts/python.exe -m pytest tests/test_memory_conflict_preference_scopes.py tests/test_memory_conflicts.py tests/test_bench_memory_recall.py -q
```
