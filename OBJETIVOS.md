# Objetivos acordados

Acordado con Luis; sólo lo que está por hacer. Al cerrar una entrada, se
borra de aquí y su evidencia va a PENDIENTES.md.

## OBJ-1 · Puerta de admisión de VRAM: preguntar antes de cargar

Acordado el 08-09-2026 de madrugada, después de que la máquina se quedara sin
memoria de commit por tener dos 27B dentro a la vez (ver PENDIENTES.md).

**Qué falla hoy.** Nadie pregunta. Ollama no rechaza nunca: si el modelo no
cabe, lo carga con capas en la CPU y el único síntoma es ir diez veces más
lento sin decir por qué. Cuando además hay otro modelo residente, el segundo
ni siquiera arranca (`cudaMalloc failed: out of memory`) y lo que llega arriba
es una ronda de research que se muere sola.

**Qué tiene que pasar.** Antes de cargar un modelo local, Faustus compara su
huella con el presupuesto libre. Si no cabe: **no lanza nada**, enseña los
residentes con sus GB, deja marcar cuáles descargar, los descarga, espera a
que `/api/ps` confirme que salieron, y sólo entonces carga el nuevo.

### Lo que ya está puesto y hay que reusar, no reescribir

- `src/vram_fit.py` — `plan()` y `pool_budgets()` ya dan presupuesto por GPU y
  `max_ctx_that_fits`; `kv_bytes_per_token_measured()` da el coste real por
  token. **La huella es pesos + caché KV de la ventana con la que se carga**,
  no el tamaño del fichero: un 27B a 131.072 tokens son 17,7 GB de pesos y
  9,1 GB de caché. Juzgar por el fichero es el error que ya se corrigió el
  07-09 en el selector.
- `routes/model_routes.py::_collect_fit_hints` — ya lee `/api/ps`, ya sabe qué
  hay dentro y cuánta VRAM sujeta cada uno, y ya mantiene `_KV_RATES` por
  digest de blob. De ahí sale la lista de residentes del diálogo.
- `POST /api/local-models/unload` (`routes/local_models_routes.py`) — ya
  existe; pone `keep_alive` a 0. La acción del diálogo es esto, una vez por
  modelo marcado.
- El canal de progreso del turno (`phase` por SSE), que desde el 08-09 ya
  lleva `loading_model` para «Cargando el modelo en memoria…». La pregunta
  viaja por ahí, no por un segundo sistema de preguntas.

### Estado (09-09, 23:10)

Hecho y en el commit `02830bba` + el de esta noche: `src/vram_admission.py`
(`assess`, tickets con espera y caducidad, `admit`, modos `ask`/`auto`/`off`),
rutas `GET/POST /api/local-models/admission…`, `POST /load` → 409 con el
veredicto, la puerta en `ResearchHandler._probe_endpoint` y en el botón
Cargar de Modelos locales, el diálogo `VramAdmissionDialog` (casillas,
sugerencia pre-marcada, «short by N GB», tres botones). Comprobado en vivo:
con `q4_K_M` dentro, pedir `q8_0` enseña «short by 12,1 GB»; al descargar el
`q4` la carga sólo empieza cuando `/api/ps` confirma que salió.

**Hecho también en el turno de chat** (10-09, commit 59436f6d):
`routes/chat_routes._vram_admission_events` corre `admit()` antes de la
primera llamada del turno y emite eventos `vram_admission`; Studio pinta el
mismo diálogo sobre el turno y la línea en vivo dice «No room in VRAM —
waiting for you to choose what to unload». Comprobado en vivo: q8_0
residente, chat con q4_K_M → diálogo → «Unload and continue» → el q8 sale,
el q4 entra, llega la respuesta. Ajustes › Sistema expone el modo
(`ask`/`auto`/`off`) y el tiempo de espera. **OBJ-1 cerrado.** Queda como
cinturón externo `OLLAMA_MAX_LOADED_MODELS=1` para lo que no pasa por
Faustus, y el aviso rojo en Modelos locales cuando hay dos modelos grandes
residentes a la vez.

### Lo que había que escribir (referencia del diseño)

- `src/vram_admission.py`
  - `plan_admission(endpoint, model, ctx) -> {fits, needed, budget, shortfall,
    residents: [{name, size_vram, ctx, digest}]}`.
  - Un registro de bloqueos con espera: publica la pregunta, se queda en un
    `asyncio.Event` y devuelve lo que el usuario decidió. Con caducidad: un
    bloqueo que nadie contesta no puede colgar un turno para siempre.
- Rutas: consultar el bloqueo pendiente y resolverlo con los nombres marcados.
- La puerta, en los dos sitios donde arranca una carga larga:
  `ResearchHandler._probe_endpoint` (ya es el sitio donde se espera al modelo)
  y el turno de chat.
- Modal en la interfaz al recibir `phase: "vram_blocked"`: lista con casillas
  y GB por modelo, y tres botones — **Descargar y continuar**, **Continuar
  igualmente** (aceptar el spill a sabiendas) y **Cancelar**.
- Ajuste `vram_admission`: `ask` (lo anterior), `auto` (descarga el menos
  usado sin preguntar, para workers y trabajo desatendido) y `off`.

### Dos agujeros que hay que declarar, no tapar

- **Un modelo que nunca se ha cargado se juzga sólo por sus pesos.** Hasta que
  Ollama no lo mete dentro no publica con qué ventana lo va a cargar
  (`OLLAMA_CONTEXT_LENGTH`, o lo que pida quien carga), así que la caché KV es
  desconocida. El diálogo tiene que decir que esa cifra es el suelo, no la
  huella. Inventar 32k o 128k sería repetir el error del 07-09.
  (PENDIENTES_UI 173-175.)
- **La puerta sólo cubre lo que carga Faustus.** Un `ollama run` en una
  terminal, o una llamada cruda a `/api/chat`, se la salta entera — que es
  exactamente como se llenó la VRAM la noche del 08-09. El cinturón para eso
  es `OLLAMA_MAX_LOADED_MODELS=1` en el entorno de Ollama: él mismo echa al
  anterior en vez de apilar. Puerta para lo interactivo, variable para lo que
  no pasa por la app.

## OBJ-2 · El agente pregunta con opciones cuando le hace falta decidir

Luis, 10-09-2026: «comprobar que el modelo, cuando le pides implementar cosas
y lo considera necesario, te pregunta entre opciones (como haces tú con
AskUserQuestion): varias opciones para elegir y una para que escribas tú, o
una checklist. Como lo haces tú, vaya.»

### Estado (10-09, 02:45) — CERRADO

La herramienta `ask_user` ya existía (`src/agent_tools/interaction_tools.py`)
y el modelo la usaba; lo que fallaba era la pantalla: el camino en vivo de
`studio/src/adapters/chat.ts` hacía `String()` de las opciones (objetos
`{label, description}`) y cada botón decía «[object Object]». Ahora
(59436f6d): un botón por opción con su consecuencia debajo, checklist con
«Enviar» cuando la herramienta dice `multi`, y siempre una línea para
escribir tu propia respuesta; la descripción de la herramienta dice cuándo
preguntar (un «impleméntame X» con varios diseños razonables), la
recomendada primero, y que no invente una opción «Otra». Comprobado en vivo
con qwen3.8 q4: tres bases de datos con descripción, el clic vuelve como
siguiente mensaje.

**Iniciativa propia, observada y corregida (10-09, 02:45; 573607c0).** Con
la descripción de la herramienta como única guía, «Impleméntame en este
proyecto un sistema para guardar las preferencias de cada usuario» fue
directo a `preferences.py` (JSON + CLI) sin preguntar. La regla vive ahora
en las Base rules efectivas y en el bloque «Workspace coding mode»: un
sistema nuevo con varios diseños razonables se decide con `ask_user`
**antes** del primer fichero; una edición pequeña se hace sin preguntar.
Misma petición después: «¿Cómo quieres que sea el sistema de preferencias
de usuario?» — «Python + JSON (recomendado)», «Node.js + JSON», «Python +
SQLite» — sin escribir nada; «Python» produjo `user_preferences.py`; y una
edición de una línea fue `read_file` → `write_file` sin pregunta.

### Qué tiene que pasar

- En modo agente, ante una decisión que no puede resolver desde la petición,
  el código o un valor por defecto razonable, el modelo emite una **pregunta
  estructurada**, no un párrafo: título corto, la pregunta, 2-4 opciones con
  una línea de consecuencia cada una, y siempre una opción libre («Otra…»).
  Variante *multiselección* (checklist) cuando las opciones no se excluyen.
- La pregunta bloquea el turno hasta que se contesta (como la aprobación de
  herramientas), se pinta en el transcript como tarjeta con botones, y la
  respuesta vuelve al modelo como resultado de la herramienta.
- Si nadie contesta (trabajo desatendido, worker), caduca con la primera
  opción marcada como recomendada y lo dice en el transcript.

### Lo que ya existe y hay que reusar

- `approval_store` y el flujo de aprobación de herramientas: ya bloquea un
  turno esperando a la pantalla. Es binario; la pregunta necesita opciones
  y multiselección, pero el canal SSE y la espera son los mismos.
- Los tickets de `src/vram_admission.py` (`open_ticket/resolve`, caducidad):
  el mismo patrón «pregunta con casillas que espera» ya implementado una vez.
- `VramAdmissionDialog.tsx`: diálogo con casillas y botones que se puede
  generalizar a «opciones + libre».

### Lo que hay que escribir

- Herramienta integrada `ask_user` (nombre provisional) con esquema
  `{question, header, options:[{label, description}], multi:false, recommended}`,
  descrita al modelo con cuándo usarla y cuándo NO (no para lo que tiene
  valor por defecto obvio; una sola pregunta por turno).
- Ruta `POST /api/questions/{id}` con la respuesta; fase SSE `question`.
- Tarjeta en Transcript con radio/checkbox, campo libre y «Responder».
- Comprobar en vivo con qwen3.8 27B que la USA cuando toca: pedirle algo
  ambiguo («impleméntame auth») y ver si pregunta o se lanza.

## OBJ-3 · La spec v2: hacer de Faustus el harness definitivo para modelos abiertos

Luis, 10-09-2026: entregó `Faustus_Paquete_Completo.zip` (187 requisitos, 192
contratos de herramienta, 48 escenarios QA, 8 JSON Schemas) con tres reglas:
**no se pierde ninguna capacidad, se extiende, reorganiza y mejora**; todo
se verifica por MCP y por pantalla, no sólo con tests; commits graduales y
rama aparte sólo si algo puede dejar la app rota.

### Estado (10-09, 09:30) — M0 cerrado, M1 abierto

- **M0.** El paquete entero está en `docs/spec/v2/` (validador incluido:
  `python docs/spec/v2/validate_examples.py`, necesita `jsonschema[format]`).
  El mapa BASE-01 (`docs/spec/v2/MAPA_REUTILIZACION.md`) clasifica los 100 P0
  contra el código real: 18 existentes, 73 parciales, 8 ausentes, 1 no
  verificable leyendo. Es el punto de partida de cualquier lote: se leen las
  filas del área antes de abrir un módulo nuevo.
- **M1, primer incremento vertical (rama `feat/spec-v2-m1`, 13 commits,
  verificado en vivo en el 7001):**
  - ARCH-01/CALL-05/OBS-03: `src/contracts/{task,tool,errors}.py` con la
    máquina de estados de §04, `ToolResult` con los siete estados de §34.5 y
    la taxonomía cerrada de errores; `GET /api/contracts/schemas[/{name}]`
    sirve los schemas del paquete. Los 8 ejemplos válidos parsean y los 12
    negativos se rechazan señalando el campo.
  - CALL-01/02/03: `src/tool_call_assembler.py` (UTF-8 partido en mitad de
    «ñ», JSON partido, ids intercalados, claves duplicadas, límites) sustituye
    al dict por índice de `llm_core` con las mismas tramas SSE;
    `validate_tool_arguments`/`repair_tool_arguments` y su enganche en
    `agent_loop` (`FAUSTUS_TOOL_ARG_VALIDATION=strict|warn|off`): tipo, enum y
    ruta fuera de alcance paran la llamada con error localizado; `"true"`,
    `"5"` y `"Week"` se reparan; campo desconocido o requisito ausente sólo
    avisan; la puerta de política va ANTES para no enseñar el schema de lo
    que no se puede ejecutar.
  - TASK-01: `update_plan` ya no corta a 8.192; `src/plan_state.py` da pasos
    con id, estado, dependencias y `verified` (sólo un paso hecho puede
    estarlo). `plan_update` se persiste en `tool_events` (antes no) y Studio
    pinta la tarjeta de pasos igual en vivo y desde historial (QA-38).
  - CALL-07/TASK-04: `ask_user` emite `question_id` e ids de opción;
    `src/question_store.py` rechaza respuesta tardía, obsoleta o duplicada y
    nunca lee el silencio como sí (QA-13/14). La respuesta desde la tarjeta
    viaja con `question_id`+`option_ids`; una obsoleta devuelve 409 sin
    persistir nada.
  - UX-02/TASK-03: `client_message_id` + `src/chat_outbox.py`: dos POST con
    el mismo id → un solo mensaje de usuario y una sola ejecución, el segundo
    reengancha con `X-Faustus-Idempotent-Replay: 1` (QA-08). Studio guarda el
    id en un outbox y reintenta con el MISMO id tras una recarga sin acuse.
  - BASE-01 (UI): `/api/version` dice build (sha+fecha) y el bundle de Studio
    realmente servido (el `?v=` de `studio.js`); tarjeta en Diagnóstico.

### Qué queda de M1 y qué abre M2 (orden propuesto)

1. CALL-06: reintentos por clase con `outcome_unknown` y conciliación, no
   optimismo (`src/llm_core.py` 2673-2823 hoy reintenta sin jitter).
2. TASK-02: reanudar una research por unidades confirmadas (hoy sólo
   «interrupted» + Reintentar desde cero).
3. ARCH-03: compilador de configuración efectiva con origen y hash;
   `_AGENT_RULES` sigue definido dos veces en `agent_loop.py`.
4. OBS-01: `trace_id`/`step_id` de extremo a extremo (hoy sólo `run_id`).
5. TOOL-01: registro único versionado de herramientas (hoy repartido en
   `tool_capabilities`, `tool_schemas` y `TOOL_TAGS`).
6. EVAL-05: convertir QA-01…48 en regresiones permanentes conforme se cierren.

