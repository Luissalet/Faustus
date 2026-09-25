# Pendientes de cierre

Actualizado: 26-09-2026. REGLA: nunca nombres de empresas/personas del buzón de Luis en commits, docs, tests ni comentarios — ejemplos siempre ficticios. Sólo trabajo vigente; quitar cada entrada al cerrarla.


## 25-09 tarde — visión, contexto, razonamiento, enjambre, podcast (FAUSTUS.md §197, OBJ-45)

- **7000 sin reiniciar**: el 7006 lleva todo desplegado y probado; el 7000 (instancia principal de Luis) lo coge al reiniciarse con el master nuevo y rehacer el `vite build` de la carpeta principal. Es una decisión de Luis cuándo reiniciarlo.
- **`context_*` automáticos**: en vivo solo se probó pidiéndolo («usa context_note»). Falta una ejecución larga real (Silhouettes o similar) para ver si el 27B las usa solo cuando se le ofrecen al 45 % o en la ronda 12, y si el aviso al umbral blando ayuda o estorba.
- **`swarm_map` modo `agent`** sin probar en vivo; el modo `llm` sí (6 ciudades). Con otros chats ocupando slots del 8081, dos elementos agotaron sus 180 s esperando cola antes del arreglo que reserva los slots ocupados; volver a medirlo con el 8081 compartido.
- **Visión**: tras el arreglo de `vision_num_ctx` la descripción del adjunto va a 6,5 GB, pero con el 27B q8 ocupando las cuatro GPU generó a ~7 tok/s (≈3 min para una descripción de 1.536 tokens). Valorar bajar `vision_max_tokens` para adjuntos o pedir descripciones más cortas.

## 25-09 mediodía — uso diario en el 7006 (FAUSTUS.md §184, puntos 17–35)

- **Desplegado en el 7006**. Visto en vivo: el historial ya no guarda la tarjeta dos veces; el seguimiento de datos acertó; «recuerda», calendario, correo y `web_fetch` de enlaces de la búsqueda sin tarjeta; la reescritura por día de la semana erróneo (lunes → viernes). Falta ver en vivo la comprobación de huecos ocupados del calendario (32) y la búsqueda en memoria por una pregunta sobre el usuario (31). Falta ver en vivo: «recuerda que…» sin tarjeta con la paráfrasis tolerante; la reescritura por día de la semana erróneo o razonamiento en voz alta (la primera prueba sí quitó el razonamiento en voz alta, pero el día seguía mal porque la fecha iba entre paréntesis — ya cubierto); que el borrador rechazado desaparezca también en Studio (evento `response_replace`).
- **Revisión de seguridad de las licencias nuevas (subagente, 25-09)**: arreglado que una captura repetida como «tool result: <herramienta>» contara como contexto propio (ahora lista blanca de etiquetas). Quedan dos observaciones aceptadas conscientemente: `seen_urls` recoge enlaces de cualquier texto que el turno vio, así que un enlace plantado en una página puede abrirse sin tarjeta si el usuario pidió buscar (la salida a red privada la bloquea `outbound_fetch`, y el enlace no lleva datos del modelo); y `_runs_a_confined_script` ejecuta cualquier script confinado de la carpeta con un «pruébalo», no sólo el que el usuario nombró. Revisar si en uso real conviene limitar `seen_urls` a resultados de búsqueda y páginas abiertas.
- **Ruta ofrecida vs inventada**: medir en turnos reales que una oferta («¿quieres que guarde X?») ya no provoca el rechazo, y que un «He guardado X» falso sí.
- **La memoria guardada, el índice de skills y las descripciones MCP arman la puerta en cada turno** por diseño. Si siguen saliendo tarjetas en peticiones explícitas, añadir la regla de «lo pidió el usuario» para esa herramienta en `src/user_request_gate.py` en vez de rebajar la puerta.

## 24-09 noche — ingeniería autónoma (FAUSTUS.md §189, OBJ-43) — verificado por tests, no en vivo

- **`github_issue` / `git_open_pr` contra GitHub de verdad**: sólo probados con transporte falso. Probar con el token de `reach` (o `gh`) sobre un repo de pruebas: leer una issue, subir rama, abrir PR, comprobar «Closes #N» y que la política git del repo los frena cuando toca.
- **`bug_hunt` con el 27B**: la generación de tests y el triaje se probaron con el modelo simulado y la batería determinista; medir calidad real sobre `src/git_radar.py` o similar y ajustar el prompt si inventa expectativas.
- **`ci_failures`**: sin ejecución de GitHub Actions accesible en la nube; probar con el repo Faustus (tiene workflows) y un run fallido.
- **`fix_memory`**: comprobar en un chat real que un turno con ficheros cambiados deja línea en `DATA_DIR/fix_memory/<owner>/` y que el turno siguiente parecido enseña el bloque «Past fixes» en el ledger de contexto.
- **Carriles en `enforce`** con `delegate_agents` real y un `AGENT.md` de biblioteca; el diálogo de Studio pintado y usado con clics.
- **Turno de noche** con 2–3 tareas reales de `dispatch` y presupuesto corto; tarjeta de Inicio vía la acción `night_shift_report`.
- **El 7000** necesita reinicio para cargar todo esto.

## 24-09 — examen Eldoria contra el 27B local (FAUSTUS.md §184)

- **El 27B sin visión propia aún no resuelve la prueba Saber.** Ejecuciones 9, 12 y 13: 37–60 rondas, sin respuesta. Cuello de botella: visión en CPU (`qwen3-vl-30b-cpu`, 30–120 s por pregunta) y que el modelo retranscribe la página (ya hay transcripción humana) en vez de buscar lo que falta. Probar la escalera de racha (§184, tercera tanda) en la ejecución 14 y, si sigue sin cerrar, medir el mismo examen con un modelo principal que vea.
- **Ejecución 15**: primera respuesta completa (La Habana, ≈ 11,5/100). Hasta la 15 ninguna ejecución tuvo web (el cliente de pruebas no enviaba `allow_web_search`); la 16 va con web. Falta: identificar los círculos (objeto → fuerte), el numeral cisterciense y la unidad (185,2 m/cable), que dependen de la visión.
- **Cliente de pruebas `talk3`**: causa encontrada (evento CTRL_CLOSE de la ventana de consola de la tarea programada); las tareas van ahora sin ventana. Vigilar que no vuelva.
- **`unconsulted_sources`**: medir falsos positivos en turnos reales de conversación. (Una fuente leída en un turno anterior de la conversación ya cuenta: `TurnLedger.note_prior_message`.)
- **Prueba 02 (Ingenio)** sin ejecutar todavía; el bonus sólo después de cerrar las dos.
- **Recuperación del motor a mitad de turno**: verificada en vivo en el 7006 el 25-09 (se mató el llama-server gestionado con la respuesta a medias; el bucle emitió `engine_lost_recovered`, Faustus lo arrancó de nuevo en 40 s, rehízo la ronda y la respuesta salió entera). Falta adoptar el 8081 como motor también en el 7000 (Ajustes → Modelos locales → Añadir motor → Rellenar).
- **Visión**: el `qwen3-vl-30b-cpu` sigue siendo el cuello de botella (cuenta mal flechas y personas). Probados: `qwen3-vl:8b-instruct` en GPU (peor y no más rápido con el 27B cargado) y el cliente Claude de suscripción como Visión (funciona, 4 min por llamada). Pendiente probar el cliente con `--model` rápido (sonnet/haiku) y sin razonamiento largo.
- **La barra de «modelos / otros»** de la tarjeta de VRAM sigue contando el motor llama.cpp como «otros»; las tarjetas por GPU ya lo nombran.
- **El 7000** necesita reinicio y `vision_model` configurado (Ajustes → Visión) para tener lo del examen.

## 24-09 — `inspect_image` sin probar en vivo (FAUSTUS.md §181)

- **Probar `inspect_image` contra un modelo de Visión real y un modelo principal con visión real.** Todos los tests nuevos (`tests/test_inspect_image_core.py`, `tests/test_inspect_image_tool.py`) usan `llm_call`/`analyze_image_with_vl_prompt` simulados; falta un turno real (foto con un círculo dibujado a mano, `action: "ask"` con una pregunta concreta) contra un endpoint de Visión de verdad.
- **`pypdfium2`/`pdf2image` no están instalados en este entorno**: el renderizado de página de PDF (`load_from_path` con `.pdf`) sólo se comprobó por su camino de degradación (mensaje claro, sin traceback). Instalar uno de los dos en un entorno con red y confirmar el camino "feliz".
- **`opencv` tampoco está instalado**: `detect_shapes` sólo se ha ejercitado por su alternativa Pillow/`numpy`. Instalar `opencv-python` y comparar la detección de círculos/rectángulos/líneas de `cv2.HoughCircles`/contornos contra la alternativa en la misma imagen sintética.
- Considerar exponer `inspect_image` también por MCP si algún cliente externo (no el propio Studio) lo pide.

## 23-09 noche — grafo de código+, prior art y el bucle con el 27B (FAUSTUS.md §179, §183)

- **Servidor de modelo estropeado.** El `llama-server` 8081 se estropeó (devolvía `////` y sopa de tokens) y el turno murió a los 14 min con un 500 del analizador. Los bucles dentro de argumentos ya se cortan (§179); falta detectar un servidor que devuelve basura desde el primer token para decirlo y no esperar, y medir si hace falta un corte por silencio a mitad de stream.
- **Hoards de Node (Ledger, Links, People)**: la primera línea de 9–14 descripciones MCP pasa de 110 caracteres; el índice ya lee la descripción entera, pero el contrato de la familia pide ≤110.
- **Reiniciar el 7000** para que cargue todo esto (y los plugins adoptados en el 7006 se adoptan igual allí desde Conectores).

## 23-09 tarde — decisiones tipadas y Nightingale's Hoard (FAUSTUS.md §177–§178)

- Repetir `scripts/eval_typed_decision.py` contra el modelo grande de Ollama cuando esté libre (el ayudante de 3B ya está medido: actualidad 73 % → 93 %, tipos de entidad 12 % → 88 %, p50 ≈ 390 ms).
- Tras reiniciar el 7000: adoptar Nightingale's Hoard desde Conectores y probarlo con un turno real del agente («limpia este CSV y hazme un gráfico por ciudad»).
- Nightingale: probar «pregúntale a tus datos» con un modelo compartido resuelto; decidir si se publica en GitHub como el resto de la familia.
## 23-09 — decisiones tipadas (FAUSTUS.md §177, OBJ-37) — sin medir contra un modelo real

- **Correr la evaluación en la máquina del dueño** y pegar las tablas en
  `docs/evals/typed-decisions.md`: `python scripts/eval_typed_decision.py
  --url http://127.0.0.1:8082/v1 --model <ayudante> --markdown` y lo mismo con
  `--url http://127.0.0.1:11434 --model <modelo residente>`. Sólo se ha
  corrido con `--fake`.
- **Ajustar umbrales con esos números** (`typed_decision_min_confidence` 0.7,
  `typed_decision_min_mass` 0.5) mirando los cubos de calibración.
- **Latencia real añadida a un turno**: la regla de actualidad duda en toda
  pregunta sin palabra clave (45 de los 60 casos de la evaluación), así que
  con el ayudante residente esos turnos pagan una llamada de hasta
  `typed_decision_timeout_ms` (1,5 s). Confirmar el p50 real en vivo y, si
  molesta, bajar el presupuesto o estrechar «pregunta».
- **Verificar en vivo el log `[freshness] typed decision:`** en un turno de
  chat real (navegador), con el ayudante cargado y descargado (descargado
  debe salir `unavailable model_not_resident` sin cargar nada).
- **El recordatorio de actualidad dentro del bucle del agente** sigue leyendo
  sólo la regla (`agent_loop._classify_turn_intent`); si la decisión dijo
  «no», la ruta no activa la web y el recordatorio ya no se añade sin
  herramientas web, pero el dominio «web» sigue entrando en la intención.
- **Sugerencias de conflicto de memoria** (`status=suggested`) sólo por API;
  falta mostrarlas en la pantalla de Memoria con su probabilidad.
- **El tipado de entidades** sólo mira la primera frase que nombra la
  entidad; una entidad mencionada de formas distintas podría merecer varias.

## 23-09 — documentación del segundo cerebro (FAUSTUS.md §176, OBJ-36) — pendiente de verificar en la máquina en vivo

El código de la ola completa ya está en la rama (§176); este bloque recoge lo
que la propia ola dejó abierto, para que no se pierda al fusionar:

- **Los resúmenes de entidad por modelo sólo corren con el modelo de
  utilidad residente e inactivo**: en la máquina del dueño el endpoint de
  utilidad es un ayudante `llama-server` en loopback — falta confirmar que
  un resumen aparece de verdad tras un rato de inactividad real, no sólo en
  el pase forzado a mano.
- **Las notas generadas** (`Projects/`, `Objectives/<Proyecto>/`,
  `Concepts/<Proyecto>/`) **cuya fuente se borró no se retiran solas** —
  siguen en la bóveda hasta que algo las toque explícitamente.
- **Un fichero ilegible** (marcador dañado, BOM roto) **se reporta en cada
  sincronización** hasta que alguien lo arregla a mano; no hay forma de
  silenciarlo ni de que el sistema lo intente reparar solo.
- **`agent_context_engine` sigue `False` por defecto en código**; sólo está
  encendido en la instancia privada donde se hizo la verificación en vivo de
  esta ola. Recomendado encenderlo en la instancia principal después de que
  el dueño pruebe la pantalla `/brain` unos días.
- **Rendimiento del modelo grande en la máquina del dueño**: los turnos de
  agente van lentos cuando otra instancia tiene cargado el modelo grande
  (~1,5 tok/s con cuantización q8 y contención) — es una condición del
  entorno de esa máquina, no de esta funcionalidad.
- **Suite completa en Windows (23-09 tarde)**: 22.268 verdes, 27 fallos con
  `-n 6`. Repetidos en serie contra la base (`master` 61081475, mismo PC,
  datos temporales): 13 pasan (dependientes de orden/paralelismo:
  `test_vram_admission` ×5, `test_workflow_waits`, `test_ui_smoke_audit` ×4,
  `test_h1`, `test_research_shared_runtime`, `test_memory_extractor_vector_degraded`);
  12 fallan igual en la base (`tests/eval/test_baseline_match.py` ×6,
  `test_a20_external_sdk_consumer`, `test_version_build` ×3,
  `test_windows_native_execution`, y la guarda de colores por
  `screens/skills/Instincts.tsx`, ya arreglada en esta rama); los 2 propios
  (guardas de Studio de la pantalla Cerebro y la ruta cruzada de unidad del
  benchmark) quedaron arreglados en 12f0efeb.

## 22-09 noche (5) - coaching con tareas reales: facturación (L4) y ventas (L5)

L4: proyecto de facturación en tres módulos con un `KeyError` pegado como traceback (facturas antiguas con `"iva": "4%"`) y una factura de las 00:30 de Madrid que caía en el mes anterior (agrupaba por mes UTC). Faustus arregló las dos cosas bien las tres veces, con tests de comportamiento, y el informe final cuadra al céntimo con el calculado a mano. Lo que fallaba era todo lo de alrededor:

L5: export de ventas con trampas (filas reexportadas, comas decimales, fechas en dos formatos, «puerto » mal escrito, un precio 1299 que era 12,99). Sin limpiar gana Centro; limpio gana Puerto (3735,27 € frente a 3556,92 €; total 9295,30 €).

- L5 en la corrida 8: las cifras cuadran al céntimo con las mías (Puerto 3.735,27 €, Centro 3.556,92 €, Universidad 2.003,11 €; total 9.295,30 €; la taza es el producto que más deja, 3.117,60 €), y el informe documenta los cinco problemas de datos que puse (24 filas duplicadas, el 1299, la devolución, formatos, «puerto »).
- L5 corrida 10 (01:09-01:31): **de principio a fin sin tarjetas**, respuesta y ficheros correctos; hasta calculó bien el contrafactual (sin corregir el 1299 Centro «ganaba» con 6.128,94 €). 22 minutos: ver los tres puntos siguientes.
- ABIERTO (rendimiento, para ti): el 27B iba a 3-5 tok/s en L5 frente a 11 tok/s al principio de la noche. El caché de prompt del servidor funciona (sonda directa: 4.301 de 4.319 tokens reutilizados), y Faustus lo aprovecha desde la ronda 3; se pierde en la ronda cortada a 240 s y la siguiente. Nada más usaba la GPU, pero la 4070 Ti (la de pantalla) estaba en 11,7 de 12,3 GB con aplicaciones de escritorio: con presión de memoria Windows puede desbordar a RAM y hundir la velocidad. Probar `--tensor-split` con menos carga en esa GPU.
- ABIERTO: el revisor automático es el mismo 27B en el mismo servidor; a esa velocidad agotó el tiempo (504) tras 3 minutos sin aportar nada. Revisarse a sí mismo vale poco; decidir si saltarlo cuando revisor = escritor.
- ABIERTO (datos dev): el extractor de skills en segundo plano pide `qwen3.5:9b` a Ollama y no está instalado (404). Debería caer a un modelo disponible o no correr.
- ABIERTO (menor): cuando un turno se para en una tarjeta, la narración previa puede quedar en inglés; el aviso de idioma se aplica en la ronda siguiente, que no llega a correr.
- ABIERTO: con el 27B q8 la ronda 2 de las tareas de código se corta a los 240 s de pensamiento en 4 de 4 corridas (L4 ×3, L5). Con el arrastre del razonamiento ya no se pierde el trabajo, pero son 4 minutos por tarea. Mirar si el presupuesto debería depender de lo que ya está escrito en el razonamiento (código completo = actuar ya) o avisar al modelo antes del corte.
- ABIERTO: la suite entera de esta noche (antes de estos arreglos): 11 fallos + 17 errores e2e/playwright sobre 20 983 correctas. `test_windows_native_execution` y `test_workflow_waits` pasan aislados (65/65): dependen del orden. Sin mirar aún: `test_l67_ops07_and_eval03_routes` (2), `test_l86_source_control_panel_js`, `test_h1`, `test_memory_extractor_vector_degraded`, `test_ui_smoke_audit` (4).

## 23-09 madrugada — hooks, instintos, bibliotecas, selector, arriendo de modelos (FAUSTUS.md §169-§173)

- ABIERTO (necesita modelo cargado): la extracción de instintos en segundo plano no se ha visto producir instintos reales — el turno de prueba acabó cuando Ollama y llama-server ya estaban apagados (`POST /api/instincts/extract` devuelve `{stored: [], count: 0}` sin modelo, sin error). Probar con `qwen3.5:4b` de utilidad residente y un turno con corrección del usuario; mirar `[instincts]` en el log del 7001.
- ABIERTO (necesita dos instancias con modelo): adopción del default residente de una vecina y el veto a desalojar un modelo activo en otra instancia — cubiertos por tests, no vistos en vivo (Ollama apagado durante la prueba).
- ABIERTO (cosmético): en la tarjeta «Lifecycle hooks» las filas con resumen de match largo saltan el toggle/Edit/Delete a una segunda línea y las etiquetas «Installed» de los presets se colocan raro al envolver.
- ABIERTO (dev-data): el 7001 se levanta sin `utility_model` en `/api/settings` (las claves salen `null`); para probar instintos hay que fijar `utility_endpoint_id/utility_model` en ese data dir.
- ABIERTO: `skill_library.install` para un owner distinto crea `<slug>-2` si el mismo slug ya está instalado sin owner (dedupe por nombre global); en el 7000 con auth no pasa.
- Trampa de despliegue anotada: `device_commit_files` con un `stagedPath` reutilizado vuelve a escribir el contenido viejo — bundles y scripts SIEMPRE con nombre nuevo (w121 fue el tercer intento de w120).

## 22-09 noche (4) — hablando con Faustus (FAUSTUS.md §166)

- Queda un hueco teórico sin reproducir: si la respuesta de rescate es sólo «budget exhausted», `agent_loop` la borra (motor local) y `_empty_response_fallback` no pone nada porque hubo herramientas.
- ABIERTO: la puerta sigue armándose en casi todos los turnos. Hoy pasan sin tarjeta `plugin_app` y la lectura de memoria cuando se piden con todas las letras; el resto (p. ej. «2+2?» → `python`) sigue preguntando. Cada herramienta nueva que quiera esta regla necesita su comparador en `src/user_request_gate.py`.
- ABIERTO (datos dev): el perfil «Jobhunter (test data, 5179)» arranca en 5179 y la conexión apunta a 5178. Útil para probar el mensaje de discrepancia; no tocar sin decidir.
- ABIERTO: siguen sin mirar a fondo `test_h1`, `test_memory_extractor_vector_degraded`, `test_workflow_waits`, `test_ui_smoke_audit` — juntos y aislados pasan; ver el resultado de la suite de esta noche.

## 22-09 madrugada (4) — una pregunta de diseño para ti

**ABIERTO, decisión tuya: `tool_approval_mode = auto` anula `desktop_control_mode = ask_each`.**

En `src/tool_capabilities.py::decision_for`, la puerta por llamada de las acciones de escritorio (ratón y teclado) solo se consulta `if mode == "ask"`. Con el modo global en `auto` —que es como está esta instalación— una acción de escritorio se ejecuta sin confirmar, aunque `desktop_control_mode` diga `ask_each`.

El comentario justo encima dice lo contrario de lo que hace el código: *"Per-call approvals come first: neither a task/chat-scope grant nor a clean run lets a desktop input action run unconfirmed"*. Enumera lo que la puerta vence, y el modo global no está en esa lista porque se decide antes.

Las dos lecturas son defendibles:
- `auto` significa "no me preguntes por herramientas", y punto.
- `desktop_control_mode = ask_each` es una elección más específica y más reciente sobre un riesgo concreto, y debería ganar.

No lo he tocado: cambia el comportamiento de seguridad de una instalación que corre en `auto` —la tuya— y esa es tu decisión, no mía. Lo encontré porque cinco tests de code mode fallaban leyendo tus ajustes reales.

## 22-09 noche (2) — bajando por los 74

Segunda pasada completa de la suite, ya con los arreglos del día: **74 fallan, 20.804 pasan, 46:12** (venía de 93 / 20.766 / 53:42). Sigo mirándolos uno a uno.

**Decisión tuya, no mía (no lo he tocado):** `l86-source-control-panel.check.mjs` dice que el modo compacto del panel de control de versiones no debe renderizar `NewRepositoryDialog`, y lo renderiza. El check no está roto: el producto cambió. El diálogo está cerrado (`open={newRepoOpen}`), así que no se ve hasta que alguien lo abre — puede ser una capacidad añadida a propósito o un descuido. Si es lo primero, lo que hay que actualizar es CONTRATO_GIT_4.md punto 3, no el check.

## 22-09 noche — la suite termina por primera vez, y lo que se vio al final

La suite completa acabó por primera vez en el día: **20.766 pasan, 93 fallan, 53 min**. Con ese número delante, los fallos dejan de ser ruido y se pueden mirar uno a uno. Estos son los que se han cerrado.

- NOTA sobre los 93: una parte son de esta clase — tests que afirman algo sobre el entorno (hardware presente, servicios levantados, binarios instalados) y no sobre el código. Los que he comprobado uno a uno contra la base son preexistentes; los cuatro que eran míos están arriba.
- ABIERTO: `test_launch_profiles::test_already_running_via_readiness_is_not_relaunched` **lanza Electron de verdad** durante la suite. Un test que arranca la aplicación deja procesos sueltos y depende del escritorio de quien lo corra.
- ABIERTO: siguen en rojo, sin mirar a fondo: `test_ui_smoke_audit` (4), `test_studio_close_dialog_js` (5), `test_studio_clipboard_js`, `test_l86_source_control_panel_js`, `test_studio_guards`, `test_w3a_composer_js`, `test_sse_catalog`, `test_typed_choice`, `test_tool_policy`, `test_process_center`, `test_tls_overrides_scope`, `test_workflow_waits`, `test_model_warmup`, `test_ollama_structured_output` (2), `test_two_tier_search` (1 de 2).
- NOTA: `test_health::test_the_usage_endpoint_carries_the_health_block` falla porque el bloque de salud dice `warn`, y ahora mismo lo dice con razón (ChromaDB en 8100 y Ollama en 11434 no responden). No es un fallo de código; es el test afirmando que la instalación está sana.

## 22-09 tarde — sesión de uso real (FAUSTUS.md §161)

- ABIERTO (preexistente, no mío): `test_two_tier_search.py::test_the_tool_index_lexical_floor_ranks_a_known_tool_first_with_no_embedder`. Sin embedder, el suelo léxico no pone `bash` entre los ocho primeros para «run a shell command» ni `web_search` para «search the web for the latest news». Importa más de lo que parece cuando Ollama está cerrado y el embedder cae al de reserva; el suelo de herramientas de workspace tapa el caso en la práctica, que es por lo que nadie lo ha notado.
- ABIERTO: la suite mata trabajadores de xdist en el tramo final («node down: Not properly terminated», varios seguidos). Es lo que producía el atasco del 97%: muere un trabajador, xdist lo reemplaza, y con suficientes caídas la ejecución se queda parada. Falta identificar qué test mata el proceso (una salida dura: segfault, `os._exit`, `sys.exit`). El timeout de 120 s acota el síntoma pero no la causa.
- NOTA: las dos entradas de memoria que dieron pie a `volatile_facts.py` siguen en el almacén real. La guarda solo impide archivar nuevas; las que ya están las quita la pasada de auditoría.

- ABIERTO: el turno conversacional sigue mostrando la tarjeta «No action taken · no_workspace_action» en la ronda 1 aunque ya no dispare una segunda ronda. Es ruido en la interfaz para un saludo.
- NOTA: la instancia de desarrollo (`Start-Faustus-Dev.ps1`, puerto 7001, `D:\LocalAI\faustus-dev-data`, sin login) es la forma limpia de probar la interfaz sin tocar la instancia real ni credenciales. Muy útil para estas sesiones.
- TRAMPA DEL ENTORNO: `Set-Content -Encoding UTF8` en PowerShell 5.1 reescribe el fichero con BOM y destroza los acentos de un fichero de test en español. Para editar ficheros con acentos, usar Python o la herramienta de escritura del puente, nunca `Set-Content`.
- TRAMPA DEL ENTORNO: un proceso lanzado con `Start-Process` desde el puente muere cuando el comando padre agota su tiempo. Para algo que dure más de ~50 s hay que usar el ejecutor persistente (`start_process` + `read_process_output`), no `Start-Process` + espera.
- TRAMPA DEL ENTORNO: el clic y el tecleo sintéticos del control de navegador **no llegan a una pestaña en segundo plano** (`document.hidden === true`): no se dispara ni un `keydown`, la acción se reporta como ejecutada y la pantalla simplemente no reacciona — idéntico a un fallo de la aplicación. Encadenar clic+tecleo+Intro en un solo lote lo agrava, porque el foco aún no se ha asentado. Antes de dar por bueno un «no hace nada», comprobar `document.hidden` y `document.activeElement`. Receta que sí funciona con la pestaña oculta: `ta.focus()`, escribir con el setter nativo de `HTMLTextAreaElement.prototype.value`, `dispatchEvent(new Event("input", {bubbles:true}))` y después `dispatchEvent(new KeyboardEvent("keydown", {key:"Enter", bubbles:true, cancelable:true}))`.

## 22-09 — decodificación bajo esquema en llama-server (FAUSTUS.md §160b)

- PREEXISTENTES (comprobados idénticos contra la base con `git stash` sobre `src/llm_core.py`): `tests/test_ollama_structured_output.py::test_local_v1_without_a_native_api_keeps_v1_and_sends_no_schema` y `::test_setting_off_restores_the_previous_request_byte_for_byte`. Los dos esperan que un Ollama `/v1` local cuyo `/api/show` no responde se quede en `/v1`, y lo que ocurre es que se reencamina a `/api/chat`. No es de este cambio; hay que mirar `_route_for_response_schema` y su prueba de identidad.
- ABIERTO: los consumidores de OBJ-27. Hoy solo `auto_review`, `doubt_review` y `research_review` piden esquema. Los puntos de decisión que corren en cada turno — clasificación de intención, enrutado de modelo, selección de herramienta — siguen pidiendo texto libre y parseándolo con suerte. Falta inventariarlos (un sub-agente no pudo: `D:\LocalAI\faustus` no es carpeta conectada de la sesión, hay que hacerlo desde el shell de la máquina).
- ABIERTO (OBJ-30): el canvas de diseño no tiene todavía herramienta de agente — hay que llamar a `design_canvas_pass.draft` desde código. Falta registrarla en las cinco piezas de la paridad y cerrar el ciclo: volver al canvas al acabar la tarea y comprobar que lo hecho cumple lo diseñado.
- NOTA: la skill `design-before-code` vive en `data/skills/`, que está en .gitignore. Existe solo en esta máquina; si se quiere versionar, hay que decidir dónde.
- ABIERTO: un modelo que piensa y una gramática no conviven. Si algún día se quiere razonamiento Y salida restringida en la misma pasada, hay que hacerlo en dos llamadas (pensar libre, luego rellenar), no en una.
- NOTA: el esquema nunca viaja junto a `tools` en ninguno de los dos backends. Si alguna vez se quiere decodificación restringida dentro del bucle de agente, eso es un diseño nuevo, no un ajuste.

## 21-09 — motor parado a la vista y la tabla de traducciones como única fuente


## 20-09 — sesión real de lote (fichas por carpeta) con qwen3.8 27B q4, aprendido haciendo de coach

- ABIERTO: el turno de continuación tras aprobar la tarjeta «Allow this task to continue?» (17 rondas, 9 comandos) no quedó en el historial: `chat_messages` solo tiene la parte previa a la tarjeta. Reproducir: gate de contexto externo → aprobar → `/api/history/<sid>` tras terminar.
- Lo que confunde es que la tarjeta del guardián de comandos destructivos (`Exact approval required before tool start`, p. ej. un `rm -f` de un `__pycache__`) usa el MISMO título «Allow this task to continue?» que la del contexto externo: parece que la concesión por carpeta no funcionó. Debería decir qué comando y por qué (borra ficheros).
- ABIERTO: un selector nativo de carpeta (`POST /api/workspace/pick`) que nadie atendió se resolvió minutos después con otra carpeta (una subcarpeta recién creada por Luis en otro programa) y cambió en silencio el workspace del compositor (`localStorage odysseus-workspace`); el agente pasó a trabajar en esa subcarpeta. Un pick que llega tarde debería caducar o pedir confirmación.
- PARCIAL (3afa3da3): cada turno nuevo arranca con el historial muy recortado (13-15k de 200k) y el modelo gasta 10-20 rondas en «localizar la raíz» del workspace que ya conocía. Revisar qué se poda entre turnos en una sesión de agente con workspace. Mitigado por el lado del prompt: el bloque de orientación lista ahora la raíz del workspace (24 nombres / 600 caracteres). Sigue abierto lo que se poda entre turnos.
- PARCIAL (e13c44f8): escribir un mensaje largo (~700 caracteres) en el compositor de una conversación larga congela el renderer >30 s (CDP `Input.dispatchKeyEvent` timeout). El borrador ya no sube al padre en cada tecla (estado local + empuje cada 250 ms). Medido lo que queda: ~350 ms por relayout de página y 700-3000 ms por evento de entrada en una conversación larga; falta agrupar el autoajuste de altura en un rAF (un solo reflow por fotograma, sin tocar nada si la altura no cambia) y estudiar contención/virtualización de la transcripción.
- ABIERTO: «Changes»/`mutations` del resumen de turno lista ficheros que no cambiaron (`pokemon_species.csv`, un `cults3d.json` en la raíz que no existe).
- ABIERTO: el panel Progress se queda en «0 of 7» aunque el trabajo avanza: el modelo no vuelve a llamar a `todowrite`. Un recordatorio del harness cada N rondas sin actualizar ayudaría.
- NOTA de coaching: el agente editó el validador (`cults3d_check.py`) para su tarea — inofensivo esta vez, pero es corregirse su propio examen. La skill ahora lo prohíbe; en el harness podría marcarse como «mutación sospechosa» cualquier escritura a un fichero que la propia tarea usa como verificador.
- ABIERTO: el checkpoint del workspace tarda ~91 s en una carpeta con blends y PNG grandes; cada turno con escritura lo paga.
- ABIERTO: la insignia «PCIe spill» aparece con el modelo 100 % en GPU según `ollama ps`.
- IDEA: `presence_penalty` 1.5 para cuantizados Qwen (recomendación oficial contra repetición) en `model_load_options[...].extra` del q4; medir antes/después con el mismo lote.

## 20-09 — Piper TTS: síntesis del paquete Python aislada en proceso hijo (FAUSTUS.md §153)

- **`piper-tts` no está instalado en esta caja de arena** (sin red de pip hacia el paquete real): `tests/test_tts_worker.py` prueba `engine_not_installed` contra el `ImportError` real de "piper" no instalado, y el resto del protocolo del worker (éxito, fallos, timeout) contra el motor falso `test_fake`. Nunca corrió una síntesis real del paquete `piper` de principio a fin, ni una descarga real de voz desde Hugging Face, ni un timeout matando al hijo real de Piper a mitad de una síntesis de verdad (solo el motor falso simula el sueño). Script exacto de repetición en Windows (instalar `piper-tts`, descargar `es_ES-davefx-medium`, sintetizar, comprobar duración del WAV, forzar un timeout corto y confirmar que mata al hijo) en FAUSTUS.md §153.
- **La app en vivo no se abrió en el navegador para esta tarea**: Settings → Voice con el selector de motor/voz Piper y el flujo de descarga por botón ya existían antes de este cambio (no se tocó la UI), así que no se repitió la comprobación visual — solo se confirmó por lectura de código que sigue intacta.

## 20-09 — localizadores de PDF y sanitizado de PII en RAG (FAUSTUS.md §146)

- No verificable sin la máquina en vivo: esta caja de arena no tiene ChromaDB alcanzable, así que `index_personal_documents`/`manage_rag add_directory` nunca se probó de verdad persistiendo en un ChromaDB real — solo con `VectorRAG` construido vía `__new__` y `add_document` sustituido (mismo patrón que `tests/test_rag_index_hidden_dirs.py`). Repetir en `D:\LocalAI\faustus-dev-data` con ChromaDB arrancado (`docker compose up chromadb`, puerto 8100): indexar un PDF real de varias páginas, comprobar los `locator` en los metadatos de Chroma, buscar con `rag_manager.search()` y confirmar que el `[fichero.pdf pN#bM]` aparece en el contexto inyectado al chat, activar `rag_pii_redaction` y confirmar que un PDF con datos de contacto reales queda sanitizado en el índice pero el fichero original en disco no cambia. Instrucciones exactas (llamadas a función) en el informe de esta tarea.
- No verificado en vivo: no hay pantalla en el Studio que muestre el localizador de un fragmento citado — hoy solo viaja en el texto inyectado al modelo, no como un elemento propio de la UI de citas.

## 19-09 noche — radar (FAUSTUS.md §133-…)

- (a) En la comprobación en vivo, la bandeja de Actividad no llegó a pintar la pregunta de Code Mode (§141) porque la pestaña de Chrome estaba oculta (`visibilityState` en `hidden`) — se respondió por la ruta de API, no desde la bandeja visualmente. Repetir con la pestaña visible.
- (b) La instancia de desarrollo en el puerto 7001 estaba leyendo el directorio de datos real porque su script de arranque fijaba una variable que el código ya no lee — arreglado en el script de arranque local fijando `ODYSSEUS_DATA_DIR`. Comprobaciones en vivo anteriores pueden haber tocado datos reales (los ajustes se revirtieron). El directorio de datos de desarrollo recibió una copia de la clave/fichero de autenticación real para poder reusar la sesión del navegador (backups `*.devbak`) — aun así el navegador seguía mostrando sin autenticar en 7001, así que las comprobaciones visuales de interruptores de Ajustes en 7001 necesitan iniciar sesión.
- (c) La revisión ciega de investigación (§144) no tiene vista propia todavía; la vista de informe de investigación tampoco muestra los veredictos de las citas.
- (d) #170 enrutado de modelo por schema no aplica hasta que exista una herramienta de extracción a schema de cara al usuario.
- (e) Los modelos pequeños a veces devuelven solo 1 perspectiva en la planificación por perspectivas (§133) en vez de las 2-4 esperadas.

## 19-09 — reglas de política por argumento, panel de Ajustes sin ver en vivo (FAUSTUS.md §132)

- No verificado en vivo: arrancar Studio de verdad, entrar en Ajustes → Tools → "Argument rules", dar de alta una regla desde el formulario, editarla, borrarla, y usar la caja "Test" con una herramienta y JSON real — todo se probó por API (`curl` contra el servidor real) y por `tsc`, no con clics reales en el navegador. El entorno de esta tarea no tiene acceso de red desde la extensión de Chrome hacia el `localhost` del contenedor donde corre el servidor.
- No verificado en vivo: disparar una regla `action: "ask"` dentro de una conversación real y ver la tarjeta de aprobación en el chat con el texto de la regla — el puente hacia `tool_approval_store` se probó por test unitario (mismo patrón que `test_tool_approval_single_action_scope.py`), no contra el bucle de agente completo (`stream_agent_loop`) con un modelo de verdad.

## 19-09 — gráficos en el chat, verificación en vivo pendiente (FAUSTUS.md §131)

- No verificado en vivo: arrancar Studio de verdad, hacer que un modelo escriba una respuesta con un bloque ` ```chart ` real y confirmar en el navegador que se pinta el SVG (barra/línea/tarta/área), que el interruptor "Show data"/"Hide data" funciona, y que un bloque `chart` con JSON roto cae al bloque de código normal en vivo, no solo en el validador puro (`studio/checks/chartSpec.check.mjs`).
- No verificado: apariencia en modo oscuro y claro lado a lado (los colores vienen de los tokens del tema, revisados por lectura, no por captura de pantalla).

## 19-09 — el suelo de temperatura local no llegaba al modo chat (FAUSTUS.md §117, tercera parte)

- No verificado en vivo: repetir la sonda contra `llama-server` (`/slots` a mitad de una petición) en modo chat llano, sin `/temp` ni preset, y confirmar `temperature=0.6` en la petición real (antes llegaba 1.0).
- No verificado en vivo: confirmar que un `/temp 0.9` de turno en modo chat sigue ganando sobre el suelo (0.9 en `/slots`).
- No verificado en vivo: confirmar que un preset con temperatura propia gana sobre el suelo en modo chat real (no solo en el test unitario que fingía `ChatHandler`).

## 18-09 noche — residencia deja de ser solo-Ollama (FAUSTUS.md §118)

- No verificado en vivo: con `llama-server` real sirviendo `qwen3.8-27b-q8-llamacpp` en `:8081` (registrado como endpoint), abrir el Studio y confirmar que el widget de Vitals muestra el nombre del modelo en vez de «no model», y que Ajustes → Local models lo lista en «Loaded now» con «servido por» y sin botón Unload.
- No verificado en vivo: con Ollama intentando cargar un modelo grande mientras `llama-server` retiene los 47 GB, confirmar que la admisión rechaza/pregunta y que el mensaje nombra el endpoint de llama.cpp (`reason`/`external_occupancy_note`).
- No verificado en vivo: apagar `llama-server` y confirmar que las tres superficies (Vitals, Local models, admisión) vuelven a su estado sin runner externo sin dejar filas fantasma.
- Pendiente de decidir junto con lo de §114: si el modelo por defecto vive en Ollama o en el endpoint llama.cpp, este parche solo hace visible lo que ya está cargado — no cambia dónde vive el default.

## 18-09 noche — el turno de agente ya no debería terminar en silencio (FAUSTUS.md §115)

- No verificado en vivo: repetir la tarea larga y repetitiva (la de la Pokédex u otra similar) contra el llama-server real y confirmar que las rondas se extienden solas con la línea de progreso («continúa: unidad N») en vez de la tarjeta «Allow this task to continue?», y que si el modelo se atasca de verdad aparece una pregunta concreta (`ask_user`) en vez de la tarjeta o el silencio.
- No verificado en vivo: confirmar en el Studio que el bloque «Tareas grandes» del system prompt aparece en una sesión con workspace activo (se puede ver con el inspector de prompt si existe, o comprobando el comportamiento: el modelo debería mencionar unidades/cursor sin que se le pida).
- Pendiente crear en la máquina de Luis `data/skills/general/iterative-cursor-loop/SKILL.md` con `owner: "*"` en el frontmatter (antes puede que exista sin ese campo, o con un valor distinto) y confirmar que aparece en `manage_skills action=list` para un usuario que no sea quien la escribió.
- Pendiente confirmar que una nota de memoria procedural guardada con dueño global (`owner=""`) aparece en el bloque de «memoria aprendida» de un chat de un usuario distinto — el mecanismo ya existía (`scoped_items`), solo se confirmó con tests unitarios, nunca contra el store real en disco.
- No se pudo aislar una causa única y reproducible de «media turno corta tras 0-1 llamadas a herramienta» — se implementaron las mejoras pedidas (auto-continuación por progreso, pregunta final, nudge de ronda vacía desde la ronda 1) igualmente porque son correctas de por sí, pero si el síntoma original vuelve a verse en vivo, revisar también el detector de bucles inline (`_stuck_rounds`/`_tool_call_signature` en `src/agent_loop.py`) y `src/loop_breaker.py` (documentado pero todavía sin cablear a la ejecución real).

## 18-09 noche — llama-server en el PC (FAUSTUS.md §114)
- `/think on` no llega a la petición de llama-server: la sesión sigue con `<think></think>` precerrado y sin `reasoning_budget`. La decisión de `_resolve_think_decision` sí funciona por ajuste/override, pero el comando de barra no la alimenta en ese endpoint. Revisar `slash_think` → overrides del turno.
- Con llama-server sirviendo el q8 (47 GB) el keeper (`warm_default_model`) queda en false y Ollama solo tiene el 4b de utilidad; al volver a Ollama para el 27B: `D:\LocalAI\Stop-LlamaServer.ps1` y `warm_default_model=true`.
- Pendiente de decidir: modelo por defecto en el endpoint "llama.cpp (local)" o en Ollama; hoy sigue `qwen3.8:27b-q8_0` en Ollama.

## 18-09 noche, segunda corrección (llama-server pensaba sin parar con `--jinja` — FAUSTUS.md §114 «Segunda corrección»)

- No verificado en vivo: falta repetir la conversación exacta contra el llama-server real y confirmar en `/slots` que `chat_template_kwargs.enable_thinking` llega en `false` por defecto, que ya no se agota el tope de 8192 razonando (antes 4 rondas, 40 minutos, sin respuesta), y probar un `/think on` explícito para confirmar que el razonamiento sigue disponible cuando se pide a propósito (con `reasoning_budget: 4096`).
- No se ha probado contra vLLM, solo contra la descripción de llama-server del incidente — confirmar que `chat_template_kwargs.enable_thinking` también surte efecto ahí si algún día se usa como endpoint.

## 18-09 noche, corrección (llama-server local seguía sin suelo de sampler ni tope — FAUSTUS.md §114 «Corrección»)

- No verificado en vivo contra el llama-server real de Luis: falta repetir la conversación que corrió 15 minutos (7800+ tokens) y confirmar en `/slots` que `max_tokens`/`repeat_penalty`/`min_p` ya llegan correctos, y que el tope de 8192 (`local_openai_max_tokens_default`) no corta una respuesta legítima larga.
- Confirmar en pantalla que el razonamiento de llama-server (`--jinja`, Qwen3) sigue yendo al panel de pensamiento del Studio y no se mezcla con la respuesta final — el manejo de `reasoning_content` ya existía y solo se confirmó con tests, nunca en vivo contra este servidor concreto.

## 18-09 noche (el sampler local no llegaba a un modelo sin `extra` guardado — FAUSTUS.md §114)

- Todo lo de este lote está probado con un `_FakeClient`/streams SSE enlatados, nunca contra el Ollama real de la máquina de Luis. Falta, en vivo: (1) repetir la conversación exacta que degeneró con `qwen3.8:27b-q8_0` (endpoint `/v1/chat/completions`, `num_ctx` 235008) y confirmar que ahora sale coherente desde el primer token; (2) confirmar en el log que la petición se mueve a `/api/chat` nativo para ese modelo aunque no tenga `extra` guardado; (3) forzar un colapso real y confirmar que la escalera de recuperación salta el escalón 2 (mismo modelo) y responde desde el modelo de utilidad en mucho menos de los 12 minutos del incidente original; (4) probar el mismo modelo por llama-server local si está configurado, y confirmar que `min_p`/`repeat_penalty` llegan como campos de primer nivel en `/v1/chat/completions`; (5) provocar en vivo una racha de gibberish (prompt adversarial, temperatura muy alta) y confirmar que el guardia corta antes de los 300 caracteres de ventana.
- No se ha medido si `local_gibberish_script_threshold` (0.40) es el valor correcto para el uso real de Luis — mayormente español/inglés con código y nombres técnicos ocasionales en otros alfabetos (p. ej. identificadores). Si aparece un falso positivo en vivo (contenido legítimo con muchos caracteres no latinos, como una respuesta que cita texto en japonés/coreano/árabe extensamente), subir el ajuste o desactivarlo por sesión.
- Queda pendiente decidir si `_is_local_ollama_target` debería ampliarse a reconocer un Ollama en un puerto no estándar SIN que el admin lo haya declarado en Ajustes → Local models (hoy no se asume Ollama solo por el puerto) — se dejó deliberadamente conservador para no inyectar campos Ollama-only en un servidor local desconocido, pero puede que valga la pena un heurístico adicional (p. ej. sondear `/api/tags`) si aparece otro caso real de un Ollama en puerto remapeado.

## 18-09 tarde — renombrado interno REVERTIDO (commit 301d008d)
- El renombrado de identificadores (§112) se desplegó y la app dejó de ver conversaciones, conectores, procesos y correo: los datos en `data/` estaban intactos, pero la cookie de sesión, las claves de `localStorage` y valores guardados (tipo `odysseus` en integraciones) ya no coincidían con lo que buscaba el código. Se revirtió entero; la carpeta sigue siendo `D:\LocalAI\faustus` (eso no depende del código).
- Para retomarlo: (1) inventariar todo identificador que viva en `data/` (ficheros JSON, columnas, valores de tipo, claves de `localStorage`, nombre de cookie) y escribir una migración idempotente; (2) hacer la cookie y las claves de `localStorage` con lectura del nombre antiguo durante una versión, no corte limpio; (3) probar contra una COPIA de `data/` real antes de tocar la instalación.
- La migración de `localStorage` del renombrado borró las claves antiguas en los navegadores que abrieron Studio entre medias: workspace y último usuario se resetean una vez.
- El resto (env vars, cookie, `localStorage`, scripts) sigue pendiente del plan de migración de arriba.

## 18-09 (renombrado interno completo y traslado a D:\LocalAI\faustus — FAUSTUS.md §112)
- Revisar: `studio/src/shell/notifications-tray.tsx` + `notifications.ts` están implementados y con backend (`routes/notifications_routes.py`) pero no se montan en `AppShell.tsx`. Decidir: cablear o borrar.
- Revisar: los nombres de colecciones vectoriales y las columnas `odysseus_kind`/`odysseus_ref` siguen con el prefijo antiguo a propósito; si algún día se hace una migración de datos, es el momento de renombrarlos.
- Revisar: `origin/dev` sigue existiendo en GitHub y `origin/HEAD` apunta a él en el clon; si la rama remota ya no sirve, borrarla (push de Luis).
- Comprobar en el PC: los dos servidores MCP «Hoard» fallan al conectar con WinError 2 desde el 17-09 (antes del renombrado): la ruta del comando ya no existe.

## 18-09 (bloque MCP del prompt recortado a lo seleccionado — FAUSTUS.md §110)

- Todo lo de este lote está probado con un `McpManager` de mentira (3 servidores × 20 tools de prueba) y con `src.settings.get_setting` parcheado — nunca contra los ~10 servidores MCP reales de la máquina de Luis. Falta, en vivo: (1) mandar «hola» y mirar el ledger de contexto (o los logs) para confirmar que la sección `mcp` bajó de los 14.657 tokens confirmados en vivo a algo cercano al tope de 1500 (o a 0 si nada se seleccionó ese turno); (2) un turno que sí necesita un tool MCP concreto (p. ej. un servidor de archivos o de navegador) sigue pudiendo llamarlo sin fricción, con su esquema nativo intacto — no solo en test; (3) `lookup_tools` con el nombre de un tool MCP que no estuviera en el conjunto seleccionado de ese turno, y confirmar que lo sigue encontrando y cargando pese al recorte del prompt; (4) activar `agent_mcp_prompt_full_listing` desde Ajustes y confirmar que el volcado completo de siempre vuelve tal cual estaba, por si alguien lo necesita para depurar un servidor MCP concreto.
- No se ha medido el efecto real sobre un modelo local pequeño (el síntoma original: contestaba con el separador de contexto o entraba en bucle con el volcado de 14.657 tokens presente) — falta una sesión en vivo con qwen3 o similar confirmando que ya no ocurre.
- El bloque de integraciones usa el mismo criterio (nombres solo, salvo que `api_call` esté en el conjunto seleccionado) pero solo se ha probado con integraciones de mentira; falta confirmar en vivo que con integraciones reales configuradas (Gitea, Linkding, Home Assistant…) el agente sigue sabiendo qué endpoint pedir cuando `api_call` sí está seleccionado.

## 18-09 (el modelo por defecto no se descarga — FAUSTUS.md §109)

- Todo lo de este lote está probado con `/api/ps`/`/api/generate` y reservas/tickets de `vram_admission` simulados, nunca contra el Ollama real de la máquina de Luis. Falta, en vivo: (1) dejar Faustus corriendo un buen rato con la otra app local activa (la que pisaba el `keep_alive` a 5 min) y confirmar con `ollama ps`/`GET /api/local-models` que el modelo por defecto nunca queda fuera de VRAM más de un ciclo del guardián (~20 s ahora, antes 120 s); (2) varios reinicios seguidos de Faustus y confirmar que cada uno recarga el modelo por defecto sin pisar una carga de otro modelo en curso; (3) mirar la pantalla Ajustes → Local models y confirmar que la fila del modelo por defecto dice «Kept loaded by Faustus (default model)» y no el «kept loaded» genérico de antes; (4) probar el botón «Unload» explícito de esa pantalla contra el modelo por defecto — debe descargarlo igual (la protección nueva es solo contra los caminos automáticos/silenciosos) y el guardián debe recargarlo en el siguiente ciclo; (5) una pregunta del móvil llegada justo en el hueco entre una caída y el siguiente ciclo de 20 s: confirmar que no se queda sin respuesta (el propio turno debería disparar su carga vía `admit()` normal, no depender solo del guardián).
- No se ha confirmado en vivo si `src/vram_admission.py::_evict`/`restore_keep_alive` eran de verdad la tercera causa sospechada de la desaparición del modelo (junto al reinicio de Faustus y al `keep_alive` de la otra app) — el código ya protegía la mayoría de los caminos automáticos antes de este lote (pin + `_protected` en `assess()`); este lote cierra el hueco que quedaba en el modo `auto` de `admit()` y en `admit_bytes`/`_ollama_suggestion_candidates`, pero sin una sesión en vivo con las tres causas a la vez no se puede confirmar cuál pesaba más.
- `OLLAMA_KEEP_ALIVE=-1` como variable de entorno del servicio Ollama (cinturón y tirantes, documentado en FAUSTUS.md §109) no se ha aplicado en la máquina real — decisión de Luis, fuera del alcance de este repositorio.

## 18-09 noche (corrección: el modelo por defecto ya cede el sitio solo — FAUSTUS.md §109 «Corrección»)

- Regresión vista en vivo la misma noche del lote X-B: al elegir un modelo local más grande que no cabía junto al por defecto (29,4 GB necesarios, 23,1 GB libres), la tarjeta salía con «0 models loaded — tick what to unload» porque `_ollama_suggestion_candidates` excluía al por defecto del todo. Corregido: el por defecto cede solo (sin tarjeta) en todos los modos cuando eso basta; solo si no basta aparece en la tarjeta, ahora etiquetado. Falta, en vivo, contra el Ollama real: (1) reproducir el caso exacto (el quant grande de esa noche) y confirmar la línea «X steps aside for Y» en vez de la tarjeta vacía; (2) confirmar que el por defecto vuelve solo a los ~10 minutos de inactividad del otro modelo (`warm_default_model_yield_minutes`), o antes si se descarga; (3) confirmar que un modelo de embeddings activo (RAG) no retrasa la vuelta del por defecto; (4) forzar el caso insuficiente y confirmar la fila «default — kept loaded by Faustus» ticable en la tarjeta real.
- Todo lo de esta corrección está probado con `/api/ps` y `assess()`/`last_active_seconds` simulados — nunca contra el Ollama real ni la heurística de nombre de embeddings contra un modelo de embeddings real instalado en la máquina (`nomic-embed-text` u otro).

## 18-09 (sin `ctx_ack` ni bucles de ceros, + escalera de recuperación — FAUSTUS.md §108)

- Todo lo de este lote está probado con streams SSE enlatados (mismo patrón que el resto de `tests/test_agent_loop*.py`), nunca contra un Ollama real. Falta, contra el 27B que produjo los dos bugs originales: (1) confirmar que con `repeat_penalty 1.05`/`min_p 0.05` de serie ya no aparece «0000…» en una sesión recién abierta con el prompt exacto que lo disparó (PENDIENTES §104: «El modelo local 27B degeneró en un turno de prueba»); (2) confirmar en pantalla que con memoria recuperada grande (~14k tokens, el caso descrito en §107) ya no responde solo `<<faustus_ctx_ack>>`, y que si lo hace varias veces seguidas la escalera de recuperación saca una respuesta útil en vez de terminar en error; (3) vigilar si fundir el contexto no fiable y la pregunta real en un solo mensaje (`--- Your message ---`) cambia el comportamiento del modelo de alguna forma no prevista — p. ej. que empiece a citar o comentar la etiqueta del envoltorio, cosa que antes no podía pasar porque quedaban en mensajes separados.
- **Escalera de recuperación (cambio de requisito del dueño, mismo día: nunca terminar en error con un modelo cargado).** El escalón 3 depende de que Ajustes → Modelos → Endpoint de utilidad tenga configurado un endpoint DISTINTO del principal (`src.endpoint_resolver.resolve_endpoint("utility", …)`); si no lo tiene, la escalera llega al escalón 4 (error) un paso antes de lo posible — confirmar en la máquina real que ese endpoint existe, responde, y que normalmente ya está cargado (para que el escalón 3 sea rápido). No se ha visto en pantalla el evento `harness_check` `status: "recovery"` (¿Studio ya lo muestra como "Recuperando…", o hace falta cablear el frontend?) ni el tiempo real que tarda la escalera completa (hasta 3 llamadas más al modelo). El mensaje final cuando los cuatro escalones fallan («The model looped; try rephrasing or another model») tampoco se ha visto renderizado, solo comprobado contra el evento `agent_terminal` en un test.
- `local_repeat_penalty_default`/`local_min_p_default` son ajustes nuevos sin campo propio en Ajustes → Modelos locales (no llevan prefijo `agent_`/`browser_`/`desktop_`, así que tampoco entran en el esquema de `agent_settings_schema.py`); hoy solo se pueden cambiar editando `settings.json` a mano o vía `PATCH /api/auth/settings` directo. Si conviene exponerlos en la UI, hace falta decidir dónde encajan (¿junto a las opciones por modelo, o un ajuste global?).

## 18-09 madrugada — verificado en vivo tras w110/w111 (§108-§110)

- «hola» en modo agente: prompt de 19.341 → **5.184 tokens** (el bloque «MCP tools» pasó de 14.657 a 500); primer token en 4,6 s (antes 15 s). Respuesta normal, sin `<<faustus_ctx_ack>>`.
- Queda: probar la escalera de recuperación con un caso real que degenere (no se ha reproducido tras el cambio) y que Studio muestre «Recovering…».
- El modelo por defecto no se ha descargado en toda la pasada (`ollama ps` â†’ Forever); pendiente ver el keeper actuar cuando el tracker de candidaturas vuelva a usar Ollama con su keep_alive de 5 min.

## 18-09 (lote S: búsqueda automática en preguntas de actualidad + favicons — FAUSTUS.md §107)

- Verificado en vivo (w107): «¿Ganó el Madrid su último partido?» busca sola y responde; ver FAUSTUS.md §107 «Pasada en vivo». Pendiente: un POST directo a `/api/chat_stream` (cookie + JSON) desde un script se queda parado tras «[doc-inject] no active doc…» sin entrar en el bucle del agente, mientras que el relé móvil funciona — averiguar qué espera ese camino (¿handshake del cliente?, ¿cola «waiting for idle»?). Y la admisión de VRAM en modo «ask» bloquea a cualquier llamador sin UI (la batería se quedó esperando la tarjeta cuando el 27B se había descargado): para peticiones por API debería decidir sola o fallar rápido.
- Otra app que use el mismo Ollama (p. ej. el tracker de candidaturas) manda su `keep_alive` por defecto (5 m) y despina el 27B; el warmup ahora re-pinea cada 2 min, pero entre medias puede caer. Alternativa: `OLLAMA_KEEP_ALIVE=-1` en el entorno del servicio Ollama.
- Un turno cuya salida es solo `<<faustus_ctx_ack>>` (visto con memoria recuperada como contexto no fiable en una pregunta simple) ya recibe un nudge; falta mirar por qué el 27B lo emite tan a menudo con contexto recuperado grande (~14k tokens) y si conviene recortar esa recuperación.
- `_fetch_favicon_bytes` en `routes/favicon_routes.py` nunca hizo una petición HTTPS real a un `favicon.ico`/`<link rel=icon>` de verdad — cubierto con `fetch` mockeado. Confirmar en vivo que el flujo de fallback (favicon.ico → homepage → placeholder) funciona contra dos o tres dominios reales conocidos.
- El aviso de una línea («This question is time-sensitive…») se añade como mensaje `role: system` justo antes del bucle de rondas; falta comprobar en un chat real que un modelo local (p. ej. qwen3.5) lo respeta y busca en la primera ronda en vez de ignorarlo.
- `_DOMAIN_RULES["web"]` reforzado con la instrucción de buscar sin pedir permiso — no se ha medido si esto hace que el modelo busque de más en preguntas límite (p. ej. «¿qué opinas del último modelo de IA?», que mezcla opinión con actualidad). Vigilar falsos positivos del heurístico en uso real más allá de los 44 casos de `tests/test_freshness.py`.

## 18-09 (lote T: Piper, proveedores de comando, frases de parada — FAUSTUS.md §106)

- Piper verificado con motor y voz reales en el scratchpad de la sesión (no en el repo ni en la máquina de Luis): falta la pasada en vivo desde Ajustes → Voz en la instancia real (botón «Instalar motor», descarga de una voz, síntesis desde el panel), y confirmar que el binario de Windows (`piper_windows_amd64.zip`) instala igual de bien — solo se probó el de Linux.
- Los proveedores «comando» se probaron con un script Python de usar y tirar, nunca con un ejecutable de terceros real — el operador que los use es quien primero validará una plantilla concreta.
- Frases de parada: el textarea nuevo en Ajustes → Voz no se ha visto en el navegador (sin sesión admin en este entorno para llegar a Ajustes en vivo); la lógica está cubierta por `voice-jarvis.check.mjs` y por `capabilities()` devolviendo `stop_phrases`.

## Noche del 17-09 (voz manos libres, FAUSTUS.md §105)

- Sin micrófono en el entorno: probar en vivo la interrupción (¿se pierde la primera palabra?), la guarda de eco (¿se traga una réplica rápida como «sí»?), las frases de parada y qué transcribe Whisper cuando se dice «Faustus» (ampliar la lista de variantes en `engine.ts::stripWakeWord` si hace falta).
- Latencia «oído en»: si molesta, el siguiente paso es STT en streaming por WebSocket con parciales (decodificar cada 300-500 ms sobre ventana deslizante) en vez de esperar al silencio y subir el clip entero.
- Voz de salida: las voces de Windows suenan a Windows. Opción local con más calidad: instalar Kokoro (ya hay proveedor «Local (Kokoro)» en Ajustes → Voz) o el nuevo proveedor Piper (FAUSTUS.md §106) — instalar el motor y descargar una voz desde Ajustes → Voz.
- Voz desde el móvil: el bucle es el mismo (HTTPS por la VPN de malla), pero la pestaña tiene que estar en primer plano; la palabra de activación no funciona con la pantalla apagada.

## Noche del 17-09 (móvil, lotes P-A/P-B — PWA instalable + push, ver `docs/api/mobile.md`, `docs/ui/pwa.md`, FAUSTUS.md §104)

- Verificado en vivo en :7000 (w100): `/sw.js` con `Service-Worker-Allowed: /` y el worker activo con ámbito `/`; `/manifest.webmanifest`; «Instalar Faustus» aparece (el navegador acepta el manifest); permiso de notificación concedido desde el diálogo del navegador; vista móvil a 500 px con cinco pestañas inferiores y sin desbordamiento horizontal; un turno enviado desde esa vista emite `turn_finished` en el bus.
- **Pendiente de verdad**: recibir un push real. El navegador de escritorio con el que se probó trae desactivado su servicio de push (`AbortError: Registration failed - push service error` al suscribirse). Ahora la sección muestra ese error de forma persistente y explica qué hacer; la prueba completa (suscribir → `Enviar notificación de prueba` → notificación con la pestaña cerrada) hay que hacerla desde el móvil o desde un navegador con el servicio de push activo, con el servidor expuesto por HTTPS (VPN de malla + `serve` del 443 al 7000).
- `pushsubscriptionchange` en `static/sw.js` nunca se ha disparado de verdad (el navegador lo dispara raramente, cuando el servicio de push rota las claves de una suscripción); su lógica de re-suscripción está escrita según la spec pero sin caso de prueba real que la dispare.
- El modelo local 27B degeneró en un turno de prueba («0000…», `prediction aborted, token repeat limit reached`) con un prompt trivial. No es de este lote, pero conviene mirar `repeat_penalty`/opciones por defecto del endpoint local.

## Noche del 17-09 (móvil, lote M-A — servidor, ver `docs/api/mobile.md`)

- Servidor listo y probado (`tests/test_notifications.py`, `tests/test_mobile_routes.py`, 34 tests): bus de eventos (`src/notifications.py`), `/api/mobile/*` (bootstrap, notifications, sessions, messages, WS, shim de envío) y los cuatro enganches (fin de turno, aprobaciones, tareas, recordatorio). Falta el lote M-B (`mobile/android/**`, `scripts/mobile_*.py`) — otro agente lo estaba haciendo en paralelo; una vez esté, probar el emparejamiento real desde un móvil (QR → `GET /api/mobile/bootstrap` → WS) y no solo con pytest.
- Dos huecos del contrato original que NO eran ciertos y quedaron corregidos en el propio código (no son deuda, ya están resueltos, pero vale la pena que quien mire el histórico lo sepa): (a) `require_admin` no aceptaba un token bearer de un usuario real (siempre pisa `current_user="api"`) — `_mobile_owner` en `routes/mobile_routes.py` lo soluciona; (b) el token de emparejamiento solo tiene el scope `chat`, no `sessions`, así que `POST /api/chat_stream` directo le daba 403 — de ahí el shim `POST /api/mobile/session/{sid}/send` (loopback interno con impersonación de owner, no duplica la lógica de streaming). `core/authz.py` ganó una regla nueva (`/api/mobile/*` → scope `chat`) sin la cual NINGÚN endpoint de este lote sería alcanzable con un token bearer — quien toque `core/authz.py` en el futuro debe saber que el test `tests/test_auth1_token_matrix.py::test_the_reachable_surface_is_exactly_this` fija la superficie completa a mano.
- `turn_error` está declarado en `src/notifications.KINDS` pero sin enganche — ningún punto de `chat_routes.py` mapea tan limpio a "una sola llamada, una vez por fallo" como `save_assistant_response` para el éxito. Si se quiere, revisar `_safe_stream()`/los branches de error alrededor de la línea 4148 de `routes/chat_routes.py`.
- El WS (`GET /api/mobile/ws`) no reenvía histórico al conectar (solo `hello.last_id`); un cliente que reconecta debe pedir `GET /api/mobile/notifications?since_id=` una vez y luego fiarse del socket. Documentado en `docs/api/mobile.md`, pero el lote M-B tiene que implementarlo así — no asumir que el WS manda lo perdido.
- No se ha probado en vivo (navegador/MCP) porque no hay móvil ni build Android en este lote — verificado con pytest (34 tests nuevos + 221 existentes de alrededor sin romper) y arrancando `app.py` completo en memoria para confirmar que `/api/mobile/*` queda registrado junto a `/api/chat_stream`.

## Noche del 17-09 (WhatsApp — FAUSTUS.md §100)

- Emparejado en vivo (17-09 ~17:40). La sesión actual se vinculó antes del arreglo del historial: para bajar todo el pasado de golpe hay que Unlink → Start → escanear otra vez; si no, «Load older messages» chat a chat.
- Tercera ola (respuestas citadas, reacciones, reenvío, edición/borrado, ticks, presencia, búsqueda, adjuntos, notas de voz grabadas, Ask Faustus) desplegada como w85: **falta la pasada en vivo** — sobre todo el permiso de micrófono de MediaRecorder en la app de escritorio, los popovers de reaccionar/reenviar, el salto a un mensaje citado que no está en la ventana cargada, y el 409 de citar/reenviar un mensaje anterior al arranque del puente (el mapa crudo vive en memoria).
- Un contacto que solo se conoce por LID (nunca llegó `sender_pn` ni contacto sincronizado) sale sin teléfono; se resuelve al re-emparejar (los contactos traen `lid`+`phoneNumber`) o cuando escribe.
- Hasta w88 cada reinicio de Faustus mataba el puente (y los perfiles de lanzamiento): `serve` terminaba a todos los descendientes. Ahora `spawn_detached` apunta los hijos en `data/runtime/detached.json` y el apagado los respeta — comprobar en el siguiente reinicio que el puente sigue vivo y que Jobhunter no hay que relanzarlo.
- Mensajes propios enviados desde el móvil mientras el puente estaba parado fallan al descifrar (`MessageCounterError`) y llegan por reintento; no es un bug nuestro.
- Ideas siguientes: reglas automáticas («si escribe X avísame por…»), crear grupos/participantes, estados, transcripción en segundo plano de audios largos, y que `/chats` sirva `archived` fiable sin re-emparejar (hoy solo llega con el historial inicial o `chats.update`).

## Noche del 17-09 (vigilantes y tarjetas de Inicio — FAUSTUS.md §99)

- Verificado en vivo: tiempo (desde el chat), correo, noticias y vigilancia de texto (por API), tarjetas en Inicio con Refresh. Queda probar el caso real de reposición en una tienda (el modelo pedirá la URL): si la tienda pinta el stock solo con JavaScript, `watch_page` dirá «sin señal clara» — entonces habría que capturar con el navegador integrado.
- El modelo eligió `when: today` para «mañana» al programarlo a las 8:00 (razonable, pero no literal): valorar que la tarjeta muestre hoy y mañana a la vez.
- `watch_page` decide disponibilidad por palabras («Add to cart», «Agotado», «Notify me»…); una tienda con render solo por JavaScript no muestra esas palabras en el HTML — en ese caso valorar el navegador integrado para la captura.
- El briefing de noticias depende del proveedor de búsqueda configurado (SearXNG por defecto): si no está levantado, la tarjeta dirá «search failed».
- Ideas siguientes (no hechas): tarjeta de calendario del día, tarjeta de «candidaturas» (Jobhunter: pendientes/entrevistas), precios (vigilar un número en la página y avisar por debajo de un umbral), RSS por URL de feed, y elegir tamaño/orden de las tarjetas arrastrando.

## Tarde del 17-09 (candidaturas por correo — FAUSTUS.md §98)

- `review_candidature_mail` verificado en vivo tres veces (rechazos, entrevistas, ambas): Jobhunter y calendario correctos e idempotentes. Desde que se leen todos los correos no-bulk de la ventana (no solo los de asunto «candidatura»), la entrevista con confirmación de hora también sale; queda valorar un evento de día completo para entrevistas a demanda con fecha límite («complete by …»); y el modelo dijo «en menos de una hora» de una entrevista ya pasada (13:00 vs 15:07): revisar cómo llega la hora actual al prompt del 27B.
- La receta `review-candidature-responses` decía «nunca crear candidaturas»; la herramienta las crea con `create_missing` (por defecto) porque es lo que Luis pidió. Actualizar el texto de la receta/`docs/api/candidature_recipe.md` para que lo diga.
- El 27B con `num_ctx` 199.680 iba a 2 tok/s con el diálogo de VRAM en cada turno; ahora 65.536. Si Luis quiere más, hay que medir la KV cache primero (la admisión dice «never measured»).
- La UI muestra «Loading the model into memory · No server signal» mientras una herramienta larga (2–3 min leyendo correos) trabaja: el estado debería decir «Running review_candidature_mail».

## Noche del 17-09 (Apps — FAUSTUS.md §101)

- Modelo por defecto precargado al arrancar (`src/model_warmup.py`, ajustes `warm_default_model*`): comprobar en el 7000 que tras el reinicio `ollama ps` muestra el 27B con expiración «Forever» y que sigue cargado tras un chat (el re-pin cada 10 min debe devolverlo a -1).

- Perfiles reales en el 7000: Dorian's (python de su venv, `-m selfhoard`, stop por su `scripts/stop.ps1`), Gepetto's (su propio Electron: `node_modules/electron/dist/electron.exe desktop/main.cjs`, que levanta el servidor 8767; `desktop=false` porque ya es ventana), Plato's (`app.py`, 5000), más Jobhunter's y Writer's con icono. Conectores `dorian` (credencial creada con `scripts/connect_faustus.py` en su repo) y `platos` conectados; `gepetto` creado pero sin probar `connect` con la app arriba (el OpenAPI de FastAPI debería dar más tools que el manifiesto).
- Un terminal de Windows quedó abierto de un arranque anterior al arreglo de `CREATE_NO_WINDOW`; cerrarlo a mano. Comprobar en el próximo Start que no aparece ninguno.
- `GET /api/launch-profiles/status` tarda ~2 s (la tabla de puertos/procesos); si molesta en la pantalla, cachear la tabla 2–3 s en `process_center`.
- La extensión de Chrome no acierta a pulsar Start en la tarjeta (un `click()` por JS sí): mirar si la rejilla desplaza el botón durante el polling de 5 s.
- Ideas: autostart de perfiles al arrancar Faustus, grupos por proyecto, importar/exportar perfiles, herramienta de solo lectura para el agente, consola en vivo por SSE en vez de cola de log.

## Tarde del 17-09 (centro de control — FAUSTUS.md §97)

- `/processes` en el Studio: falta cruzar los hijos MCP con la tabla `McpServer` para mostrar el nombre del servidor en vez de la línea de comandos, y decidir si el agente recibe una herramienta de **solo lectura** sobre la lista (nunca el Stop).
- Las apps que el asistente abre por Windows-MCP (Cursor, ChatGPT) salen en «Other apps» por nombre; no hay forma de saber quién las abrió. Si hace falta, el asistente puede anotar en Faustus lo que lanza (`POST` a un registro) — no hecho.

## Mediodía del 17-09 (conectores — FAUSTUS.md §96)

- Conectores de Luis dados de alta en el 7000: Jobhunter's Hoard (:5178, 15 tools, perfil `node server/index.js` con `PORT=5178`) y Writer's Hoard (:8766, 126 tools, perfil `open_exe` sobre `release/win-unpacked/Writers Hoard.exe`). Falta probar en vivo «seguir a la app»: arrancar un segundo Jobhunter (caería en 5179), parar el de 5178 y pulsar Check — el conector debe mudarse y decir «Followed the app from … to …».
- Un `stop` del servidor 7000 mata también las apps lanzadas desde un perfil (hijas del proceso): Writer's Hoard se cerró al reiniciar el 7000. Valorar lanzar los perfiles desacoplados (`CREATE_NEW_PROCESS_GROUP` + `DETACHED_PROCESS`) para que sobrevivan a un reinicio de Faustus.
- La app de escritorio sigue sin devtools ni menú: para diagnosticar otra pantalla en blanco vale `Ctrl+Shift+I` (el menú por defecto de Electron sigue activo aunque no se vea).

## Madrugada del 17-09 (harness para implementaciones largas — FAUSTUS.md §95, OBJ-16)

### Qué mirar en el 7001

- **Repetido el chat #14 (17-09, 03:00): OK.** Plan de 4 WP + «Sigue implementando el plan» con el 27B: tracker parseado, 4/4 implementados con tests reales, `ui_smoke` cazó un 404 real de assets que los tests no veían y la ronda de arreglo lo cerró. Tres fallos del harness corregidos sobre la marcha (FAUSTUS.md §95, «Verificado en vivo»). Repetido también el chat #15 (segundo chat, mismo adjunto): 4/4 cerradas con `plan_done` y evidencia, 0 mutaciones. Queda por repetir con un plan GRANDE (≥ 60 KB, 20+ tareas) y en un segundo chat SIN adjunto («Continua»).
- **`ui_smoke` con Flask real en Windows: OK** (`flask run --port`, `app.mjs` â†’ `text/javascript`, `style.css` â†’ `text/css`, Playwright sin errores de consola). Falta verlo con un proyecto FastAPI y con `npm start`.
- **Puerta dura.** Un turno con un test rojo nuevo: la tarjeta `verified` trae `gate: tests_failed` y el resumen `complete_unverified` («Not sealed as complete» en el Studio).
- **Deriva de dependencias.** Quitar `shapely` del venv del proyecto y abrir un turno: la nota de sistema con «falta shapely — instálala con install_dependencies» tiene que llegar antes de cualquier bash.

### Limitaciones honestas

- Turno de SOLO verificación sobre un plan ya hecho (chat #15 controlado): `check_completion` rechazó dos veces («creado» sobre ficheros solo leídos → correcto; «el servicio está listo» → `claims_without_mutation`, discutible). Afinar: cuando el tracker cierra 4/4 con evidencia y no hay claims de cambio, un «verificado, sin cambios» no debería costar una ronda.
- La puerta dura solo impide sellar `complete`; no abre una ronda de arreglo propia (las de tests/smoke/review siguen siendo las que arreglan).
- `plan_done` informa de los ficheros de la tarea que el turno no tocó, pero no ejecuta criterios tipados; el `Goal` de WP27 (`test_passes`, `http_ok`) aún no está enganchado al tracker.
- El parser de planes es heurístico (encabezados/listas/checkboxes): un plan en prosa pura da 0 tareas y cae al recorte a TOC de §90.
- `delegation_receipts` reintenta una vez con orden corta; si el segundo intento vuelve vacío, el padre recibe el aviso explícito y sigue — no hay tercer intento.

## Noche del 16-09 (auditoría Cursor, paridad 36/36, Reach, Creator fase 1-2)

Contexto completo en FAUSTUS.md §90-94 y OBJETIVOS.md OBJ-11/13/14/15.

### Qué mirar en el 7001

- **Creator con el flag.** `creator_enabled` sigue OFF por defecto en `src/settings.py`. Para ver algo hay que activarlo a mano y comprobar por pantalla: `/creator` (WP05), la biblioteca (WP03), un preflight real contra un motor instalado (WP09) y el lifecycle de un plugin (WP32) de principio a fin — nada de esto se ha probado con navegador real en este lote, solo con TestClient/pytest.
- **Personas.** `GET /api/personas` y el render de sistema (`render_system_block`) están probados por HTTP; falta comprobar en el Studio que un AGENT.md con `persona: security-auditor` realmente antepone el bloque al prompt en un turno de verdad, no solo en el test unitario de `agent_defs.py`.
- **Reach.** `reach_doctor` con `live=1` no se ha corrido contra la red real en esta sesión — solo contra `httpx.MockTransport`. Antes de dar los 9 canales por buenos en producción, correr `GET /api/reach/doctor?live=1` una vez con las credenciales que haya configuradas y mirar cuántos de los 9 responden `ready` de verdad.

### Los fallos heredados de la suite

Pasada del 17-09 sobre `89740d02` (+ arreglos): **18.946 verdes**; 46 fallos listados, de los que 7 eran nuevos y se corrigieron en el mismo día (`test_l62_edit03_bom_full_overwrite` ×4 por un contextvar que `test_H45_wiring` dejaba puesto; `test_studio_guards` ×2 por colores literales de WP13/WP20; `test_worker_refusals` por el reintento H3 sobre un worker con todas las llamadas rechazadas), 2 son de carga (`test_p1_eval_02_ablation` con un worker xdist caído, `test_browser_mcp_reconnect` pasa en solitario) y el resto son los heredados de abajo. Con 8 GB de RAM la suite tumba un worker y un `playwright-mcp` colgado deja al controlador esperando: correr con `-v --max-worker-restart=6` y leer los FAILED del log. La pasada anterior (16-09, `6c5bfd7`-`3a7d4a3`) daba **17.951 verdes / 38 fallos**, ninguno introducido ese día: los mismos ids fallan en una worktree de `bce096a8` con la misma carpeta (`comm` de las dos listas). Ficheros (por número de tests): `test_version_build.py` (3, reflog/git build info del entorno), `test_foreground_model_routing.py` (3), `test_agent_harness_loop.py` (3), `test_windows_native_execution.py` (2, solo Windows), `test_two_tier_search.py` (2), `test_l91_domain_synonyms.py` (2), `test_l68_desk02_ui_journeys_live.py` (2), y uno en cada uno de `test_w4a_requirements_js`, `test_w3a_composer_js`, `test_studio_vram_live_js`, `test_studio_saved_evidence_js`, `test_studio_research_restart_js`, `test_studio_ask_user_options_js`, `test_studio_approval_errors_js`, `test_l86_source_control_panel_js`, `test_cmp01_doc_session_js`, `test_cmp05_attention` (todos los `_js` fallan en la worktree porque no tiene `node_modules` — en la copia principal pasan), `test_research_resume`, `test_rerank`, `test_l67_exec_target_and_preview`, `test_docs_no_orphan_images` (`docs/spec/superpowers/*` de Cursor fuera de las carpetas permitidas; `LIFECYCLE.md` ya movido a `docs/spec/distribution/`), `test_desktop_tools::test_settings_defaults`, `test_asset_versioning`, `test_agent_profiles_builtin`, `test_agent_bash_windows`, `test_agent_asks_before_a_new_system`, `eval/test_baseline_match`, `acceptance/test_a04_replay_cursor::test_resume_without_an_active_run_is_still_a_404` (solo bajo xdist: «no such table: sessions», pasa en serie) y `acceptance/test_a21_embed_two_sessions` (2, sin `node_modules`/`happy-dom` en la worktree). Regla: un fallo nuevo se demuestra con la misma comparación, no se supone.

### xfail y limitaciones honestas que cada lote dejó

- **A31** — el coste de un delegate sin tabla de precios reporta siempre `unpriced_usage`, nunca `0.0` falso.
- **A29** — `loop_breaker.py` cableado en `agent_loop.py` (observa cada (tool, args, resultado), `observe_skipped` en las recuperaciones, `stop_reason=non_progressing_loop`).
- **Creator** — 27 de 43 paquetes implementados y montados detrás de `creator_enabled` (OFF); ningún motor real (ComfyUI, faster-whisper, TTS, ACE-Step/MusicGen) instalado en este entorno, así que todo adapter reporta `available=False` honesto. Quedan WP17/19/21/23/25/26/28/29/31/33/34/35/37/38/39/40/42.
- **Reach** — el backend de sesión de navegador (`x`/`reddit`) es un seam real sin lector conectado; búsqueda en `x` sin navegador ni `reach_nitter_base` responde `unavailable` honesto.
- **Code graph** — sin edge `INHERITS`; no recorre cadenas de herencia.
- **Fan-out** — sin SSE de progreso, solo poll.
- **PDF ops** — `compress` es deflate + dedup de `pypdf`, no re-muestreo de imágenes; `PATH_ARGUMENT_FIELDS` no se amplió con las rutas de `pdf_ops`, así que el reparador de argumentos (JSON-string→objeto) no las cubre todavía.

### Trampas nuevas de hoy

- **Reserva de VRAM compitiendo consigo misma.** `try_reserve`/`reserved_bytes` contaban la reserva SOBRANTE de un modelo que se está recargando como si fuera hueco ocupado por otro trabajo. Arreglado con `exclude_model=` — un modelo nunca compite con su propia reserva pendiente, otros modelos sí la siguen viendo. Visto en vivo en el turno de humo del 7001 con un 27B que se descargó entre la tarjeta de aprobación y la reanudación del turno (§94).
- **El ping de restore de `run_model_pin` carga el modelo si estaba descargado.** Un `generate` con prompt vacío contra Ollama para "restaurar" el `keep_alive` original CARGA el modelo si no estaba residente — 18 GB por nada. El restore ahora comprueba que el modelo sigue cargado antes de tocarlo (§90, §94).
- **`moduleResolution=Node10` vs TypeScript 6.** `sdk/ts` es `"type": "module"` pero sus builds CJS necesitan `Node10`; TypeScript 6 convirtió esa opción en error duro salvo `ignoreDeprecations: "6.0"`, y TypeScript 5.9 RECHAZA ese mismo valor (TS5103). `sdk/ts/scripts/tsc-node10.mjs` detecta la versión del compilador instalado y pasa el flag solo cuando hace falta — no asumir una versión fija de `tsc` en ningún script nuevo que compile ese paquete.
- **`.git` de solo lectura en Windows.** Un `git clone` deja ficheros pack de `.git` en modo solo lectura en Windows; `shutil.rmtree` normal falla al borrar el directorio de trabajo temporal. `_rmtree_force` (en `src/skill_sources.py` y replicado en `src/creator/plugins.py` con el mismo nombre y el mismo comentario "mirrors skill_sources._rmtree_force") quita el atributo de solo lectura antes de borrar. Cualquier código nuevo que clone un repo a un directorio temporal y lo borre después necesita la misma función, no `shutil.rmtree` a secas.
- **`subprocess.list2cmdline` para comandos de test en Windows.** `src/fanout/runner.py` arma el comando del test del proyecto con `list2cmdline(argv)` en Windows (`os.name == "nt"`) en vez de `shlex.join`, que asume comillas POSIX y rompe rutas con espacios o backslashes en cmd.exe.
- **`ast.literal_eval` sobre `FUNCTION_TOOL_SCHEMAS`.** Varios tests de paridad (`test_objective_tool_schema.py`, `test_tool_index_schema_parity.py`, `test_t8_wiring.py`) no importan `src/tool_schemas.py` — parsean su AST y hacen `ast.literal_eval` sobre el nodo de la asignación. Esto exige que `FUNCTION_TOOL_SCHEMAS` (y cualquier estructura que estos tests lean) siga siendo un **literal de dict/list en el código fuente**, nunca el resultado de una función o una comprensión que solo se evalúa en tiempo de ejecución — un tool nuevo añadido con `.append()` o construido dinámicamente no lo verán estos tests y darán una paridad falsa.
- **La unión de tools incluidas+diferidas decide las reglas de dominio, no solo las incluidas.** `src/agent_loop.py` calcula `_domain_union = included | deferred` antes de derivar qué bloques de reglas (entorno, permisos por dominio) entran en el prompt — una tool todavía diferida (no promovida por `lookup_tools`) YA cuenta para decidir si, por ejemplo, entra el bloque de entorno de shell. Cualquier lote que añada un dominio de reglas nuevo tiene que unirse a este cálculo, no solo a `included`, o una tool ofrecida-pero-diferida se queda sin su bloque de reglas hasta que se promueve.

## Ejecución nativa en Windows y el permiso por carpeta (14-09-2026)

Contexto completo en FAUSTUS.md §85. Lo que queda abierto:

- **Pantalla para las concesiones por carpeta.** `src/tool_approval_grants.py` guarda
  la respuesta «Siempre en esta carpeta» de la tarjeta de permiso y ya tiene
  `list_for(owner)` y `revoke(owner, workspace)`, pero no hay dónde verlas ni
  quitarlas. Sitio natural: Settings › Security, junto a las aprobaciones. Hasta
  entonces la única forma de revocar es borrar la fila de
  `<ODYSSEUS_DATA_DIR>/tool_approval_grants.json`.
- **La herencia es por subárbol y eso hay que verlo.** Conceder sobre
  `C:\Users\luism\Desktop\Proyectos independientes` cubre TODO lo que cuelgue de
  ahí. La tarjeta lo dice, pero la pantalla de arriba debería enseñar la ruta
  exacta concedida.
- **`powershell` no tiene `#!bg`.** Los trabajos en segundo plano siguen siendo de
  `bash` (`src/bg_jobs.py` lanza con el shell de bash). Un `.bat` largo que haga
  falta detached hoy se envuelve desde `bash` con `#!bg`, y eso **funciona a
  propósito**: el marcador `#!bg` se parte en `src/tool_execution.py` antes de
  llegar a `BashTool`, así que la guarda que enruta `powershell`/`cmd` al tool
  nuevo no lo ve. Es la única vía para detached en Windows y está bien que lo
  sea, pero conviene que `powershell` tenga su propio `#!bg` y que entonces la
  guarda cubra también ese camino.
- **POSIX con Docker caído cambia de comportamiento.** `agent_sandbox_mode` es
  `auto` por defecto: donde antes se rechazaba el comando, ahora lo corre el host
  y el resultado lo dice (`sandbox_skipped`). Quien quiera la puerta dura tiene
  que poner `strict` a mano. Decidir si el valor por defecto debería depender del
  sistema operativo en vez de ser global.
- **Suite entera sin pasar.** Este frente pasó 813 tests en los 30 ficheros que
  tocan sandbox, aprobaciones, validación de argumentos, selección de tools y la
  puerta de contexto externo, más `tsc`/`vite` y los checks del Studio. La suite
  completa (~14 min) no se ha corrido desde estos cambios.

## Comprobaciones pendientes

- **Voz física:** conversación completa con micrófono en español e inglés. No activar grabación ni permisos para cerrar esta casilla sin intervención del usuario.

- **Docker Desktop y el entorno del puente MCP.** Toda la noche del 08-09 se cayó al arrancar con `unable to get 'ProgramData'` cuando lo lanzaba una sesión a través del puente (cuatro vías probadas: directa, con el entorno repuesto, vía `explorer.exe` y como tarea programada interactiva). Lanzado por Luis a las 22:12 arrancó a la primera. El puente entrega un PowerShell **sin `ProgramData` ni `ALLUSERSPROFILE`**; anotado por si vuelve a aparecer con otro programa.

- **`OLLAMA_MAX_LOADED_MODELS=1` en el entorno de Ollama** para lo que no pasa por Faustus (`ollama run` en una terminal). No es código nuestro: es una variable en el servicio de Ollama de la máquina de Luis.

## Lista de Luis, 09-09 de madrugada (estado a las 23:10)

Las tres GPUs son reales: RTX 4070 Ti (12 GB) + dos RTX 5060 Ti (16 GB) = 43,9 GB. La GPU 1 salía sin lectura por un `—` en lugar de «0 MB» cuando está vacía; corregido.

## Lo que rompió la máquina el 08-09 (regla, no anécdota)

Dos 27B dentro a la vez —`q8_0` residente de una prueba (33 GB, con spill) y `q4_K_M` cargado por una research (17 GB)— más un build de Vite, dos tandas de pytest y siete procesos de Docker Desktop, superaron el **commit limit** de la máquina (147,7 GB = 128 de RAM + 20 de pagefile). La cascada, en orden: `cudaMalloc failed: out of memory` en la ronda 2, `MemoryError` en el servidor, `can't start new thread`, y después ni PowerShell arrancaba (`0xC000012D`, STATUS_COMMITMENT_LIMIT). El escritorio se quedó en negro con una sola ventana de error.

**La regla: nunca dos modelos grandes cargados a la vez, y nada pesado corriendo mientras hay uno dentro.** `ollama ps` antes de cargar, `ollama stop` del anterior. La puerta de admisión que automatiza esto es OBJ-1 en OBJETIVOS.md.

Las carencias de backend del índice anterior están implementadas; se ha eliminado ese índice vacío.
Las ampliaciones acordadas viven en OBJETIVOS.md; ahora mismo, OBJ-1 (puerta de admisión de VRAM). Eliminados los índices de UI resueltos; se pueden recuperar del historial Git.
No contar planes de inspiración o notas de implementación como otra cola de tareas.

## Spec v2 (cierre lotes 60-72 — 2026-09-11)

Ya no es una rama aparte: `feat/spec-v2-m1` está fusionada a master, y los
lotes 60-70 cierran el resto del paquete P0/P1 auditado en `MAPA_REUTILIZACION.md`
y `MAPA_P1.md`. Estado real tras este cierre:

- **P0** (`docs/spec/v2/MAPA_REUTILIZACION.md`): 99 IDs P0 propios de ese
  fichero, **99 existente, 0 parcial** (PLAN-01 y PLAN-03 se detectaron al
  recontar contra `backlog.json` y los cerró el Lote 71: plan corto/expandible
  y la tarjeta «Quién edita qué» sobre los leases reales de una delegación).
- **P1/P2/LAB** (`docs/spec/v2/MAPA_P1.md`): 84 IDs, **83 existente, 1
  parcial** (HW-06, decisión de producto — ver abajo).
- `FAUSTUS_TOOL_ARG_VALIDATION` nace en `strict`. Si un modelo local empieza
  a ver «INVALID ARGUMENTS» donde antes la herramienta se apañaba, bajar a
  `warn` y anotar la forma que envía para añadirla a la reparación
  (`src/tool_schemas.py::repair_tool_arguments`).
- `question_store` y `chat_outbox` son SQLite propios en `DATA_DIR`; entran
  en el backup por defecto, sin credenciales. Purga: 24 h terminadas / 12 h
  aceptadas (outbox).

### Abiertos — lo que sigue genuinamente pendiente

- **HW-06 — decisión de producto.** La primitiva de nodos remotos
  (`src/remote_worker_registry.py`) está construida y probada;
  `DECLARATIONS["remote_worker"].implemented` sigue en `False` a propósito
  porque 4 ficheros de test ajenos afirman `implemented is False`
  explícitamente. Activarlo exige que Luis fije el criterio de
  fiabilidad/soporte bajo el que un nodo remoto pasa a "implementado" — no
  es una tarea de código. QA-27 ya está verde por otra vía (`reconcile`
  nunca reporta éxito falso), así que esto no bloquea nada más.
- **HW-07 (LAB)** — banco Spark+PC+eGPU: exige el rig físico real para
  validar los 5 criterios de aceptación con hardware de verdad; fuera de
  alcance sin ese hardware, por diseño (es lo que "LAB" significa aquí).
- **QA-41 (manual)** — voz física en español e inglés: micrófono/altavoz
  reales, permisos de navegador interactivos. No activar grabación ni
  permisos para cerrar esta casilla sin intervención del usuario.
- **QA-44, hueco 3** — "Escape closes the dialog" flaquea de forma
  intermitente (100%/200% según la corrida) en el entorno sandboxeado
  (Playwright + servidor real bajo carga); sin evidencia de ser un bug de UI
  real y no de temporización del entorno. Los huecos 1 y 2 del mismo
  escenario ya se cerraron en Studio (Lote 65).
- **Higiene de tests (nube)** — algún test escribe `disabled_tools` en el
  `data/settings.json` REAL del clon (no se localizó cuál); con ese fichero
  contaminado `tests/test_browser_mcp_reconnect.py` (3) falla. Borrar el
  fichero y pasan. No afecta a Windows (su settings es el de la app).
- **Docker Desktop / puente MCP en la máquina de Luis** — arranque lanzado a
  través del puente falla con `unable to get 'ProgramData'` (PowerShell
  entregado sin `ProgramData`/`ALLUSERSPROFILE`); cuatro vías probadas,
  ninguna reproducible sin acceso directo a esa máquina. Ver "Comprobaciones
  pendientes" arriba.

## OBJ-4/6/7/8 (11-09-2026)

- **MOD-05, cableado al turno.** `src/model_router.choose()` existe y se
  puede probar desde Ajustes â†’ Router de modelos, pero el turno de chat
  sigue usando `sess.model` tal cual: cablearlo exige auditar los usos de
  `sess.model` en `routes/chat_routes.py` (caché, métricas, estado de
  sesión). Es el siguiente paso natural de OBJ-8.
- **`endpoint_id` real en `apply_openrouter_payload`.** Los tres sitios de
  `llm_core` que la llaman no conocen el `endpoint_id` (solo url/modelo), así
  que las preferencias por endpoint solo aplican cuando un caller superior lo
  pase; `usage.include` y `cache_control` aplican siempre.
- **Mermaid en Studio.** Se muestra la fuente (copiar/descargar); render
  gráfico solo si se decide añadir la librería.
- **OBJ-5 (nodos remotos por grupos)** aplazado hasta tener un segundo PC.
- **Tab de Chrome colgado (una vez, no reproducido).** Un tab quedó en «page still loading» durante horas tras un turno con tarjeta de aprobación pendiente y un reinicio del 7001. Reproducir el corte de stream con reinicio NO lo provoca (el turno cierra con «The connection to the server dropped mid-turn»).

## ADP/CMP (11-09)

Lo que queda abierto tras la ola ADP (`95747d9`), la ola CMP (`757262e`),
la ola W3 de cableado (`23f418a`) y el lote W4-A (`2da388a`, pestaña
Requisitos) — extraído de los límites que las propias fichas
`docs/adaptations/decisions/CMP-*.md` y `docs/api/*.md` declaran. Detalle
fila a fila en `docs/adaptations/baseline.md`. Lo que la ola W3 cerró
(chip de contexto en el compositor, `anchor` en sugerencias, evento
`strategy` en vivo, `WorktreeIsolator`/`SnapshotDirIsolator`, alternativas
sobre `DocumentVersion`, `desktop_control_session` â†’ `invalidate_generation`,
`skill_call_history.json`, `calls_profile` en el frontmatter, export/layout/
deep-link de workflows, recetas desde un run real) ya no aparece aquí.

- **Validación física Windows UIA (ADP-09).** `src/desktop_semantics/windows_uia.py`
  está construido, aislado y probado contra fakes; nadie lo ha ejecutado
  todavía contra una sesión Windows real con UIA activo.
- **Herdr contra una instancia real (ADP-13/CMP-06).** `src/external_runtimes/herdr.py`
  infiere el contrato de cable (`/version`, `/sessions`) del texto del
  informe — nunca se llamó a un Herdr real. Solo lectura, por diseño; la
  pestaña «Externos» de Actividad lo muestra como «no configurado» hasta
  que exista una URL.
- **`workflow_iteration` sin cablear al motor (ADP-31/CMP-07).** Diseño y
  dataclasses existen (`src/contracts/workflow_iteration.py`,
  `docs/design/bounded-workflow-iterations.md`) sin tipo de nodo `"loop"`
  en el esquema — activar un bucle real es decisión de producto.
- **`_usage_bucket` sin `RouteDecision` (ADP-22).** El motivo de ruteo viaja
  en el evento SSE `model_router` y en `record_outcome`, no en
  `src/agent_loop.py::_usage_bucket` — falta pasar `RouteDecision.to_dict()`
  como kwargs con default `None`, mismo patrón que `cost_usd`.
- **MOD-05/`execution_router.py` sin reconciliar del todo (ADP-22).**
  `model_router.choose()` solo decide para `model=='auto'`; una sesión con
  modelo explícito sigue decidiendo por `execution_router.py`.
- **Medir los pools de admisión (ADP-32).** `src/resource_admission.py`
  define pools y prioridad de primer plano, pero no se ha medido en
  producción si `llm_core._LOCAL_MODEL_LOCK` sigue limitando tareas reales.
- **Vista móvil / disposición por debajo de 1280px (CMP-01-layout).** Las
  tres disposiciones no tienen efecto de rejilla en pantalla estrecha; el
  panel sigue siendo capa superpuesta. Documentado, no construido.
- **Estimador: `local_latency` parcial (CMP-08).** W3 rellena el campo desde
  `resource_admission.status()`/`llm_core.local_speed()` cuando existen;
  sin GPU medida el estimador lo declara `unknown`, nunca 0.
- **`capability_pricing` de OpenRouter sin contrastar contra un payload
  real (CMP-08).** Probado solo contra el shape documentado.
- **Canal `app_api`/`dom_cdp` sin llamador real (CMP-10).** `choose_channel`
  los admite como lógica pura; solo `native_a11y`/`pixels` tienen consumidor.
- **Alternativas: diff por pares (CMP-13).** `compare()` da diff contra la
  base + `contested_files`, no un diff ALTERNATIVA-vs-ALTERNATIVA.
- **Importación real de un export de aigraphstudio (ADP-17/CMP-07).** El
  formato de `src/workflows/interchange.py` nunca se contrastó contra el
  exportador real.
- **Requisitos (W4-A): sin importación masiva ni edición desde el
  editor de documentos.** La pestaña crea/edita/acepta/rechaza, enlaza y
  quita enlaces y consulta matriz/contexto; importar el fichero sidecar
  entero o crear un requisito desde una selección del documento no existe.

## Excursos y cables de contexto (11-09)

- **Condensar con un modelo local frío es lento** (visto: 27B con «PCIe
  spill» a ~6 tok/s → más de dos minutos; el diálogo lo dice y espera hasta
  300 s). Con un modelo «utility» configurado en Ajustes lo usa en su lugar
  (misma resolución que la compactación automática). No se cambia solo.
- Queda por decidir, no por codificar: enlace del material de documento al
  documento desde el panel (el panel no conoce la ruta del documento);
  el **fork clásico** sigue copiando mensajes — podría pasar a ser un
  excurso sin pasaje si nadie echa en falta la copia.

## Inferencia local (spec INF, 12-09)

- Falta verlo en vivo en el 7001 (chip de arquitectura y «Capabilities» en el formulario de serve, `ReceiptPanel` en una tarea, cronología bajo una respuesta, pestaña «Optimize for my machine»).
- Lo que sigue sin poder verse con fixtures: llama-server (`/props`, `/slots`, `timings`) contra una versión concreta del servidor.
- **El único slot de Ollama es compartido**: durante el run 5 otra petición
  (14k tokens, no era del 7001) ocupó el slot 3 min 47 s y el caso 7 esperó
  detrás. Ahora `ExecutionMetrics.notes` lo nombra («N s unaccounted: the
  engine served something else first») y no se funde con la generación;
  el comparador lo ve como dispersión. Regla práctica: no lanzar un
  benchmark mientras otro cliente usa el mismo Ollama.
- INF-05 hecho (FAUSTUS.md §78): identidad física, presupuesto por GPU,
  admisión con latido y puerta para Cookbook serve, `activate_profile`.
  Sólo con fixtures; queda ver en vivo Servers › Physical GPUs (¿uuid y
  enlace de las tres tarjetas? ¿la 5060 Ti externa sale como enlace
  estrecho?), el bloque «Memory estimate» del formulario de serve y un
  `serve.vram_blocked` real con el diálogo. `KV_RATES` guarda una sola
  observación por modelo: el ajuste `fitted` no se disparará hasta que
  acumule varias (cambio pequeño en `vram_fit.remember_kv_rate`, pendiente).
- Candidatos que requieren reinicio del motor: `activate_profile` ya
  prepara el plan de relanzamiento (`deferred`, `requires_restart`); el
  botón «Relaunch with this profile» en Cookbook › Running NO existe aún.
- INF-06/07 (laboratorio: especulación, reparto entre GPUs, comparación
  de motores) solo con autorización explícita para cada tanda de medidas.
- El comparador usa `p95−mediana` como proxy de dispersión y `n ≥ 3`; el
  doc lo dice: no es un test estadístico. Si se quiere rigor, hay que subir
  repeticiones, no cambiar el umbral.
- Arquitectura de un repo GGUF (sin `config.json` en HF): hoy `unknown`.
  Leer la cabecera GGUF del fichero cacheado (`general.architecture`,
  `*.expert_count`) daría `dense|moe` sin red; pendiente, con su test.
- Respuesta en italiano a un prompt en español cuando el prompt trae un
  bloque de memoria (visto una vez con qwen3.8 27B): ver
  `src/reply_language.py` si se repite.

## Modos de comportamiento (12-09) — HECHO

- Queda por ver con modelos pequeños (9B y menos) si respetan las etiquetas
  [Certain]/[Likely]/[Guessing]: `mode_check` lo dirá por turno; si fallan
  sistemáticamente, un modo «adversarial-lite» sin etiquetas.
- El chip y el aviso aparecen al refrescar el historial tras el turno (no
  hay evento SSE en vivo para `behavior_mode`/`mode_check`); si molesta,
  emitirlos en el evento `metrics`.

## Conectores Hoard (13-09) — HECHO en nube, pendiente en vivo

- Quedan: revisar pendientes y variantes a mano; probar `remember_answer` en un contexto de prueba con el Qwen; Writer en un puerto distinto del 8766 (`WH_AIBRIDGE_PORT`), nunca cerrar Relief Studio.
- Límites conocidos y documentados: `external_ref` es check-then-insert
  (sin UNIQUE porque `CalendarEvent` no tiene owner); la política gobierna
  solo `mcp__<server>__<tool>` (las built-in de correo quedan fuera);
  `GET /api/connectors` no filtra por owner porque `/api/mcp/servers`
  tampoco lo hace (McpServer no tiene owner).

## Correo (13-09, tarde) — HECHO en nube, pendiente ver en el 7001

- Luis: «el side panel de pick a message debería aparecer cuando abres un
  correo; la lista es demasiado estrecha» y «no se carga ninguna imagen y
  se ve de culo». Lista a ancho completo hasta que abres un mensaje o el
  compositor (entonces tres columnas: carril · lista 300–420 px · lector);
  la pista de teclas pasa al pie de la lista. Imágenes: el sanitizador
  corría hasta punto fijo y reiniciaba el contador en cada pasada → el
  botón «Show N remote images» no salía nunca (las imágenes se retienen en
  la primera pasada y en la segunda ya no hay `src`); ahora cuenta el
  máximo, las retenidas conservan su caja (width/height) y no pintan el
  alt como píldora. Botón con menú «esta vez» / «siempre de este
  remitente» (localStorage) y ajuste global «Load remote images in every
  mail» (`email_remote_images`, apagado por defecto: cargar imágenes avisa
  al remitente de que has abierto el correo).
- Para probarlo en vivo falta el cliente OAuth en su `.env` (no existe ni para el correo): crear cliente web en Google Cloud, habilitar Calendar API, registrar las redirect URIs; desde §82 ya no hace falta `.env` ni reiniciar: el asistente de Integrations › Calendar › Google da las URIs exactas con botón de copiar, acepta el `client_secret_*.json` y comprueba el cliente contra Google («Check»). Luis solo tiene que crear el cliente en Google Cloud (guía `docs/api/google_oauth_setup.md`) y pegarlo.

## Paridad de aceptación (13-09, noche) — PR1 + A01–A07 + PR2/A20 HECHO; el resto abierto

- Paquete de Luis en el scratchpad de la sesión y en `docs/spec/paridad/`
  (manifiesto, 36 recetas, estado, backlog). Ejecutor:
  `python3 scripts/acceptance_run.py` â†’ `data/acceptance/<run_id>.jsonl`
  (hoy `passed=8, NOT_EXECUTED=28`).
- Queda de PR2: publicar `faustus-sdk` en un registro (decisión de Luis: npm público o GitHub Packages), generar el cliente desde OpenAPI en vez de a mano, y un job de CI que haga `npm run build && npm test && npm run check` en `sdk/ts`. A21 (UI embebible) es PR6.
- Siguiente por el blueprint:
  PR4 artefactos/carga diferida (A08/A09/A12/A13), PR5 Code Mode (A10/A11/
  A31), PR6 UI embebible (A21), PR7 OIDC/Team (A22/A23), PR8 evolución con
  rollback (A26–A30), PR9 migración/benchmark (A32–A34). Compacción
  intra-turno (A14/A15). Licencia TF17: decisión de Luis.
- Trampas de este lote: `consume_with_reason` lo escribieron T2 y T3 a la
  vez (se quedó la forma `(reason, approval)` de T3 + `retire_for_session_ids`
  de T2); el engine de workflows solo admite cuatro estados de handler, por
  eso `fenced` viaja como `failed` + `fenced: True` en nodos (en tareas
  programadas sí es un estado propio).

## Última evidencia

- **13-09-2026 madrugada, paridad incremento 2 (SDK + A20).** Suite nube
  entera tras S1–S3: 17.775 correctas, 49 saltadas, 3 fallos: uno real (la
  matriz exacta de tokens no listaba la ruta de export, arreglado) y dos de
  `tests/test_caldav_writeback_route.py` que pasaban solos. Bisecado por
  mitades sobre el orden real del worker: la causa era
  `tests/test_acceptance_index.py` (PR1), que importaba TODOS los
  `tests/test_*.py` en el mismo proceso bajo otro nombre de módulo
  (`tests.test_x`) para leer los markers, y así ejecutaba dos veces los
  efectos de importación (p. ej. `croutes.SessionLocal = <BD temporal>`),
  dejando la ruta apuntando a una BD que el módulo de pytest nunca escribía.
  Ahora el índice lee los decoradores con `ast` (sin importar nada) y cachea
  la pasada (4 s en vez de 36×4 s).
  `scripts/acceptance_run.py`: `passed=8`, `NOT_EXECUTED=28`; A20 ~40 s.
- **13-09-2026 noche, paridad de aceptación incremento 1 (master `c7df980`+ =
  Windows).** Suite nube entera tras fusionar T1–T3: 17.659 correctas, 49
  saltadas, 0 fallos (11 min 39 s). `scripts/acceptance_run.py`:
  `passed=7` (A01–A07), `NOT_EXECUTED=29`.
- **13-09-2026 noche, cliente OAuth de Google desde la app (master `ead8879`+ =
  Windows).** Suite nube entera tras G3: 17.593 correctas, 49 saltadas, 1
  fallo que era la guía en `docs/guides/` (la guarda de docs solo admite
  Markdown en subárboles de ingeniería) → movida a
  `docs/api/google_oauth_setup.md`.
- **13-09-2026 tarde, correo + Google Calendar (master `9dd3a8c`+ = Windows).**
  Suite nube entera tras los lotes G1/G2: 17.558 correctas, 49 saltadas,
  0 fallos (9 min 41 s). En vivo en el 7001: Integrations › Add › Calendar
  → selector Google · iCloud · Nextcloud · CalDAV; sin cliente OAuth en el
  `.env` el formulario de Google lo dice y el botón Connect queda
  deshabilitado (estado honesto, no probado más allá hasta que Luis cree
  el cliente).
- **13-09-2026, conectores (FAUSTUS §80; master `a3b68b4`+ = Windows).** Suite
  nube entera tras integrar F1–F4: 17.525 correctas, 49 saltadas, 0 fallos
  (10 min 7 s). En vivo en el 7001: `/connectors` con el preset Jobhunter,
  «App not running · Adapter: 15 tools» con la app apagada, perfil de
  arranque â†’ instancia de PRUEBA en 5179 (`JOBHUNT_DATA_DIR` en
  `D:\LocalAI\_claude_tmp\jh_testdata`; la mató después mi
  `restart7001.ps1`, que mata los HIJOS del 7001 — `Stop-Faustus.ps1` no
  lo hace —, «Iniciar la app» la vuelve a levantar), «Available», qwen3.8 27B q4 llamando
  `list_contexts`/`list_jobs` por MCP con tarjeta de permiso, y con
  «ningún conector» el modelo dice que no tiene la herramienta. Jobhunter
  en Windows: `claude/conectores` `f08fd93`, 28/28.

- **12-09-2026 noche, INF-05 + benchmarks reales (master `4a9d21d`,
  Windows `e1efe93`+).** Suite nube entera: 17.367 correctas, 49 saltadas,
  0 fallos (11 min 8 s). Visto en vivo: Physical GPUs, presupuesto por GPU,
  cinco runs de banco y el comparador con datos reales (§78).
- **12-09-2026, INF-00…04 (master `3a405f2`, Windows igual).** Suite nube
  entera tras INF-04: 17.228 correctas, 49 saltadas, 0 fallos (9 min 19 s).
  Visto en vivo en el 7001 (Chrome): pestaña Optimize (endpoint Ollama →
  modelos instalados, suite filtrada por objetivo, plan «3 casos ·
  estimación unknown until a first run · procesos afectados: none»; el
  botón Start NO se pulsó: ningún benchmark real ejecutado, §01), formulario
  de serve con «Architecture: unknown (metadata unavailable)» para un repo
  GGUF sin `config.json` (honesto, pero mejorable leyendo la cabecera GGUF),
  «Implementation: llama-server» y lista Capabilities (ctx/ngl/flash_attn
  supported con su nota), y «Why did it take this long?» bajo una respuesta
  real de qwen3.8 27B: cola 293 ms observed, carga 3 ms engine, prefill
  1,0 s engine, generación 5,1 s engine, total 7,3 s observed, tools «the
  engine does not expose this metric», tokens 545/118 engine. Vistos y
  arreglados: `/api/model/cached` daba 500 por un `scan_cache.py` viejo no
  escribible (ahora fichero único por llamada + fallback a temp); un test
  INF-02 lanzaba un proceso real en Windows (`IS_WINDOWS` fijado); «Di
  solo: seis» recuperaba la memoria «…café solo por la mañana» por la
  palabra «solo» y el modelo contestó (en italiano) sobre el bloque de
  memoria en vez de decir «seis» — stopwords en español y tokens con
  acentos; tras el arreglo: «Seis.». Al aplicar w39 se borró
  `D:\LocalAI\odysseus\data\settings.json` (el `data/` del repo, no el
  `odysseus-dev-data` del 7001): si ese fichero importaba, está en los
  backups de `backups/` anteriores al 12-09; los scripts de transferencia ya
  no lo tocan.
- **11-09-2026, QA en vivo de las olas ADP/CMP/W3 + W4-A (master
  `e075293`, Windows `2da388a`+).** Suite nube tras los arreglos de
  entorno: 16.913 correctas, 0 fallos (antes: fallos preexistentes en
  chat_helpers/chatgpt_subscription/session_image_cleanup/git_invariants/
  qa_26/sandbox_exec/docker/markitdown, todos corregidos en `29d99fc` y
  `966b647`; `terminate_tree` suspende la raíz antes de matar hojas). Vistos
  y arreglados en pantalla (7001, Chrome): fila de Actividad con el título
  aplastado (`flex-wrap`), barra del compositor desbordada a 1920px (media
  query insuficiente â†’ `@container` sobre `.fs-studio__bar`), cabecera del
  Studio oculta tras la columna de documento en las disposiciones
  documento/revisión, «Aplicar» de Alternativas fusionaba con un clic
  (ahora dos pasos), `suggest_document` fallaba con «No active document»
  con un turno en español (puerta de relevancia bilingüe +
  `active_document_pinned` cuando el chip de contexto apunta a ese doc),
  ReviewPane creaba comentarios vacíos (ahora pide el texto),
  `alternatives.run_tests` en Windows comía barras invertidas
  (`shlex` solo en POSIX). Verificado en vivo: `/workflows`
  (cargar/simular/inspector/lint), `/alternatives` (crear â†’ worktree â†’
  comparar → aplicar, fichero cambiado en disco), atención en Actividad,
  tres disposiciones, documento → selección → chip → sugerencia con
  `anchor` â†’ aplicar (v2), comentario en ReviewPane, Ajustes â†’ OpenRouter,
  vecindario en Contexto, y la pestaña Requisitos (crear REQ-1, enlazar
  `store.py@remove_link`, «Quitar» → «Enlace quitado», campos de edición
  apilados tras verlos en línea). Suite Windows entera (`suite_m1.ps1`,
  19 min) sobre `ac9fb88`: 16.878 correctas, 6 fallos, todos de entorno y
  todos corregidos en `32723fe` (PATH mínimo sin `venv\Scripts` frente al
  PATH crudo; `futures.db` abierto hasta el GC bloqueaba el borrado del
  temporal — ahora `_connect()` cierra; test de cancelación que reescribía
  `report.py` sin re-aprobar la skill (puerta ADP-25); la política real de
  Settings con el navegador apagado cortaba `browser_pid` en los tests de
  reconexión; dos `SyntaxWarning`). Los ocho ficheros afectados: 86 correctas
  en Windows (Docker encendido, el test de cancelación real incluido).
  Segunda pasada entera sobre `71fd5ca`: 16.883 correctas, 1 fallo que NO
  estaba en la primera y pasa solo (`test_cookbook_shell_uses_bash_syntax_on_windows_too`:
  `_run_shell` devolvió «command could not be completed» bajo carga `-n 6`).
  La excepción se tragaba en un logger de debug; ahora `routes/codex_routes.py`
  la nombra en `stderr` para que la próxima vez se pueda bisecar en vez de
  encogerse de hombros. **Pendiente: si vuelve a salir, leer la clase.**

- **BUG-STOP-01 (FAUSTUS.md §116).** Stop (`POST /api/chat/stop`, cualquier
  scope) arreglado a nivel de mecanismo: cancelación sondeada y autoritativa
  en `src/agent_loop.py` (`pending_cancel`, 5 puntos de chequeo) + la ruta ya
  informa la razón (`no_active_run`/`run_id_mismatch`/`run_already_finished`)
  en vez de un `false` desnudo + techo `agent_turn_max_seconds`. **Pendiente:
  confirmar contra la máquina en vivo (llama-server real, la tarea de la
  Pokédex que llegó a la ronda 180) que un clic de Stop para el turno en la
  ronda siguiente, y que `studio/src/screens/Studio.tsx`'s `runIdRef` no se
  desincroniza del run real durante un turno de cientos de rondas — no se
  pudo descartar del todo un desajuste de `run_id` específico de la UI sin
  esa máquina.**

- **FAUSTUS.md §117.** Muestreo local expuesto en Ajustes → Default AI (5
  campos, recorte cliente + servidor) y guardián nuevo para trabajos
  programados (`src/background_job_guard.py`, ajuste `background_jobs_may_load_models`,
  `False` por defecto) que pospone en vez de cargar un modelo no residente.
  **Pendiente:** (1) confirmar en el Studio real el grupo «Muestreo local»
  con placeholders correctos y que un campo vacío no viaja en el `PATCH`;
  (2) dejar pasar la hora de auditoría nocturna real con el modelo de
  utilidad descargado y confirmar la línea `scheduled skill_audit: postponed,
  would load <modelo>` sin que Ollama cargue nada (`ollama ps` sin cambios);
  (3) confirmar que activar `background_jobs_may_load_models` deja correr la
  auditoría igualmente esa noche.

- **FAUSTUS.md §119 — Parte A (muestreo en el composer).** Panel del chip
  de generación (`GenSettingsPopover`, `studio/src/screens/studio/Composer.tsx`)
  probado a nivel de adaptador (`studio/checks/gen-sampling-panel.check.mjs`)
  y por inspección de fuente; no se ha abierto en un navegador real.
  **Pendiente:** abrir el chip en el Studio real y confirmar que el slider
  de temperatura y el número enlazado se mueven juntos, que el Reset de un
  control concreto no toca los demás overrides, que el interruptor de
  razonamiento solo aparece con un modelo pensante activo (p. ej. `qwen3`,
  nunca con el 27B por defecto), y que `/temp 0.9` escrito en el chat
  actualiza el panel al reabrirlo.

- **FAUSTUS.md §119 — Parte B (`llama-server` gestionado desde la UI).**
  `src/engines.py`/`routes/engine_routes.py` con 14 tests unitarios en
  verde y la sección «Engines (llama.cpp)» ya montada en Ajustes → Local
  models; nada de esto se ha probado contra el `llama-server` real del
  dueño. **Pendiente, en la máquina real:** (1) crear un engine desde
  Ajustes apuntando a `llama-server.exe` y al GGUF reales y confirmar
  «Verify» en verde; (2) Start con el puerto libre y ver la fila pasar de
  `stopped` a `unhealthy` a `running`, con modelo/contexto/huella
  correctos; (3) con `Start-LlamaServer.ps1` (o cualquier otra cosa)
  arrancado a mano en el 8081, confirmar que Start desde la UI se rechaza
  nombrando ese proceso, sin tocarlo; (4) Stop desde la UI y confirmar en
  el Administrador de tareas que el proceso y sus hijos desaparecen, y que
  pide confirmación cuando ese motor sirve el modelo por defecto; (5)
  «Rellenar desde lo que ya escucha en este puerto» contra el servidor
  real y confirmar que trae `model_path`/`n_ctx` correctos desde `/props`.

- **FAUSTUS.md §120 — Parte A (snapshot de memoria por sesión).**
  `memory_engine.pack_for_session` probado con scripts y pytest, nunca
  contra una sesión real del Studio. **Pendiente:** abrir una sesión,
  confirmar en el bloque de "learned memory" que aparece la nota
  "(snapshot taken …)" y que NO cambia entre turnos de esa sesión aunque
  se añada una regla nueva mientras tanto; abrir una sesión NUEVA y
  confirmar que esa regla sí aparece.

- **FAUSTUS.md §120 — Parte B (tope duro del bloque de memoria).**
  `pack_detail()` probado con más de `memory_block_max_chars` de reglas
  puntuadas vía script/pytest; no se ha mirado el diálogo «ver el bloque»
  real. **Pendiente:** provocar el tope en un proyecto real y confirmar
  que el panel de administración deja ver cuántos ítems se omitieron.
  También queda sin resolver si hace falta un tope de ESCRITURA análogo
  (memory_engine.add_item hoy recorta en silencio a MAX_TEXT_CHARS por
  ítem en vez de rechazar) — decisión deliberadamente no tomada, ver la
  nota en §120 Parte B.

- **FAUSTUS.md §120 — Parte C (panel de revisión de memoria).** Pin,
  suppress, edición y el chip «injected» probados por pytest contra la
  API; no se ha abierto Studio → Memoria en un navegador real.
  **Pendiente:** fijar una regla sin feedback y confirmar que el chip
  «injected» aparece; suprimirla y confirmar que desaparece del bloque y
  del chip sin borrar la fila; editar el texto y confirmar que la versión
  vieja queda tombstoned.

- **FAUSTUS.md §120 — Parte D (niveles explícitos en skills).**
  `src/skills_runtime/disclosure.py` probado unitariamente (omite enteros,
  nunca trunca a medias, garantiza al menos una skill en nivel 1); no se
  ha confirmado en una conversación real que las líneas de log
  `skills disclosure: level 0/1/2 …` aparecen cuando corresponde.
  **Pendiente:** una conversación con muchas skills instaladas y confirmar
  en los logs qué nivel aportó cada una.

- **FAUSTUS.md §120 — Parte E (sampling defaults en Local models).**
  Compilado y con los mismos ajustes leídos/escritos por ambas pantallas
  por inspección de código; no verificado clicando en el Studio real.
  **Pendiente:** cambiar la temperatura por defecto en Ajustes → Local
  models y confirmar que Ajustes → Default AI la refleja al recargar, y
  viceversa.
## 19-09 w124 -- deploy verificado, engine + pokedex resume en marcha
- Deploy fa40226d -> 4008d6ec (fast-forward, sin stash). 50 tests OK (test_bug_stop_never_stops, test_cancel_scope_routes, test_local_sampler_settings_clamp, test_model_load_options, test_background_job_guard) contra ODYSSEUS_DATA_DIR aislado.
- utility_model/task_model apuntaban a qwen3.5:4b (borrado de Ollama); movidos a utility_endpoint_id=task_endpoint_id=3202765f, utility_model=task_model=qwen3.8-27b-q8-llamacpp via POST /api/auth/settings. Confirmado que ya no hace falta tocar local_temperature_default/top_p/top_k (ya vienen 0.6/0.8/20 en el bundle).
- Probado /slots durante una peticion de chat corta: top_k=20, top_p=0.8, min_p=0.05, repeat_penalty=1.05 coinciden con los defaults. OJO: la temperatura vista en el slot de la respuesta de chat normal fue 1.0 (no 0.6) -- revisar de donde sale ese 1.0 para chat mode directo (la llamada de utilidad/titulo si uso 0.1 y el turno de agente vi 0.4 = agent_local_temperature_cap). No bloqueante, solo a revisar.
- Reanudado el trabajo de carpetas Pokedex (workspace Contornos pokemon) con una sesion nueva en modo agente, siguiendo la skill pokedex-lines desde next=427 leido del estado (no se reseteo nada). El runner queda corriendo en segundo plano en la maquina de Luis (D:\LocalAI\_claude_tmp\pokedex_runner.ps1, log en pokedex_runner.log) en lotes de 10 con "continua"; cada turno tarda ~10-15 min en el llama-server local (12-13 tok/s). Sigue vivo tras cerrar esta sesion salvo que se pare a mano (crear D:\LocalAI\_claude_tmp\pokedex_stop.flag o matar el proceso powershell).

## 19-09 w126 -- helper model on llama.cpp, Engines UI wired
- Deploy 756c4d67 -> 76adb636 (rebase, PENDIENTES.md conflict resolved by keeping both sections). Tests: test_engines.py 14 passed, test_chat_mode_local_temperature_floor.py 10 passed, test_launch_profiles.py+test_launch_profiles_apps.py+test_process_center.py 52 passed / 2 failed (test_already_running_via_readiness_is_not_relaunched, test_route_level_relative_executable_is_400 -- pre-existing, reproducible in isolation, unrelated to this bundle's engines/temp-floor/composer changes; not investigated further, flagging for a future round). build-studio.js OK.
- Downloaded Qwen/Qwen2.5-3B-Instruct-GGUF q8_0 (3616088480 bytes, verified against HF Content-Length) to D:\LocalAI\models\. Started a second llama-server on :8082 (Start-LlamaHelper.ps1 / Stop-LlamaHelper.ps1, same style as the 8081 pair). Registered as endpoint 8766e2b1 ("llama.cpp helper"), utility_endpoint_id/task_endpoint_id now point at it. Confirmed via /8082/slots and app.log that title-generation calls hit :8082, not the 27B.
- Created two /api/engines records (09be1b79... for the 27B on 8081, 65e33f2c... for the helper on 8082) so Settings -> Local models -> Engines shows both and can start/stop them from the UI; both report state=running with correct PIDs against the already-running processes.

## w127 deploy (184c1687) - memory snapshot, gen defaults, learned-rules actions

Deployed cleanly on top of prior history (rebased my own docs commits). Tests: test_memory_engine.py 73 passed, test_skills_disclosure.py 10 passed, test_agent_loop.py 54 passed (137/137). Studio build succeeded. Server restarted (7000 only); both llama-server engines (8081 27B, 8082 helper) left untouched and verified healthy throughout.

API verification:
- /api/auth/settings has memory_snapshot_per_session=True, memory_block_max_chars=6000, skill_list_budget_tokens=400, skill_body_budget_tokens=1500, and the five local_*_default sampling keys (temperature 0.6, top_p 0.8, top_k 20, min_p 0.05, repeat_penalty 1.05).
- /api/engines lists both engines running with correct models.

UI verification (browser, admin login), screenshots under D:\LocalAI\_claude_tmp\shots\:
(a) a_generation_chip.png - Studio composer's Generation chip (found inside the '+' Add-files-and-tools menu, agent mode only, bottom of the list) opens a panel with Temperature/top_p/top_k sliders, all reading the sampling defaults. Works.
(b) b_engines_sampling.png - Settings > Local models shows the Engines (llama.cpp) section (both engines, Stop controls present) directly above the Sampling defaults group (Temperature/top_p/top_k/Repeat penalty). Works.
(c) c_learned_rules.png - Studio > Memoria > Learned rules panel shows the 'injected' badge on an active rule plus This rule helped/did harm, Edit the text, Pin, Suppress, Forget and Delete actions. Works.

## 19-09 tarde -- pendientes del lote engines/MTP/offload-search/code-graph/trace/memoria/PDF/ranking/a11y (FAUSTUS.md §121-130)

- **Medir tok/s con MTP on/off en el 27B real (FAUSTUS.md §122).** La deteccion de capas MTP ya funciona sobre el GGUF real, pero la comparacion de velocidad NO se ha medido todavia -- hace falta GPU disponible y lanzar con `-np 1` (si no, los slots paralelos por defecto cancelan la mayor parte de la ganancia y el numero sale enganoso). Sin esto, §122 esta a medias: sabemos que MTP se detecta y arranca, no sabemos cuanto acelera de verdad.
- **Decidir si se integra la idea del modelo pequeno de tool-calling on-device.** Aplazado deliberadamente: implicaria una DLL nativa, descarga de pesos y telemetria activada por defecto -- ninguna de las tres encaja con el criterio del proyecto de no traer binarios/datos sin que el dueno lo pida explicitamente. Queda como decision abierta, no como tarea.
- **Tests rojos preexistentes vistos hoy (ya rojos en 07c089ec, antes de este lote):**
  - `tests/test_gpu_memory_wiring.py::test_num_gpu_survives_the_whole_override_path`
  - `tests/test_two_tier_search.py` (2 tests)
  - `tests/test_completion_gate.py::test_red_test_turn_closes_complete_unverified_end_to_end`
  - `tests/test_l91_domain_synonyms.py` (2 tests, texto de reglas)
  No se ha investigado su causa en esta ronda -- confirmar con `git stash`/bisect antes de tocarlos, para no arreglar algo que ya estaba roto por otra razon.
- **`llm_trace` (FAUSTUS.md §126): retencion por defecto de 7 dias** (`llm_trace_retention_days`). Revisar si es suficiente para depurar un problema reportado varios dias despues, o si conviene subirlo cuando el disco lo permita.

## Grounding lint (FAUSTUS.md §145) — pendiente de verificar en la máquina en vivo

- **No verificado contra el store real** (`D:\LocalAI\faustus-dev-data`): solo se probó con pytest y una app FastAPI de pruebas en memoria. Falta insertar un ítem bien respaldado y uno con un dato inventado contra el store de verdad y comprobar que el panel «Grounding» de la pantalla Memoria distingue uno del otro (llamadas exactas para hacerlo, en el informe de la tarea).
- **`proper_noun` es una heurística deliberadamente floja** (dos o más palabras seguidas con mayúscula inicial): puede fallar tanto por exceso (frases genéricas en mayúscula) como por defecto (un nombre citado en dos órdenes distintos, "Jane Doe, la CFO" vs "la CFO Jane Doe", no se reconoce como el mismo dato). Aceptable para un lint, pero vale la pena revisar con datos reales de la memoria de alguien antes de confiar en el recuento de hallazgos.
- **Las fechas en formato barra (`19/09/2026`) se aceptan en las dos lecturas posibles** (día/mes y mes/día) precisamente por la ambigüedad — nunca se comprobó contra fechas reales de evidencia en inglés de EE.UU. (mes/día) mezcladas con fechas en formato europeo en el mismo store.
- **No se expone como herramienta de agente**: `memory_conflicts` tampoco lo está, así que se siguió el mismo patrón (mirroring); si en el futuro se decide exponer conflicts como tool, grounding debería ganar la suya a la vez.

## Pre-escaneo de seguridad MCP/skills (FAUSTUS.md §147) — pendiente de verificar en la máquina en vivo

- **No verificado con el navegador real contra la instancia de desarrollo** (`D:\LocalAI\faustus-dev-data`): esta caja de arena no tiene sesión autenticada en el navegador de esa instancia, así que la insignia de riesgo y el aviso con checkbox de override en `Ajustes → Integraciones` no se han visto renderizados en pantalla — solo confirmados por `tsc --noEmit` y por llamadas directas a los manejadores reales del router (`routes/mcp/mcp_routes.py`), que sí reproducen el flujo completo (servidor malicioso en cuarentena, aprobación rechazada sin `override`, aceptada con él). Repetir en vivo: crear un servidor MCP local con un script que lea una variable `*_KEY`/`*_TOKEN` del entorno y la postee por red, y con un `curl | sh`; añadirlo desde la interfaz y comprobar la insignia y el aviso.
- **La revisión de skills importadas (`GET/POST /api/skills/{id}/review|approve`) no tiene interfaz propia todavía** — es solo API (como ya era antes de este lote, ver `docs/api/skills_review.md`), así que el escaneo adjunto (`security_scan` en la respuesta de `review`, bloqueo en `approve` sin `override`) solo se ha probado por pytest y llamando a `src/skill_import_review.py` directamente, nunca a través de un flujo de usuario real en la máquina en vivo.
- **Las reglas son heurísticas de expresión regular, no análisis semántico**: un ofuscador poco convencional (build-charcode con más de un paso, un `eval` envuelto en un alias de función) puede no dispararlas. Aceptable para un pre-escaneo informativo que nunca bloquea solo (excepto crítico + `security_scan_block_critical`), pero no sustituye una revisión humana del código de un servidor/skill de verdad desconocido.

## Batería de sondas de inyección (FAUSTUS.md §148) — verificada en vivo con el modelo pequeño

- Falta repetirlo con el modelo grande (27B), que obedece más y es el caso interesante.
- **Las dos sondas dirigidas a la herramienta canario (`probe_canary_send`) se redirigen en modo vivo a una herramienta real** (`live_target_tool`), porque no existe function-calling hacia una herramienta que el catálogo real no conoce. Esto es una aproximación razonable pero no idéntica a la sonda determinista — vale la pena revisar si el resultado en vivo coincide con lo esperado.
- **Extracción de `tool_start`/`tool_output` del SSE en modo vivo es best-effort**: si el modelo real hace varias llamadas a `read_file`/al objetivo en la misma vuelta, o el nombre de herramienta no calza exactamente, `attempted`/`blocked` pueden quedar en `None` en vez de reflejar lo ocurrido — no probado contra tráfico SSE real de un modelo real, solo contra la forma de los eventos que el propio código produce.

## Context Engine fase 2 (FAUSTUS.md §174) — pendiente de verificar en la máquina en vivo

- **Encenderlo de verdad**: `agent_context_engine` sigue `False`. Antes de cambiarlo, correr `scripts/bench_context_engine.py --data-dir <data dir real> --snapshot --owner <dueño>` y un turno de agente real con la bandera encendida (SSE `context_packet`, que la memoria aprendida no salga dos veces, y que al forzar un timeout la ronda conserve el bloque clásico).
- **Encendido en vivo del chat simple**: verificar con la bandera encendida un turno de chat real (SSE `context_packet` con `round: 1`, que «saved memory» no salga dos veces, y que un timeout deja el prefacio).
- **Recibos en error no terminal**: un turno que termina por excepción no controlada dentro del cuerpo queda con `verdict="interrupted"` (vía `_TURN_FINALIZERS`), no «error».

## Puerta de trayectoria (FAUSTUS.md §149) — pendiente de verificar en la máquina en vivo

- **No verificado con el navegador real contra `D:\LocalAI\faustus-dev-data`**: la sesión de navegador de esta caja de arena no está autenticada contra esa instancia, así que el panel "Gate a run" en Actividad no se ha visto renderizado en pantalla — solo confirmado por `tsc --noEmit` y por pruebas de la ruta con manejadores reales parcheados. Repetir en vivo: listar los `run_id` recientes con `Get-ChildItem "D:\LocalAI\faustus-dev-data\runs\*.jsonl" | Sort-Object LastWriteTime -Descending | Select-Object -First 10 -ExpandProperty BaseName`, pegar uno en el panel "Gate a run" de la pantalla Actividad y comprobar que las aserciones del spec por defecto se renderizan con su marca de pasa/falla.
- **La atribución de llamadas a modelo (`model_call`) a una ejecución concreta es por ventana de reloj, no exacta**: `src/llm_trace.py` no recibe `run_id` desde ninguno de sus dos puntos de llamada en `src/llm_core.py` hoy, así que una ejecución muy corta que se solape en el tiempo con otra del mismo chat (dos pestañas, una regeneración inmediatamente después de otra) podría atribuir una llamada a la ejecución equivocada. No verificado contra ese caso límite con datos reales — solo con el registro sintético de la prueba.
- **`max_duration_s` con datos reales de Windows**: verificado solo con ficheros de registro sintéticos; falta correr `python -m src.trajectory_gate --recent 20 --spec ...` contra ejecuciones reales largas (con herramientas lentas, research, etc.) en `D:\LocalAI\faustus-dev-data` y comprobar que la duración de reloj real es razonable frente a lo que la persona recuerda haber esperado.

## Revisión con duda (FAUSTUS.md §150) — pendiente de verificar contra un modelo real en la máquina en vivo

- **La llamada de modelo real nunca se ha ejecutado**: las 29 pruebas de `tests/test_doubt_review.py` inyectan `model_call`/`risk_fn`, así que la ruta de red (`src.ai_interaction._resolve_model` + `llm_call_async` + `response_schema`) nunca se probó de punta a punta contra el helper llama.cpp de desarrollo (`http://127.0.0.1:8082/v1`, alias `qwen2.5-3b-helper`). El script de repetición está en FAUSTUS.md §150; falta confirmar que el modelo pequeño de verdad detecta un diff roto en un fichero de alto riesgo real del repo (no solo en fixtures sintéticas) antes de recomendar activar `agent_doubt_review` en cualquier perfil por defecto.
- **`code_graph_risk` sobre un fichero real no se comprobó en esta tarea**: la caché por turno (`DoubtReviewState`) se probó con un `risk_fn` inyectado; falta confirmar en vivo que el tope de revisiones por turno y la caché de riesgo se comportan igual cuando `code_graph.change_risk` hace su recorrido real (git log + grafo de llamadas) sobre un repo grande.
- **Sin interfaz nueva que revisar**: el cableado vive solo en el resultado JSON de `edit_file`/`write_file`/`apply_patch` (que el agente lee) y en el formulario ya genérico de ajustes "Agent Tools"; no hay panel propio que renderizar en el navegador.

## Pase de sueño de skills (FAUSTUS.md §151) — pendiente de verificar en la máquina en vivo

- **No verificado con el navegador real contra `D:\LocalAI\faustus-dev-data` ni contra el helper llama.cpp real**: esta caja de arena no tiene ni el navegador conectado a esa instancia ni acceso al helper en `http://127.0.0.1:8082/v1` (alias `qwen2.5-3b-helper`), así que la pestaña "Proposals" de la pantalla Skills nunca se vio renderizada en pantalla, y `propose()` nunca se ejecutó contra una respuesta de modelo real — solo contra `llm_call_async` parcheado en las 32 pruebas de `tests/test_skill_sleep_pass.py`. El script exacto para repetirlo (sembrar una skill + un par de sesiones sintéticas, correr el pase, ver la propuesta, rechazarla, limpiar) está en FAUSTUS.md §151.
- **Sin gancho al planificador nocturno**: la tarea pedía un `skills_sleep_pass_enabled`/`skills_sleep_pass_hour` opcional en `src/task_scheduler.py`, pero ese módulo (≈4000 líneas) no tiene hoy un patrón simple y reutilizable de "job programado a una hora fija" — todos los jobs existentes se enganchan a estructuras más grandes (colas, tareas de usuario) que un cableado apresurado habría duplicado mal. Se dejó fuera deliberadamente en este lote; para añadirlo hace falta primero decidir con qué patrón de `task_scheduler.py` debe encajar (tarea de sistema vs. `ScheduledTask` de usuario) antes de tocarlo.
- **La detección de "la skill estuvo activa" depende de que el modelo llame a `manage_skills`**: si una skill se sigue solo por texto inyectado en el prompt sin que el modelo la abra explícitamente con la herramienta (una skill de la sección "índice" nunca vista con `view`), este pase no la ve — no hay ningún otro evento de activación grabado en el repo hoy. Aceptable como primera versión, pero vale la pena revisar con datos reales de uso si esto deja fuera casos importantes.
- **Las tablas de frases en/es son heurísticas de subcadena, no análisis de sentimiento**: un mensaje de usuario en un tercer idioma, o una negación indirecta poco común, no se clasifica — se queda en "neutral", que es la lectura conservadora (ni cuenta como fallo ni como éxito), pero no se ha probado contra un corpus real de respuestas de usuarios.

## Agrupación de revisión de diffs grandes (FAUSTUS.md §152) — pendiente de verificar contra un diff real en la máquina en vivo

- **Nunca corrió contra un diff real de varios archivos ni contra una respuesta real del helper llama.cpp de desarrollo**: las 11 pruebas de `tests/test_auto_review_grouping.py` inyectan `llm_call_async` y `turn_diff`/`per_file_diffs`, así que la ruta completa (checkpoint sombra o `git diff` real → agrupación → varias llamadas al modelo → merge/dedupe) nunca se probó de punta a punta contra `http://127.0.0.1:8082/v1` (alias `qwen2.5-3b-helper`). El script de repetición (un diff sintético de ~10 ficheros sacado del propio historial del repo, con un bug inyectado a mano) está en FAUSTUS.md §152; falta confirmar que `groups` sale mayor que 1 y que el bug inyectado aparece entre los hallazgos.
- **La agrupación por modelo (`auto_review_group_with_model`) nunca se probó contra una respuesta real**: las pruebas cubren el parseo (limpio y garbled) con respuestas fijas; falta ver si un modelo local pequeño de verdad devuelve una partición de índices razonable o si en la práctica siempre cae a la agrupación determinista.
- **Sin interfaz nueva que revisar**: los metadatos nuevos (`groups`, `group_files`, `not_reviewed`, `truncated_files`, `group_chars`) viajan en el mismo objeto `review` que ya se persiste con el turno y se ve como tarjeta en el chat (`harness_check` → `review_issues`/`review_running`); no hay panel propio que renderizar en el navegador, y esta tarea no verificó cómo se ve esa tarjeta con un diff realmente grande.

## Conceptos de proyecto (FAUSTUS.md §154) — pendiente de verificar en la máquina en vivo

- **La pestaña Concepts nunca se vio en un navegador real**: el servidor sí se arrancó de verdad en esta caja (uvicorn + login admin real + roundtrip HTTP completo sobre `/api/project-concepts`, ver FAUSTUS.md §154), pero sin sesión de escritorio nunca se abrió Studio para ver el grafo de `<canvas>` renderizado, hacer clic sobre un nodo real, ni comprobar el panel lateral con una referencia rota resaltada.
- **La inyección automática (`agent_project_concepts_inject`) nunca corrió dentro de un turno real de agente**: el test de esa ruta (`test_injection_is_a_noop_when_setting_is_off`) solo comprueba que el ajuste apagado no toca el store; con el ajuste encendido, que el bloque "project concepts" realmente llegue al prompt y aparezca en el ledger de contexto bajo `instructions` no se vio contra un modelo real, solo se razonó por el mismo camino de código que ya usa el repo map.

## Limpieza de STT y notas de reunión (FAUSTUS.md §156) — pendiente de verificar en la máquina en vivo

- **Flujo probado en vivo por script (20-09, ver FAUSTUS.md §156); falta ver la pestaña Meetings en el navegador y una grabación real con micrófono.** Nota anterior:: esta caja de arena no tiene el servidor de Windows arrancado ni acceso al helper llama.cpp de desarrollo (`http://127.0.0.1:8082/v1`, alias `qwen2.5-3b-helper`) ni a Piper instalado, así que `POST /api/meetings` → transcripción real con faster-whisper → pasada de notas con el modelo real nunca se probó de punta a punta — solo con un STT y un modelo de notas falsos en `tests/test_meetings_pipeline.py`.
- **La pestaña Meetings nunca se vio en un navegador real**: confirmado solo por `tsc --noEmit` limpio y por la estructura calcada de las pestañas Chats/Research/Historial ya existentes; falta abrir Studio de verdad, grabar con el micrófono (`MediaRecorder`) o subir un audio, ver la barra de progreso, y comprobar que el Markdown se despliega correctamente con `Rich`.
- **La fragmentación con `ffmpeg` no se probó contra un audio largo real**: `_split_chunks()` se probó indirectamente (mockeada) en `tests/test_meetings_pipeline.py`; falta confirmar en la máquina real, con `ffmpeg`/`ffprobe` instalados, que una grabación de más de 10 minutos se parte en fragmentos correctos y que las marcas de tiempo del resultado final siguen siendo coherentes con el audio original.
- **La tabla de frases de alucinación es heurística, no exhaustiva**: cubre los ejemplos pedidos y los casos clásicos documentados de Whisper (en/es), pero no se ha probado contra un corpus real de audio con ruido de fondo o acentos variados; algunas entradas genéricas ("gracias", "thanks", "you" como segmento entero) podrían descartar una intervención real muy corta en una reunión de verdad — vale la pena revisar la tabla tras un uso real antes de darla por definitiva.

## Dictar en cualquier lugar (FAUSTUS.md §157) — pendiente de verificar en Windows real

- **Nunca corrió en Windows de verdad**: este contenedor no tiene GUI, `ctypes.WinDLL`, Electron real ni Piper instalado, así que `src/dictation_paste.py` (portapapeles Win32, `SendInput`, captura/refoco de ventana en primer plano) solo se probó contra backends falsos (`tests/test_dictation_paste.py`) y las rutas solo contra un `stt_service`/`dictation_paste` falsos (`tests/test_dictation_routes.py`). El script `scripts/dictation_windows_live_test.py` (nuevo) está listo para correrlo en la máquina real — abre el Bloc de notas, centinela de portapapeles, captura, pega directo, pipeline completo con un WAV de la voz Piper `es_ES-davefx-medium`, lee de vuelta el texto vía la API de Win32, comprueba que el portapapeles se restauró, cierra sin guardar — pero no se ha ejecutado.
- **El atajo global de Electron y la ventana oculta de grabación nunca se probaron end-to-end**: `desktop/dictation.cjs` y `desktop/dictation-recorder-preload.cjs` solo se probaron con `globalShortcut`/`BrowserWindow`/`net` inyectados y falsos (`desktop/dictation.test.cjs`, 7 pruebas) — nunca se abrió la app de escritorio real, se pulsó el atajo de verdad, ni se vio la ventana oculta pedir permiso de micrófono y grabar. La primera vez que se pida el permiso `media` en esa ventana aparecerá el diálogo de confirmación que ya usa el resto de la app (`desktop/main.cjs`'s `permissionRequest`) — necesita un clic humano una vez por sesión, no hay forma de saltárselo sin debilitar la política de permisos existente.
- **El atajo es "pulsar para empezar / volver a pulsar para terminar", no mantener pulsado**: `globalShortcut` de Electron no distingue tecla-abajo de tecla-arriba multiplataforma sin un hook nativo de teclado de bajo nivel, que esta tarea no añadió (ninguna dependencia nueva). El texto de Configuración ya lo dice así, pero vale la pena confirmar con el dueño si el comportamiento real (toggle) es aceptable o si en algún momento merece la pena añadir esa dependencia para un push-to-talk de verdad.
- **La tabla de formatos de portapapeles preservados es solo texto (`CF_UNICODETEXT`)**: si el portapapeles tenía una imagen, HTML enriquecido o una lista de archivos copiados antes de dictar, ese formato se pierde tras el pegado — el resultado lo señala en `note`, pero no se ha probado en vivo qué apps lo notan de forma molesta (p. ej. pegar una imagen copiada justo antes de dictar).

## CERRADO (e8accbbf, ffe3e9e6) — Dos pruebas de tool_serve fallan segun el orden de ejecucion

`tests/test_tool_serve.py::test_search_by_query_hits_keyword_hints_without_embedder`
y `::test_serve_returns_promote_list_and_schemas_for_named_tools` fallan en
suite completa y tambien al ejecutar solo ese fichero, pero cada una pasa
cuando se ejecuta sola. Verificado con `git stash` que no las provoca el
cambio del tokenizador: fallan igual sin el.

La primera devuelve la lista de herramientas de correo sin `send_email`; la
segunda recibe un payload con `summary` pero sin `schema`. Las dos pintan a
estado global de modulo que otra prueba del mismo fichero deja cambiado
(probablemente el monkeypatch de `get_tool_index` o el catalogo de esquemas).

Lo que hay que hacer no es re-pinear la asercion sino encontrar que prueba
contamina a cual, y aislar ese estado en una fixture, como se hizo con
`_BASH_PROBED`/`_BASH_CACHE` en `platform_compat`.

## Deriva de arquitectura, autonomía en sombra, dos skills nuevas (FAUSTUS.md §180) — pendiente de verificar en la máquina en vivo

- **Nada de esto corrió contra un servidor real (7001/7006) ni contra un modelo real**: este entorno es un worktree aislado sin Faustus arrancado ni acceso a un helper llama.cpp/Ollama; toda la verificación fue con `pytest` (falsos/inyectados donde hacía falta), `tsc --noEmit` y `npm run build` de Studio. Falta: (1) provocar una deriva real (mover un fichero de módulo, introducir un ciclo) en un repo de verdad y comprobar que la nota aparece en el resumen del turno; (2) dejar `approval_autonomy` en `shadow` una sesión entera y revisar que el panel de Ajustes → Agente muestra un historial creciente y coherente con lo que de verdad se aprobó o denegó; (3) promover una familia a mano desde el panel y comprobar en una llamada real que `active` la aprueba sin tarjeta.
- **El panel de autonomía nunca se vio en un navegador real**: confirmado solo por `tsc --noEmit` limpio y `npm run build` en verde; falta abrir Studio de verdad, ver la tabla de familias renderizada con datos reales, y pulsar "Promover"/"Degradar" contra el servidor.
- **Granularidad de familia**: hoy una familia es "un nombre de herramienta" (`family_for`); si en el uso real una misma herramienta mezcla llamadas muy distintas en riesgo (p. ej. `write_file` sobre un fichero de configuración frente a uno de datos), esa promoción puede ser demasiado gruesa — vale la pena revisar con datos de uso reales antes de recomendar `active` por defecto en ningún perfil.
- **El panel de autonomía sólo muestra el agregado por familia**, no el registro completo de decisiones individuales; si hace falta auditar una decisión concreta habría que ir a la base de datos directamente (`approval_shadow_log`) — sería razonable añadir un desplegable con las últimas N filas por familia.
- **`code_graph_drift` no se ha medido en un repositorio grande de verdad**: los tests usan repos sintéticos de un puñado de ficheros; falta ver cuánto tarda `drift()` (y si el presupuesto de tiempo por defecto, 8-10 s, es realista) en un repositorio con miles de ficheros como el propio Faustus.
- **La habilidad `learn-this-repo` nunca se probó con un modelo real siguiendo sus pasos**: se verificó que el `SKILL.md` parsea, cumple el tope de palabras y sobrevive el reinstalado, pero nadie la ha usado de verdad para estudiar un repositorio — falta ver si el fichero de notas (`LEARN_REPO_NOTES.md`) que el modelo produce es realmente útil para retomar una sesión días después.

## Radar de git (FAUSTUS.md §185, §187)

- Sin probar: tema claro y 420 px; la tarjeta de Inicio del vigilante `git_radar` con una notificación push real en el móvil; que el 27B llame `git_radar` a la primera con «¿qué tengo sin subir?» (banco de frases pasa, la conversación real no se ha hecho).
- El 7000 (app de escritorio, `data/`) aún no tiene `git_watch_roots` ni
  `git_scan_exclude`: ponerlos desde Source control → Watched folders.

## Todo local: visión del propio modelo (FAUSTUS.md §194)

- El servidor del 27B es compartido por el 7003, el 7006 y el 7009: con tres turnos a la vez la generación cae a 2–3 t/s y un turno sencillo con imagen tardó 15 min. Mirar si conviene limitar ranuras por instancia o una cola por prioridad antes que MTP.
- El perfil del motor 72561c7e del 7006 se editó a mano (`--mmproj`, espera de 180 s); el script externo `Start-LlamaServerTask.ps1` también lleva ya el proyector. Si se recrea el motor desde Ajustes, se encontrará solo.
- En el turno de prueba la primera ronda tardó 565 s para 130 tokens de salida: casi todo fue esperar turno en el servidor compartido con 16,7 k tokens de prompt. Esa ronda llamó a `inspect_media` (sólo cabecera; la herramienta ya avisa de que no mira los píxeles) y la siguiente a `read_file`, que es la que le dio la imagen. Es inofensivo, pero es una ronda de más.
- Con la casilla verificada en el navegador (etiqueta «Visión», casilla marcada, Guardar deja `--mmproj` una sola vez y la espera de 180 s), queda sin probar sólo el caso de un modelo sin proyector (casilla desactivada).

## Uso diario 25-09 tarde (FAUSTUS.md §196)

- La puerta del modelo local (`_local_model_slot` en `src/llm_core.py`) es un solo candado para toda la instancia: dos turnos del mismo 7006 contra el mismo `llama-server` con 4 ranuras esperan uno tras otro («waited 60s for the local model slot»). Valorar un grupo por URL (varias peticiones al mismo servidor y modelo ya cargado, hasta sus ranuras; exclusivo frente a otros destinos, que son los que cargan modelos en la VRAM). Tocar con cuidado: es la protección de VRAM.
- Cantidades en listas de la compra y recetas escaladas: el 27B no usa `python` para multiplicar raciones y se equivoca (6 muslos para 7 personas «2,5–3 kg»). Un aviso de respuesta, al estilo de `answer_checks`, que detecte «para N personas» con cantidades y pida la cuenta, está por pensar.
- Erratas del 27B en castellano (≈1 por respuesta larga): no las causa `repeat_penalty` (A/B en FAUSTUS.md §198). Queda probar una temperatura más baja para respuestas largas en prosa o una pasada de ortografía local antes de mostrar; ninguna está hecha.
- Seguridad (revisión de §199): tras contenido de fuera, `web_search` sigue permitido y su consulta es un canal de salida (anterior a §199, no lo abre la lectura privada nueva). Valorar si una consulta de búsqueda que contiene texto de una lectura privada reciente debe pedir tarjeta.

- Arranque lento una vez: el 7006 tardó 4 min 15 s entre «Secret file hardening» y «Background-job monitor started» (19:07:10 → 19:11:25 del 25-09), sin una línea de log y con dos núcleos al 100 %, tras matar un proceso que acababa de terminar turnos. El reinicio siguiente tardó 15 s. En ese tramo están `recover_interrupted_runs`, `crash_recovery.boot_scan` y las reconciliaciones de benchmarks, recibos y VRAM; si vuelve a pasar, `py-spy dump` al proceso durante el hueco dirá cuál.
