# Presupuesto de memoria desglosado — INF-05 §11/§14 (Lote A)

`src/memory_budget.py` + `GET /api/hardware/budget` y `GET /api/hardware/
context-limits`. Antes de este contrato, "cuánta VRAM usa esta GPU" era un
único número (`gpu_shared_memory.vram_snapshot`'s `used`) sin decir de
quién, y "cabría este modelo" mezclaba pesos, caché KV y margen del sistema
en la misma estimación que ya usa `vram_admission`/`vram_fit`. Este módulo
separa las dos preguntas — "qué hay ahora" y "qué costaría cargar esto" — y
en ninguna de las dos inventa un número que los datos no sostienen.

## Qué es un `MemoryBudget`

Uno por **GPU física** (`gpu_key = identity_key(...)`, nunca un índice).
Dos motores en la misma tarjeta (Ollama y un `llama-server` gestionado por
Faustus, por ejemplo) producen **un solo** presupuesto con dos
`consumers` — nunca dos presupuestos separados que un lector podría sumar
dos veces.

```json
{
  "gpu_key": "uuid:GPU-...", "gpu_index": 0, "gpu_name": "RTX 4070 Ti",
  "total_bytes": 12878610432, "observed_at": "2026-09-12T00:00:00Z",
  "components": {
    "weights_resident": {"bytes": null, "source": "absent", "note": "..."},
    "kv_state": {"bytes": null, "source": "absent", "note": "..."},
    "buffers_runtime": {"bytes": null, "source": "absent", "note": ""},
    "auxiliary_models": {"bytes": null, "source": "absent", "note": "not tracked in this reading"},
    "other_processes": {"bytes": 512000000, "source": "observed", "note": ""},
    "system_margin": {"bytes": 838860800, "source": "estimated", "note": "..."},
    "free": {"bytes": 2147483648, "source": "observed", "note": ""}
  },
  "consumers": [
    {"kind": "ollama", "label": "qwen3.8:8b", "pid": 4821, "bytes": 8500000000, "source": "observed"},
    {"kind": "faustus_serve", "label": "faustus serve · serve-abc123", "pid": null, "bytes": null, "source": "absent"}
  ],
  "shared_spill": {"bytes": null, "source": "absent", "note": ""},
  "stale": false
}
```

- **Nunca 0 para "no observado"**: cada componente y cada consumidor lleva
  su propio `source` (`observed | reported_engine | estimated | manual |
  absent`); `absent` es el valor honesto cuando no se sabe, no `0`.
- **`weights_resident`/`kv_state` quedan `absent` en este lote**: separarlos
  por GPU exige el tamaño en disco de cada modelo residente (no forma parte
  de `/api/ps`) emparejado con una tasa KV medida — `physical_budgets` no
  recibe esos tamaños de fichero como entrada. El total por modelo SÍ está,
  en `consumers[].bytes` — ver "Límites pendientes".
- **`shared_spill`**: la memoria compartida WDDM del runner de Ollama
  (`gpu_shared_memory.collect()`), mostrada, **jamás sumada** a VRAM ni a
  `free`.
- **`stale`**: `True` cuando la lectura de topología es más vieja que
  `max_age_s` (30 s por defecto). Una lectura obsoleta **nunca** autoriza
  una carga — lo aplica `admissible()`, no la construcción del presupuesto.

## `GET /api/hardware/budget?endpoint=&model=&ctx=&slots=&weights_bytes=`

Construye un `physical_budgets(...)` por cada GPU física visible, y —
cuando `model` o `weights_bytes` viene en la query — una `CandidateEstimate`
para "cargar esto ahora". `endpoint` resuelve residentes de Ollama vía
`/api/ps` (solo si `endpoint` es un Ollama de esta máquina — cualquier otro
motor no tiene puerta que abrir aquí, igual que `vram_admission.
ollama_root`). El `verdict` de nivel superior se calcula contra la GPU con
más bytes libres conocidos entre las disponibles (la candidata más
plausible); cada `MemoryBudget` individual puede evaluarse aparte con
`memory_budget.admissible(budget, estimate)`.

```json
{
  "budgets": {"uuid:GPU-...": {"...": "MemoryBudget"}},
  "estimate": {
    "weights": {"bytes": 8000000000, "source": "observed", "note": ""},
    "kv_state": {"bytes": 512000000, "source": "observed",
                 "note": "observed overhead in this configuration (ctx 8192)"},
    "buffers": {"bytes": null, "source": "absent", "note": "..."},
    "margin": {"bytes": 838860800, "source": "estimated", "note": "..."},
    "total_lower": 9350860800, "total_upper": 9350860800,
    "complete": true, "basis": "single_observation",
    "validity": {"ctx_min": 8192, "ctx_max": 8192, "slots": 1},
    "notes": []
  },
  "verdict": {"verdict": "fits", "reason": "", "shortfall_bytes": null},
  "system_memory": {"ram_total": 137438953472, "ram_available": 90000000000,
                    "commit_total": null, "commit_limit": null,
                    "commit_available": null, "source": "psutil"}
}
```

### `CandidateEstimate.basis`

- **`single_observation`** — una sola medición KV (`vram_fit.KV_RATES`),
  válida solo en el `ctx` en que se midió (`validity.ctx_min ==
  validity.ctx_max`); pedir un `ctx` distinto marca `extrapolated beyond
  measured range` y ese término solo entra en `total_upper`, nunca en
  `total_lower`.
- **`fitted`** — dos o más observaciones a `ctx` distintos:
  `memory_budget.fit_overhead(...)` ajusta un término fijo + uno variable
  por mínimos cuadrados y publica el dominio `[ctx_min, ctx_max]`; fuera de
  ese dominio, lo mismo que arriba — solo a `total_upper`.
- **`metadata`** — sin observaciones pero con `arch.kv_per_token` (derivado
  por el llamador de los metadatos del modelo); conserva la nota de híbrido
  cuando `arch.hybrid` es verdadero (no se aplica atención densa a todas las
  capas).
- **`incomplete`** — arquitectura desconocida y sin observación: `complete:
  false`, `total_lower` = solo pesos + margen, `total_upper: null`. Nunca
  una cifra con falsa precisión.

`slots > 1` multiplica el KV por `slots`, añade la nota "measured with 1
slot; N slots is an extrapolation, not a guarantee" y ensancha
`total_upper` un 25 % adicional sobre ese KV. Para `vllm`/`sglang`,
`notes` incluye el aviso de KV paginado: la cifra es un suelo, no lo que el
motor realmente reservará (`gpu-memory-utilization` decide eso).

## Los tres límites de contexto — `GET /api/hardware/context-limits`

```json
{"limits": {
  "native": {"value": 8192, "source": "hf_config",
             "note": "extended by rope_scaling (yarn) to 32768: not a quality guarantee"},
  "configured": {"value": 32768, "source": "receipt", "note": ""},
  "evaluated": {"min": 4096, "max": 4096, "source": "bench_runs",
                "note": "largest observed prompt across 2 run(s)"}
}}
```

- **`native`** — lo que el modelo declara haber sido entrenado con. Cuando
  el `config.json` distingue `max_position_embeddings` (el valor ampliado)
  de `rope_scaling.original_max_position_embeddings` (el original), `native`
  es el **original**, nunca el ampliado — una extensión RoPE/YaRN no es una
  garantía de calidad, y presentarla como "nativa" lo sería.
- **`configured`** — lo que el motor en marcha tiene puesto de verdad:
  primero el recibo de lanzamiento observado (`receipt.observed.ctx` /
  `.num_ctx`), si no `model_load_options.resolve_for_request(...)`
  (`num_ctx` guardado), si no `absent` con nota "engine default not
  observed" — nunca un valor por defecto inventado.
- **`evaluated`** — el prompt más grande que un *run* de benchmark
  realmente envió (`src.bench.runner.list_runs`, filtrado por modelo/
  endpoint cuando se dan), `absent` si no hay ningún run que mencione ese
  modelo/endpoint.

## Límites pendientes

- El desglose pesos/KV/buffers **por GPU** (`components.weights_resident`,
  `.kv_state`, `.buffers_runtime`) queda `absent` — necesita el tamaño en
  disco de cada modelo residente, que `/api/ps` no da y que este lote no
  añade como entrada de `physical_budgets`. `consumers[].bytes` sí lleva el
  total por modelo/proceso.
- Los consumidores `faustus_serve` se atribuyen por los índices de GPU que
  el propio `plan` del recibo registró (`plan["gpus"]`) — una sesión sin ese
  campo (lanzamientos anteriores a este lote, o sin GPUs explícitas) no
  aparece atribuida a ninguna tarjeta.
- `fit_overhead`/`basis: "fitted"` está implementado y probado, pero
  `vram_fit.KV_RATES` hoy solo guarda **una** medición por modelo (la más
  reciente sobrescribe la anterior) — hasta que algo empiece a conservar un
  historial por `ctx`, las llamadas reales casi siempre verán
  `single_observation`, `metadata` o `incomplete`, nunca `fitted`.
- Nada de esto simula hardware: cada test parte de fixtures explícitas y
  `subprocess.run`/`_get` parcheados, nunca `nvidia-smi` real.
