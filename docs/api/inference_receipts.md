# Contratos, capacidades y recibo de arranque — INF-02 §05-07

INF-02 (Lote A backend). Antes de este contrato, un arranque de servidor local
solo dejaba `requested_cmd`/`final_cmd`/`rewrites` (INF-01 §D): qué se pidió y
qué comando terminó ejecutándose, pero nada sobre si las opciones pedidas eran
compatibles con esa implementación, ni sobre si el proceso realmente las
aplicó una vez arriba. Este contrato añade esas dos preguntas sin fusionarlas
en un booleano.

## Los tres ejes que nunca se colapsan

Para cada opción (`ctx`, `flash_attn`, `cache_type_k`, ...) hay tres hechos
distintos, y una respuesta que solo tuviera uno de ellos estaría mintiendo
por omisión sobre los otros dos:

- **`support`** (`supported` | `unsupported` | `unknown`): ¿la implementación
  acepta esta opción, según el manifiesto versionado? Es la única pregunta
  que se puede responder **antes** de arrancar nada.
- **`effective`** (`{value, state}`, `state` ∈ `confirmed` | `mismatch` |
  `unconfirmed` | `not_applicable`): ¿el proceso en ejecución tiene esta
  opción puesta, según lo que una sonda pasiva observó? Solo
  `POST /api/model/serve/{id}/verify` puede mover este campo — nunca la
  evaluación previa al arranque.
- **`benefit`** (`{state, benchmark_id}`, `state` ∈ `not_evaluated` |
  `observed_gain` | `no_gain` | `regression`): ¿un benchmark demostró que
  esta opción mejora algo? Ningún endpoint de INF-02 lo mueve — queda fijo en
  `not_evaluated` hasta que exista un banco de pruebas (fuera de alcance
  aquí).

`unknown`/`unconfirmed`/`not_evaluated` son resultados legítimos, nunca un
sinónimo de `false`, `0` o una lista vacía. La ausencia de telemetría (un
`/props` que no expone `flash_attn`) no es una comprobación negativa (el
motor NO tiene flash attention activado) — son hechos distintos y el
contrato los distingue por diseño (`src/contracts/inference.py`,
`CapabilityAssessment`).

## El manifiesto versionado (`config/inference_capabilities.json`)

Una lista, por implementación (`llama-server`, `llama_cpp.server`, `vllm`,
`sglang`, `ollama`, `mlx`), de qué opciones acepta y bajo qué requisitos:

```json
{
  "manifest_version": "1.0.0",
  "reviewed_at": "2026-09-12T00:00:00Z",
  "llama-server": {
    "doc": "https://github.com/ggml-org/llama.cpp/...",
    "options": {
      "cache_type_k": {
        "scope": "server_start",
        "since": null,
        "flag": "--cache-type-k",
        "requires": ["option:flash_attn==on"],
        "note": "KV-cache quantization for K requires flash attention..."
      }
    }
  }
}
```

- **`since: null`** significa "versión mínima no verificada" — nunca se
  inventa un número de versión; `note` explica por qué no se sabe.
- **`requires`** es una lista de condiciones comprobables:
  `arch.kind==moe`, `arch.mtp==true`, `gpus>=2`, `option:flash_attn==on`.
  `src/inference_capabilities.py` las evalúa contra `arch`/`gpus`/`options`
  tal como llegaron en la petición — nunca contra lo que el motor
  "probablemente" tiene.
- El manifiesto documenta explícitamente las diferencias de H04 entre
  `llama-server` (el binario nativo, con `--flash-attn`, `--tensor-split`,
  `--parallel`, ...) y `python -m llama_cpp.server` (el wrapper, que **no**
  tiene equivalente para varias de esas flags — ver
  `src.inference_capabilities.LLAMA_CPP_PY_OMITTED`, copiado a mano de
  `studio/src/lib/cookbook/serve.ts`'s `LLAMA_CPP_PYTHON_NO_EQUIVALENT` para
  que Python y TypeScript no diverjan en silencio) pero **sí** admite
  `cache_type_k`/`cache_type_v` sin la exigencia de flash attention, porque
  no tiene ninguna flag de flash attention contra la que exigirlo.
- Ollama: `num_ctx`/`keep_alive` son opciones **por petición**; la
  cuantización de la caché KV (`kv_cache_type`, controlada por
  `OLLAMA_KV_CACHE_TYPE`) y `flash_attn` (`OLLAMA_FLASH_ATTENTION`) son
  **globales al servidor** — el manifiesto lo marca con `scope: "global"` y
  ninguna UI debería ofrecerlas como si fueran por conversación.

## Endpoints

Todos exigen `require_admin` (igual que el resto de Cookbook). Ninguno carga
un modelo, instala nada, ni reinicia un proceso — verificar es leer.

### `POST /api/model/serve/assess`

Puro: evalúa un plan contra el manifiesto sin ningún efecto secundario. La UI
lo llama en cada cambio de opción (con *debounce*), y `POST /api/model/serve`
vuelve a correr exactamente la misma lógica del lado de Python antes de
lanzar nada — nunca hay dos conjuntos de reglas divergentes entre TypeScript
y Python.

```json
// request
{"implementation": "vllm", "options": {"expert_parallel": true}, "arch": {"kind": "dense"}}
// response (200)
{
  "assessments": [{"option": "expert_parallel", "requested": true, "support": "unsupported",
                     "scope": "server_start", "requirements": ["arch.kind==moe"],
                     "effective": {"value": null, "state": "unconfirmed"},
                     "benefit": {"state": "not_evaluated", "benchmark_id": null},
                     "evidence": {"kind": "versioned_manifest", "observed_at": null},
                     "reasons": ["requirement not met: arch.kind==moe"]}],
  "blockers": [/* el mismo objeto, es la sublista unsupported */]
}
```

### `POST /api/model/serve` (campos nuevos)

`ServeRequest` acepta ahora `plan: dict|None` (`{implementation, model,
options, arch}`, las mismas opciones estructuradas del formulario, nombres
canónicos del manifiesto) y `force_manual: bool = False`.

- Con `plan` y algún `hard_blockers` (una opción `unsupported` que el
  cliente pidió explícitamente) y sin `force_manual`: **`409`**
  `{"error", "error_class": "serve.incompatible", "assessments": [...]}` — no
  se lanza nada.
- Con `force_manual: true`, o sin bloqueos: se lanza como antes de INF-02, y
  la respuesta incluye `"receipt"`, el `LaunchReceipt` recién archivado
  (`verify_state: "pending"`).
- Sin `plan` (cliente anterior a INF-02, o un comando manual pegado a mano):
  el recibo se archiva igual, con `assessments: []` y `plan: {"manual":
  true}` — es un caso legítimo, menos verificado, no un error.

### `GET /api/model/serve/{session_id}/receipt`

El recibo tal cual está archivado, o `404`
`{"error", "error_class": "serve.receipt_not_found"}`.

### `POST /api/model/serve/{session_id}/verify`

`{"base_url"?: str, "authorized_probe"?: bool}` → el recibo, con
`assessments[].effective` actualizado y `checks`/`differences` rellenados a
partir de sondas **pasivas**, con 5 s de timeout cada una:

- **`llama-server`**: `GET /props` (+ `GET /slots` si responde) — observa
  `n_ctx`, `total_slots`, `flash_attn`/`cache_type_k`/`cache_type_v` cuando
  `default_generation_settings` los expone (si no, `unconfirmed`),
  `model_path`, `build_info`. `GET /v1/models` confirma que el modelo pedido
  aparece listado.
- **`llama_cpp.server`**: solo `GET /v1/models` — el wrapper no expone
  `/props`, así que toda opción queda `unconfirmed`.
- **`ollama`**: `GET /api/version`, `GET /api/ps` (`context_length` si
  aparece → `num_ctx` confirmado/discrepante; si no aparece, `unconfirmed`),
  `POST /api/show` (sin `verbose`) para el modelo del plan.
- **`vllm`/`sglang`**: `GET /v1/models` (+ `GET /version` en vllm) — ninguno
  expone configuración de runtime, así que toda opción asociada queda
  `unconfirmed`; solo `model_listed` se puede confirmar.

`checks` siempre incluye `http_reachable` y `model_listed`
(`passed`/`failed`/`skipped`). `chat_probe` es `skipped` con detalle "not
authorized" salvo que `authorized_probe: true` — en cuyo caso se manda un
único `POST` de un token (`max_tokens: 1` / `num_predict: 1`) al endpoint de
chat. Una sonda caída produce `verify_state: "failed"` con
`checks.http_reachable.failed` y **ningún** valor inventado — los
`assessments` existentes quedan tal cual estaban.

Sin `base_url` explícito, el endpoint lo deriva del puerto en
`final_cmd`/`requested_cmd` (`--port`/`-p`, o `OLLAMA_HOST=host:port` para
Ollama); si no puede, **`400`** `{"error_class": "serve.base_url_required"}`
— nunca asume un puerto por defecto.

## Lo que un recibo NO demuestra

- Que una opción `supported` mejore nada de la tarea — eso es `benefit`, y
  aquí siempre queda `not_evaluated`.
- Que un `HTTP 200` en el `chat_probe` acredite visión, JSON estructurado o
  llamadas a herramientas — solo prueba que el endpoint contestó a un
  mensaje de texto de un token.
- Que `effective.state: "unconfirmed"` signifique que la opción está
  desactivada — solo dice que ninguna sonda disponible la expone.
- Que un recibo viejo (`generation` anterior, en `history`) siga
  certificando el proceso actual — un reinicio con un `final_cmd` distinto
  debe invalidarlo (`src.launch_receipts.mark_stale`).
