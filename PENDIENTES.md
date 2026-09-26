# Pendientes de cierre

Actualizado: 26-09-2026. REGLA: nunca nombres de empresas/personas del buzón de Luis en commits, docs, tests ni comentarios — ejemplos siempre ficticios. Sólo trabajo vigente; quitar cada entrada al cerrarla.

Ordenado por tipo de trabajo (reorganizado el 26-09: se quitaron las entradas ya cerradas u obsoletas, con la prueba de cada cierre en FAUSTUS.md, los tests o el historial de git). Dentro de cada sección, lo más reciente primero.

## A. Decisiones o acciones de Luis

- **Permisos nuevos de Ledger's, Links y People's Hoard** (26-09): los tres esperan aprobación en Ajustes › Integraciones («new permissions pending approval»). Tras el re-escaneo su riesgo es `medium`: cada puente lee su propio `*_TOKEN` del entorno para llamar a su API local.
- **Ollama como hogar del modelo por defecto** (18-09, §114/§118): el 7000 ya usa por defecto `qwen3.8-27b-q8-llamacpp` en el 8081; lo que queda por decidir es si Ollama conserva algún papel (hoy no tiene modelos cargados) o se retira del arranque.
- **Volver a Ollama tras usar llama-server** (18-09, §114): paso manual — `D:\LocalAI\Stop-LlamaServer.ps1` y reactivar `warm_default_model=true`.
- **`OLLAMA_KEEP_ALIVE=-1`** como variable de entorno del servicio Ollama (17-09/18-09, §107/§109): para que otra app con `keep_alive` de 5 min no desaloje al 27B entre re-pines.
- **Umbrales de decisiones tipadas** (23-09, §177): ajustar `typed_decision_min_confidence` (0.7) y `typed_decision_min_mass` (0.5) mirando los cubos de calibración reales.
- **Umbral de gibberish** `local_gibberish_script_threshold` (0.40) (18-09, §114): subirlo o desactivarlo por sesión si aparece un falso positivo real con muchos caracteres no latinos legítimos.
- **Heurística de Ollama en puerto no estándar** (18-09, §114): decidir si vale la pena sondear `/api/tags` para reconocerlo sin declaración manual en Ajustes.
- **Encender `agent_context_engine`** en la instancia principal (23-09, §176): sigue `False` por defecto; recomendado tras que Luis pruebe `/brain` unos días.
- **Descripciones MCP de los Hoards de Node** (Ledger's, Links', People's) (23-09, §179): superan 110 caracteres en primera línea; viven en sus propios repos, no en éste.
- **Renombrar el prefijo `odysseus_*`** (colecciones vectoriales, columnas `odysseus_kind`/`odysseus_ref`) (18-09, §112): sólo tiene sentido junto a una migración de datos real; decidir cuándo.
- **Borrar `origin/dev`** en GitHub si ya no sirve (18-09, §112): necesita push de Luis.
- **Publicar Nightingale's Hoard en GitHub** (23-09, §178): decidir, igual que el resto de la familia.
- **`data/skills/general/design-before-code`** vive sólo en una máquina y está en `.gitignore` (19-09, §132): decidir si se versiona y dónde.
- **Perfil dev «Jobhunter (test data, 5179)»** arranca en 5179 con conector apuntando a 5178 (23-09, §169): dejado así a propósito; no tocar sin decidir.
- **`NewRepositoryDialog` visible en modo compacto** del panel de control de versiones (22-09): decidir si es capacidad nueva (actualizar `CONTRATO_GIT_4.md` punto 3) o descuido a cerrar.
- **Instalar una voz local de más calidad** (Kokoro o Piper) desde Ajustes → Voz (17-09, §105; §106/§153): las voces de Windows funcionan pero suenan peor. La pantalla ya se vio (selector con «Local (Piper)» y frases de parada). Falta pulsar «Instalar motor» y descargar una voz, que baja ficheros de internet y sólo se hace con tu permiso; esa misma instalación probaría por fin el binario de Piper para Windows (sólo se probó el de Linux).
- **Probar un push real** (17-09, §104): hace falta un móvil o un navegador con el servicio de push activo, servidor por HTTPS (VPN de malla); el navegador usado hasta ahora lo tiene desactivado.
- **SearXNG debe estar levantado** para el briefing de noticias (17-09, §99): si no, la tarjeta dice «search failed».
- **`agent_sandbox_mode` por defecto** (`auto`, corre en el host si Docker está caído) (14-09, §85): decidir si debería depender del sistema operativo en vez de ser global.
- **Sesión admin en el 7001**: iniciar sesión en la instancia de desarrollo (19-09, §133) para poder seguir con las comprobaciones visuales de Ajustes pendientes ahí.
- **`utility_model`/`utility_endpoint_id` nulos en el 7001** (23-09, §169): fijarlos en ese data dir para poder probar la extracción de instintos.
- **Nota de coaching** (20-09, §90): el agente editó su propio validador (`cults3d_check.py`) durante una tarea; decidir si el harness debe marcar como sospechosa cualquier escritura a un fichero que la propia tarea usa como verificador.

- **Prueba de voz física completa** (QA-41, comprobación pendiente): conversación completa por micrófono en español e inglés; no se puede activar grabación ni permisos sin que Luis esté delante.
- **Puente MCP y Docker Desktop en la máquina de Luis** (08-09, spec v2): el arranque lanzado a través del puente falla con `unable to get 'ProgramData'` (PowerShell sin `ProgramData`/`ALLUSERSPROFILE`); lanzado directamente por Luis funciona a la primera. No reproducible sin acceso a esa máquina.
- **`OLLAMA_MAX_LOADED_MODELS=1`** (spec v2): variable del servicio Ollama de la máquina de Luis, no es código nuestro; decidir si se deja así.
- **HW-06, cuándo un nodo remoto pasa a "implementado"** (spec v2, HW-06): la primitiva (`src/remote_worker_registry.py`) está construida y probada; falta que Luis fije el criterio de fiabilidad/soporte para activar el flag.
- **HW-07, banco Spark+PC+eGPU** (spec v2, LAB): exige el rig físico real para validar los 5 criterios de aceptación; fuera de alcance sin ese hardware.
- **Render de Mermaid en Studio** (11-09): hoy solo se muestra la fuente (copiar/descargar); decidir si merece la pena añadir una librería de render gráfico.
- **OBJ-5, nodos remotos por grupos** (11-09): aplazado hasta que Luis tenga un segundo PC.
- **Adaptador Herdr contra una instancia real** (ADP-13/CMP-06): `src/external_runtimes/herdr.py` infiere el contrato solo del texto del informe; falta que Luis dé una URL de un Herdr real para probarlo (solo lectura, por diseño).
- **Nodo de tipo "loop" en workflows** (ADP-31): diseño y dataclasses existen (`src/contracts/workflow_iteration.py`) sin tipo de nodo en el esquema; activar un bucle real es decisión de producto.
- **Importar un export real de otro programa de diagramas** (ADP-17): el formato de `src/workflows/interchange.py` nunca se contrastó contra un exportador real; falta que Luis aporte un fichero de ejemplo real.
- **Enlace de material de documento y futuro del "fork clásico"** (11-09, excursos): sin decidir si el panel debería conocer la ruta del documento, y si el fork clásico debería dejar de copiar mensajes.
- **Mediciones de laboratorio INF-06/07** (spec INF): especulación, reparto entre GPUs, comparación de motores; solo con autorización explícita de Luis para cada tanda.
- **Modelo pequeño de tool-calling on-device** (19-09 tarde): aplazado a propósito — implicaría una DLL nativa, descarga de pesos y telemetría por defecto, contra el criterio del proyecto; decisión de Luis si se retoma.
- **Retención de `llm_trace`** (FAUSTUS §126): 7 días por defecto; decidir si es suficiente para depurar un problema reportado varios días después o si conviene subirla cuando el disco lo permita.
- **Exponer `grounding`/`memory_conflicts` como herramienta de agente** (FAUSTUS §145): hoy ninguno de los dos lo está (mirroring deliberado); decidir si se exponen juntos.
- **Publicar `faustus-sdk` en un registro** (paridad, TF02): decidir npm público o GitHub Packages.
- **Licencia y procedencia, TF17** (paridad): "decisión pendiente del propietario" según `docs/spec/paridad/MATRIZ_PARIDAD.md`; ningún PR técnico la puede cerrar sola.
- **Tope de escritura en memoria** (FAUSTUS §120 Parte B): `memory_engine.add_item` hoy recorta en silencio a `MAX_TEXT_CHARS`; decisión deliberadamente no tomada sobre si debería rechazar en vez de recortar.
- **Enganchar el pase de sueño de skills al planificador nocturno** (FAUSTUS §151): `task_scheduler.py` no tiene un patrón simple reutilizable para "job a hora fija"; falta decidir con qué patrón (tarea de sistema vs `ScheduledTask` de usuario) encajarlo.
- **Granularidad de familia en autonomía de aprobaciones** (FAUSTUS §180): hoy una familia es "un nombre de herramienta"; revisar con datos de uso reales antes de recomendar el modo `active` por defecto en ningún perfil.
- **`git_watch_roots`/`git_scan_exclude` en la instancia 7000** (FAUSTUS §185/§187): el escritorio (7000, `data/`) aún no los tiene configurados; ponerlos desde Source control → Watched folders.
- **Política de reparto del 27B compartido entre instancias** (FAUSTUS §194): 7003/7006/7009 comparten servidor y con tres turnos a la vez cae a 2–3 tok/s; decidir si limitar ranuras por instancia o usar una cola por prioridad antes de tocar MTP.
- **Atajo de dictado: toggle o push-to-talk** (FAUSTUS §157): hoy es pulsar/volver a pulsar; confirmar con Luis si vale así o si merece la pena un hook nativo de teclado para mantener pulsado.
- **`web_search` tras leer contenido privado** (uso diario 25-09, §199): valorar si una consulta que contiene texto de una lectura privada reciente debería pedir tarjeta de aprobación.
- **Cliente OAuth de Google Calendar** (13-09, Correo): Luis tiene que crear el cliente web en Google Cloud, habilitar la Calendar API y registrar las redirect URIs (guía en `docs/api/google_oauth_setup.md`); el asistente ya da las URIs exactas y valida el `client_secret`.

Acciones físicas o de cuentas que sólo puede hacer Luis (micrófono, móvil, WhatsApp, instalar voces, lanzar sus apps, datos reales de memoria):

- **Guardián del modelo por defecto** (18-09, §109): con Ollama real, confirmar el ciclo de ~20 s sin perder VRAM, reinicios seguidos sin pisar cargas, la etiqueta «Kept loaded by Faustus» en Ajustes, que Unload explícito sí funciona, y que una pregunta del móvil en el hueco no se queda sin respuesta.
- **El por defecto cede el sitio solo** (18-09, §109): reproducir el caso del modelo grande que no cabía y ver «X steps aside for Y» en vez de tarjeta vacía; confirmar la vuelta a los ~10 min y que un modelo de embeddings activo no la retrasa.
- **Auditar el almacén real de memoria** (22-09, §161): quitar con la pasada de auditoría las dos entradas que dieron pie a `volatile_facts.py` (siguen ahí).
- **Síntesis real de Piper** (20-09, §153): instalar el paquete, descargar `es_ES-davefx-medium`, sintetizar, comprobar duración del WAV, forzar un timeout corto y confirmar que mata al hijo.
- **«Seguir a la app» en Conectores** (17-09, §96): arrancar un segundo Jobhunter (cae en 5179), parar el de 5178, pulsar Check y confirmar «Followed the app from … to …».
- **Perfiles y conector `gepetto` en vivo** (17-09, §101): probar los perfiles reales (Dorian's, Gepetto's, Plato's, Jobhunter's, Writer's) y `connect` con la app arriba.
- **Creator con el flag activado** (16-09, §90-94): comprobar por pantalla `/creator`, la biblioteca, un preflight real contra un motor instalado y el lifecycle de un plugin de principio a fin.
- **Voz manos libres sin micrófono probado** (17-09, §105): interrupción, guarda de eco, frases de parada, y qué transcribe Whisper al decir «Faustus».
- **Palabra de activación con pantalla apagada** (17-09, §105): confirmar que sigue sin funcionar en el móvil.
- **`pushsubscriptionchange` nunca disparado** (17-09, §104): esperar una rotación real de claves de push o forzarlo con flags del navegador.
- **Emparejamiento móvil real** (17-09, mobile M-A): QR → `GET /api/mobile/bootstrap` → WS, en cuanto exista el build Android (lote M-B).
- **Backfill completo de WhatsApp** (17-09, §100): Unlink → Start → escanear de nuevo para bajar todo el histórico de golpe.
- **Tercera ola de WhatsApp en vivo** (17-09, §100): permiso de micrófono, popovers de reaccionar/reenviar, salto a un mensaje citado fuera de ventana, el 409 al citar un mensaje anterior al arranque del puente.
- **Puente de WhatsApp tras reinicio** (17-09, §100): confirmar que sigue vivo (gracias a `detached.json`) y que Jobhunter no hay que relanzarlo.
- **ADP-09, validación física Windows UIA** (11-09): `src/desktop_semantics/windows_uia.py` solo probado contra fakes; falta correrlo contra una sesión Windows real con UIA activo.
- **`capability_pricing` de OpenRouter** (CMP-08): solo probado contra el shape documentado; contrastar contra un payload real.
- **Grounding lint contra datos reales** (FAUSTUS §145): insertar un ítem bien respaldado y uno inventado en el store real y comprobar que el panel «Grounding» distingue uno del otro; revisar también la heurística `proper_noun` y las fechas en formato barra (día/mes vs mes/día) con datos reales.
- **Meetings con una grabación real** (FAUSTUS §156): la pestaña (Biblioteca › Meetings) se abrió en el 7000 con «Record» y «Upload audio»; falta subir un audio real y ver transcripción y notas.
- **Fragmentación con `ffmpeg` en audio largo real** (FAUSTUS §156): `_split_chunks()` solo probado mockeado; confirmar con una grabación de más de 10 minutos que los fragmentos y las marcas de tiempo son coherentes.
- **Tabla de alucinaciones de Whisper con audio real** (FAUSTUS §156): cubre los casos clásicos documentados, pero no se probó contra ruido de fondo o acentos variados; alguna entrada genérica podría descartar una intervención corta real.
- **Atajo global de Electron y ventana oculta de grabación** (FAUSTUS §157): `desktop/dictation.cjs` solo probado con `globalShortcut`/`BrowserWindow`/`net` inyectados; falta abrir la app real, pulsar el atajo y ver el diálogo de permiso de micrófono.
- **Formatos de portapapeles perdidos al dictar** (FAUSTUS §157): solo se preserva texto (`CF_UNICODETEXT`); si había una imagen, HTML enriquecido o ficheros copiados antes de dictar, se pierde — falta ver qué apps lo notan.

## B. Código por hacer

Nada abierto a 26-09 (mañana): lo que era una mejora pasó a OBJETIVOS (OBJ-47).



## C. Verificar en vivo sin modelo

- **Pantallas nuevas del 26-09 sin datos para verlas llenas** (§209): el desglose por decisión del panel de autonomía (el registro en sombra está vacío mientras el modo sea `off`), los conflictos de memoria sugeridos (hoy no hay ninguno) y «Relaunch with this profile» (sale tras activar un perfil que deja algo pendiente; no se activó con el examen en marcha). Verificadas en vivo: la revisión de skills importadas (proyecto › Reglas, dos skills con su riesgo y el botón Aprobar) y el nombre del servidor MCP en Procesos.

- **Optimize con una medición real, y lo que cuelga de una tarea** (spec INF): la pestaña Optimize del Cookbook ya se abrió en el 7000 el 26-09. Muestra el plan (endpoint, modelo, objetivo y la suite `es_conversation`) y no arranca nada al abrirla. Falta lanzar una medición con el 8081 libre y ver en vivo el chip de arquitectura, «Capabilities», el `ReceiptPanel` de una tarea y la cronología bajo una respuesta.
- **`llama-server` gestionado desde la UI, resto de casos** (FAUSTUS §119 Parte B): crear un engine real desde Ajustes contra `llama-server.exe`, ver Start pasar de `stopped` a `unhealthy` a `running`, el rechazo si el puerto ya está ocupado por otro proceso, el Stop con confirmación cuando sirve el modelo por defecto, y «Rellenar desde lo que ya escucha en este puerto».
- **Tope duro del bloque de memoria en pantalla** (FAUSTUS §120 Parte B): provocar el tope en un proyecto real y confirmar que el panel deja ver cuántos ítems se omitieron.
- **Casilla de «aprobar de todas formas» con un servidor MCP crítico de verdad** (FAUSTUS §147): las insignias de riesgo y «Re-scan security» ya se vieron en Ajustes › Integraciones (26-09); falta un servidor local que lea un `*_TOKEN` y lo mande a un host externo, para ver la cuarentena y la casilla.
- **Pestaña Concepts con datos** (FAUSTUS §154): la pestaña ya se abrió en el 7000 (Contexto › Concepts con proyecto; sin proyecto dice «No project bound»), pero ningún proyecto tiene conceptos todavía: falta ver el grafo `<canvas>`, el clic en un nodo y el panel de referencias rotas cuando el agente registre alguno.
- **Deriva de arquitectura contra un refactor real** (FAUSTUS §180): provocar una deriva real (mover un fichero, introducir un ciclo) en un repo de verdad y comprobar la nota en el resumen del turno; dejar `approval_autonomy` en `shadow` una sesión entera y revisar el historial del panel.
- **Arranque lento del 7006** (§199/§209/§211): la pila a los 90 s del 26-09 señaló la recuperación de ejecuciones leyendo registros; ya sólo se lee la cola de los terminados. En el próximo reinicio del 7006 (tras el examen 30), mirar cuánto tarda entre «Secret file hardening» y «Application startup complete» y, si sigue en minutos, leer la pila nueva de `brain_data/logs/crash.log`.

## D. Verificar en vivo con el modelo local

- **Caché del prompt en una tarea con imágenes** (26-09, §211): en el examen 31 (con `agent_keep_images_batch` y el recordatorio de idioma que ya no se mueve), comprobar en `server7006.err` que las líneas `[engine] round` sólo pierden caché cada cuatro imágenes y nunca por el recordatorio; `prefix_diff.py` (en `_claude_tmp`) dice qué mensaje cambió.
- **Tope del juego de herramientas por chat** (26-09, §211): en el 7000, dos turnos seguidos del mismo chat pidieron herramientas distintas (correo y luego imagen) y, al pasar de 28, el segundo empezó juego nuevo: 19k tokens releídos, 57 s. Correr la batería con `scripts/daily_eval.py --set agent_sticky_toolset_max=N` (28 y 44) y comparar aciertos y el «caché del prompt» de la cabecera; fijar el valor por defecto con eso.
- **Nota de `vision_write_every` en el examen 30** (26-09, §211): comprobar en la traza (`iq.py`) que la sexta pregunta sobre una misma imagen trae la nota, y si el 27B escribe `RESPUESTA.md` antes que en el 29 (11:47 del tercer tramo). Mirar también en `server7006.err` las líneas `[engine] round` por si alguna ronda pierde la caché.
- **A/B del razonamiento en rondas de continuación** (26-09, §210): tras el examen, correr `scripts/daily_eval.py --set agent_followup_reasoning_budget=N` con N = 0, 1536 y 2048 (el ajuste se restaura solo al acabar), comparar aciertos y tiempo por ronda, y fijar el valor por defecto con el resultado.
- **Calibración de tokens reaprendida** (26-09, §210): tras desplegar, comprobar en `GET /api/token-calibration` que `qwen3.8-27b-q8-llamacpp` vuelve a un factor cercano a 0,9 en chats de texto, y que el ledger del turno enseña la línea «Razonamiento del modelo que se conserva» en una tarea larga.
- **Borrador primero en una tarea larga** (26-09): en el examen 30, comprobar que a la cuarta parte del turno el 27B crea `RESPUESTA.md` con la estructura y lo va completando.
- **Relevo antes del tope de tiempo** (26-09, §209): en la próxima tarea larga (examen 30), comprobar que al 85 % del tope el 27B deja el plan con resultados y un fichero de notas, y que el turno siguiente arranca desde ahí en vez de releerlo todo.
- **«Mañana a las 8» en una tarea programada** (26-09): pedirla en un chat y comprobar que `next_run` cae mañana.
- **Tarjeta del guardián de comandos destructivos y línea de estado** (26-09, §209): en un turno real, que la tarjeta nombre el comando y su motivo, y que durante una herramienta larga la línea de estado diga qué herramienta corre. Los veredictos por cita del informe de investigación salen con la siguiente investigación.
- **Cantidades para N personas** (26-09, `answer_checks.servings_requested`): pedir en el 7006 «lista de la compra para 8: pollo al horno con patatas» y comprobar que el 27B calcula por persona con `python` y que piezas y pesos cuadran.
- **`context_*` automáticos en una tarea larga** (25-09, §197): ver si el 27B los usa solo, a partir de qué punto, y si el aviso al umbral blando ayuda o estorba.
- **`swarm_map` en modo `agent`** (25-09, §197): probarlo con el 8081 compartido por otros chats (el modo `llm` ya se probó con 6 ciudades).
- **Reescritura de día de la semana y paráfrasis de «recuerda que…»** (25-09, §184): confirmar en turnos reales que el bug lunes→viernes y el razonamiento en voz alta ya no aparecen.
- **Ruta ofrecida vs inventada** (25-09, §184): medir en turnos reales que una oferta ya no provoca rechazo y que un «he guardado X» falso sí.
- **Calidad real de `bug_hunt`** (24-09, §189): medirla con el 27B sobre `src/git_radar.py` o similar; ajustar el prompt si inventa expectativas.
- **`fix_memory` en un chat real** (24-09, §189): comprobar que un turno con ficheros cambiados deja línea en `DATA_DIR/fix_memory/<owner>/` y que el turno siguiente muestra «Past fixes».
- **Carriles de `enforce` con `delegate_agents` real** (24-09, §189): probar con un `AGENT.md` de biblioteca y el diálogo de Studio con clics reales.
- **Turno de noche real** (24-09, §189): 2-3 tareas de `dispatch` con presupuesto corto; comprobar la tarjeta de Inicio vía `night_shift_report`.
- **Examen Eldoria, escalera de racha y prueba 02** (24-09, §184): probar la tercera tanda en la ejecución 14; si sigue sin cerrar, medir con un modelo principal que vea; ejecutar la Prueba 02 (Ingenio).
- **Elementos del examen que dependen de visión** (24-09, §184): identificar los círculos, el numeral cisterciense y la unidad (185,2 m/cable). Todo local: la visión es la del propio 27B (mmproj en el 8081).
- **`unconsulted_sources` en conversación real** (24-09, §184): medir falsos positivos.
- **`inspect_image` contra Visión real** (24-09, §181): probar con un modelo de Visión real y un modelo principal con visión real, foto con `action: "ask"` y pregunta concreta.
- **`eval_typed_decision.py` contra el modelo grande de Ollama** (23-09, §177): repetirlo cuando esté libre (el ayudante 3B ya está medido).
- **Latencia real de la llamada de actualidad** (23-09, §177): confirmar el p50 (hasta 1,5 s); si molesta, bajar presupuesto o estrechar la regla.
- **Nightingale's Hoard con un turno real** (23-09, §178): adoptarla tras reiniciar el 7000 y probarla («limpia este CSV y hazme un gráfico por ciudad»); probar «pregúntale a tus datos» con un modelo compartido resuelto.
- **Resumen de entidad tras inactividad real** (23-09, §176): confirmar que aparece de verdad, no sólo en el pase forzado a mano.
- **Presupuesto de pensamiento y corte a 240 s** (22-09): medir si debería depender de lo ya escrito en el razonamiento, dado que el 27B q8 corta la ronda 2 en 4/4 corridas de tareas de código.
- **Instintos en segundo plano con modelo cargado** (23-09, §169): probar con `qwen3.5:4b` residente y un turno con corrección del usuario; mirar `[instincts]` en el log del 7001.
- **Adopción de default entre instancias** (23-09, §169): probar con dos instancias con modelo cargado la adopción del default residente de una vecina y el veto a desalojar un modelo activo en otra.
- **Perspectivas insuficientes en modelos pequeños** (19-09, §133): medir si siguen devolviendo sólo 1 en vez de 2-4.
- **Regla `action:"ask"` en conversación real** (19-09, §132): disparar una dentro de un turno con modelo de verdad y ver la tarjeta de aprobación en el chat.
- **Auto-continuación de tarea larga y repetitiva** (18-09, §115): contra llama-server real, confirmar que las rondas se extienden solas con la línea de progreso, y que un atasco real dispara un `ask_user` concreto.
- **Memoria procedural de dueño global** (18-09, §115): confirmar contra el store real que una nota con `owner=""` aparece en el bloque de memoria aprendida de otro usuario.
- **Síntoma de turno cortado tras 0-1 llamadas** (18-09, §115): si reaparece, revisar `_stuck_rounds`/`_tool_call_signature` en `agent_loop.py` y confirmar que `loop_breaker.py` actúa.
- **Efecto del recorte del prompt MCP en un modelo pequeño** (18-09, §110): medirlo con qwen3 o similar, que antes se atascaba con el volcado de 14.657 tokens.
- **Por qué el 27B emite `<<faustus_ctx_ack>>` solo** (18-09, §107): investigar con memoria recuperada grande (~14k tokens) y si conviene recortarla.
- **Aviso de pregunta sensible al tiempo respetado** (18-09, §107): confirmar en un chat real que un modelo local (qwen3.5 o similar) busca en la primera ronda.
- **Falsos positivos de «buscar sin pedir permiso»** (18-09, §107): vigilar en uso real en preguntas límite que mezclan opinión y actualidad.
- **Hora actual mal usada en el prompt** (17-09, §98): el 27B dijo «en menos de una hora» de una entrevista ya pasada (13:00 vs 15:07); revisar cómo llega la hora al prompt.
- **`presence_penalty` 1.5 para Qwen cuantizado** (20-09, §90): probarlo (recomendación oficial contra la repetición) y medir antes/después con el mismo lote.
- **Persona de `AGENT.md` en un turno real** (16-09, §90-94): comprobar en el Studio que `persona: security-auditor` antepone de verdad el bloque al prompt.
- **Caso completo del sampler local** (18-09, §108): repetir en vivo la coherencia desde el primer token, la ruta a `/api/chat` nativo, el escalón 2 de la escalera saltado, `min_p`/`repeat_penalty` como campos de primer nivel, y el corte antes de 300 caracteres en una racha de gibberish provocada a propósito.

- **Modelos pequeños y las etiquetas [Certain]/[Likely]/[Guessing]** (Modos de comportamiento, 12-09): ver si modelos de 9B o menos las respetan; si fallan sistemáticamente, un modo «adversarial-lite» sin etiquetas.
- **`remember_answer` con Qwen** (Conectores Hoard, 13-09): probarlo en un contexto de prueba real.
- **Stop en un turno de agente realmente largo** (BUG-STOP-01, FAUSTUS §116): confirmar contra `llama-server` real que un clic de Stop para el turno en la ronda siguiente en una tarea de cientos de rondas, y que `runIdRef` del Studio no se desincroniza.
- **Auditoría nocturna con `background_jobs_may_load_models` activado** (FAUSTUS §117): confirmar que esa noche la auditoría sí corre con el modelo de utilidad descargado.
- **Snapshot de memoria por sesión en el Studio real** (FAUSTUS §120 Parte A): abrir una sesión, confirmar la nota "(snapshot taken …)" estable entre turnos, y que una sesión nueva sí recoge una regla añadida mientras tanto.
- **Tok/s con MTP on/off en el 27B real** (FAUSTUS §122): la detección de capas MTP ya funciona; falta medir la ganancia real con GPU disponible y `-np 1` (con varias ranuras paralelas la ganancia se pierde casi entera).
- **Batería de sondas de inyección contra el 27B** (FAUSTUS §148): verificada solo con el modelo pequeño; repetir con el grande, que obedece más y es el caso interesante.
- **Sondas del canario redirigidas a una herramienta real** (FAUSTUS §148): comprobar que el resultado en modo vivo coincide con lo esperado de la sonda determinista.
- **Extracción SSE `tool_start`/`tool_output` con tráfico real** (FAUSTUS §148): best-effort; probar con un modelo real que hace varias llamadas a la misma herramienta en la misma vuelta.
- **Encender `agent_context_engine` en un turno de agente real** (FAUSTUS §174): correr `scripts/bench_context_engine.py` y un turno real con la bandera activa (SSE `context_packet`, memoria aprendida sin duplicarse, timeout conserva el bloque clásico).
- **Encender el Context Engine para chat simple** (FAUSTUS §174): un turno de chat real con la bandera activa, mismos chequeos.
- **Revisión con duda contra el helper real** (FAUSTUS §150): confirmar que el modelo pequeño (`qwen2.5-3b-helper`, `:8082`) detecta de verdad un diff roto en un fichero de alto riesgo real del repo.
- **Pase de sueño de skills contra un modelo real** (FAUSTUS §151): sembrar una skill + sesiones sintéticas, correr el pase con el helper real, ver la propuesta en la pestaña Proposals y rechazarla.
- **Agrupación automática de diffs grandes contra el helper real** (FAUSTUS §152): un diff sintético de ~10 ficheros con un bug inyectado a mano contra `http://127.0.0.1:8082/v1`; confirmar `groups > 1` y que el bug aparece entre los hallazgos.
- **Inyección automática de conceptos de proyecto en un turno real** (FAUSTUS §154): con el ajuste encendido, confirmar que el bloque "project concepts" llega al prompt y aparece en el ledger bajo `instructions`.
- **Grabación real de una reunión con micrófono** (FAUSTUS §156): el pipeline ya se probó en vivo por script (20-09); falta una grabación real con `MediaRecorder` o un audio subido y ver la barra de progreso y el Markdown final.
- **Prueba de dictado en Windows real** (FAUSTUS §157): correr `scripts/dictation_windows_live_test.py` (listo, nunca ejecutado) con la voz Piper `es_ES-davefx-medium` de principio a fin.
- **`learn-this-repo` usada por un modelo de verdad** (FAUSTUS §180): nadie la ha usado todavía para estudiar un repositorio; ver si `LEARN_REPO_NOTES.md` resulta útil para retomar una sesión días después.
- **El 27B llamando a `git_radar` sin que se lo pidan** (FAUSTUS §185/§187): el banco de frases pasa; falta la conversación real con «¿qué tengo sin subir?».
- **Temperatura más baja para respuestas largas en prosa** (uso diario 25-09, §198): las erratas del 27B en castellano no las causa `repeat_penalty` (ya descartado por A/B); falta probar con una temperatura más baja.
- **Huecos del calendario y búsqueda en memoria** (25-09, §184): comprobar en el 7006 el hueco ocupado (32) y una búsqueda en memoria por pregunta sobre el usuario (31).
- **Borrador rechazado en el Studio** (25-09, §184): confirmar que desaparece también ahí vía el evento `response_replace`.
- **`seen_urls` de alcance amplio** (25-09, §184): vigilar en uso real si conviene limitarlo a resultados de búsqueda y páginas abiertas (hoy recoge cualquier enlace visto en el turno).
- **Suelo de temperatura en modo chat** (19-09, §117): repetir la sonda `/slots` a mitad de petición en chat llano y confirmar `temperature=0.6`; confirmar que `/temp 0.9` de turno y un preset con temperatura propia siguen ganando al suelo.
- **Arreglo `--jinja`** (18-09, §114): repetir la conversación que colgaba y confirmar en `/slots` que `enable_thinking` llega en `false` por defecto y que `/think on` sigue funcionando con `reasoning_budget:4096`; comprobar lo mismo si algún día se usa otro motor compatible OpenAI.
- **Sampler de llama-server** (18-09, §114): repetir la conversación de 7800 tokens y confirmar en `/slots` `max_tokens`/`repeat_penalty`/`min_p`, que el tope de 8192 no corta una respuesta legítima, y que el razonamiento va al panel de pensamiento sin mezclarse con la respuesta.
- **Recorte del bloque MCP del prompt** (18-09, §110): confirmar la bajada de tokens en un «hola», que un tool MCP concreto sigue siendo llamable, que `lookup_tools` encuentra uno no seleccionado, que el volcado completo se restaura con el ajuste, y probarlo con integraciones reales (Gitea, Linkding, Home Assistant).
- **Recuperación sin `ctx_ack`** (18-09, §108): contra el 27B real, confirmar que ya no aparece «0000…», que no responde sólo `<<faustus_ctx_ack>>` repetidamente con memoria recuperada grande, y que fundir contexto y pregunta en un mensaje no le hace citar la etiqueta.
- **Escalón de recuperación en pantalla** (18-09, §108): ver renderizado `harness_check status:"recovery"` («Recuperando…») y el mensaje final de los 4 escalones fallidos; confirmar que el endpoint de utilidad responde rápido.
- **Escalera de recuperación tras w110/w111** (18-09, §108): repetirla con un caso real que degenere (no reproducido desde el cambio) y ver «Recovering…» en Studio; vigilar que el por defecto no se descargue en una pasada larga.
- **Code Mode con la pestaña visible** (19-09, §133): repetir la pregunta que se respondió por API porque la pestaña de Chrome estaba oculta.
- **Bloques ` ```chart ` en el navegador** (19-09, §131): confirmar el SVG, el interruptor «Show/Hide data», el fallback de un JSON roto, y comparar modo oscuro y claro.
- **Historial tras aprobar una tarjeta** (20-09, §90): reproducir gate de contexto externo → aprobar → comprobar que `/api/history/<sid>` guarda la parte posterior (hoy se pierde).
- **KV cache real del 27B** (17-09, §98): medirla antes de subir `num_ctx` en candidaturas (hoy 65.536, bajado de 199.680 por ir a 2 tok/s).
- **Harness con plan grande** (17-09, §95): repetir con un plan ≥60 KB / 20+ tareas y un segundo chat sin adjunto («Continua»); probar `ui_smoke` contra un proyecto FastAPI y con `npm start` (sólo Flask probado).
- **ADP-32, medir los pools de admisión** (11-09): `src/resource_admission.py` define pools de prioridad pero no se ha medido en producción si `llm_core._LOCAL_MODEL_LOCK` limita tareas reales.
- **Niveles 1 y 2 de skills en el log** (FAUSTUS §120 Parte D): en los logs del 7000 y del 7006 sólo aparece el nivel 0 (49 turnos); falta un turno que elija una skill y confirmar que sube a 1 o 2.
- **Tablas de frases en/es del pase de sueño de skills** (FAUSTUS §151): heurísticas de subcadena; probarlas contra un corpus real de respuestas de usuarios.
- **Tarjeta de revisión con un diff realmente grande** (FAUSTUS §152): ver cómo se ve la tarjeta `harness_check` (`review_issues`/`review_running`) con un diff multi-archivo genuinamente grande.
- **Vision con un modelo sin proyector** (FAUSTUS §194): con la casilla activada ya se probó (etiqueta «Visión», espera de 180 s); falta el caso con la casilla desactivada.
