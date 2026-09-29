# Piloto temporal adversarial — 29-09-2026

Estado: **implementado y probado en el motor local**, con los límites indicados abajo.

## Fuente y matiz visitado

Inspiración: [Graphiti](https://github.com/getzep/graphiti), revisión ya inspeccionada
`852ca401d89f54cf47fd66e11ee35724cabe202b`, especialmente su
[criterio de deduplicación de hechos](https://github.com/getzep/graphiti/blob/852ca401d89f54cf47fd66e11ee35724cabe202b/graphiti_core/prompts/dedupe_edges.py).
No se repitió la investigación ni se incorporó código del proyecto: se evaluó el
motor propio con eventos muy parecidos que difieren en fecha, cantidad o calificador.

## Fallo reproducido y cambio

Dos recuerdos con texto idéntico y vigencias separadas se fusionaban en la
curación. Al borrar uno se perdía un período histórico: el mismo estado puede
haber sido cierto antes, dejar de serlo y volver a ser cierto más tarde.

`src/memory_curator.py` exige ahora igualdad de ventanas declaradas y del estado
temporal tanto en la clave exacta como en la comparación difusa. Los inicios que
coinciden con `created_at`, sin final ni estado temporal, se tratan como el inicio
implícito que añade el motor: las inserciones ordinarias a distintas horas siguen
deduplicándose. También conserva fechas
escritas con meses y días de la semana en español o inglés: una variación de
una sola palabra en una frase larga puede superar el umbral de similitud.
Las protecciones previas de cifras y calificadores se mantienen.

La política es conservadora incluso para períodos solapados. Fusionarlos sin
almacenar múltiples períodos y su procedencia descartaría o inventaría vigencia.
Los duplicados con la misma vigencia siguen fusionando evidencias y la segunda
curación es idempotente. Hay una ambigüedad del esquema actual: no conserva si
`valid_from == created_at` se pasó explícitamente o se asignó por defecto.
En ese caso abierto, sin estado temporal, se conserva la política anterior de
deduplicación; una marca de origen de vigencia permitiría distinguirlos en el futuro.

## Evidencia y cobertura

- Antes de la corrección: el archivo heredado del agente interrumpido obtuvo
  **1 fallo y 6 aciertos**; falló el caso de texto idéntico con períodos separados.
- Se reforzaron las frases de los casos difusos para que superen explícitamente
  el umbral. La versión heredada no lo garantizaba y podía pasar sin ejercer esa ruta.
- Corpus final: **15 pruebas**. Fechas por meses en inglés/español, días de la
  semana, cantidades, calificador `only`; vigencias separadas con texto exacto y
  difuso; consulta histórica, intervalo vacío y consulta actual; vigencias
  solapadas con cambio de inicio/final o final abierto; evidencias compartidas,
  idempotencia, deduplicación ordinaria entre horas distintas por ruta exacta y
  difusa, separación de vigencia futura frente a inicio implícito y sustitución
  legítima de residencia Madrid → Lisboa.
- Suite de memoria y temporal: **285 pruebas correctas** con el corpus final.

Comandos usados, desde Faustus con su entorno:

```text
venv/Scripts/python.exe -m pytest tests/test_memory_temporal_adversarial.py tests/test_memory_engine.py tests/test_brain_temporal.py tests/test_brain_temporal_regressions.py tests/test_brain_temporal_chronology.py tests/test_brain_temporal_supersede.py -q
venv/Scripts/python.exe -m pytest tests/test_memory_temporal_adversarial.py -q
git diff --check -- src/memory_curator.py
```

## Límites

Este corpus cierra la comprobación local de los casos temporales señalados; no es
una evaluación exhaustiva de lenguaje natural ni un reemplazo del modelo temporal
de Graphiti. La protección de palabras calendario admite falsos negativos de
deduplicación (por ejemplo, `May` como nombre). No cubre todas las abreviaturas,
fechas escritas completamente con palabras o todos los idiomas/calificadores.
La igualdad de ventanas no agrega intervalos ni migra datos. Las pruebas usan el
almacenamiento local y la recuperación textual, sin medir recuperación vectorial
ni calidad de respuestas generadas por un modelo.
