# Faustus — qué añade este fork sobre Odysseus

**Faustus** es el fork personal de Luis de [Odysseus](https://github.com/odysseus-dev/odysseus) (interfaz local tipo Cowork sobre Ollama). Este documento es el registro vivo de **todo lo que Faustus añade o cambia respecto al Odysseus original**: se actualiza con cada bloque de trabajo, sirve de changelog del fork y de material para el currículum (qué se construyó, por qué, cómo se verificó).

- Base del fork: commit upstream `c9dd68d8` (27-08-2026, "refactor(docs): separate Pages site source").
- Ramas: `feat/projects` (principal, `D:\LocalAI\odysseus`) y `feat/reliability` (desarrollo, worktree `D:\LocalAI\odysseus-dev`, instancia de pruebas en el puerto 7001). La rama de desarrollo se fusiona en la principal por fast-forward.
- Cifras a 31-08-2026 (18:05): **107 commits**, 339 ficheros tocados, **+23.049 líneas** (−816); 25 módulos nuevos (14 de backend, 3 de rutas, 8 de frontend) + `scripts/faustus_rename.py`, 31 ficheros de tests nuevos. Suite completa: **6.028 tests en verde en Windows** (partía de 178 fallos ambientales) y 6.081 en Linux; e2e Playwright **10/10** en Windows y Linux.
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

## Cómo mantener este documento
Cada bloque de trabajo añade una sección (fecha, qué, por qué, ficheros, cómo se verificó, cifras) y actualiza las cifras de cabecera (`git log --oneline c9dd68d8..HEAD | wc -l`, `git diff --stat c9dd68d8..HEAD`). Los commits del fork llevan mensajes largos que explican el porqué: `git log c9dd68d8..HEAD` es la fuente detallada.
