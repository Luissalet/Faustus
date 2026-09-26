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
- Hecho, pero distinto de lo planeado aquí: la respuesta va por el propio
  `chat_stream` (con `question_id`/`option_ids`/`revision` en el cuerpo,
  `routes/chat_routes.py::question_store.resolve_question`), no por una ruta
  `POST /api/questions/{id}` dedicada; `GET /api/questions` lista las
  abiertas y la fase SSE `question` sigue igual.
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

## OBJ-10 · Conectores Hoard y memoria de candidaturas — EN CURSO (13-09-2026)

Pedido por Luis con `PLAN_MAESTRO_CONECTORES_HOARD.md` (13-09) y ampliado
después a Jubhunter's Hoard («puedes mejorar Jobhunter y tocarlo como
necesites»). Principios del plan que se respetan tal cual: reutilizar el
MCP existente sin agregador propio; catálogo sin secretos; salud de la app
separada del handshake del adaptador; arranque solo por perfiles del
usuario (argv, sin shell, readiness, idempotente, sin matar procesos
ajenos); conectores persistentes pero **cumplidos por tarea** en el
despachador; sin fallback silencioso a API de pago; ack ≠ aceptación;
eventos idempotentes con referencia externa; los datos reales de Jobhunter
no se leen ni se migran desde aquí.

**Hecho (FAUSTUS.md §80).** Fases B (Jobhunter), C, D, E y F-Faustus:
catálogo y presets, sidecar, siete estados reales, perfiles de arranque,
`/connectors`, `connector_ids` por sesión/proyecto/tarea con enforcement en
`execute_tool_block` y filtrado antes del tool-RAG, `tool-support` sin
fallback, `external_ref` en calendario, filtros de correo, clasificador y
receta con fixtures, `record_employer_response` y biblioteca de respuestas
con ids/revisión/backup en Jobhunter.

**Queda (Fase G y lo que solo se puede hacer en su máquina).** Ver en vivo
en el 7001; conectar Jobhunter real (arrancar su servidor con el código
nuevo, preset con `JOBHUNT_DIR`, comprobar `available`) y probar con un
Qwen local: listar contexto y ofertas, leer una respuesta, registrar una
pregunta en un contexto de prueba; ejecutar «Recuperar de borradores» desde
la UI de Jobhunter (backup automático + el manual que haga Luis) y revisar
pendientes/variantes; Writer's Hoard necesita otro puerto que el 8766 de
Relief Studio (`WH_AIBRIDGE_PORT`); credenciales de correo/calendario en
los formularios existentes antes de la receta real; prueba de navegador
aparte (el MCP de Jobhunter no navega).

## OBJ-11 · Paridad con el harness de referencia y ventaja medida — CERRADO en su alcance de aceptación (36/36) (13/16-09-2026)

Pedido por Luis con el dictamen externo del 13-09-2026 (análisis de ChatGPT, 17 documentos, 36 recetas de aceptación propuestas). Objetivo del propietario: que no quede un motivo funcional ni práctico para elegir el harness de referencia; ventaja solo con evidencia. Reglas que se respetan: un solo núcleo (nada de segundo runtime ni segundo motor de memoria); ninguna fila se cierra por existir una clase; cada cierre enlaza un test que ejercita código real; lo no cerrado se declara abierto; la licencia (AGPL vs MIT) es decisión de Luis.

**Cerrado (FAUSTUS.md §83, §84, §92).** Los 36 casos de aceptación A01-A36 están en verde — `docs/spec/paridad/ESTADO_ACEPTACION.md`: 36 verde, 0 `xfail`, 0 `pendiente`, cada fila enlazando el test real que la cierra. Dos olas de doce lotes (T4-T8/U1-U7, más la integración A16-A36) cerraron: descubrimiento de tools auditado y acotado (A08-A09); offload de resultados grandes con aislamiento por tenant (A12-A13); Code Mode con la misma puerta de política del turno (A10-A11); sandbox enchufable con rechazo explícito, nunca ejecución silenciosa en host (A16-A17); reautorización OAuth de MCP acotada + catálogo de esquemas versionado (A18-A19); SDK TS instalado desde tarball real contra un servidor con auth (A20); `<faustus-chat>` embebible con aislamiento de sesión/token probado en tres capas (A21); login OIDC con PKCE y `state`/`nonce` de un solo uso (A22); identidad de servicio con scope y revocación efectiva (A23); DST/misfire/dedup del scheduler (A24) y skills git-backed con rollback verificado por digest (A25); loop-breaker determinista (A29) y presupuesto padre+hijos reservado/reconciliado (A31); evolución candidata con rechazo por esquema/privilegio, evaluación reservada (held-out) y reversión automática por digest de dependencia (A26-A30); benchmark sin coste `0` falso y sin reducir el denominador por una celda ausente (A32-A33); migración sin escribir credenciales (A34); ciclo de vida de distribución con backup/restore bit a bit (A35); mapa de superficie de sesión (A36).

**Sub-puntos que quedan abiertos, declarados en la propia matriz (nunca ocultos).** (a) `sdk/ts` no está publicado en ningún registro — solo tarball local, escrito a mano, sin CI de release. (b) Instalación limpia en máquina sin nada instalado no se ha podido automatizar en este entorno (sin máquina limpia disponible); solo backup/restore y la seguridad de desinstalar están probados de verdad. (c) «Compartir sesión» (link público/externo) es un hueco real: ninguna ruta lo cubre. (d) El sandbox de skills git-backed no pasa por el aislador `sandbox_exec.py`. (e) Multi-réplica con Postgres/Redis (fila 6 de `MATRIZ_PARIDAD.md`) sigue abierta. (f) Perfil headless/núcleo separado de servidor+UI+SDK (fila 4) sigue parcial.

## OBJ-12 — Faustus corre donde corre el proyecto: Windows de primera clase — EN CURSO (14-09-2026)

Abierto el 14-09-2026 después de que Luis usara la app sobre una carpeta
Windows y recibiera, turno tras turno, «bash no disponible en entorno»,
«arranca Docker», «/bin/sh: cmd: not found» y «the grep tool rejects absolute
Windows paths». Diagnóstico completo y lo ya hecho en FAUSTUS.md §85; lo
pendiente concreto, en PENDIENTES.md § «Ejecución nativa en Windows».

**El principio.** La máquina del usuario no es un obstáculo entre Faustus y el
trabajo: es donde el trabajo se verifica. Un harness que solo sabe ejecutar en
un contenedor Linux no puede comprobar un proyecto Windows — ni su `.bat`, ni
su `winget`, ni su `.venv\Scripts\python.exe` — y termina devolviendo al
usuario el comando para que lo ejecute él. Eso es exactamente lo que Faustus
existe para no hacer.

**Cerrado en este lote (§85).** Sandbox como opción y no como puerta
(`agent_sandbox_mode`, `auto` por defecto, `strict` conserva la regla dura);
herramienta `powershell` nativa; el `python` del proyecto en vez del nuestro;
rutas absolutas dentro del workspace aceptadas por el validador de argumentos;
responder a la propia pregunta del agente cuenta como continuación; un
`base_revision` mal escrito deja de abortar la escritura; bloque de entorno en
el prompt construido desde el propio ejecutor; y la respuesta «Siempre en esta
carpeta» de la tarjeta de permiso, guardada en disco y heredada por los chats
posteriores de ese workspace.

**Lo que falta para cerrarlo.**

1. **Paridad de detached.** `#!bg` solo existe en `bash`. Un `.bat` o un
   `winget install` largo debería poder irse al fondo desde `powershell` con la
   misma tarjeta de trabajo y la misma reinvocación automática.
2. **Pantalla de concesiones.** Ver y revocar las concesiones por carpeta
   (Settings › Security), con la ruta exacta a la vista, porque la herencia es
   por subárbol.
3. **El resto del toolchain Windows.** `cmd`/`.cmd` sin pasar por PowerShell,
   codificación de consola en salidas de programas que no son UTF-8, y rutas
   UNC (`\\servidor\recurso`) en el confinamiento del workspace.
4. **Un banco Windows.** Una carpeta de prueba con `.bat`, venv propio y un test
   suite, recorrida entera por el agente de punta a punta, como el benchmark de
   whiplash hizo con Deep Research: la prueba de que «verificar es su trabajo»
   se cumple sin que nadie lea el transcript a mano.
5. **Elegir el defecto por sistema operativo.** Hoy `agent_sandbox_mode` es
   global. En POSIX con Docker instalado, el defecto razonable sigue siendo el
   contenedor; en Windows nunca lo es.

## OBJ-13 · Reach: ojos en internet y grafo de código — EN CURSO (16-09-2026)

Abierto el 16-09-2026 con la ola Reach (R1-R4, FAUSTUS.md §93): que Faustus pueda leer un hilo de X, un issue de GitHub, un vídeo de YouTube o un paper de arXiv sin depender de CLIs de terceros ni de que el usuario copie/pegue texto a mano — y que pueda razonar sobre la arquitectura de un repo (quién llama a quién, qué cambió, qué rutas HTTP hay) sin leer ficheros uno a uno.

**Hecho.** `src/reach/` con 9 canales (web, youtube, github, reddit, x, hackernews, rss, arxiv, wikipedia), cada uno con backends ordenados y fallback real registrado; `reach_doctor` da el estado de los 9 sin nunca devolver un token. `src/code_graph/` sobre el Context Engine existente: `trace_path`, `detect_changes` desde `git diff` real, `get_architecture`, indexado automático tras cada turno de código. `src/fanout/`: N candidatos (local gratis + de pago mezclados) en aislamiento real, puntuación ponderada de seis dimensiones. `src/pdf_ops.py`: transformar un PDF existente (merge/split/rotate/compress/watermark/OCR), lo único que la capa PDF no cubría. `src/personas/`: 16 identidades de agente reutilizables encajadas en `agent_defs.py`.

**Queda.** Driver de sesión de navegador en vivo para que `x`/`reddit` puedan buscar sin depender de `reach_nitter_base` (hoy reporta `unavailable` honestamente en vez de inventar); SSE de progreso del fan-out en lugar de poll; picker de personas en el Studio (la API ya está lista); edge `INHERITS` en el Context Engine compartido para que `code_graph` recorra cadenas de herencia.

## OBJ-14 · Creator (plan de 43 paquetes) — EN CURSO (16-09-2026)

Abierto el 16-09-2026 con el plan Creator (`docs/spec/creator/plan/`, 43 work packages) para que Faustus produzca — no solo edite — imagen, vídeo, audio y música con la misma disciplina de evidencia y aprobación que ya tiene el resto del agente. `creator_enabled` en OFF por defecto: nada de esto es visible sin activarlo a mano.

**Hecho.** WP00 (inventario), WP01 (QA01: fingerprint versionado), WP02 (dominio de documentos), WP03 (biblioteca/linaje), WP04 (ingesta), WP05 (shell `/creator`), WP06 (`ModelSpec`/`DeploymentManifest`), WP07 (14 ejes de capacidad), WP08 (Model Explorer y comparación), WP09 (preflight puro), WP10 (puerto de adaptadores), WP11 (recetas ComfyUI expresivas, 7 de fábrica + cuarentena de usuario), WP12 (operaciones no destructivas), WP13 (timeline: relojes/snapping/retiming/undo real), WP14 (renderer FFmpeg determinista con perfiles y caché), WP15 (ASR/transcripción alineada, adapter whisper), WP16 (editor de subtítulos con exportadores propios), WP18 (casting de voces/TTS con registro de consentimiento), WP20 (canvas de imagen por capas, compositor PIL determinista), WP22 (storyboard y plan de producción con DAG validado), WP24 (Music Studio, adapter ACE-Step/MusicGen), WP27 (Goal con criterios tipados), WP30 (inventario físico), WP32 (lifecycle de plugins), WP36 (harness de pruebas de adaptadores/fallos), WP41 (presets/evaluación/descubrimiento). Todos los routers con rutas HTTP de este listado están montados en `app.py` bajo `creator_enabled` (OFF por defecto); el bug de VRAM del turno de humo (§94) sigue corregido.

**Siguiente.** WP17, WP19, WP21, WP23, WP25, WP26, WP28, WP29, WP31, WP33, WP34, WP35, WP37, WP38, WP39, WP40, WP42 — 17 paquetes del plan de 43 sin empezar. Ningún motor real (ComfyUI, faster-whisper, motores TTS, ACE-Step/MusicGen) está instalado en este entorno; verificación de Studio para los WP de esta ola pendiente por pantalla/MCP más allá de `tsc`/`vite build`.

## OBJ-15 · Tiempo como constructo — CERRADO (16-09-2026)

Pedido implícito en la auditoría del lote close-the-loop: sin un reloj en los resultados de tool, el modelo no distingue un comando de 40 ms de uno de 40 minutos. `src/tool_clock.py` (FAUSTUS.md §91) antepone `⏱ took … · turn elapsed … · started …` a cada resultado, con aviso explícito sobre 60 s, duración en el SSE `tool_output` y en la tarjeta de paso del Studio. Ajuste `agent_tool_wall_time`, activado por defecto. La doc pública del harness de referencia no muestra esto — se hizo igual, porque con modelos locales lentos cambia el comportamiento del agente.

## OBJ-16 · Harness para implementaciones largas (Silhouettes) — EN CURSO (17-09-2026)

Pedido el 16-09 tras el proyecto «3D modeling silhouettes»: 24 chats reales, un proyecto desde cero desastroso, un plan de ChatGPT de 172 KB que produjo código incoherente, y el mismo trabajo sacado adelante con otro harness. Análisis forense chat a chat → ocho medidas deterministas (FAUSTUS.md §95): puerta dura de cierre (`completion_gate`), smoke de UI conducido por el harness (`ui_smoke`, estado `ok_smoke`), deriva de dependencias al empezar el turno, delegación verificada con recibos y un reintento, política de reescritura completa (2ª → `require_edit`, 4ª → bloqueo), deuda de tests exentos como todos de alta prioridad, plan tracker con estado por tarea y tools `plan_*`, y «adjunto = ejecutar» (`plan_without_action`).

**Hecho.** Los seis módulos y su cableado en `agent_loop`/`agent_harness`/`tool_execution`/`filesystem_tools`, tests de módulo y de bucle (LLM guionizado), Studio (estados nuevos en la lista de checks, evento `plan_tracker`), settings y catálogo SSE.

**Hecho en vivo (17-09).** Chat #14 repetido en forma controlada contra el 27B: 4/4 tareas con tests reales, `ui_smoke` cazó un bug real que los tests no veían; tres fallos del harness corregidos sobre la marcha (§95). **Siguiente.** Repetirlo con un plan grande (≥ 60 KB) y con un segundo chat sin adjunto («Continua»). Enganchar los criterios tipados de `Goal` (WP27) a `plan_done`. Ronda de arreglo cuando la puerta dura degrada un turno (hoy solo impide sellar). Diarización/idioma por segmento no tiene que ver aquí — es OBJ-14.

## OBJ-17 · Centro de control de procesos — HECHO (17-09-2026)

Pedido el 17-09: ver qué se está ejecutando (puertos, apps abiertas por el asistente, trabajos en segundo plano) y poder pararlo. Hecho dentro de Faustus (no como conector: un conector es una herramienta del modelo, y el Stop tiene que ser de una persona). FAUSTUS.md §97. **Siguiente.** Nombre del servidor MCP en las filas hijas; herramienta de solo lectura para el agente; registro de lo que el asistente lanza por Windows-MCP.

## OBJ-22 · Faustus en el móvil (mando remoto del PC) — HECHO como web app instalable (17-09-2026)

Pedido: avisos al móvil cuando acaba cosas, ver progreso, mandar tareas y preguntar desde el móvil, todo el cómputo en el PC; «mejor una web app, como hacíamos». Hecho (FAUSTUS.md §103-§104): bus de notificaciones común (`src/notifications.py`) con ganchos en fin de turno, aprobaciones, tareas y recordatorios; API compacta `/api/mobile/*` con WebSocket; Web Push de verdad (RFC 8291/8292 con `cryptography`, claves VAPID propias, `/api/push/*`); manifest y service worker en la raíz del origen con `push` y `notificationclick`; Ajustes → Este dispositivo (instalar, activar/probar/quitar avisos); Studio con barra inferior de cinco pestañas (Inicio · Studio · Calendario · Notas · Ajustes) en pantallas estrechas. Verificado en vivo salvo la recepción de un push real (el navegador de escritorio de prueba tiene el servicio de push apagado). **Siguiente.** Prueba completa desde el móvil por la VPN de malla con HTTPS; vista de pulgar para aprobaciones en Inicio; acciones «Aprobar/Denegar» en la propia notificación; instalación guiada (QR con la URL).

## OBJ-21 · Apps: control de las apps del desarrollador desde Processes — HECHO (17-09-2026)

Pedido: conectar tres apps más, un menú para lanzarlas/pararlas/reiniciarlas con iconos y estado, «como Processes pero universal», con consola, alta y baja libres, y que todas abran en ventana de escritorio. Hecho (FAUSTUS.md §101): perfiles con icono/descr./stop/desktop, estado por pid o puerto, ventana Electron genérica con identidad propia en la barra, sección Apps en Processes, adaptador REST→MCP genérico y presets `dorian`/`gepetto`/`platos`. **Siguiente.** Arrancar apps al arrancar Faustus (autostart por perfil), agrupar por proyecto, exportar/importar perfiles, y que el agente tenga una herramienta de solo lectura sobre el estado de las apps.

## OBJ-18 · Candidaturas desde el correo (Jobhunter's Hoard + calendario) — HECHO (17-09-2026)

Pedido: «Revisa mi correo dos semanas y actualiza qué empresas me han rechazado / me han dado entrevistas en jobhunter's hoard y ponlas en el calendario», que funcione con el 27B local. Hecho como una herramienta determinista (`review_candidature_mail`, FAUSTUS.md §98) que hace la receta entera en una llamada. Verificado en vivo tres veces con el 27B: rechazos y entrevista registrados en Jobhunter (estado y mensajes), evento único en el calendario, tercera pasada idempotente en 44 s. **Siguiente.** Evento de día completo para entrevistas a demanda con fecha límite, texto de la receta, hora actual en el prompt.

## OBJ-19 · Vigilantes programados y tarjetas de Inicio — HECHO (17-09-2026)

Pedido: tareas programadas en lenguaje natural (tiempo diario, briefings de noticias, aviso de reposición en tienda, resumen de correo) y un panel de Inicio con las tarjetas elegidas. Hecho: cuatro acciones deterministas (`src/watchers.py`), `pin_to_home` en `manage_tasks`, tarjetas en Inicio y toggle en Automations (FAUSTUS.md §99). Verificado en vivo (tiempo desde el chat; correo, noticias y vigilancia por API; tarjetas con Refresh). **Siguiente.** Caso real de tienda con render JS (navegador integrado), tarjetas de calendario/candidaturas/precios, orden por arrastre.

## OBJ-20 · WhatsApp desde Faustus — HECHO y emparejado (17-09-2026)

Pedido: conectar la cuenta de WhatsApp para leer, resumir y enviar («dile a X…»); después «que tenga las capacidades de chat de WhatsApp Web + poder usar a Faustus para interactuar con él». Hecho en tres olas (FAUSTUS.md §100): puente Node por el protocolo multidispositivo (QR) con LID↔número, nombres de grupo, historial, multimedia, avatares y notas de voz transcritas; respuestas citadas, reacciones, reenvío, edición, borrado, ticks, presencia, acuses de lectura, búsqueda, adjuntos y notas de voz grabadas; panel «Ask Faustus» (resumir, redactar, traducir, preguntar) que nunca envía; herramientas `whatsapp_read` (con búsqueda e ids) / `whatsapp_send` (cita, adjunto, nota de voz) / `whatsapp_react` con permiso; tarjeta `whatsapp_digest`. **Siguiente.** Verificación en vivo de la tercera ola; respuestas automáticas con reglas («si escribe X avísame», «contesta a los del grupo con…»); crear grupos y gestionar participantes; estados; transcripción automática de audios largos en segundo plano.

## OBJ-23 · Ola de radar frente a otros workspaces de LLM — HECHO (19-09-2026)

Pedido implícito en la comparación continua con otros entornos de trabajo para LLM: cerrar diez huecos concretos encontrados en esa comparación. Hecho en una ronda (FAUSTUS.md §121-130): motores llama.cpp bajo demanda con apagado por inactividad (§121), decodificación especulativa MTP detectada del propio GGUF (§122), búsqueda de texto completo en resultados descargados con `artifact_search` (§123), consultas de impacto en el grafo de código con alias resueltos e indexado de todo el workspace (§124), independencia de fuentes sindicadas en Deep Research (§125), traza de cada llamada al modelo con bifurcación a otro modelo (§126), aviso de conflictos en memoria aprendida (§127), navegación de PDF por su propia estructura (§128), ranking de búsqueda web explicable (§129) y auditoría de accesibilidad/rendimiento en el smoke de UI (§130). **Siguiente.** Medir tok/s con MTP on/off en el 27B real (PENDIENTES.md, 19-09 tarde); decidir si se integra la idea del modelo pequeño de tool-calling on-device (aplazada, ver PENDIENTES.md).

## OBJ-24 · Extracción estructurada a un schema del usuario (herramienta de extracción + enrutado por complejidad del schema) — PENDIENTE

El enrutado de modelo por schema (#170) queda sin destino mientras no exista una herramienta de cara al usuario que extraiga texto/documentos a un schema propio. Falta definir la herramienta de extracción y, a partir de ahí, decidir el enrutado por complejidad del schema.

## OBJ-25 · Mostrar en Research los veredictos de citas y la revisión ciega — PENDIENTE

La revisión ciega de informes (FAUSTUS.md §144) y los veredictos de verificación de citas existen como datos que viajan con el resultado de la investigación, pero la vista de informe de Research no los muestra todavía. Falta la pantalla.

## OBJ-26 · Gancho de planificador para el pase de sueño de skills — PENDIENTE

El pase de sueño de skills (FAUSTUS.md §151) corre a demanda desde la pestaña "Proposals" o por API; queda pendiente el ajuste `skills_sleep_pass_enabled`/`skills_sleep_pass_hour` para que corra solo por la noche, como se pidió originalmente. Requiere primero decidir con qué patrón de `src/task_scheduler.py` debe encajar (tarea de sistema como el audit nocturno de skills, o un `ScheduledTask` de usuario) antes de cablearlo — ver PENDIENTES.md §151.

## OBJ-27 · Decisión tipada en vez de texto libre — EN CURSO (22-09-2026)

Del barrido de repos del 22-09 (ver `docs/radar/2026-09-22.md`). Es la entrada
de mayor retorno del lote y la que menos depende de nadie: no hay que adoptar
ningún proyecto externo, solo aplicar el principio.

**Qué falla hoy.** Faustus usa el modelo grande para decisiones cuya salida es
*elegir una opción de un conjunto cerrado*: clasificación de intención,
enrutado de modelo, selección de herramienta del tool RAG, y la próxima capa de
control de navegador. Eso no necesita generación. Paga el precio de una
llamada de 27B y además deja la puerta abierta a que el modelo invente un valor
que no está en la lista, que luego hay que parsear y validar.

**Qué tiene que pasar, en dos fases.**

1. **Decodificación restringida sobre el modelo que ya está cargado.** Cuando
   la salida pertenece a un conjunto cerrado, la petición lleva una gramática
   (GBNF en llama-server) o un formato forzado (`format` en Ollama) construido
   a partir de ese conjunto. La respuesta deja de ser parseable-con-suerte y
   pasa a ser válida por construcción. Sin dependencias nuevas.
2. **Un encoder pequeño para intención y enrutado.** Un modelo de decisión
   tipada (encoder no autorregresivo, del orden de 300-400M, Apache 2.0,
   descargable) resuelve la elección en milisegundos en vez de en una llamada
   al 27B. Cabe al lado del modelo grande. Solo tiene sentido después de la
   fase 1 y con etiquetas propias: en frío, fuera de dominio, estos modelos
   están cerca del azar.

**Criterio de aceptación.** Cada punto de decisión migrado demuestra, con la
misma entrada, (a) que la salida nunca cae fuera del conjunto, y (b) cuántos
tokens y cuántos milisegundos ahorra frente al camino anterior. Sin esa medida
no se da por cerrado.

**Relación con lo que ya hay.** `src/agent_harness`/`local_model_policy` y la
recuperación de herramientas (OBJ-7) son los primeros destinos. OBJ-24
(extracción a un schema del usuario) es el mismo mecanismo visto desde el otro
lado: si la fase 1 deja una capa de decodificación restringida reutilizable,
OBJ-24 se apoya en ella.

## OBJ-28 · Refinamiento del harness propuesto tras cada tarea — PENDIENTE (22-09-2026)

Del mismo barrido. El patrón: al terminar una tarea, revisar la trayectoria y
**proponer el edit CRUD más pequeño** sobre el estado del harness — prompt,
skill, memoria o especificación de sub-agente — con registro `disparador →
resultado` y deshacer por identificador. El prompt base queda inmutable; solo
se edita la capa de alrededor.

Faustus tiene media pieza: el pase de sueño de skills (FAUSTUS.md §151) ya
propone revisiones de SKILL.md que el usuario aprueba. Falta extenderlo a los
otros tres ejes y añadir el deshacer.

**Regla no negociable:** propone, nunca aplica. La crítica principal al
proyecto de donde sale el patrón es justamente que se auto-modifica sin
aprobación humana obligatoria. Encaja con OBJ-26 (cuándo corre) y con las
tarjetas de aprobación que ya existen (cómo se acepta).

## OBJ-29 · Omisión recuperable en el Context Engine — HECHO EN CÓDIGO, FALTA VERIFICAR EN VIVO (23-09-2026)

Hoy el Context Engine trata una omisión como dato de salida, no como log, pero
lo omitido no se puede recuperar: si el compilador decide que algo no entra, el
modelo no tiene forma de pedirlo. El patrón a copiar es la compresión
reversible: lo que no entra se guarda localmente con un identificador corto que
sí viaja en el prompt, más una herramienta que lo recupera bajo demanda. Ataca
directamente el pendiente de «cada turno arranca con 13-15k de 200k y el modelo
gasta rondas redescubriendo lo que ya sabía».

**La trampa, ya documentada en casa.** Los backends locales cachean el prefijo
del system byte a byte (por eso la línea de idioma va como `user` y no como
`system`). Una compresión que reescriba el contexto de forma distinta en cada
turno invalida ese caché y sale más cara de lo que ahorra. Lo comprimido tiene
que ser estable entre turnos o no se hace.

Va después de la fase 2 del Context Engine (llevar el compilador al camino
caliente detrás de la bandera), no antes.

**Hecho (FAUSTUS.md §174):** junto con la fase 2. Lo omitido por presupuesto
se guarda con un id corto determinista por (dueño, referencia); el paquete en
vivo lo lista al final como `[ctx:<id>] <título> (<fuente>)`, en orden fijo
para no romper el prefijo; `context_recall {ids}` lo devuelve entero con su
procedencia (herramienta de agente y del servidor MCP de contexto). Falta
medir en la máquina en vivo cuántas rondas ahorra.

## OBJ-30 · Canvas de diseño antes de codificar — HERRAMIENTA VIVA (22-09-2026)

Las siete dimensiones (requisitos, entidades, enfoque, estructura, operaciones,
normas, salvaguardas) como esquema forzado, guardadas en el grafo de conceptos.
Ver FAUSTUS.md §160c. Hecho el módulo, la pasada, la skill y la verificación en
vivo.

**Hecho después (13c2f88a, e6d35ca5, a72ea47b):** la herramienta de agente
`design_canvas` {goal, context?, name?, concept_id?, save?}, registrada en las
cinco piezas que la paridad exige. Dársela al agente destapó dos fallos que
nadie podía ver desde código:

- `_resolve_model` nunca implementó el centinela `"auto"` que le pasan el
  route de chat, `auto_review`, `doubt_review`, `research_review`, el torneo y
  la ruta de ejecución de herramientas: buscaba un modelo llamado literalmente
  "auto". Las pasadas de revisión fallan en abierto, así que llevaban sin
  ejecutarse quién sabe cuánto sin dejar rastro; el canvas no falla en abierto,
  y por eso salió. Ahora significa `default_model`.
- ya resolviendo, el canvas se lo escribía el modelo más pequeño de la máquina
  (el ayudante en :8082): 179,8 s bajo la gramática y completion vacía. El
  modelo del turno viaja ahora en el contexto de herramienta como `turn_model`
  y el canvas lo prefiere: 81,5 s y las siete dimensiones.

**Lo que falta:** la vuelta al canvas al terminar la tarea para comprobar que el
trabajo cumple lo que se diseñó — que es la mitad que le da sentido a guardarlo
y enlaza con OBJ-28.

## OBJ-31 · Hooks de ciclo de vida configurables — HECHO (23-09-2026)

Seis eventos, tres acciones que sólo añaden (command/inject/warn), presets, log y tarjeta en Ajustes → Tools. FAUSTUS.md §169.

## OBJ-32 · Instintos (aprendizaje continuo con confianza y alcance por proyecto) — HECHO (23-09-2026)

Extracción en segundo plano con el modelo de utilidad, confianza dinámica, promoción a global, evolución a borrador de skill, herramienta `manage_instincts`, pestaña en Skills. FAUSTUS.md §170. Pendiente: medir en uso real qué fracción de instintos extraídos son útiles (ver PENDIENTES).

## OBJ-33 · Bibliotecas incluidas: skills, reglas por lenguaje y agentes — HECHO (23-09-2026)

29 skills, 52 reglas, 19 agentes adaptados; `.faustus/rules/` descubierto e inyectado con presupuesto; pestañas Library y Rules. FAUSTUS.md §171.

## OBJ-34 · Selector híbrido de skills — HECHO (23-09-2026)

Semántico + léxico + disparador × prior de resultado; explicación por API y panel. FAUSTUS.md §172.

## OBJ-35 · Arriendo compartido de modelos entre instancias — HECHO (23-09-2026)

Fichero por instancia en carpeta compartida de máquina, líder de residencia por default, pines y reservas de vecinas respetados, adopción del default residente, `/api/local-models/instances`. FAUSTUS.md §173. Pendiente: verificar en vivo con dos instancias nuevas (ver PENDIENTES).

## OBJ-36 · Segundo cerebro: bóveda markdown propia, entidades con tiempo y grafo — HECHO (23-09-2026)

Lo que el dueño pedía tras ver la moda de «hazle un segundo cerebro a tu
agente»: no una imitación de una app de notas de terceros, sino una bóveda
markdown que Faustus mismo crea y posee, con el motor de contexto (fase 2,
OBJ-29) como camino de recuperación, validez temporal de los hechos,
entidades tipadas con relaciones, y una vista de grafo. Borrar una nota en la
bóveda silencia la memoria (reversible); nunca la olvida de verdad.

Hecho: `src/brain/` (bóveda con sincronización de dos vías, notas libres,
búsqueda FTS5 y grafo de notas; analizador temporal ES/EN sin inventar
fechas; entidades tipadas con relaciones y ventanas de validez, un edge por
hecho, revalidación que retira duplicados del modelo; extracción por reglas
+ pasada opcional del modelo de utilidad, siempre en segundo plano y sin
cargar/descargar modelos por su cuenta; resúmenes de entidad citados),
rutas `/api/brain/*`, servidor MCP `brain`, herramienta de agente `brain`,
fuente del Context Engine, y la pantalla `/brain` («Cerebro») con explorador,
lectura/edición, panel de entidad y grafo de fuerza dirigida. FAUSTUS.md
§176; referencia completa de API en `docs/api/brain.md`.

**Queda:** medir cuánto tarda el resumen de entidad en aparecer tras un rato
de inactividad real del modelo de utilidad; retirar solas las notas
generadas (proyecto/objetivo/concepto) cuya fuente se borró; dejar de
repetir el aviso de un fichero ilegible en cada sincronización una vez
reportado una vez; encender `agent_context_engine` en la instancia principal
tras esta prueba en la privada (ver PENDIENTES).

## OBJ-37 · Decisiones tipadas: clasificar leyendo probabilidades, no generando — HECHO (23-09-2026)

Donde el código clasificaba texto con listas de palabras clave frágiles,
una pregunta cerrada al modelo de utilidad ya cargado, respondida con un solo
prefill: las respuestas van etiquetadas con letras de un token y se leen las
probabilidades del siguiente token (valor, confianza, masa), sin generar
razonamiento, con prefijo compartido entre campos para aprovechar la caché
de prompt. Consultiva siempre, con presupuesto de latencia, nunca carga un
modelo, y la regla determinista sigue siendo la primera pasada y la reserva.

Hecho: `src/typed_decision.py` (formas OpenAI-compatible y Ollama nativa,
residencia, reserva por letra, estadísticas), `POST /api/typed-decision` y
`/stats`; cableado en la comprobación de actualidad del chat (sólo en los
casos en que la regla duda), el tipado de entidades «other» del segundo
cerebro (sólo en segundo plano) y sugerencias de conflicto de memoria
(estado `suggested`, nunca resueltas); evaluación con 100 casos ES/EN y modo
`--fake` para CI. FAUSTUS.md §177; API en `docs/api/typed_decision.md`.

**Queda:** medir en vivo contra el ayudante en loopback y contra Ollama
(`scripts/eval_typed_decision.py`), ajustar umbrales con esos números, llevar
las sugerencias de conflicto a la pantalla de Memoria y buscar otros sitios
con listas de palabras frágiles donde un error sea barato (p. ej. «¿merece la
pena guardar esta memoria?»).

## OBJ-38 · Grafo de código+: de qué está hecho un repo y qué caminos importan — HECHO (23-09-2026)

Comunidades (módulos y áreas con nombre, tests asignados a lo que ejercitan)
y flujos de ejecución (de cada punto de entrada, con criticidad, tests que lo
alcanzan y comando de pytest) sobre el grafo de código existente; `impact`
dice qué flujos toca un cambio. Herramientas `code_graph_communities` y
`code_graph_flows`, rutas `/api/code-graph/*` y servidor MCP `code_graph`.
FAUSTUS.md §179; spec en `docs/spec/code_graph.md`.

**Queda:** resúmenes por el modelo de utilidad en vivo; la resolución por
nombre del índice sigue inventando alguna arista dentro del mismo lenguaje
(`run_pca → transform`); una vista en Studio (el grafo del cerebro serviría
de base).

## OBJ-39 · «¿Ya existe?»: reutilizar, adaptar o escribir, con cada repo verificado — HECHO (23-09-2026)

Antes de construir, el agente descompone la idea con una lista de
comprobación y Faustus verifica en vivo cada repositorio que propone
(existencia, renombrado, archivado, fork, último push, licencia contra la del
proyecto), rebaja veredictos cuando la licencia o la salud no dan, y guarda
el informe. Herramienta `prior_art`, rutas `/api/prior-art/*`, servidor MCP
`prior_art`. FAUSTUS.md §179; API en `docs/api/prior_art.md`.

**Queda:** pantalla en Studio para los informes; poner el token de GitHub en
Ajustes si 60 peticiones/hora se quedan cortas.

## OBJ-40 · Deriva de arquitectura: una base del grafo de código y qué cambió desde ella — HECHO (24-09-2026)

El grafo de código (OBJ-38) describe la arquitectura en un instante; faltaba
saber si cambió desde la última vez. `snapshot` graba una base (comunidades,
acoplamiento, flujos, símbolos públicos aproximados); `drift` la compara
contra el grafo actual por solapamiento de ficheros (los ids son un hash del
propio conjunto, así que no sirven para emparejar directamente) y por campo
estable en los flujos: comunidades nuevas o desaparecidas, ficheros que
cambiaron de módulo, dependencias nuevas (con aviso si entran en una
comunidad antes aislada), ciclos introducidos, flujos que cambiaron de forma
o criticidad, símbolos públicos eliminados que aún se referencian. Puntuación
0-100 con los hallazgos explicados.

Hecho: `src/code_graph/drift.py`, enganche no bloqueante en el bucle del
agente (`src/drift_check.py`, ajuste `code_graph_drift_check`, activo por
defecto): graba una base antes de la primera edición del turno y compara tras
la última, con una nota en el resumen si la deriva supera el umbral.
Herramienta `code_graph_drift`, rutas `/api/code-graph/drift*`. FAUSTUS.md
§180.

**Queda:** medir en vivo durante un refactor real; el presupuesto de tiempo
por defecto (8-10 s) no se ha medido contra un repositorio de miles de
ficheros (ver PENDIENTES).

## OBJ-41 · Autonomía en sombra: aprender de qué aprobaciones se repiten sin arriesgar las que no — HECHO (24-09-2026)

Cada tarjeta de aprobación pedía siempre confirmación, sin importar cuántas
veces ya se hubiera aprobado exactamente lo mismo. Ahora, opcional
(`approval_autonomy`, apagado por defecto): `shadow` registra qué habría
decidido una puntuación de confianza 0-1 (solo lectura, si el usuario nombró
el objetivo, aprobaciones previas de esa familia, si es reversible) frente a
lo que la persona decidió de verdad, sin cambiar nada visible; `active`
aprueba sola una familia ya promovida (≥20 decisiones de confianza alta,
≥95 % de acuerdo, cero desacuerdos en algo destructivo). Bloqueos duros que
ninguna puntuación salta: nada destructivo, nada fuera del workspace
confirmado, ningún envío de mensaje o pago.

Hecho: `src/approval_autonomy.py`, integración en la única rama de la puerta
de aprobación que ya cubre esas dos clases de capacidad (nunca toca la
confirmación de escritorio ni el guardia de comandos peligrosos), rutas
`/api/approval-autonomy/*` (leer es admin, promover es solo humano — el
modelo no puede promocionar sus propias herramientas), panel en
Ajustes → Agente. FAUSTUS.md §180.

**Queda:** dejarlo en `shadow` una sesión real y revisar el historial;
revisar si la familia por nombre de herramienta es demasiado gruesa con datos
de uso reales (ver PENDIENTES).

## OBJ-42 · Dos skills nuevas: diseño web/accesibilidad y un tutor de repositorios — HECHO (24-09-2026)

`web-accessibility-design`: reglas de diseño web y accesibilidad WCAG 2.2
(semántica, teclado y foco, formularios, responsive, contraste, movimiento),
adaptada y condensada de un paquete de reglas con licencia MIT, con su
atribución completa en un `NOTICE` propio. `learn-this-repo`: un tutor nativo
que usa `code_graph_communities`/`code_graph_flows` para construir un plan de
estudio en orden de dependencia, explica cada módulo desde el código real,
hace preguntas de repaso, y guarda el dominio por módulo en un fichero de
notas del workspace para retomar la sesión más tarde. (La transcripción de
YouTube que se había pedido como tercera skill ya existía — `reach_read` con
`youtube_transcript_api` y comentarios por `yt-dlp` — así que no hacía falta
una nueva.)

De paso, un arreglo de fondo en el formato de skills (`services/memory/
skill_format.py`): una sección no reconocida (p. ej. `## Referencia: …`)
perdía su encabezado al volver a guardarse, así que una relectura posterior
la fundía dentro de la sección conocida anterior — justo lo que hace
`src/skill_library.py` al instalar una skill de la biblioteca. FAUSTUS.md
§180.

**Queda:** usar `learn-this-repo` de verdad con un modelo real y ver si el
fichero de notas resulta útil para retomar sesiones (ver PENDIENTES).


## OBJ-41 · Radar de git: qué repositorios esperan un commit o un push — HECHO (24-09-2026)

Un cambio hecho desde un chat, la pestaña cerrada, y el push que nunca
llega: el radar (`src/git_radar.py`) mira todos los repositorios que
Faustus ve — carpetas enlazadas de los proyectos más las carpetas vigiladas
globales (`git_watch_roots`, editables desde Source control) — y dice
cuáles tienen trabajo que no ha salido de la máquina (sin commit, sin push,
sin upstream, sin remoto, conflictos). Tira en Source control con clic al
repo, badge en la barra lateral y bloque en Inicio que sólo aparece cuando
hay algo. Los repos de carpetas vigiladas son repos normales del panel.
FAUSTUS.md §185; API en `docs/api/git.md`.

Segunda tanda (mismo día, FAUSTUS.md §187): vigilante programado
`git_radar` («avísame si algo lleva N días sin subir», avisa sólo cuando
cambia), herramienta `git_radar` para el agente («¿qué tengo sin subir?»)
e instantánea en disco para que la primera llamada tras reiniciar no
espere. **Queda:** nada abierto.

## OBJ-43 · Ingeniería autónoma: issue→PR, cazador de bugs, CI, memoria de arreglos, carriles y turno de noche — HECHO (24-09-2026)

Seis capacidades que faltaban frente a los agentes de código de moda y que
Faustus ahora tiene, sin quitar nada: `github_issue`/`git_open_pr` (de la
issue a la pull request respetando la política git del repo), `bug_hunt`
(tests de borde generados, ejecutados aislados y triados en bug real / test
malo), `ci_failures` + equipo `deploy-*` de cinco agentes de biblioteca,
`fix_memory` (ledger de arreglos por proyecto recordado antes de cada turno),
carriles de traspaso entre agentes (`off`/`shadow`/`enforce`, grafo Mermaid)
y turno de noche (cola desatendida con presupuesto e informe), más
`code_history` (churn, blame, co-cambio y riesgo por fichero/símbolo).
FAUSTUS.md §189; API en `docs/api/{git,bug_hunt,ci_failures,fix_memory,handoff_lanes,night_shift,code_history}.md`.
**Queda:** verificación en vivo con el 27B y con un repo real de GitHub (ver PENDIENTES).


## OBJ-44 · Familia Hoard usada de verdad desde Faustus — HECHO (25-09-2026)

Que cada app Hoard aguante preguntas reales hechas con el 27B, no solo sus
tests: arrancar todo desde el Hub, llamar a cada tool por el proxy y pasar
baterías de preguntas por el 7003. Dos vueltas el 25-09 (FAUSTUS.md §191 y
§192): puentes que arrancan su app, timeline de Argus en segmentos, `since`
humano en Links y Echo, primeras líneas que el índice entiende, búsqueda
híbrida en Vitruvius, duplicados plegados en Vulcan, `recall` que sabe qué
hay delante ahora, Writer's Hoard conectado (arranque por máquina en el Hub).
Tercera vuelta (§193): `since` común en `hoard_link` (Python y JS, en las 18
apps), DiskHoard guarda el último escaneo, Vitruvius acepta páginas por ruta,
y la landing con Vitruvius salió de principio a fin con el 27B (19/19 en
`page_assay`).
Cerrado en §195, §200 y §201: el selector ofrece las skills de familia (tags en
español, umbral configurable), `lookup_tools` solo propone nombres que se pueden
llamar, Writer's Hoard 0.1.3 publicada con una sola biblioteca, Nightingale's
Hoard publicado, y el Lab de Nightingale de punta a punta con el 27B sin
tarjetas de aprobación.

**Todo local (25-09, tarde; FAUSTUS.md §194).** Luis: «todo local, queremos
que sea potente por sí mismo». Nada de modelos de suscripción para visión ni
para nada. Hecho: el 27B carga su propio proyector (`--mmproj`) y lee las
imágenes él mismo en la GPU; los motores lo encuentran y lo pasan solos.
**Queda:** medir MTP (`draft-mtp`) con una sola instancia en el servidor;
reparto de ranuras entre instancias que comparten el servidor; probar
`--image-min-tokens` en escaneos con letra pequeña; repetir la ruta del
examen con visión local.


## OBJ-45 · Lo que valía la pena de otros harness: visión para modelos ciegos, contexto gestionado por el modelo, razonamiento por turno, enjambre local y podcast — HECHO (25-09-2026)

Cinco capacidades sacadas de revisar plataformas de otros fabricantes, sin
quitar nada y solo donde Faustus no lo tenía ya: resolvedor único del modelo
de visión auxiliar con autodetección local y ventana pequeña (`vision_routing`,
`vision_num_ctx`), herramientas `context_*` para que el modelo fije, saque o
resuma su propio contexto (con vertido por resultado en rutas no nativas y
tokens por trayectoria), modo Auto/Rápido/Pensar/A fondo por turno
(`think_mode`), `swarm_map` sobre los slots libres reales del servidor, y el
informe de Deep Research como podcast a dos voces Piper.
FAUSTUS.md §197; API en `docs/api/{vision,context_tools,research_podcast}.md`,
`docs/spec/think_mode.md`, `docs/recipes/swarm.md`.
**Queda:** ver en navegador el chip de razonamiento, los selectores del
podcast y la sección Swarm; medir el ofrecimiento automático de `context_*`
en una ejecución larga real; enjambre en modo `agent` en vivo.


## OBJ-46 · Investigación continua: configuración del modelo local, harness, contexto y herramientas

Acordado el 25-09-2026 (noche): «que no sea sólo prueba y error: un flujo de ideas, consejos y descubrimientos alimentando el trabajo, sobre modelos, harness, contexto, herramientas… y otros proyectos (GitTrend y similares)». Cada ronda manda subagentes a investigar con fuentes; lo que se aplica se mide en el 7006 antes y después. El registro de ideas con fuentes vive en el documento del proyecto `claude/faustus-investigacion-continua.md`.

**Aplicado y medido.**
- Prompt estable dentro del turno y entre turnos (Manus, «Don't Break the Cache»): el paquete de contexto no cambia de sitio ni de contenido dentro del turno (§203–§204); el razonamiento de las vueltas del turno se conserva para Qwen3 (su plantilla lo espera; quitarlo rompía la caché); el chat conserva el juego de herramientas del turno anterior mientras cubre el nuevo (la lista de herramientas va arriba del prompt). En una tarea larga, el recordatorio de idioma ya no se mueve cada ronda y una imagen de herramienta no arrastra el paquete (§207).
- Embebedor bilingüe propio para elegir herramientas y umbral de puntuación (§204).
- Llamadas que llama.cpp deja en el razonamiento, recuperadas (§205).
- Presupuesto de razonamiento que de verdad se aplica (`thinking_budget_tokens`, §206).
- Batería diaria de 20 tareas con comprobaciones deterministas (`scripts/daily_eval.py`, §206).
- Contar por mosaicos en `inspect_image` (LVLM-Count, §207).
- Skills del formato abierto de `SKILL.md` importables tal cual (§207).

**Por probar, en este orden.**
0. Decodificación especulativa con la cabeza MTP del propio 27B (`--spec-type draft-mtp --spec-draft-n-max 2`, y `ngram-mod` al lado): el GGUF la trae y el build del 8081 la acepta; en la comunidad da 1,7× de velocidad de generación sin VRAM aparte. Medir tok/s y aciertos con la batería diaria antes y después. Requiere reiniciar el 8081.
1. Línea base de la batería diaria en el 7006 y, con ella, el muestreo por modo de la ficha de Qwen3 (pensando: temp 0,6, top_p 0,95, min_p 0) frente a min_p 0,1, contando también palabras inexistentes en castellano (`typos_ab.py`).
2. Presupuesto de razonamiento por ronda: en el examen 29 una ronda que acabó en un `ls` gastó 3.579 tokens (6,5 min a 9 tok/s). Probar un presupuesto menor en las rondas que sólo leen o listan y el completo en la primera y en la síntesis, medido con la batería y el examen.
3. Recitar el plan (`todowrite`) cada pocas rondas en tareas largas, sin repetir la petición entera (ya se vio que eso hace empezar de cero).
4. Caché KV en q8_0 (`-ctk q8_0 -ctv q8_0`) y `--image-max-tokens` en el 8081: requieren reiniciarlo, y lo comparten las otras instancias.
5. Enrutador barato con el 3B para decidir si hace falta una herramienta (Probe&Prefill).
6. Votar varias lecturas de visión sólo en las celdas dudosas.

**Descartado por ahora (con motivo).** Decodificación especulativa con modelo borrador: resultados mixtos o negativos en multi-GPU y sin VRAM libre. `split-mode tensor`: exige caché KV sin cuantizar y enlace rápido entre GPU.

## OBJ-47 · Mejoras y funciones pendientes trasladadas desde PENDIENTES (26-09-2026)

PENDIENTES queda para fallos y comprobaciones; lo que es una mejora o una función nueva vive aquí. Cada línea conserva su origen.

- **Tipado de entidades sólo mira la primera frase** que las nombra (23-09, §177): una entidad mencionada de formas distintas podría merecer varios tipos.
- **Migrar a esquema los puntos de decisión de cada turno** (22-09, §160b, OBJ-27): clasificación de intención, enrutado de modelo, selección de herramienta siguen en texto libre; hoy sólo `auto_review`/`doubt_review`/`research_review` piden esquema.
- **Falta la herramienta de agente `design_canvas_pass.draft`** (22-09, §160b): registrarla y cerrar el ciclo (volver al canvas al acabar la tarea y comprobar que lo hecho cumple lo diseñado).
- **Razonamiento + salida restringida no conviven** (22-09, §160b): si algún día se quieren juntos, hacerlo en dos llamadas (pensar libre, luego rellenar); el esquema tampoco viaja hoy junto a `tools`.
- **Sin UI para el localizador de una cita** (20-09, §146): hoy sólo viaja en el texto inyectado al modelo.
- **Enrutado de modelo por schema bloqueado** (19-09, §133): no puede aplicarse hasta que exista una herramienta de extracción a schema de cara al usuario.
- **Idea: STT en streaming por WebSocket** con parciales en vez de esperar al silencio (17-09, §105), si la latencia de «oído en» molesta.
- **WS de móvil no reenvía histórico al conectar** (17-09, mobile M-A): sólo manda `hello.last_id`; el cliente Android (lote M-B) debe pedir `since_id` una vez y fiarse del socket después.
- **`watch_page` no ve stock renderizado sólo con JavaScript** (17-09, §99): decide disponibilidad por palabras clave en el HTML; valorar el navegador integrado para esos casos.
- **Ideas de tarjetas de Inicio sin hacer** (17-09, §99): calendario del día, «candidaturas» (Jobhunter pendientes/entrevistas), vigilancia de precio bajo un umbral, RSS por URL, arrastrar para reordenar.
- **Ideas de WhatsApp sin hacer** (17-09, §100): reglas automáticas, crear grupos, estados, transcripción en segundo plano de audios largos, `archived` fiable sin re-emparejar.
- **Ideas de Apps sin hacer** (17-09, §101): autostart de perfiles al arrancar Faustus, grupos por proyecto, importar/exportar perfiles, herramienta de solo lectura para el agente, consola en vivo por SSE.
- **Apps abiertas por Windows-MCP sin atribución** (17-09, §97): si hace falta, anotar en Faustus lo que el asistente lanza.
- **La puerta dura no abre ronda de arreglo propia** (17-09, §95): sólo impide sellar `complete`; valorar si conviene que dispare una.
- **`plan_done` no ejecuta criterios tipados** (17-09, §95): informa de ficheros no tocados pero el `Goal.test_passes`/`http_ok` de un WP aún no está enganchado al tracker.
- **Parser de planes heurístico** (17-09, §95): un plan en prosa pura da 0 tareas; valorar un fallback.
- **`delegation_receipts` con un solo reintento** (17-09, §95): si el segundo intento vuelve vacío no hay tercero; valorar si hace falta.
- **Vista móvil / disposición bajo 1280px** (CMP-01): las tres disposiciones del Studio no tienen efecto de rejilla en pantalla estrecha; el panel sigue siendo una capa superpuesta.
- **Canal `app_api`/`dom_cdp` sin llamador real** (CMP-10): `choose_channel` los admite como lógica pura, pero solo `native_a11y`/`pixels` tienen consumidor.
- **Diff por pares en Alternativas** (CMP-13): `compare()` (`src/alternatives.py`) da diff contra la base + ficheros en conflicto, no ALTERNATIVA-vs-ALTERNATIVA.
- **Requisitos: falta importación masiva y creación desde selección** (W4-A): la pestaña crea/edita/enlaza/consulta, pero no importa el fichero sidecar entero ni crea un requisito a partir de una selección del editor de documentos.
- **Rigor del comparador de bancos** (spec INF): usa `p95−mediana` y `n≥3` como proxy de dispersión, no un test estadístico; subir repeticiones si se quiere más rigor.
- **Cliente del SDK generado desde OpenAPI** (paridad, TF02): `sdk/ts` sigue escrito a mano; generarlo desde el OpenAPI del servidor en vez de a mano.
- **`user_request_gate.py` sin comparador por herramienta** (22-09/25-09, §166): cada herramienta nueva que deba pasar sin tarjeta con una petición explícita necesita su propio comparador; hoy sólo lo tienen `python`, `plugin_app` y la lectura de memoria.
- **MOD-05/`execution_router.py` sin reconciliar del todo** (ADP-22): `model_router.choose()` solo decide cuando el modelo pedido es `auto`; una sesión con modelo explícito sigue pasando por `execution_router.py`.
