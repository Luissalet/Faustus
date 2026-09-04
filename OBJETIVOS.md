# Objetivos — hacia dónde va Faustus

Compañero de `PENDIENTES.md`. Ese fichero dice **lo que está roto o sin verificar**; este dice
**lo que todavía no existe y en qué orden hay que construirlo**. Si algo aparece aquí como hecho,
tiene sección en `FAUSTUS.md` y tests que lo prueban; si no, no está hecho.

Fuente: `D:\LocalAI\inspiration\MASTERPLAN_FAUSTUS_MULTIPROPOSITO.md` (04-09-2026), síntesis de las
referencias de OpenHands, Letta, n8n, Aider, ComfyUI, OpenCode y OpenClaw.

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
| 3 · Motor creativo | ComfyUI como servicio, plantillas versionadas, galería de artefactos con receta | 🟡 04-09-2026 — cliente ComfyUI, plantillas aprobadas y renders durables con procedencia (§35). **No hay ComfyUI en esta máquina**: probado contra un servidor que imita su API. Faltan galería, hwfit y vídeo |
| 4 · Workflows, approvals y conectores | Procesos que sobreviven a reinicios, idempotentes | 🟡 04-09-2026 — aprobaciones con puerta humana (§33) **y el núcleo de workflows durables** (§34): claim antes de actuar, pausa por persona o por reloj, ramas. Faltan los conectores y cablear `skill`/`deliver` a algo que exista |
| 5 · Coding profesional | `ChangeSet`, repo map, LSP opcional, intents explore/plan/implement/review | 🟡 partes sueltas ya existen |
| 6 · Gateway, canales y dispositivos | Un canal de bajo riesgo con pairing, nodos, después voz | ⏳ |
| 7 · Ecosistema y operación | Hooks versionados, CLI `doctor`, telemetría, transferencia de artefactos | ⏳ |

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
      su campo. Dos plantillas de imagen que usan **solo nodos del core** de ComfyUI.
- [x] Renders durables: tabla `media_runs`, `poll()` que reconcilia **preguntando al motor** tras un
      reinicio, y un motor caído deja el run como estaba en vez de inventarle un fallo.
- [x] Procedencia completa en el artefacto: receta, versión, huella, semilla, motor, id del trabajo,
      modelo y **licencia**. El prompt **no** viaja: va un digest y una nota que apunta al run.
- [x] Rutas `/api/media/*` (sin ninguna que acepte un grafo, y hay test de eso) y 5 tools MCP.
- [ ] `services/hwfit/` con perfiles de VRAM/GPU, colas y estimaciones honestas. `rank_image_models`
      ya sabe decir «no cabe»; falta atarlo a una plantilla antes de encolar.
- [ ] Galería de artefactos con preview, receta, **licencia del modelo** y botón «variar/reproducir».
      Hoy los artefactos existen y nadie los enseña.
- [ ] La plantilla de vídeo: necesita custom nodes (AnimateDiff/SVD) que no se pueden probar sin
      tenerlos instalados, y una plantilla escrita a ciegas es peor que ninguna.
- [ ] **Nada de esto se ha ejecutado contra un ComfyUI real** — no hay uno en esta máquina. El
      protocolo está probado contra un servidor que imita su API.

Skills iniciales: `image.product` ✅, `image.reference-edit` ✅, `video.short-form`,
`video.subtitle`, `audio.voiceover`, `document.report`.

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
  - [ ] Conectar `deliver` a un canal real y `skill` al `execution_router` con workspace. Ahí es
        donde la Fase 4 se junta con la 1 y la 6.
  - [ ] Un planificador que llame a `advance()` en bucle (hoy lo llama quien quiera: la ruta, la
        tool MCP o una persona; nadie lo hace solo).
- **5 · Coding:** `ChangeSet` estándar (plan, ficheros, diff, comandos, tests reales, artefactos) y
  la unión de `auto_review`, `review_state`, `workspace_checkpoints` y `git_invariants`. Ninguna
  afirmación de arreglo termina sin diff y evidencia acorde al modo.
- **6 · Gateway:** orden prudente — canal de texto → pairing → nodos → companion → voz → widgets.
  `contracts.ExternalIdentity` ya define el binding, la revocación con motivo y las capacidades.
- **7 · Ecosistema:** hooks versionados sobre el bus de eventos, CLI `status/doctor/config validate`,
  OpenTelemetry, transferencia de artefactos con hash y expiración. Fleet solo después de un gateway
  unitario validado.

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

## Descartado a propósito (y por qué)

- Marketplace público de plugins **antes** de tener firma, permisos y revocación.
- Telefonía en vivo y bots en reuniones antes de la ingestión segura de grabaciones.
- Fleet multi-host antes de un nodo único emparejado, revocable y observable.
- Instalación automática de modelos, nodos de ComfyUI o paquetes desde lenguaje natural.
- Editor visual de workflows como requisito de la primera versión.
- Una memoria global que mezcle clientes y proyectos por comodidad de recuperación.
- El catálogo de 100 skills. Primero hay que poder **instalar, autorizar, ejecutar, cancelar,
  versionar y desinstalar una** de forma segura.
