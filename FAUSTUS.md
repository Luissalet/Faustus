# Faustus — qué añade este fork sobre Odysseus

**Faustus** es el fork personal de Luis de [Odysseus](https://github.com/odysseus-dev/odysseus) (interfaz local tipo Cowork sobre Ollama). Este documento es el registro vivo de **todo lo que Faustus añade o cambia respecto al Odysseus original**: se actualiza con cada bloque de trabajo, sirve de changelog del fork y de material para el currículum (qué se construyó, por qué, cómo se verificó).

- Base del fork: commit upstream `c9dd68d8` (27-08-2026, "refactor(docs): separate Pages site source").
- Ramas: `feat/projects` (principal, `D:\LocalAI\odysseus`) y `feat/reliability` (desarrollo, worktree `D:\LocalAI\odysseus-dev`, instancia de pruebas en el puerto 7001). La rama de desarrollo se fusiona en la principal por fast-forward.
- Cifras a 31-08-2026 (16:00): **83 commits**, ~180 ficheros tocados, **+19.000 líneas**; 20 módulos nuevos (12 de backend, 3 de rutas, 5 de frontend) + `scripts/faustus_rename.py`, 21 ficheros de tests nuevos. Suite completa: **5.926 tests en verde en Windows** (partía de 178 fallos ambientales) y 5.992 en Linux.
- Máquina de referencia: RTX 4070 Ti 12 GB, 128 GB RAM, Windows 11, Ollama 0.33.x; modelos `qwen3-coder:30b`, `qwen3.5:9b`, `qwen3.8:27b`, `qwen3-coder-next`.

---

## 1. Proyectos (29-08 → 31-08-2026)

Una capa de **Proyectos** sobre los chats, al estilo Cowork: cada proyecto vincula una carpeta de chats, un workspace (carpeta del disco), instrucciones propias y una memoria de ficheros.

- `services/projects.py` (modelo y persistencia en `data/projects.json`), `routes/project_routes.py` (API REST), `static/js/projects.js` (galería, editor, navegador de memoria en la barra lateral).
- Tres *hooks* en el flujo de chat: al crear un chat dentro de un proyecto hereda su workspace e instrucciones; el workspace del proyecto confina las herramientas de fichero; la memoria del proyecto se inyecta en el contexto.
- Botones de borrado de proyectos y chats con confirmación.
- 31-08: cada proyecto tiene además **mandos del agente**: workspace de confianza, modo propuesta, checkpoints on/off, tests del proyecto on/off, comando de tests, modelo revisor (§3.5–3.7) y una pestaña **Agent activity** (auditoría, §3.11).

## 2. Arnés de fiabilidad del agente (31-08-2026, madrugada)

Problema de partida: con modelos locales el agente **decía que había hecho cambios que no había hecho**, inventaba rutas de ficheros y se quedaba pensando 20 minutos. Se diagnosticó con logs y un banco de pruebas propio (`D:\LocalAI\agent-bench`) y se corrigieron las causas raíz, no los síntomas.

Causas encontradas y arregladas:
1. El selector de herramientas (tool-RAG) no entendía peticiones de código en español → el modelo no recibía `read_file/edit_file` y narraba. Heurística multilingüe + con workspace siempre hay herramientas de fichero/terminal.
2. Temperatura 1.0 en tareas de código → tope 0.4 en endpoints locales (`agent_local_temperature_cap`).
3. Qwen3-Coder emite llamadas a herramientas como texto (`<function=…>`) → parser propio.
4. `finish_reason=length` ignorado → continuación automática; límite de pasos → un ciclo extra automático.
5. **Ollama `/v1` ignora `think`, `top_k`, `repeat_penalty`, `num_ctx`** (verificado en 0.33.2) → reruteo transparente al `/api/chat` nativo cuando hace falta (streaming y no-streaming), gated por las capacidades que declara `/api/show`.
6. Nombres de herramienta inventados (`list`) terminaban el turno en silencio → ronda de corrección con los nombres reales.
7. Modelos densos "thinking" a 2 tok/s → watchdog de pensamiento (corta y reintenta sin thinking).
8. La ruta del chat descartaba los eventos `tool_progress` (fallo heredado) → cola de salida en vivo y tablero de sub-agentes.
9. Comandos que no terminan (un `uvicorn` en primer plano) → guard previo, watchdog de salida inactiva, muerte del árbol de procesos completo (`taskkill /T`, `killpg`).

Lo construido (`src/agent_harness.py` + `src/agent_loop.py`):
- **Libro de evidencia por turno** (`TurnLedger`): qué herramienta corrió, si falló, qué rutas tocó. Al terminar, el texto del modelo se contrasta con la evidencia: afirmaciones de cambios sin escrituras, ficheros inexistentes, "voy a…" sin hacer nada → **rechazo con mensaje `[Harness check]`** y otra ronda (máx. 2), después nota visible "no respaldado".
- Chequeo de sintaxis tras editar (py_compile / node --check / JSON) con ronda de arreglo.
- `read_file`/`edit_file` sobre rutas inexistentes devuelven los ficheros reales parecidos; `edit_file` sin coincidencia muestra las líneas más parecidas.
- Fichero **sustituido en silencio** (el usuario nombra un fichero que no existe y el modelo cambia otro sin decirlo) → ronda de honestidad obligatoria.
- Política para modelos locales (10 reglas): nombres exactos, preguntar (`ask_user`) ante ambigüedad, `edit_file` antes que reescribir ficheros enteros (las reescrituras que borran ≥5 líneas quedan anotadas).
- Tarjetas 🛡 **Turn summary** y **Verified / Unverified** en el chat, persistidas con el mensaje (`metrics.harness`).
- Panel **Progress** (`todowrite`) con marcas *unverified* / *no write* por objetivo.
- **Controles del modelo** por chat (temperatura, max tokens, top-p, thinking; `/temp`, `/maxtokens`, `/topp`, `/think`, `/gen`) y pill con tok/s.
- Widget de **uso en tiempo real** (GPU %, VRAM, temperatura, modelo cargado y reparto GPU/CPU, RAM; `GET /api/system/usage`).
- **Sub-agentes** (`delegate_agents`, `/agents a | b | c`): cada tarea en un chat hijo con su propio arnés, tablero en vivo, informe basado en evidencia.
- **Chats en segundo plano**: un turno sigue en el servidor al cambiar de chat o recargar; re-enganche con la línea de tiempo completa; puntos de estado en la barra lateral (trabajando / terminado sin leer / esperando aprobación); *Stop run* desde el menú.
- **Visor de ficheros editados**: chips por fichero → panel con contenido y diff, copiar, mostrar en carpeta, abrir en editor, revertir (uno o todos).
- Banco de pruebas propio (`agent-bench/`: `run_task.py`, `run_matrix.py`, demo-app con tests) para medir modelos y regresiones por API real.

Resultados del banco (qwen3-coder:30b): t1 botones de borrado 261 s / 3 ficheros verificados; t8 petición ambigua → pregunta en 27 s en vez de inventar un arreglo; t4 fichero inexistente → pregunta o concluye que no hay bug; multiagente t3: 3 ficheros, verificado, ~2,5 min.

## 3. Verificación funcional y autonomía con red (31-08-2026, tarde) — las 12 funciones

Segunda tanda, a partir de una lista priorizada de límites del arnés ("verifica afirmaciones, no que el cambio funcione", "los turnos en segundo plano se pierden al reiniciar", "revertir depende de git", "180 tests ambientales fallan en Windows"…).

### 3.1 Tests del proyecto tras cada turno con cambios — `src/project_tests.py`
Detecta el runner (comando del proyecto → pytest → npm test → cargo/go/make), lo ejecuta acotado (timeout, sin entrada interactiva, salida capada, **muerte del árbol de procesos** al agotar el tiempo) y devuelve un veredicto estructurado. Con pytest ejecuta solo los tests **relacionados** con los ficheros cambiados (por nombre y por `import` del módulo). Si fallan: **una ronda de arreglo** con el fallo real; el resultado sale en la tarjeta Verified ("tests passed · 2 passed") y se persiste. Ajustes `agent_project_tests`, `agent_project_test_command`, `agent_project_tests_scope`, `agent_project_tests_timeout_seconds`, `agent_project_tests_fix_round`.

### 3.2 Checkpoints automáticos y "volver a antes de este turno" — `src/workspace_checkpoints.py`
Un **repositorio git sombra** por workspace (git-dir en `data/checkpoints/…`, work-tree = la carpeta del usuario; nunca toca el `.git` del usuario, funciona en carpetas sin git). Antes de la primera escritura de cada turno se hace un snapshot; el turno guarda su `sha`. Con eso: **diff por fichero respecto al inicio del turno**, contenido previo de cualquier fichero, **Restore to before this turn** (borra lo creado, restaura lo modificado) y **Commit these changes…** en el git del usuario con un mensaje propuesto y solo los ficheros del turno. Excluye carpetas vendored y ficheros grandes; tope de tamaño del repo sombra; bloqueo por workspace. Rutas `/api/workspace/checkpoint/{status,changes,file,restore,list,reset}`, `/api/workspace/commit/proposal`, `POST /api/workspace/commit`.

### 3.3 Instrucciones del proyecto — `src/project_instructions.py`
`AGENTS.md` / `CLAUDE.md` / `.odysseus/INSTRUCTIONS.md` / `.cursorrules` / … del workspace se inyectan en el **prompt de sistema** de cada turno (caché por mtime, tope de tamaño con aviso de truncado).

### 3.4 Mapa del repositorio — `src/repo_map.py`
Estilo Aider: árbol compacto + símbolos de nivel superior (Python por `ast`; JS/TS/Go/Rust/Java/C#/Ruby/PHP por regex) de los ficheros más relevantes para la petición, con presupuesto de tokens (`agent_repo_map_tokens`). Se inyecta una vez por turno como dato de referencia (no dispara la puerta de aprobación). Menos rondas de `glob/grep`, menos rutas inventadas.

### 3.5 Workspace de confianza — `src/tool_capabilities.py`
Un proyecto marcado *trusted* deja pasar sin aprobación las **escrituras de fichero cuyo destino resuelve dentro de su carpeta**; `bash`, borrados (`apply_patch` *Delete File*), rutas fuera de la carpeta y cualquier otro efecto siguen pidiendo aprobación. `delegate_agents` tiene su propia opción.

### 3.6 Modo propuesta → aplicar — `services/review_state.py`
Con *review mode* los cambios se hacen sobre el checkpoint y quedan **pendientes por fichero**: en el visor se acepta o se rechaza (rechazar = restaurar ese fichero al checkpoint). `GET /api/workspace/review/{message_id}`, `POST …/decide`.

### 3.7 Auto-revisión del diff — `src/auto_review.py`
Tras el turno, un segundo pase **sin herramientas** (mismo modelo u otro: `agent_auto_review = off | same | <modelo>`, o `review_model` del proyecto) lee solo la petición y el diff del turno y devuelve JSON con hallazgos; los de severidad `error` disparan **una ronda de arreglo**. Tarjeta *Review* en el chat. Errores encontrados y corregidos en vivo: `max_retries=0` no hacía ninguna llamada; el pase como trabajo *background* se quedaba esperando detrás de la puerta de modelo local (el propio turno la mantenía ocupada); el modo thinking de qwen3.5 se comía el presupuesto de tokens por `/v1`.

### 3.8 Sub-agentes v2 — `src/agent_tools/subagent_tools.py`
**Ficheros exclusivos por worker** (declarados `[a.py, b.py]` o "primero que escribe, dueño"): una escritura sobre un fichero de otro worker la **rechaza el despachador** con un error accionable. **Worker revisor** opcional al final (con permiso para leer todo). **Parar un worker** desde la UI (`POST /api/chat/subagent/stop/{child}`), re-lanzar. Sintaxis `/agents [--review] [--serial] [ficheros] {modelo} tarea | tarea`.

### 3.9 Scorecard de modelos — `src/scorecard.py`, `routes/scorecard_routes.py`
Una línea JSONL por turno de agente (modelo, duración, rondas, llamadas, verificado, preguntó, tests, revisión, tok/s). `/scorecard` en el chat pinta la tabla por modelo (tasa de verificación, preguntas, tests OK, revisión OK, mediana de tiempo…). `GET /api/scorecard`, `GET /api/scorecard/table`.

### 3.10 Runs persistentes y cola de GPU — `src/agent_runs.py`
Cada run detached escribe un **log de replay en disco**; al arrancar, los runs que un reinicio cortó se recuperan como mensaje parcial marcado y se avisa (`/api/chat/activity.interrupted`, toast). **Cola FIFO por carril** (la GPU local sirve un turno a la vez): el segundo chat ve su posición en vivo (`queue_status`), *Stop* funciona también en cola.

### 3.11 Auditoría por proyecto — `src/project_audit.py`
Cada turno con cambios se registra (`data/audit/<workspace>.jsonl`): ficheros, modelo, chat y mensaje, checkpoint, tests, revisión. Pestaña **Agent activity** del proyecto con salto al turno; índice por fichero. `GET/DELETE /api/projects/{id}/audit`.

### 3.12 Arnés de tests en Windows + e2e Playwright
La suite pasó de **178 fallos ambientales a 0** en la máquina de Luis: UTF-8 en subprocesos node/python (`PYTHONUTF8`, encodings explícitos), URIs `file://` para módulos ESM, rutas temporales de la plataforma, `bash` resuelto a Git Bash bajo pytest (CreateProcess encuentra antes el stub de WSL de System32), tests POSIX-only marcados con `skip` razonado, ficheros de objetos git de solo lectura al borrar repos sombra… Además, **tests e2e con Playwright** (`tests/e2e/`, `ODYSSEUS_E2E=1`): servidor real + endpoint de modelo falso guionizado; flujos aprobación → checkpoint → tests → tarjeta Verified → visor con diff → restaurar; run en segundo plano al cambiar de chat; segundo chat esperando en la cola.

### Resultados en vivo (qwen3.5:9b en la 4070 Ti, 31-08 tarde)
| escenario | resultado |
|---|---|
| t6 (contador + endpoint + todowrite) | 50–63 s, 3 ficheros, checkpoint → tests "1 passed" → revisión → **verified** |
| t7 (renombrar campo; un test lo captura) | 36 s: `tests_failed` (1 failed) → ronda de arreglo → "2 passed" → revisión ok → **verified** |
| t3 v2 (2 workers con ficheros propios + revisor) | 78 s: workers 34 s y 29 s en paralelo, revisor 13 s sin cambios, tests ok, **verified** |
| reinicio con un run en marcha | recuperado al arrancar: mensaje parcial guardado, marcado `interrupted`, `ack` desde la API |

Para comparar: qwen3-coder:30b hacía t6 en 133–279 s. `qwen3.5:9b` (7,5 GB en VRAM, ~30 tok/s) quedó como modelo de pruebas del agente.

## 4. Segunda pasada de la tarde (31-08-2026, 15:15–16:30): más verificación, más tests

- **Revisión con evidencia** (`src/auto_review.py`): cada hallazgo del revisor debe traer una línea copiada del diff (o, si falta trabajo, las palabras exactas de la petición). Lo que no se localiza en el diff queda como aviso y **nunca cuesta una ronda de arreglo**; si nada se localiza, el veredicto pasa a "ok" con nota. Si tras la ronda de arreglo el agente no cambió nada, la revisión queda marcada **disputed** (el agente miró y no estuvo de acuerdo) en vez de un aviso rojo de "defectos". Motivo: qwen3.5:9b se inventó un botón "colocado después" y discutió consigo mismo dentro del hallazgo.
- **Tests comparados con el checkpoint** (`src/project_tests.py`, `workspace_checkpoints.export_tree`): cuando los tests fallan tras el turno, los mismos ficheros de test se ejecutan sobre una exportación del árbol del checkpoint (`git archive` del repo sombra, sin `-x`). Cada fallo queda clasificado como **nuevo** o **preexistente**; el mensaje de la ronda de arreglo lo dice; y si todos los fallos son preexistentes en tests que no están ligados por nombre a los ficheros cambiados (el test roto de otro), el turno **no gasta ronda de arreglo** y la tarjeta dice "ya fallaba antes de este cambio". Un test ligado por nombre (`test_calc.py` al tocar `calc.py`) sigue mereciendo la ronda: puede ser justo lo que se pidió arreglar. Setting `agent_project_tests_baseline`.
- **`/agentsmd [write]`** (`POST /api/workspace/instructions/draft`): borrador de `AGENTS.md` para el workspace con lo que el runtime ya detecta (lenguajes, estructura, manifiestos, comando de tests) y las convenciones que un modelo local necesita explícitas; nunca sobreescribe.
- **`scripts/faustus_rename.py`** (`--check`): vuelve a aplicar la marca visible tras un merge del proyecto original; tests que fijan la marca en la UI, los identificadores intactos y la idempotencia del script.
- Scripts `.bat` del PC renombrados (`Start/Stop/Restart-Faustus.bat`, los antiguos como atajos).
- Cifras tras esta pasada: suite Linux **5992 passed**; en vivo (qwen3.5:9b): t7 con fallo nuevo clasificado contra el checkpoint → ronda de arreglo → verified en 51 s.

## 5. Renombrado a Faustus (31-08-2026)
El nombre visible de la aplicación pasa de Odysseus a **Faustus** (interfaz, título y manifest, login, notificaciones, identidad en el prompt del modelo, correos, scripts del PC). Los identificadores internos (variables `ODYSSEUS_*`, claves de `localStorage`, ids/clases CSS, nombres de módulos, carpetas `D:\LocalAI\odysseus*`) se conservan a propósito: no se rompen los datos ni el venv y el fork puede seguir recibiendo cambios del proyecto original.

---

## Cómo mantener este documento
Cada bloque de trabajo añade una sección (fecha, qué, por qué, ficheros, cómo se verificó, cifras) y actualiza las cifras de cabecera (`git log --oneline c9dd68d8..HEAD | wc -l`, `git diff --stat c9dd68d8..HEAD`). Los commits del fork llevan mensajes largos que explican el porqué: `git log c9dd68d8..HEAD` es la fuente detallada.
