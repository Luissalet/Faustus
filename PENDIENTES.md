# Pendientes — fallos conocidos, cosas a revisar y deuda declarada

Este fichero existe porque hay una diferencia entre *"no lo hemos hecho"* y
*"lo hemos hecho y no funciona"*, y la segunda hay que poder encontrarla.
Cada entrada dice **qué pasa**, **cómo se sabe** y **qué costaría**. Lo que no
se pueda verificar se marca como no verificado en vez de darse por bueno.

Convención: `[!]` rompe algo hoy · `[?]` no verificado · `[~]` deuda aceptada
a propósito · `[+]` mejora pendiente.

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

- `[!]` **La trampa "ofrecido y luego rechazado" con `suggest_document`.**
  Un skill (`data/skills/ai-integration-setup`) hacía fallar 13 tests en el
  árbol de Luis y en ningún otro. Bisecado hasta ahí. El arreglo pertenece al
  punto de uso, no al preflight: preflight corre una vez al empezar el turno y
  un documento puede crearse *durante* el turno, así que podar la herramienta
  ahí quitaría una llamada legítima. Se revirtió a propósito un arreglo que
  pasaba 89 tests pero rompía dos de `test_external_context_tool_gate.py`.
  → El diagnóstico está escrito; el arreglo no.

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

- `[+]` **El `ChangeSet` existe y nadie lo produce solo.** `from_turn()` y `from_dispatch()` están
  escritos y probados, las rutas y las dos tools MCP funcionan, pero el `TurnLedger` termina un
  turno sin construir uno. Hasta que el resumen que ve el usuario lleve el veredicto al lado, la
  regla de la fase —«ninguna afirmación de arreglo sin evidencia»— la cumple quien se acuerde de
  preguntar, que es la forma más fácil de no cumplirla.

- `[~]` **`services/review_state.py` sigue siendo el dueño del aceptar/rechazar por fichero.** Un
  `ChangeSet` es el informe; lo que una persona decidió sobre él es otra pregunta con otra vida.
  Son complementarios hoy, pero si el ChangeSet se persiste alguna vez habrá que decidir cuál de
  los dos guarda el checkpoint, porque ahora mismo lo llevan los dos.

- `[?]` **El motor creativo no se ha ejecutado nunca contra un ComfyUI real.** No hay ComfyUI
  instalado en esta máquina y nada escucha en el 8188. El cliente implementa su API documentada y
  está probado contra un `ThreadingHTTPServer` que la imita con sus formas reales, más dos pruebas
  en vivo contra la 7001 con ese servidor en el 8188. Lo que **no** está comprobado: que un ComfyUI
  de verdad devuelva exactamente esas formas, que el grafo de las dos plantillas sea aceptado por su
  validador, y cuánto tarda un render de verdad. La primera vez que haya uno instalado, esto es lo
  primero que hay que volver a mirar.

- `[~]` **`config/media_workflows/` trae dos plantillas de imagen y ninguna de vídeo.** Las dos usan
  **solo nodos del core** de ComfyUI a propósito, así que funcionan en una instalación de serie con
  un checkpoint. Una plantilla de vídeo necesita custom nodes (AnimateDiff/SVD) que no se pueden
  probar sin tenerlos, y una plantilla escrita a ciegas es peor que ninguna.

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

- `[+]` `src/agent_runners.py::build_env` → cubre `external_worker.py:247`.
  **No es una línea segura tal cual**: `external_worker` resuelve el ejecutable
  con `shutil.which(argv[0], path=full_env["PATH"])`, así que si el usuario
  instaló su CLI en nuestro venv, limpiar el PATH lo deja "no instalado". Hace
  falta `which()` contra el PATH original y lanzar con el entorno limpio.
- `[+]` `routes/agent_runner_routes.py:76`, `routes/codex_routes.py:532` — se
  arreglan solos con el anterior.
