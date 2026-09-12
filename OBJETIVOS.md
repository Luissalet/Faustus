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

## OBJ-3 · La spec v2: hacer de Faustus el harness definitivo para modelos abiertos — CERRADO (11-09-2026)

### Estado final (11-09-2026, cierre lotes 60-70, master cfaa30d) — CERRADO

Los 187 requisitos del paquete de Luis (100 P0 + 84 P1/P2/LAB) están
auditados fila a fila contra el código real, con evidencia verificada por
grep en este mismo commit:

- **P0** (`docs/spec/v2/MAPA_REUTILIZACION.md`): 99 IDs propios del
  fichero — **97 existente, 2 parcial** (PLAN-01, PLAN-03 — plan corto
  expandible en Studio y visibilidad de propiedad de recursos entre
  subagentes; ninguno bloquea el resto del paquete).
- **P1/P2/LAB** (`docs/spec/v2/MAPA_P1.md`): 84 IDs — **83 existente, 1
  parcial** (HW-06 — nodos remotos, decisión de producto explícita, no
  hueco de código: ver su fila).
- **QA** (`docs/spec/v2/QA_ESTADO.md`): 46 verde, 1 xfail (QA-44, solo el
  hueco de temporización intermitente en Escape), 1 manual (QA-41, voz
  física — exige hardware e intervención humana).
- Nuevo en el cierre 60-70, operativo para quien use Faustus a diario:
  trazabilidad de extremo a extremo por `call_id`
  (`GET /api/observability/trace/{call_id}`), caos en dry-run desde Studio,
  coste de workers remotos, perfil de privacidad elegible con OCR/TTS/STT
  auditados, concesiones activas revocables, carpetas recientes de
  proyecto, ventana de trabajo con búsqueda de fragmento exacto, resumen de
  compactación inspeccionable, export verificado antes de servir el enlace,
  y una cabecera de versión que rechaza a un cliente demasiado viejo en
  cualquier `/api/*`. Ver `FAUSTUS.md` §68 para el detalle operativo
  completo.

M0 y M1 (contratos tipados, `ToolResult`, ensamblador de tool-calls robusto,
plan estructurado, `ask_user` con `question_id`, idempotencia de chat,
`/api/version` con build) quedaron cerrados como quedan descritos arriba;
las ocho olas de P1 (Lotes 45-55) y los lotes 60-70 completan el resto. Los
dos huecos de código que quedan (PLAN-01/PLAN-03) y la única decisión de
producto pendiente (HW-06) están documentados con su propia fila y no
requieren un lote de cierre adicional para OBJ-3 — se seguirán como
entradas sueltas en `PENDIENTES.md` si Luis decide abordarlas.

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

### Estado (10-09, tras el Lote 55) — las ocho olas de P1 cerradas

M1 sigue abierto tal cual queda arriba (nada de esta lista se tocó en el
Lote 55). Lo que sí cerró es la otra pata de OBJ-3: los 87 requisitos P1/P2/
LAB que el paquete de Luis proponía además de los 100 P0, repartidos en ocho
olas de lotes (`docs/spec/v2/MAPA_P1.md`, una tabla por lote) que fueron
auditando y, donde hacía falta, construyendo cada área — contexto/memoria,
planificación/herramientas, artefactos/edición, UX/ajustes/actividad,
workbench/accesibilidad, media/voz, hardware/observabilidad/operación,
conectores/automatizaciones/escritura/evaluación — hasta que el Lote 55
integró lo que quedaba suelto (dos routers escritos y nunca montados en
`app.py`, una cola nueva para ACT-05) y auditó los ocho IDs que ningún lote
había tocado desde el Lote 50. El recuento real, cruzado 1:1 contra
`docs/spec/v2/backlog.json` (no contra lo que cada lote decía haber hecho):
**84 de 84** P1/P2/LAB tienen fila (74 existente, 9 parcial, 1 ausente —
`backlog.json` declaraba 87 pero tres no correspondían a ningún ID real, un
desajuste que venía arrastrándose sin recontar desde el Lote 50). De los P0
originales, el último recuento completo (Lote 29, ola 4) fue 100 de 100
clasificados (70 parcial, 23 existente, 6 ausente, 1 no verificable); las
olas 5 y 6 (lotes 30-44) cerraron huecos fila a fila sin volver a sumar el
total, así que esa cifra global es la última fiable — `MAPA_REUTILIZACION.md`
sigue siendo la fuente fila a fila más reciente. De los 48 escenarios QA:
44 verde (regresión permanente en `tests/qa/`), 3 `xfail` (mecanismo
documentado como no construido), 1 manual (voz física, exige micrófono
real). Lo que sigue genuinamente abierto tras las ocho olas: **TASK-05**
(steering que invalide pasos de un plan en curso, sin ningún caller);
el proceso Playwright por sesión que WEB-03 dejó anotado como
arquitectónico; los tres huecos intermitentes de QA-44 (dialog/diff a
200 %, Escape) sin investigar a fondo; y los nueve IDs "parcial" que
`MAPA_P1.md` detalla uno a uno (primitiva real sin el último cableado a un
caller de producción, o con menos alcance que el título del ID). Ver
PENDIENTES.md › «Spec v2 · P1» para la lista completa con evidencia.

(Nota histórica: el párrafo anterior es el estado tal como lo dejó el Lote 55;
el cierre de lotes 60-70 de 2026-09-11 resolvió TASK-05, el Playwright por
sesión y ocho de los nueve IDs "parcial" — ver "Estado final (11-09-2026...)"
más arriba y `docs/spec/v2/MAPA_P1.md` para el detalle fila a fila.)

## OBJ-4 · Panel de control de versiones en Studio — CERRADO (11-09-2026)

Pedido por Luis, 2026-09-10 — estilo VS Code.

### Estado final (11-09-2026) — CERRADO

Todo lo de abajo existe y está verificado en vivo en el 7001 de Luis
(commits 61cd184…ae76060 y b5f2707): descubrimiento de repos por carpeta
enlazada (subrepos incluidos, una sola llamada `status --porcelain=v2`,
caché 30 s, 0,5 s para 24 repos), rama/ramas/log/diff/cambios, identidad
git visible con selector (identidades de `~/.ssh/config`, manuales y
cuentas `gh`), crear repo (`init`/`clone`) y publicarlo en GitHub con un
botón, crear/mergear/borrar rama con diálogos, push que fija el upstream
solo, pull/fetch/sync, política por repo de lo que el agente puede hacer
(rama aparte / commit / push) que las tools `git_*` del agente respetan, y
el mismo panel dentro de cada proyecto (pestaña Repositorios) y del chat
(pestaña git del panel lateral, con borde arrastrable — sin sliders).
Los seis criterios de aceptación se cumplen; el 5 (pull con conflicto) se
prueba con un remoto bare en `tests/test_l89_git_merge.py`. Detalle en
`FAUSTUS.md` §69 y `docs/api/git.md`.

**Qué falta hoy.** Faustus no tiene ninguna vista de control de versiones.
Un usuario que trabaja sobre un repo git enlazado a un proyecto no tiene
forma de ver, desde Studio, en qué rama está, qué ha cambiado, quién hizo
el último commit ni si hay algo sin confirmar — tiene que salir a una
terminal o a otra herramienta.

**Qué tiene que pasar.** Un panel nuevo (candidato: `studio/src/screens/VersionControl.tsx`,
o una pestaña dentro de `Project.tsx`) que, para las carpetas enlazadas de
un proyecto:

- Detecta cada repo git presente (incluidos subrepos/submódulos anidados
  dentro de las carpetas enlazadas, no solo la raíz).
- Por repo: rama actual, lista de ramas locales, el último commit de cada
  una (hash corto, autor, fecha, mensaje de una línea).
- Cambios pendientes: ficheros modificados/añadidos/borrados/sin seguimiento,
  con recuento y, al abrir un fichero, el diff.
- Usuario git configurado para ese repo (`user.name`/`user.email`, local y
  global si difieren).
- Acciones básicas **seguras**: cambiar de rama (bloqueado si hay cambios
  sin confirmar que la ratón entrañaría perder — mismo criterio de
  "conflicto recuperable, no pérdida de trabajo" que EDIT-01), confirmar un
  commit con mensaje, hacer pull/push (con aviso previo si hay commits
  divergentes, nunca un force silencioso). Nada destructivo (reset --hard,
  descartar cambios, borrar rama) sin una confirmación explícita y reversible
  donde sea posible.

### Criterios de aceptación

1. Con una carpeta enlazada que contiene un repo git y dos subrepos, el
   panel lista los tres, cada uno con su rama y su HEAD real — verificado
   contra `git status`/`git log` ejecutados a mano en la misma carpeta.
2. Modificar un fichero fuera de Studio (editor externo) y refrescar el
   panel muestra ese fichero como cambio pendiente, sin falsos positivos
   para ficheros sin tocar.
3. Cambiar de rama con cambios sin confirmar pendientes se bloquea o pide
   confirmación explícita — nunca pierde trabajo en silencio.
4. El usuario git mostrado coincide exactamente con `git config user.name`/
   `user.email` para ese repo (local antes que global).
5. Una acción de pull con conflicto real no dice "hecho" — muestra el
   conflicto y no deja el repo a medio fusionar sin decirlo.
6. Una carpeta enlazada sin ningún repo git no muestra el panel como roto
   ni como "cargando" para siempre — un estado vacío claro (reusar
   `EmptyState.tsx`, ACT-06).

Sin fecha de lote asignada todavía; queda para que Luis lo priorice frente
al resto de PENDIENTES.md.

## OBJ-5 · Nodos remotos con emparejamiento por grupos (HW-06) — APLAZADO

Pedido por Luis, 2026-09-11: «Si quiero. No tengo un segundo PC ahora
mismo, pero sería útil. Lo más fácil para unirlos serían grupos de
seguridad como en Brave». La primitiva (`src/remote_worker_registry.py`)
existe y está probada; lo que falta es el emparejamiento tipo Brave Sync:
un grupo con código/QR, dispositivos que se unen al grupo, y el criterio
de «implementado» que hoy `DECLARATIONS["remote_worker"]` mantiene en
`False`. Se acomete cuando haya un segundo equipo con el que verificarlo
de verdad; sin hardware no se marca como hecho.

## OBJ-6 · Tablero tipo Jira por proyecto — CERRADO (11-09-2026)

Pedido por Luis, 2026-09-11: «un tablero tipo Jira … que por proyecto
pueda ser consultado y manipulado por los agentes o el usuario … bugs …
ideas, features … identificadores como P1-noseque». Hecho en b5f2707:
`src/project_board.py` (SQLite `DATA_DIR/board.sqlite3`; incidencias
`CLAVE-N` con clave por proyecto, tipos bug/feature/idea/task, estados,
prioridad, asignado, comentarios, eventos, enlaces entre incidencias y a
commits — «fixes/closes/cierra/arregla CLAVE-N» en un mensaje de commit
mueve la incidencia sola), `routes/board_routes.py`, tools del agente
`board_*` (listar, «lo que está listo», crear, actualizar, comentar,
enlazar, reclamar), bloque de tablero en el prompt del proyecto, pantalla
kanban con arrastrar-y-soltar en el proyecto y panel compacto en el chat,
y los `CLAVE-N` del transcript convertidos en enlaces. `docs/api/board.md`,
`tests/test_l91_*`–`test_l94_*`.

## OBJ-7 · Lenguaje natural para todas las tools — CERRADO (11-09-2026)

Pedido por Luis, 2026-09-11: «que no tenga que ser todo tan explícito …
un usuario no debería saberse de memoria todas las tools de Faustus»,
y «no me refiero solo para git, me refiero para todas las tools». Hecho
en b5f2707: ejemplos en español e inglés por tool
(`src/tool_index_examples.py`) que alimentan la recuperación de tools,
sinónimos por dominio en `src/action_intents.py` (media, tablero, git,
ficheros, web…), las tools que el usuario nombra o insinúa se ofrecen
siempre, y un banco de 121 frases naturales
(`tests/test_l91_natural_language_tools.py`, 100 % resueltas a la tool
correcta). Regla de oro: «dime X para el proyecto Y» tiene que bastar.

## OBJ-8 · Copiar lo que interesa de otros proyectos — EN CURSO

Pedido por Luis, 2026-09-11: investigar aigraphstudio y OpenRouterTeam
«e implementa todo lo que pueda ser interesante»; después amplió la lista
(agent-desktop, open-knowledge, herdr-studio) y dijo que la investigación
la hará él con ChatGPT y traerá los hallazgos. Tanda 1 hecha (0017b70 y
d534de0), ver `FAUSTUS.md` §71: contabilidad de coste real de OpenRouter,
preferencias de proveedor por endpoint ligadas a la política de privacidad,
web search `:online` solo explícito, fallback nativo `models[]`,
`cache_control` para Claude vía OpenRouter, router medido de modelos
locales (MOD-05, sin cablear aún al turno de chat), Mermaid de
workflows/futures, linter de perfiles de agente (Tarjan) y estimador de
coste de workflows. Descartado a propósito: el editor visual de canvas
de aigraphstudio (un segundo formato de proyecto desconectado de los
datos reales), `openrouter/auto` y BYOK (van contra «nunca pasar a pago
en silencio» y «una sola autoridad sobre privacidad»).

**Tanda 2 «ADP» (11-09-2026, commit `95747d9`).** Auditoría fila a fila
de las 32 fichas ADP-01..32 contra el código real (`docs/adaptations/
baseline.md`) y, donde el hueco era genuino, construcción directa: ocho
estados de atención con motivo y no-leídos (`src/attention.py`), escritorio
semántico completo — snapshot acotado, `ref` con sesión+generación,
disposición de entrega separada del resultado observado, backend UIA
opcional (`src/desktop_semantics/`) —, requisitos versionados por proyecto
con matriz de evidencia y caducidad honesta (`src/requirements/`),
wiki-links con backlinks y comentarios anclados por cita+contexto
(`src/document_links.py`, `src/document_comments.py`), política de
proveedor explícita con MOD-05 cableado a `/api/chat` para `model=='auto'`
(`src/provider_policy.py`), dry-run e importación de workflows externos
(`src/workflows/preflight.py`, `interchange.py`), revisión de skills con
hash/diff/aprobación pinneada (`src/skill_import_review.py`), snapshot
acotado del navegador con subárbol/búsqueda (`src/browser_view.py`),
`repo_map` consolidado sobre `code_index` (duplicidad cerrada) y pools de
admisión de recursos (`src/resource_admission.py`). Detalle completo y
estado fila a fila: `docs/adaptations/baseline.md` (reconciliado en el
lote W3-E de esta misma ola).

**Tanda 3 «CMP» (11-09-2026, commit `757262e`).** Informe comparativo V2
(§3.1-3.14), nueve lotes en paralelo sobre huecos que la tanda 2 dejó
documentados: sesión documental compartida entre panel y editor + selector
de ocurrencias sin ambigüedad + `ReviewPane` (CMP-01/02/03), tres
disposiciones del Studio — conversación/documento/revisión — sin duplicar
estado (CMP-01-layout), vecindario de conocimiento tipado con evidencia
caducada honesta y recibos de contexto por turno (CMP-04), cuatro ejes de
atención (ciclo/espera/conexión/siguiente acción) sobre `src/attention.py`
(CMP-05), adaptador Herdr de solo lectura con contrato propio declarado
pendiente de validar (CMP-06), simulador estructural de workflows +
pantalla `/workflows` con canvas SVG propio y tres modos (CMP-07),
estimador con cuentas separadas (activaciones/llamadas/operaciones
externas) y comparador de planes (CMP-08), estrategia observable del
turno con perfiles fast/balanced/deep_review (CMP-09), canal de acción
explícito para el escritorio semántico con fallback visible (CMP-10),
recetas de trabajo reutilizables con `from-run` redactado (CMP-12),
alternativas aisladas comparables con fusión a tres vías real (CMP-13), y
el showcase de tres recorridos con datos mínimos (CMP-14). Fichas
completas en `docs/adaptations/decisions/CMP-*.md`, cada una con su propia
sección "Puntos a cablear por el orquestador" — la ola siguiente (W3,
`CONTRATO_W3.md`) es precisamente el cableado de esos puntos (evento
`strategy` y `anchor` en Studio, CMP-11 de capacidades explicables,
`local_latency` real del estimador, aisladores de futures, exportar/
persistir workflows).

**Estado:** EN CURSO — la ola W3 (en marcha en paralelo a este mismo
documento) cablea los puntos que las tandas 2 y 3 dejaron documentados
como pendientes de un lote posterior; no cierra OBJ-8 todavía.


## OBJ-9 · Inferencia local honesta: veracidad, evidencia, visibilidad y banco — EN CURSO

Pedido por Luis, 2026-09-12, con `Faustus_Especificacion_Claude_Inferencia_
Local.md` como única fuente («aquí tienes tus siguientes pasos cuando acabes
con el readme»). El principio: **soporte**, **aplicación efectiva** y
**beneficio medido** son tres ejes distintos que nunca se colapsan en un
«optimizado»; lo no observado es `unknown`/`absent`, nunca cero; sondas
pasivas por defecto; benchmarks solo explícitos con plan y presupuesto; un
solo modelo grande a la vez; ascenso de un perfil únicamente por evidencia.

**Hecho (12-09, commits `99dbe3a`, `7a54923`, `017cc6c`, `e2b5995`; FAUSTUS
§77).** INF-00 auditoría del código real (MoE/MTP deducidos del nombre,
promesas de velocidad sin medir, opciones perdidas en silencio, timings del
motor descartados). INF-01: arquitectura desde metadatos con `unknown` como
resultado, recibo de traducción del comando (`applied/omitted/manual`),
reescrituras del servidor visibles. INF-02: contratos, manifiesto de
capacidades por implementación, evaluación antes del comando, recibo de
arranque solicitado → aplicado → confirmado por sondas pasivas. INF-03:
fases medidas con su fuente, «¿por qué ha tardado tanto?» bajo cada
respuesta, Scorecard con motor. INF-04: tres suites deterministas sin juez
LLM, runner secuencial por la puerta de admisión con presupuesto y parciales,
comparador con `inconclusive` honesto, perfiles con ascenso solo por
`improvement`, pestaña «Optimize for my machine».

**Queda.** Verificación en vivo en el 7001 y un primer benchmark real con un
modelo pequeño (autorizado por Luis); INF-05 (topología, presupuesto de
memoria, admisión reconciliada — Cookbook serve aún no pasa por
`vram_admission` —, candidatos con reinicio, `activate_profile`); INF-06/07
laboratorio (especulación, reparto entre GPUs, comparación de motores) solo
con autorización por tanda. Después, el documento de investigación de
memoria/contexto (F01 laboratorio de memoria y F04 atlas piloto primero,
según su propia secuencia).
