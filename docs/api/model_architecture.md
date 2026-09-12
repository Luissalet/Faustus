# Arquitectura desde metadatos, no desde el nombre — `GET /api/models/architecture`

INF-01 §A (H01/H03). Antes de este contrato, `studio/src/lib/cookbook/serve.ts`
decidía si un modelo era MoE (Mixture-of-Experts) o si soportaba MTP
(multi-token prediction) comprobando si el nombre contenía `qwen3.5` o
etiquetas como `a10b`/`a22b`/`a3b`. Eso alcanza también a variantes densas
de la misma familia (Qwen3.5-27B es densa) y nunca cubre por completo una
familia real (Qwen3.5-397B-A17B documenta MTP y no aparece en ninguna lista
de substrings). Este endpoint sustituye esa heurística por una lectura de
metadatos, con **desconocido como resultado legítimo**, nunca como un `false`
o una lista vacía disfrazados de negativo.

## Contrato

```
GET /api/models/architecture?repo=<hf repo o nombre>&source=<auto|hf|ollama|llamacpp>
```

Autenticación: cualquier usuario con sesión (`require_user`, igual que
`GET /api/models`) — es una lectura pasiva, no una acción.

Respuesta (HTTP 200 siempre; un fallo de red es un resultado, no un 5xx):

```json
{
  "repo": "org/Modelo-27B",
  "kind": "dense" | "moe" | "unknown",
  "total_params": 27000000000,
  "active_params": null,
  "num_experts": null,
  "mtp": null,
  "architectures": ["ModeloForCausalLM"],
  "source": "hf_config" | "ollama_show" | "llamacpp_props" | "none",
  "observed_at": "2026-09-12T10:00:00Z",
  "note": null
}
```

- **`kind`**: `"moe"` solo si hay un contador de expertos (`num_experts`,
  `num_local_experts`, `n_routed_experts`) mayor que 1, o `moe_intermediate_size`
  presente. `"dense"` si el config es reconocible (`architectures`/`model_type`)
  y no tiene esos campos. `"unknown"` si el config falta, está vacío, o es
  contradictorio (p. ej. `num_experts: 1` junto con `moe_intermediate_size`).
- **`mtp`**: `true` si hay `num_nextn_predict_layers`/`mtp_num_hidden_layers`
  positivo o un flag `use_mtp`. **`null`** — nunca `false` — cuando el campo
  simplemente no está: la ausencia es desconocimiento, no una respuesta
  negativa medida.
- **`total_params`/`active_params`**: casi nunca vienen declarados en
  `config.json` (se podrían estimar desde `hidden_size`/capas, pero eso sería
  una conjetura vestida de medición) — quedan en `null` salvo que la fuente
  los reporte directamente (Ollama sí reporta `parameter_size`, ver abajo).
- **`source`**: de dónde salió la respuesta, nunca implícito. `"none"` cuando
  no se pudo leer nada (bloqueo de privacidad, red caída, HTTP≠200, tamaño
  excedido, JSON inválido) — en ese caso `observed_at` es `null` y `note`
  explica el motivo.
- **`note`**: texto libre, solo para diagnóstico — nunca se usa para decidir
  nada en el cliente.

## Fuentes

### `hf_config` — lectura pasiva de Hugging Face

`GET https://huggingface.co/<repo>/raw/main/config.json`, sin autenticar,
timeout 8 s, tope 256 KB (por `content-length` cuando está, y de nuevo
mientras se consume el cuerpo). **Nunca** seguimos `auto_map` ni ejecutamos
código remoto del repositorio, y nunca se pide un segundo fichero. Antes de
la llamada se consulta `src.privacy_policy.assert_outbound` — bajo un perfil
`local_only` el resultado es `kind: "unknown", source: "none"` con nota, sin
tocar la red (auditable con un contador de llamadas al transporte, ver
`tests/test_model_architecture.py`).

### `ollama_show` — `POST /api/show` en el Ollama local

Cuando `repo` tiene forma de tag de Ollama (sin `/`, opcionalmente
`nombre:variante` — la misma forma que `looksOllamaTag` comprueba en el
cliente), se hace un único `POST /api/show`. De la respuesta:
`model_info["general.architecture"]`, `*.expert_count` (cualquier prefijo de
arquitectura, p. ej. `qwen3moe.expert_count`), y `details.parameter_size`
(cadena tipo `"397B"`, convertida a entero aproximado — ya es una
aproximación tal y como la reporta Ollama, esta lectura no le añade
precisión). `/api/show` no informa de MTP: ese campo queda siempre en `null`
para esta fuente.

### `llamacpp` — hoy, siempre desconocido

Las propiedades de arquitectura de un servidor llama.cpp en marcha se leen
de `GET /props` de **ese proceso**, no de un fichero estático — y este
endpoint responde antes de lanzar nada. En vez de fingir una lectura
inventando un parser de metadatos GGUF que este módulo no tiene, `source:
"llamacpp"` devuelve siempre `unknown` con una nota que explica por qué
(«no fingir soporte» — ver `ESPEC_INFERENCIA_LOCAL.md` §17). Cuando exista un
lector real de propiedades de un servidor en marcha, esta fuente se conecta
ahí; hasta entonces, desconocido es la respuesta honesta.

### `auto`

Si `repo` tiene forma de tag de Ollama, se intenta `ollama_show` primero; si
no contesta nada (`source: "none"`), se prueba `hf_config` como respaldo
(unos pocos repos de HF son de un solo segmento, p. ej. `gpt2`). Si `repo`
tiene un `/`, se va directo a `hf_config`.

## Caché

En memoria por `(repo, source)`, TTL 1 h, más un espejo en
`data/model_architecture_cache.json` (mismo TTL, comprobado por
`observed_at`) para no perder la caché entre reinicios. Un resultado sin
resolver (`source: "none"`) **no se cachea** — ni en memoria ni en disco —
para que la siguiente llamada reintente en vez de memorizar un fallo de red
transitorio.

## Cómo lo usa el cliente

`studio/src/lib/cookbook/serve.ts::detectModelOptimizations(modelName, arch?)`
y `buildServeCmd(...)` solo activan flags/env vars de MoE cuando
`arch?.kind === 'moe'`, y MTP solo cuando `arch?.mtp === true` — nunca por
coincidencia de substring en el nombre. `ServeForm.tsx` pide este endpoint
al elegir modelo (debounce 400 ms, abortable, nunca bloquea el formulario),
muestra un chip «Architecture: dense · 27B · source hf_config» / «unknown
(metadata unavailable)», y deshabilita (con motivo, sin ocultar) los
interruptores `expert_parallel`/`moe_env`/`speculative`/
`llama_speculative_mtp` mientras la arquitectura no esté verificada. Editar
el comando a mano (`cmdOverride`) sigue disponible y se marca como «manual,
unverified» en el resumen.

Ver también `docs/api/model_capabilities.md` (una pregunta distinta: qué
sabe hacer un modelo, no cómo está construido) y
`src/model_capability_readers/ollama.py` (mismo `/api/show`, otra pregunta:
capacidades de chat, no campos de arquitectura — por eso este módulo lee
`/api/show` por su cuenta en vez de forzar ese lector a una segunda forma).
