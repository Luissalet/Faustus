# Piloto hardware/contexto — 29-09-2026

Estado: **arnés preparado y probado con dobles; medición real bloqueada**.

Referencia original: [Magnitude](https://github.com/magnitudedev/magnitude), ya
evaluado en el radar. Este paso adapta el método de comprobar una elección de
modelo/configuración con datos del equipo; no reabre su revisión ni copia código.

## Inspección no invasiva

- Existe un artefacto real local: `D:/LocalAI/models/qwen2.5-3b-instruct-q8_0.gguf`
  (nombre observado en el árbol de modelos; 3.616.088.480 bytes). Su existencia
  no demuestra que esté cargado ni que un servidor lo exponga.
- La configuración de Faustus nombra `qwen3.8-27b-q8-llamacpp` como modelo principal
  y `qwen2.5-3b-helper` como auxiliar. `warm_default_model` y
  `background_jobs_may_load_models` están desactivados. No se cambiaron ajustes.
- Una consulta de metadatos a `http://127.0.0.1:11434/api/ps`, con timeout de
  dos segundos y sin proxy de entorno, terminó en `httpx.ConnectTimeout`.
  No se obtuvo una lista de modelos residentes. No se inició ni descargó un modelo.
- El servicio ajeno del puerto 8090 identificado por el coordinador como posible
  fixture queda excluido; no es evidencia válida de inferencia real y no se utilizó.

## Reutilización y cambio

Faustus ya tiene `src/bench/runner.py`: planificación persistida, admisión de VRAM,
protección de modelos fijados/en uso, ejecución secuencial, métricas con procedencia,
comprobaciones de calidad e interrupciones. `src/vram_fit.py` ya separa estimaciones
de memoria de mediciones. No se añadió otro cliente de generación que eluda ese flujo.

`scripts/benchmark_context_pair.py` prepara dos perfiles del mismo modelo,
`num_ctx=2048` y `num_ctx=8192`, y ejecuta la misma suite `es_conversation` mediante
el arnés existente. Tres casos por perfil, presupuesto de 180 segundos por perfil
(comprobado entre casos por el arnés; no es un timeout duro de una generación).

- Endpoint y modelo son obligatorios; este piloto admite solo Ollama local en
  puerto 11434 para conservar la admisión existente. No pretende controlar el
  contexto de un llama-server que requiere opciones de arranque.
- Sin `--run` escribe únicamente el plan: no consulta la red ni carga un modelo.
- Con `--run` exige que el modelo solicitado ya aparezca residente en `/api/ps`
  antes de cada perfil. No instala, descarga, arranca ni detiene servidores.
  El cambio de `num_ctx` puede hacer que Ollama reconfigure/recargue ese modelo;
  por eso la ejecución se habilita explícitamente y reutiliza la admisión de Faustus.
- Guarda los resultados de cada perfil. Fallos, salida incompleta o errores en
  muestras detienen el segundo perfil y quedan registrados, sin convertirlos en éxito.
- `heuristics` contiene la duración prevista. Las muestras conservan métricas con
  sus etiquetas de origen (`reported_engine`, `observed_client`, `computed`, etc.).
  Nunca se convierte una velocidad estimada en velocidad observada.
- `requested_context` es **capacidad solicitada**, no longitud real del prompt.
  `resident_after.context_length` es el valor informado por Ollama o `null` si no
  está disponible. `contexts_verified` solo es verdadero si ambos valores observados
  coinciden con lo solicitado. Terminar la ejecución no acredita por sí solo esa coincidencia.

## Uso posterior con un modelo real residente

Desde Faustus, sustituyendo `MODELO_RESIDENTE` por su identificador exacto:

```text
venv/Scripts/python.exe -m scripts.benchmark_context_pair --endpoint http://127.0.0.1:11434/v1 --model MODELO_RESIDENTE --out data/benchmarks/context-pair.json
```

El mismo comando con `--run` ejecuta las llamadas. Revisar entonces identidad del
modelo, contexto observado, métricas de las muestras, calidad y errores. No cambiar
el modelo predeterminado en función de este piloto.

## Validación y pendiente real

**18 pruebas con dobles correctas**: plan sin red, rechazo de endpoint inadecuado o
con credenciales, bloqueo sin modelo residente, secuencia 2k/8k, huellas de perfiles
actualizadas, separación de heurísticas y observaciones, interrupción ante error,
metadatos de contexto malformados y modelo incorrecto.
Junto con las pruebas existentes del arnés y de métricas: **51 pruebas correctas**.

```text
venv/Scripts/python.exe -m pytest tests/test_benchmark_context_pair.py -q
venv/Scripts/python.exe -m pytest tests/test_benchmark_context_pair.py tests/test_bench_runner.py tests/test_execution_metrics.py -q
```

La comparación con hardware real **sigue pendiente** de un servicio Ollama listo
y un modelo real residente, o de preparar un adaptador de contexto para el servidor
llama.cpp que usa esta instalación. No hay cifras nuevas de velocidad, memoria ni
latencia medidas con un modelo real en este piloto. La suite corta compara capacidad
configurada; una evaluación de documentos de 2k/8k tokens requerirá además corpus
tokenizado y comprobar tokens de entrada, truncamiento, caché y repeticiones.
