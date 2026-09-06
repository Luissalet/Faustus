# Objetivos — hacia dónde va Faustus

Compañero de `PENDIENTES.md`. Ese fichero dice **lo que está roto o sin verificar**; este dice
**lo que todavía no existe y en qué orden hay que construirlo**. Si algo aparece aquí como hecho,
tiene sección en `FAUSTUS.md` y tests que lo prueban; si no, no está hecho.

Fuente: `D:\LocalAI\inspiration\MASTERPLAN_FAUSTUS_MULTIPROPOSITO.md` (04-09-2026), síntesis de las
referencias de OpenHands, Letta, n8n, Aider, ComfyUI, OpenCode y OpenClaw.

**El overhaul de interfaz tiene backlog propio**, en la rama `feat/studio-ui`: `docs/ui/OBJETIVOS_UI.md`
para lo que falta, `docs/ui/PENDIENTES_UI.md` para lo que puede romperse y `docs/ui/DECISIONES_UI.md`
para lo ya cerrado. No dupliques tickets de UI aquí.

**La decisión central:** Faustus no es otro chat con integraciones. Es el sistema operativo personal
de trabajo creativo y técnico: el usuario formula una intención y Faustus compone una skill o un
workflow, conserva contexto con permisos, ejecuta cada paso en el backend adecuado, pide aprobación
antes de efectos relevantes y entrega artefactos reproducibles.

**La regla de priorización:** una idea nueva entra solo si mejora uno de estos planos sin duplicarlo
— acceso, control, skill, memoria, ejecución, workflow o artefacto. Si no encaja, será una skill o
una integración posterior, no otra arquitectura paralela.

---

## Estado por fases

| Fase | Qué entrega | Estado |
|---|---|---|
| 0 · Contratos y migración segura | El vocabulario común: 8 contratos, catálogo de backends, tabla de artefactos | ✅ 04-09-2026 — `FAUSTUS.md` §30, 73 tests |
| 1 · Ejecución segura y artefactos | `DockerWorkspaceBackend`, router, y que el código deje de correr en el proceso web | 🟡 04-09-2026 — sandbox probado contra contenedores reales (§31) y **el `bash`/`python` del agente ya pasa por él** detrás de `agent_sandbox_execution` (§32). Faltan los runs de coding y la galería |
| 2 · Runtime de skills y memoria útil | Instalar/revertir capacidades sin tocar el core; alcances de memoria y `MemoryView` | 🟡 04-09-2026 — puente `SKILL.md`↔manifiesto, descubrimiento que para en el repo, `MemoryView`. Falta cablearlo al prompt y la instalación/reversión (§32) |
| 3 · Motor creativo | ComfyUI como servicio, plantillas versionadas, galería de artefactos con receta | 🟡 04-09-2026 — cliente ComfyUI, plantillas aprobadas y renders durables con procedencia (§35), **ejecutado contra un ComfyUI real** (§39) y **las 4 plantillas renderizadas de verdad, imagen y vídeo, repartidas entre las dos GPUs** (§40). Faltan galería y hwfit |
| 4 · Workflows, approvals y conectores | Procesos que sobreviven a reinicios, idempotentes | 🟡 04-09-2026 — aprobaciones con puerta humana (§33) **y el núcleo de workflows durables** (§34): claim antes de actuar, pausa por persona o por reloj, ramas. Faltan los conectores y cablear `skill`/`deliver` a algo que exista |
| 5 · Coding profesional | `ChangeSet`, repo map, LSP opcional, intents explore/plan/implement/review | 🟡 04-09-2026 — el contrato `ChangeSet` existe y **delega el veredicto en `prove`** en vez de inventar un quinto vocabulario (§37): afirmación contra evidencia, `ok` de tres valores, intents que prometen. Faltan repo map, LSP y cablearlo al harness |
| 6 · Gateway, canales y dispositivos | Un canal de bajo riesgo con pairing, nodos, después voz | ⏳ |
| 7 · Ecosistema y operación | Hooks versionados, CLI `doctor`, telemetría, transferencia de artefactos | 🟡 04-09-2026 — el **doctor** existe (§38): CLI, ruta y tool MCP, nada informa OK sin comprobarse y todo lo que falla lleva el arreglo. Faltan hooks, telemetría y transferencia |

---

## Fase 1 — ejecución segura y artefactos (P0, a medias)

**Objetivo:** que código, documentos y medios dejen de depender del proceso web.

- [x] `src/execution_backends.py` y `src/execution_router.py` sobre `contracts.ExecutionSpec`.
- [x] `DockerWorkspaceBackend`: uid 1000, **un** workspace montado, red denegada por defecto,
      `--cap-drop ALL`, `no-new-privileges`, límites de memoria/CPU/pids, timeout que mata el
      contenedor y cancelación por nombre. Nunca descarga una imagen por su cuenta.
- [x] `src/artifact_store.py`: recolección por hash de contenido, tipado, deduplicación y filas con
      procedencia. Lo que no se sabe queda a NULL.
- [x] Sondas reales en `capability_registry.observe()`, con tres estados que no se confunden.
- [x] `src/agent_gate.py` sigue siendo **política**; no se ha tocado.
- [x] **El shell del agente pasa por el sandbox**, detrás de `agent_sandbox_execution` (apagado por
      defecto; apagado = idéntico a antes, con test). `bash` y `python` corren en el contenedor con
      traducción de rutas del workspace. Encendido y sin sandbox: **rechazo con motivo, nunca el
      host**.
- [ ] `filesystem_tools` — **no se enruta a propósito**: ya está confinado por comprobación de ruta,
      y meterlo en el contenedor cambia latencia y semántica sin cerrar el agujero que importa.
      Revisar si aparece un caso que lo justifique.
- [ ] Los runs de coding (`agent_harness`/dispatch) siguen fuera del sandbox.
- [ ] Generalizar `src/generated_images.py` y la galería hacia la tabla `artifacts`.
- [ ] Cancelación cooperativa desde la UI (hoy `cancel()` existe en el backend y nadie lo llama).
- [ ] Limpieza de `data/artifacts/runs/`: nadie borra los directorios de scratch todavía.

**Criterio de parada, literal del masterplan.** No se avanza si un run puede leer `data/.app_key`,
escapar del workspace, heredar secretos o caer al host sin confirmación.
→ **Comprobado el 04-09 contra contenedores reales** (`tests/test_execution_backends.py`): las cuatro.

**Criterios de aceptación heredados de OpenHands:**

- [x] Una skill maliciosa no puede leer fuera del workspace (el `data/` del host no está montado).
- [x] Un timeout mata el contenedor y conserva la salida parcial **marcada como parcial**.
- [x] Un secreto no declarado es un rechazo antes de arrancar; el declarado no pasa por la tabla de
      procesos del host (`--env-file` 0600, borrado en un `finally`).
- [x] El fallback local **nunca** se activa en silencio: requiere dos síes independientes.
- [ ] El usuario puede ver qué backend, modelo, skill y entradas produjeron cada salida — la fila lo
      guarda, la UI todavía no lo enseña.

**Limitaciones que se aceptan a sabiendas y están escritas en el docstring del módulo:**

- `/artifacts` **no** es write-only (Docker no lo tiene); lo que hay es un directorio propio y vacío
  por run, más una foto previa para no atribuir mal la salida.
- Un secreto dentro de un contenedor lo ve quien hable con el demonio de Docker.
- Una allowlist de red **se rechaza**: enforzarla necesita un proxy de salida que no existe.

---

## Fase 2 — runtime de skills y memoria útil (P0, empezada)

- [x] **El puente:** `SKILL.md` ↔ `contracts.SkillManifest` (`src/skills_runtime/bridge.py`), con
      claves planas `permissions_*` porque el frontmatter de este repo no admite mapas anidados.
      Deny-by-default de verdad: sin `permissions_backends`, la skill no corre en ninguna parte.
- [x] Descubrimiento de `.odysseus/skills`, `.agents/skills` y `.claude/skills` **hasta la raíz del
      repositorio** (para en `.git`; sin repo no sube nada — antes llegaba al home del usuario y
      adoptaba sus skills personales). La procedencia se registra y **nunca eleva**.
- [x] `src/memory_view.py` sobre `contracts.MemoryView`: alcance como muro, orden determinista,
      descartados con motivo, `explain()` para el operador.
- [ ] Cablear `MemoryView` al prompt del agente y a `context_budget` / `context_ledger`. Hoy el
      módulo es puro y **nadie lo llama**.
- [ ] Alcances `user/project/skill/run` dentro de `memory_engine` y `memory_curator` (el contrato
      los define; el motor todavía no los guarda).
- [ ] `src/plugins/`: instalación, versión y reversión de una skill; hoy solo se lee lo que hay.
- [ ] `creative-bible.md` y `technical-context.md` dentro de `.odysseus/`, sin exigir Git.

---

## Fase 3 — motor creativo (P0, empezada)

- [x] ComfyUI como **servicio separado** (GPL-3.0: se integra por API, no se copia código) y
      `src/media_backends/comfyui.py` con submit/estado/cancelación/recogida de outputs. **Nunca
      instala** un modelo ni un custom node, y **comprueba antes de encolar**: pregunta a
      `/object_info` qué hay y rechaza nombrando el fichero que falta.
- [x] `config/media_workflows/` con plantillas versionadas — **nunca JSON arbitrario del agente**.
      `src/media_workflows.py` rellena solo lo declarado; `computed` son tablas de consulta, no
      expresiones; la sustitución reemplaza cadenas enteras, así que un prompt no puede salirse de
      su campo. Cuatro plantillas —tres de imagen y una de vídeo— que usan **solo nodos del core**
      de ComfyUI.
- [x] Renders durables: tabla `media_runs`, `poll()` que reconcilia **preguntando al motor** tras un
      reinicio, y un motor caído deja el run como estaba en vez de inventarle un fallo.
- [x] Procedencia completa en el artefacto: receta, versión, huella, semilla, motor, id del trabajo,
      modelo y **licencia**. El prompt **no** viaja: va un digest y una nota que apunta al run.
- [x] Rutas `/api/media/*` (sin ninguna que acepte un grafo, y hay test de eso) y 5 tools MCP.
- [ ] `services/hwfit/` con perfiles de VRAM/GPU, colas y estimaciones honestas. `rank_image_models`
      ya sabe decir «no cabe»; falta atarlo a una plantilla antes de encolar.
- [ ] Galería de artefactos con preview, receta, **licencia del modelo** y botón «variar/reproducir».
      Hoy los artefactos existen y nadie los enseña.
- [x] **La plantilla de vídeo** (04-09, §40): `video.short-form.v1` con SVD y **solo nodos del
      core** —resultó que AnimateDiff no hacía falta—. mp4 de verdad en 42,4 s. Queda que es
      img2vid: parte de una imagen, dura 14 fotogramas y no tiene audio.
- [x] **Ejecutado contra un ComfyUI real** (04-09, §39): instalado en `D:\LocalAI\ComfyUI` con su
      propio venv (torch cu128, que es el que tiene kernels para la 5060 Ti). Render de verdad en
      4,1 s sobre la 4070 Ti, artefacto con procedencia entera, cancelación, y el hito completo
      brief→render→aprobación. `Start-ComfyUI.ps1` / `Stop-ComfyUI.ps1` junto a los de Faustus.
- [x] **SDXL descargado y probado** (04-09, §40): `image.product` 10,1 s y `image.reference-edit`
      6,1 s contra el motor real. Los cuatro grafos han pasado ya por el validador de ComfyUI.
- [x] **Las dos GPUs** (04-09, §40): `src/media_backends/pool.py` encuesta a los motores de
      `COMFYUI_URLS` y elige **el menos ocupado y, a igualdad, la tarjeta más pequeña que sirva**,
      dejando la grande libre. La elección se explica en el run (`chosen_because`). Medido: dos
      renders en paralelo, uno por GPU, 6,1 s y 8,1 s.

Skills iniciales: `image.product` ✅, `image.reference-edit` ✅, `image.quick-draft` ✅,
`video.short-form` ✅ — **las cuatro renderizadas de verdad** —; `video.subtitle`,
`audio.voiceover`, `document.report` siguen sin existir.

---

## Fases 4–7 (resumen)

- **4 · Workflows:** `WorkflowRun`/`NodeRun`, reintentos, deduplicación, nodos `manual`, `schedule`,
  `webhook`, `skill`, `condition`, `wait`, `human_approval`, `artifact_store`, `deliver`. Los
  workflows guardan **IDs de conexión, nunca secretos**. No se avanza si al reiniciar se repite una
  publicación, un render o un email.
  - [x] **Las aprobaciones ya son reales** (04-09, §33): tabla `approvals`, `src/approval_store.py`
        y rutas. Conceder/denegar pasa por `require_human`, que el token interno del modelo **no**
        abre; leer y pedir siguen siendo `require_admin` porque pedir permiso no es darlo. Se guarda
        el plan entero, así que un plan que deriva se responde con los campos que se movieron.
  - [x] **La aprobación se exige antes de arrancar un run** (`execution_router.execute`): si el
        manifiesto levanta tarjetas, el run no arranca, se abre la pendiente y el motivo lleva su
        id. Incluye las tarjetas **implícitas** (pedir la red las gana aunque no se declaren), y un
        secreto de más invalida la tarjeta concedida.
  - [ ] Extenderlo al `bash`/`python` del agente, a los runs de coding y a los envíos que no vienen
        de una skill. Hoy esas rutas las cubre el sistema de aprobación de *tools* que ya existía,
        que aprueba un comando y no un plan.
  - [x] **El núcleo de workflows existe** (04-09, §34): `contracts/workflow.py`, tablas
        `workflow_runs`/`node_runs`, `src/workflows/` (store, engine, handlers), rutas
        `/api/workflows/*` y 5 tools MCP. La clave de idempotencia se deriva del plan y **se
        escribe antes de actuar**, así que un proceso muerto a media publicación vuelve a la fila,
        no al envío. `pausado` es un estado con su motivo (una persona o un reloj), no un error.
  - [x] Los tipos de nodo que **alcanzan fuera** (`skill`, `deliver`, `artifact_store`) **rechazan
        por nombre** mientras nadie les conecte un runtime. Un run verde sin correo enviado sería
        el peor fallo posible de un motor de workflows.
  - [x] **`skill` cableado a un render** (04-09, §36): un nodo cuyo `config.skill` es
        `media:<plantilla>` arranca el render y se pausa con hora de despertar, y cada despertar le
        pregunta al motor. Sin maquinaria nueva: reusa la pausa por reloj de la Fase 4 y la fila de
        `media_runs` de la Fase 3. Es el primer hito de producto en miniatura —brief → render →
        persona aprueba— y está probado en vivo.
  - [ ] Conectar `deliver` a un canal real y `skill` al `execution_router` para skills de código
        con workspace. Ahí es donde la Fase 4 se junta con la 1 y la 6.
  - [ ] Un planificador que llame a `advance()` en bucle (hoy lo llama quien quiera: la ruta, la
        tool MCP o una persona; nadie lo hace solo).
- **5 · Coding:** `ChangeSet` estándar (plan, ficheros, diff, comandos, tests reales, artefactos) y
  la unión de `auto_review`, `review_state`, `workspace_checkpoints` y `git_invariants`. Ninguna
  afirmación de arreglo termina sin diff y evidencia acorde al modo.
  - [x] **El contrato existe** (04-09, §37): `contracts/changeset.py` + `src/changesets.py`, rutas
        `/api/changesets/*` y 2 tools MCP. Sostiene la evidencia **por referencia** (lleva la sha
        del checkpoint, no el texto del diff) y **no añade un quinto veredicto**: `judge()` delega
        en `prove`. Rechaza una afirmación que la evidencia no sostiene, una exactitud no ganada, un
        `explore` que escribió, y un resultado de una ejecución que no ocurrió.
  - [x] `workspace_checkpoints.has_checkpoint()`: una sha desconocida ya no se contesta como «no
        cambió nada». Salió corriéndolo contra un checkpoint de verdad.
  - [x] **Cableado al final de cada turno del agente**: las afirmaciones salen de
        `find_claimed_paths(full_response)` —lo que dijo la RESPUESTA, no lo que registró el
        ledger— y el veredicto viaja en la tarjeta de resumen, que solo interrumpe con el titular
        cuando hay contradicción. Falta verlo en un turno real con un modelo real.
  - [ ] Repo map y LSP opcional; los intents como modos de verdad del harness, no solo un campo.
- **6 · Gateway:** orden prudente — canal de texto → pairing → nodos → companion → voz → widgets.
  `contracts.ExternalIdentity` ya define el binding, la revocación con motivo y las capacidades.
- **7 · Ecosistema:** hooks versionados sobre el bus de eventos, CLI `status/doctor/config validate`,
  OpenTelemetry, transferencia de artefactos con hash y expiración. Fleet solo después de un gateway
  unitario validado.
  - [x] **El `doctor`** (04-09, §38): `python -m src.doctor`, `/api/doctor` y la tool
        `faustus_doctor`. Reúne las sondas de las seis fases y contesta lo que una persona pregunta
        de verdad. Nada informa OK sin comprobarse —un `unknown` nunca se redondea—, todo lo que no
        está OK lleva el arreglo, y una capacidad ausente es un hecho, no un fallo (salida 1 solo
        con un `fail` real).
  - [ ] Hooks versionados sobre el bus de eventos y `config validate`.
  - [ ] Telemetría (OpenTelemetry) y transferencia de artefactos con hash y expiración.

---

## Primer hito de producto: «de brief a vídeo aprobado»

Completamente local cuando el hardware lo permita. Prueba a la vez proyecto, memoria, skills,
ejecución aislada, cola de medios, artefactos, workflow, aprobaciones y UI:

1. El usuario elige proyecto y aporta brief y referencias.
2. Faustus selecciona memoria de marca y usa la skill de guion.
3. Un workflow genera storyboard, imágenes y vídeo con plantillas ComfyUI aprobadas.
4. El render corre en `MediaWorkerBackend`, con presupuesto y cancelación.
5. Se guardan vídeo, miniatura, guion, prompts y procedencia como artefactos.
6. Un nodo de QA y el usuario revisan antes de enviar o publicar.

Segundo hito: **«de issue a parche revisado»**, la misma plataforma en coding.

---


---

## Auditoría de backend: los lotes de seguridad (05-09-2026)

Frente propio, con su documento: `inspiration/AUDITORIA_BACKEND_Y_FEATURES_FAUSTUS.md` (§8.1
reparte los lotes). El orden lo fija el propio informe, y SEC-1 va antes que nada.

| Lote | Qué entrega | Estado |
|---|---|---|
| **Sprint 0B** | Los parches pequeños de la auditoría: B-002, B-003, B-005, B-006, B-018 | ✅ 05-09-2026 — `FAUSTUS.md` §44, rama `feat/sec-1`, 1 commit, 37 tests nuevos |
| **B-007** | Disponibilidad de herramientas evaluada en el punto de uso | ✅ 05-09-2026 — `FAUSTUS.md` §45, 18 tests nuevos |
| **SEC-1** | Logs OAuth, herencia de secretos, prompts en argv, credenciales reutilizables y backup inseguro | ✅ 05-09-2026 — `FAUSTUS.md` §42, rama `feat/sec-1`, 5 commits, 90 tests nuevos. Lo que no cubre está en `PENDIENTES.md` |
| **AUTH-1** | Matriz declarativa de autorización y scopes fail-closed | ✅ 05-09-2026 — `FAUSTUS.md` §43, rama `feat/sec-1`, 2 commits, 42 tests nuevos. B-011 + C-010 (tokens) y B-004 |
| **STATE-1** | Settings con revisión y escritura transaccional | ✅ 06-09-2026 — §46, B-012 |
| **NET-1** | Broker de salida con perfiles de confianza | ✅ 06-09-2026 — §47.1, B-019 |
| **SSH-1** | Pairing, `known_hosts` privado, argv centralizado | ✅ 06-09-2026 — §47.2, B-025. **Rompe conexiones existentes hasta emparejar** |
| **MAIL-1 / UPLOAD-1** | Adjuntos con dueño y TTL; índice de subidas entre procesos | ✅ 06-09-2026 — §47.3, B-023 y B-021 |
| **LIFE-1** | Supervisor de tareas y propiedad de procesos | ✅ 06-09-2026 — §48, B-013 y B-001 |
| **RUN-1** | Leases, outbox, estados y reconciliación | ✅ 06-09-2026 — §49, B-014, B-015 y B-016 |
| **ART-1** | Blob / ArtifactOccurrence / DerivedArtifact | ✅ 06-09-2026 — §50, B-017. Nota de diseño + mitad aditiva; la migración es posterior |
| **CB-1** | El HF_TOKEN fuera de los scripts | ✅ 06-09-2026 — §51, B-024 |
| CAP-1 / EVAL-1 / MEDIA-1 | Registro de activos, laboratorio, derivados | ⏳ features, no bugs de la auditoría |

Regla que se hereda del informe y que SEC-1 ya siguió: **cada lote desplegable y reversible por
separado**, un solo dueño del schema por lote, y nada se declara hecho sin un test que planta un
centinela y demuestra dónde no aparece.

## Context Engine: de la sombra a la canónica (06-09-2026)

Frente propio, con su documento: `D:\LocalAI\inspiration\PLAN_CONTEXT_ENGINE_FAUSTUS.md` (§20
reparte las fases, §23 fija la evaluación). Lo construido está en `FAUSTUS.md` §53: contratos,
presupuestos, nueve adaptadores, compilador, manifiesto, bloques, cápsulas, experiencias, índice
de código, pizarra compartida, recetas multimodales, mantenimiento, 34 rutas, 8 tools MCP,
pantalla en Studio y 386 tests. Son **29 ficheros y 15.393 líneas** en `src/context_engine/`,
todo detrás de dos banderas apagadas por defecto.

Y ahí está lo que falta: **la Fase 1 está completa y la Fase 2 no está empezada**. Existen las
piezas de las fases 3 a 8 —los almacenes, con sus reglas y sus tests— pero nadie las llama desde
producción, porque la vía que las llamaría es la compilación canónica.

| Fase | Qué entrega | Estado |
|---|---|---|
| 0 · Línea base | Benchmark reproducible antes de tocar el camino caliente | ⏳ **sin construir** — se saltó para llegar a la sombra |
| 1 · Contratos y paquete en observación | Compilar en paralelo sin cambiar el prompt enviado | ✅ 06-09-2026 — §53; sombra cableada en `agent_loop`, evento `context_shadow`, `manifest.compare()` |
| 2 · Compilación canónica | Que el `ContextPacket` decida de verdad qué se envía | ⏳ **la costura está puesta y nadie la ha cruzado** |
| 3 · Bloques y cápsulas | Continuidad tras compactar o reiniciar | 🟡 los dos almacenes existen, están probados y tienen rutas y tools; no los llama nada en producción |
| 4 · Experiencias verificadas | Aprender de ejecuciones reales | 🟡 el almacén y `admit()` existen; falta el extractor que destila un run terminado en una experiencia |
| 5 · Índice estructural de código | Localizar contexto de repositorio con precisión | 🟡 el índice existe y es incremental; falta el benchmark contra búsqueda textual/vectorial que el plan exige |
| 6 · Memoria compartida | Colaboración sin duplicar investigación | 🟡 la pizarra existe; falta integrarla con Consejo, Dispatch y subagentes |
| 7 · Recetas multimodales | Reproducibilidad en imagen, vídeo y audio | 🟡 el almacén existe; nadie escribe una receta al renderizar ni la lee al repetir |
| 8 · Consolidación y optimización | Scheduler en idle, caché medida, reranking opcional | 🟡 las seis tareas existen y la caché mide; **nadie las llama en bucle** |

### Fase 0 — el benchmark que sigue sin existir (P0)

Es la deuda más incómoda de este frente: el criterio de salida de la Fase 0 era «benchmark
reproducible **antes** de cambiar el camino caliente», y el camino caliente ya tiene un
observador encima. Sin esto, la Fase 2 no se puede justificar con datos.

- [ ] Corpus de tareas reales: coding, investigación, escritura, imagen, vídeo y uso cotidiano.
- [ ] Casos con información relevante, distractores, contradicciones y datos obsoletos — los
      cuatro, porque un corpus sin distractores mide otra cosa.
- [ ] Medir el prompt **actual**: tokens por sección, cuántos recuerdos y chunks se inyectan,
      acierto, recuperación, latencia y consumo. La mitad de esto ya se puede leer del
      `context_ledger` y del informe sombra; falta el arnés que lo agregue.
- [ ] Ventanas reales por modelo y configuración (hoy `resolve_budget()` cae a
      `DEFAULT_UNKNOWN_WINDOW` cuando no puede probarlas).

### Fase 2 — migrar el camino caliente de sombra a canónica (P0)

El criterio de salida, literal del plan: *igual o mejor calidad con menos contexto promedio y sin
perder instrucciones críticas*. No se avanza sin la Fase 0 delante.

- [ ] **Que `agent_context_engine` haga algo.** Hoy `wiring.enabled()` existe, lee el ajuste y su
      propio docstring dice que **nadie lo llama**: está ahí para que los consumidores tengan un
      solo sitio donde preguntar cuando llegue el momento. Encender la bandera hoy no cambia
      ningún prompt.
- [ ] **Cablear el paquete a `_build_system_prompt`** (`src/agent_loop.py:2526`), que es donde se
      montan hoy memoria, documento activo, skills y contexto de proyecto. Es el punto de corte:
      o el paquete manda, o siguen mandando los concatenadores.
- [ ] **`project_id` todavía no llega a `_build_system_prompt`.** Su firma tiene `owner`,
      `workspace` y `session_id`, y nada más; el `project_id` sí llega a `wiring.build_request()`
      desde `harness_options`. Sin ese argumento, el alcance de proyecto —que es la mitad de la
      clave de la caché y toda la frontera de aislamiento de bloques y recetas— no puede aplicarse
      en el prompt canónico.
- [ ] **El adaptador de sesiones no tiene proveedor de historial.**
      `src/context_engine/adapters/sessions.py` se niega a propósito a leer la base de datos de
      sesiones (`SessionManager.get_session` no acepta dueño, muta `last_accessed` y devuelve una
      transcripción distinta de la que el turno está usando). Espera que el llamante que ya tiene
      los mensajes se los entregue con `set_history_provider`. **Nadie lo llama en producción**,
      así que `available()` es `False` y no hay sección `recent_messages` en ningún paquete
      compilado hoy. Una línea en el arranque de la ruta de chat, pero hay que decidir cuál.
- [ ] **`observe_receipt` está implementado, probado y sin cablear.**
      `src/context_engine/wiring.py` lo tiene entero —qué referencias abrió el turno, cuántos
      resultados de herramienta añadió, el `outcome_ref` y el veredicto— y sólo lo llaman los
      tests. Es la costura que cierra el bucle: sin recibos, `historical_utility` no tiene de
      dónde salir y la selección no se puede medir. Va con la Fase 2 porque un recibo sobre un
      paquete que no se entregó no significa nada.
- [ ] Recorte seguro de resultados de herramientas antiguos, que hoy hace `context_compactor` por
      su cuenta y con otro criterio.
- [ ] Recuperación bajo demanda (progressive disclosure) desde el propio turno, no sólo en la
      compilación inicial.
- [ ] **Fallback por bandera al comportamiento anterior**, y con test de que apagado es idéntico a
      antes — la misma regla que se aplicó al sandbox del agente (§32).

### Las ablaciones de §23

El plan es explícito: *no adoptar GraphRAG, rerankers complejos o un nuevo vector store sin una
mejora medida sobre tareas reales*. Ocho configuraciones a comparar sobre el corpus de la Fase 0,
y ninguna se ha corrido todavía:

- [ ] 1. contexto actual (el prompt que monta hoy `_build_system_prompt`);
- [ ] 2. sólo BM25;
- [ ] 3. BM25 + vectores;
- [ ] 4. híbrido actual (lo que hace `memory_engine` y `rag_vector` hoy);
- [ ] 5. Context Compiler **sin** experiencias;
- [ ] 6. Context Compiler completo;
- [ ] 7. con y sin índice estructural de código;
- [ ] 8. con y sin cápsula después de compactar.

Las métricas que hay que sacar de cada una están en §23: recuperación (`Recall@k`,
`Precision@k`, MRR/nDCG, contradicciones relevantes recuperadas, fuentes obsoletas incluidas por
error, duplicación entre secciones), agente (éxito de tarea, número de tools y pasos, relecturas
evitables, errores por contexto ausente y por contexto obsoleto, continuidad tras compactar,
veredicto de `prove`, intervención humana) y coste (tokens de entrada por llamada y sección,
porcentaje de ventana, latencia de compilación y por fuente, hit rate de caché, disco y RAM por
propietario). El ledger (`context_packets`) ya guarda la mitad de las de coste; las de
recuperación necesitan el corpus.

### Ganchos que otros planes van a necesitar

- [ ] **Mantenimiento sin planificador.** Las seis tareas de `src/context_engine/maintenance.py`
      sólo se disparan a mano: `POST /api/context/maintenance/run` o la tool
      `context_diagnostics`. `should_yield()` ya sabe cederle la máquina a un turno en vuelo; lo
      que falta es quien la llame en idle. Es el mismo agujero que el `advance()` de los
      workflows (Fase 4) y probablemente el mismo planificador.
- [ ] **Nada escribe una experiencia.** El extractor que convierte un run terminado —con su
      ChangeSet y su veredicto de `prove`— en una llamada a `experiences.admit()` no existe. Sin
      él la Fase 4 es un almacén vacío con muy buenas reglas de admisión.
- [ ] **Nada escribe una receta.** `src/media_backends/` y `media_runs` tienen todo lo que
      `GenerationRecipe` necesita; falta el enganche al terminar un render y la lectura al pedir
      «lo mismo pero con esta otra referencia». Es lo que junta la Fase 3 del masterplan con la
      Fase 7 de este plan.
- [ ] **La pizarra no la usa ningún subagente.** Consejo y Dispatch siguen comunicándose por el
      resumen del coordinador, que es exactamente el canal con pérdidas que `shared_memory`
      existe para sustituir.
- [ ] **`MemoryView` sigue sin cablear** (Fase 2 del masterplan, arriba). Cuando se cablee hay que
      decidir si el alcance lo aplica él o `ContextPolicy`: dos muros para lo mismo es cómo se
      abre un agujero en uno de los dos.
- [ ] **Incógnito no llega desde la ruta de chat.** `wiring.build_request()` lo lee de
      `harness_options` y la ruta todavía no lo rellena, así que hoy siempre es `False`. La
      política ya está escrita y aplicada antes de la recuperación; falta el interruptor.

## Project Context Links: lo que el plan 2 deja pendiente (06-09-2026)

`FAUSTUS.md` §54 cierra las fases 1 a 4 de
`inspiration/PLAN_PROJECT_CONTEXT_LINKS_FAUSTUS.md`: identidad de proyecto en
`sessions.project_id`, vínculos tipados sobre `context_items`, cinco resolvers, la tool
`manage_project_context`, seis rutas HTTP, la fuente del Context Engine y la pantalla. 12 ficheros
nuevos, 4.189 líneas, 176 tests. Lo que sigue es lo que el mismo plan deja fuera, más los cabos
que se vieron al cablearlo.

### Fases 5, 6 y 7 del plan (P1)

- [ ] **Versiones `snapshot`.** `VERSION_POLICIES` la acepta y **los resolvers la rechazan** con
      el motivo dicho: necesita materializar un Artifact inmutable desde el documento y enlazar
      esa copia, y ese camino de escritura no existe. Hoy la única forma de fijar una revisión es
      `pinned` con número de versión, que sigue el documento vivo si alguien borra esa versión.
- [ ] **Invalidación atómica del índice.** `refresh` marca `index_status="stale"` y **conserva**
      `index_revision` a propósito (§13 del plan: el índice viejo sigue sirviendo hasta que el
      nuevo esté completo). Falta la otra mitad: el intercambio, que hoy no puede fallar porque
      nadie lo hace.
- [ ] **Multimodal.** Imágenes y vídeo se **describen** (etiqueta, tipo de medio, tamaño, receta
      de generación: modelo, backend, seed, versión) y nunca se decodifican. Qué representación
      debe recibir un modelo multimodal es decisión del Context Engine y todavía no la toma nadie.
- [ ] **Promoción automatizada.** Nada convierte un documento muy citado, una carpeta muy leída o
      un artefacto aprobado en un vínculo del proyecto: adjuntar es siempre un acto explícito del
      usuario o del agente. Es la fase 7 y es la que necesita antes las señales de uso.

### La indexación que nadie procesa (P0 para que el vocabulario no mienta)

- [ ] **`index_status="queued"` no lo consume nadie.** `ProjectContextService.attach` lo escribe
      en cada vínculo nuevo (salvo con `retrieval_policy="disabled"`, que nace en `none`) y
      `refresh` escribe `stale`; **no hay indexador por proyecto**, así que ningún vínculo llega
      jamás a `indexing` ni a `ready`. Los resolvers ya exponen `extract()` con chunks y
      localización, y `ProjectStore.context_revision` ya existe para que un trabajo asíncrono
      descarte su resultado si los vínculos se movieron: las dos piezas están, falta el trabajador
      y el sitio desde donde llamarlo. Mientras tanto la lectura es directa por resolver y el
      Context Engine la marca `degraded` con nota, que es el estado honesto — pero la pantalla
      enseña «Indexación en cola» de algo que no está en ninguna cola.
- [ ] **Los tres eventos que nadie emite.** `EVENT_NAMES` tiene los ocho nombres
      `project_context_*`; el servicio emite cinco. `project_context_indexed`,
      `project_context_index_failed` y `project_context_retrieved` esperan al indexador y al
      recibo de recuperación.

### `run_id` y `turn_id` no llegan a las tools (P0, barato)

- [ ] **El `ctx` de las tools no lleva `turn_id`, y su `run_id` está siempre vacío.**
      `src/tool_execution.py` construye el `ctx` con `progress_cb`, `session_id`, `owner`,
      `gen_overrides`, `harness_options`, `project_id` y `run_id` — y su propio comentario dice
      que `run_id` **no viaja en `turn_options`** (el bucle lo guarda en
      `ToolRunSecurityContext`, que no llega ahí), así que sale `""`. `turn_id` no aparece en
      absoluto: `do_manage_project_context` lo acepta como parámetro y el despachador no se lo
      pasa.
- [ ] **Consecuencia directa: las prioridades 3 y 4 de `references.resolve()` no se pueden
      disparar.** «Creado en el turno actual» compara `e.turn_id == turn_id` y «el turno anterior
      compatible» exige `e.turn_id` no vacío; con todo a `""` la resolución de «este documento»
      cae siempre al puntero global de la sesión (prioridad 2) o al título (prioridad 5). El
      código de las dos prioridades está escrito y probado con ids inyectados en los tests; lo que
      falta es que el runtime los ponga. Es una línea en `agent_loop.py` para meter `run_id` y
      `turn_id` en `turn_options`, y una en `tool_execution.py` para pasarlos.

### Quién registra una referencia de turno (P1)

- [ ] **Sólo las tools de documentos lo hacen.** `_note_turn_reference` está cableado en
      `create_document`, `update_document` y `edit_document`. **La generación de imagen y las
      subidas no registran nada**, así que «añade esta imagen al proyecto» justo después de
      generarla no tiene a qué resolver y termina pidiendo un id que el usuario no ve. Los kinds
      `artifact` y `gallery_image` ya tienen resolver; lo que falta es la llamada de una línea al
      terminar cada uno de esos caminos.

### El prompt no describe la tool nueva (P1, una entrada de diccionario)

- [ ] **`TOOL_SECTIONS` (`src/agent_loop.py`) no tiene sección para `manage_project_context`.**
      Tiene `project_context`, `search_project_chats` y `project_objectives`; la mitad mutante
      llegó al esquema, al índice de recuperación, al preflight y al despacho, pero no a las
      instrucciones en prosa que el prompt del sistema le da al modelo sobre cómo usarla. El
      modelo la ve en el esquema y no en el manual, que es exactamente el reparto que produce
      llamadas con la forma correcta y la intención equivocada.

## Perfiles de agente y Completion Modes: lo que el plan 3 deja pendiente (06-09-2026)

`FAUSTUS.md` §55 cierra el grueso de
`inspiration/PLAN_PERFILES_AGENTES_Y_COMPLETION_MODES_FAUSTUS.md` (plan 3 de 11) **sin crear un
catálogo paralelo de perfiles**: `AgentDef` gana 15 campos y 13 claves de frontmatter, y todo lo
ortogonal a la identidad vive en `src/agent_profiles/` (8 ficheros, 5.414 líneas), con 7 rutas HTTP,
35 perfiles versionados en cinco familias, 5 packs y los diez perfiles de §8 — de los cuales dos
(`surgeon`, `auditor`) se publican ampliando `implementer` y `reviewer` en lugar de duplicarlos.
2.866 líneas de test, 252 tests. Lo que sigue es lo que el mismo plan deja fuera, más los cabos que
se vieron al cablearlo.

### La resolución se fija tarde (P0)

- [ ] **Se resuelve cuando arranca el tool, no al encolar.** `_attach_resolution` en
      `src/agent_tools/subagent_tools.py` fija la `ResolvedAgentExecution` de cada worker justo antes
      de construir su primer prompt, que es correcto para una delegación inmediata y **no es lo
      mismo** para un trabajo despachado: entre encolar y arrancar puede haber horas, y en esas horas
      alguien edita el `AGENT.md`. La revisión que se fija es la del momento de arrancar, así que la
      promesa de §20 —«un trabajo encolado hoy no cambia de comportamiento porque alguien mejore un
      agente esta tarde»— sólo se cumple hoy para el camino corto. `src/dispatch.py` **no importa
      `agent_profiles`** en absoluto. Lo que falta es llamar a `resolver.resolve()` + `snapshot()`
      al construir el job y a `rehydrate()` al arrancarlo; las dos funciones existen, están probadas
      y `rehydrate` ya sabe decir en `caveats` que la definición cambió desde que se fijó.

### La `CompletionPolicy` efectiva no llega al bucle (P0)

- [ ] **`src/agent_loop.py` no ve el modo resuelto, así que parar sigue siendo heurístico.** Del
      modo sólo llega una frase al preámbulo del worker (`run.completion_note`, la `description` de
      la política) y el recorte de `max_rounds`/`timeout_s` por `min`. Los cuatro campos que dicen
      *cuánto más* —`explore_frontier`, `bonus_budget_share`, `stop_on_core_proved`,
      `max_extra_layers`— **no los lee nadie**: `agent_loop.py` no importa `agent_profiles`. Un
      `literal` y un `maximalist` producen hoy exactamente el mismo criterio de parada, y la
      diferencia se la deja al modelo por prosa. Mientras eso siga así, un modo es una sugerencia
      bien documentada y no una política.

### Los siete eventos de §1.7 que nadie emite (P1)

- [ ] **Ninguno de los siete existe en el código.** `agent_resolution_created`, `agent_selected`,
      `agent_selection_degraded`, `completion_mode_resolved`, `completion_mode_changed`,
      `agent_definition_updated` y `agent_capability_unavailable` no aparecen en ningún fichero del
      repositorio — ni en `src/contracts/event.py::EVENT_NAMES`, ni emitidos, ni consumidos. Todos
      tienen que llevar `resolution_id`, `agent_slug`, `definition_revision`, `project_id`,
      `session_id`, `run_id` y el motivo cuando proceda, y todos esos datos ya están dentro de la
      `ResolvedAgentExecution`: es cablear, no derivar. Sin ellos, la caché del Context Engine y
      cualquier proyección de estado no pueden reaccionar a un cambio de definición ni a un
      candidato que se cayó.

### `planner` sin ampliar (P1)

- [ ] **El tercer built-in se quedó fuera del plan.** `implementer` y `reviewer` se publican
      aumentados desde `agent_profiles/builtin.py`; `planner` sigue exactamente como estaba, con
      `default_completion_mode` en el valor por defecto, `capabilities` vacío y las cuatro
      referencias de perfil en `default`. Es coherente —§7 separa planificador de explorador, y no
      había entrada de §8 que le correspondiera— pero significa que **el coordinador que más se usa
      es el único que no participa de la selección por capacidades**: no declara `planning` y por
      tanto un filtro duro que la pida lo descarta. Decidir si se le añade una entrada `augments` o
      si se acepta a propósito, y escribirlo donde se vea.

### Dos constantes que deberían subir a `src/constants.py` (P2, barato)

- [ ] **`OUTCOMES_FILENAME` está declarado en `src/agent_profiles/selection.py`** y su propio
      comentario dice por qué no debería: `src/constants.py` es la única fuente de verdad de lo que
      vive bajo `DATA_DIR`, y este cambio no podía tocarla. El fichero de resultados observados de la
      selección (`agent_selection_outcomes.json`) es persistente y de propietario, exactamente como
      el resto de los que sí están allí. Es una línea movida y una nota en la revisión.

### `SelectionTrace` no tiene `caveats` (P2)

- [ ] **La traza de selección mete sus advertencias dentro de `reason`.** `SelectionTrace` tiene
      `requested`, `chosen`, `reason`, `alternatives_rejected` y `scores`, y no un campo para lo que
      hay que saber sin ser un rechazo — por ejemplo que una petición humana pasó por encima de una
      cuarentena, que hoy se escribe como `caveat: …` **concatenado dentro de `reason`** y recortado
      a 400 caracteres con el resto. `ResolvedAgentExecution` y `AgentDef` sí tienen `caveats`, y por
      eso pierde: una advertencia que viaja dentro de una frase no se puede contar, ni filtrar, ni
      pintar distinta en la pantalla. Añadir el campo es barato; lo que hay que decidir antes es si
      los caveats de selección se funden con los de la resolución al construirla o se quedan
      separados para poder decir de dónde salió cada uno.

### `cost_latency_fit` con peso 0, y el mapeo tarea→`TaskSpec` que falta (P1)

- [ ] **El término existe, se calcula y se reporta con peso 0.** §14 pide un ajuste de coste y
      latencia, y `WEIGHTS` lo lleva a `0.0` **a propósito y dicho en el código**: `TaskSpec` no
      carga ni plazo ni presupuesto, así que no hay nada contra lo que ajustar, y preferir el carril
      barato de todas formas sería el módulo inventando una preferencia que el llamante nunca
      declaró. Se reporta en cero para que la entrada que falta se vea en la traza en vez de
      olvidarse. Lo que hay que construir antes de subirle el peso es el par de campos en `TaskSpec`
      y quién los rellena.
- [ ] **`resolver._call_filtered(TaskSpec, **task)` sólo pasa las claves que casan con campos de
      `TaskSpec`**, y el diccionario `task` que le llega es el contrato de override de §15
      (`model`, `endpoint_id`, `max_rounds`, `timeout_s`, `completion_mode`…), que **no comparte casi
      ninguna clave** con `intent`, `description`, `required_capabilities`, `required_tools`, `mode`,
      `specialties` y `output_contract`. Resultado: cuando nadie nombra un agente, la selección
      recibe hoy una `TaskSpec` casi vacía y decide con muy poca información, aunque el ranking
      completo esté escrito y probado. El filtrado por firma es correcto —los dos módulos se
      escribieron en paralelo—; lo que falta es **el mapeo explícito de una tarea de dispatch a una
      `TaskSpec`**, y el sitio natural es el llamante, no el resolver.

## Modo Consejo — lo que queda (P1)

El plan 4 de 11 (`inspiration/PLAN_MODO_CONSEJO_MULTIMODELO_FAUSTUS.md`) está construido hasta la
fase 4: contratos, persistencia, ledger, síntesis, participantes, contexto, planificador, eventos,
políticas, orquestador, adaptadores y servicio, más `routes/council_routes.py` y la pantalla
`/council` (`FAUSTUS.md` §56), con la auditoría de conexión de §56.10 aplicada — el ledger publica
en el flujo, `verified` es alcanzable sólo con un paquete de `prove`, y `GET /{id}/state` existe.
Lo que sigue **no** está, y esto es lo que falta por construir; lo que está construido y no cuadra
vive en `PENDIENTES.md`.

### Fase 5 del plan: nada de esto existe (P1)

- [ ] **Router aprendido.** Hoy el enrutado es determinista (`policies.route`) y devuelve `None`
      cuando las reglas no deciden — que es la señal de que ahí se *puede* gastar una llamada de
      coordinador. Falta la mitad que aprende de lo que la sala acabó haciendo: qué participante
      aportó de verdad en qué clase de asunto, para dejar de preguntar a todos. Requiere resultados
      observados por `(participante, intención)`, que es la misma estructura que `agent_profiles`
      ya usa para su ranking; el riesgo a evitar es inventar un segundo almacén de resultados.
- [ ] **Compresión incremental del contexto.** `context.py` cuenta el «resumen del resto» y no lo
      escribe, a propósito. Lo que falta es comprimir **entre turnos** en vez de recortar en cada
      uno: hoy una sala larga paga la ventana entera en cada ronda y lo único que la protege es el
      límite de caracteres del bloque de pares.
- [ ] **Worktrees aislados por participante.** Un `collaborate` con dos drivers hoy se serializa a
      base de claims sobre el mismo árbol. Con una worktree por propietario, dos tareas sin
      solapamiento de recursos podrían correr de verdad en paralelo — y el ledger ya sabe cuáles no
      se solapan (`ready_tasks` lo calcula). Depende de que los runs de coding pasen por el sandbox
      (Fase 1, todavía abierta).
- [ ] **Participantes remotos.** Un asiento es hoy un modelo local o un endpoint configurado. El
      plan quiere sentar a un participante que vive en otra máquina; eso cae encima de la Fase 6
      (gateway, pairing, nodos) y no antes.
- [ ] **Métricas por política.** No hay ni una: cuánto cuesta un `debate` frente a un `consult`,
      cuántas rondas hacen falta antes de converger, con qué frecuencia una política acaba en
      `disputed`. `scheduler.stats()` tiene los números por sala y nadie los agrega; sin esto, elegir
      política es una corazonada y el techo de gasto de una sala se pone a ojo.

### Del plan, lo que no se hizo en las fases ya cerradas (P1)

- [ ] **La aprobación humana del turno no está cableada.** `awaiting_approval` es un estado del grafo
      y `TURN_STATES_AT_REST` lo trata como «espera a una persona, no a un proceso», pero ninguna
      política lo elige y ninguna ruta lo resuelve. Una sala `collaborate` con efectos debería poder
      pararse ahí; hoy pasa de `participants_selected` a `running` sin puerta.
- [ ] **`council_usage` no lo emite nadie.** El consumo sólo se ve preguntando por `/usage` o por
      `/state`; ninguna de las dos avisa sola, así que una sala que se está comiendo su presupuesto
      no lo dice hasta que alguien pregunta. Emitirlo al cerrar cada ronda es barato y es lo que
      hace que un tope sirva de algo antes de agotarse.
      (`council_activity_verified` **ya tiene emisor**: `_verify()` lo publica cuando `prove`
      devuelve `proved` y el ledger consiente — ver `FAUSTUS.md` §56.10.)

### La migración de los presets de Group Chat (P1)

- [ ] **Los grupos guardados no se convierten en salas.** `studio/src/adapters/group.ts` guarda
      `GroupPreset` —participantes con `modelId`, `endpointId`, `characterId`— en el almacén del
      usuario, y la pantalla `/group` sigue leyéndolos. Un `GroupPreset` es exactamente una lista de
      asientos sin roles ni presupuestos, así que la conversión es mecánica: modelo + endpoint +
      nombre de personaje → `ParticipantSpec` con `roles: []`, política `chat`, presupuestos por
      defecto. Lo que hay que decidir antes de escribirla es **qué pasa con el personaje**: un
      `characterId` es un prompt de sistema, y un asiento de consejo no tiene campo para eso hoy —
      `CouncilParticipant` lleva `agent_slug` y `completion_mode`, no un prompt suelto.
- [ ] **La pantalla `/group` no tiene fecha de retirada.** Group Chat es ahora la política `chat`, y
      mantener las dos pantallas indefinidamente es mantener dos respuestas a «¿dónde hablo con
      varios modelos?». El orden razonable es: convertir los presets, poner un aviso en `/group` que
      lleve a `/council`, y retirarla cuando la conversión esté probada — nunca antes, porque el
      transcript de un grupo vive en su sesión padre de Studio y no en `council.db`.

### `/api/tournament` sigue existiendo, y falta el camino (P1)

- [ ] **Hay dos maneras de pedir una ronda ciega.** `routes/tournament_routes.py` sigue en pie y
      `POST /api/council` con `policy: "tournament"` hace lo mismo por dentro —el consejo llama a
      `tournament.run`, no lo reimplementa—, pero con ledger, propietarios y cierre. Que el motor sea
      el mismo es lo correcto; que haya **dos APIs con dos vocabularios de resultado** es deuda: una
      contesta con el estado del torneo y la otra con un `CouncilSummary`. Lo que falta escribir es
      el camino de migración: qué hace `/api/tournament` cuando el consejo esté probado, si pasa a
      ser un atajo que abre una sala `tournament` o si se queda como API de bajo nivel y se dice en
      voz alta que la de alto nivel es el consejo. Decidirlo **antes** de que alguien construya una
      tercera cosa encima de la de bajo nivel.
- [ ] **La pantalla de Tournament vive dentro de `/agents?t=tournament`.** Si el torneo pasa a ser
      una política, esa pestaña debería abrir una sala en vez de una vista propia.

### La voz como canal de participación (P2)

- [ ] **Hablarle a la sala.** Faustus ya tiene STT y TTS (`routes/stt_routes.py`,
      `routes/tts_routes.py`). El caso que el plan apunta y nadie ha construido es el obvio en una
      sala de varios modelos: escuchar la deliberación mientras ocurre y **tomar la palabra por voz**
      — que es un `POST /{id}/messages` con el texto transcrito, y una lectura del transcript filtrada
      por autor. Lo que hay que resolver antes es la atribución: una transcripción es del usuario y
      tiene que entrar como `author_kind: "user"` aunque llegue por otro canal, exactamente por la
      misma razón por la que la respuesta de un par no entra como `role="user"` (`FAUSTUS.md` §56.2).
      Depende de la Fase 6 para cualquier cosa que no sea el micrófono local.

## State Mirror y Opportunity Engine — lo que queda (P1)

El plan 5 de 11 (`inspiration/PLAN_STATE_MIRROR_FAUSTUS.md`) está construido hasta la **fase 3**:
contratos y freshness (fase 0), estado interno con once adaptadores, persistencia, materialización,
consultas y cursor de cambios (fase 1), máquina local —git, servicios, modelos, hardware,
conexiones— (fase 2), y reconciliación con conflictos y proyecciones (fase 3). Está en
`FAUSTUS.md` §57; lo construido que no cuadra vive en `PENDIENTES.md`.

### Fases 5 a 7: el Opportunity Engine no existe (P1)

Nada de `src/opportunity_engine/` está escrito, y el plan es explícito en que debe llegar **después**
del read model y empezar **en sombra**. Lo que falta, en el orden en que el plan lo pide:

- [ ] **Detectores deterministas en modo sombra.** Los diez del §11.3: ejecución terminada con
      siguiente paso conocido, ejecución bloqueada por aprobación, objetivo cuyo bloqueo acaba de
      resolverse, cambios sin verificar, servicio necesario recuperado, artefacto maestro cambiado
      con derivados obsoletos, recurso escaso ocupado sin trabajo, job externo sin reconciliar,
      dependencia degradada, mantenimiento seguro pendiente. **Registrar qué habría propuesto sin
      emitir nada**, y medir duplicación y falsos positivos antes de que nadie vea una propuesta.
      Seis de los diez ya son consultables: `queries.running_work`, `blocked_work`,
      `pending_approvals` y `unverified_changes` responden hoy.
- [ ] **Presupuesto de atención.** Máximo por ventana, por proyecto y por tipo; horario silencioso;
      prioridad mínima; enfriamiento tras descartar; agrupación. Sin esto, un detector correcto es
      una fuente de ruido y se acaba silenciando entero.
- [ ] **Dedupe por revisión y evidencia, y feedback acotado** (`accepted`, `dismissed`, `snoozed`,
      `muted`). El §12 dice la regla difícil: *la ausencia de respuesta es una señal débil, no un
      rechazo*, y nunca se aprende a ocultar una alerta obligatoria por molesta.
- [ ] **Puente a la ejecución.** Aceptar una oportunidad **no ejecuta su texto**: revalida la
      revisión de estado, resuelve la capacidad por id, simula, crea el run por el ejecutor común,
      pide las aprobaciones y cierra con evidencia. Si el estado cambió desde la propuesta, se
      recalcula o caduca.

### Del propio State Mirror, lo que las fases cerradas no cierran (P1)

- [ ] **`GET /api/state/project` no existe, y Context Engine lo necesita.** `projection.project()`
      está escrito, probado y sin ruta: el §17 no lista ninguna, así que no se escribió. Es la
      pieza que convierte este subsistema en una fuente de contexto (`SOURCE_TYPES` del Context
      Engine ya reserva `"state"` para ella, y `ranking.py` ya sabe que una proyección de estado
      vale medio día). Falta la ruta y el `ContextSource` que la consuma.
- [ ] **El barrido no corre solo.** `agent_state_mirror_sweep_seconds` existe como ajuste y nada lo
      lee: hoy un barrido ocurre porque alguien pulsa «Reconcile» o llama a `/reconcile`. Falta
      engancharlo al supervisor de tareas (`src/task_supervisor.py`), con la condición del §2.4 —
      polling adaptativo, ceder bajo carga, y no mantener despiertos GPU ni discos.
- [ ] **`replay()`**: reconstruir el estado materializado desde el log append-only, o declarar
      incompatibilidad de schema. Es un criterio de aceptación del §26 y lo que haría del schema
      versionado una garantía en vez de una convención.
- [ ] **Resolver un conflicto.** Hoy sólo se puede abandonar. Ver `PENDIENTES.md`: hace falta quien
      decida, y `next_check` ya nombra qué lo zanjaría.
- [ ] **Namespaces aislados de verdad.** `branch:`, `simulation:` y `voice:` están en el contrato,
      el reductor se niega a mezclarlos y **nada escribe todavía en ellos**. Los llenarán Branching
      Futures (plan 10) y la voz (plan 11); la frontera ya está puesta para cuando lleguen.
- [ ] **Fuentes remotas (fase 7).** Webhooks firmados, cursores y reconciliación remota. Ninguna
      fuente de este build sale de la máquina, que es la decisión correcta para empezar.


## Universal Delta Engine — lo que queda (P1)

El plan 6 de 11 (`inspiration/PLAN_UNIVERSAL_DELTA_ENGINE_FAUSTUS.md`) está construido hasta la
**fase 2**: contrato común y modelo de confianza (fase 0), código con AST, símbolos, firmas, imports,
dependencias y configuración (fase 1), y workflows, skills y estado (fase 2). Está en `FAUSTUS.md`
§58; lo construido que no cuadra vive en `PENDIENTES.md`.

### Fases 3 a 5: documentos completos, imagen, audio y vídeo (P2)

El adaptador de documentos existe para texto, Markdown y JSON/YAML, y **deliberadamente** no toca
PDF ni render por página. Los de imagen, audio y vídeo no existen, y el orden importa: el plan dice
que los adaptadores probabilísticos sólo se abren «tras fijar honestidad/confidence», y esa fijación
—la escalera de tiers, los techos de confianza, el rechazo de `preserved` sin observación— es
justo lo que esta fase construyó. Ahora se pueden abrir.

- [ ] **Documentos, fase 3 completa.** Render por página, evidencia por span y por región, y las
      pruebas de reflujo y cambio de paginación sobre un PDF real. Lo que hay hoy compara estructura,
      cifras, citas y aritmética de tablas, que es lo que se puede hacer sin render.
- [ ] **Imagen, fase 4.** Metadatos, hashes, alineación geométrica, máscaras y regiones, color, y
      evaluadores de identidad y pose **bajo política**. Con overlays de evidencia como artefactos
      derivados con su hash y su retención, no como imágenes sueltas. El §11 del plan tiene la lista
      de honestidad que hay que respetar: la similitud perceptual no demuestra identidad, y un
      detector que no encuentra un objeto no prueba su ausencia.
- [ ] **Audio y vídeo, fase 5.** Transcripción, waveform, timestamps, planos y keyframes, con
      análisis adaptativo de los segmentos que cambiaron. Y la regla que el §12 subraya: **nunca
      afirmar preservación frame-perfect si sólo se muestreó**.

### Del propio motor, lo que las fases cerradas no cierran (P1)

- [ ] **Resolver revisiones de verdad.** `sources.py` no sabe abrir un `artifact`, un `document`, un
      `blob`, un `state`, un `workflow` ni un `skill`: devuelve `readable=False` con su motivo, que
      es correcto y limitante. Hoy esos dominios se comparan con `sources.stash()`. Falta el puente
      al Artifact Store (`src/artifact_identity.py`) y al espejo de estado.
- [ ] **`correlation_id`.** El campo existe y viaja; nadie lo crea. Decidir dónde nace y propagarlo,
      o quitarlo — un identificador que siempre vale `""` es peor que ninguno porque parece que
      funciona. Afecta también al consejo y al espejo, que ya lo llevan en su payload común.
- [ ] **Un ChangeSet al que apuntar.** Hoy un delta de código guarda `proof_ref` y pierde el
      ChangeSet que lo produjo. O se persiste el ChangeSet, o se ancla en `(workspace, checkpoint)` —
      que es durable mientras exista el repositorio en sombra, y `has_checkpoint()` existe justamente
      para distinguir «no cambió» de «ese sha ya no está».
- [ ] **Evidencia como artefacto.** Ningún adaptador emite `EvidenceRef` todavía. El §18 quiere
      overlays, spans y regiones **con hash y retención**, heredando la sensibilidad de la fuente.
      Es lo que convierte «confía en mí» en «míralo».
- [ ] **Cobertura de comportamiento.** Siempre 0.0 porque nadie entrega resultados de tests al
      adaptador. Conectarlo al arnés existente daría la única dimensión que hoy está declarada a cero
      en todos los deltas.
- [ ] **Los invariantes de `security` que sólo saben decir `unknown`.** `secrets_not_exposed` no
      corre ningún escáner de credenciales. Mientras no lo haga, contesta `unknown` — correcto, y
      poco útil. Hay detector de secretos en el repositorio desde el plan 1; conectarlo.
- [ ] **Más lenguajes en el adaptador de código.** Hoy el AST es sólo Python; el resto pasa por las
      regex de `repo_map.symbol_lines`, con tier `algorithm` declarado. El índice estructural del
      Context Engine (`code_index.py`) ya tiene el mismo problema y la misma solución pendiente.

### Fase 6: intención e invariantes avanzadas (P2)

- [ ] **Perfiles por operación** y tolerancias por dominio, más allá de los cuatro que hoy son
      descripción (`literal`, `default`, `greedy`, `maximalist`).
- [ ] **Interpretación asistida de la petición**, versionada y confirmada. Hoy `intent.compile` es
      determinista y todo lo que no sabe traducir va a `unknowns`, que es la decisión correcta para
      empezar: un contrato interpretado por un modelo contamina al árbitro. Lo que falta es el paso
      con confirmación humana, no el paso automático.
- [ ] **Benchmark multimodal y corpus con cambios sembrados** (§29). Sin él no hay forma de medir
      precisión y recall de los cambios semánticos, ni de saber cuántos «preserved» son falsos.

### Los consumidores que este motor estaba esperando (P1)

Delta Engine es la pieza de la que cuelgan cuatro de los cinco planes que quedan, y cada uno usa una
parte distinta:

- **Greedy Completion (plan 7)** — necesita `incidental` y `regression` para saber si seguir,
  corregir o parar.
- **Modo Enséñame (plan 8)** — relaciona acciones con efectos antes/después e infiere
  postcondiciones; el adaptador de estado es su base.
- **Immune System (plan 9)** — usa el diff de contratos de capacidad para canary y regresión.
- **Branching Futures (plan 10)** — compara cada rama contra la **misma** base y entre candidatas;
  el §1.6 avisa: nunca comparar ramas desde bases distintas sin declararlo como limitación material.

## Descartado a propósito (y por qué)

- Marketplace público de plugins **antes** de tener firma, permisos y revocación.
- Telefonía en vivo y bots en reuniones antes de la ingestión segura de grabaciones.
- Fleet multi-host antes de un nodo único emparejado, revocable y observable.
- Instalación automática de modelos, nodos de ComfyUI o paquetes desde lenguaje natural.
- Editor visual de workflows como requisito de la primera versión.
- Una memoria global que mezcle clientes y proyectos por comodidad de recuperación.
- El catálogo de 100 skills. Primero hay que poder **instalar, autorizar, ejecutar, cancelar,
  versionar y desinstalar una** de forma segura.

## Después del plan 7 (06-09-2026)

### Los planes que quedan, en orden

8. `PLAN_MODO_ENSENAME_FAUSTUS.md`
9. `PLAN_FAUSTUS_IMMUNE_SYSTEM.md`
10. `PLAN_BRANCHING_FUTURES_FAUSTUS.md`
11. `PLAN_VOZ_JARVIS_FAUSTUS.md`

### Lo que el Completion Engine deja preparado y sin usar

- **Ejecutar de verdad, no solo decidir.** Hoy el enganche del bucle propone y el modo sombra mide. El siguiente paso es dejar que un turno real ejecute una mejora del lote y la verifique, con `verification` como línea que nadie puede tocar. Es la mitad del motor que aún no ha corrido en producción.
- **Techos reales de presupuesto en el bucle.** Mientras `max_tool_calls` / `token_budget` / `wall_seconds` sean 0, `budget` no puede ser nunca un motivo de parada, y la distinción entre `budget` y `unfinished` — que es el motivo por el que existe este motor — solo funciona a medias.
- **La Opportunity Engine del §19.** Lo diferido se acumula (20 por turno en las pruebas) y hoy no va a ninguna parte: se cuenta y se olvida. El motor ya lo etiqueta bien (`round_full` vs `below_threshold`), que es la parte difícil.
- **Medir la sombra.** Con `shadow` de tres estados en la API y en la pantalla, ya se puede comparar "lo que habría hecho" contra "lo que hizo". Falta el informe que ponga los dos números juntos y responda si el modo greedy vale lo que cuesta.
- **Que el rechazo humano se propague.** `completion_refusals` es del motor. Un "no" a una mejora debería significar algo también para la Opportunity Engine y para el Immune System del plan 9, en vez de vivir en una tabla sola.
