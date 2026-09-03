# Faustus — qué añade este fork sobre Odysseus

**Faustus** es el fork personal de Luis de [Odysseus](https://github.com/odysseus-dev/odysseus) (interfaz local tipo Cowork sobre Ollama). Este documento es el registro vivo de **todo lo que Faustus añade o cambia respecto al Odysseus original**: se actualiza con cada bloque de trabajo, sirve de changelog del fork y de material para el currículum (qué se construyó, por qué, cómo se verificó).

- Base del fork: commit upstream `c9dd68d8` (27-08-2026, "refactor(docs): separate Pages site source").
- Rama: **una sola, `master`** (`D:\LocalAI\odysseus`), que trackea `origin/master` en `github.com/Luissalet/Faustus`. Las ramas `feat/projects` y `feat/reliability` y la worktree de pruebas se consolidaron el 31-08.
- Cifras a 03-09-2026 (19:00, en `master`): **298 commits**, +112.000 líneas sobre la base; **97 módulos nuevos** en `src/`, `routes/`, `services/` y `static/js/`, **168 ficheros de tests nuevos**. Suite completa: **9.100 tests en verde**, ~6 min en Linux (2 fallos preexistentes del entorno: `markitdown` sin conversor docx y el escáner de marca sobre un docstring en español); e2e Playwright 12 flujos. En Windows hay además 12 fallos de plataforma y 13 dependientes del `data/` local, todos presentes también en el commit base (§24.4).
- Máquina de referencia: RTX 4070 Ti 12 GB **+ RTX 5060 Ti 16 GB (eGPU, desde el 02-09)**, 128 GB RAM, Windows 11, Ollama 0.33.x; modelos `qwen3-coder:30b`, `qwen3.5:9b` (visión), `qwen3.8:27b`, `qwen3-coder-next`.

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
- **`/scorecard here`**: la tabla por modelo filtrada al workspace vinculado; las paradas en la puerta de aprobación ya no cuentan como turnos ni como "preguntas" (inflaban ambos).
- **`/checkpoints [n | reset]`**: los últimos checkpoints del workspace con "qué difiere ahora" y **Restore here** (volver varios turnos atrás sin el git del usuario).
- Cifras tras esta pasada: suite Linux **5992 passed**, Windows **5939 passed / 0 failed**, e2e Playwright **3/3 en el PC**. En vivo (qwen3.5:9b): t7 con fallo nuevo clasificado contra el checkpoint → ronda de arreglo → verified en 51 s; t6 con un test roto preexistente → sin ronda de arreglo, verified en 68 s; matriz t1/t4/t5/t8: 59 s / 12 s / 3 s / 102 s. Scorecard real del día (`/scorecard`), 13 turnos de qwen3.5:9b: 100 % verificados, 0 % preguntas (en t8 —petición ambigua— este modelo arregla en vez de preguntar; qwen3-coder:30b preguntaba), tests OK 91 % (11 ejecuciones), revisión OK 44 % (9, antes del filtro de evidencia), mediana 40 s, 29 tok/s.

## 5. Renombrado a Faustus (31-08-2026)
El nombre visible de la aplicación pasa de Odysseus a **Faustus** (interfaz, título y manifest, login, notificaciones, identidad en el prompt del modelo, correos, scripts del PC). Los identificadores internos (variables `ODYSSEUS_*`, claves de `localStorage`, ids/clases CSS, nombres de módulos, carpetas `D:\LocalAI\odysseus*`) se conservan a propósito: no se rompen los datos ni el venv y el fork puede seguir recibiendo cambios del proyecto original.

## 6. Atajos del compositor: menciones `@` de ficheros y `#` para recordar (31-08-2026, tarde-noche)

Auditoría comparando Faustus con los workspaces de Claude (Code/Cowork) y ChatGPT. La mayoría de lo que tienen ya estaba: modo plan, cola de mensajes mientras el agente trabaja, editar/regenerar/bifurcar un mensaje, buscador de chats con Ctrl+K, ejecución de código con vista previa HTML, presets, tareas programadas, memoria, comparador de modelos, atajos de teclado, exportar. Faltaban dos atajos del compositor, y los dos atacan justo el punto débil de un modelo local pequeño: **decirle exactamente de qué fichero hablas** y **no tener que repetirle las mismas reglas**.

### 6.1 Menciones `@` de ficheros del workspace

Escribir `@` en el compositor abre un buscador difuso de los ficheros del workspace; Tab o Enter inserta la ruta relativa. Es el `@` de Claude Code y Cursor (el `#` de ChatGPT).

- **`src/file_mentions.py`** (nuevo): ranking difuso (coincidencia exacta del nombre > prefijo > subcadena > subsecuencia estilo fzf, con penalización por profundidad, tests y código vendorizado), extracción de las menciones del texto enviado (`@ruta` y `@"ruta con espacios"`, sin confundir correos), y resolución contra el índice de ficheros ya cacheado: ruta relativa exacta → sin distinguir mayúsculas → nombre de fichero único. **Un nombre ambiguo no se adivina**: se devuelve como `ambiguous` y el modelo tiene que preguntar — adivinar es exactamente el fallo de sustitución que el arnés persigue desde la mañana.
- El bloque que se inyecta antes del mensaje del usuario dice que esos son los ficheros exactos, y **mete el contenido de los pequeños** (presupuesto `agent_file_mention_inline_chars`, 6000 por defecto): a 30 tok/s eso ahorra una ronda entera de `read_file` (~30 s). Los que no caben se listan con "read_file it" en vez de meter un trozo inútil.
- **Ficheros con pinta de secreto** (`.env`, `.env.local`, `.netrc`, `.npmrc`, `id_rsa`, `*.pem/.key/.p12`, `secrets.yaml`, `credentials`) **se nombran pero nunca se pegan** en el prompt: el modelo sabe de qué fichero hablas y puede leerlo con `read_file` si de verdad lo necesita, pero el secreto no viaja (ni acaba en los logs de la petición) solo por haberlo nombrado. Leer un secreto debe ser un acto deliberado, no un efecto secundario.
- Un fallo propio, encontrado auditando esto: `lstrip("./")` quita un **conjunto de caracteres**, no un prefijo, así que `@.env` se convertía en `env` y se reportaba como "no existe". Todos los dotfiles estaban rotos. Arreglado con un helper que quita `./` como prefijo, y tests.
- Las menciones que no existen y las ambiguas se le dicen al modelo explícitamente ("di que no existe en vez de editar otro fichero").
- `GET /api/workspace/files?workspace=&q=&limit=` (solo admin, como `/browse`: enumera rutas del host). Lee el índice cacheado, así que cada pulsación cuesta una ordenación, no un `os.walk`.
- **`static/js/fileMentions.js`** (nuevo): el popup, con *debounce* de 90 ms, caché por consulta, navegación con flechas, y `keydown` en captura para ganarle el Enter al botón de enviar. Reutiliza el CSS del popup de comandos.
- **Bug preexistente encontrado y arreglado**: `extract_path_tokens()` devolvía `@src/app.py` con la arroba incluida, así que **cualquier** ruta escrita con `@` (no solo las de esta función) no casaba con el índice del workspace, se contaba como "fichero que el usuario nombró y no existe" y podía disparar la ronda `target_substituted` sin motivo. Ahora se quita la arroba inicial. Dos tests de regresión.

### 6.2 `#` (y `/remember`) — añadir una regla a las instrucciones del proyecto

Empezar un mensaje con `#` guarda esa línea como regla permanente en el fichero de instrucciones del proyecto (`AGENTS.md`, `CLAUDE.md`… el que ya use, y si no hay ninguno crea `AGENTS.md`), que el runtime inyecta en el prompt de todos los turnos siguientes. Es el `#` de Claude Code, y encaja con el `/agentsmd` de la pasada anterior.

- **`project_instructions.remember()`**: normaliza la regla (quita la almohadilla, marcadores de lista y saltos de línea; tope 500 caracteres), la añade como viñeta bajo `## Notes added from chat` creando la sección si falta, **la inserta antes de la siguiente sección** en vez de al final del fichero, **conserva CRLF** si el fichero lo usa, y **no duplica** una regla que ya está (lo dice). Nunca reescribe lo que había.
- `POST /api/workspace/instructions/remember` (solo admin) y el comando `/remember <regla>` (alias `/recuerda`).
- **`static/js/composerSigils.js`** (nuevo, sin DOM para poder testearlo): `isMemoryLine()` decide qué es una línea de memoria — **una sola línea, una sola almohadilla, y con workspace vinculado**. `##` sigue siendo un encabezado de Markdown y un mensaje de varias líneas se envía normal: secuestrar cualquiera de los dos convertiría `#` en una trampa.

### 6.3 Versiones del chat: deshacer lo que borra una edición

Editar un mensaje (o "regenerar desde aquí") **truncaba** el chat: todo lo que venía después se borraba de la base de datos y no volvía. Claude y ChatGPT guardan la rama anterior y te dejan alternar entre versiones; aquí importa más, porque una respuesta puede ser veinte minutos de un modelo local.

- **`src/chat_versions.py`** (nuevo): antes de truncar, la cola que se va a borrar se guarda aparte en `DATA_DIR/chat_versions/<sesión>.json` — fichero por sesión como `agent_runs`, **sin tocar el esquema de la base de datos**: nada que migrar, y un fichero corrupto cuesta historial, nunca el chat. Poda por número (`chat_versions_keep`, 10), por antigüedad (`chat_versions_keep_hours`, una semana) y por tamaño. Los resúmenes no llevan los mensajes; la vista previa es **la respuesta**, no la pregunta.
- **Restaurar es simétrico**: al recuperar una versión, la cola actual se guarda como versión antes de reemplazarla, así se puede ir y volver. Si no lo fuera, la función solo cambiaría qué respuesta pierdes.
- `POST /api/session/{sid}/truncate` captura la cola y devuelve el resumen; `GET/POST/DELETE …/versions` listan, restauran y olvidan. **La captura nunca bloquea el truncado**: si falla, se registra y el truncado sigue (una red de seguridad que puede romper la caída que está amortiguando es peor que ninguna).
- En el frontend, los **tres** sitios que truncaban (editar, regenerar, regenerar-variante) pasan ahora por `_truncateWithVersion()`, que muestra un aviso con **Undo** (restaura al momento) y el comando **`/versions [n | clear]`** lista las versiones con su botón *Restore*, al estilo de `/checkpoints`.

### 6.4 Citar una selección

Seleccionar texto dentro de un mensaje ofrece un botón **❝ Quote** que deja el pasaje en el compositor como cita Markdown. Lo tienen Claude y ChatGPT, y aquí arregla la continuación más habitual de una respuesta larga del agente: "esta parte, explícala/rehazla". Sin ello se reescribe la frase a mano, o se dice "el tercer punto" y un modelo de 9B adivina cuál era.

- **`static/js/quoteSelection.js`** (nuevo): `blockquote()` y `withQuote()` son funciones puras (testeables sin DOM). Las líneas en blanco conservan su `>` para que la cita sea **un solo bloque** en cualquier renderizador; el corte por longitud (700 caracteres) respeta la palabra, salvo cuando eso dejaría la cita en nada (una línea minificada, un token larguísimo), donde cae al corte duro; un borrador que ya hubiera en el compositor se conserva **debajo** de la cita, que es donde va la pregunta.
- El botón se activa en `mousedown`, no en `click`: un `click` borraría la selección antes de leerla.

### 6.5 Las menciones enviadas se ven y se abren

En el mensaje ya enviado, cada `@ruta` se convierte en una **ficha pulsable** que abre ese fichero en el visor lateral (`static/js/mentionChips.js`). Cierra el círculo del selector: después de enviar se ve de un vistazo a qué ficheros apuntó el turno, y se puede comprobar uno sin salir del chat. Es puramente cosmético — el servidor resuelve las menciones del texto igual.

- Se decoran **solo los mensajes del usuario** (que una respuesta cite `@x.py` es texto del modelo, no una ruta que el usuario señaló) y nunca dentro de `code`, `pre` o enlaces. Un `MutationObserver` sobre `#chat-history` cubre streaming, carga de historial y cambio de sesión sin engancharse a cada ruta de render.
- El test compara la expresión regular del JS con `file_mentions.extract()` sobre los mismos casos: si divergen, aparecería una ficha donde el servidor no ve mención (o al revés), justo donde la función intenta ganarse la confianza. Y comprueba que recomponer las partes devuelve el mensaje **idéntico**.

### 6.6 Verificación en navegador (e2e) y un fallo que solo aparece ahí

`tests/e2e/test_composer_shortcuts.py` (nuevo, 5 flujos Playwright): el popup de `@` se abre y Enter inserta la ruta **sin enviar el mensaje**; `@` encuentra un fichero anidado y Escape cierra el popup; `#` escribe la regla en AGENTS.md y repetirla dice "Already in" sin duplicar; y el flujo completo de versiones — turno, editar el mensaje (la primera respuesta desaparece), `/versions`, *Restore*, y la primera respuesta vuelve; y la ficha de una mención enviada abre `calc.py` en el visor.

El primer flujo **falló al escribirlo**, y por un fallo real: `initFileMentions` se engancha desde un `import()` dinámico, así que quien escribe en una página recién cargada (o pega un borrador) ya tiene texto en el compositor cuando se enganchan los escuchadores, y el popup no se abría hasta la siguiente tecla — "la arroba no hace nada". Arreglado con un `refresh()` inicial y un escuchador de `focus`.

### 6.7 Lo que cambia en vivo (qwen3.5:9b, instancia dev 7001)

Dos pruebas contra el modelo real, sobre `agent-bench/demo-app`:

- **t9a — mencionar el fichero correcto** (`En @static/js/sessions.js cambia el texto del botón de borrar…`): **22 s, 2 rondas, 2 herramientas**, editó exactamente `sessions.js` y nada más, verificado. **Cero rondas de exploración**: no hubo `ls`, `glob` ni `grep`, porque el fichero venía ya en el contexto.
- **t9b — mencionar un fichero que no existe** (`Arregla el bug de @static/js/cards.js…`): es literalmente el fallo t4 de la mañana, en el que el modelo metía un arreglo especulativo en `projects.js` y lo presentaba como la corrección. Ahora, en **8 s, cero herramientas y cero cambios**: *"El archivo `@static/js/cards.js` no existe en este workspace… ¿Estás buscando en `projects.js` la función que renderiza las tarjetas?"*, con la lista de los ficheros JS que sí existen. El arnés ya no tiene que corregir nada a posteriori porque el error no llega a ocurrir.

### 6.8 Un hallazgo de la auditoría: 28 herramientas de navegador por una falsa alarma

Comprobando en vivo que una regla escrita con `#` llega al prompt (llega: `project_instructions.block()` la inyecta, y el modelo de 9B simplemente la ignoró), salió a la luz por qué la ignoró. La petición *"Añade a server.py una función health() que devuelva {"status": "ok"}"* salió con **75 herramientas y ~38.000 tokens de prompt**, frente a las 31 y ~12.000 de la misma tarea con una mención `@`.

Causa: el índice semántico de herramientas emparejó *"health"* / *"status ok"* con `browser_console_messages` (y con el dominio "cookbook" de servir modelos). Ese único acierto marginal disparaba `_expand_browser_mcp_tools()`, que **añade las 28 herramientas del navegador Playwright** — pensado para cuando la ruta declara intención de navegador, no para un vecino semántico. Resultado: 10,8 s hasta el primer token y las reglas del propio AGENTS.md diluidas al 0,1 % del prompt.

Arreglo (`_browser_intent_is_real()`): se expande cuando la ruta nombra el servidor (`builtin_browser`, la vía prevista), cuando aparece una herramienta que **abre sesión** (`navigate`, `tabs`, `snapshot`) o cuando hay **dos o más** herramientas de navegador. Un solo acierto periférico se conserva tal cual, sin expandir — `browser_console_messages` sin navegador abierto no sirve de nada, pero costaba 28 esquemas. 9 tests.

Medido en la misma tarea antes y después (qwen3.5:9b, instancia dev):

| | antes (t10) | solo navegador (t10b) | los dos (t10c) |
|---|---|---|---|
| herramientas enviadas | 75 | 46 | **24** |
| tokens de entrada (ronda 1) | 38.176 | 29.190 | **20.174** |
| tiempo hasta el primer token | 10,8 s | 4,8 s | 7,7 s (caché fría tras reiniciar) |

**−68 % de herramientas y −47 % de prompt en la misma petición**, con el mismo resultado: `server.py` editado y verificado. El tiempo hasta el primer token de t10c no es comparable (la instancia acababa de reiniciarse, caché KV vacía); el recuento de tokens sí lo es.

Dos honestidades sobre esta prueba: (1) las 46 restantes incluían otro dominio equivocado, y la causa resultó ser más tonta de lo esperado: el clasificador de Cookbook (servir modelos) emparejaba la palabra suelta **`server`**, y la petición decía **`server.py`**. `server.py`, `server.js` y `server.ts` están entre los nombres de fichero más comunes que hay, así que ese falso positivo saltaba constantemente en peticiones de código normales y añadía las 13 herramientas de servir modelos. Arreglado con `server(?!\.\w)` en el clasificador y en la regex de contexto de continuación, con 4 tests que comprueban las dos direcciones (`arregla server.js` no es Cookbook; "what's running on the server", "gpu box", "serving" siguen siéndolo). (2) La regla escrita con `#` **sí llegaba al prompt** (`project_instructions.block()` la inyecta, verificado a mano: 355 caracteres) y el modelo de 9B la ignoró igualmente en ambas pasadas. El atajo `#` hace su trabajo; que un modelo pequeño obedezca una regla de estilo enterrada en 30.000 tokens es otra cosa.

### 6.9 Regresión del bucle del agente

Matriz habitual (`run_matrix_q35.ps1`, qwen3.5:9b, tras el arreglo del punto 6.8), con las cifras de la tanda anterior entre paréntesis: t1 regresión de botones **67 s** (59 s), t4 ruta inventada por el usuario **94 s** (12 s), t5 solo lectura **5 s** (3 s), t8 petición ambigua **65 s** (102 s). Las cuatro terminan en `complete`, **0 llamadas fallidas**. La t4 tarda más porque esta vez exploró de verdad (16 llamadas) en lugar de concluir a la primera que el fallo no existía; es una tarea que este modelo resuelve de forma distinta en cada pasada, así que se anota tal cual y no como mejora ni como regresión.

### Verificación
**93 tests nuevos**: 86 de unidad en 10 ficheros (`test_file_mentions.py`, `test_file_mentions_routes_js.py`, `test_project_instructions_remember.py`, `test_composer_sigils_js.py`, `test_chat_versions.py`, `test_chat_versions_routes.py`, `test_quote_selection_js.py`, `test_mention_chips_js.py`, `test_browser_mcp_expansion.py`, `test_cookbook_domain_false_positive.py`), 2 de regresión añadidos a `test_agent_harness.py`, y **5 flujos e2e** en `tests/e2e/test_composer_shortcuts.py`. Entre ellos, los de contrato que impiden que las tres capas se separen: lo que inserta el popup de `@` es lo que resuelve el servidor; la regex de las fichas del transcript se compara con `file_mentions.extract()` sobre los mismos casos; y `resendUserMessage` no puede volver a llamar a `/truncate` directamente sin pasar por la captura de versiones.

Suites completas en verde antes y después: **Windows 6.028 / 0 fallos** (partía de 5.940), **Linux 6.081 / 0** (partía de 5.993), **e2e Playwright 10/10** en las dos plataformas (~80 s).

Dos avisos aprendidos por el camino: (1) el guardián de regresión `test_resend_message_nondestructive.py` comprobaba que la URL de `/truncate` aparece tras la guarda `replaceFromHere`; al pasar por `_truncateWithVersion()` hubo que actualizarlo para vigilar la llamada nueva. (2) Un test de rutas con `SessionManager` real obliga a recargar `core.database` (liga su engine al importar), y dejarlo recargado le pasa su base de datos temporal a todos los tests posteriores — 10 fallos por orden. Se sustituyó por un doble sobre una lista; la capa real la cubre el flujo e2e.

---

---

## 7. Identidad visual: la marca Faustus (31-08-2026, noche)

Odysseus se identifica con un glifo de **velero** (dos velas y una ola) que sigue el color de acento del tema. Faustus se quedó con él durante todo el fork, así que el renombrado (§5) era sólo textual: la app seguía enseñando el logo de otro proyecto. Este bloque cierra eso con una marca propia.

**La marca.** Punta de flecha con un **bocadillo de chat recortado en negativo** (casa, tres puntos y cola) y dos alas laterales con muesca. Se partió de una referencia en PNG y se vectorizó midiendo, no calcando: k-means a 3 clusters para separar las tintas, máscaras binarias, **ajuste de rectas por mínimos cuadrados a cada arista** y las intersecciones de esas rectas como vértices (`approxPolyDP` directo daba ±2 px). Salen 12 vértices para el cuerpo, 5 por ala y 3 círculos. Contrastado contra el original: **IoU 0,953**; lo que falta es el antialiasing blando del PNG de referencia, que engorda ≈ 1 px todo el contorno.

**Dos variantes, por una razón medible.** Los tres puntos del bocadillo miden r ≈ 0,7 px a 32 px y se convierten en una mancha gris. La variante `small` (sin puntos, huecos del bocadillo abiertos, alas separadas) es la que se usa en favicon y en la pantalla de bienvenida (1,8 rem); la completa queda para los iconos grandes del manifest. Paleta medida del original: degradado vertical `#9F6DE0` → `#6F3ECA` en el cuerpo, `#B085E4` en alas y puntos — que resulta ser ≈ 62 % de opacidad del cuerpo, así que la marca funciona con un solo color más `opacity`, igual que hacía el velero.

**Ficheros.** `assets/branding/faustus-logo.svg` (master con degradado), `-flat.svg` (dos tintas, tematizable con `var(--logo-ink)`), `faustus-mark.svg` (`currentColor`, también en `static/icons/`) y `faustus-mark-small.svg`. Iconos regenerados: `static/icons/icon-192/-512/-maskable-512.png` (el maskable con su safe zone sobre `#282c34`) y los `.ico` multi-tamaño, que ahora traen la variante correcta en cada frame (16 y 32 px la simplificada, 48 px la completa) en vez de un reescalado del mismo dibujo.

**De paso, un 404 viejo.** `notes.js`, `tasks.js`, `settings.js` y `calendar/reminders.js` apuntaban las notificaciones del navegador a `/static/favicon.ico` y `/static/favicon.png`, y **ninguno de los dos existía**. Ahora existen. Nota de mantenimiento: `.gitignore` ignora `*.png`, así que los iconos van con `git add -f`.

### Verificación
El SVG vive **inline en cuatro sitios**, cada uno con su escapado distinto — data: URI url-encoded en el `<link rel=icon>`, concatenación de strings JS en el script de arranque, template literal en `theme.js::_updateFavicon`, y HTML plano en la pantalla de bienvenida — y no hay build que los mantenga sincronizados. Sustituir el dibujo con una regex **se comió las comillas dobles** del literal del script de arranque: un error de sintaxis que deja la página en blanco y que **los 262 tests de Python seguían pasando**. De ahí `tests/test_faustus_mark.py` (15 tests): parsea el SVG de los cuatro sitios como XML y compara la geometría, comprueba que el glifo viejo ya no aparece, que los dos registros de iconos por ruta siguen en sync, que existen los assets y que las rutas de notificación resuelven — y pasa **cada bloque `<script>` inline de `index.html` por `node --check`**, que es lo único que habría cazado el fallo real.

---

---

## 8. Deuda del roadmap upstream (31-08-2026, noche)

El `ROADMAP.md` de Odysseus es una lista de "help wanted" con 30 puntos. Se hizo una **criba explícita**: todo lo que existe para que el proyecto funcione en máquinas ajenas (smoke tests de instalación en macOS/Linux/Docker/WSL, Cookbook multiplataforma y SGLang, ranking de descargas, auditoría de proveedores cloud, accesibilidad, tours de primer arranque, pulido móvil, hardening multiusuario, LDAP) **queda fuera** — este fork corre en un PC, el de Luis. Lo que sí se implementó son los siete puntos que afectan a usar Faustus a diario con modelos locales. Cada uno se eligió porque *ya había mordido* o porque el roadmap lo marca como alta prioridad para modelos pequeños.

### 8.1 Salud de los servicios: el informe que nadie pedía

`GET /api/diagnostics/services` existía en upstream desde hace meses, con sus sondas de ChromaDB, SearXNG, ntfy, email y endpoints — y **cero llamadas desde el frontend** (`grep` en `static/`: ni una). Ese es exactamente el fallo del roadmap ("better degraded-state reporting"): cuando Docker se cierra, ChromaDB se va con él, el RAG de documentos y la memoria vectorial caen a coincidencia por palabras **en silencio** y las respuestas simplemente empeoran.

- `src/service_hints.py`: tabla de pistas accionables por categoría de fallo, sin secretos (nada se interpola desde el `meta` de la sonda, que puede llevar URLs con credenciales). Incluye el caso peor: `chromadb` en estado `disabled`, que significa "los almacenes no se llegaron a crear al arrancar" y por tanto keyword-only para toda la sesión.
- `src/service_recovery.py` + `VectorRAG.reconnect()` / `MemoryVectorStore.reconnect()`: reinicialización **sobre el objeto existente**. Crear una instancia nueva no serviría: el chat processor, el proveedor de memoria y media docena de rutas guardan la referencia vieja. Con esto se recupera "cerré Docker" sin reiniciar Faustus.
- `POST /api/diagnostics/services/reconnect` y pistas adjuntas al GET.
- `static/js/serviceHealth.js`: punto en la barra de usuario (verde/ámbar/rojo), sondeo cada 60 s y al volver a la pestaña, **aviso una sola vez en la transición ok → degradado**, y panel con qué está roto, qué hacer y el comando para copiar.

### 8.2 Libro de contexto y adelgazado de herramientas

Punto nº1 del roadmap para modelos locales ("agent prompt/context bloat"). La mitad que faltaba no era un recortador, era **una medición**: nadie podía decir que 9k de una ventana de 32k eran esquemas de herramientas antes de escribir una palabra.

- `src/context_ledger.py`: reparto por secciones (sistema, esquemas de tools, instrucciones, skills, memorias, documentos, web, resultados de herramientas, historial, tu mensaje) de los mensajes exactos que se van a enviar, más una línea de consejo cuando una sección se desmadra para la ventana en juego. Se emite como evento `context_ledger` (ronda 1, y luego sólo si crece un 25 % o se pasa del 75 %), lo reenvía `chat_routes` y lo pinta `agentHarnessUI` como tarjeta.
- `src/tool_slimming.py`: **sólo en ventanas de 4k/8k/16k**, recorta la prosa de los esquemas (descripciones de herramienta y de parámetros) hasta que caben en ~15 % de la ventana. Nunca elimina una herramienta — quitar la que el modelo necesitaba es un fallo indepurable — y nunca muta los esquemas globales, que son singletons compartidos entre peticiones. Se apaga con `agent_tool_schema_slim`.

### 8.3 Copias de seguridad que demuestran que restaurarían

El roadmap pide "backup/restore guide and helper flow for `data/`". El CLI (`scripts/odysseus-backup`) ya existía; lo que faltaba es que **nadie ejecuta un CLI a mano**, y la máquina sin copia siempre es la que lo tiene todo dentro.

- `src/backup_service.py`: mismo formato tar.gz que el CLI (entradas bajo `data/`, intercambiables), snapshot automático cada `backup_interval_hours` conservando `backup_keep`, y **verificación real**: se reabre el archivo, se validan los miembros y se extrae *sólo* los `.db` a un temporal para pasarles `PRAGMA integrity_check`. Una copia que no restauraría se detecta el día que se hace, no el día que hace falta.
- `GET/POST /api/backup/snapshots|snapshot|verify` (admin, ruta confinada al directorio de copias) y `/backup` en el compositor. **No hay endpoint de restauración a propósito**: sobrescribir `data/` con la app corriendo y las bases abiertas convierte un problema en dos; la API devuelve el comando exacto.
- Dos bugs de Windows que cazaron los tests: (1) un `.db` que no es SQLite hacía que `.backup()` lanzara y **se filtraran las dos conexiones**, así que `TemporaryDirectory` moría con `WinError 32` y se llevaba por delante la copia entera — un fichero basura bastaba para dejar el sistema sin backups (el CLI de upstream tiene la misma forma); (2) un tar.gz truncado sale por `EOFError`/`zlib.error`, que no son `TarError` ni `OSError`, así que el camino de "archivo corrupto" escapaba como 500 en vez de informar.

### 8.4 Notas al agente

"Todos should be assignable to an agent from the UI." El agente ya tenía `manage_notes`; faltaba el gesto. `static/js/noteToAgent.js` añade un botón a la tarjeta de nota que compone el prompt nombrando la nota por id, listando **sólo los ítems abiertos con su índice real**, y pidiendo que haga el trabajo de verdad y vaya marcando cada ítem con `manage_notes` — así se actualiza la lista que estás mirando, no una copia. Se envía por el camino normal del chat, con lo que siguen aplicando el modo agente, el workspace vinculado y la cola de la GPU.

### 8.5 Inyección de prompt: atacar el envoltorio

La auditoría que ya había (`test_prompt_injection_audit.py`) comprueba que el contenido recuperado **entra** en el envoltorio. Esta ataca el envoltorio mismo. Los marcadores de guarda se neutralizaban con dos `str.replace()` literales, así que `<<<end untrusted source data>>>`, un ángulo de más o un carácter de ancho cero metido dentro de la palabra pasaban enteros y podían cerrar el bloque antes de tiempo.

- Detección insensible a mayúsculas, tolerante a espacios/guiones bajos/ángulos extra, y repetida hasta punto fijo para que un marcador partido no se recomponga.
- Se eliminan antes los portadores invisibles: **bloque de etiquetas Unicode** (E0000–E007F, que codifica una frase entera que se renderiza como nada), espacio de ancho cero, uniones de palabra y BOM. **No** se tocan ZWNJ, ZWJ ni las marcas bidi: el persa y los emojis compuestos los necesitan, y destrozar un documento es un bug por derecho propio.
- El intento queda **registrado en los metadatos** del mensaje en vez de pasar en silencio, y las etiquetas se capan a 200 caracteres.

### 8.6 Guardarraíles de CSS y versión por hash

`static/style.css` son 42.000 líneas que empiezan con reglas de tipo que ganan a las clases de cualquier componente nuevo (ver [[faustus-css-gotchas]]). Dos medidas:

- El `?v=` de la hoja era un token escrito a mano: cada cambio de CSS necesitaba una segunda edición para verse, y olvidarla produce el peor parte de bugs posible ("tu arreglo no ha hecho nada"). Ahora `index.html` lleva `{{ASSET_V:style.css}}` y `src/app_helpers` lo sustituye por el **hash del contenido**, así que la URL cambia cuando cambia el fichero y sólo entonces. El JS conserva sus tokens literales: esas mismas cadenas aparecen dentro de `import … from './x.js?v=…'` y desincronizarlas carga el módulo dos veces.
- `tests/test_css_guardrails.py` congela las tres trampas: nada de nuevos selectores de tipo globales, toda clase de botón que se pinta el fondo debe declarar su propio `:hover` (el `button:hover` global gana a la regla base de una clase), y `-webkit-line-clamp` no se extiende (ya no recorta en el Chrome actual). Los infractores existentes van en listas explícitas de deuda, y un test avisa cuando una de esas listas se queda obsoleta.

### 8.7 Deep Research a la medida de la tarjeta

Los siete números de `research_*` vienen ajustados para un modelo alojado y rápido. En una GPU local eso lanza varias extracciones contra la misma tarjeta, todas revientan el timeout de 90 s y la investigación termina sin contenido — que se lee como "la web no tenía nada", no como un problema de ajustes.

- `src/research_presets.py`: perfiles por VRAM (`tight` <10 GB, `mid` 10–16, `roomy` 17–32, `big` 33+) con presupuesto de tokens, concurrencia de extracción y timeouts; el hardware se detecta con `services.hwfit` y, si falla, se cae al perfil conservador diciéndolo. `apply_patch` escribe **sólo** las claves que le pertenecen, para que "aplicar preset" no sea un endpoint de escritura arbitraria de ajustes disfrazado.
- **Bloqueadores**, la otra mitad: búsqueda desactivada, SearXNG elegido sin instancia que responda (el caso real de este PC: `search_url` vacío y el contenedor sin levantar), un proveedor con clave y la clave vacía, o ningún modelo. Cada uno con su arreglo de un clic, siempre **opt-in**, nunca como efecto colateral del preset.
- `GET/POST /api/research/preset[/apply]` y `/researchfit` en el compositor (`/research` ya estaba cogido por el panel).

### Verificación
**175 tests nuevos en 15 ficheros** (46 ficheros tocados, +4.780 líneas). Además de los de unidad, los de cableado: cada función nueva tiene un test que comprueba que **está enchufada**, porque el fallo que abre esta sección — un endpoint perfecto que nadie llama — es la forma más silenciosa de no entregar nada. Y los tres bugs reales de esta tanda (las dos conexiones SQLite filtradas, el `zlib.error` del tar truncado, el `?v=` olvidado) los encontraron los tests, no el uso.

---

## 9. Memoria compartida de GPU: el fallo que ningún otro indicador enseña (31-08-2026, noche)

`nvidia-smi` solo conoce la memoria física de la tarjeta. Windows, además, deja que el driver coloque asignaciones de GPU en la RAM del sistema y las lea por PCIe — la *shared GPU memory* del Administrador de tareas, 102 GB de ella en esta máquina. Para inferencia eso son ~25 GB/s contra los ~500 GB/s de la GDDR6X y, como generar relee los pesos activos en cada token, una capa servida desde ahí cuesta unas 20 veces más. Lo grave no es la lentitud: es que **no se ve**. Cuando el *sysmem fallback* de CUDA atrapa una asignación que no cabe, el modelo carga igual, `nvidia-smi` marca la VRAM casi llena, `ollama ps` dice 100% GPU, la temperatura y el uso de GPU son normales — y el modelo va a una fracción de su velocidad. Todos los indicadores en verde y el rendimiento hundido.

### 9.1 Medir el síntoma — `src/gpu_shared_memory.py`

Contadores WDDM por proceso leídos con PDH vía `ctypes` (`\GPU Process Memory(*)\Shared Usage` y `Dedicated Usage`), una sola consulta para los dos y sin subprocesos: ~280 ms la primera vez (calentar PDH) y ~3 ms después, con caché de 2 s porque el widget refresca cada 1,5 s mientras genera.

- **El runner de Ollama no se llama `ollama`.** En Windows es `llama-server.exe`, un proceso hijo. Filtrar por el nombre de Ollama encuentra el servidor y la app de bandeja — los dos que no tienen ni un byte de GPU — y se pierde al único que importa; hay que recorrer los hijos.
- **El umbral está medido, no elegido.** Un proceso CUDA siempre aparca memoria de sistema (buffers de staging): con qwen3.5:9b entero en la 4070 Ti generando a 65 tok/s son 706 MB planos, el 7,7% de su huella, sin moverse durante toda la generación. Un umbral absoluto bajo daría alarma siempre, así que hacen falta las dos condiciones: más de 1 GiB **y** más del 15% de la huella del runner.
- El `dedicated` de estos contadores es un *commitment* de WDDM, no lo que `nvidia-smi` llama "used": se vio un proceso declarando 7,7 GB "dedicados" mientras la tarjeta entera reportaba 1,6 GB en uso. Sirve para saber quién tiene la tarjeta, no para hacer cuentas — esas las hace el advisor con `ollama ps`.

### 9.2 Arreglarlo donde Faustus sí manda — `src/vram_fit.py`, `GET /api/system/vram-fit`

El ancho de la ventana de contexto es el mando en el que nadie piensa: los pesos los fija el fichero, la caché KV crece linealmente con `num_ctx`, y cuando la suma deja de caber es cuando el driver empieza a paginar.

- La KV por token se **mide** cuando el modelo está cargado (`ollama ps`.size − el fichero en disco, dividido por el contexto con el que se cargó): 14.828 B/token reales para qwen3.5:9b. La alternativa —la fórmula sobre los metadatos GGUF— da 131.072 para ese mismo modelo, 9 veces de más, porque su GGUF no trae `attention.head_count_kv` y solo 1 de cada 4 bloques es de atención completa. Cuando únicamente hay estimación, el plan lo dice y la trata como cota superior.
- Orden de preferencia deliberado: bajar el contexto → caché KV a `q8_0` → y solo entonces mover capas a la CPU. Las capas en CPU leen esa misma RAM sin el viaje por PCIe: es la versión honesta del mismo intercambio. Nunca sube el contexto por su cuenta; si sobra sitio lo informa (`max_ctx_that_fits`).
- `num_gpu` se suma a los overrides por chat para poder aplicar el reparto de capas. `OLLAMA_KV_CACHE_TYPE` y `OLLAMA_GPU_OVERHEAD` solo se recomiendan: Ollama los lee al arrancar, no por petición.
- Detalle que costaba 11 GB de error: el tamaño del fichero se busca por tag exacto. Emparejar por nombre base devolvía los 28 GB de `qwen3.8:27b-q8_0` al preguntar por `qwen3.8:27b-q4_K_M`, y todo el cálculo cuelga de ese número.

### 9.3 El ajuste del driver: lo que **no** se puede automatizar — `src/nvidia_drs.py`

Comprobado contra `nvapi64.dll` (driver 560.94), no supuesto: `NvAPI_Initialize`, `DRS_CreateSession`, `DRS_LoadSettings` y `DRS_GetBaseProfile` funcionan sin elevación, e incluso se puede crear un perfil por aplicación. Pero `DRS_EnumAvailableSettingIds` devuelve 102 ajustes y **la política de sysmem fallback no está entre ellos**: `0x10ECECC9` responde `NVAPI_SETTING_NOT_FOUND` tanto al leer como al escribir, porque el Panel de control la escribe por una vía privada. Y `DRS_SaveSettings` sin elevar devuelve `NVAPI_ACCESS_DENIED` de todas formas.

El módulo reporta eso —`exposed: false`, `manual_only: true`, con el motivo— en lugar de fingir que puede, ofrece los pasos y abre el Panel de control. Que además es el ajuste que menos falta hace por aquí: Ollama calcula su propio presupuesto de VRAM y no le hace caso (issue abierto `ollama/ollama#16725`). Donde sí importa es en lo que usa PyTorch, como la generación de imágenes.

### 9.4 Dónde se ve

Cuatro sitios, porque el fallo es invisible por definición: la **pill de uso** se pone en rojo con `⚠ PCIe spill`; el **panel de uso** gana su sección *Shared GPU memory* con los dos números y la explicación; los **controles de modelo** tienen el botón **Fit to VRAM**, que calcula el plan y lo aplica; y la página de hardware del **Cookbook** lleva una tarjeta permanente, para que el número se vea antes de que sea un problema y no solo después.

### Verificación

38 tests nuevos en 4 ficheros: la regla del umbral contra la línea base medida, la aritmética del ajuste (incluida la estimación de atención híbrida), lo que el driver expone de verdad, y el cableado de las cuatro superficies —un endpoint perfecto que nadie llama sigue siendo no entregar nada—. En vivo: qwen3.5:9b cargado al 100% en GPU, 65 tok/s, 706 MB de shared constantes durante la generación → "no spill", que es la respuesta correcta.

## 10. Referencias a código: la traza de error se convierte en el código real (31-08-2026, madrugada del 1-09)

**El problema.** El caso más común de un agente de código es *«me peta esto»* seguido de un traceback pegado. Hasta ahora no recibía ningún trato especial: la traza entraba como texto plano y el modelo de 9B gastaba dos o tres rondas (60–90 s) haciendo `grep` y `read_file` para volver a encontrar lo que la traza ya decía — fichero, línea y función—, y a veces terminaba arreglando el fichero equivocado. Y `@src/app.py:42` no servía de nada: la clase de caracteres de `MENTION_RE` no incluía `:`, así que resolvía el fichero y **tiraba el número de línea**.

**Qué hace.** `src/code_refs.py` lee el mensaje del usuario, extrae las referencias a código, las resuelve contra el workspace y le pone delante al modelo la **ventana numerada del fichero real** con la línea señalada.

- **Extracción** (`extract`): traceback de Python en sus dos formas (`File "/p/app.py", line 42` y la de Windows con letra de unidad), línea de fallo de pytest (`tests/test_x.py:42: AssertionError`), node-id de pytest (`tests/test_x.py::TestC::test_foo`, que no trae línea y se centra en la definición), stack de Node con columna (`at fn (/p/a.js:12:5)`) y el genérico `ruta:línea[:col]`. Las URLs se enmascaran antes de escanear, porque `http://localhost:8080` no es la línea 8080, y la letra de unidad forma parte de la ruta, nunca del número.
- **Resolución** (`resolve`): exacta → sin mayúsculas → **por sufijo más largo** → basename único. El sufijo es lo que hace que funcione de verdad: una traza casi siempre viene de otro checkout o de CI, así que `/home/ci/app/src/a.py` tiene que casar con `src/a.py` del workspace.
- **Ventanas** (`window`, `turn_context`): ±25 líneas con números alineados y marca en la línea señalada; dos marcos del mismo fichero a menos de dos radios se funden en **una sola** ventana; tope de 5 ficheros y presupuesto de caracteres (`agent_code_ref_chars`, 4000). Un fichero que ya venga entero por una mención `@` no se repite.
- **Lo de fuera se nombra, no se pega**: los marcos de `site-packages`, `node_modules`, venv, stdlib y `<frozen …>` se listan aparte con una frase explícita de que no son código del usuario. Todo el bloque va con el mismo envoltorio de contenido no confiable que las menciones, y se inyecta después del mapa del repo y antes del mensaje del usuario.

**Un falso positivo que se muere por el camino.** Esas mismas rutas de dependencias contaban hasta ahora en `user_missing_paths()` como «ficheros que el usuario nombró y no existen», y podían disparar la ronda de honestidad `target_substituted` sin ningún motivo. Pegar un traceback de una librería castigaba al modelo por algo que no había hecho.

**De regalo, del mismo módulo:** `@src/app.py:42` y `@src/app.py:120-160` recortan la ventana en vez de inlinear el fichero entero, y una línea nombrada gana a la regla de «demasiado grande para inlinear» — si dices la línea, la quieres ver.

**Ficheros.** Nuevo: `src/code_refs.py`, `tests/test_code_refs.py`. Tocados: `src/agent_loop.py` (inyección), `src/file_mentions.py` (rangos en las menciones), `src/agent_harness.py` (el falso positivo), `src/settings.py` (`agent_code_refs`, `agent_code_ref_chars`).

**Verificación.** 31 tests nuevos con un corpus de trazas **reales** — capturadas ejecutando código que falla de verdad, no escritas a mano —, más los negativos (ruta sin línea, URL con puerto, fichero inexistente), el presupuesto con 8 marcos, la fusión de ventanas, el symlink que escapa (no se inlinea) y el **test de cableado** que parsea `agent_loop.py` con `ast` y exige la llamada, el envoltorio y el orden de inyección: un módulo que nadie llama sigue siendo no haber entregado nada.

## 11. Exportar conversaciones: seis formatos desde un modelo de bloques (01-09-2026, madrugada)

**El problema.** El export existía —`md`, `txt`, `json`, `html`— pero los cuatro se construían a mano dentro de la ruta, en noventa líneas de concatenación de cadenas. El HTML escapaba el texto y sustituía el salto de línea por `<br>`, así que **los bloques de código y todo el markdown se perdían**: una respuesta con código salía como un muro de `<br>`. Y ningún formato incluía marcas de tiempo, el modelo, ni **las llamadas a herramientas del agente** — un transcript de agente sin sus tool calls no es un registro de lo que pasó.

**La forma.** Un **modelo de bloques intermedio** (`src/chat_export_model.py`) del que renderizan los seis formatos, así que una conversación se lee igual caiga donde caiga. El markdown se parsea **una sola vez** con el paquete `markdown` —que ya era dependencia— y su HTML se camina con `HTMLParser` de la stdlib hacia los bloques: fenced code, tablas y listas son justo lo que más emite un modelo y justo lo que un parser casero hace mal, así que no se escribió uno.

- **PDF** (`src/chat_export_pdf.py`, reportlab/Platypus). Se eligió reportlab por ser Python puro y BSD: WeasyPrint necesita Pango y cairo nativos en Windows, y Chromium son 150 MB de navegador. Portada, bandas de rol con color, código en caja gris que **parte las líneas largas en vez de recortarlas**, tablas con rejilla y cabecera repetida entre páginas, citas con barra lateral, enlaces reales y pie con «Page N of M».
- **DOCX** (`src/chat_export_docx.py`, python-docx) con estilos de Word de verdad —`Heading`, `List Bullet`, `Quote`, más estilos propios con el sombreado *en el estilo*, no como formato manual— para que se pueda reestilar en Word. Hipervínculos reales, que python-docx no expone y hay que montar como relación `w:hyperlink`.
- **HTML** autónomo: markdown renderizado de verdad, CSS embebido, claro y oscuro, sin un solo recurso externo. Se renderiza **desde los bloques**, no desde la salida cruda del parser, y eso hace el XSS imposible por construcción: cada nodo de texto pasa por `html.escape`, así que un `<script>` escrito en el chat sobrevive como texto literal en vez de ejecutarse — y sin censurar lo que el usuario escribió.
- **En lote**: `GET /sessions/export` con `project`, `folder` o `ids` devuelve un zip con un fichero por chat y un `index.md`. Si una conversación falla, entra un `.txt` con el error y el lote continúa.
- La ruta ya no adivina: un `fmt` desconocido da **400 con la lista** en vez de caer a markdown en silencio, y una dependencia opcional ausente da **503 nombrando el paquete**. La UI descarga por `fetch` + blob en lugar de `window.open`, que es la única forma de enseñar ese error en vez de una pestaña en blanco.

**Dos trampas que costaron sangre.** El `Content-Disposition` iba sin comillas ni codificar, así que un chat llamado «Informe 2026» producía una cabecera rota; ahora lleva `filename*=UTF-8''`. Y reportlab genera un **CMap inválido** para cualquier codepoint por encima de U+FFFF: `makeToUnicodeCMap` formatea con `%04X`, o sea cinco dígitos hex donde debería ir el par suplente UTF-16. Eso no estropea el emoji: corrompe **la capa de texto del PDF entero** —copiar, pegar y buscar dejan de funcionar, y pypdf revienta al leerlo—. Se descubrió porque el primer PDF con un emoji no se dejaba extraer. Decisión: en PDF los emoji se sustituyen por `?` aunque la fuente tenga el glifo; en DOCX salen intactos. Las tildes y la eñe van por una TTF registrada con cadena de búsqueda y respaldo carácter a carácter, y cuando no hay nada se sustituye el glifo — nunca se lanza una excepción.

**Ficheros.** Nuevos: `src/chat_export_model.py`, `src/chat_export.py`, `src/chat_export_pdf.py`, `src/chat_export_docx.py`, `static/js/chatExport.js` y cinco ficheros de tests. Tocados: `routes/session_routes.py` (la ruta pasa de 90 líneas de cadenas a una delegación), `static/js/sessions.js`, `projects.js`, `slashCommands.js`, `requirements.txt`.

**Verificación.** 193 tests nuevos. Se comprueban **los bytes de salida**, no que la llamada no reviente: el PDF se abre con pypdf y se afirma que el texto del chat está dentro, tildes incluidas; el DOCX se abre con `zipfile` y se comprueba su `word/document.xml`. Casos cubiertos: una URL de 2000 caracteres sin espacios que no debe desbordar (medido con el propio partidor de líneas de reportlab, no a ojo), un bloque de 500 líneas, una tabla de diez columnas, un `<b>` literal escrito por el usuario que no debe interpretarse como marcado —la trampa clásica de reportlab—, ocho cargas de XSS verificadas parseando el HTML de salida, y 500 mensajes en 0,67 s.

## 12. La puerta de análisis estático: dejar de conformarse con que el fichero parsee (01-09-2026, madrugada)

**El hueco.** El arnés comprobaba sintaxis y nada más: `py_compile`, `node --check`, `json.load`. Eso acepta encantado `Depends(get_db)` sin el import, `self.metodo_que_no_existe`, o un `from x import y` que no existe. Y el error número uno de un modelo pequeño **no** es escribir código que no parsea: es **usar nombres que no existen**, porque comprimir a 9B parámetros pierde justo los identificadores poco frecuentes.

El coste real, en esta máquina: el modelo escribe la ruta usando un símbolo que no importó, `py_compile` dice OK, corren los tests del proyecto —cuarenta segundos de reloj— y revienta con `NameError`. O peor: no hay ningún test que cubra esa rama, los tests pasan, la tarjeta dice **verified**, y el fallo aparece cuando arrancas la app.

**Qué hace.** `src/static_checks.py` descubre qué herramienta hay disponible —`ruff` en el venv del proyecto, `pyflakes`, `eslint` si hay config, `go vet`; `tsc` y `cargo` en modo `types`— y corre **solo reglas de corrección, nunca de estilo** (`--select F,E9`). Un proyecto sin configurar tiene cientos de avisos de estilo que ahogarían la señal. Los hallazgos se cruzan con el diff del checkpoint y **solo cuentan los de las líneas que el turno añadió**: un aviso preexistente no puede gastar una ronda de arreglo, la misma regla que ya aplicaba `compare_with_baseline` a los tests y por la misma razón. Sin ninguna herramienta disponible el veredicto es `unavailable`: no gasta ronda, no marca fallo, y dice qué instalar.

Va **entre** el chequeo de sintaxis y los tests, que es donde vale: fallar en 0,2 s en vez de en 40 s de pytest. Y vuelca en `TurnLedger.static_checks`, que ya existía, ya se pintaba y ya se puntuaba — la tarjeta y el scorecard salieron gratis.

`pyflakes` entra en `requirements.txt` como respaldo puro-Python y **corre en proceso**, no como subproceso: solo recorre el AST, nunca importa el código, y es el único camino que funciona en el build congelado, donde `sys.executable -m` relanzaría la aplicación entera (§ el mismo motivo que documenta `host_python()`).

**Dos cosas que solo se ven ejecutando las herramientas de verdad.** `FORCE_COLOR=0` **enciende** el color en ruff: su librería lee la variable como *presente = forzar color*, sea cual sea el valor. Como `project_tests._clean_env()` la pone para los test runners, cada hallazgo llegaba como `\x1b[1msrc/api.py\x1b[0m…` y no casaba con ningún regex — **la puerta habría dicho "limpio" sobre un fichero lleno de F821**. Y la columna tiene que ser obligatoria en el regex genérico, o `a.py:no_es_una_linea:1` inventa un hallazgo.

**Verificación.** 28 tests, entre ellos el cableado por partida doble: el que parsea `agent_loop.py` con `ast` y exige el **orden** (sintaxis < estático < tests), y tres funcionales que conducen `stream_agent_loop` de verdad y comprueban que el prompt de arreglo nombra el fichero, la línea y el código de regla.

## 13. Lo que enseñó usar la aplicación: la carpeta escondida y el modelo que no cabe (01-09-2026, madrugada)

Estas dos no salieron de leer código. Salieron de abrir Faustus en el navegador y usarlo como lo usaría alguien que llega nuevo.

### La acción central del producto estaba a tres niveles de profundidad

Vincular una carpeta es *la* acción de un agente de código: sin ella el modo Agente no puede leer ni escribir nada. Y no había **ninguna forma visible** de hacerlo. El indicador de workspace tenía `display:none` hasta que ya había carpeta, y su tooltip decía *"click to clear"* — solo servía para **quitarla**. El único punto de entrada era un elemento dentro del menú del chevron, que además solo aparece en modo Agente. Cuatro clics desde el arranque en frío, cero puntos de entrada visibles. Mientras tanto, el estado vacío gastaba su mejor sitio en un consejo rotatorio sobre el shift-click de la barra lateral.

Ahora el estado vacío en modo Agente dice cuál es la situación —*"No folder linked — Agent mode cannot read or edit files until you pick one"*, o *"Working in demo_app"*— y ofrece el botón que abre el selector que ya existía. El indicador es visible en todo el modo Agente: sin carpeta la abre, con carpeta la nombra, y la × sigue limpiándola. De 4 clics a 3 en frío, de 3 a 2 estando ya en Agente.

### El selector de modelos no decía cuál cabe en la tarjeta

El modelo por defecto de la máquina de referencia, `qwen3.8:27b-q8_0`, **no cabe** en sus 12 GB. La píldora de GPU lo detecta y avisa de PCIe spill —eso lo construyó §9— pero **solo después** de cargar el modelo y esperar. Medido en vivo esa noche: un turno de agente a **1,06 tok/s**. El selector ofrecía los seis modelos como iguales.

Ahora cada modelo local lleva su tamaño y un veredicto de tres estados, con las cifras reales en el tooltip y **la palabra además del color** (un color solo no es señal para bastante gente, y desaparece en un tema de alto contraste). El presupuesto descuenta lo que el propio runner retiene, porque cambiar de modelo lo descarga. Sin datos no se pinta nada: un modelo que no cabe **sigue siendo elegible**, solo avisado. Y solo se anota lo que sirve un Ollama en loopback — otro en la LAN corre en otra tarjeta y el veredicto sería una mentira segura.

El resultado, en la máquina de Luis: de seis modelos instalados, **uno solo cabe**.

Una regresión propia, encontrada abriendo el selector después de enviarlo: la fila mide ~290 px y el nombre, el endpoint y la insignia se la repartían, así que tres filas volvían como `qwen3.8:…` y no se podía distinguir el `q4_K_M` del `q8_0` — que es la única razón por la que abres el menú. Como la insignia solo existe para un Ollama en loopback, donde el endpoint es `127.0.0.1:11434`, la insignia pasó a ocupar **el sitio** del endpoint en vez de su espacio. El `@media (max-width: 480px)` que ya había no podía hacerlo: mide el viewport, no el menú, así que en una pantalla de 1568 px nunca disparaba sobre un popup de 290.

## 14. Lo que solo se ve usando la aplicación (01-09-2026, madrugada)

Esta sección es distinta a las demás: **ninguno de estos huecos salió de leer código.** Salieron de abrir Faustus en el navegador contra el Ollama de la máquina, vincular una carpeta de verdad y pedirle al agente una tarea pequeña —*"añade una función `apply_tax(total, rate)` a `cart.py` y su test"*— tres veces seguidas, arreglando entre una y otra lo que aparecía. Los tres fallos estaban en cadena y ninguno se veía desde el código: cada uno tapaba al siguiente.

### Primera vuelta: la palabra `rate` dejaba al agente sin `read_file`

El modelo contestó *"el archivo `cart.py` no puede ser leído con la herramienta actual"* y gastó ocho rondas probando `project_context`, `get_workspace`, `ls` y `grep` sin llegar a editar nada. No era culpa suya: **no le habían dado `read_file`**.

El clamp de intención web de la ruta decide con un regex de palabra sobre el texto crudo, y ese regex lleva `rate` dentro (por *exchange rate*). Pedir escribir una función llamada `apply_tax(total, **rate**)` se clasificó como una búsqueda web: la denylist desactivó `bash`, `python`, `read_file`, `write_file` y `edit_file`, y **re-habilitó** `web_search` y `web_fetch`. Un solo mecanismo explicaba las dos anomalías del log.

El arreglo no es parchear el regex —perseguir una palabra deja abierta la clase entera— sino un **suelo duro**: con carpeta vinculada, `read_file` y `ls` van siempre, y `edit_file`/`apply_patch` salvo en turno de baja señal. Se **resta de la denylist** en vez de sumarse a los esquemas, así que solo puede conservar una herramienta ya seleccionada, nunca inventarla, y no pisa `guide_only`, `block_all_tool_calls`, la denylist de no-admin, la allowlist de modo plan ni el ajuste del operador. `bash`, `python` y `write_file` quedan fuera del suelo a propósito: son el trío privilegiado que una ruta puede retirar legítimamente.

De paso: `cart.py` a secas no contaba como objetivo de código (el regex solo aceptaba rutas con barra), y **`Anade` sin tilde** no casaba ningún verbo español. El idioma en sí no influía —el clasificador es bilingüe— pero `rate` dispara en los dos.

Y el log mentía por omisión justo cuando hacía falta: `tool_names` y `relevant_tools` iban recortados a quince elementos, así que faltaban seis herramientas y no cinco, y las de web sí estaban entre las relevantes. Ahora registra los conjuntos completos y la diferencia en las dos direcciones.

### Segunda vuelta: se ofrecía `read_file` y luego se bloqueaba al ejecutarlo

Con el suelo puesto, la lista enviada ya era correcta —quince herramientas con `read_file` dentro— y aun así el log decía `Tool blocked before approval by current_tool_policy: read_file`, cinco veces. Trece llamadas, ocho fallidas, ningún fichero cambiado. El modelo llegó a llamar a `web_search` con la consulta *"demo_app workspace status"* —buscando en internet cuál era su propia carpeta— y acabó rindiéndose: *"las herramientas están bloqueadas en este workspace"*, volcando el código como texto.

**Ofrecer una herramienta y luego bloquearla es peor que no ofrecerla**: es una trampa por construcción, y un modelo de 9B se estrella contra ella hasta agotar el turno.

La causa: el clamp mete los mismos nombres en la denylist por **dos canales** —`disabled_tools` y el `ToolPolicy` que la envuelve— así que los dos predicados de la puerta disparaban a la vez; y como el log etiquetaba ambos con la misma palabra, no se distinguían. El suelo sí restaba de la denylist, pero en una variable local que **solo veía la lista de esquemas**: el prompt en prosa, la puerta del bucle y la del dispatcher seguían leyendo el conjunto sin reconciliar.

De ahí sale el invariante, que vale más que el arreglo: **lo que una ronda ofrece, esa misma ronda lo puede ejecutar.** Una denylist leída por las cuatro superficies, y una alarma `[tool-coherence] OFFERED THEN BLOCKED` que grita con nombre y origen si alguna vez divergen —con un test que la rompe a propósito para demostrar que suena—. El log pasó a decir *qué* predicado, *qué* política, y **dónde entró el nombre** en la denylist; eso son los veinte minutos que costó diagnosticarlo.

### Tercera vuelta: funcionó. Y entonces apareció el fallo de verdad

`READ_FILE done`, propuesta de `edit_file` correcta en la puerta de aprobación, **dos ficheros cambiados, sintaxis comprobada, tests del proyecto en verde**. De trece llamadas con ocho fallos a tres con uno.

Pero en la tarea siguiente el modelo hizo **una** edición correcta en `cart.py` y escribió: *"He completado la tarea. He añadido: en `cart.py`… **y en `tests/test_cart.py`** el test `test_total_con_envio()`."* Lo segundo era falso — ese fichero no se tocó. Y el turno salió **Verified**.

`check_completion` cazaba *"he modificado X"* cuando **no hubo ningún efecto**. Aquí sí hubo uno, así que la puerta se abrió y la afirmación sobre un segundo fichero pasó sin comprobar. La comprobación era *"¿pasó algo?"*, no *"¿pasó lo que dices?"* — y con un modelo pequeño, **terminar la mitad del trabajo y narrar el todo es un fallo mucho más común que no hacer nada y decir que sí**.

Ahora cada fichero nombrado se contrasta con lo realmente mutado. Lo delicado era la frontera entre afirmar y mencionar: cuenta un verbo de escritura en pasado dentro de un marco que atribuye autoría —incluida la **cabecera de lista que arrastra el verbo** a las viñetas siguientes, que es exactamente la forma del incidente, con el verbo y el fichero en líneas distintas—; no cuentan la lectura, el estado previo, la negación, la localización ni lo hipotético. Y el arnés **calla del todo cuando no puede saberlo** (una mutación sin ruta identificable, un `delegate_agents`, un `bash` con pista de escritura): acusar en falso gasta una ronda de un modelo a 20 tok/s y erosiona la confianza en la tarjeta, que es lo único que la hace útil.

### Y dos cosas menores que también salieron de mirar la pantalla

Se le ofrecían al modelo **herramientas que no podían funcionar**: llamó a `project_context` dos veces y las dos fallaron porque el chat no estaba en un proyecto. Cada herramienta imposible en la lista es una trampa; ahora un preflight las quita **con su motivo**, y si el modelo la pide igualmente con un fence, el error que recibe es ese motivo y no un "unknown tool" — un mensaje accionable cierra el bucle en una ronda, uno genérico lo abre.

Y `read_file` sobre un fichero grande devolvía una tajada ciega: sobre este mismo repo, `src/agent_loop.py` daba el **4,75 %** del fichero, cortado a mitad de línea y sin un solo símbolo, mientras el modelo creía haberlo visto. Ahora devuelve un mapa —los hechos, 124 símbolos con su línea que alcanzan el 64 % del fichero, las primeras ochenta líneas completas y la llamada literal para pedir cualquier otro tramo— por **un 64 % menos de tokens**.

## 15. Orquestación de agentes con cuadros de mando (02-09-2026)

Lo que había: `delegate_agents` lanzaba workers y el tablero enseñaba una fila por worker con "running/done". Lo que se pidió: **"más datos, control y feedback"** — lo que enseña Cowork cuando lanza agentes (una tarjeta por agente con lo que está haciendo ahora), pero con más información y con mandos.

Lo que hay ahora, en tres capas:

- **Contrato de eventos** (`src/agent_tools/subagent_tools.py`, `src/agent_runs.py`): cada evento de un worker lleva `ts`, `session_id` y un `delegation` id por llamada; `started` trae instrucción, ficheros, modelo, `max_rounds` y `timeout_s`; `round` y `tool` (inicio / progreso con cola del bash y segundos / fin); un **`tick` cada 5 s** con elapsed, segundos sin señal, ronda, última tool, tokens de entrada y salida y `stalled` con su motivo; `steer`, `supervisor` y un `done` con métricas finales. Todo pasa por `_compact_key` para que los ticks no inunden el buffer de replay.
- **Supervisor determinista y semáforo de GPU**: un worker sin señal durante `agent_subagent_stall_seconds` (120 s) o repitiendo la misma llamada tres veces recibe **un mensaje de dirección automático** ("pareces atascado: … termina con lo que tienes, pregunta o cambia de enfoque"); si sigue igual otro periodo, se le para con motivo `stalled` y el informe lo dice. No hay LLM en el supervisor: es un reloj y un contador, cuesta cero tokens. Y como hay una sola GPU, `agent_subagent_max_parallel` (2) limita cuántos generan a la vez; los demás salen como `queued` y no cuentan su timeout hasta que arrancan.
- **Dirigir un worker en marcha**: `POST /api/chat/subagent/steer/{child}` inyecta un mensaje del usuario en la cola del worker y entra antes de su siguiente ronda, sin perder el checkpoint ni el prefijo de KV. Parar uno solo (`/stop/{child}`), borrar el chat padre para también a los hijos, y `GET /api/chat/activity` publica los workers para la barra lateral.

El tablero (`static/js/agentHarnessUI.js`, "v3"): **una tarjeta por worker** con índice, nombre, rol (worker/reviewer), modelo, píldora de estado (running / queued / done / stopped / failed / **no activity 134s** / loop), el **chip de actividad al estilo Cowork** (*Reading files · Editing files · Running command · Browsing · Using the desktop · Thinking · Idle*, derivado de la tool en vuelo), elapsed en vivo (un ticker de 1 s; el `elapsed_s` del tick manda sobre el reloj del navegador, que no comparte hora con el servidor), ronda `r3/14`, tools (y fallidas), tokens in/out, la última llamada con su comando y la cola del `bash`, los ficheros que posee y los que cambió (chips que abren el diff), las líneas de dirección y del supervisor, y los botones **■ Stop / ✎ Steer… / ↗ Open chat / ↻ Re-run** (este último deshabilitado mientras el padre streamea, porque un envío durante el stream es un Stop de toda la delegación). Steer y Re-run son **formularios inline dentro de la tarjeta**: los `window.prompt()` nativos bloquean la página y los navegadores embebidos los rechazan. El estado vive por chat padre *y por delegación*: los eventos que llegan con el chat en segundo plano se guardan y se repintan al volver, y al recargar la tarjeta se reconstruye desde `tool_event.subagents` con tokens, duración, ficheros y modelo.

Verificado en vivo con `qwen3.5:9b`: dos workers en paralelo (una función nueva y su test) — tarjetas con *Thinking → Editing files*, 23 s y 44 s, 27k y 111k tokens de entrada acumulados, *verified*, un fichero cambiado cada uno; el segundo hizo once rondas porque el test le salía 45 en vez de 40 y lo corrigió solo. Un worker con un `sleep 100` en `bash` no se marca atascado (la cola del comando es señal); el atasco real —modelo mudo— está cubierto por tests del watchdog.

## 16. Ver y actuar: el modelo mira capturas, el escritorio y el navegador (02-09-2026)

Tres capacidades que Cowork tiene y Faustus no tenía: que el modelo **vea** (capturas), que **maneje el escritorio** y que **navegue** con una vista en vivo. Las tres comparten una pieza que faltaba.

### La pieza que faltaba: la imagen no llegaba al modelo

El servidor de navegador (Playwright MCP) devolvía las capturas como contenido de imagen, la interfaz las pintaba… y **al modelo le llegaba `[Screenshot captured (image/png)]` seguido de 8 KB de base64 en texto**. Con `--caps vision` activado, además, se le ofrecían seis tools de ratón por coordenadas que exigen una imagen que nunca veía. Ahora (`src/tool_images.py`, `_append_tool_results`): cualquier resultado de tool con `images:[{data, mimeType}]` se convierte en un **bloque `image_url` multimodal** en un mensaje de usuario sintético marcado como no fiable, solo si el modelo tiene visión — y eso se pregunta a Ollama (`/api/show` → `capabilities`), no a una heurística por nombre que decía que `qwen3.5:9b` no veía. Si el modelo no ve, lo describe el `vision_model` configurado, o se le dice que no pudo verla. La imagen se reduce a `agent_tool_image_max_px` (1280) y JPEG; `estimate_tokens` cobra 1.200 tokens por imagen y el recorte de contexto conserva solo la última (`agent_keep_images`): antes las capturas se acumulaban invisibles en la ventana.

### Escritorio (`src/agent_tools/desktop_tools.py`)

Siete tools sin dependencias nuevas (ctypes en Windows, `xdotool`/`wmctrl` en Linux, `pyautogui` opcional): `desktop_screenshot` (monitor o región; devuelve tamaño de pantalla, tamaño de imagen y escala, para que el modelo razone en píxeles de la captura), `desktop_list_windows`, `desktop_focus_window`, `desktop_click`, `desktop_type`, `desktop_key`, `desktop_scroll`. Las coordenadas se expresan en píxeles de **la última captura** y se mapean a pantalla con su origen y escala; el proceso se declara DPI-aware para que coincidan. Las tools de entrada están en `ALWAYS_APPROVE_TOOLS`: piden aprobación **en cada llamada**, por encima de las aprobaciones por tarea o por chat (`desktop_control_mode`: ask_each / ask_task / off; en off ni se ofrecen). Un preflight las quita cuando no hay escritorio.

Verificado en vivo: *"haz una captura de mi escritorio y dime qué ventanas hay abiertas"* → `qwen3.5:9b` describió el escritorio real: iconos (Blender, Steam, Discord, PyCharm…), carpetas por nombre, la barra de tareas y la hora. El primer intento falló por otra cosa (§17).

### Navegador

Sobre el Playwright MCP integrado: **perfil persistente** por defecto (`browser_profile` = persistent, en `<datos>/browser-profile`: cookies y sesiones sobreviven; isolated sigue disponible), `browser_headless`, **`browser_cdp_endpoint`** para pilotar el Chrome real del usuario (`--remote-debugging-port`), `browser_vision_caps` apagado (las tools de ratón por coordenadas eran ruido), `browser_snapshot_max_chars` (12.000: un árbol de accesibilidad de 24k tokens reventaba un 9B), `browser_allow_code_execution` apagado (sin `browser_evaluate` ni `browser_run_code_unsafe` salvo opt-in). Una **política propia** en vez de la puerta genérica de "contexto externo": navegar, snapshot, captura, consola y red libres; click, escribir, rellenar, subir ficheros y ejecutar código con aprobación. El servidor `npx` se reconecta si muere (antes quedaba muerto toda la sesión con el estado diciendo *connected*), el interruptor "browser" y el privilegio `can_use_browser` cubren todas las tools por prefijo (cubrían 12 de 30) y la intención se detecta también en castellano.

Y la **vista en vivo** (`src/browser_view.py`, `static/js/browserView.js`): tras cada acción del navegador se captura el viewport en JPEG y se emite un evento `browser_view`; un panel derecho enseña el último frame con título y URL, una tira de los ocho últimos y un punto *Live* que se enciende con el primer frame del turno que streamea y se apaga al acabar o al cambiar de chat. Verificado: *"abre example.com, dime el título y a dónde lleva el enlace"* → navegación, panel abierto solo, snapshot, respuesta correcta (y honesta: el enlace se llama *Learn more*, no *More information*).

## 17. Robustez y paridad de workspace (02-09-2026)

- **Índice de herramientas sin ChromaDB** (`src/tool_index_memory.py`): el selector de tools por embeddings dependía de Chroma; con Docker cerrado cada petición pagaba 1,5 s de timeout y caía a palabras clave. Ahora hay un carril en memoria (coseno sobre fastembed) con caché de embeddings en disco y **warmup al arrancar**; Chroma sigue siendo opcional. Arranque en caliente: 1,5 s, de los que 50 ms son el índice.
- **Settings → Agent Tools → "Agent & automation"** (`src/agent_settings_schema.py`, `static/js/agentSettings.js`): las 63 opciones del agente, el navegador, el escritorio y la visión —hasta ahora solo alcanzables por API o slash— en nueve grupos con ayuda, la clave en monoespaciado, filtro, guardado por grupo y reset por campo. Un test de paridad rompe si aparece una clave `agent_*` sin ficha.
- **Settings → "Local models"** (`routes/local_models_routes.py`, `static/js/localModels.js`): lo que LM Studio hace mejor que nadie. Barra de VRAM de la tarjeta (modelos / otros / reserva / presupuesto), **cargados ahora** (residente, split GPU/CPU, contexto, cuándo expira, *Unload*), instalados (tamaño y veredicto de ajuste, cuantización y parámetros, capacidades vision/tools/think/embed, contexto, *Load*, *Set default*, **opciones por modelo** —`num_ctx`, `num_gpu`, `keep_alive`— que `llm_core` aplica por debajo de los overrides de cada petición, *Delete* con confirmación propia), **pull con progreso en vivo** (SSE que sobrevive a cerrar la pestaña y se reengancha, cancelable) y un catálogo *Discover* offline de 50 familias con cada tag anotado con si cabe en esta tarjeta. Verificado: cargar `qwen3.5:9b` (7 GB residentes, 100 % GPU), pull de `all-minilm:22m` con barra, borrado.
- **Lo que enseñó usarlo hoy**, arreglado con test cada uno: un BOM UTF-8 (fichero escrito por PowerShell) contaba como error de sintaxis para pyflakes en proceso → ronda de arreglo falsa y el modelo reescribiendo el fichero para "quitar el BOM"; las tools de escritorio desaparecían porque la rama "máquina local" *reemplazaba* la selección por el juego de fichero/terminal (ahora se re-añaden las semillas de dominio); el modelo por defecto era el de 29 GB que no cabe (*PCIe spill*, 2 tok/s) y cada recarga volvía a él → un chat nuevo arranca con **el último modelo elegido a mano**, por usuario; el clon de medida del composer estaba posicionado sin `left/top` y un `scrollIntoView` desplazaba todo el chat 300 px a la izquierda; "1 tool call (1 failed)" cuando era la puerta de aprobación → *awaiting approval*; un worker que dijo *"el comando se está ejecutando, esperaré"* con cero tools salió como *done* (ahora un anuncio en progreso sin tool es un anuncio incumplido y el arnés lo devuelve).

## 18. Después de cada entrega, la auditoría: once fallos reproducidos y corregidos (02-09-2026, tarde)

Un subagente auditó los módulos nuevos de la ronda (delegación, modelos locales, índice de tools, escritorio, vista del navegador) con **scripts de reproducción**, no con lectura: cada fallo tiene un test que falla en rojo antes del arreglo.

- **Seguridad**: la puerta de delegación dictada por el usuario comparaba solo las palabras de la instrucción, así que un `context` escrito por el modelo llegaba literal a los workers (que corren con la puerta desactivada), `files` podía apuntar fuera del workspace y una tarea repetida N veces lanzaba N workers → ahora se compara **toda** la carga normalizada, con cada tarea consumida una sola vez. Las mutaciones de *Local models* (borrar, pull, cargar/descargar, opciones) eran alcanzables por el modelo a través del token interno de `app_api` → en la lista de bloqueo, GET sigue abierto.
- **Corrección**: las opciones guardadas por modelo se perdían en silencio si Ollama no escuchaba en el 11434 (solo se rerutaba en ese puerto) → también cuando el admin declaró ese servidor como Ollama; pulls fantasma tras reiniciar; el formulario de opciones borrado por el repintado de 8 s; cancelar + volver a pull devolvía el job cancelado; un 500 por `keep_alive` inválido; una caché de índice estructuralmente inválida tumbaba el carril de memoria (y el aviso culpaba a Chroma); `desktop_scroll` sin coordenadas apuntaba al centro de la pantalla virtual (fuera de todo monitor con dos pantallas); la vista del navegador emitía un fotograma para una acción aparcada en la tarjeta de aprobación; el juego de tools *lean* de los workers devolvía las diez tools a la vez por una sola palabra clave.
- 9 commits, 213 tests nuevos/ajustados, todo verificado después en la máquina real.

## 19. Dos tarjetas: reparto de modelos, potencia y vista conjunta o separada (02-09-2026, noche)

Luis añadió una **RTX 5060 Ti 16 GB** por eGPU. Lo primero que enseñó el PC: el Ollama en marcha (arrancado mientras cambiaba el driver) **no había detectado ninguna GPU** (`inference compute id=cpu … total_vram=0 B`); reiniciado, ve `CUDA0` + `CUDA1`, `total_vram=27.9 GiB` y sube solo el contexto por defecto a 32k. Después, medido en vivo cómo reparte Ollama 0.33 (`sched.go` en su log): un modelo que cabe en una tarjeta va a **la que más memoria libre tiene**; `main_gpu: N` en las opciones de la petición **lo fija** (`selecting requested single GPU … requested_main_gpu=0`); uno que no cabe en ninguna se **parte entre las dos** (`qwen3.8:27b-q4_K_M`, 17 GB: 8,5 + 10,2 GB, 100 % GPU, 23,6 tok/s — antes se desbordaba a CPU). Sobre esos datos:

- **`/api/system/usage`** (`routes/system_usage_routes.py`, `src/gpu_placement.py` nuevo): cada tarjeta con uuid, bus, libre, **los modelos que residen en ella y cuántos bytes** (`nvidia-smi --query-compute-apps` da el pid del runner por GPU; los contadores WDDM por pid y por adaptador dan los bytes; el `FROM <blob>` del modelfile casa cada runner con su modelo), un bloque **`gpu_pool`** (suma de VRAM, potencia y límite, máximo de uso y temperatura) y, por modelo cargado, `placement` = *single / split / cpu* con el reparto por tarjeta.
- **Pill y panel de uso** (`static/js/sysUsage.js`): conmutador **Combined / Separate** (persistente). Combinado: `GPU 12% · 22.6/28G · 50°` y un bloque "GPUs (2)" con barras del conjunto (uso máx/medio, VRAM, **potencia W / límite W**, temperatura) y una fila compacta por tarjeta con los modelos que tiene ("qwen3.8:27b-q4_K_M · 9.6 GB · split with #1"). Separado: `GPU0 12% 10.7/12G · GPU1 0% 11.9/16G` y una sección por tarjeta. Cada modelo de Ollama lleva su línea *Placement*.
- **Local models**: la barra del conjunto y **una barra por tarjeta** (modelos / otros / libre, presupuesto por tarjeta, reserva CUDA × N), chip de ubicación en cada modelo cargado (`GPU 0 · RTX 4070 Ti`, `split #0 9.6 GB + #1 11.6 GB`), un cuarto veredicto de ajuste **`split`** (cabe en el conjunto pero en ninguna tarjeta sola, con la nota que lo explica; `qwen3.8:27b-q8_0`, 27,9 GB, sigue siendo *no fit* contra 24,9 usables) y en *Options…* el selector **`main_gpu`** (Auto / GPU 0 — RTX 4070 Ti (12 GB) / GPU 1 — …) que `llm_core` manda en cada petición y que el botón *Load* también envía junto a `num_ctx`/`num_gpu` (antes *Load* cargaba con los valores del servidor y el primer chat recargaba el modelo). El asesor *Fit to VRAM* presupuesta el conjunto (reserva por tarjeta).
- **Verificado en el navegador integrado**: opciones `ctx 16k · gpu #0` para `qwen3.5:9b` → *Load* → `GPU 0 · RTX 4070 Ti` (Ollama lo habría puesto en la 5060 Ti); *Load* de `qwen3.8:27b-q4_K_M` → `split #0 9.6 GB + #1 11.6 GB`, 100 % GPU; un chat con el q8_0 a 128k de contexto → el pill avisa **⚠ PCIe spill · 56 %↑GPU** (0,7 tok/s) y el asesor propone 55/66 capas a 8k con KV q8.
- **Lo que enseñó usarlo**, con test cada uno: el panel tenía scroll horizontal (las cifras por tarjeta en una línea) y los nombres largos aplastaban el conmutador; `LOCALHOST_BYPASS=true` (el modo de desarrollo) era inutilizable en el navegador: el middleware dejaba pasar sin usuario y cada ruta con su propia comprobación (research, email, projects, cookbook…) devolvía 401/403 → el manejador global mandaba a `/login`, que devolvía a `/` → bucle de recargas; ahora el bypass **actúa como el primer admin** (solo loopback directo, nunca tras proxy). Y el arnés marcó *"no puedo saber en cuántas GPUs estoy corriendo"* como una acción anunciada y no hecha (una segunda ronda a 0,7 tok/s por una respuesta de una línea): un progresivo dentro de una negación o de una pregunta indirecta es una descripción de estado.

## 20. Lo que da de sí la segunda tarjeta, medido (03-09-2026, madrugada)

Antes de programar nada más, números en la máquina real (`qwen3.5:9b`, `think:false`, 8k de contexto, ~125 tokens por respuesta): 9B en la 4070 Ti **72,9 tok/s**, en la 5060 Ti **66,2 tok/s**; el 27B q4_K_M (17 GB) pasa de desbordar a CPU a **100 % GPU repartido, 23–24 tok/s**; el q8_0 de 29 GB sigue sin caber. **Dos peticiones al mismo modelo van en serie** (2,2 s + 4,2 s, wall 4,6 s: un slot por modelo; `OLLAMA_NUM_PARALLEL=2` lo lee el servidor pero llama-server sigue con `n_slots = 1` en 0.33.2); `ollama cp` + `main_gpu` distinto **no** da un runner por tarjeta (mismo blob = mismo runner, la copia expulsa al original). **Dos modelos distintos sí generan a la vez** (9B en la 5060 Ti + 27B repartido: wall 10,9 s frente a 16,5 s en serie, cada uno más lento mientras comparten tarjeta). Conclusión: la segunda tarjeta da **capacidad** (modelos de 17–20 GB enteros en GPU, contexto por defecto 32k) y paralelismo solo entre modelos distintos.

- **Runners huérfanos** (`src/gpu_placement.orphan_runners`, `POST /api/system/gpu/orphans/release`): reiniciar Ollama deja vivos sus `llama-server.exe` (vi 13 GB retenidos en la 5060 Ti con `ollama ps` vacío, contados como "other" en todas las gráficas). El panel de uso y *Local models* los listan con tarjeta y bytes y ofrecen **Release** (mata solo un runner re-verificado como huérfano en ese momento, solo admin, vetado para `app_api`). Verificado en vivo: huérfano de 6,5 GB → Release → 380 MB.
- **Modelo de los workers** (`agent_subagent_worker_model`, Settings → Agent & automation → Sub-agents): los workers usan el del coordinador salvo que se fije otro (el `model` de una tarea sigue mandando). Con dos tarjetas, fijar ese modelo a la otra tarjeta (main_gpu) es lo que hace que coordinador y workers se solapen de verdad; la ayuda del ajuste lo dice.

## 21. Fable workers: que el modelo caro planifique y revise, y los workers locales hagan el trabajo (03-09-2026, madrugada)

Petición de Luis: que Fable (Claude en Cowork) no se fume sus tokens y tire de workers locales todo lo que pueda. Lo que pasa por un bucle de herramientas (leer ficheros, editar, tests, arreglar, repetir) son decenas de miles de tokens; Faustus ya corre ese bucle en modelos locales (`/agents`). **Dispatch** abre ese bucle a un coordinador externo y devuelve un **resultado compacto**: por worker estado, ficheros cambiados, checks estáticos, git, rondas/tools/tokens y sus últimas palabras (≤ 1200 caracteres) — nunca la transcripción.

- **`POST /api/dispatch`** (`src/dispatch.py`, `routes/dispatch_routes.py`): cada trabajo corre la misma maquinaria que `delegate_agents` (locks de ficheros, watchdog, supervisor, semáforo GPU, toolset lean) dentro de un **chat "Workers"** propio, con el tablero de control, steer/stop y transcripciones; al terminar se graba en ese chat como un turno de `delegate_agents`, así el tablero se reconstruye del historial. `GET /{id}` (progreso por worker mientras corre; resultado compacto al acabar), `/{id}/wait` (long-poll ≤ 600 s), `/{id}/events`, `/{id}/cancel`, `/config` (qué modelo usaría), `/guide`. Espejo JSON en `DATA_DIR/dispatch/`; un trabajo que pilló un reinicio vuelve como *interrupted*.
- **Token con scope `agents:dispatch`** (perfil `fable_workers`); el modelo dentro de un chat no puede llamar a `/api/dispatch` por `app_api` (tiene `delegate_agents` con su puerta). Ajustes `dispatch_model` / `dispatch_endpoint_id` (visto en vivo: sin ellos el trabajo cayó en el q8_0 de 29 GB, el modelo por defecto → ahora `dispatch_model` manda aunque no haya endpoint id).
- **Servidor MCP `mcp_servers/workers_server.py`** para Claude Desktop / Cowork / Claude Code: `workers_guide` (cómo usar bien a los workers: qué delegar, cómo escribir una tarea, cómo leer el resultado, el bucle plan → dispatch → wait → check), `dispatch_workers`, `workers_wait`, `workers_status`, `workers_events`, `workers_cancel`, `workers_list`. Y una **skill** lista (`integrations/claude/skills/faustus-workers/SKILL.md`, dentro del bundle de Claude Code `/api/claude/plugin.zip`; `integrations/faustus-workers/README.md` dice cómo instalarla en Cowork) para que cualquier modelo tipo Fable delegue solo. Documentado en `website/fable-workers.md`.
- **Página Workers** (barra lateral, `/workers`, `static/js/workers.js`): una caja en lenguaje natural (una línea = un worker, máx. 4), la carpeta, *parallel*, *reviewer*, modelo (muestra cuál usaría), *Run*; lista de trabajos con estado, progreso por worker, resultado compacto, *Cancel* y *Board* (abre el chat Workers).
- **Verificado en vivo**: por API, *"add apply_discount with validation and a test; pytest must pass"* → 44 s, 12 rondas, 2 ficheros, 7 tests en verde, **~1,5k tokens de vuelta frente a los 118k que consumió el worker**; desde la página Workers en castellano (*"Añade a cart.py currency_format_usd…"*) → 27 s, 9 rondas, 2 ficheros, y el chat Workers muestra el tablero.
- **Política de reparto de GPUs** (`src/gpu_policy.py`, `gpu_placement_prefer`; selector *Placement* en Local models y grupo *GPU placement* en Agent & automation): Luis prefiere que los modelos ocupen la 5060 Ti y de la 4070 Ti solo lo necesario. Medido antes: un modelo fijado con `main_gpu` a una tarjeta en la que no cabe **no se reparte, va a CPU** (54/66 capas, 10 tok/s frente a 19–24) y `tensor_split` se ignora. Así que *Fill GPU N first* fija a esa tarjeta solo los modelos que caben con margen para el contexto (reserva CUDA + 18 %); los grandes siguen en *Auto* (split). Se aplica en cada petición de chat (`llm_core`), en el botón *Load* y en los workers; un pin por modelo siempre gana, y el formulario avisa si la tarjeta elegida no puede con el modelo. Verificado: 9B → `GPU 1 · RTX 5060 Ti`; 27B q4 → sigue repartido 100 % GPU.

## 22. Workers fiables: la respuesta es evidencia, no la palabra del worker (03-09-2026, madrugada)

Petición de Luis: revisar el modo de workers para asegurar que es fiable y que lo que devuelve está probado de forma aceptable; investigar cómo lo hacen otros. Dos fuentes: una **auditoría adversaria** del código (16 hallazgos, con un test de reproducción por cada uno) y lo que hacen sistemas comparables — el patrón orquestador + verificador de Anthropic (y su aviso del *early victory*: el agente que declara éxito tras una prueba), el bucle de reflexión lint/test de Aider, el chequeo de regresión de Agentless, la entrega solo-resumen de Roo Code Orchestrator. Lo que faltaba, en una frase: **nada de lo que volvía lo comprobaba Faustus** — `files_changed` era el libro del propio worker, "los tests pasan" era su prosa, ningún comando de tests corría fuera del worker, y un trabajo cuyo único worker se quedó *stalled* se contestaba como `done` / exit 0 / 0 errores.

- **Evidencia** (`src/dispatch.py`): antes del trabajo se hace un **checkpoint** del workspace (el repo sombra del harness — el `.git` del usuario no se toca; sin git, una foto por mtime del árbol) y después se diff-ea: `result.changes` (added / modified / deleted, exactos por contenido) es lo que cambió de verdad; `files_changed` es esa lista; lo que un worker *dijo* que cambió y no cambió sale como `claimed_only`.
- **Verificación por Faustus**: tras los workers, Faustus corre `verify` en el workspace — el comando que da el coordinador (`pytest -q`, `npm test`, `make check`…) o, en `auto`, el runner de tests detectado del proyecto sobre los tests relacionados con los ficheros cambiados (`verify_scope: all` para la suite entera). Los fallos se comparan con el checkpoint: un test que ya fallaba antes es `pre_existing` y no bloquea. Sin runner y sin comando → `ok: null`, "not verified" — nunca "passed".
- **Una ronda de arreglo acotada** (Aider / Anthropic *retry with feedback*): si la verificación falla, **un worker fixer** recibe la salida del comando fallido más las tareas originales y se verifica otra vez (`fix_rounds`, por defecto 1, máx. 2; `attempts` en el resultado). Si sigue fallando → `partial`.
- **Estado honesto**: `done` solo si todos los workers acabaron y la verificación pasó o no pudo correr; `partial` si un worker acabó `error` / `timeout` / `stalled` / `stopped` o la verificación falló; `verdict` lo dice en una línea ("1/2 workers done (timeout) · 3 files changed on disk · verification FAILED (1 failed)"). `exit_code` y `totals.errors` siguen al estado.
- **Una máquina**: el semáforo "como mucho N workers a la vez" es **compartido por todas las delegaciones del endpoint** (un `/agents` de un chat y dos trabajos a la vez corrían 3 × N workers contra un Ollama); los trabajos en el **mismo workspace (o uno anidado) corren de uno en uno** — el segundo espera como `queued` y lo dice en `phase`; los **locks de ficheros se sueltan cuando el worker acaba** (la tarea dependiente de una ejecución secuencial tenía prohibidos los ficheros del worker anterior — un bug latente de `/agents`).
- **Cancelar conserva la evidencia**: `cancelling` hasta que los workers se han desenrollado, luego `cancelled` con lo que cambió en disco; cancelar antes de arrancar también deja turno.
- **La puerta**: solo admins (un usuario normal recibía 200/400 según existiera la carpeta — un oráculo de rutas del host — y podía gastar el endpoint del admin); un predicado de visibilidad para la lista y la lectura por id; el texto del coordinador entra en el chat Workers marcado como **contexto externo no fiable** (la puerta de herramientas lo trata como un documento pegado); `workspace` obligatorio (sin él el cwd de los workers era el DATA_DIR de Faustus); el modelo que se reporta es el que corre (`ctx.model` gana al ajuste de sub-agentes); **`Idempotency-Key`** / `client_request_id` (un POST reintentado devuelve el mismo trabajo); `gen_overrides` solo con mandos de muestreo (nunca `main_gpu` / `num_gpu` / `keep_alive`, que pisarían la política de reparto).
- **Acotado**: solo los fallos de los checks estáticos, 40 rutas reclamadas por worker, sin foto git por worker (era el estado de TODO el árbol repetido por worker: 14k tokens en un repo sucio); los espejos JSON rotan a 200; cada tarea aparece en `progress` desde el principio; una respuesta *running* trae `wait_again`, `ceiling_s` y `phase`; el long-poll llega a 1800 s.
- **MCP** (`mcp_servers/workers_server.py`): cada dispatch lleva su Idempotency-Key y reintenta una vez ante un error de conexión; 401/403 dicen qué variable de entorno o scope falta; el render muestra verdict, cambios en disco, *claimed but NOT changed*, la verificación con los tests que fallan, "call workers_wait again", y la pista de re-despachar en *interrupted* / *cancelled*. Esquema: `workspace` obligatorio, `verify`, `verify_scope`, `fix_rounds`. Guía del coordinador y skill actualizadas.
- **Página Workers**: una línea en blanco o un marcador de lista empieza una tarea (un párrafo con saltos de línea era tres workers con fragmentos de frase), contador "N workers" en vivo, campos *Verify with* (con el runner detectado como placeholder) y *Fix rounds*, bloques verdict / changed on disk / verification, estados partial / verifying / cancelling.
- **El chat Workers** graba el mismo bloque `harness` que un turno de chat: badge 🛡, chips de ficheros con **diff contra el checkpoint del trabajo** (Accept / Reject / *Restore to before this turn*), línea de tests. De paso, un bug preexistente de `chatRenderer`: el clic en un badge 🛡 restaurado moría con `metadata is not defined` — los chips nunca aparecían al recargar.
- **Verificado en vivo (7001, carpeta sin git)**: tarea vaga *"10 % off from 10 units"* con un test plantado que exige `ValueError` para cantidades negativas → el worker hizo lo suyo, **el pytest de Faustus pilló "DID NOT RAISE ValueError"**, `fixer-1` recibió la salida y añadió la comprobación → 9 passed, diff del checkpoint = solo `cart.py`, respuesta de 3,2 KB, 40 s. Un verificador que falla a propósito con `fix_rounds: 0` → `partial`; un segundo trabajo en una subcarpeta → `queued` con el motivo, cancelado limpio; un POST repetido con la misma clave → el mismo trabajo; y desde el chat Workers, el diff de `cart.py` contra el checkpoint con Accept / Reject.
- Tests: `tests/test_dispatch_reliability.py` (27, nacidos de los 16 repro de la auditoría) + los de dispatch / página / locks adaptados. Suite: 7.809 en verde.


## 23. Las ideas del "Agentic Coding Flywheel", traídas a una sola app (03-09-2026, mañana)

Luis dejó en `D:\LocalAi\inspiration\` dos informes: el barrido de 53 repos de Jeffrey Emanuel
(Dicklesworthstone) buscando mecanismos robables, y tres ideas propias. Su ecosistema son ~90 CLIs sueltas
pegadas con tmux; la apuesta de Faustus es **absorber los mecanismos dentro de una sola superficie** donde
no haya que saber qué es un lease, un bead ni un pane. El estado de cada idea se lleva en
`D:\LocalAi\inspiration\ESTADO_IMPLEMENTACION.md`.

### 23.1 Dashboard de objetivos por proyecto (la idea nº1 de Luis)
El problema real: el estado de un plan vivía dentro de un chat. Al cerrar un turno el agente decía "falta X"
y ese "falta X" se perdía en el scroll; para saber por dónde iba un proyecto había que entrar en la sesión y
leer hacia atrás.

- **Almacén** (`services/objectives.py`): `<workspace>/.odysseus/objectives.jsonl` como verdad versionable —
  una línea por objetivo y las **dependencias como aristas separadas** (el modelo de `beads_rust`), más
  `objectives_log.jsonl` append-only con cada delta, conflicto y evidencia. Escritura atómica, fichero
  corrupto → `.corrupt` y arranque en vacío: nunca rompe un mensaje.
- **El agente nunca reescribe la lista**: emite **deltas tipados `ADD` / `EDIT` / `KILL` con `rationale`**
  (el patrón de `brenner_bot`) y un **compilador determinista** los ordena (ADD→EDIT→KILL), los valida
  (título duplicado, id desconocido, estado inválido, ciclo en las dependencias, `KILL` de un agente sin
  rationale) y **marca conflicto en vez de pisar una edición humana** (`base_updated_at` + `last_actor`).
- **Priorización por grafo** (`beads_viewer`): `PageRank×0.30 + betweenness×0.30 + blocker_ratio×0.20 +
  staleness×0.10 + priority×0.10` sobre las dependencias declaradas, con **priority hints** cuando el orden
  estructural diverge del que puso el humano. Todo en stdlib y determinista.
- **Lectura obligatoria**: el bloque de objetivos entra en el system prompt del proyecto, y
  `post_compact_reminder()` (`src/context_compactor.py`) lo **reinyecta después de cada compactación** junto
  a las reglas del proyecto — el truco de `post_compact_reminder`, que arregla el fallo real de "el agente
  olvida el plan a mitad de sesión larga".
- Superficies: tool `project_objectives`, `GET/POST/PATCH/DELETE /api/projects/{id}/objectives` (+ `/deltas`),
  sección **Objectives** en el hub del proyecto (estado, prioridad, "blocked by", badge ⚡ del hint, actividad),
  y las tools MCP `objectives_list` / `objectives_apply`. Evidencia automática: un trabajo de `/api/dispatch`
  que menciona un `OBJ-n` deja un registro de evidencia con confianza según el verdict.
- **Verificado en vivo (7001)**: alta y edición desde la UI, deltas de agente aplicados con el `KILL` sin
  rationale rechazado como conflicto, y `qwen3.5:9b` marcando `OBJ-1` como *done* y añadiendo `OBJ-6` con la
  herramienta. Test de aceptación que dio Luis — *"este mismo punto podría haberlo puesto en objetivos y al
  entrar lo revisas tú"* — cumplido.

### 23.2 Guarda de comandos destructivos (`dcg` + `slb` + recibos de decisión)
`src/command_guard.py` clasifica cada comando en **SAFE / CAUTION / DANGEROUS / CRITICAL** con la mecánica
exacta del original: **whitelist-first**, rechazo rápido por substring antes de tocar una regex, packs por
dominio (`fs`, `git`, `db`, `containers`, `system`), **lookahead** para bloquear `--force` pero no
`--force-with-lease`, **escaneo de heredocs y de `python -c` / `bash -c`** (el comando peligroso suele ir
escondido dentro) y **fail-open con presupuesto de latencia**: si la guarda tarda, deja pasar en vez de colgar
el turno. Recall > precisión, como en el original.

- La aprobación **no es un sistema nuevo**: se ata al mecanismo de aprobación exacta que Faustus ya tenía,
  que sella el **SHA-256 del comando** y lo **revalida justo antes de ejecutar** (la idea de `slb`) — un
  comando distinto en un byte no viaja en esa aprobación. Un DANGEROUS/CRITICAL se pregunta **aunque haya
  un permiso de sesión concedido antes para otra cosa**: la guarda va delante del bypass.
- **Checkpoint antes de ejecutar** el comando aprobado (el snapshot de rollback de `slb`, sobre el repo
  sombra que ya teníamos).
- **Recibos encadenados por hash** (`franken_engine` en pequeño): cada decisión ≥ CAUTION deja un registro con
  `prev_hash`, y `verify_chain()` detecta cualquier edición retroactiva. Bypass en 3 niveles: allowlist con
  caducidad y motivo, variable de entorno de un solo uso atada al hash, y la tarjeta de aprobación.
- Modos `off` / `observe` / `enforce` (por defecto `enforce`), `/api/command-guard/*` y la tool MCP
  `guard_explain` para que un coordinador pueda consultar un comando antes de despacharlo.
- **Verificado en vivo**: `rm -rf ./tmp_prueba_guard` → tarjeta con fingerprint → aprobar → checkpoint →
  ejecutado **una vez** → dos recibos (`blocked`, `approved`) con la cadena íntegra;
  `git push --force-with-lease` sale SAFE y `dd of=/dev/sda` CRITICAL.

### 23.3 Memoria que aprende de los resultados y olvida sola
Lo que había (AGENTS.md, memoria del proyecto) es **estático y escrito a mano**. Esto son las dos capas del
informe, que son capas y no alternativas: `eidetic_engine` (el almacén explicable) + `cass_memory` (la
síntesis de reglas accionables).

- `src/memory_engine.py`: SQLite propio con **cuatro niveles de vida media distinta** (working 1 d, episodic
  30 d, semantic 180 d, **procedural que solo decae por contradicción**), **trust class** por origen
  (human_explicit .85 / agent_validated .65 / agent_assertion .50 / legacy_import .30), evidence spans que
  apuntan al chat de origen, y el scoring del original: `0.5^(días/90)` con el **daño pesando ×4**.
  Recuperación híbrida **0.45 léxico + 0.45 semántico + 0.10 grafo** con **degradación explícita**
  (sin modelo de vectores se renormaliza a solo-léxico; nunca un error), y `pack()` determinista con
  presupuesto de caracteres.
- `src/memory_curator.py`: **100 % determinista, sin LLM** — dedupe por similitud, conflictos, escalera de
  madurez candidate → established → proven → deprecated, poda, y la jugada que da nombre a la idea: si
  `harmful_ratio > 50 %` con al menos 3 señales, **la regla se invierte en anti-patrón** (`AVOID: …`).
- **El bucle de aprendizaje**: el bloque de reglas se inyecta en el prompt del agente, se anota **qué reglas
  entraron en el turno**, y cuando el turno termina con verificación real (tests del proyecto / veredicto del
  arnés) se apunta `helpful` o `harmful` a esas reglas. Sin señal, nada — no se inventa feedback.
- Superficies: tool `memory_rules`, `/api/memory-engine/*`, MCP `memory_pack`, y la pestaña **Rules** en Brain
  (nivel, madurez, trust, score, filtros, 👍/👎, "Run curator").
- **Verificado en vivo**: tres 👎 sobre una regla la convirtieron en anti-patrón y `qwen3.5:9b`, en el turno
  siguiente, la citó como tal y por su id.

### 23.4 Robot mode, TOON y el envelope estándar — y el fallo que solo aparece midiendo
`src/toon.py` (key folding, arrays tabulares con cabecera, `decode` que round-trippea todo),
`src/robot_envelope.py` (`{ok, data, error_code, error, elapsed_ms, schema_version}`, la forma de
`frankenterm`) y `?robot=1` / `?format=toon` en las lecturas que consume una máquina (dispatch, objetivos,
memoria, guarda, uso del sistema). La respuesta **sin parámetros es byte-idéntica** a la de antes, con test.

Lo interesante es el fallo: la primera versión pasaba el payload de la UI por TOON y, **medido contra la
7001, salía MÁS grande** (ratios 1.15–1.28) — los arrays no eran tabulares porque cada fila llevaba listas
anidadas, y la indentación costaba más que las llaves de JSON. El ahorro de TOON vive en su forma tabular, así
que robot mode no debía re-codificar el payload del navegador sino **proyectarlo** a filas planas de solo
escalares (`src/robot_projection.py`), que es lo que "robot mode" significa en el original. Tras el arreglo,
medido en vivo: **memoria 0.29, objetivos 0.37, guarda 0.44, uso del sistema 0.47** (53–71 % menos). La
proyección es lossy a propósito y la respuesta normal sigue trayéndolo todo.

### 23.5 Cuarteto de fiabilidad
- **Detector de convergencia** (`src/convergence.py`, de `automated_plan_reviser_pro`) con la fórmula exacta
  `0.35·tendencia_de_tamaño + 0.35·velocidad_de_cambio + 0.30·tendencia_de_similitud` y las bandas 0.75 / 0.50.
  `fix_rounds` deja de ser un contador fijo y pasa a ser un **máximo**: el bucle para solo cuando las rondas
  dejan de cambiar algo (`stopped_by: convergence`), y por eso el tope sube de 2 a 4 mientras el detector está
  activo. Apagado, el comportamiento es idéntico al de antes (con test que lo fija).
- **Outcomes de cuatro valores** (`src/tool_outcome.py`, de `fastmcp_rust`): `success / expected_error /
  cancelled / panic`. Un worker que **para el usuario ya no cuenta como fallo** en el resumen del turno, y un
  bloqueo por política es un error esperado, no un pánico.
- **Timeout de idle adaptativo** (`src/adaptive_timeout.py`, de `claude_code_agent_farm`): 3 × la mediana de
  los ciclos recientes, acotado a [30, 600]. Con un matiz que la fórmula cruda no tenía: aquí **solo puede
  alargar** el watchdog, nunca acortarlo — matar una compilación silenciosa es peor que esperar de más.
- **StdioProtectionWrapper** (`src/stdio_guard.py`, de `ultimate_mcp_client`): un `print()` despistado del
  código de la app corrompía el stream JSON-RPC de un servidor MCP stdio. El guard redirige stdout a stderr
  mientras hay sesión, es reentrante y se activa **dentro** de `stdio_server()` (fuera desviaría el propio
  protocolo). Puesto en los cinco servidores stdio.

### 23.6 Cómo se hizo y qué queda
Método: subagentes en worktrees con propiedad **disjunta** de ficheros y un contrato escrito por feature,
luego linearizado con merge + cherry-pick, parches al PC y **verificación en la instancia 7001 con el
navegador integrado y modelos locales de verdad** — que es donde apareció el fallo de TOON, que ninguna suite
de tests con fixtures sintéticos habría encontrado.

Suite completa tras la tanda: **8.231 en verde** (2 fallos preexistentes del entorno). Pendiente del informe:
grafo de conocimiento 2D como vista de auditoría (G2), agentes especializados con corpus propio (G3, la más
diferencial), `wait-for`/`events` como primitivas de orquestación, búsqueda de dos niveles e importación de
historiales de ChatGPT/Claude/LM Studio, torneo multi-modelo con fusión, el paso `prove`, y el ballast de disco.


## 24. Los expertos con corpus propio y el grafo que explica (03-09-2026, tarde)

Las dos ideas de Luis que quedaban del informe de `D:\LocalAi\inspiration\`: la que él marcó como
más diferencial (agentes especializados con su propio corpus) y la que el propio informe recomendaba
**acotar** (el grafo de conocimiento).

### 24.1 Agentes especializados con corpus propio (G3, fase 1)
Un corrector narrativo con los libros de guía; otro con los apuntes del máster. En local gana por tres
motivos que no son opinables: los PDFs no salen de la máquina, no hay límite de subida, y el corpus se
edita y se reindexa en caliente.

- **El experto** (`services/experts.py`): `DATA_DIR/experts/<slug>/` con `EXPERT.md`
  (frontmatter + instrucciones + **rúbrica**: sin rúbrica un corrector local divaga), `corpus/` con
  los ficheros que el usuario suelta, `index.json` con los chunks y `usage.json` con los contadores.
- **Procedencia por página**: cada chunk sabe de qué fichero y de qué página sale. Cuando la librería
  no puede dar la página, el chunk queda con `page: null` y `page_confidence: "unknown"` — **nunca se
  adivina un número**. `pypdf` extrae texto pero no rasteriza, así que el renderizado de la página se
  ofrece solo si PyMuPDF (ya opcional para el visor de PDF) está instalado, y si no la respuesta lo
  dice y enlaza el fichero, en vez de añadir una dependencia por la puerta de atrás.
- **Búsqueda de dos niveles con degradación explícita** (`frankensearch`): BM25 siempre, más vectores
  fusionados por **RRF `Σ 1/(60+rank)`** cuando los hay. Sin ChromaDB se sirve solo-léxico con
  `degraded: true`; **nunca un error**. Medido en vivo con ChromaDB caído: `tier: "lexical"`,
  `degraded: true`, resultados correctos.
- **Las correcciones son deltas tipados por span**, no prosa reescrita (`brenner_bot` aplicado a la
  narrativa): `{op, span, quote, replacement, rationale, rule, severity, citations, anchored, label}`.
  Los offsets de un modelo local no son de fiar, así que el span **se valida contra su cita literal** y
  se relocaliza cuando la cita aparece una sola vez; si aparece varias o ninguna, la corrección se
  **rechaza con su motivo** y se muestra — un `EDIT` sin cita no toca la prosa de nadie.
- **La regla de honestidad, que es el punto entero**: una corrección solo puede decir que viene del
  corpus si el chunk citado la sostiene, comprobado en tres capas de barato a caro (`mindmap-generator`)
  y **sin llamar a ningún LLM**. Si cita un marcador que no estaba en el bloque, o el chunk no la
  sostiene, sale etiquetada **"model's opinion, not the corpus"**. No se descarta —el usuario puede
  quererla— pero no puede disfrazarse de autoridad.
- **Story bible** (`src/story_bible.py`): personajes, cronología y hechos establecidos como estado
  estructurado, con detección de contradicciones léxica y conservadora. Es lo que ni ChatGPT ni Claude
  hacen: te corrigen la frase, no te avisan de que el personaje tenía los ojos verdes en el capítulo 3.
- Superficies: página **Experts** (galería, editor, corpus, reindex, búsqueda), panel de revisión con
  control de cambios Accept/Reject, `@expert:<slug>` en el compositor, y la tool `expert_review`.
- **Medido en vivo** (corpus de un manual de estilo, `qwen3.5:9b`): el modelo propuso
  *"Marta caminaba lentamente hacia la puerta"* → *"Marta se arrastraba hacia la puerta"*, que es
  literalmente lo que dice el capítulo 3 del corpus. El sistema **le relocalizó el span** (sus offsets
  estaban mal) y aun así la marcó como **opinión del modelo**, porque citó un marcador que no existía.
  La corrección era buena y la etiqueta era correcta: el corpus no la respaldaba *como fue citada*.
- Fase 2 (LoRA para la voz y el criterio) sigue pendiente a propósito: necesita cientos de pares
  texto→corrección aceptada que solo genera el uso. Meter los PDFs en un fine-tune para "aprendérselos"
  es la forma más cara, lenta y alucinógena de hacer lo que el RAG hace mejor.

### 24.2 El grafo de procedencia (G2), acotado como manda el informe
El veredicto del informe era que el 3D es escaparate y que **el grafo paga cuando las aristas son
verdad de terreno, no cuando las inventa un LLM** — con el dato duro de que `eidetic_engine_cli` pondera
su propio grafo con **0.10** frente a 0.45 léxico y 0.45 semántico. Así que: **2D, aristas declaradas,
y vendido como vista de auditoría**.

- `src/provenance_graph.py` construye el grafo **solo** de lo que ya estaba almacenado: dependencias
  declaradas entre objetivos, evidence spans de la memoria, el `inverted_from` que escribió el Curator,
  los ficheros que cada checkpoint cambió, las citas de corpus, y duplicados **verificados
  literalmente**. Cada arista lleva un `why` en una frase, porque el objetivo es que el usuario pueda
  preguntar por qué está ahí. No hay ni una arista que haya afirmado un modelo, y el hueco para las
  inferidas queda documentado pero vacío.
- `src/text_overlap.py`: q-gramas → winnowing → fingerprints → voto por diagonal → **verificación
  literal del span** (`franken_overlap`). Posicional, sin embeddings, y nunca reporta un span que no
  haya comparado carácter a carácter.
- Lo que da: **`explain`** (la cadena de evidencia paso a paso: por qué el agente cree esto),
  **`impact`** (qué se rompe si tocas esto), huérfanos y duplicados, y una señal de ranking **acotada a
  [0, 0.10]**, con el porqué de ese tope escrito en el docstring.
- La página es 2D, dibuja como mucho 200 nodos elegidos por grado y **dice "showing 200 of N — narrow
  the filter"** en vez de pintar una nebulosa ilegible.
- **Medido en vivo** sobre los datos reales de la instancia: 25 nodos y 18 aristas del historial de
  trabajos; `explain(OBJ-1)` devolvió *"OBJ-1 was EDITed from this chat session on 2026-09-03 — La API
  de objetivos ya está cimentada y verificada en vivo"*, e `impact(OBJ-1)` = OBJ-2, OBJ-3, OBJ-5. Y
  cuando una fuente no está, `sources` dice cuál y por qué (*"no project with a bound folder was
  given"*, *"Faustus stores no review records"*) en lugar de fingir un grafo vacío — la postura
  anti-mock de `vibe_cockpit`.

### 24.3 De paso, dos cosas que el uso destapó
- **`@expert:corrector` se reportaba como fichero inexistente**: la regex de menciones no tiene `:` en
  su clase de caracteres, así que casaba la palabra suelta `expert` y el resolvedor la listaba en
  *missing* — una mención que el usuario había escrito bien, culpándole a él. Ahora las menciones con
  espacio de nombres se reconocen y el resolvedor de ficheros las ignora; `@expertos/notas.md` sigue
  siendo una ruta.
- **Una revisión devolvía spans sin el texto al que apuntan**, así que el panel tenía que pedirle al
  usuario que pegara su propia prosa otra vez. `review()` ya lleva `text`; `compact_result()` lo quita,
  porque devolverle al modelo la prosa del usuario es justo lo que esa forma compacta existe para evitar.

### 24.4 Sobre los 25 fallos de la suite en Windows
La suite completa en el PC dio 25 fallos y en Linux 2. Comprobado con dos worktrees limpias
(commit base `2fe3acb` y HEAD, ambas sin `data/`): **fallan exactamente los mismos 12**, así que ninguno
es regresión. Los otros 13 aparecen solo en el árbol de Luis y se reproducen **igual en el commit base**
apuntando a una copia de su `data/`: son dependientes de sus datos locales, no del código. (No es el
`default_model`: limpiarlo no los arregla.) En Linux la suite completa queda en **8.427 en verde** con
los 2 fallos de entorno conocidos.


## 25. Esperar por una condición, y un torneo entre modelos (03-09-2026, tarde)

Las dos piezas que quedaban del Tier 2 del informe, y el fallo de coherencia que aparecieron al probarlas.

### 25.1 `wait-for` y eventos en vivo (`frankenterm`)
La regla del original es **bloquear por una condición, no por un `sleep`**, y **leer el estado de un worker de
su propia salida** en vez de configurarlo a mano. La lista de pendientes del fork decía exactamente eso: el
tablero de sub-agentes solo aparecía al terminar el trabajo.

- `src/output_rules.py` clasifica los últimos 8 KB de la salida de cada worker en
  `rate_limited / waiting_for_input / stuck / auth_error / disk_full / oom`, con substrings primero y regex
  solo para el pack que ya casó, y **devuelve la línea que hizo saltar la regla**: el tablero dice *por qué*
  cree que un worker está atascado en vez de afirmarlo. Un worker así **se reporta, nunca se mata** — la
  política de `srps` que ya habíamos adoptado.
- `wait_for(job, condition, timeout)` acepta `done`, `phase:<n>`, `worker:<label>:<estado>`, `event:<texto>` y
  `changed`. Resuelve por `asyncio.Event` que la propia ruta de progreso del trabajo despierta, **sin ningún
  bucle de espera dentro**; los tests miden el tiempo transcurrido, así que una implementación por polling
  los suspende. Un timeout devuelve `met: false`, no un error (el mismo criterio que los outcomes de cuatro
  valores). El estado es no-pegajoso para mostrar y pegajoso para esperar, para que una condición no se
  pierda porque el estado envejeció fuera de la ventana.
- `/api/dispatch/{id}/events?stream=1` emite SSE en vivo con latido cada 15 s y una trama final; **la
  respuesta sin parámetros sigue siendo byte-idéntica** (con test). La página Workers se llena en directo y
  vuelve al sondeo de siempre si el stream falla, se apaga por ajuste o lo corta un proxy.

### 25.2 Torneo multi-modelo con fusión explícita
El protocolo del original: mismo prompt a N modelos **a ciegas y en paralelo** en la ronda 0, luego rondas
donde cada modelo ve todas las respuestas con la instrucción de *tomar lo mejor de todas cuando sea
complementario, no conflictivo*, y un juicio con tres métricas 0–100.

- Las respuestas viajan **anonimizadas**, y no solo sin etiqueta: si un modelo local abre con "Como Qwen…",
  ese nombre se borra del texto, porque si no filtra su identidad por su propia prosa.
- **Respeta lo que medimos en esta máquina** (§20): dos peticiones al mismo modelo van en serie, dos modelos
  distintos generan a la vez. Un lock por modelo y el semáforo de GPU compartido, **en ese orden** — al revés
  hay interbloqueo en cuanto una tarea tiene la última ranura y espera un lock que otra sostiene esperando
  ranura.
- Para antes con el **detector de convergencia** de §23.5: `rounds` es un máximo, y hace falta que *todos* los
  modelos hayan convergido, no la media — un modelo asentado no debe cortar una ronda que los demás siguen
  aprovechando.
- Un juicio mal formado no se rellena: esa nota queda en `null` y el orden pasa a un desempate determinista
  **etiquetado como tal** (`ranking: judge | mixed | deterministic`), porque llamar "juzgado" a medio juicio
  sería mentir. Un modelo que falla o se cancela no tumba el torneo.
- La página muestra una tarjeta por modelo llenándose por rondas, la tabla ordenada con las tres notas, y un
  botón **Merge** que arma el prompt de síntesis y lo deja en el compositor.
- **Probado en vivo**: `qwen3.5:9b` contra `qwen3-coder:30b`, ronda 0 arrancando ambos en el mismo instante,
  dos rondas, juez real (100/85/90 frente a 100/85/85) y `ranking: judge`.

### 25.3 Los dos endpoints SSE hablaban dialectos distintos
Al abrir el stream del torneo con un `EventSource` normal no llegaba nada, mientras el mismo código contra
`/api/dispatch/{id}/events` funcionaba. La causa es una regla del protocolo que es fácil no ver: una trama con
línea `event: <nombre>` **no llega nunca a `EventSource.onmessage`**, solo a un listener registrado para ese
nombre exacto. Dispatch mandaba tramas sin nombre más una final `event: end`; el torneo nombraba todas
`event: event`. Dos endpoints SSE en la misma app discrepando en eso significa que una página escrita contra
uno es sorda al otro. Unificado al dialecto de dispatch. De paso: la página del torneo **no abría el stream
que ella misma traía** — sondeaba cada 1,5 segundos —, y ahora lo sigue con el mismo fallback con pestillo que
usa la de Workers.

### 25.4 Una trampa de herramientas diagnosticada, y por qué NO se arregló
Bisecando los fallos de la suite en Windows (§24.4) hasta la carpeta culpable —`data/skills/`, una sola
skill— salió la causa concreta: con esa skill presente, un turno **sin documento abierto ofrece
`suggest_document` y ese mismo turno lo rechaza** con *"Open the exact document to edit, then request this
action again so its id and version can be sealed"*. Es exactamente la trampa que la alarma
`[tool-coherence] OFFERED THEN BLOCKED` del propio bucle existe para cazar, y a un modelo pequeño le cuesta
una ronda entera más los tokens del esquema.

Se escribió una regla de preflight que la podaba, y **se revirtió a propósito**. El preflight corre una sola
vez al empezar el turno, y un documento puede nacer *durante* el turno (`create_document` y después editarlo):
podar ahí quitaría una herramienta legítima, y `tests/test_external_context_tool_gate.py` fija justo ese caso
—el esquema se mantiene en la mesa para que la acción se pueda expresar, y el runtime la rechaza enseñando qué
hacer—. El arreglo correcto va **en el punto de uso**, no al inicio del turno, y merece un cambio que se pueda
razonar por sí solo en vez de colarse en una tanda. Queda el diagnóstico escrito, que vale más que un parche
que rompe otra cosa.


## 26. Traer tu pasado, y no dar nada por probado (03-09-2026, noche)

Lo que quedaba del informe: la feature de migración, el paso `prove`, la recuperación tras un corte,
la salud honesta, el ballast de disco y la procedencia de lo que scrapea el navegador. Con esto el
tablero de `D:\LocalAi\inspiration\ESTADO_IMPLEMENTACION.md` queda entero en verde.

### 26.1 Importa tu pasado, y búscalo sin haber descargado nada
- `src/history_import.py` normaliza a un modelo canónico (`Conversation → Message`) en su propio
  SQLite los exports de **ChatGPT, Claude, LM Studio y del propio Faustus**. Las cinco reglas del
  módulo son las que separan un importador de un triturador de archivos: un parser que no reconoce
  el fichero **dice que no en vez de adivinar**; una conversación rota **se salta con su motivo** y
  las otras cuatrocientas entran igual; el import es **idempotente** por `(source, external_id)`;
  una fecha que no se puede leer queda en **`None`, jamás "ahora"** (estampar la hora del import
  haría que todo el archivo pareciera de hoy y corrompería cualquier orden posterior); y los
  exports grandes **se leen en streaming**, porque un `conversations.json` real pesa cientos de MB.
- Honestidad sobre las fuentes: ChatGPT y Claude están verificados contra documentación real; el de
  LM Studio está **INFERIDO y marcado como tal** en el módulo, porque su propia documentación dice
  que la estructura no es fiable. Un formato inferido que se presenta como verificado es una mentira
  que solo se descubre corrompiendo el archivo de alguien.
- `src/hash_embed.py` + `src/two_tier_search.py`: embeddings por **FNV-1a proyectado a 384 dims y
  normalizado L2** — sin modelo, sin red y deterministas entre procesos — fusionados con BM25 por
  **RRF `Σ 1/(60+rank)`**, y refinados con el embedder real cuando lo hay. Un Faustus recién
  instalado que no ha descargado nada **sigue buscando**.
- El dato incómodo, medido y documentado en el módulo en vez de escondido: con el **RRF plano del
  original**, la búsqueda de herramientas salía **peor que BM25 solo** (10/21 aciertos frente a
  13/21) — las dos vías leen los mismos tokens, así que no son independientes y RRF asume que lo
  son. La vía hash se pondera a 0.5 y la tabla de medidas está en el docstring.
- **Verificado en vivo**: un export de ChatGPT con una rama abandonada importa **solo la buena**; la
  conversación malformada se salta con su motivo; la de fecha ilegible queda con `started_at: null`;
  reimportar da 0 creadas / 2 actualizadas; y la búsqueda encuentra la conversación correcta con
  `tier: hybrid, degraded: true`, es decir sin ChromaDB.

### 26.2 `prove`: una mutación no es la finalización del objetivo
`src/prove.py` cierra el ciclo que faltaba en `/api/dispatch` (§22 ya hacía prepare → revalidate →
commit → observe). Devuelve un paquete canónico con **cuatro veredictos**, y el que importa es
`unproved`: *el trabajo pudo ocurrir y nada puede demostrarlo*. **No es un fallo**, y es un valor
distinto de `partial` y de un error — confundirlos es exactamente lo que el original prohíbe.
La lista de incertidumbre nunca está vacía cuando hay motivo (sin runner de tests, checkpoint
imposible, lista truncada, fallback por mtime, un worker cancelado), y la identidad es un SHA-256
con **prefijo de longitud en cada campo variable** antes de concatenar, así la paginación del
transporte no puede cambiarla.

Medido en vivo con un worker real: cambió el fichero de verdad, su afirmación coincidía con lo
observado en disco, y el veredicto fue **`partial` (0.65)** con
`no_verification_runner: "nada corrió que pudiera probar el trabajo"`. Esa es la respuesta honesta,
y es justo la que un sistema complaciente no daría.

### 26.3 Recuperación tras un corte, y salud que no se supone buena
- `src/crash_recovery.py` agrupa **solo por mtime** (los procesos que mueren juntos dejan de
  escribir a la vez) en la ventana `[boot − lookback, boot + slack]`, y **agrupa primero y filtra
  después**, porque filtrar antes desplaza el clúster real. El plan **refija el mismo modelo y los
  mismos parámetros** que tenía el trabajo, y nada se declara reanudado sin **sondear la tabla de
  procesos**. No reanuda solo: marca `interrupted` con el motivo. `psutil` no es dependencia
  declarada, así que la hora de arranque sale de `/proc/stat btime`, `GetTickCount64` o
  `kern.boottime`, y **si no se puede saber, la función no hace nada** en vez de adivinar.
- `src/health.py`: un componente **sin datos aporta 0**, no se le supone bien; ausencia de señal no
  es ausencia de problema. En vivo: 90/A con 6 de 7 componentes reportando y el séptimo diciendo
  literalmente *"no data source yet — nothing has reported this, which is not the same as nothing
  being wrong"*. Ningún componente inventado: solo lo que ese endpoint ya medía, más el espacio en
  disco.

### 26.4 Ballast, y procedencia de lo que el navegador trae
- `src/disk_ballast.py`: ficheros preasignados que se liberan con un `unlink` instantáneo para
  comprar margen real mientras se decide qué borrar; urgencia por EWMA + aceleración + un PID con
  las constantes del original; y scoring de artefactos re-derivables con **veto total si hay un
  `.git/` dentro**. **Nunca borra**: mueve a cuarentena con `undo`. Sale en modo `observe`, así que
  instalarlo no toca un solo byte hasta que el usuario lo active.
- `src/web_provenance.py` ancla cada bloque que el navegador entrega al modelo con su url, su rango
  de caracteres y un hash, de modo que una afirmación posterior se puede contrastar con lo que
  realmente se descargó. Y una honestidad deliberada: no tenemos el pipeline de capturas por tiles
  del original, así que el ancla es **rango + hash, no coordenada de píxel**, y el docstring lo dice
  en vez de insinuar que hacemos lo que no hacemos.
- `src/claim_verify.py` es la escalera de 5 capas de barato a caro, **sin LLM en las cuatro
  primeras**. La capa 4 —los números y las entidades de la afirmación tienen que aparecer en la
  fuente— es la que caza una cifra inventada, y **solo refuta, nunca confirma**: pasarla no es
  apoyo, o una paráfrasis con las entidades correctas se daría por probada. La capa 5 va etiquetada
  como juicio del modelo y su número **no se mezcla** con el score determinista, igual que la regla
  de honestidad de los expertos (§24.1).

### 26.5 Tres cosas que solo aparecieron usando la app
- **El importador vivía bajo `/api/history`**, donde el historial de chats ya tiene
  `GET /api/history/{session_id}`. Ese parámetro de ruta se traga a todos sus hermanos: en vivo,
  `/api/history/conversations` respondía *"Session conversations not found"*. Solo sobrevivía
  `POST /import`, porque el router viejo no tiene POST. Sus tests montaban **solo su propio
  router**, así que no podían verlo; ahora hay uno que monta los dos en el orden de `app.py`.
- **Su test de streaming era flaky**: un umbral absoluto sobre `ru_maxrss`, que es una marca de agua
  del proceso entero, así que el mismo código pasaba y fallaba en ejecuciones consecutivas. Medir la
  carga como control tampoco servía —una marca de agua no se puede leer dos veces en un proceso: la
  segunda daba ~0 y la comparación pasaba **midiendo nada**—. Ahora usa `tracemalloc`, que sí se
  reinicia, con un suelo en el control para que una comparación sin sentido falle en vez de pasar.
- **`/api/storage/*` era alcanzable por el modelo**: el mismo agujero que la auditoría de §18
  encontró en Local models. `app_api` hace loopback con el token interno, que `require_admin` acepta
  sin sesión de usuario ni tarjeta de aprobación, y esas escrituras reservan gigas, los liberan y
  **mueven ficheros del usuario** — en una máquina cuya presión de disco es justo lo que la feature
  gestiona. Un modelo que acaba de leer una web que dice "libera espacio" no debe poder actuar sobre
  ella. Bloqueadas; `GET /status` sigue abierto a propósito, porque leer qué llena el disco y qué se
  vetó es exactamente lo que el modelo debe hacer para **contárselo al usuario**.

---

## 27. El perímetro que faltaba, y agentes que se pueden cambiar de pieza (03-09-2026, noche)

Dos agujeros de perímetro encontrados en la segunda pasada por los repos de dicklesworthstone —
donde ya casi no quedaba nada que copiar— y la respuesta a lo que pediste: *"quiero que sea versátil
para que se puedan usar distintos modelos, agentes etc. Claude, qwen, openclaw, lo que sea, piezas
modulares e intercambiables"*.

### 27.1 Un fichero de instrucciones dentro de un repo es código de otro
`src/workspace_trust.py` + `routes/workspace_trust_routes.py`. `AGENTS.md`, `CLAUDE.md` y compañía
viven **dentro del repositorio que abres**, así que quien manda un PR manda instrucciones al agente.
Faustus ahora los trata como lo que son: contenido no confiable hasta que **tú** dices que sí, una
vez, por fichero y por hash. Cambia el fichero, vuelve a preguntar. No hay "confiar en todos".

### 27.2 El modelo llegaba a `/api/storage/*` por la puerta de servicio
`app_api` tiene un token de loopback interno para que las herramientas hablen con la propia app. Ese
token es suficientemente privilegiado para llegar al almacenamiento. Es el mismo agujero que §18
cerró para los modelos locales, en otra puerta. Añadido a `_APP_API_BLOCKLIST_METHOD_PATH` en
`src/tools/system.py` con un mensaje de rechazo que explica por qué. `GET /status` se deja abierto a
propósito: es información que el agente necesita y no revela nada.

### 27.3 El entorno de un hijo que no es nuestro
`src/native_env.py`. Faustus corre dentro de su propio virtualenv, así que su entorno lleva
`VIRTUAL_ENV`, un `PYTHONPATH` y un `PATH` que empieza por nuestro `bin`. **Todo** subproceso que
hereda ese entorno resuelve `python`, `pip` y sus imports contra *nuestro* venv en vez del suyo: los
tests del proyecto del usuario, un runner externo, un CLI en python. El síntoma es el peor de todos
—funciona en la máquina del que lo programó y importa el paquete equivocado en la del usuario.
`native_host_environment()` quita las siete marcas del venv y las entradas de `PATH` que caen dentro
de él, conservando orden y separador, y devuelve el `PATH` original si fuera a quedarse vacío (un
hijo sin `PATH` no arranca: un venv filtrado es mejor que un exec roto).

La distinción que hay que acertar: los hijos **nuestros** —los MCP builtin, `host_python()`— deben
seguir heredando el venv, porque ahí es lo correcto. Aplicarlo a ciegas rompe la app; el script de
cookbook lee `$VIRTUAL_ENV` en tiempo de ejecución para encontrar las wheels de CUDA. Está aplicado
en `workspace_checkpoints.py` (donde `git commit` dispara el **pre-commit hook del usuario**, que
suele ser python suyo) y la tabla completa de sitios —aplicado / omitido a propósito / pendiente—
está en el mensaje del commit. El de mayor valor pendiente es `src/project_tests.py`.

### 27.4 No hacer un commit automático en un repo a medias
`src/git_invariants.py`. Un auto-commit que entra en un repositorio en mitad de un rebase, un merge
o un cherry-pick es destructivo y silencioso. `check_preconditions()` informa de **todos** los
problemas, no del primero: no es un work tree, hay una operación en curso, `HEAD` está desatado, el
remoto o la rama no son los esperados. `canonical_git_remote()` reduce las grafías ssh/https/scp a
`host/owner/repo` y **mantiene un alias ssh como host** —tu propio remoto es `git@Luissalet:…`, así
que esto no es hipotético. Cuando falla, se **rechaza** el commit y se enseña por qué. No hay flag
para saltárselo.

### 27.5 Piezas intercambiables: cualquier agente como worker
`src/agent_runners.py`, `src/external_worker.py`, `routes/agent_runner_routes.py`,
`static/js/agentRunners.js`. El catálogo **no está escrito a mano**: se parsea del `ollama launch
--help` que tengas instalado, así que OpenClaw, OpenCode, Hermes, Droid, Pi, Cline, Copilot CLI y
Oh My Pi aparecen si los tienes y desaparecen si no. Un `dispatch` puede nombrar un runner y el
worker externo corre con él; lo que ese runner **no permite comprobar** entra en el paquete de
`prove` como incertidumbre declarada en vez de darse por bueno.

## 28. Deep research que se puede citar, y sacarlo en md, docx o pdf (03-09-2026, noche)

Comparaste nuestro deep research con un informe de ChatGPT Deep Research y la diferencia no era la
longitud: era que **cada afirmación del suyo se podía seguir hasta una fuente**, y el nuestro no.

### 28.1 Citas numeradas que alguien comprueba
`src/research_citations.py` — determinista, sin LLM, sin red. Un `SourceRegistry` numera cada página
**la primera vez que se ve** y no la renumera nunca, así que una cita escrita en la ronda 2 sigue
resolviendo en el informe final. La identidad de una URL se normaliza (esquema y host en minúsculas,
puerto por defecto, barra final, `#fragmento`, y una veintena de parámetros de rastreo: `utm_*`,
`fbclid`, `gclid`), así que la misma página vista dos veces es un solo número. `www.` **no** se
quita: hay hosts que sirven contenido distinto, y una fusión falsa atribuye una afirmación a la
fuente equivocada sin decirlo.

Lo importante no es que el modelo escriba `[n]`: es que **después alguien lo comprueba**.
`repair_citations()` borra los marcadores colgantes —un `[7]` cuando solo hay 5 fuentes— en vez de
dejar la mentira en el texto, funde las dos gramáticas de cita en una, y añade una sección de
**Fuentes solo con las que realmente se citan**. Es idempotente. Nunca inventa una cita: un párrafo
sin cita se queda sin cita, y la cifra de cobertura lo dirá.

### 28.2 Gradar la evidencia sin mentir sobre lo que se ha gradado
`grade_claims()` no reimplementa nada: llama al `src/claim_verify.py` de §26, la escalera de cinco
capas de barato a caro. Capa 1/2 → `alta`, capa 3 → `moderada`, lo demás → `débil`.

Y aquí está la regla de honestidad, que es el sentido de todo el apartado: **la nota dice si la
fuente citada sostiene la frase, no si la frase es verdad en el mundo.** El informe de referencia se
gana las palabras "evidencia alta" del diseño de los estudios; nosotros no podemos y no vamos a
fingir que sí. Por eso la leyenda del informe **la genera python con los recuentos reales**, no el
modelo: una leyenda escrita por el modelo es el modelo opinando sobre su propia fiabilidad.

Consecuencia incómoda que se documenta en vez de esconderse: el umbral de la capa 3 es 0.75, así que
una frase cierta y bien parafraseada cae a menudo en `débil`. Es exacto para lo que medimos —¿dice
esto el extracto que guardamos?— y hay que leerlo así.

### 28.3 El informe responde a *tus* preguntas, en *tu* idioma
`_extract_subquestions()` saca las preguntas del prompt (determinista primero: saltos de línea,
viñetas, numeración y `?`; el LLM solo como último recurso) y el informe final exige **una sección
por pregunta, en tu orden**. Tu prompt de fisioterapia era una lista numerada y esa forma ahora
sobrevive hasta el índice. `detect_language()` decide el idioma por reparto de stopwords sobre
es/en/fr/de/pt/it y se pasa explícito a los prompts: se acabó que una pregunta en español devuelva
un informe en inglés.

Los prompts piden además tablas comparativas con una columna de "qué significa en la práctica",
llamadas `> **Implicación práctica:**`, y cifras siempre en la misma frase que el estudio que las
produjo.

### 28.4 Sacarlo de la app: md, docx, pdf (y html, txt, json)
`src/report_export.py` + `GET /api/research/export/{id}?format=`. **No se ha escrito ni un
renderizador**: el informe se convierte en bloques con el `markdown_to_blocks` que ya existía y se
entrega al pipeline de exportación de conversaciones (`chat_export`, `chat_export_docx`,
`chat_export_pdf`). Título, línea de metadatos, cuerpo, apéndice de fuentes —omitido si el cuerpo ya
trae el suyo— y pie. La ruta copia **exactamente** la puerta de propiedad del resto: 404, nunca 403,
para no filtrar que el informe existe. `GET /api/research/export-formats` dice qué formatos se
pueden producir ahora mismo, para no ofrecer una descarga que va a fallar.

### 28.5 Dos parches del deep research de Diogenes
- Si falla el planificador, el plan de reserva es **determinista** (las preguntas extraídas) y el
  aviso dice qué se ha degradado. Una ejecución degradada honesta es mejor que una silenciosa.
- Si la extracción devuelve `summary` vacío pero `evidence` con contenido, **el hallazgo se
  conserva**: la página ya se ha pagado, tirarla es tirar el trabajo.

### 28.6 Firecrawl autoalojado, portado de Diogenes
Diogenes es un fork del mismo upstream, así que su `services/search/providers.py` es el nuestro más
un bloque de Firecrawl: esto es un **port**, no una reescritura. `_get_firecrawl_instance()` no tiene
fallback a la API hospedada **a propósito**, y se mantiene: caer en silencio a `api.firecrawl.dev`
mandaría las búsquedas de un usuario local-first a un tercero. Si el appliance no responde, el deep
research vuelve al fetcher nativo con un aviso que dice por qué; una investigación no se muere
porque un servicio esté caído. La clave hereda el tratamiento de secreto de las demás por sufijo
`_api_key`, sin cableado nuevo.

### 28.7 No reinventar: lo que se borró
Tres agentes en paralelo escribieron cada uno un ayudante que el árbol ya tenía. Corregido:
`detect_language` estaba duplicado (la copia del harness delega ahora en la buena: el inglés no
cambia en 34/34 casos y el español pasa de 8/26 a 23/26 aciertos), el patrón de bloques de código
estaba escrito dos veces en el mismo `visual_report.py`, y los serializadores de bloques de
`chat_export` tienen ya nombre público en vez de importarse por debajo.

Y dos duplicados que **se han dejado a propósito, con la prueba**: los dos partidores de frases no
son la misma función —forzar el de `story_bible` en el informe cambiaba la cobertura impresa de 6 a
5 de 8 y partía `p. ej.` en fragmentos incitables—, y el escáner de zonas protegidas del linkificador
es más débil, no más fuerte, en lo que comparten: sobre un informe cortado a mitad de un bloque de
código, inventaba `[1] [2] [3]` a partir de `rows[1]`, `cols[2]`, `cols[3]`. Eso es exactamente la
fuente inventada que todo el apartado 28 existe para impedir.

## 29. Buscador sin límite, y lo que enseñó una ejecución de verdad (03-09-2026, noche)

Todo lo de §28 se verificó en el 7001 con `qwen3.5:9b` contra fuentes reales. Lo que sigue es lo que
esa ejecución enseñó, que no se podía saber leyendo el código.

### 29.1 DuckDuckGo no es un buscador, es un scrape
La segunda investigación seguida murió con «Search engine unavailable». DuckDuckGo no tiene API
pública: se le raspa el HTML, y corta en cuanto haces dos seguidas. Era el proveedor por defecto de
facto solo porque es el único que no necesita configurar nada.

La respuesta no era añadir un proveedor —ya había siete— sino **levantar el que ya estaba definido**:
SearXNG en el `docker-compose.yml` del propio repo, fijado a una versión concreta y con la API JSON
activada. Corre en la máquina, agrega decenas de motores, sin clave y sin cuota. Con Firecrawl
(§28.6) al lado queda el par que Diogenes hizo canónico: **SearXNG descubre, Firecrawl lee**.

Medido, misma pregunta, mismo modelo: DuckDuckGo dio **10 URLs en 2 rondas**; SearXNG dio **36 en 3**.

Nota de operación, porque costó una hora: Docker Desktop arrancado desde un shell con el entorno
recortado falla con `unable to get 'ProgramData'` y luego con rutas `unix://C:\...` mal formadas. No
es Docker: es un hijo heredando un entorno roto de su padre — **exactamente la clase de fallo que
`native_host_environment()` (§27.3) existe para impedir**, encontrada por accidente y desde el otro
lado. Se arranca como lo haría un doble clic y funciona.

### 29.2 Los cuatro defectos que solo aparecen ejecutando
1. **La gradación medía lo que no era.** 51 de 57 citas salían «débil» en un informe visiblemente
   bien documentado. La causa es estructural: el modelo escribe en español, varias fuentes están en
   inglés, y las capas de `claim_verify` buscan cifras, nombres propios y un 0.75 de solape de
   tokens. Una paráfrasis traducida no pasa ninguna. Ahora se comprueba **solo lo que se puede
   comprobar entre idiomas —las cifras— y se dice que el resto no se comprobó**, que es distinto de
   decir que es débil. Tres resultados en vez de una escala: cifras en la fuente / cifras ausentes
   de la fuente / sin comprobar. En la ejecución de verificación: **9 confirmadas, 3 con cifras que
   no están en la fuente que citan, 49 sin comprobar**. Esas 3 son la señal que importa, y la escala
   vieja las enterraba dentro de «débil: 51».
2. Se coló texto del prompt como encabezado: `## Evidence For and ## Evidence Against`. Ningún
   prompt de categoría lleva ya un `#`, y eso es lo que comprueba el test.
3. Encabezados en inglés en un informe en español, y una categoría mal detectada («factcheck» para
   una pregunta abierta) que imponía su esqueleto encima del del usuario. Regla nueva: **si hay
   subpreguntas explícitas, la categoría no manda**. Las preguntas del usuario ganan.
4. La primera «subpregunta» era la pregunta entera, y los `1)` `2)` `3)` del usuario salían
   renumerados encima de los nuestros. Ahora la pregunta principal es lo que va **antes** del primer
   marcador, y `el grupo 1) tuvo menos dolor` sigue siendo prosa, no una enumeración.

Verificado tras el arreglo, misma forma de pregunta: encabezados `## 1. ¿Qué sabemos...?` a
`## 4. ¿Para quién está contraindicado?`, todo en español, sin esqueleto de categoría encima.

### 29.3 Un informe apoyado en dos páginas se lee igual que uno apoyado en siete
El hallazgo más incómodo, y solo visible midiendo: el modelo recibió **7 fuentes que él mismo había
leído, escribió 75 marcadores y usó 2**. Todos los marcadores resuelven, así que la comprobación de
citas pasa y no dice nada. Pero un informe que descansa en dos páginas es otra cosa que uno que
abarca siete, y en la prosa no se distingue: los números se ven igual.

Dos cambios, y ninguno puede fabricar una cita. El prompt dice ahora que cada fuente numerada se
buscó y se leyó **para esta pregunta**, así que donde una fuente posterior cubra mejor una sección
hay que citarla ahí — y en la misma frase, que una fuente sin nada que aportar se deja fuera en vez
de citarse en vacío, porque inflar la cuenta es peor que tenerla baja. Y la leyenda imprime la
amplitud, **pero solo cuando se queda corta**: un informe que usó todo lo que reunió no dice nada,
porque la línea sería ruido.

### 29.4 El cromo de chat en un documento
El PDF abría con «1 message · Exported…» y una barra gris que decía «Report». Los renderizadores de
docx y pdf son los del export de conversaciones, y anunciaban un mensaje y su rol. Un `Transcript`
puede marcarse ahora como documento y ambos se saltan esa parte. Lo demás no se toca: se comprobó
**byte a byte** que una conversación exportada sale idéntica antes y después, con las dos huellas
(zip por contenido de miembros, PDF con `invariant`).

### 29.5 Lo que no se hizo, y por qué
De la segunda tanda de Diogenes, la mitad ya estaba: el **auditor de skills** es byte a byte el
nuestro; el `reconnect()` de ChromaDB es una añadidura **nuestra** que ellos no tienen; el guardado
de un secreto redactado ya está protegido por tres hechos separados (los admins reciben los ajustes
sin redactar, el POST es solo-admin, y el POST es un patch que ignora las claves ausentes) — se
añadió el test de regresión igualmente, y se comprobó que falla si se rompe cualquiera de los tres.
`download_models.py` es para los motores nativos de Diogenes y no aplica.

Lo que sí faltaba y se portó: la **lane de embeddings implícita** (una lane personalizada existe solo
si el operador guardó un endpoint, no porque `EmbeddingClient` tenga un Ollama por defecto), el
**oráculo de salida** (`src/output_oracle.py`: un paso declara qué debe contener su salida **al
crear el plan**, y si falta el código de salida se fuerza a 65 — `output_matched` es `None` cuando no
se declaró nada, que es «sin comprobar», no «pasó»), y **no matar un proceso que no arrancamos
nosotros**: `_kill_tree` mataba por pid sin preguntar si el pid seguía siendo de nuestro hijo, y en
la ruta de cancelación el proceso suele haber terminado ya —un pid reciclado se lleva por delante un
árbol ajeno con `/T`. Ahora se autoriza por objeto vivo y hora de creación, sin bandera para
saltárselo, porque quien llama aquí es un modelo y una bandera que un modelo puede poner no es una
salvaguarda.

## Cómo mantener este documento
Cada bloque de trabajo añade una sección (fecha, qué, por qué, ficheros, cómo se verificó, cifras) y actualiza las cifras de cabecera (`git log --oneline c9dd68d8..HEAD | wc -l`, `git diff --stat c9dd68d8..HEAD`). Los commits del fork llevan mensajes largos que explican el porqué: `git log c9dd68d8..HEAD` es la fuente detallada.
