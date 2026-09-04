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
| 1 · Ejecución segura y artefactos | `DockerWorkspaceBackend`, router, y que el código deje de correr en el proceso web | 🟡 04-09-2026 — el sandbox existe y está probado contra contenedores reales; **el agente todavía no pasa por él** (`FAUSTUS.md` §31) |
| 2 · Runtime de skills y memoria útil | Instalar/revertir capacidades sin tocar el core; alcances de memoria y `MemoryView` | ⏳ |
| 3 · Motor creativo | ComfyUI como servicio, plantillas versionadas, galería de artefactos con receta | ⏳ |
| 4 · Workflows, approvals y conectores | Procesos que sobreviven a reinicios, idempotentes | ⏳ |
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
- [ ] **Enrutar el agente por el backend.** `src/agent_tools/subprocess_tools.py`,
      `filesystem_tools.py` y los runs de coding siguen donde estaban. Es la parte arriesgada y
      necesita su propia preferencia experimental y su propia sesión.
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

## Fase 2 — runtime de skills y memoria útil (P0)

- [ ] `src/skills_runtime/` y `src/plugins/`: instalación, permisos, versión y reversión.
- [ ] **El puente que falta hoy:** `SKILL.md` con frontmatter ↔ `contracts.SkillManifest`. Ahora
      mismo el contrato valida manifiestos que ninguna skill real escribe.
- [ ] Descubrimiento explícito de `.odysseus/skills`, `.agents/skills` y `.claude/skills` hasta la
      raíz del repo — cargando instrucciones bajo demanda y **sin elevar permisos por procedencia**.
- [ ] Alcances `user/project/skill/run` en `memory_engine`, `memory_curator` y `services/memory/*`.
- [ ] `src/memory_view.py` sobre `contracts.MemoryView`, atado a `context_budget` y `context_ledger`.
- [ ] `creative-bible.md` y `technical-context.md` dentro de `.odysseus/`, sin exigir Git.

---

## Fase 3 — motor creativo (P0)

- [ ] ComfyUI como **servicio separado** (GPL-3.0: se integra por API, no se copia código) y
      `src/media_backends/comfyui.py` con submit/progreso/cancelación/artifacts.
- [ ] `config/media_workflows/` con plantillas versionadas — **nunca JSON arbitrario del agente**.
- [ ] `services/hwfit/` con perfiles de VRAM/GPU, colas y estimaciones honestas.
- [ ] Galería de artefactos con preview, receta, **licencia del modelo** y botón «variar/reproducir».

Skills iniciales: `image.product`, `image.reference-edit`, `video.short-form`, `video.subtitle`,
`audio.voiceover`, `document.report`.

---

## Fases 4–7 (resumen)

- **4 · Workflows:** `WorkflowRun`/`NodeRun`, reintentos, deduplicación, nodos `manual`, `schedule`,
  `webhook`, `skill`, `condition`, `wait`, `human_approval`, `artifact_store`, `deliver`. Los
  workflows guardan **IDs de conexión, nunca secretos**. No se avanza si al reiniciar se repite una
  publicación, un render o un email.
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
