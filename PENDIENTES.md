# Pendientes de cierre

Actualizado: 09-10-2026. REGLA: nunca nombres de empresas/personas del buzón de Luis en commits, docs, tests ni comentarios — ejemplos siempre ficticios. Sólo trabajo vigente; quitar cada entrada al cerrarla.

Ordenado por tipo de trabajo (reorganizado el 26-09: se quitaron las entradas ya cerradas u obsoletas, con la prueba de cada cierre en FAUSTUS.md, los tests o el historial de git). Dentro de cada sección, lo más reciente primero.

## A. Decisiones o acciones de Luis

- **Borrar datos de prueba del 02-10 que no puedo borrar yo**: en Hypatia, el examen de profesor «Examen · Tema 4- Análisis sintáctico» (`9076685c…`, botón «Borrar examen»), su copia practicable en Exámenes de la asignatura (`11334f6c…`) y la pregunta aprobada en la prueba «¿Para qué se utiliza la programación dinámica en el análisis sintáctico?» (`93e30fab…`, en el banco del Tema 4); en Funes, las sesiones `20261002-035830-1a78` (falló al grabar) y `20261002-035920-d55c` («Prueba de reunión (borrar)»). El resto de datos de prueba (CookHoard, HomeHoard, el plazo espejo de Kafka y las 3 propuestas de People) ya está limpio.
- **Probar el puente de Telegram con un bot real** (01-10, §245): está probado contra un servidor falso de la API. Para la prueba real hace falta que crees un bot (en Telegram, el chat de creación de bots → `/newbot`) y pegues su token en Ajustes del agente → Telegram, con tu id de chat en la lista (el bot te lo dice la primera vez que le escribes). ¿Lo hacemos?
- **Medir MTP y MoE en CPU (#332, #345)**: necesitan el 8081 en exclusiva durante un rato. Hoy no se puede, porque pediste dejar GPU libre. Dime cuándo.

- **Probar Claude como modelo con una clave real** (26-09, §212): la caché rodante de la conversación y el pensamiento dentro de bucles con herramientas sólo están probados contra respuestas simuladas; hace falta un endpoint de Anthropic (o `anthropic/*` por OpenRouter) con clave para un turno de agente de varias rondas, mirando `[anthropic-cache] read=` en el log y que no haya 400 por bloques de pensamiento.

- **Permisos nuevos de Ledger's, Links y People's Hoard** (26-09): los tres esperan aprobación en Ajustes › Integraciones («new permissions pending approval»). Tras el re-escaneo su riesgo es `medium`: cada puente lee su propio `*_TOKEN` del entorno para llamar a su API local.
- **Papel de Ollama** (18-09, §114/§118; actualizado 08-10): el 7000 ofrece por defecto GLM-5.3-Flash NVFP4 en las tres Sparks mientras sirve, configurable en Ajustes → Sparks; las GPU locales siguen disponibles. Thinking, esfuerzo GLM y espera inicial están activados y configurables. Queda decidir si Ollama conserva algún papel o se retira del arranque.
- **Volver a Ollama tras usar llama-server** (18-09, §114): paso manual — `D:\LocalAI\Stop-LlamaServer.ps1` y reactivar `warm_default_model=true`.
- **`OLLAMA_KEEP_ALIVE=-1`** como variable de entorno del servicio Ollama (17-09/18-09, §107/§109): para que otra app con `keep_alive` de 5 min no desaloje al 27B entre re-pines.
- **Umbrales de decisiones tipadas** (23-09, §177): ajustar `typed_decision_min_confidence` (0.7) y `typed_decision_min_mass` (0.5) mirando los cubos de calibración reales.
- **Umbral de gibberish** `local_gibberish_script_threshold` (0.40) (18-09, §114): subirlo o desactivarlo por sesión si aparece un falso positivo real con muchos caracteres no latinos legítimos.
- **Heurística de Ollama en puerto no estándar** (18-09, §114): decidir si vale la pena sondear `/api/tags` para reconocerlo sin declaración manual en Ajustes.
- **Encender `agent_context_engine`** en la instancia principal (23-09, §176): sigue `False` por defecto; recomendado tras que Luis pruebe `/brain` unos días.
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

- **Prueba de voz física completa** (QA-41, comprobación pendiente): conversación completa por micrófono en español e inglés; no se puede activar grabación ni permisos sin que Luis esté delante. Incluye la transcripción especulativa (§243): en el registro del navegador debe salir `[voice] speculative transcript used` cuando el turno acaba en una pausa, y nada si sigues hablando tras la pausa corta.
- **Puente MCP y Docker Desktop en la máquina de Luis** (08-09, spec v2): el arranque lanzado a través del puente falla con `unable to get 'ProgramData'` (PowerShell sin `ProgramData`/`ALLUSERSPROFILE`); lanzado directamente por Luis funciona a la primera. No reproducible sin acceso a esa máquina.
- **`OLLAMA_MAX_LOADED_MODELS=1`** (spec v2): variable del servicio Ollama de la máquina de Luis, no es código nuestro; decidir si se deja así.
- **HW-06, cuándo un nodo remoto pasa a "implementado"** (spec v2, HW-06): la primitiva (`src/remote_worker_registry.py`) está construida y probada; falta que Luis fije el criterio de fiabilidad/soporte para activar el flag.
- **HW-07, banco Spark+PC+eGPU** (spec v2, LAB): exige el rig físico real para validar los 5 criterios de aceptación; fuera de alcance sin ese hardware.
- **Render de Mermaid en Studio** (11-09): hoy solo se muestra la fuente (copiar/descargar); decidir si merece la pena añadir una librería de render gráfico.
- **OBJ-5, nodos remotos por grupos** (11-09): aplazado hasta que Luis tenga un segundo PC.
- **Adaptador Herdr contra una instancia real** (ADP-13/CMP-06): `src/external_runtimes/herdr.py` infiere el contrato solo del texto del informe; falta que Luis dé una URL de un Herdr real para probarlo (solo lectura, por diseño).
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
- **`capability_pricing` de OpenRouter** (CMP-08): solo probado contra el shape documentado; contrastar contra un payload real.
- **Grounding lint contra datos reales** (FAUSTUS §145): insertar un ítem bien respaldado y uno inventado en el store real y comprobar que el panel «Grounding» distingue uno del otro; revisar también la heurística `proper_noun` y las fechas en formato barra (día/mes vs mes/día) con datos reales.
- **Meetings con una grabación real** (FAUSTUS §156): la pestaña (Biblioteca › Meetings) se abrió en el 7000 con «Record» y «Upload audio»; falta subir un audio real y ver transcripción y notas.
- **Fragmentación con `ffmpeg` en audio largo real** (FAUSTUS §156): `_split_chunks()` solo probado mockeado; confirmar con una grabación de más de 10 minutos que los fragmentos y las marcas de tiempo son coherentes.
- **Tabla de alucinaciones de Whisper con audio real** (FAUSTUS §156): cubre los casos clásicos documentados, pero no se probó contra ruido de fondo o acentos variados; alguna entrada genérica podría descartar una intervención corta real.
- **Atajo global de Electron y ventana oculta de grabación** (FAUSTUS §157): `desktop/dictation.cjs` solo probado con `globalShortcut`/`BrowserWindow`/`net` inyectados; falta abrir la app real, pulsar el atajo y ver el diálogo de permiso de micrófono.
- **Formatos de portapapeles perdidos al dictar** (FAUSTUS §157): solo se preserva texto (`CF_UNICODETEXT`); si había una imagen, HTML enriquecido o ficheros copiados antes de dictar, se pierde — falta ver qué apps lo notan.

- **Cantidades para N personas** (26-09, `answer_checks.servings_requested`; repetido el 01-10 en el 7001): el 27B q4 da la lista en la primera ronda con cantidades razonables (unos 400 g de pollo por persona) sin calcular con `python`; el empujón de «sin acción en el espacio de trabajo» que lo mandaba a buscar recetas ya no salta en listas de la compra, recetas ni menús (§247). Falta decidir si una lista para N personas debe calcular siempre por persona con `python`.

## B. Código por hacer

Nada abierto a 09-10. Cerrado el 09-10: la prueba `test_loop_call_that_cannot_get_its_claims_is_refused_not_started` (mantenía la reserva 3 s fijos y en Windows el turno tardaba más en llegar a la llamada; ahora la suelta al acabar el bucle, `f0f16648`), `extract_to_schema` sin ejemplos en el índice de herramientas (`604f046d`), el endpoint de pruebas `capture-8090` del 7000 sin servidor detrás (desactivado, no borrado) y la recarga del Hub para la revisión ligada a entregas (el Hub corre desde las 21:14 del 08-10 con `2886aef` dentro).
