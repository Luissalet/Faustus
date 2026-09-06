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

- `[~]` **44 tests rojos en Windows, ninguno de hoy, y 17 los provoca el `data/` local.** Medido el
  04-09 (§40.7): la suite entera da **10.345 en verde**, 47 fallos, 6 errores, 74 saltados en
  15 min 34 s. Comparado como debe compararse —misma carpeta, mismo `data/`, misma lista, cambiando
  solo el commit— el commit anterior da **exactamente los mismos 44**: cero regresiones. Lo nuevo es
  saber que **17 desaparecen en una worktree limpia**, así que no son «de Windows» sino del estado
  local de esta máquina. Los grupos: `test_sys_usage_js` (11), `test_process_ownership` (6),
  `test_agent_loop_offer_execute_coherence` (5), `test_report_export` (4), y los 6 errores de
  parámetros gigantes (`test_claim_verify`, `test_output_rules`, `test_research_citations`). Ninguno
  se ha diagnosticado uno por uno todavía; lo que está probado es que son heredados.

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

- `[!]` **`bg_jobs.refresh()` mata por pid sin comprobar propiedad en la rama
  de timeout.** El orden del `elif` hace que `_pid_alive` nunca se alcance para
  un registro caducado, y un `_pid_alive` no ayudaría: un pid reciclado *está*
  vivo. El arreglo real es persistir la hora de creación del pid al lanzar y
  compararla al matar — que es lo que hace `process_ownership.note_started` en
  memoria, pero su `_create_time` es privado. Ruta del monitor, no alcanzable
  por el agente.

- `[!]` **`taskkill /T` en Windows y el pid del padre huérfano.** Windows nunca
  limpia el pid del padre de un huérfano, así que un proceso cuyo padre real
  murió hace tiempo y cuyo pid de padre registrado se recicló en nuestro
  `bash.exe` está *dentro de nuestro árbol* para taskkill. Desde el pid no hay
  nada comprobable. Solo lo arreglaría recorrer el árbol filtrando por hora de
  creación, lo que haría que `psutil` fuese obligatorio en la ruta de matar.

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
- `[!]` `tests/test_agent_gate.py::test_the_hook_script_is_not_left_behind` falla en Windows
  **desde antes de esta rama** (comprobado con el árbol guardado en stash): el directorio
  `faustus-gate-*` del hook queda en el temporal. No es de SEC-1, pero está sin dueño.

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

- `[!]` **Uno de los dos casos visibles del informe sigue abierto.**
  `routes/webhook/webhook_routes.py` valida la `base_url` en la ruta, pero la petición la emite
  `llm_call_async` en `src/llm_core.py`, que B-019 no lista. La ventana de rebinding sigue ahí
  y necesita un lote que sea dueño de `llm_core`.
- `[+]` **`src/url_security.py` es un tercer clasificador de direcciones privadas.** Parte de
  la fragmentación que B-019 describe; fuera de las rutas que nombra.

### SSH-1 (B-025)

- `[!]` **Rompe conexiones existentes y no hay interfaz para arreglarlo.** Con el `known_hosts`
  privado vacío, todo host remoto deja de conectar hasta emparejarlo. Los endpoints están
  (`/api/cookbook/ssh/fingerprint`, `/pair`, `/unpair`); Studio no.
- `[+]` **Quedan sitios con el flag antiguo:** `routes/hwfit_routes.py` y
  `services/hwfit/hardware.py` pasan `strict_host_key_checking=False`, y
  `core/platform_compat._ssh_exec_argv` y `routes/cookbook_helpers.run_ssh_command_async`
  siguen aceptando ese valor. No están entre las rutas de B-025.
- `[?]` **Nada se probó contra un SSH real.** `ssh-keyscan` está doblado en todos los tests, así
  que la forma real de su salida y el comportamiento de `UserKnownHostsFile` con una ruta de
  Windows están sin verificar contra un `ssh.exe` de verdad.

### MAIL-1 (B-023) y UPLOAD-1 (B-021)

- `[!]` **Agujero residual en `document_routes.py`.** `prepare-signed-reply` sigue dejando
  ficheros `<uuid>_<nombre>` planos en la raíz del staging, y hay un puente que los resuelve con
  la semántica antigua. Un token filtrado de ahí sigue siendo adjuntable por cualquier usuario
  autenticado. El arreglo es una llamada a `register_compose_upload`; el fichero no está entre
  los que B-023 nombra, y hay un test que fija el puente para que quitarlo sea deliberado.
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
- `[+]` **`core/platform_compat.kill_process_tree` se queda como estaba**, así que
  `routes/cookbook_routes.py` sigue llamando a `taskkill /T` sobre un pid pelado. Mismo fallo,
  fichero no listado.
- `[?]` **La rama POSIX está simulada.** Todos sus tests corren en Windows con `IS_WINDOWS`
  monkeypatcheado y `os.getpgid`/`os.killpg` inyectados. Nada se ha ejecutado en POSIX real.
- `[!]` **psutil pasa a ser necesario** para desmontar árboles, y no hay pin en `requirements`.
  Sin él, `bg_jobs` se niega a señalar —que es la regla del informe— donde antes mataba sin
  preguntar.

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

- `[!]` **La migración entera está pendiente, y ese es el plan.** Fases 2 a 5 de la nota:
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

- `[!]` **`tests/test_static_checks.py::test_the_loop_does_not_spend_a_round_on_a_warning_that_was_already_there`
  falla, y es preexistente.** Comprobado corriéndolo antes y después del cambio, en el mismo
  árbol: falla igual en los dos. Dos motivos que se suman y ninguno es del Context Engine: el
  arnés del test marca como cambiada una línea que nadie tocó, y el propio test **habla con el
  Ollama vivo de esta máquina**, así que su resultado depende de qué modelo esté cargado. Un
  test estático que necesita un modelo no es un test estático; hay que separarlo en dos o
  saltarlo sin endpoint.
- `[~]` **`tests/test_disk_ballast.py` (6 tests) y `test_atomic_io` fallan sólo con `pytest -n 6`.**
  En serie pasan los siete. Es contención: los dos escriben ficheros grandes y miden espacio o
  atomicidad, y seis workers a la vez sobre el mismo disco se pisan. **No es una regresión**, pero
  queda anotado para que la próxima persona que vea esos rojos en paralelo no vuelva a gastar
  media hora descartándolos. El arreglo real es marcarlos `xdist_group` o `serial`.

### Deuda que el propio subsistema declara

- `[!]` **`src/context_engine/wiring.py::observe_receipt` está implementado, probado y no
  cableado.** Registra qué referencias abrió el turno, cuántos resultados de herramienta añadió,
  el `outcome_ref` y el veredicto; sólo lo llaman los tests. Es la costura de la Fase 2, y
  mientras no exista `historical_utility` no tiene de dónde salir: el factor está en la fórmula
  del ranking y siempre vale lo mismo. Un recibo sobre un paquete que no se entregó no significa
  nada, así que va con la migración, no antes.
- `[!]` **El adaptador de sesiones no tiene proveedor de historial por defecto.**
  `src/context_engine/adapters/sessions.py` se niega a propósito a leer la base de datos de
  sesiones —`SessionManager.get_session` no acepta dueño (cualquier id, incluido uno que un
  modelo escriba en un mensaje, resuelve a los mensajes de esa sesión), muta la caché y
  `last_accessed`, y devuelve una transcripción distinta de la que el turno está usando— y espera
  que se la entregue quien ya tiene los mensajes, con `set_history_provider`. **Nadie la llama en
  producción.** Consecuencia: `available()` es `False` y **ningún paquete compilado hoy tiene
  sección `recent_messages`**. Es el estado honesto (visiblemente ausente antes que
  silenciosamente equivocado), pero es un agujero en la comparación sombra: el informe compara un
  paquete sin historial contra un prompt que sí lo lleva.
- `[?]` **`budgets.AppParityEstimator` no reproduce `src.model_context.estimate_tokens`
  exactamente, y su docstring dice que sí.** Usa el mismo 0,3 caracteres por token
  (`APP_PARITY_CHARS_PER_TOKEN = 1.0 / 0.3`) y el mismo coste de 4 tokens por mensaje, pero
  `_BaseEstimator.count()` hace `math.ceil(len(text) / chars_per_token)` mientras
  `estimate_tokens` hace `int(len(content) * 0.3)`: uno redondea hacia arriba y el otro trunca,
  **≤1 token de diferencia por cadena**. La diferencia sí está documentada, pero en el docstring
  de `_ledger_tokens` (`wiring.py`), que por eso reporta las dos mediciones para que nadie
  concluya que una de las dos tarjetas de la pantalla está rota; el docstring de la clase en
  `budgets.py` sigue diciendo *"Reproduces `src.model_context.estimate_tokens` exactly"*, que es
  falso. Hay que decidir: o se unifican (que `AppParityEstimator` trunque) o se corrige el
  docstring. Unificarlas es lo correcto — un carril que existe para comparar y no compara igual
  no sirve para lo que se creó.
- `[+]` **`budgets.py` nombra un test que no existe.** El comentario de `IMAGE_BLOCK_TOKENS` dice
  *"Kept in sync by `tests/test_context_engine_budgets.py`, which reads the constant from there"*,
  y ese fichero **no está en `tests/`**. Nada comprueba que los 1200 de
  `context_engine/budgets.py` sigan al valor de `src/model_context.py`; si uno cambia, el otro se
  queda callado. El docstring de `adapters/documents.py` cita ese mismo arreglo como precedente
  del suyo (que sí existe, en `test_context_engine_sources.py`). Una línea de test.
- `[~]` **Nada llama a `maintenance.run()` en bucle.** Las seis tareas sólo se disparan a mano
  (`POST /api/context/maintenance/run` o la tool MCP). `should_yield()` ya sabe cederle la máquina
  a un turno en vuelo, y `agent_context_ledger_days` promete una poda que hoy no ocurre sola: el
  ledger crece hasta que alguien pulse el botón. Mismo agujero que el `advance()` de los
  workflows.
- `[?]` **La sombra no se ha corrido contra un turno real con un modelo real.** `shadow_round` y
  `manifest.compare()` están probados con mensajes construidos en los tests. Nadie ha encendido
  `agent_context_engine_shadow` en una conversación de verdad y leído el informe.
- `[~]` **`agent_context_engine` no hace nada todavía.** `wiring.enabled()` lee el ajuste y su
  propio docstring dice que nadie lo llama: existe para que los consumidores de la Fase 2 tengan
  un solo sitio donde preguntar. Encenderlo hoy no cambia ningún prompt, lo que es correcto pero
  no es lo que un usuario deduce de una casilla en Ajustes.
- `[~]` **`incognito` siempre es `False` en la práctica.** `wiring.build_request()` lo lee de
  `harness_options`, que `routes/chat_routes._project_harness_options` rellena sólo con los campos
  de `services.projects.AGENT_OPTION_FIELDS`. La política existe, se aplica **antes** de la
  recuperación (que es lo que importa) y está probada; lo que falta es que la ruta la rellene.

### Lo que se vio en los carriles viejos (no es del Context Engine, pero está ahí)

- `[!]` **`src/memory_engine.py::pack_detail()` marca como accedidas todas las memorias que
  devuelve.** Su búsqueda interna sí llama a `search(..., touch_hits=False)`, pero al final la
  función hace `touch(ids, now)` sobre **todo** lo que empaquetó: reglas procedurales,
  antipatrones y aciertos. Recuperar no es usar. Un ítem que el presupuesto tira después no se
  usó, y decirle lo contrario al curador es entrenarlo con una mentira: `access_count` y
  `last_used` inflados desplazan la decadencia y cambian qué se promueve. El Context Engine lo
  esquiva por otra vía —`adapters/memory.py` reconstruye la selección sin consulta desde
  `scoped_items()`, que es una lectura pura, y registra el uso una sola vez desde
  `ContextReceipt`— pero **el carril viejo sigue tocando en cada turno**, y es el que está
  encendido.
- `[!]` **`services/docs/service.py::query` no propaga `owner` a `rag_vector.search`.** La firma
  es `query(self, query, top_k=5)` y llama a `self.rag.search(query, k=top_k)`, sin dueño.
  `rag_vector.search` **sí** acepta `owner` y con él aplica un `where={"owner": owner}` sobre la
  colección; sin él devuelve chunks de todos los dueños de la instalación. El Context Engine no
  usa este camino (su adaptador de documentos pasa por `rag_manager` con el dueño de
  `request.execution`), pero cualquiera que use `DocsService` tiene una fuga entre usuarios. El
  arreglo es un parámetro y una línea.
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

- `[!]` **`DELETE /api/projects/{id}/context/{item_id}` no emite `project_context_detached`.** La
  ruta va por `ProjectStore.remove_context_item` y no por `ProjectContextService.detach`, y lo
  hace por un motivo real y escrito en su docstring: un solo endpoint tiene que servir para el
  `item_id` legado de diez hex y para el `ctx_...` nuevo, y `remove_context_item` no necesita
  dueño efectivo más allá del que `_get_or_404` ya comprobó, así que un borrado que funciona no se
  convierte en un fallo cerrado en una instalación sin login. El precio es que **el evento no
  sale**: `src/context_engine/cache.py::on_event` invalida en `project_context_detached`, así que
  tras un desvinculado desde la pantalla la caché del Context Engine conserva entradas derivadas
  de un vínculo que ya no existe hasta que otra cosa la mueva. El arreglo barato es emitir el
  evento en la ruta después del borrado; el correcto es que `detach` acepte los dos formatos de id
  y la ruta pase por el servicio.
- `[+]` **`_StampedPatchStore` (`routes/project_routes.py`) es ya un no-op y se puede borrar.**
  Es un proxy que quita `updated_at` del parche antes de dárselo al store, escrito cuando
  `LINK_PATCHABLE_FIELDS` no aceptaba ese campo y `patch_link` lo rechazaba. Su propio docstring
  dice cuál era el arreglo permanente —«one word added to `LINK_PATCHABLE_FIELDS`»— y esa palabra
  **ya está añadida**, con su comentario al lado. El proxy sigue en pie, envolviendo cada llamada
  de `_context_service()`, sin cambiar nada. Borrarlo es quitar una clase y una línea; dejarlo es
  dejar un parche que finge arreglar algo que ya no está roto, que es como se acumulan las capas
  que nadie se atreve a tocar.
- `[!]` **`ProjectContextService` no marca el vínculo cuando el resolver dice `missing`.**
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
  siendo membresía pura y la salud se pide aparte. Hoy no está decidido, sólo está ausente.
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
- `[?]` **Los seis fallos de tests que se ven al correr esto son preexistentes.** Cinco en
  `tests/test_agent_loop_offer_execute_coherence.py` y uno en
  `tests/test_external_context_tool_gate.py`. Comprobado revirtiendo el cambio y corriendo con el
  mismo `data/` contra master: fallan igual. Los cinco del primero además **ya estaban anotados en
  este fichero desde el 04-09** («Lo primero que hay que mirar», §40.7), en el grupo de los 44
  rojos heredados de Windows. Ninguno se ha diagnosticado uno por uno todavía; lo que está probado
  es que no son de aquí.
- `[+]` **El docstring de `src/project_context/service.py` dice que sus eventos no están en
  `EVENT_NAMES`, y ya lo están.** La sección «Events» explica que `_emit` intenta el `emit()` real
  y guarda el sobre en un buffer en memoria (`unrouted_events()`) porque los nombres
  `project_context_*` «are not in `src/contracts/event.py::EVENT_NAMES`, and that file is not this
  change's to edit». Los ocho nombres **sí** están ahí ahora, así que el camino de respaldo está
  muerto y el docstring describe un mundo anterior. El buffer no molesta —cuesta una lista vacía—
  pero `CONTEXT_EVENT_NAMES` y su comentario («Listed here so whoever adds them has the set»)
  también sobran, y quien lea el módulo se creerá que sus eventos no llegan a ninguna parte.
