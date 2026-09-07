# Pendientes — fallos conocidos, cosas a revisar y deuda declarada

Este fichero existe porque hay una diferencia entre *"no lo hemos hecho"* y
*"lo hemos hecho y no funciona"*, y la segunda hay que poder encontrarla.
Cada entrada dice **qué pasa**, **cómo se sabe** y **qué costaría**. Lo que no
se pueda verificar se marca como no verificado en vez de darse por bueno.

Convención: `[!]` rompe algo hoy · `[?]` no verificado · `[~]` deuda aceptada
a propósito · `[+]` mejora pendiente.

La deuda y los riesgos del overhaul de interfaz viven aparte, en
`docs/ui/PENDIENTES_UI.md` (rama `feat/studio-ui`).

---

## Lo primero que hay que mirar

- `[?]` **Jarvis: validación con micrófono real pendiente (07-09-2026).** La implementación de voz
  está en `docs/design/voice-jarvis.md`: Whisper local, inglés/español, síntesis nativa
  de Windows, esfera reactiva, revisión de transcripción e interrupción manual.
  Los 20 tests específicos, TypeScript y compilación pasan. La revisión visual
  de escritorio/móvil cerró tres hallazgos. Falta comprobar micrófono físico y conversación completa con
  el LLM. Activación por palabra y barge-in acústico son mejoras pendientes,
  no capacidades incluidas en esta entrega inicial.

- `[x]` **La referencia histórica de 44 tests rojos ya no describe esta revisión.**
  La primera suite completa del 07-09 produjo 12.307 correctos, 12 fallos y 82
  omitidos. Se corrigieron el color de respaldo de la esfera y la contaminación
  de identidad de módulos causada por tests que recargaban autenticación y rutas
  sin restaurarlas. La reproducción conjunta de esos archivos y sus consumidores
  pasa: 222 correctos, uno omitido. La segunda suite completa da 12.318 correctos,
  82 omitidos y sólo un fallo de ubicación documental, corregido moviendo la guía
  de Jarvis a `docs/design/` y revalidando el área afectada. No se repitió la suite
  completa tras ese movimiento. Conservar la medición histórica del 04-09 en
  `FAUSTUS.md` §40.7, no como estado actual.

---

## Un patrón que ya se ha repetido tres veces

**Un test que afirma el estado del mundo caduca el día que alguien implementa
lo que afirmaba que no existía.** Ha pasado en las Fases 1, 3 y 4, siempre
igual: un test decía «este backend está *declarado pero no implementado*» o
«las tools son *exactamente* estas doce», y la fase siguiente lo rompió — con
razón, porque el test describía septiembre y no una regla.

El arreglo es siempre el mismo y conviene hacerlo **al escribir el test**, no
al romperlo:

- fijar la regla («un backend implementado responde lo que encontró una sonda
  real»), no el inventario («media_worker no está implementado»);
- fijar «no se ha perdido nada» en vez de «la lista es esta» cuando lo que
  importa es que ningún nombre desaparezca;
- en un test de rutas, **fijar la dependencia caída a propósito**: que haya un
  Docker o un ComfyUI corriendo en la máquina que ejecuta los tests no es una
  propiedad de este código, y un test que solo pasa cuando falta fallará en la
  primera máquina que lo tenga.

---

## Fallos que rompen algo hoy

- `[x]` **La trampa "ofrecido y luego rechazado" con `suggest_document`** —
  cerrada el 05-09-2026 (B-007, `FAUSTUS.md` §45). El arreglo está donde decía
  el diagnóstico: en el punto de uso. `src/tool_availability.py` responde
  "¿puede ejecutarse esto ahora?", la herramienta pregunta cuando la llaman, y
  la negativa dice qué falta y qué lo devolvería. `agent_loop` la retira de la
  ronda siguiente y **la reincorpora** en cuanto un `create_document` o un
  `manage_documents` tiene éxito — que es justo lo que el preflight no podía
  hacer. Queda sin comprobar una cosa: los 13 tests originales necesitaban
  `data/skills/ai-integration-setup`, que ya no está en el árbol, así que la
  reproducción exacta de aquel fallo no se ha repetido.

- `[x]` **`bg_jobs.refresh()` mataba por pid sin comprobar propiedad en la rama
  de timeout.** El orden del `elif` hace que `_pid_alive` nunca se alcance para
  un registro caducado, y un `_pid_alive` no ayudaría: un pid reciclado *está*
  vivo. El arreglo real es persistir la hora de creación del pid al lanzar y
  compararla al matar — que es lo que hace `process_ownership.note_started` en
  memoria, pero su `_create_time` es privado. Ruta del monitor, no alcanzable
  por el agente. **Cerrado en la auditoría actual:** el lanzamiento registra la
  identidad del proceso y el timeout sólo termina el árbol si esa identidad
  sigue coincidiendo; un PID reciclado se rechaza.

- `[x]` **`taskkill /T` en Windows y el pid del padre huérfano.** Windows nunca
  limpia el pid del padre de un huérfano, así que un proceso cuyo padre real
  murió hace tiempo y cuyo pid de padre registrado se recicló en nuestro
  `bash.exe` está *dentro de nuestro árbol* para taskkill. Desde el pid no hay
  nada comprobable. Solo lo arreglaría recorrer el árbol filtrando por hora de
  creación, lo que haría que `psutil` fuese obligatorio en la ruta de matar.
  **Cerrado en la auditoría actual:** `kill_process_tree` usa el registro de
  propiedad y `psutil`; ya no entrega un PID desnudo a `taskkill /T`.

---

## No verificado

- `[?]` **El ida y vuelta real del hook de Claude Code.** No hay binario de
  `claude` en el contenedor. Todo lo que posee Faustus es genuino —el listener,
  el script del hook, el registro, la corrección de `updatedInput`, la
  reconciliación— pero que Claude Code *ejecute* ese comando, mande *esa* forma
  de payload y *honre* esa respuesta está tomado de su documentación, no de una
  ejecución.

- `[?]` **`session_id` en el evento `init` de `stream-json`.** Se lee del shape
  ya codificado en los tests, no de una versión viva de Claude Code.

- `[?]` **La superficie de configuración de Codex.** Documenta algo con forma de
  puerta (`sandbox_mode`, `approval_policy`) pero no se pudo verificar contra un
  binario, así que su `gate` quedó en `"none"` en vez de adivinar. El vocabulario
  `"config"` está listo para quien lo verifique.

- `[?]` **El reranker contra un llama.cpp vivo.** El formato de `/v1/rerank` está
  verificado contra un servidor propio con la forma documentada, no contra
  llama-server. Se aceptan las dos grafías de respuesta.

- `[?]` **Latencia del reranker en hardware real.** Los 1.4 s/consulta medidos
  son onnxruntime en CPU detrás de un servidor de un solo hilo. Una GPU sería
  otra cosa; no se ha estimado una cifra.

- `[?]` **El comportamiento de `native_env` bajo un venv real.** Este contenedor
  no está en uno (`sys.prefix == sys.base_prefix`), así que
  `detected_venv_roots()` devuelve vacío en las rutas de producción. Los tests
  lo conducen con un entorno falso.

- `[?]` **`canonical_git_remote` contra un remoto ssh con alias real.** Probado
  con una URL de alias configurada; nunca se ha empujado a través de uno.

- `[?]` **Las páginas nuevas en un navegador.** Agent runners, Agent defs: los
  renderizadores puros corren bajo node y el cableado está fijado a nivel de
  fuente, pero nadie ha hecho clic y el CSS está mirado, no renderizado.

- `[?]` **Word y PDF abiertos de verdad.** El cromo de documento se juzga por
  las corridas de texto del OOXML y la capa de texto del PDF, no abriendo los
  ficheros.

- `[?]` **El backfill de la galería a `artifacts`, contra datos de verdad.** La galería de Luis está
  vacía hoy (0 filas en `data/app.db` y en los datos de la 7001), así que el ensayo sobre una copia
  de la base real creó 0 artefactos. Lo que está probado es con filas sintéticas: 2 importadas, 2
  rechazadas con motivo (`unknown_extension:txt`, `filename_is_a_path`), idempotente, y el rollback
  deja la galería intacta. Cuando haya imágenes de verdad conviene volver a mirar los números y, en
  particular, cuántas llegan **sin `file_hash`** — esas quedan con `sha256` a NULL a propósito.

- `[+]` **La UI del sandbox se queda en el picker.** El aviso del selector de carpeta y el tooltip
  de la píldora ya dicen la verdad (contenedor / no disponible / sin sandbox), pero **el resultado
  de un comando no enseña dónde corrió**: `sandboxed`, `image`, `isolation` y `duration_ms` viajan
  en el dict del tool y nadie los pinta en la tarjeta BASH. Ahí es donde se mira.

- `[~]` **El sandbox del agente está apagado por defecto.** `agent_sandbox_execution` enruta `bash`
  y `python` por el contenedor, y funciona; pero mientras esté apagado —que es el valor por
  defecto— el agente ejecuta donde ejecutaba siempre. Encenderlo es la decisión pendiente, y antes
  conviene usarlo un rato en la 7001: cambia latencia (≈0,4 s de arranque por comando) y rompe
  cualquier comando que dependa de herramientas del host que la imagen no tiene.

- `[~]` **Los runs de coding y la galería siguen fuera del sandbox.** El dispatch/harness ejecuta
  por su ruta de siempre, y `generated_images` escribe por la suya. La garantía de aislamiento
  cubre hoy el shell del agente, no todo lo que hace Faustus.

- `[~]` **`filesystem_tools` no se enruta, a propósito.** Ya está confinado al workspace por
  comprobación de ruta, y meterlo en el contenedor cambiaría latencia y semántica sin cerrar el
  agujero que importa —el shell—, que ya está dentro. Anotado por si aparece un caso que lo
  justifique.

- `[+]` **`MemoryView` no lo llama nadie.** El módulo construye la selección y la explica, con
  tests; falta cablearlo al prompt del agente y a `context_budget`/`context_ledger`. Hasta entonces
  el prompt se arma como antes y la vista no existe para el modelo.

- `[~]` **La aprobación se exige en `execution_router.execute()`, y solo ahí.** Un run cuyo
  manifiesto levanta tarjetas no arranca sin ellas, abre la tarjeta pendiente y devuelve su id en
  el motivo. Lo que **no** pasa todavía por esa puerta: el `bash`/`python` del agente
  (`sandbox_exec` llama a `choose` y al backend directamente, no a `execute`), los runs de coding y
  cualquier envío de email o publicación que no venga de una skill. El sistema de aprobación de
  *tools* que ya existía (`tool_approvals`, sellado al hash del comando) sigue siendo el que cubre
  esas rutas y **no se ha tocado**: son complementarios, no duplicados — uno aprueba un comando,
  el otro un plan.

- `[~]` **Los nodos `deliver` y `artifact_store` no están cableados a nada.** Rechazan por su nombre
  (*«no sender is wired to the 'deliver' node type; nothing was sent»*) y eso es deliberado: un run
  verde sin correo enviado es el peor fallo posible de un motor de workflows. `deliver` necesita un
  canal (Fase 6). `skill` **sí** está cableado, pero solo para `media:<plantilla>` (§36); una skill
  de código sigue necesitando el `execution_router` con un workspace (Fase 1), y su rechazo apunta
  a lo que sí funciona.

- `[+]` **Nadie llama a `advance()` en bucle.** Lo llaman la ruta `/api/workflows/runs/{id}/advance`,
  la tool MCP o una persona. `advance()` ya despierta los `wait` cuya hora pasó, así que un
  planificador de un minuto sería la implementación entera — pero mientras no exista, un nodo
  `wait` se queda pausado hasta que alguien pregunte.

- `[+]` **La UI no enseña los workflows.** Todo está en rutas y tools; no hay página de runs, ni
  lista de pausados, ni botón de reanudar. Un run pausado esperando a una persona solo se ve desde
  la lista de aprobaciones pendientes, que es la mitad de la historia.

- `[+]` **El doctor no mira la memoria, el navegador ni los modelos.** Cubre runtime, backends,
  ejecución, coding, medios, aprobaciones, workflows y skills — que es donde han caído las seis
  fases del masterplan. Lo que no toca todavía: si Ollama responde y con qué modelos, el estado del
  navegador integrado, y si la memoria/Chroma está sana. Son las tres siguientes.

- `[?]` **El `ChangeSet` del turno no se ha visto en un turno real todavía.** Ya se construye al
  final de cada turno del agente y su veredicto viaja en la tarjeta de resumen (probado bajo node y
  renderizado a mano en el navegador de la 7001). Lo que **no** se ha visto es un turno de verdad,
  con un modelo de verdad, editando un fichero y afirmando algo: ahí es donde se sabrá si
  `find_claimed_paths` acierta lo suficiente como para que la línea sea útil en vez de ruido. Si
  falla, fallará por exceso —marcando como no vista una ruta que el modelo mencionó de pasada— y
  eso se arregla en el extractor de afirmaciones, no en el contrato.

- `[~]` **`services/review_state.py` sigue siendo el dueño del aceptar/rechazar por fichero.** Un
  `ChangeSet` es el informe; lo que una persona decidió sobre él es otra pregunta con otra vida.
  Son complementarios hoy, pero si el ChangeSet se persiste alguna vez habrá que decidir cuál de
  los dos guarda el checkpoint, porque ahora mismo lo llevan los dos.

- `[+]` **La plantilla de vídeo es muda, corta y de una sola toma.** `video.short-form.v1` renderiza
  de verdad (SVD, 14 fotogramas, mp4 de 93 KB en 42,4 s, §40) pero SVD es **img2vid**: necesita una
  imagen de entrada, no sabe partir de un texto, y no hay audio, ni interpolación, ni continuidad
  entre clips. Para «un vídeo corto» de verdad falta encadenar: imagen → clip → clip → montaje.

- `[+]` **Nadie arranca el segundo motor.** El pool (`src/media_backends/pool.py`) reparte entre los
  motores que encuentra en `COMFYUI_URLS`, pero levantarlos es manual (`Start-ComfyUI-Pool.ps1`) y
  la variable se pone a mano en el arranque de la instancia. Si solo hay uno, el pool funciona con
  uno y lo dice; nadie avisa de que la segunda GPU está ociosa porque falta un proceso.

- `[~]` **Los dos motores dicen llamarse `cuda:0`.** Cada ComfyUI arranca con `--cuda-device N` y
  entonces ve su tarjeta como el dispositivo 0, así que los dos se presentan como
  «cuda:0 NVIDIA GeForce RTX ...». No es mentira —es lo que el motor sabe de sí mismo— y el nombre
  de la tarjeta y la URL los distinguen, pero leído rápido parece que las dos son la misma. Si
  alguna vez confunde a alguien, la etiqueta útil es el modelo de la tarjeta, no el índice.

- `[~]` **El pool encuesta a los motores cada vez que planifica.** `survey()` pregunta
  `/system_stats`, `/object_info` y `/queue` a cada URL en cada `plan`. Con dos motores en local son
  milisegundos; con motores remotos o con más de dos habría que cachear como hace el registro de
  capacidades (10 s) y refrescar en segundo plano.

- `[+]` **Los artefactos de un render no se ven en ninguna parte.** La fila lleva receta, semilla,
  modelo y licencia; no hay galería que lo enseñe ni botón de «variar/reproducir», que es justo lo
  que hace útil guardar la semilla. Los dos mundos siguen separados: `generate_image` escribe en
  `generated_images/` + `gallery_images`, y un render escribe en el almacén de artefactos.

- `[+]` **Nada llama a `media_runs.poll()` solo.** Igual que `advance()` en los workflows: lo llaman
  la ruta, la tool o una persona. Un render encolado no se recoge hasta que alguien pregunta.

- `[~]` **hwfit sabe decir «no cabe» y nadie se lo pregunta antes de encolar.**
  `rank_image_models()` ya calcula si un modelo entra en la VRAM de esta máquina, con margen del
  10%. El criterio del masterplan —«si la GPU no es suficiente, Faustus informa y ofrece una
  variante viable; no simula que el trabajo está corriendo»— todavía no se cumple: hoy se encola y
  el fallo llega del motor.

- `[?]` **El puente de skills contra skills de verdad.** Luis tiene **una** skill guardada, y no
  declara permisos, así que lo único comprobado con datos reales es el caso deny-by-default. Las
  claves `permissions_*`, los `outputs: [name=type]` y los alcances de memoria están probados con
  ficheros sintéticos.

- `[~]` **`/artifacts` no es write-only.** Docker no tiene montaje de solo escritura. Lo que hay es
  un directorio propio y vacío por run (lo crea el router) más una foto previa en el backend para no
  atribuir a un run la salida de otro. La afirmación fuerte del masterplan no se cumple; la débil sí,
  y está escrita en el docstring.

- `[~]` **Un secreto dentro de un contenedor lo ve quien hable con el demonio de Docker.**
  `docker inspect` enseña el entorno de un contenedor vivo. Van por `--env-file` 0600 para que no
  aparezcan en la tabla de procesos del host, que es la fuga que sí se puede cerrar. En esta máquina
  hablar con el demonio ya es equivalente a root.

- `[~]` **Una allowlist de red se rechaza en vez de enforzarse.** Hace falta un proxy de salida.
  Rechazar es el lado correcto en el que fallar, pero significa que un skill que quiera red solo
  puede tenerla entera.

- `[+]` **Nadie limpia `data/artifacts/runs/`.** Cada run deja su directorio de scratch. Los
  ficheros recolectados se mueven al store, así que lo que queda son directorios vacíos, pero
  se acumulan.

- `[+]` **`DockerWorkspaceBackend.cancel()` no lo llama nadie.** Existe y mata el contenedor por
  nombre; falta el botón y el cableado desde los runs.

- `[?]` **Ninguna skill real usa todavía un `SkillManifest`.** El contrato existe y se valida, pero
  las skills de hoy siguen siendo `SKILL.md` con frontmatter (`services/memory/skill_format.py`).
  El puente entre las dos formas es Fase 2 del masterplan; hasta entonces, que un manifiesto valide
  no significa que haya algo que lo lea.

---

## Deuda aceptada a propósito

- `[~]` **El gate no ve dentro de un comando permitido.** Juzga *llamadas a
  herramientas*. `Bash("./build.sh")` clasificado seguro ejecuta lo que haga ese
  script. Está en `GATED_NOTE` y en cada resultado con gate.

- `[~]` **El gate no confina rutas dentro de bash.** Solo aplica el tier de
  `command_guard`. `echo x > ../../otro-sitio` se juzga exactamente como lo
  juzga `command_guard`, ni mejor ni peor.

- `[~]` **Un agente que reescriba su propio script de hook.** Mismo usuario, así
  que el fichero 0o500 en un temporal es advertencia, no barrera. La mitigación
  es detección: las llamadas sin recibo del gate salen como `unseen` y se
  convierten en incertidumbre declarada.

- `[~]` **`unguarded` a nivel de trabajo en `DispatchJob.to_dict`.** Sigue
  cableado a `True`. Hacerlo condicional necesita `runner_gates` en el espejo
  (es estado por ejecución, no por trabajo), y tras un reinicio se daría la
  vuelta. La respuesta precisa por ejecución sí se persiste en el bloque
  `external_gate` de la prueba.

- `[~]` **`locks` y `worker_key` no llegan al gate desde dispatch.** El
  `FileLockRegistry` se construye dentro de `DelegateAgentsTool.execute` y no
  sale de ahí. Un "denegado, lo tiene otro" que no se puede comprobar es mejor
  ausente que fingido; hay un test que afirma la ausencia.

- `[~]` **Tres sitios de shell conservan el venv a propósito.** El runner de
  Cookbook lee `$VIRTUAL_ENV` en ejecución para construir `LD_LIBRARY_PATH` y
  encontrar las wheels de CUDA; quitárselo mataría un vLLM a los minutos de
  cargar un modelo. La razón está escrita como comentario en los tres sitios
  para que la próxima barrida no lo reabra.

- `[~]` **Las reglas de ruta no entran en un shell.** Ningún patrón alcanza
  dentro de `bash`/`python`. Una definición que conserve un shell y deniegue una
  ruta recibe una advertencia impresa en su tarjeta.

- `[~]` **Las reglas de lectura solo gobiernan `read_file`.** `grep`/`glob`/`ls`
  toman una raíz y un patrón y responden sobre un árbol; de ahí no se saca una
  ruta con certeza.

- `[~]` **El reranker no siempre ayuda, y está medido.** Sobre consultas
  generadas mecánicamente donde la pregunta es un fragmento literal del
  documento, BM25 ya está perfecto y el reranker es neutro en un set y **peor en
  otro (40/43)**. Por eso es opt-in y por eso el resultado dice si corrió.

---

## Mejoras pendientes

- `[+]` **`tool_index.py` no rerankea.** Una línea de opt-in en su llamada a
  `two_tier_search.search()`. Es donde caería la mejora de 6/8 → 8/8 medida.

- `[+]` **`experts.search` hace una consulta a la BD por llamada** para ver si
  hay reranker. Sub-milisegundo frente a ~10 ms de búsqueda, pero está en una
  ruta donde el usuario espera y no está cacheada.

- `[+]` **`Citations` y `Claims cited` se calculan y no se enseñan.**
  `research_handler._format_research_report` y `visual_report.py` pintan una
  lista fija de claves (`Duration/Rounds/Queries/URLs/Model/Search`).

- `[+]` **El registro de citas no se persiste.** Muere con el investigador, así
  que una investigación continuada reconstruye los números desde
  `prior_findings` — mismas URLs dan mismos números solo si llegan en el mismo
  orden.

- `[+]` **La segmentación de frases es heurística.** Parte en `.!?…` seguido de
  mayúscula/dígito/comilla. Se equivoca en "Dr. Smith" o "p. ej." y eso mueve la
  frase a la que se pega un marcador. Los marcadores nunca se pierden.

- `[+]` **`Fig. 3` se parte.** Un dígito cuenta como inicio de frase. La regla
  del dígito es la que mantiene entera `"40%. [1] Recovery"`, así que no es
  gratis quitarla.

- `[+]` **La metadata del fichero exportado sigue diciendo "chat".** El docx
  guarda `comments = "Chat transcript exported by Faustus"` y el PDF
  `subject="Chat transcript"`. Invisible en la página, visible en Propiedades.

- `[+]` **El documento exportado mezcla idiomas.** Sus etiquetas (metadatos,
  "Sources", el pie) son inglesas aunque el informe sea español. Tres constantes.

- `[+]` **`_blocks_to_txt` sigue juntando la lista.** Se dejó a propósito: el
  texto plano nunca se vuelve a parsear, así que no hay fallo de fidelidad.

- `[+]` **`test_dispatch_external_runner.py` creció ~330 líneas.** El repo tiene
  un `OVERSIZED_TEST_SPLIT_PLAN.md`; este fichero es candidato.

- `[+]` **`static/favicon.png` está en `.gitignore` y dos tests dependen de él.**
  Pasan en el árbol de Luis y fallan en cualquier worktree limpio. O se commitea
  el asset o el test deja de depender de un artefacto ignorado.

- `[+]` **`markitdown[docx]` no está instalado** y un test lo pide. Fallo
  ambiental permanente en la suite.

---

## Sitios de fuga del venv aún sin tocar
Cada uno es una línea (`env=native_host_environment()`), listados por valor:

- `[x]` `src/agent_runners.py::build_env` — **hecho en SEC-1c**: ya no copia
  `os.environ`, sino el perfil `agent`. Y el `which()` de `external_worker` busca primero en
  el PATH del hijo y luego en el nuestro, que es lo que este punto pedía: una CLI instalada
  en nuestro venv se sigue encontrando, y el hijo sigue sin recibir nuestro PATH.
  Lo que decía el ticket original:
  **No es una línea segura tal cual**: `external_worker` resuelve el ejecutable
  con `shutil.which(argv[0], path=full_env["PATH"])`, así que si el usuario
  instaló su CLI en nuestro venv, limpiar el PATH lo deja "no instalado". Hace
  falta `which()` contra el PATH original y lanzar con el entorno limpio.
- `[+]` `routes/agent_runner_routes.py:76`, `routes/codex_routes.py:532` — se
  arreglan solos con el anterior.

---

## SEC-1: lo cerrado, y lo que el mismo informe deja abierto

El lote SEC-1 (rama `feat/sec-1`, 05-09-2026) cierra B-009, B-020, B-008, B-022 y B-010 de
`inspiration/AUDITORIA_BACKEND_Y_FEATURES_FAUSTUS.md`; el registro está en `FAUSTUS.md` §42.
Lo que **no** cierra, dicho aquí para que se pueda encontrar:

- `[+]` **Perfiles de entorno para los otros hijos.** SEC-1c cableó los perfiles allowlist en
  los dos consumidores que el informe señala primero —runners externos y servidores MCP—.
  Siguen en `native_host_environment()` (que ahora sí quita el token interno, pero entrega
  todo lo demás): `src/project_tests.py`, `src/git_invariants.py`,
  `src/workspace_checkpoints.py`, `src/bg_jobs.py`, `src/builtin_actions.py`,
  `services/shell/service.py` y `src/agent_tools/subprocess_tools.py`. Los perfiles `build` y
  `git` existen para ellos. El de shell es el delicado: es la herramienta del usuario, y
  recortarle el entorno rompe usos legítimos; necesita decisión, no sólo código.
- `[+]` **Asistente de migración de servidores MCP antiguos.** El informe pide una migración
  que enseñe **nombres de variables, nunca valores**, para que un servidor con
  `inherit_env: true` pueda pasar a mínimo sabiendo qué pierde. Hoy la herencia se mantiene
  entera (menos el token interno) y la única salida es el interruptor por servidor.
- `[+]` **La interfaz no sabe de perfiles de backup.** `POST /api/backup/snapshot` acepta
  `profile` y `passphrase`; Studio no los envía, así que desde la interfaz siempre sale un
  backup `content`. Falta el selector y el campo de contraseña (va en `docs/ui/PENDIENTES_UI.md`).
- `[?]` **Contraseña del backup completo, sólo por entorno.** `FAUSTUS_BACKUP_PASSPHRASE` es
  deliberado: un `settings.json` vive dentro del directorio que el backup protege. Pero eso
  significa que el backup automático **no** es completo salvo que alguien exporte la variable
  en el arranque. Sin ella toma `content`, que es la degradación honesta.
- `[~]` **`vault.json` y `BW_SESSION`.** El perfil `content` ya lo excluye entero, así que la
  sesión de Bitwarden no viaja en un backup en claro. Lo que el informe pide además —no
  persistir `BW_SESSION`, o cifrarlo con caducidad— sigue pendiente en `routes/vault`.
- `[x]` `tests/test_agent_gate.py::test_the_hook_script_is_not_left_behind` fallaba en Windows
  **desde antes de esta rama** (comprobado con el árbol guardado en stash): el directorio
  `faustus-gate-*` del hook queda en el temporal. **Cerrado en la auditoría actual:** el
  cierre quita el atributo de sólo lectura antes de borrar y la regresión pasa en Windows.

## AUTH-1: lo cerrado, y lo que el mismo informe deja abierto

El lote AUTH-1 (rama `feat/sec-1`, 05-09-2026) cierra B-011, la parte de C-010 que toca a los
tokens `ody_`, y B-004; el registro está en `FAUSTUS.md` §43. Lo que **no** cierra:

- `[+]` **La matriz sólo gobierna a los tokens de API.** `core/authz.py` declara la superficie
  de los principales `api_token`; las sesiones de cookie siguen autorizándose ruta a ruta con
  `require_admin` y compañía. C-010 pide la matriz para **todos** los tipos de principal
  (`user`, `api_token`, `internal`, `mcp`), con `owner_rule` aplicado de verdad y no sólo
  declarado. `Rule.owner_rule` y `effect_class` ya viajan en cada regla precisamente para eso:
  están escritos, todavía no se consultan.
- `[+]` **`/api/codex/*` sigue siendo un agujero declarado.** La regla exige *todos* los
  scopes conocidos, que es la manera honesta de decir «esto no está segmentado». Segmentarla
  requiere decidir qué hace cada ruta de codex, y eso es trabajo de RUN-1.
- `[?]` **El presupuesto del clasificador no es configurable.** `gate_check` llama a
  `classify_tool` con los 50 ms por defecto. En la máquina de pruebas un comando adversario de
  4.095 reglas evaluadas ni se acerca al límite, así que no hay prisa; si alguna vez sube
  `budget_exceeded` en `/api/command-guard/log`, el ajuste es un setting, no un rediseño.
- `[?]` **Una degradación se cuenta en memoria del proceso.** Los contadores viven en el
  proceso y se pierden al reiniciar. Es suficiente para ver un clasificador rompiéndose ahora
  mismo; no sirve para una serie temporal. Si hace falta histórico, los recibos
  `guard_degraded` ya están en el log encadenado y son la fuente buena.
- `[~]` **La tarjeta de un comando sin clasificar no se distingue en la interfaz.** El backend
  la sella y la marca (`tier: UNKNOWN`, `rule: guard.degraded:*`); Studio la enseña como
  cualquier otra tarjeta de comando destructivo. Debería decir que no se sabe qué hace, que no
  es lo mismo que saber que es peligroso (va en `docs/ui/PENDIENTES_UI.md`).

## Sprint 0B: lo cerrado, y lo que deja abierto

El Sprint 0B (rama `feat/sec-1`, 05-09-2026) cierra B-002, B-003, B-005, B-006 y B-018 de la
auditoría; el registro está en `FAUSTUS.md` §44. Lo que **no** cierra:

- `[x]` **B-007 se cerró justo después**, en su propio commit (`FAUSTUS.md` §45).
- `[+]` **La misma política de UTC falta en `chat_export.py`.** B-005 se arregló donde el
  informe lo señala (`report_export.py`), pero `chat_export.py:692` sigue usando
  `datetime.now()` sin zona y su nombre de fichero (`:1467`) sale de ahí. Es el mismo fallo,
  fuera del alcance del lote; el arreglo es una línea y un repaso de los tests que fijan la
  cadena *"Exported: ..."*.
- `[?]` **`semver_key` acepta lo que acepta el validador, que es más laxo que semver.org.**
  El regex del contrato permite identificadores de prerelease con ceros a la izquierda
  (`1.0.0-01`), que la especificación prohíbe. Se ha dejado como estaba para no invalidar
  datos ya escritos; la ordenación los trata como numéricos, que es lo razonable.
- `[~]` **El resto de factories de rutas no se han revisado una a una.** El guard de
  `tests/test_route_factory_isolation.py` impide que aparezca un `APIRouter` de módulo nuevo,
  pero no dice nada de otras formas de estado global en `routes/` (cachés, managers guardados
  en el módulo). Nadie ha buscado esas.

## B-007: lo cerrado, y lo que deja abierto

B-007 (rama `feat/sec-1`, 05-09-2026, `FAUSTUS.md` §45) pone la disponibilidad de herramientas
en el punto de uso. Lo que **no** cierra:

- `[+]` **Sólo hay una regla.** `src/tool_availability.py` gobierna `suggest_document`, que es
  la que nombra el informe. `edit_document` y `update_document` tienen la misma dependencia de
  un documento destino y siguen devolviendo su error propio; añadirlas es una entrada en
  `_RULES`, pero hay que mirar antes qué tests fijan sus mensajes actuales.
- `[+]` **La lista de herramientas retiradas no viaja al modelo.** Se retira el esquema, y eso
  basta para que no se vuelva a llamar, pero al modelo no se le dice *"esto ya no está y por
  esto"*. El `remedy` de la negativa lo explica una vez, en el resultado de esa llamada; una
  nota en el siguiente turno sería más clara.
- `[?]` **El registro es estático.** Las reglas se declaran en el módulo, no en el registro de
  herramientas ni en los skills. Si un skill quisiera declarar su propia condición de
  disponibilidad hoy no puede.
- `[~]` **Los 13 tests del diagnóstico original no se han reproducido.** Necesitaban
  `data/skills/ai-integration-setup`, que ya no está en `data/skills`. Si el skill reaparece,
  vale la pena volver a correrlos antes de dar el asunto por muerto.

## Frente 1 cerrado: lo que los últimos ocho lotes dejan abierto

Los 25 bugs de `inspiration/AUDITORIA_BACKEND_Y_FEATURES_FAUSTUS.md` están cerrados
(`FAUSTUS.md` §42-§51). Esto es lo que **no** cubren, dicho con nombre y motivo.

### STATE-1 (B-012)

- `[+]` **`save_settings` sigue siendo una escritura ciega.** Unas dos docenas de sitios hacen
  leer-modificar-guardar con él. Ahora toman el lock y suben la revisión, así que no se
  entrelazan, pero no pasan `expected_revision`: gana el último. Migrarlos a `update_settings`
  es trabajo mecánico y hay que hacerlo ruta a ruta.
- `[+]` **La misma política de UTC falta en `chat_export.py`.** B-005 se arregló en
  `report_export.py`, que es donde lo señala el informe; `chat_export.py:692` sigue usando
  `datetime.now()` sin zona y su nombre de fichero sale de ahí.
- `[~]` **Dos mecanismos de lock en el mismo árbol.** STATE-1 usa `core/file_lock.py` (O_EXCL
  con rotura por antigüedad) y UPLOAD-1 usa locks consultivos del sistema operativo, que se
  sueltan solos al morir el proceso. Los segundos son mejores; habría que quedarse con ellos.

### NET-1 (B-019)

- `[x]` **El segundo caso visible de rebinding quedó cerrado en la auditoría actual.**
  `routes/webhook/webhook_routes.py` valida la `base_url` en la ruta, pero la petición la emite
  `llm_call_async` en `src/llm_core.py`, que B-019 no lista. La ventana de rebinding sigue ahí
  y necesita un lote que sea dueño de `llm_core`. `llm_call_async` valida la resolución,
  fija la IP pública usada por el transporte, desactiva redirecciones y no hereda proxies
  del entorno para esta ruta; el pin se vuelve a construir en cada petición.
- `[+]` **`src/url_security.py` es un tercer clasificador de direcciones privadas.** Parte de
  la fragmentación que B-019 describe; fuera de las rutas que nombra.

### SSH-1 (B-025)

- `[!]` **Rompe conexiones existentes y no hay interfaz para arreglarlo.** Con el `known_hosts`
  privado vacío, todo host remoto deja de conectar hasta emparejarlo. Los endpoints están
  (`/api/cookbook/ssh/fingerprint`, `/pair`, `/unpair`); Studio no.
- `[x]` **Los sitios con el flag antiguo quedaron cerrados:** `routes/hwfit_routes.py` y
  `services/hwfit/hardware.py` pasan `strict_host_key_checking=False`, y
  `core/platform_compat._ssh_exec_argv` y `routes/cookbook_helpers.run_ssh_command_async`
  seguían aceptando ese valor. Ya pasan por el almacén emparejado de Faustus y el helper
  genérico rechaza explícitamente `strict_host_key_checking=False`.
- `[?]` **Nada se probó contra un SSH real.** `ssh-keyscan` está doblado en todos los tests, así
  que la forma real de su salida y el comportamiento de `UserKnownHostsFile` con una ruta de
  Windows están sin verificar contra un `ssh.exe` de verdad.

### MAIL-1 (B-023) y UPLOAD-1 (B-021)

- `[x]` **Agujero residual en `document_routes.py`.** `prepare-signed-reply` dejaba
  ficheros `<uuid>_<nombre>` planos en la raíz del staging, y hay un puente que los resuelve con
  la semántica antigua. Un token filtrado de ahí sigue siendo adjuntable por cualquier usuario
  autenticado. El arreglo es una llamada a `register_compose_upload`; el fichero no está entre
  los que B-023 nombra, y hay un test que fija el puente para que quitarlo sea deliberado.
  **Cerrado:** ahora registra el fichero con dueño mediante `register_compose_upload`.
- `[+]` **El poller programado no pasa dueño.** `email_pollers.py` tiene `row_owner` a mano y
  llama sin él. Una línea.
- `[+]` **La limpieza de adjuntos es oportunista**, colgada de la ruta de staging, no de un
  scheduler.
- `[+]` **B-021 sólo tomó el camino corto.** La migración a SQLite, escribir metadatos antes de
  publicar y el barrido de reconciliación siguen pendientes: son ART-1.

### LIFE-1 (B-013, B-001)

- `[+]` **Sin Job Objects en Windows.** Se persiste el pgid en POSIX; en Windows la propiedad se
  demuestra por hora de creación. Un Job Object habría que crearlo al lanzar, en
  `core/platform_compat`, fuera de las rutas de B-001.
- `[x]` **`core/platform_compat.kill_process_tree` ya no llama a `taskkill /T` sobre un PID
  pelado.** Delega en la terminación con prueba de propiedad y conserva el fallback explícito
  sólo para los llamantes que han aceptado el árbol no verificado.
- `[?]` **La rama POSIX está simulada.** Todos sus tests corren en Windows con `IS_WINDOWS`
  monkeypatcheado y `os.getpgid`/`os.killpg` inyectados. Nada se ha ejecutado en POSIX real.
- `[x]` **psutil pasa a ser necesario** para desmontar árboles, y ya está fijado en
  `requirements.txt` (`psutil>=5.9,<8`). Sin él, `bg_jobs` se niega a señalar —que es la regla
  del informe— donde antes mataba sin preguntar.

### RUN-1 (B-014, B-015, B-016)

- `[+]` **La clave de idempotencia no llega a los adaptadores.** Se acuña, se persiste y se
  expone, pero no se enhebra hasta webhooks, correo ni `builtin_actions`; B-014 no los nombra.
  `IDEMPOTENT_SINKS` está vacío a propósito: sin un destino que lo soporte, `effectively_once`
  no se promete.
- `[+]` **Nada llama a `heartbeat_node` ni a `recover_expired_node_leases` en bucle**, ni hay
  enganche de `reconcile()` de media en el arranque —eso es `app.py`—.
- `[?]` **Las tres migraciones no se han ejecutado contra una base de datos preexistente real.**
  Los tests obtienen las columnas de `create_all`.
- `[?]` **La forma posicional de las entradas de ComfyUI** (`client_id` dentro de `extra_data`,
  índice 3) viene del conocimiento del formato, no de la instancia de esta máquina. Si es
  errónea, la reconciliación no encuentra nada y los runs se quedan en `submit_unknown`: falla
  del lado seguro, pero conviene comprobarlo.

### ART-1 (B-017)

- `[~]` **La migración entera está pendiente, y ese es el plan.** Fases 2 a 5 de la nota:
  copiar `artifacts` a ocurrencias, hashear las filas de galería, y los dos cortes. Nada llama
  todavía al almacén nuevo desde `collect()`/`persist()`; `artifacts` sigue siendo la verdad.
- `[+]` **La recolección de basura no borra bytes por defecto**, y aunque se le pida se niega a
  tocar lo que la tabla vieja todavía nombre. Dos censos cubren los mismos ficheros hasta la
  fase 5 y sólo uno sabe del otro.
- `[?]` **Las dos carreras con hilos usan SQLite sobre fichero.** Pasaron siempre aquí, pero es
  el sitio donde esperaría inestabilidad en una máquina cargada.

### CB-1 (B-024)

- `[+]` **Sin Secret Broker.** El informe lista cuatro alternativas equivalentes; se implementó
  la del fichero de un solo uso. Un broker no existe en el repositorio.
- `[?]` **Tres tests se saltan en Windows** porque comprueban modos POSIX ejecutando el snippet
  generado. El comportamiento se verificó a mano en un shell Linux.
- `[?]` **Nada se ha ejecutado contra un scp o un PowerShell remotos reales:** la rama de
  Windows remoto está verificada sólo como texto generado.

### De la propia auditoría, más allá de los bugs

- `[ ]` Los cambios estructurales C-001 a C-008 (RunService único, sandbox por defecto,
  supervisor durable de trabajos, app factory, dividir los módulos gigantes, migraciones
  formales, dependencias reproducibles, errores tipados) están **sin empezar**. LIFE-1 hizo la
  parte de C-004 que tocaba al ciclo de vida; el resto no.
- `[ ]` **B-008 sigue parcial.** SEC-1c cableó los perfiles de entorno en runners externos y
  servidores MCP; los otros siete consumidores de `native_host_environment()` siguen recibiendo
  el entorno completo menos el token interno.

## Context Engine: lo cerrado, y lo que deja abierto (06-09-2026)

El Context Engine (`FAUSTUS.md` §53, `OBJETIVOS.md`) cierra la Fase 1 del plan
`inspiration/PLAN_CONTEXT_ENGINE_FAUSTUS.md`: 29 ficheros, 15.393 líneas, 386 tests, todo detrás
de `agent_context_engine` y `agent_context_engine_shadow`, apagadas por defecto. Esto es lo que
**no** cierra, más lo que se vio de camino.

### Tests

- `[x]` **`tests/test_static_checks.py::test_the_loop_does_not_spend_a_round_on_a_warning_that_was_already_there`
  ya pasa.** Comprobado corriéndolo antes y después del cambio, en el mismo
  árbol: falla igual en los dos. Dos motivos que se suman y ninguno es del Context Engine: el
  arnés del test marca como cambiada una línea que nadie tocó, y el propio test **habla con el
  Ollama vivo de esta máquina**, así que su resultado depende de qué modelo esté cargado. Un
  test estático que necesita un modelo no es un test estático; hay que separarlo en dos o
  saltarlo sin endpoint. **Cerrado en la auditoría actual:** el editor falso conserva los
  finales de línea y ya no atribuye al turno un cambio LF→CRLF que no hizo.
- `[~]` **`tests/test_disk_ballast.py` (6 tests) y `test_atomic_io` fallan sólo con `pytest -n 6`.**
  En serie pasan los siete. Es contención: los dos escriben ficheros grandes y miden espacio o
  atomicidad, y seis workers a la vez sobre el mismo disco se pisan. **No es una regresión**, pero
  queda anotado para que la próxima persona que vea esos rojos en paralelo no vuelva a gastar
  media hora descartándolos. El arreglo real es marcarlos `xdist_group` o `serial`.

### Deuda que el propio subsistema declara

- `[x]` **`src/context_engine/wiring.py::observe_receipt` está implementado, probado y
  cableado.** Registra qué referencias abrió el turno, cuántos resultados de herramienta añadió,
  el `outcome_ref` y el veredicto; sólo lo llaman los tests. Es la costura de la Fase 2, y
  mientras no exista `historical_utility` no tiene de dónde salir: el factor está en la fórmula
  del ranking y siempre vale lo mismo. Un recibo sobre un paquete que no se entregó no significa
  nada, así que va con la migración, no antes. El bucle lo llama sólo después de entregar un
  paquete real y de conocer el veredicto del turno.
- `[x]` **El adaptador de sesiones tiene proveedor de historial acotado al turno.**
  `src/context_engine/adapters/sessions.py` se niega a propósito a leer la base de datos de
  sesiones —`SessionManager.get_session` no acepta dueño (cualquier id, incluido uno que un
  modelo escriba en un mensaje, resuelve a los mensajes de esa sesión), muta la caché y
  `last_accessed`, y devuelve una transcripción distinta de la que el turno está usando— y espera
  que se la entregue quien ya tiene los mensajes, con `set_history_provider`. **Nadie la llama en
  producción.** Consecuencia: `available()` es `False` y **ningún paquete compilado hoy tiene
  sección `recent_messages`**. Es el estado honesto (visiblemente ausente antes que
  silenciosamente equivocado), pero es un agujero en la comparación sombra: el informe compara un
  paquete sin historial contra un prompt que sí lo lleva. **Cerrado:** `history_scope` recibe
  exactamente el historial que ya posee el turno y exige coincidencia de dueño y sesión, sin
  tocar la caché global ni `last_accessed`.
- `[x]` **`budgets.AppParityEstimator` reproduce `src.model_context.estimate_tokens`
  exactamente.** Usa el mismo 0,3 caracteres por token
  (`APP_PARITY_CHARS_PER_TOKEN = 1.0 / 0.3`) y el mismo coste de 4 tokens por mensaje, pero
  `_BaseEstimator.count()` hace `math.ceil(len(text) / chars_per_token)` mientras
  `estimate_tokens` hace `int(len(content) * 0.3)`: uno redondea hacia arriba y el otro trunca,
  **≤1 token de diferencia por cadena**. La diferencia sí está documentada, pero en el docstring
  de `_ledger_tokens` (`wiring.py`), que por eso reporta las dos mediciones para que nadie
  concluya que una de las dos tarjetas de la pantalla está rota; el docstring de la clase en
  `budgets.py` sigue diciendo *"Reproduces `src.model_context.estimate_tokens` exactly"*, que es
  falso. Hay que decidir: o se unifican (que `AppParityEstimator` trunque) o se corrige el
  docstring. Unificarlas es lo correcto — un carril que existe para comparar y no compara igual
  no sirve para lo que se creó. Ya usa truncado y las mismas reglas para bloques y tool calls.
- `[x]` **`budgets.py` ya tiene la prueba de paridad que nombra.** El comentario de `IMAGE_BLOCK_TOKENS` dice
  *"Kept in sync by `tests/test_context_engine_budgets.py`, which reads the constant from there"*,
  y ese fichero **no está en `tests/`**. Nada comprueba que los 1200 de
  `context_engine/budgets.py` sigan al valor de `src/model_context.py`; si uno cambia, el otro se
  queda callado. El docstring de `adapters/documents.py` cita ese mismo arreglo como precedente
  del suyo (que sí existe, en `test_context_engine_sources.py`). `test_context_engine_budgets.py`
  fija la constante y compara entradas generadas contra la función canónica.
- `[x]` **El mantenimiento del Context Engine ya corre en bucle.** Las seis tareas también se disparan a mano
  (`POST /api/context/maintenance/run` o la tool MCP). `should_yield()` ya sabe cederle la máquina
  a un turno en vuelo, y `agent_context_ledger_days` promete una poda que hoy no ocurre sola: el
  ledger crece hasta que alguien pulse el botón. Mismo agujero que el `advance()` de los
  workflows. `scheduler_loop` reparte las tareas de workspace entre todos los proyectos,
  respeta un presupuesto y cede mientras haya conversación activa; el supervisor lo arranca
  después del primer render.
- `[?]` **La sombra no se ha corrido contra un turno real con un modelo real.** `shadow_round` y
  `manifest.compare()` están probados con mensajes construidos en los tests. Nadie ha encendido
  `agent_context_engine_shadow` en una conversación de verdad y leído el informe.
- `[x]` **`agent_context_engine` ya gobierna el camino canónico.** `wiring.enabled()` lee el ajuste y su
  propio docstring dice que nadie lo llama: existe para que los consumidores de la Fase 2 tengan
  un solo sitio donde preguntar. Encenderlo hoy no cambia ningún prompt, lo que es correcto pero
  no es lo que un usuario deduce de una casilla en Ajustes. El bucle compila el paquete por ronda,
  entrega el render acotado, emite el manifiesto y registra el recibo; apagado conserva el camino
  anterior.
- `[x]` **`incognito` llega desde la ruta de chat.** `wiring.build_request()` lo lee de
  `harness_options`, que `routes/chat_routes._project_harness_options` rellena sólo con los campos
  de `services.projects.AGENT_OPTION_FIELDS`. La política existe, se aplica **antes** de la
  recuperación (que es lo que importa) y está probada; la ruta lo añade ahora a las opciones
  internas sin convertirlo en una preferencia persistente del proyecto.

### Lo que se vio en los carriles viejos (no es del Context Engine, pero está ahí)

- `[x]` **`src/memory_engine.py::pack_detail()` ya no marca como accedidas las memorias que
  devuelve.** Su búsqueda interna sí llama a `search(..., touch_hits=False)`, pero al final la
  función hace `touch(ids, now)` sobre **todo** lo que empaquetó: reglas procedurales,
  antipatrones y aciertos. Recuperar no es usar. Un ítem que el presupuesto tira después no se
  usó, y decirle lo contrario al curador es entrenarlo con una mentira: `access_count` y
  `last_used` inflados desplazan la decadencia y cambian qué se promueve. El Context Engine lo
  esquiva por otra vía —`adapters/memory.py` reconstruye la selección sin consulta desde
  `scoped_items()`, que es una lectura pura, y registra el uso una sola vez desde
  `ContextReceipt`— pero **el carril viejo sigue tocando en cada turno**, y es el que está
  encendido. El toque se hace únicamente en `note_injected`, después de saber qué se entregó.
- `[x]` **`services/docs/service.py::query` propaga `owner` a `rag_vector.search`.** La firma
  es `query(self, query, top_k=5)` y llama a `self.rag.search(query, k=top_k)`, sin dueño.
  `rag_vector.search` **sí** acepta `owner` y con él aplica un `where={"owner": owner}` sobre la
  colección; sin él devuelve chunks de todos los dueños de la instalación. El Context Engine no
  usa este camino (su adaptador de documentos pasa por `rag_manager` con el dueño de
  `request.execution`), pero cualquiera que use `DocsService` tiene una fuga entre usuarios. El
  arreglo es un parámetro y una línea; ambos están ya aplicados y cubiertos por regresión.
- `[+]` **`src/memory_engine.record_outcome` sólo puntúa ítems `procedural`.** Está dicho en su
  docstring y es defendible —son las reglas que se le pidió al modelo que siguiera, así que un
  pase o un fallo es evidencia sobre ellas—, pero la consecuencia es que `semantic` y `episodic`
  **nunca reciben feedback**: se inyectan, se cuentan como usadas (ver el punto de `pack_detail`)
  y su puntuación no se mueve nunca por el resultado del turno. O se les da una señal propia, o
  conviene dejar escrito que su score no aprende.
- `[~]` **`src/memory_view.py` y `src/contracts/memory.py` siguen sin cablear a producción.** Ya
  estaba en `OBJETIVOS.md` (Fase 2 del masterplan) desde el 04-09 y sigue igual: sólo los llaman
  sus tests. Ahora hay además una segunda implementación del mismo muro —`ContextPolicy` y el
  filtrado previo del planner—, así que cuando se cablee hay que decidir cuál manda. Dos muros
  para lo mismo es cómo se abre un agujero en uno de los dos.

## Project Context Links: lo cerrado, y lo que deja abierto (06-09-2026)

Project Context Links (`FAUSTUS.md` §54, `OBJETIVOS.md`) cierra las fases 1 a 4 del plan
`inspiration/PLAN_PROJECT_CONTEXT_LINKS_FAUSTUS.md`: 12 ficheros nuevos, 4.189 líneas, 176 tests.
Lo que falta por construir está en `OBJETIVOS.md`; esto es lo que está construido y **habría que
mirar**.

- `[x]` **La cola de indexación ya es real.** `src/project_context/indexer.py` consume enlaces
  `queued`, `stale` y `indexing`, cede ante conversaciones activas y publica por transacción en
  `data/project_context_index.db`. La lectura comprueba dueño, proyecto y revisión antes de usar
  chunks; conserva la lectura directa degradada como respaldo. Los eventos `indexed`,
  `index_failed` y `retrieved` ya tienen productor. Verificado con 198 pruebas del servicio,
  fuentes del Context Engine, configuración y supervisor, incluidas carreras de revisión y
  aislamiento entre dueños.

- `[x]` **`DELETE /api/projects/{id}/context/{item_id}` emite `project_context_detached`.** La
  ruta va por `ProjectStore.remove_context_item` y no por `ProjectContextService.detach`, y lo
  hace por un motivo real y escrito en su docstring: un solo endpoint tiene que servir para el
  `item_id` legado de diez hex y para el `ctx_...` nuevo, y `remove_context_item` no necesita
  dueño efectivo más allá del que `_get_or_404` ya comprobó, así que un borrado que funciona no se
  convierte en un fallo cerrado en una instalación sin login. El precio es que **el evento no
  sale**: `src/context_engine/cache.py::on_event` invalida en `project_context_detached`, así que
  tras un desvinculado desde la pantalla la caché del Context Engine conserva entradas derivadas
  de un vínculo que ya no existe hasta que otra cosa la mueva. El arreglo barato es emitir el
  evento en la ruta después del borrado; el correcto es que `detach` acepte los dos formatos de id
  y la ruta pase por el servicio. La ruta ya pasa por `ProjectContextService.detach`, que acepta
  ambos identificadores e invalida la caché por el evento normal.
- `[x]` **`_StampedPatchStore` (`routes/project_routes.py`) se ha eliminado.**
  Es un proxy que quita `updated_at` del parche antes de dárselo al store, escrito cuando
  `LINK_PATCHABLE_FIELDS` no aceptaba ese campo y `patch_link` lo rechazaba. Su propio docstring
  dice cuál era el arreglo permanente —«one word added to `LINK_PATCHABLE_FIELDS`»— y esa palabra
  **ya está añadida**, con su comentario al lado. El proxy sigue en pie, envolviendo cada llamada
  de `_context_service()`, sin cambiar nada. Borrarlo es quitar una clase y una línea; dejarlo es
  dejar un parche que finge arreglar algo que ya no está roto, que es como se acumulan las capas
  que nadie se atreve a tocar.
- `[x]` **`ProjectContextService` marca el vínculo cuando el resolver dice `missing`.**
  `refresh` sobre una fuente borrada emite `project_context_source_missing`, devuelve
  `RefreshResult(state="missing")` y **deja el vínculo exactamente como estaba** — que es lo
  correcto en la mitad importante (el vínculo sobrevive, sigue siendo evidencia y se puede
  desvincular; buscar otra fuente con el mismo título es justo lo que §19 prohíbe), pero la
  consecuencia es que **el estado no se persiste en ninguna parte**: la forma normalizada de
  `ProjectStore.normalize_link` no tiene campo de estado de fuente. `list_links` sigue diciendo
  `index_status: "ready"` de algo que ya no existe, y sólo un `inspect` por vínculo —una llamada
  por fila, que es lo que hace hoy la pantalla con cuatro workers en paralelo— descubre que está
  roto. Añadir el campo es fácil; lo que hay que decidir antes es si un estado observado se guarda
  junto a la pertenencia (y entonces caduca, y hay que decir cuándo se leyó) o si la lista se queda
  siendo membresía pura y la salud se pide aparte. Se eligió persistir `source_state`,
  `source_checked_at` y `source_message`: pertenencia y salud siguen siendo datos distintos,
  pero la lista ya muestra el último estado observado sin una consulta por fila.
- `[+]` **La clave de la URL de la pestaña de proyecto es una etiqueta traducida.** `TABS` en
  `studio/src/screens/Project.tsx` mezcla dos vocabularios: `brief` y `chats` en inglés, y
  `objetivos`, `memoria`, `actividad`, `contexto` y `ajustes` que son **exactamente la traducción
  al castellano** de sus etiquetas (`t('Context')` → «Contexto» en `i18n/es.ts`). Un enlace
  profundo del producto —`/projects/{id}?tab=contexto`, que `scripts/shot_studio.py` ya tiene
  cableado— depende así del idioma en el que se escribieron las etiquetas, no de un identificador
  estable. **Observado en el navegador: el enlace profundo cambia según el idioma.** El código de
  hoy no lo explica solo: `setTab` escribe `entry.id` y `rawTab` se valida contra `TABS`, así que
  la URL no debería moverse al cambiar de idioma; conviene reproducirlo con la UI en inglés antes
  de tocar nada, porque si de verdad cambia hay una segunda vía que no está en `Project.tsx`. El
  arreglo de fondo es el mismo en los dos casos: ids en inglés estables, con alias de los actuales
  para no romper los enlaces que ya existen.
- `[+]` **No hay `studio/checks/*.check.mjs` para proyectos, y la aritmética ya está exportada
  esperándolo.** `studio/src/adapters/projects.ts` saca fuera de los componentes todo lo que la
  pantalla afirma: `groupLinksByRole` (orden por el vocabulario de roles, no alfabético),
  `linkIsBehind` (dos hechos distintos —`index_status === 'stale'` y el `stale` que devuelve
  `inspect`— colapsados en un «refréscame»), `linkIsBroken`, `countLinks`, `shortRevision`,
  `refusalOf` (el 200 que dice que no) y `contextLinkFrom`. Es exactamente el mismo patrón que
  `adapters/context.ts` + `studio/checks/context.check.mjs` + `tests/test_studio_context_js.py`
  (102 comprobaciones), y aquí hay **cero**. Un panel cuyos números no se pueden verificar es
  decoración, y estos números deciden si el usuario cree que el agente puede escribir en una
  carpeta.
- `[x]` **Los seis fallos heredados de coherencia de tools están diagnosticados y cerrados.** Cinco en
  `tests/test_agent_loop_offer_execute_coherence.py` y uno en
  `tests/test_external_context_tool_gate.py`. Comprobado revirtiendo el cambio y corriendo con el
  mismo `data/` contra master: fallan igual. Los cinco del primero además **ya estaban anotados en
  este fichero desde el 04-09** («Lo primero que hay que mirar», §40.7), en el grupo de los 44
  rojos heredados de Windows. Ninguno se ha diagnosticado uno por uno todavía; lo que está probado
  era que no son de aquí. La disponibilidad dinámica retira `suggest_document` hasta que exista
  documento y la repone al abrir/crear uno; las skills editables siguen atravesando correctamente
  la aprobación exacta en vez de ejecutar como si fueran de confianza.
- `[+]` **El docstring de `src/project_context/service.py` dice que sus eventos no están en
  `EVENT_NAMES`, y ya lo están.** La sección «Events» explica que `_emit` intenta el `emit()` real
  y guarda el sobre en un buffer en memoria (`unrouted_events()`) porque los nombres
  `project_context_*` «are not in `src/contracts/event.py::EVENT_NAMES`, and that file is not this
  change's to edit». Los ocho nombres **sí** están ahí ahora, así que el camino de respaldo está
  muerto y el docstring describe un mundo anterior. El buffer no molesta —cuesta una lista vacía—
  pero `CONTEXT_EVENT_NAMES` y su comentario («Listed here so whoever adds them has the set»)
  también sobran, y quien lea el módulo se creerá que sus eventos no llegan a ninguna parte.

## Perfiles de agente y Completion Modes: lo cerrado, y lo que deja abierto (06-09-2026)

Perfiles de agente y Completion Modes (`FAUSTUS.md` §55, `OBJETIVOS.md`) cubre el grueso del plan 3
de 11: `src/agent_profiles/` (8 ficheros, 5.414 líneas), 15 campos nuevos en `AgentDef`, 7 rutas y
252 tests en verde. Lo que falta por construir está en `OBJETIVOS.md`; esto es lo que está
construido y **habría que mirar**.

- `[x]` **La fuga de `faustus-gate-*` quedó cerrada.**
  `tests/test_agent_gate.py::test_the_hook_script_is_not_left_behind` **sigue fallando después** de
  borrar las 264 carpetas huérfanas de `%TEMP%`: cada ejecución de la suite del gate deja una nueva
  (`1 failed, 74 passed`, y la aserción imprime el nombre de la carpeta que acaba de nacer). No es
  «basura acumulada»: es un fallo activo. La causa exacta, comprobada a mano, **no es un bloqueo de
  Windows**: `src/agent_gate.write_hook_script` termina con `os.chmod(path, 0o500)` a propósito
  —para que el agente ajeno no pueda reescribir su propio hook— y en Windows eso deja el fichero en
  modo `444`; `GateSession.close()` (`src/external_worker.py`) hace
  `shutil.rmtree(self._tmpdir, ignore_errors=True)`, `rmtree` no puede borrar un fichero de sólo
  lectura y devuelve `PermissionError [WinError 5] Access is denied`, y **`ignore_errors=True`
  convierte ese fallo en silencio**. La carpeta sobrevive para siempre y nadie se entera.
  Comprobado: un `os.chmod(path, S_IWRITE)` previo la borra a la primera, y un `rmtree` con un
  `onerror` que hace exactamente eso limpió las 25 que quedaban de una pasada. El arreglo correcto
  es ese manejador (dos líneas, y `ignore_errors` deja de hacer falta para el caso normal); un
  reintento con backoff no serviría, porque el modo del fichero no cambia con el tiempo. Mientras
  tanto el precio era una carpeta por run gated en `%TEMP%`, con el hook dentro. El cierre hace
  escribible el hook de sólo lectura antes de retirar el directorio y la regresión pasa en Windows.
- `[x]` **El diálogo de configuración efectiva se cortaba por la derecha** — cerrado en este mismo
  commit (`FAUSTUS.md` §55.10). Sacaba scroll horizontal y dejaba fuera la columna `from`, que es la
  que dice de qué nivel de precedencia salió cada valor y por tanto la única que justifica el
  diálogo. Arreglado en `studio/src/screens/agents/Defs.tsx` y `studio/src/screens/agents.css`: el
  diálogo se ensancha sólo para esta tabla, los anchos de `field` y `from` se declaran en porcentaje
  en vez de medirse del contenido, y un digest o una raíz de trabajo parten dentro de su celda.
  `node scripts/build-studio.js --force` y `tsc --noEmit`, limpios.
- `[x]` **La selección automática recibe una `TaskSpec` completa.**
  `resolver._choose` construye la spec con `_call_filtered(TaskSpec, **task)`, que pasa **sólo las
  claves que casan con campos de `TaskSpec`** — filtrar por firma es correcto y está bien
  justificado (los dos módulos se escribieron en paralelo, y adivinar la firma ajena rompe el día
  que crece), pero el diccionario `task` que llega es el contrato de override de §15 (`model`,
  `endpoint_id`, `max_rounds`, `timeout_s`, `completion_mode`…) y **no comparte casi ninguna clave**
  con `intent`, `description`, `required_capabilities`, `required_tools`, `mode`, `specialties` y
  `output_contract`. Consecuencia: cuando nadie nombra un agente, el ranking completo de §14 —que
  está escrito y probado con 35 tests— decide con casi nada, y el `fallback` alfabético gana más de
  lo que debería. Lo que falta es **el mapeo explícito de una tarea de dispatch a una `TaskSpec`**,
  y el sitio es el llamante, no el resolver. El llamante construye ahora el mapeo explícito y
  conserva capacidades, tools, intención, especialidades, modo y contrato de salida.
- `[~]` **Tres tests fijaban un número o un valor concreto en vez de un invariante, y hubo que
  reescribirlos al cablear los perfiles.** Es la cuarta vez que pasa lo de «Un patrón que ya se ha
  repetido tres veces», arriba, y merece quedar escrito con los tres casos por nombre porque los
  tres fallaron **por la misma razón** —el catálogo pasó de 3 a 11 definiciones y dos built-in
  pasaron a declarar cosas— y ninguno señalaba un fallo real:
  - `test_a_user_file_replaces_a_builtin_of_the_same_slug` comparaba contra un **número literal** de
    definiciones. Ahora mide la **delta** (`len(after) == before`) y que el slug aparezca una sola
    vez, que es la regla que quería fijar: reemplaza, no añade.
  - `test_the_payload_a_task_carries_gains_the_new_fields_and_loses_nothing` afirmaba que
    `default_completion_mode` era el **valor por defecto del módulo**. Ahora lo compara contra lo
    que la definición declara (`defs.get("implementer").default_completion_mode`) y contra el
    vocabulario; afirmar el defecto sólo probaba que ninguna definición declara nunca un modo.
  - `test_resolve_ignores_owner_and_project_id_from_the_body` daba por ganador al
    `project_default` del cuerpo. Con `reviewer` declarando `professional`, `agent_default`
    **outranks** `project_default` por diseño, así que ahora el test afirma que el
    `project_default` **pierde** y que el `task_override` gana, que es la regla de §1.6 y no un
    accidente del catálogo de septiembre.

  La regla, otra vez y ahora con la variante nueva: **no fijes un inventario ni un valor por
  defecto; fija la relación.** «Reemplaza en vez de añadir», «lo que la definición declara es lo que
  viaja» y «este nivel gana a ese otro» sobreviven a que el catálogo crezca. «Son once», «es
  `greedy`» y «gana el proyecto» caducan el día que alguien implementa lo que el test daba por
  inexistente.

## Modo Consejo: lo cerrado, y lo que deja abierto (06-09-2026)

**Nota de cierre (06-09-2026, antes de mezclar `feat/council`).** Esta lista se auditó entera antes
del merge. De los seis `[!]`, **cinco quedan arreglados** y uno se queda como está a propósito; los
`[~]` baratos también. La marca de cada entrada dice en qué estado quedó y el párrafo *Arreglado*
explica qué se hizo — el diagnóstico original se conserva debajo porque describe el fallo mejor que
cualquier resumen, y porque es el tipo de fallo que vuelve.

Lo que protege el arreglo es `tests/test_council_wiring.py` (25 pruebas). No pregunta si un módulo
funciona —de eso ya hay 457 pruebas, todas verdes mientras estos seis fallos estaban vivos— sino si
los módulos **se alcanzan entre sí**. Es la cuarta vez en este proyecto que un subsistema se entrega
construido y desconectado (fuentes derivadas sin registrar, `LINK_PATCHABLE_FIELDS` sin
`updated_at`, diez perfiles que no llegaban al loader), y la primera en que se deja una prueba cuyo
único trabajo es esa pregunta.

El plan 4 de 11 está construido y documentado en `FAUSTUS.md` §56. Lo que falta por
construir está en `OBJETIVOS.md`; esto es lo que **ya está construido y no cuadra**. Casi todo lo de
abajo son desajustes **entre módulos** que se escribieron a la vez: cada uno hace lo correcto por su
cuenta y la línea que los une no existe o dice otra cosa.

- `[x]` **El ledger emite sus eventos a una lista en memoria, no al flujo de la sala.**
  *Arreglado.* `CouncilLedger` acepta un `publisher` (con un centinela `UNSET_PUBLISHER`, para que
  «sin configurar» y «deliberadamente mudo» no sean la misma instrucción), `_emit()` publica además
  de anexar, y `CouncilOrchestrator.__init__` llama a `ledger.publish_to(self._events)` — sin eso el
  ledger resolvería el flujo del registro de módulo mientras la sala escucha el que le inyectaron, y
  una sala tendría dos flujos. Los ocho nombres finos del ledger (`council_task_added`,
  `council_task_assigned`, `council_task_status`, `council_claim_conflicted`,
  `council_claim_handoff_refused`, `council_claim_transferred`, `council_objection_resolved`,
  `council_decision_superseded`) se añadieron a `COUNCIL_EVENTS` **y** a `EVENT_NAMES`. Una prueba
  lee los `_emit("…")` del propio fuente con `ast` y falla si aparece uno sin declarar: es la única
  forma de cubrir los que ninguna prueba dispara, y `publish()` los reescribiría como
  `council_error` en silencio. `service.ledger()` construye el suyo con `publisher=None` — una vista
  de lectura no debe poder anunciar nada. Diagnóstico original:
  `CouncilLedger._emit()` construye el evento y hace `self.events.append(event)` — y nada más. No
  toca `events.stream_for(session_id)`. Consecuencia medible: `council_claim_acquired`,
  `council_objection_recorded` y `council_decision_recorded` **no llegan nunca al SSE**, así que una
  página abierta no se entera de que se ha reclamado un fichero, de que alguien ha objetado ni de que
  se ha tomado una decisión; sólo lo ve si vuelve a pedir `/ledger`. La cobertura es además desigual
  y eso lo hace peor que un hueco limpio: el orquestador sí publica `council_claim_released` al
  cancelar y al parar a un participante, y `council_task_handed_off` al traspasar, de modo que un
  claim **liberado** aparece en el flujo y el mismo claim **adquirido** no. Segundo desajuste dentro
  del mismo: el ledger emite los nombres `council_task_status` y `council_task_assigned`, que **no
  están en `COUNCIL_EVENTS`**; el día que alguien conecte `_emit` al flujo, `publish()` los
  reescribirá como `council_error` con `unknown_event` — que es el comportamiento correcto de
  `events.py` y una sorpresa garantizada para quien haga la conexión. El arreglo son dos decisiones,
  no una: pasarle el flujo al ledger (por constructor, como ya recibe `store` y `claims_backend`), y
  añadir los dos nombres a la tupla cerrada o renombrarlos a los que ya existen. Mientras tanto la
  pantalla `/council` recarga el ledger cuando llega cualquier evento de mensaje o de estado de
  turno, que funciona pero es un sondeo disfrazado.

- `[x]` **El cierre no puede decir `verified` hoy, y no porque falte evidencia.**
  *Arreglado, y por los dos extremos.* `EXECUTION_KEYS` no dejaba pasar `changes` ni `verification`,
  así que el orquestador tiraba justo la observación que `verify_task` necesita: sin ella no había
  ChangeSet que juzgar y ninguna tarea podía probarse nunca. Ahora las acepta (son medidas de
  `dispatch.compact`, no afirmaciones de un modelo — siguen fuera de `INVOCATION_KEYS`), y
  `_run_task` llama a `_verify()` cuando el estado llega a `done` o `review`. Ese camino **sólo
  puede bajar la afirmación**: sin verificador en la build la tarea se queda en `done`; si `prove`
  no dice `proved`, se queda en `done` y el paquete se guarda igual (un «miramos y no pudimos
  demostrarlo» es un resultado que el cierre tiene que poder contar); y si el ledger veta por una
  objeción bloqueante abierta, se queda en `done` y se anuncia el veto. Sólo un paquete `proved`
  **y** un ledger que consiente escriben `verified` y publican `council_activity_verified`.
  `verify_task` es síncrono y lee el disco, así que corre en `asyncio.to_thread`: una sala con el
  bucle bloqueado en un diff no puede responder a un `pause`. `_summary()` pasa ahora
  `proof=self._proof_packet()`, que elige el veredicto **más débil** de los recogidos — cuatro
  tareas probadas y una contradicha no son una sesión verificada. Y `verified` se añadió a
  `contracts.TASK_STATUSES` y a `TERMINAL_TASK_STATUSES`: sin él, «lo dijo el worker» y «lo
  comprobamos» serían la misma palabra. Diagnóstico original:
  `synthesis.status_of()` sólo llega a `verified` con un paquete de `prove` cuyo veredicto sea
  `proved`, y `orchestrator._summary()` llama a `_synthesis.build(self._ledger, messages=…,
  usage=…, stop_reason=…)` **sin `proof=`**. Nada en el turno construye ese paquete: `verify_task`
  existe, está probado y no lo llama nadie desde el orquestador. Resultado: todo cierre sale como
  mucho `decided` o `unverified`, `verification.verdict` es siempre `"none"` y la nota que lo
  acompaña dice literalmente «no proof packet was supplied». La escalera funciona; le falta el
  peldaño de entrada. Es coherente con no mentir —mejor `unverified` que un `verified` inventado—
  pero hay que decirlo en voz alta: **hoy la palabra `verified` es inalcanzable por la vía normal**.

- `[x]` **Las líneas de contribución salen sin nombre y sin roles, y el participante que calló
  desaparece.** *Arreglado.* `build()` tiene ahora `participants=` (palabra clave, como el resto: la
  prueba que fijaba la lista exacta de parámetros se reescribió para fijar la **garantía** —ningún
  argumento por el que pueda entrar una frase— en vez de una foto de la firma, que es lo que la hizo
  fallar), `_summary()` le pasa `self._participants()`, y `_row()` acepta una cadena suelta como id
  porque el orquestador cae a una lista de ids cuando no puede leer los asientos: un cierre que
  descartara esas filas informaría de una sala vacía justo cuando su estado es más difícil de leer.
  Diagnóstico original:
  `synthesis.build()` hace `contributions=tuple(contributions(messages, ()))` — con la
  lista de participantes **vacía**. El docstring de `contributions()` promete dos cosas que su único
  llamante rompe: que `display_name` y `roles` vengan del participante (con `()` cae siempre al
  `participant_id` crudo y a una lista vacía) y que «un participante que no dijo nada sigue teniendo
  su fila, porque el silencio en una sala a la que se le pidió opinión es información» (con `()` no
  hay de dónde sacar esa fila). Y `build()` **no tiene parámetro `participants`**, así que ni un
  llamante que quisiera hacerlo bien podría: el arreglo toca la firma.

- `[x]` **El consumo que llega al cierre no distingue entrada de salida.**
  *Arreglado, y de paso salió un fallo de aritmética que sólo se ve cuando el número se pinta.* El
  orquestador lleva ahora su propia suma de `input_tokens`/`output_tokens` (`_spend()`, que es el
  único sitio que ve todos los resultados) y `_usage()` la publica junto al total del scheduler, que
  sigue siendo el número contra el que se aplicó el presupuesto. Un `usage` que sólo trae un total
  no se parte a ojo: inventar el reparto sería una medida que este módulo no hizo. **Y el fallo:**
  con la pantalla ya pintando el reparto, un turno real dio `Tokens in/out/total: 1228/79/79` — un
  total menor que su propia entrada. `_tokens()` caía a `output_tokens` cuando el endpoint no manda
  `total_tokens`, así que el presupuesto se cobraba la respuesta y no la pregunta. Ahora suma prompt
  y compleción, que son dos mitades de un número, no dos candidatos. Diagnóstico original:
  `orchestrator._usage()` devuelve `total_tokens`, `calls`, `wall_seconds`, `waited_ms` y `source`,
  y `synthesis._usage()` espera además `input_tokens` y `output_tokens`, que rellena a 0. Todo cierre
  informa por tanto `0/0/N` tokens. Los números existen aguas arriba —`StreamingChatInvoker` devuelve
  el `usage` de la llamada— y se pierden al agregarlos en el scheduler, que sólo cuenta el total.

- `[~]` **`/api/council/{id}/usage` se pone a cero al reiniciar.** `service.usage()` lee
  `scheduler.stats()`, y el registro de schedulers es un diccionario de proceso: los presupuestos
  persisten en `council.db` y **el gasto no**. Una sala que consumió 80.000 tokens ayer informa hoy
  de 0 gastados sobre un límite de 120.000, lo que no es sólo un número feo: `can_start()` volvería a
  dejar arrancar llamadas que el presupuesto ya no debería permitir. No está claro si es deuda o
  decisión —el plan dice «presupuesto por actividad» y una actividad no sobrevive al reinicio— pero
  hoy no lo dice ni el código ni la API.

- `[x]` **El aviso de suplantación existe, se calcula y se pierde al recargar.**
  *Arreglado por el camino limpio y sin campo nuevo.* `claims_identity()` es una función del
  contenido, y el contenido **sí** se guarda; el error era guardarla otra vez. `service.messages()`
  la recalcula por fila al servir el transcript, así que la advertencia es la misma cadena que
  produjo el evento y no puede divergir de él después de un cambio de build. El adaptador leía sólo
  `metadata.claims_identity` —donde nada escribía nunca— y ahora lee el campo de la fila con
  `metadata` como respaldo para mensajes guardados por builds anteriores. Diagnóstico original:
  `CouncilMessage.claims_identity()` funciona y el orquestador lo publica en el evento
  `council_message` (`claims_identity=claimed`). Pero **no se guarda**: `CouncilMessage` no tiene
  campo para ello y el orquestador no lo mete en `metadata`, así que `GET /{id}/messages` no lo trae.
  Un usuario que estaba mirando cuando llegó el mensaje ve el aviso; el que abre la sala mañana, no —
  y es el segundo el que está auditando. El arreglo barato es `metadata: {"claims_identity": …}` al
  componer el mensaje; el limpio es un campo. La pantalla ya lee `metadata.claims_identity` y no
  reimplementa el detector: dos copias de esas expresiones regulares divergirían, y la que se
  equivocara sería la del navegador.

- `[x]` **Nada expone `orchestrator.state()`.** *Arreglado:* `CouncilService.state()` y
  `GET /api/council/{id}/state`, verificado contra la app real. Lee el orquestador que **este**
  proceso tiene y no construye uno: fabricar un coordinador para responder a un GET abriría una sala
  que nadie pidió. Una sala sin turno en vuelo responde `live: false` con lo que sabe el store; una
  viva añade `mutating`, `running_tasks`, `late_results`, `cancelled_turns`,
  `stopped_participants` y el `usage`. `scripts/council_openapi_check.py` gana una lista
  `BEYOND_PLAN` para que la ruta 16 aparezca como *declarada fuera del §13* y no como una sorpresa.
  Diagnóstico original: ahí viven `paused`, `stopped_participants`,
  `mutating` (qué recurso está escribiendo ahora mismo un participante), `running_tasks` y
  `late_results`, y su docstring dice que es «todo lo que una ruta, una página o un test necesitan
  saber». `CouncilService` no tiene método que lo devuelva y `council_routes.py` no tiene ruta.
  Consecuencia concreta en la interfaz: «este participante está parado» y «este resultado llegó tarde
  y no cuenta» **no se pueden pintar**; la pantalla deduce la pausa del `status` de la sesión, que es
  una aproximación (el `_paused` del orquestador y el `status` persistido pueden no coincidir si el
  `_set_session_status` falla, y ese caso está contemplado en el código con un `persisted: False`).

- `[~]` **El único camino a un cierre completo es pedir una síntesis, y pedirla escribe.**
  `council_activity_completed` lleva sólo `status` y `stop_reason`; el `CouncilSummary` entero viaja
  dentro de `TurnOutcome`, que sólo sale por el valor de retorno de `run_turn` (que nadie devuelve al
  cliente) y por `command("request_synthesis")`. Así que una página que se recarga después de un
  turno terminado puede enseñar la palabra del veredicto y para leer las decisiones, los cambios y la
  verificación tiene que **pedir otra síntesis** — que publica un `council_activity_completed` nuevo.
  Un «leer» que escribe en el flujo de auditoría. Falta un `GET /{id}/summary` que construya el cierre
  desde el ledger sin emitir nada; `synthesis.build()` ya es puro, así que es una ruta y un método.

- `[~]` **`preset_id` se acepta y no se guarda.** La ruta lo lee del cuerpo, `service.create()` lo
  recibe y lo escribe en un `logger.info`. Está razonado en el docstring —`CouncilSession` no tiene
  campo y ensanchar un contrato ajeno para anotar algo que la ruta ya expandió en `participants` sería
  el peor de los dos errores— y aun así el efecto neto es que **la sala no sabe de qué preset salió**,
  que es justo lo que hace falta para «vuelve a abrir esta misma mesa» y para las métricas por
  política de `OBJETIVOS.md`.

- `[~]` **`blind_round` paga una llamada de juez que nadie pidió.** `tournament.run` rankea siempre
  sus finalistas, así que una ronda ciega de consejo gasta un juez aunque el llamante sólo quisiera
  las respuestas. Está dicho en el docstring del método y la salida deja `answers` y `judge`
  separados para que se pueda ignorar, pero el coste ya se pagó. Arreglarlo es un parámetro en
  `tournament.run` (`rank=False`), no un cambio en el consejo.

- `[x]` **Tres conjuntos del ledger nombran estados de tarea que el contrato no tiene.**
  *Arreglado el que importaba.* `verified` **está** ahora en `contracts.TASK_STATUSES` y en
  `TERMINAL_TASK_STATUSES`, porque hace falta para el arreglo de arriba y porque la alternativa era
  que «lo dijo el worker» y «lo comprobamos» compartieran palabra. Los comentarios de `ledger.py` y
  `synthesis.py` se actualizaron: ya no dicen que el contrato no lo tiene, y `synthesis.py` explica
  la diferencia entre los dos en vez de copiar el conjunto sin razón. Queda `ready`/`assigned` en
  `STARTABLE_TASK_STATUSES` y `ASSIGNED_TASK_STATUSES`, que son los nombres del **plan** para lo que
  el contrato llama `pending` y `claimed`: no se corresponden con ningún valor legal, así que la
  condición sencillamente nunca los ve. Inofensivo y todavía confuso. Diagnóstico original:
  `SATISFIED_TASK_STATUSES` y `VERIFICATION_TASK_STATUSES` incluyen `verified`, y
  `STARTABLE_TASK_STATUSES` incluye `ready` y `assigned`; `contracts.TASK_STATUSES` es
  `pending|claimed|running|review|done|blocked|failed|cancelled` y no tiene ninguno de los tres.
  `ledger.py` **lo dice** en un comentario y lo hace a propósito (que una vocabulario futuro no deje
  de satisfacer dependencias en silencio), y `_check_vocabulary` seguiría rechazando un
  `set_task_status(…, "verified")`, así que no rompe nada hoy. Lo que sí es deuda: `synthesis.py`
  copia el mismo conjunto **sin el comentario**, de modo que quien lea sólo ese fichero concluirá que
  `verified` es un estado de tarea legítimo.

- `[x]` **`synthesis.ABSTENTION_TYPES` cuenta una palabra que no existe.** *Aclarado sin cambiar el
  comportamiento:* el comentario dice ahora que `abstention` es el valor del contrato y `abstain` la
  tolerancia para un invoker que devuelva el verbo en vez del sustantivo — cosa que el orquestador
  traduce, pero un llamante directo de `contributions()` no. Quitar la mitad muerta habría sido más
  limpio y también más frágil. Diagnóstico original: es
  `{"abstain", "abstention"}` y `MESSAGE_TYPES` sólo tiene `abstention`; la mitad del conjunto está
  muerta. Inofensivo y confuso: el lector deduce que hay dos formas de abstenerse.

- `[x]` **`/api/council/config` no publica media docena de vocabularios que una interfaz necesita.**
  *Arreglado, los diecisiete.* `config()` manda ahora `turn_states`, `terminal_turn_states`,
  `message_types`, `visibilities`, `author_kinds`, `task_statuses`, `terminal_task_statuses`,
  `claim_kinds`, `claim_states`, `objection_targets`, `objection_severities`, `objection_statuses`,
  `decision_statuses`, `stop_reasons`, `reserved_ids`, `events` (los 23 de `COUNCIL_EVENTS`),
  `writing_profiles`, `close_statuses`, `round_modes`, `blindness` y `debate_phases`, cada uno leído
  del módulo que lo posee. `FALLBACK_WRITING_PROFILES` sigue en el adaptador y ahora es lo que dice
  ser: un respaldo para un servidor anterior al campo, no una segunda opinión sobre permisos.
  `CLOSE_STATUSES` también se queda, porque los cinco llevan **etiqueta y tono** y eso no es
  competencia del servidor; teniendo la lista de ambos lados, un sexto estado aparece como uno que
  el navegador no sabe pintar en vez de como uno que pinta mal. Verificado contra la app real: 29
  claves en `/api/council/config`. Diagnóstico original:
  Manda `policies`, `roles`, `tool_profiles`, `policy_tool_ceilings`, `completion_modes`,
  `session_statuses`, `default_budgets`, `commands`, `errors` y `verdicts`. **No** manda
  `WRITING_PROFILES` (qué perfiles escriben — sin él, distinguir un asiento de sólo lectura de uno
  que puede escribir obliga a repetir la lista en el front, que es lo que hace hoy
  `adapters/council.ts` con un `FALLBACK_WRITING_PROFILES` y una nota), ni `synthesis.STATUSES` (los
  cinco estados de cierre que la pantalla tiene que pintar), ni `MESSAGE_TYPES`,
  `OBJECTION_SEVERITIES`, `TASK_STATUSES`, `CLAIM_STATES`, `STOP_REASONS` o `VISIBILITIES`, ni las
  fases, modos y valores de ceguera de `policies.py`. La ruta hace lo correcto —`config()` lee de los
  módulos que poseen cada lista, así que añadir una es una línea— pero mientras no se añadan, la
  promesa de «el formulario se pinta desde el servidor» se cumple sólo para la mitad de la pantalla.

- `[x]` **El *seam* `use_orchestrator` de `service.py` conecta de verdad — comprobado.** El servicio
  se escribió contra un orquestador mockeado y `_ORCHESTRATOR_CONTRACT` es una tupla de cadenas, no un
  `Protocol`, así que nada lo verificaba automáticamente. Comprobado a mano contra
  `orchestrator.py`: `_orchestrator_for()` construye con `(session, store=, scheduler=, events=,
  invoker=, executor=, clock=)` y `CouncilOrchestrator.__init__` acepta exactamente esos (más
  `ledger=`, opcional); y las ocho llamadas de `_send()` casan con las firmas reales —`pause()`,
  `resume()`, `request_synthesis()`, `cancel_turn(turn_id, *, actor)`,
  `stop_participant(participant_id, *, actor)`, `steer(participant_id, message, *, actor)`,
  `handoff_task(task_id, to, *, actor)`— incluyendo el paso por nombre de los posicionales, que es lo
  que se rompería primero. `assign_role` no va al orquestador a propósito: cambia un asiento
  guardado, no un turno en vuelo. La asimetría que quedaba —`state()` en el contrato
  escrito y sin llamante— se cerró con `CouncilService.state()` y `GET /{id}/state`. El
  contrato sigue siendo una tupla de cadenas y no un `Protocol`; ahora hay al menos una prueba que
  compara las firmas de la costura nueva
  (`test_the_verifier_the_room_calls_has_the_shape_the_adapter_offers`), que es el mismo truco
  aplicado donde más barato sale.

### Lo que se queda abierto a propósito

Cuatro cosas de arriba siguen en `[~]` y ninguna es un descuido:

- **El gasto se pone a cero al reiniciar.** Persistirlo es una tabla y una decisión sobre qué
  significa «presupuesto por actividad» cuando la actividad sobrevive al proceso. Hasta que se
  decida, `/usage` dice de dónde salen los números (`source: council_scheduler`) y `state()` los
  repite, así que al menos se ve que son de este proceso.
- **`request_synthesis` es la única vía a un cierre completo, y escribe.** Falta un
  `GET /{id}/summary` que construya el cierre sin emitir nada; `synthesis.build()` ya es puro, así
  que es una ruta y un método. No entra aquí porque el arreglo de `verified` cambia lo que ese
  endpoint devolvería, y es mejor añadirlo cuando el cierre esté asentado.
- **`preset_id` se acepta y no se guarda.** Sigue necesitando un campo en `CouncilSession` y su
  migración; ensanchar el contrato por un `logger.info` era el peor de los dos errores y lo sigue
  siendo.
- **`blind_round` paga un juez que nadie pidió.** El arreglo está en `tournament.run` (`rank=False`),
  no en el consejo, y tocar el torneo desde la rama del consejo mezcla dos cosas.

---

## State Mirror: lo cerrado, y lo que deja abierto (06-09-2026)

El plan 5 de 11 está construido hasta la fase 3 (contratos, estado interno, máquina local,
reconciliación) y documentado en `FAUSTUS.md` §57. El Opportunity Engine —fases 5 a 7— **no está**;
lo que falta por construir vive en `OBJETIVOS.md`. Esto es lo que **ya está construido y no cuadra**.

**Nota de método.** `tests/test_state_mirror_wiring.py` (23 pruebas) se escribió antes del merge,
con el único trabajo de preguntar si las piezas se alcanzan entre sí — la respuesta a que este
proyecto haya entregado un subsistema desconectado cuatro veces seguidas. Cazó una de las tres cosas
de abajo. Las otras dos salieron de abrir la pantalla y leer lo que la máquina real contestaba, que
sigue siendo el único método que encuentra lo que ninguna prueba busca.

### Cerrado antes de mezclar

- `[x]` **Cinco adaptadores escritos, probados y sin cablear.** `workspace`, `services`, `models`,
  `hardware` y `connections` importaban, pasaban sus pruebas y no estaban en `ADAPTER_FACTORIES`,
  que es la única tupla que hace que un adaptador exista. La prueba que ahora lo impide recorre el
  paquete con `pkgutil` en lugar de leer una lista, porque una lista es exactamente lo que estaba
  mal. La prueba hermana que había en `test_state_mirror_adapters_internal.py` comparaba conjuntos
  con `==` y por tanto fallaba al crecer el paquete: se reescribió como subconjunto, porque el
  reflejo ante ese fallo es **encoger `ADAPTER_FACTORIES`**, que desconectaría cinco adaptadores que
  funcionan para poner verde una prueba en el fichero equivocado.

- `[x]` **Un barrido impecable que no observaba nada.** Los once adaptadores corrían, cero fallos, y
  `workspace` y `objectives` devolvían cero. A los dos se les entregaba un `Scope` sin carpeta y sin
  proyecto. Los adaptadores hacen bien en no adivinar una carpeta; la resolución pertenece al
  barrido, que es la única capa que sabe quién pregunta. Arreglado en `reconcile._with_default_folder`.

- `[x]` **Y el arreglo se equivocó dos veces, de la misma manera.** Primero llamó a
  `store.list_projects()` —un método que no ha existido nunca— envuelto en un `if callable(...)` que
  convirtió el error en silencio; luego, ya con `store.list()`, leyó `folder` (el **nombre**) en vez
  de `workspace` (la **ruta**), y el síntoma fue un `workspace_available: false` impecablemente
  honesto sobre un repositorio que estaba ahí mismo. Las dos versiones pasaban su prueba, porque el
  doble de la prueba tenía las mismas claves inventadas que el código. Ahora el doble **hereda de
  `ProjectStore`**, así que un método renombrado falla, y el comentario en `reconcile.py` dice la
  regla: *un `getattr` con respaldo sobre un método que debería existir no es robustez, es una forma
  de no enterarse.*

- `[x]` **Una tarjeta que se contradecía a sí misma.** Pintaba «nothing has been observed about this
  yet» encima de «1 field(s)». `decidingField` devuelve nulo cuando el campo decisorio del schema no
  se ha observado, que no es lo mismo que una entidad sin campos. En una pantalla cuyo propósito
  entero es que le crean sobre qué se sabe, eso no es cosmético.

- `[x]` **El `text-overflow: ellipsis` de las tarjetas estaba escrito, era correcto y no hacía
  nada.** Un hijo flex no encoge por debajo de su contenido sin `min-width: 0`, así que una sesión
  bautizada con su primer mensaje se salía de su tarjeta y cruzaba por encima de la de al lado.

- `[x]` **La proyección de proyecto estaba escrita y no era alcanzable.** Ya existe
  `GET /api/state/project`, con dueño y vocabularios validados, y el Context Engine la consume
  mediante `StateMirrorSource` sin lanzar sondeos en el camino caliente.

- `[x]` **El intervalo de barrido era un ajuste decorativo.** El supervisor arranca ahora el
  bucle del State Mirror; deduplica ámbitos de usuario/proyecto, nunca usa un dueño vacío, respeta
  la bandera y cede mientras haya un chat activo.

### Lo que se queda abierto, y por qué

- `[~]` **`run_state.v1` está diseñado como si sólo existiera `dispatch`.** `phase`, `progress`,
  `worker_states`, `last_heartbeat` y `proof_status` no tienen equivalente en `agent_runs`,
  `media_runs` ni `bg_jobs`, y `budget_remaining` no lo tiene en ninguno de los cuatro. Un schema
  que declara campos que tres de sus cuatro fuentes no pueden llenar hace que esas tres parezcan
  incompletas cuando en realidad hablan otro idioma. O el schema se parte, o se declara
  explícitamente que esos campos son de dispatch.

- `[~]` **`last_heartbeat` no sobrevive a un reinicio ni siquiera en dispatch.** `dispatch._load()`
  reconstruye un job desde su espejo JSON y restaura todo **menos** `events` y `worker_states`. En
  la máquina real los 93 jobs salían de disco y ninguno de los dos campos apareció una sola vez.

- `[~]` **`approval_pending` es inalcanzable, y por una razón que merece decidirse.** `ApprovalRow`
  tiene columna `run_id`, así que el join existe en los datos — pero guarda un id desnudo sin motor,
  y los ids de aquí son `<motor>:<run_id>`. Una aprobación no puede nombrar la ejecución a la que
  pertenece. O `ApprovalRow` gana una columna de motor, o hace falta un resolvedor que busque un id
  desnudo en los cuatro registros. Conviene decidirlo **antes** de construir la arista
  aprobación→ejecución.

- `[~]` **Los 93 jobs de dispatch de esta máquina no tienen dueño, y por eso el espejo no los ve.**
  No es un fallo del adaptador: `dispatch.visible_to` dice, en su propio docstring, que «un job sin
  dueño no es de nadie en modo multiusuario», y el barrido corre como `admin`. Es correcto y es
  desconcertante — la pantalla de Activity los enseña y el espejo no. Lo que falta no es ensanchar
  el filtro (eso sería una fuga), sino decidir qué pasa con el trabajo huérfano de antes de que se
  activara la autenticación.

- `[~]` **`connection_state` sólo publica `configured`.** `authenticated`, `reachable` y
  `granted_actions` son negativas deliberadas, no huecos: una credencial guardada no es una
  credencial aceptada, nada en un barrido abre un socket, y ningún almacén registra qué se le
  permite hacer a una conexión. Un token revocado se ve exactamente igual que uno vivo.

- `[~]` **`models` está callado hasta que otra cosa haya sondeado `/api/system/usage`.** Todas las
  rutas a `/api/ps` son corrutinas y un barrido síncrono no arranca un bucle, así que el adaptador
  lee el documento que `collect_usage` deja en su caché y lo sella con el `ts` de ese documento. Un
  proceso recién arrancado da cero observaciones. Es el modo de fallo honesto —un documento de hace
  diez minutos se califica `stale`, no `fresh`— pero significa que el estado de los modelos es tan
  reciente como el sondeo de la interfaz.

- `[~]` **`head` y `last_verified_changeset` no tienen fuente.** Nada público en el repositorio
  devuelve el SHA de HEAD de un workspace (`git_invariants` es quien sabe ejecutar git con
  seguridad, y sólo se le añadió `current_branch`), y `src/changesets.py` no persiste nada: su
  propio docstring dice «nothing is stored». No hay un último ChangeSet al que apuntar.

- `[~]` **`current_branch` no distingue HEAD desprendido de fallo.** Devuelve `""` para los dos, así
  que en HEAD desprendido el campo se **omite** en vez de decir qué pasa. Distinguirlos exige
  ensanchar el tipo de retorno.

- `[~]` **Un conflicto no tiene camino a `resolved` ni a `superseded`.** Sólo a `abandoned`, que
  `reconcile` escribe cuando la entidad se retira. Un conflicto no puede resolverse solo con lo que
  hay: `MaterializedState` guarda UN valor por campo con UNA fuente, así que una vez plegada la
  discrepancia no queda contra qué comparar las dos afirmaciones. `next_check` nombra lo que lo
  zanjaría y **nadie lo ejecuta**. Hace falta quien decida: una ruta, una persona, o una re-sonda
  dirigida que mantenga vivas ambas afirmaciones.

- `[~]` **Nada reproduce el log de observaciones para reconstruir el estado.** El log es
  append-only y `reducers.reduce_many` existe, pero no hay `replay()`. El §26 lo pide como criterio
  de aceptación («reproducir eventos reconstruye el mismo estado materializado o declara
  incompatibilidad de schema») y es lo que convertiría el schema versionado en una garantía en vez
  de en una convención.

- `[~]` **Las consultas de situación llevan sus propios vocabularios de estado.**
  `contracts.SCHEMA_FIELDS` declara que una ejecución tiene `status`, no cuáles existen, así que
  `queries.py` declara `RUNNING_RUN_STATUSES`, `PROVEN_VERDICTS` y compañía citando en comentarios
  de dónde los sacó. Si `dispatch` o `capability_registry` añaden una palabra, la consulta deja de
  reconocerla en silencio y **ninguna prueba dentro de este paquete puede cazarlo**.

- `[~]` **`artifact_state.v1` tiene cinco campos sin fuente**: `approval`, `latest_version`,
  `derivatives`, `stale_derivatives` y `publication_state`. `derivatives` es alcanzable vía
  `src/artifact_identity.py::derivatives_for()`; los otros cuatro no tienen origen en ninguna parte
  del repositorio.

- `[~]` **`session_state.workspace` y `last_activity_at` están vacíos** porque el dataclass
  `core.models.Session` no los lleva, aunque la tabla `sessions` sí tiene `last_message_at` y
  `last_accessed`. Es el mismo hueco que el plan 2 arregló para `folder`, `mode` y `project_id`,
  una fila más abajo.

- `[~]` **`token_budget` de una proyección es una estimación de 4 caracteres por token**, no una
  llamada al tokenizador. Sobrecuenta la puntuación JSON, así que se queda corto antes que pasarse —
  la dirección segura, pero conviene saberlo antes de ajustar un presupuesto fino.

---

## Universal Delta Engine: lo cerrado, y lo que deja abierto (06-09-2026)

El plan 6 de 11 está construido hasta la fase 2 (contrato común, código, workflows/skills/estado) y
documentado en `FAUSTUS.md` §58. Imagen, vídeo y audio —fases 4 y 5— **no están**; lo que falta por
construir vive en `OBJETIVOS.md`. Esto es lo que **ya está construido y no cuadra**.

**Nota de método.** `tests/test_delta_engine_wiring.py` (22 pruebas) se escribió antes del merge y
cazó tres de las cuatro cosas de abajo. La cuarta —el `matched` sobre un contrato vacío— la encontró
el agente que escribió los tests de servicio, leyendo el docstring del servicio y comprobando si el
código lo cumplía. Las dos técnicas se complementan: una pregunta si las piezas se alcanzan, la otra
si el código hace lo que su propia documentación promete.

### Cerrado antes de mezclar

- `[x]` **Una regresión bloqueante archivada como `info`.** El adaptador de código informaba
  `security.permissions_not_widened: violated, blocking` por un `import subprocess` nuevo y emitía
  ese mismo import como un `added` sin `invariant_refs`. El clasificador no podía unirlos —el
  invariante no declara ruta, el hallazgo no nombraba invariante— así que la fila que **causaba** la
  violación quedaba `incidental / info`. Arreglado con un join explícito en el adaptador
  (`_widening_invariants`), que es la capa que sabe que importar `subprocess` es una ampliación; el
  clasificador es deliberadamente ciego a lo que una ruta significa en un dominio concreto.

- `[x]` **Las rutas cazaban la clase hija.** `except DeltaError` no coge un `ContractError`, que es
  su **padre** y lo que levantan todos los helpers de `src/contracts/base.py`. `POST /api/deltas`
  sin `source` —el error de cliente más común— contestaba 500 en vez del rechazo 200 que el propio
  docstring del fichero promete. Ocho manejadores. Hay una prueba de cableado que falla si alguien
  vuelve a la subclase.

- `[x]` **El interruptor apagado dejaba basura en el almacén.** `create` congelaba el contrato y
  guardaba la petición **antes** de mirar el flag, así que una comparación rechazada dejaba las dos
  filas detrás; y `create(run=False)` con el motor apagado triunfaba entero, porque en ese camino
  nadie leía el interruptor. Invisible desde HTTP —la ruta corta antes— y real para cualquier otro
  llamante.

- `[x]` **`matched` sin que nadie hubiera pedido nada.** `verdict.assess` no tenía regla sobre un
  contrato vacío, así que «compara estas dos y dime qué cambió» podía contestar la palabra que
  significa «el encargo está hecho». Ahora es `partial`.

- `[x]` **Una comparación bloqueaba la aplicación entera, y sólo se vio en el navegador.** Los
  manejadores son `async def` y llamaban al servicio en línea, así que una comparación de dos
  checkpoints de este repositorio —minutos de CPU recorriendo y parseando el árbol— retenía el bucle
  de eventos. El síntoma no era «los deltas van lentos»: era que **todas** las peticiones de la
  aplicación se ponían en cola detrás de una, la pantalla se quedaba en esqueletos para siempre y el
  log del servidor dejaba de escribir. Ninguna prueba podía verlo, porque una prueba nunca tiene una
  segunda petición. Arreglado con `asyncio.to_thread` en los dos endpoints que trabajan, como ya
  hacía `src/council/orchestrator.py::_verify`, y fijado con una prueba de cableado que lee el
  fuente.

- `[x]` **Fuera del plan: la cuarentena de sqlite era código muerto en Windows.** El patrón
  `_connect` dejaba la conexión abierta cuando fallaba la sonda `SELECT count(*) FROM
  sqlite_master`, y en Windows eso hace que el `os.replace` de la cuarentena falle con `WinError 32`:
  el fichero corrupto se queda donde está y **todas las aperturas siguientes fallan igual para
  siempre**. Estaba en `src/state_mirror/persistence.py`; corregido allí también. `src/council/
  persistence.py` ya lo hacía bien.

### Lo que se queda abierto, y por qué

- `[~]` **Un checkpoint se compara entero, y no hay forma de acotarlo.** El presupuesto tiene
  `max_elements`, que corta la **alineación**, pero la instantánea de cada extremo recorre el árbol
  y parsea cada fichero antes de llegar ahí. Comparar dos checkpoints de este repositorio son
  minutos de CPU aunque sólo cambiara un fichero. Falta lo obvio: pasar las rutas que `changed_since`
  ya sabe decir, y dejar que el llamante acote a un subárbol. Hoy la comparación útil de código es
  la de dos ficheros o un checkpoint pequeño; sobre el repositorio entero es correcta y cara.

- `[~]` **Dos ficheros con rutas distintas no se alinean, y eso desconcierta.** Comparar
  `_viejo/persistence.py` con `src/state_mirror/persistence.py` da «todo añadido» y ninguna
  correspondencia, porque la clave de un elemento lleva la ruta dentro. Es correcto —emparejar por
  nombre de fichero sería adivinar— pero es la primera cosa que alguien intenta al querer comparar
  «el mismo fichero antes y después», y el delta no le dice que probablemente quería un checkpoint.
  Falta una pista, no un emparejamiento.

- `[~]` **`sources.py` no resuelve `artifact`, `document`, `blob`, `state`, `workflow` ni `skill`.**
  Devuelven `readable=False` con un motivo concreto, que es un resultado correcto y no un hueco —
  pero significa que hoy una comparación de estos dominios se hace con `sources.stash()`, es decir
  con el contenido en la mano del llamante. Falta el puente al Artifact Store y al espejo. Los tres
  adaptadores afectados tienen una prueba que fija el agujero para que no se cierre por accidente.

- `[~]` **`correlation_id` no lo genera nadie.** El plan lo exige «en todos los eventos» y el campo
  existe, viaja y se propaga; lo que no hay es quien lo cree. Hoy es `""` en todos los payloads, que
  es peor que no tenerlo porque parece que funciona. Hace falta decidir dónde nace —¿el turno? ¿el
  run? ¿la petición?— y propagarlo desde ahí. Es una decisión de arquitectura, no una línea.

- `[~]` **Un ChangeSet no se persiste, así que un delta no puede apuntar a uno.** `src/changesets.py`
  no guarda nada por diseño. La integración construye el ChangeSet, lo somete a `prove` y guarda
  sólo `proof_ref`; el ChangeSet en sí se pierde. Para reconstruirlo hay que volver a mirar el
  checkpoint, y `has_checkpoint()` puede decir ya que no está.

- `[~]` **El adaptador de código no enumera un checkpoint entero.** `workspace_checkpoints` no expone
  «lista todos los blobs en este sha», así que el conjunto de rutas es el árbol de trabajo corregido
  por las filas `D` de `changed_since`. Un fichero que estaba en el checkpoint y que el paseo del
  índice poda (oculto, `node_modules`) no se enumera. Nombrado, no rodeado con una segunda
  integración de git.

- `[~]` **El escaneo de riesgo del adaptador de código es sólo Python y sólo de tokens.** Compara
  conjuntos de tokens sobre la revisión entera, así que mover una llamada a `subprocess` que ya
  existía no es una ampliación —correcto— pero tampoco detecta nada en JavaScript, Go o Rust, donde
  `repo_map` sólo tiene regex. Y `security.secrets_not_exposed` contesta **siempre** `unknown`: aquí
  no corre ningún escáner de credenciales, y contestar `preserved` porque ningún import se movió
  sería exactamente el fallo que la regla 1 del contrato existe para prevenir.

- `[~]` **De la lista de cambios bloqueantes del §14, cuatro no son comprobables hoy** porque el
  contrato real no tiene los campos: postcondición eliminada, rollback eliminado, timeout, y test
  removido (`NODE_TYPES` no tiene nodo de test, así que un workflow no puede decir que tenía uno).
  Los cuatro devuelven `unknown` con la limitación nombrada. Otros tres —permiso ampliado, secreto
  nuevo, proveedor local→externo— sólo se comprueban si el llamante los pone en `config`.

- `[~]` **`state`, `workflow` y `skill` no direccionan sus metadatos.** En workflow no se emiten
  elementos `meta:`, así que cambiar la descripción o la versión de una definición no produce ningún
  hallazgo; y los conflictos del espejo se acotan a la **entidad** y no al campo, porque
  `MaterializedState.conflicts` guarda ids y no objetos.

- `[~]` **Renombrar el id de un nodo de workflow sale como `missing` + `added`.** El id es la
  dirección de la que cuelgan todos los `edge:` y `param:`, y afirmar una correspondencia que la
  definición no hace sería inventarla. Declarado en las limitaciones del resultado.

- `[~]` **No se emite ningún `EvidenceRef`.** Los hallazgos llevan direcciones (ruta, línea, símbolo,
  celda), que es lo que hace falta para encontrarlos, pero no hay artefactos de evidencia con hash y
  retención. El §28 pide «página/rango de evidencia» para documentos, y eso necesita la capa de
  render por página que esta fase no tiene.

- `[~]` **La cobertura de comportamiento es siempre 0.0.** Ningún adaptador recibe resultados de
  tests. Está declarado en cada delta con su nota, y la nota dice además lo del §9 que conviene no
  olvidar: un test en verde no demuestra la ausencia de cambio colateral.

- `[~]` **`literal` es un almacén de proceso.** `sources.stash()` guarda en memoria para comparar
  valores que el llamante ya tiene, y se olvida al terminar. No es persistencia y el docstring lo
  dice, pero significa que un delta de un `literal` no se puede recomputar más tarde: su fingerprint
  sigue siendo válido y sus dos extremos ya no existen.

## Plan 7 — Greedy Completion Engine (06-09-2026)

### Ajustes: cómo quedó tu servidor

`agent_completion_engine` lo encendí para verificar en el navegador y **lo he vuelto a dejar en OFF**, que es su valor por defecto. `agent_completion_engine_shadow` sigue en ON (su valor por defecto): el motor mide y no cambia nada.

De sesiones anteriores **siguen encendidos** `agent_council`, `agent_state_mirror` y `agent_delta_engine`, los tres con default `False` en código. Si quieres el comportamiento de fábrica, apágalos en Settings → Agent & automation.

### Encender el motor rompe 5 tests del harness — y no es un bug del motor

Con `agent_completion_engine` en ON, estos cinco fallan:

- `test_agent_harness_functional.py::test_tests_still_failing_after_fix_round_is_reported_not_looped`
- `test_agent_harness_functional.py::test_pre_existing_test_failures_do_not_cost_a_fix_round`
- `test_agent_harness_functional.py::test_ungrounded_review_errors_do_not_cost_a_fix_round`
- `test_agent_harness_loop.py::test_truncated_output_is_auto_continued`
- `test_agent_harness_loop_partial_work.py::test_the_model_that_writes_both_files_is_verified`

Todos por lo mismo: cuentan rondas exactas (`assert calls["n"] == 2`) y el motor, cuando está encendido, añade una ronda. **Eso es exactamente lo que el motor existe para hacer.** El fallo no es del motor: es que esos tests fijan un mundo donde el motor está apagado sin decirlo. Lo correcto es que fijen el ajuste ellos mismos (un `monkeypatch` de `agent_completion_engine` a `False`), pero son tests congelados fuera del alcance del plan 7 y prefiero no tocarlos de refilón.

**Comprobado:** con el ajuste en su valor por defecto, la suite completa no tiene ni una regresión mía. 24 fallos en total, 24 preexistentes — verificado con el método de siempre: misma carpeta, mismo `data/`, solo cambia el commit, en serie. Baseline 15 fallos en el subconjunto sospechoso, con mis cambios 20, y los 5 de diferencia se van al poner el ajuste como estaba.

### Datos de prueba en la base

`data/completion_engine.db` tiene tres decisiones sembradas a mano (`run_browser_check_1/2/3`, owner `admin`) y un rechazo real en `completion_refusals` ("este modulo se reemplaza en junio, no merece un test nuevo"). Son de la verificación en navegador. Se pueden borrar sin consecuencias; si los dejas, la pantalla Completion arranca con contenido.

### Cosas para revisar

1. **El motor descubre 21 mejoras y ejecuta 0.** En los tres turnos sembrados el resultado siempre fue `unfinished` con 20-21 diferidas y ninguna ejecutada. Es correcto — el enganche del `agent_loop` está en modo sombra y `MAX_PER_BATCH = 3` — pero significa que la mitad *ejecutora* del motor no se ha visto funcionar todavía con trabajo real. **Hasta que un turno de verdad ejecute una mejora y la verifique, esa mitad está construida y no probada en el producto.**

2. **`decide_for_turn` nunca lanza, por diseño.** Se traga cualquier excepción para no romper el turno que estaba contabilizando. Es lo correcto, pero quiere decir que un fallo del motor se ve como "no decidió nada" y no como un error. Los eventos (`/api/completion/events`) son el único sitio donde eso se nota.

3. **Los techos del presupuesto son 0 hoy.** `max_tool_calls`, `token_budget` y `wall_seconds` se pasan a 0 porque el bucle no tiene esos límites todavía; solo las *rondas* se miden de verdad. Por eso ninguna decisión puede parar por `budget` aún. Cuando el bucle tenga límites reales, hay que pasarlos aquí o el motor seguirá diciendo `unfinished` donde debería decir `budget`.

4. **`declined` es nuevo en un vocabulario cerrado.** Añadir un motivo a `REJECTION_REASONS` obliga a tocar cuatro sitios (contracts, closeout, adaptador de Studio, pantalla). El test `test_every_rejection_reason_has_a_sentence` lo pilló al instante, que es lo que tenía que pasar. Vale la pena recordarlo antes de añadir el siguiente.

5. **Los rechazos guardados antes de hoy no tienen motivo.** El bug de `ranked.rejected()` (ver FAUSTUS §59) estuvo vivo desde que se escribió el servicio. Cualquier decisión guardada antes del arreglo tiene sus rechazos como `status: candidate` sin `rejection_reason`, y la pantalla los dibujará como "nobody recorded why". No hay migración: son tres filas de prueba. Si algún día importa, se regeneran volviendo a decidir.
