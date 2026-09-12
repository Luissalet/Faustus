# Métricas de ejecución por turno — INF-03 §08

INF-03 (Lote A backend). Antes de este contrato, un turno mostraba
`tokens_per_second`/`time_to_first_token`/`response_time` como cifras únicas
sin decir de dónde salían: el mismo `tokens_per_second` podía ser la
velocidad de decodificación real que reportó el motor, o tokens totales
dividido entre el reloj de pared del turno completo (que incluye cola,
carga, prefill y herramientas) — dos números muy distintos con el mismo
nombre. `metrics.execution` (`src/contracts/inference.py::ExecutionMetrics`,
construido por `src/execution_metrics.py::build_execution_metrics`) separa
el turno en fases y marca, fase por fase, de dónde sale cada una.

Los campos existentes (`tokens_per_second`, `time_to_first_token`,
`response_time`, `prefill_tps`, ...) no cambian: `metrics.execution` es
aditivo, una segunda vista del mismo turno con más detalle, nunca un
reemplazo.

## Regla única

**Un número que no se observó es `absent`, nunca `0`.** Un `MetricValue`
(`{"value": number|null, "source"}`) con `source: "absent"` y
`value: null` significa "esto no se supo", no "esto tardó cero
milisegundos" ni "no hubo tokens". Ver `src/contracts/inference.py`,
`MetricValue`/`METRIC_SOURCES`.

## Las cinco fuentes (`MetricValue.source`)

| `source` | Qué significa |
|---|---|
| `observed_client` | Faustus mismo tomó dos relojes monotónicos y restó — cola en `vram_admission`, duración de una herramienta, `total_ms` del turno completo. |
| `reported_engine` | El motor lo dijo explícitamente en su propia respuesta (`done` de Ollama, `timings` de llama.cpp). El número más de fiar cuando existe. |
| `computed` | Faustus lo dedujo de dos timestamps propios cuando el motor no lo reportó, y las condiciones para esa resta eran limpias (un único turno, sin herramientas de por medio). |
| `inferred` | Lo mismo que `computed`, pero las condiciones NO eran limpias (hubo herramientas entre el primer token y el final) — el número existe pero mezcla fases, y siempre viene con una nota en `notes` explicando qué se mezcló. Nunca aparece sin nota. |
| `absent` | No se pudo establecer nada — ni el motor lo dio, ni las condiciones para deducirlo se cumplían. `value` es `null`. |

## Forma de `metrics.execution`

```json
{
  "schema_version": 1,
  "phases": {
    "queue_wait_ms":  {"value": 12.5,  "source": "observed_client"},
    "load_ms":        {"value": 50.0,  "source": "reported_engine"},
    "prefill_ms":     {"value": 200.0, "source": "reported_engine"},
    "generation_ms":  {"value": 900.0, "source": "reported_engine"},
    "tools_ms":       {"value": null,  "source": "absent"},
    "total_ms":       {"value": 1180.5,"source": "observed_client"}
  },
  "tokens": {
    "prompt":    {"value": 128, "source": "reported_engine"},
    "generated": {"value": 64,  "source": "reported_engine"}
  },
  "scope": "request",
  "engine": {"implementation": "ollama", "host": "127.0.0.1", "port": 11434,
             "managed": "external"},
  "observed_at": "2026-09-12T00:00:00Z",
  "notes": []
}
```

`total_ms` es la única fase que siempre tiene un valor (`observed_client`):
Faustus mide el turno completo con su propio reloj sin depender de nada que
el motor decida reportar o no. Las demás fases pueden faltar todas y
`total_ms` seguirá presente — ver T07 en `tests/test_execution_metrics.py`.

## Fase por fase, motor por motor

| Fase | Ollama (nativo, `/api/chat`) | llama.cpp / OpenAI-compatible (`timings`) | vLLM / OpenAI-compatible genérico | Cloud (OpenAI, OpenRouter, Anthropic, ...) |
|---|---|---|---|---|
| `queue_wait_ms` | `observed_client` si `src/vram_admission.py::admit()` midió espera para ESTE endpoint local; `absent` si no hay puerta (endpoint remoto, admisión en `off`). | igual que Ollama — la puerta es la misma función, independiente del motor detrás. | igual — la puerta mira el endpoint, no el motor. | `absent` siempre: la puerta de VRAM solo vigila Ollama local. |
| `load_ms` | `reported_engine` desde `load_duration` (ns→ms) cuando el `done` lo trae. | `absent` siempre — llama.cpp no reporta tiempo de carga por petición. | `absent` (sin bloque de timings equivalente hoy). | `absent`. |
| `prefill_ms` | `reported_engine` desde `prompt_eval_duration`; si el motor no lo dio, `computed` cuando el turno fue una única ronda sin herramientas, si no `absent`. | `reported_engine` desde `timings.prompt_ms`. | igual regla de `computed`/`absent` que Ollama cuando no hay bloque de timings. | normalmente `absent` (sin bloque de timings); `computed` solo si el camino de chat rastreara el primer token — hoy no lo hace, ver más abajo. |
| `generation_ms` | `reported_engine` desde `eval_duration`; si falta, `computed` (turno limpio) o `inferred` con nota (hubo herramientas de por medio). | `reported_engine` desde `timings.predicted_ms`. | igual regla `computed`/`inferred` que Ollama. | igual regla, sujeta a la misma limitación de primer token. |
| `tools_ms` | suma de `tool_events` con duración observada (`observed_client`); `absent` si ningún evento trae duración. | igual — no depende del motor, depende de qué herramientas corrieron. | igual. | igual. |
| `total_ms` | `observed_client` siempre (reloj propio de Faustus, fin − inicio). | igual. | igual. | igual. |
| `tokens.*` | `reported_engine` desde `prompt_eval_count`/`eval_count`, o `computed` (estimación por caracteres) cuando el motor no dio uso real. | `reported_engine` desde `usage.prompt_tokens`/`completion_tokens`. | igual. | igual. |

`engine_timings` (el bloque intermedio que `src/llm_core.py` añade al evento
SSE `usage`, no parte del contrato público) es lo único que distingue Ollama
de llama.cpp para estas fases: `{"load_ms", "prompt_ms", "predicted_ms",
"total_ms", "prompt_n", "predicted_n", "source": "ollama"|"llamacpp"}`, en
milisegundos, `null` en cualquier campo que el motor no reportó. Un
proveedor cloud u OpenAI-compatible sin bloque de *timings* no tiene esta
clave en absoluto — ausente, nunca `null` como valor de la clave completa.

## Por qué las fases no se suman como si fueran secuenciales

`prefill_ms + generation_ms + tools_ms` puede superar `total_ms`: el reloj
del motor y el reloj de Faustus no miden exactamente el mismo intervalo (hay
solape en los bordes, y una herramienta puede ejecutarse en paralelo con la
espera de red). Cuando eso ocurre, `build_execution_metrics` **no recorta ni
ajusta ninguna fase para que cuadre** — eso inventaría una precisión que no
existe. En su lugar añade a `notes`:

```
"phases overlap: engine and client clocks are not additive"
```

Un lector de la cronología debe tratar esa nota como "estas fases se
solapan, no las sumes tú tampoco" — nunca como un error a esconder.

## `prefill_ms`/`generation_ms` `computed` vs `inferred`

Cuando el motor no reportó estas dos fases directamente, Faustus las deduce
de sus propios timestamps (envío, primer token visible, fin del turno) SOLO
si las condiciones son limpias:

- **`computed`**: el turno fue una única ronda de generación, sin llamadas a
  herramientas de por medio. `prefill_ms` = primer_token − envío;
  `generation_ms` = fin − primer_token. Ambos números son directamente el
  prefill/la generación puros.
- **`inferred`**: hubo herramientas entre el primer token y el final del
  turno. `generation_ms` se sigue calculando igual (fin − primer_token),
  pero ese intervalo también incluye el tiempo de las herramientas — por
  eso nunca es `computed` en este caso, y siempre lleva una nota explicando
  qué se mezcló. `prefill_ms` en cambio queda `absent`: con herramientas de
  por medio no hay una única cifra de "prefill" que aislar de forma honesta.
- **`absent`**: el turno se interrumpió antes del primer token, o cualquier
  otra condición que impida establecer un número real.

## El camino de chat puro (`routes/chat_routes.py`)

El turno de chat sin agente (`chat_mode == "chat"`, sin herramientas) NO
rastrea un timestamp de "primer token visible" — ese bucle de manejo de
`delta` es anterior a INF-03, y añadir ese rastreo ahí habría sido un
cambio bastante mayor que este lote. Como consecuencia, en este camino
`prefill_ms`/`generation_ms` **nunca son `computed`**: solo aparecen cuando
el motor los reportó directamente (`reported_engine`) vía `engine_timings`,
y quedan `absent` en cualquier otro caso — nunca una cifra deducida con
menos evidencia que el camino del agente. `queue_wait_ms` sí se mide en este
camino (la puerta de VRAM ya corre aquí, antes de la primera llamada al
motor); `tools_ms` no aplica (no hay herramientas en este modo).

## Enlace con Scorecard

`src/scorecard.py::build_entry` acepta ahora `engine` (el nombre plano de
`ExecutionMetrics.engine.implementation` — `"ollama"`, `"llama-server"`,
...; nunca el diccionario completo, para que agrupe limpio),
`serve_session_id` (el lanzamiento exacto de `src/launch_receipts.py` que
sirvió el turno, más fino que el `endpoint_label` — un reinicio del mismo
endpoint sube de generación pero es el mismo `serve_session_id` solo si
`launch_receipts` lo reconoce) y `phases` (el `phases` de este mismo
`ExecutionMetrics`, sin remedir nada). Los tres son opcionales y aditivos:
una fila grabada antes de este lote sigue cargando igual y agrupa bajo
`"?"` en `aggregate_by(..., key_fields=(..., "engine"))`.

## Cómo se lee la cronología (para quien construya la vista de Studio)

Cada fase se dibuja o se lista según su `source`:

- `observed_client` / `reported_engine` / `computed`: una barra, con la
  fuente como insignia — son números en los que confiar con distinto grado
  de certeza, no un aviso.
- `inferred`: una barra, pero la nota asociada debe mostrarse junto a ella,
  no en un lugar aparte donde se pierda el contexto de qué se mezcló.
- `absent`: **nunca una barra** (ni de longitud cero) — una línea de texto
  del tipo "el motor no expone esta métrica". Una barra en cero se
  confunde con "esto tardó 0 ms", que es una afirmación distinta y falsa.

`notes` (incluida la de solape) se muestra siempre que no esté vacía,
como texto plano asociado al turno completo, no a una fase concreta.
