<p align="center">
  <img src="assets/branding/faustus-wordmark.png" alt="Faustus" width="300">
</p>

<p align="center">Una estación de trabajo de IA autoalojada para modelos locales, proveedores en la nube y equipos de agentes.</p>

<p align="center">
  <a href="README.md">English</a> ·
  <a href="#inicio-rápido">Inicio rápido</a> ·
  <a href="#qué-puedes-hacer">Funciones</a> ·
  <a href="#arquitectura">Arquitectura</a> ·
  <a href="website/setup.md">Guía de instalación</a>
</p>

![Faustus Studio](assets/screens/studio.png)

## Qué es Faustus

Faustus reúne chat, agentes de programación, investigación, escritura, flujos de imagen y vídeo, voz y conocimiento de proyectos en un solo espacio. Es un fork personal de [Odysseus](https://github.com/odysseus-dev/odysseus), con backend Python/FastAPI e interfaz React/TypeScript.

Usa modelos locales mediante Ollama y servidores compatibles, o conecta APIs en la nube. Configura un orquestador y sus especialistas dentro de la conversación. Mantén juntos el trabajo, las fuentes, los archivos generados y el contexto del proyecto, y ve qué está ejecutándose sin adivinar si el modelo se ha congelado.

La idea que se repite es que el agente tiene que enseñar su trabajo: de qué contexto partió un turno, qué herramienta produjo qué evidencia, qué modelo respondió y cuánto costó, qué aprobación desbloqueó qué acción. Cada función descrita más abajo tiene un test que falla si eso deja de cumplirse, y una sección en [FAUSTUS.md](FAUSTUS.md) que explica por qué existe.

Local-first significa que eliges dónde se ejecuta la inferencia. Las APIs y los clientes oficiales autenticados consumen la facturación o cuota del proveedor; la inferencia local utiliza tu propio hardware. Elegir un proveedor remoto le envía el contexto necesario para esa petición.

## Inicio rápido

Instala Docker y Docker Compose y, a continuación:

```bash
git clone https://github.com/Luissalet/Faustus.git
cd Faustus
cp .env.example .env
docker compose up -d --build
```

En PowerShell, usa `Copy-Item .env.example .env` en lugar de `cp`.

Abre **http://localhost:7000**. La contraseña inicial de administrador aparece en `docker compose logs odysseus`. Compose incluye los servicios de búsqueda y almacenamiento vectorial; no descarga un modelo de lenguaje por ti.

1. Conecta tu servidor de modelos desde Ajustes o Cookbook. Si Ollama está en el equipo anfitrión de Docker, configura su dirección accesible siguiendo la [guía de instalación](website/setup.md).
2. Crea un proyecto y adjunta los archivos, documentos o fuentes que debe conocer.
3. Inicia una conversación. Elige **Chat** para conversar o **Agente** para herramientas y acciones.
4. Consulta **Actividad** para seguir conversaciones, flujos de trabajo, renders y aprobaciones.
5. Añade motores de imagen/vídeo, servicios de voz o clientes de agentes externos cuando los necesites.

Instalación nativa, Windows/macOS, GPU, HTTPS y configuración: [guía de instalación](website/setup.md). Conserva tu carpeta `data/` y haz una copia de seguridad antes de actualizar.

## Qué puedes hacer

### Lanzadores de escritorio y web para Windows

Los lanzadores están dentro del repositorio y resuelven las rutas desde la carpeta del proyecto:

| Lanzador | Acción |
| --- | --- |
| `Start-Faustus-Desktop.bat` | Abrir una ventana Electron nativa con minimizar, maximizar/restaurar, pantalla completa y cierre integrados en el tema. |
| `Start-Faustus.bat` | Arrancar Faustus y abrir la interfaz web en el navegador. |
| `Stop-Faustus.bat` | Detener el servidor gestionado por estos lanzadores. |
| `Restart-Faustus.bat` | Reiniciar ese servidor y abrir la interfaz web. |

La primera instalación necesita Python y Node.js. Los lanzadores preparan las dependencias que falten, reconstruyen los assets de Studio cuando están desactualizados e instalan el runtime de escritorio fijado si es necesario. `Start-Faustus.ps1 -NoBrowser` arranca el modo web sin abrir pestaña; `-Port 7001` permite elegir otro puerto.

Cerrar la ventana de escritorio detiene el backend **sólo si esa ventana lo arrancó**. Un servidor web ya en marcha se reutiliza y sigue funcionando al cerrar la ventana. Se comprueba la propiedad del proceso; no se detienen otros procesos Python ni servidores de modelos externos ajenos. La autenticación de escritorio es independiente de la del navegador, así que hay que iniciar sesión una vez en la ventana.

Las tareas programadas necesitan un servidor Faustus en marcha y el ordenador despierto. Usa el modo web para mantener el backend funcionando tras cerrar las pestañas del navegador; cerrar una ventana de escritorio que sea la propietaria también detiene la programación. En un chat Agente, pide una recurrencia en español o inglés, indica hora y zona horaria y gestiona la tarea guardada en **Automatizaciones**. Las tareas recurrentes admiten zonas horarias IANA y cambios de horario de verano; las tareas antiguas sin zona conservan su comportamiento en UTC.

### Instalar Faustus en el móvil

Studio se instala como app independiente (PWA) desde el propio navegador, sin tienda ni compilación. En navegadores Chromium (escritorio o Android), **Ajustes → Este dispositivo** ofrece «Instalar Faustus» en cuanto el navegador lo permite; en Safari de iOS, que nunca lo ofrece por sí solo, la misma pantalla explica el paso manual (Compartir → Añadir a pantalla de inicio). Una vez instalada abre a pantalla completa con su icono, mantiene la interfaz operativa sin red y recibe notificaciones del sistema —turno terminado, aprobación pendiente, recordatorio— aunque la app esté cerrada, mediante Web Push estándar (VAPID + `aes128gcm` implementados sobre `cryptography`, sin servicios de mensajería de terceros). Para llegar a Faustus fuera de tu red el servidor tiene que estar expuesto por HTTPS (una VPN o un túnel), cosa que «Este dispositivo» recuerda pero no configura. Por debajo de 767 px la navegación pasa a una barra inferior de cinco pestañas (Inicio, Studio, Calendario, Notas, Ajustes). [API móvil](docs/api/mobile.md) · [notas de la vista móvil](docs/ui/pwa.md).

### Herramientas creativas dentro de la conversación

- **Señalar para editar:** selecciona un punto en una captura del navegador y describe el cambio. Se añaden al borrador una captura anotada y la procedencia de la captura, sin enviarla automáticamente. El agente debe inspeccionar la página actual y el proyecto; las coordenadas de la captura no son ubicaciones inventadas en el código fuente.
- **Referencias visuales de proyecto:** guarda `@referencias` con nombre como enlaces de contexto de proyecto, distingue sujeto, estilo y composición, y adjúntalas explícitamente desde el selector. Quitar el enlace de una referencia no elimina su imagen de la galería.
- **Aprender un estilo:** extrae reglas de estilo editables a partir de ejemplos en TXT/Markdown usando el modelo elegido, compara la respuesta base con la respuesta con estilo y guarda y selecciona un preset. La comparación hace dos llamadas al modelo usando la conexión seleccionada.
- **Vídeo local:** transcribe con un modelo Whisper ya instalado, edita o traduce manualmente los segmentos con marca de tiempo, exporta SRT/VTT, y renderiza narración usando las voces de Windows en inglés o español instaladas. Sin API en la nube ni descargas automáticas de modelos. Las entradas están acotadas a 64 MB, 3 minutos y 1080p; se requiere FFmpeg/FFprobe. Esto es narración local práctica, no clonación de voz ni sincronización labial. La exportación narrada sustituye el audio original.
- **Notas de reunión:** graba o sube un archivo de audio y recibe notas en Markdown — resumen, decisiones, tareas (responsable/fecha límite cuando se mencionan), preguntas abiertas y la transcripción completa con marcas de tiempo — mediante un trabajo en segundo plano que fragmenta las grabaciones largas, transcribe cada fragmento con el motor de voz configurado, y hace una pasada con un modelo local para redactar las notas. Si esa pasada no está disponible, la transcripción se guarda igualmente con un aviso en lugar de perderse. Está en Biblioteca → Meetings.

### Chat y panel de trabajo persistente

- Alternar entre modelos locales y de API; conectar OpenAI, Claude, Gemini y OpenRouter con configuración guiada y prueba de conexión. Las llamadas a OpenRouter informan de su coste real, las preferencias por endpoint (recopilación de datos, orden de proveedores, búsqueda web solo cuando se pide) son explícitas, y un perfil de privacidad solo local rechaza las llamadas salientes en lugar de degradarlas en silencio.
- Un interruptor en Settings → Default AI (también en Settings → Local models) elige si el modelo por defecto se carga al arrancar y se mantiene cargado — cambia en vivo, sin reiniciar: encendido lo carga ya, apagado lo libera al momento (VRAM libre en segundos, nunca a mitad de un turno de chat). Funciona con los dos backends locales: mantenido en Ollama vía `keep_alive`, o un `llama-server` de llama.cpp gestionado que se arranca y se exime de su propio temporizador de inactividad mientras el interruptor está encendido. Cada lista de modelos (Default AI, Local models, el selector del chat) muestra quién lo sirve de verdad — Ollama, llama.cpp o una API remota — detectado una vez y compartido en todos lados.
- Decodificación restringida en los dos backends locales: cuando una pasada interna pide un objeto JSON, el esquema viaja como gramática — `format` en Ollama nativo, `response_format` en llama-server — y el modelo no puede emitir un token que lo rompa. Forzar JSON a secas no basta: devuelve JSON válido con un valor fuera del conjunto permitido.
- Cuando el motor que sirve el modelo de chat por defecto está parado, Settings → Local models lo dice encima de la lista de motores y ofrece un botón que lo arranca, en vez de dejar que el siguiente chat falle sin nada que hacer.
- **Un turno cortado sigue respondiendo.** Cuando el cortacircuitos termina una ejecución por repetirse sin avanzar, todo lo necesario para responder ya está en la conversación, así que una última pasada sin herramientas la escribe — en la ruta que estaba respondiendo de verdad, para que el coste caiga donde cayó el trabajo. Un turno detenido nunca es una respuesta en blanco.
- Preguntar no se trata como escaquearse: con un workspace abierto, una petición de que te expliquen algo se responde con prosa sin devolverla exigiendo una llamada a herramienta, y el recordatorio permanente de idioma se coloca de forma que lo último que lee el modelo sea el paso que acaban de darle.
- Pegar capturas directamente con **Ctrl+V**, adjuntar archivos y mencionar ficheros del workspace.
- Navegar por chats largos con la barra de mensajes: previsualización al pasar el cursor, clic o arrastre para saltar, o usar flechas, Inicio/Fin y RePág/AvPág. Consultar mensajes anteriores pausa el seguimiento automático del flujo.
- Leer el Markdown generado junto al chat, editarlo y guardarlo con detección de conflictos. Los borradores sin guardar pertenecen a su conversación y sobreviven a la navegación entre paneles. Los tres diseños de Studio (conversación, documento, revisión) comparten una misma sesión de documento, de modo que una selección en el documento se convierte en un cable de contexto en el redactor, y una sugerencia del agente aterriza en su anclaje, nunca en la primera línea que coincida.
- Mantener archivos, resultados generados, fuentes, contexto del proyecto, actividad de agentes y capturas del navegador en un panel lateral redimensionable.
- Cambiar a otra conversación mientras el servidor sigue ejecutando el turno actual. Ver la posición en cola, la actividad en curso, el uso de herramientas y las solicitudes de permiso; reconectar con el trabajo existente. Una tarjeta de aprobación cuyo permiso murió con un reinicio lo indica, en lugar de ofrecer botones muertos.
- Cada turno muestra la estrategia que eligió el agente (edición directa, planificar y ejecutar, investigación, revisión especializada, explorar alternativas) y por qué; un turno que merece repetirse puede guardarse como receta con sus entradas reales.
- **Ve cada llamada al modelo que hizo una sesión** — un panel «Model calls» muestra la petición exacta enviada (secretos tapados), la respuesta ensamblada, las llamadas a herramientas, los tiempos y los errores de cada llamada, y cualquier llamada registrada puede reenviarse sin cambios a otro modelo para comparar respuestas lado a lado.
- Buscar y navegar con **Ctrl+K**. Usar español o inglés, temas, controles de densidad, tamaño de texto ajustable y movimiento reducido.

- Elige un **modo de comportamiento**: una postura conversacional con nombre (adversarial, socrático, escueto, mentor, equipo rojo, observador externo, editor, o uno escrito por ti) que cambia cómo argumenta Faustus, nunca lo que puede hacer; entra en el prompt después del preset de tarea y estrictamente antes de la política de contenido no fiable, también en modo agente e Incógnito ([API de modos](docs/api/behavior_modes.md)).
- Enchufa **tus propias apps locales como plugins**: cada conector es ya un manifiesto suyo (`plugins/<id>/plugin.json`), y una aplicación puede traer su propio `faustus-plugin.json` para que se ofrezca en cuanto se la ve, con el formulario relleno, sin mandar un parche a Faustus ([Plugins](docs/api/plugins.md)). Faustus puede listarlos, levantar uno que esté caído desde un perfil de arranque que hayas guardado, y enseñártelo cuando se lo pidas — una app que ya responde se adopta en vez de reiniciarse, y nada le permite pararlas. Nombrar una de tus apps en una petición trae estas herramientas consigo, y una app que arranca en otra dirección de la que dice su conexión se nombra como tal en vez de esperarla. Lo que pides con todas las letras («arranca Jobhunter», «¿qué recuerdas de mí?») se ejecuta sin tarjeta de aprobación; lo que el modelo decide por su cuenta tras leer contenido no fiable sigue preguntando. Igual con el trabajo dentro de la carpeta de un proyecto: pasar sus tests, un comando que escribes entre comillas invertidas, mirar sus ficheros con el shell, un análisis de datos en Python que se demuestra que no sale de la carpeta y escribir el fichero que nombras; instalar paquetes, capturar la pantalla o código que podría salir de la carpeta sigue preguntando. Una pantalla `/connectors` sobre el gestor MCP existente con estados reales que separan «la app está apagada» de «el adaptador falló el handshake», perfiles de arranque configurados por el usuario (argv estructurado, sin shell, readiness, idempotentes) y una lista de conectores permitidos por chat / proyecto / tarea programada que el despachador de herramientas hace cumplir en vez de solo ocultar ([API de conectores](docs/api/connectors.md), [selección de herramientas](docs/api/tool_selection.md), [receta de candidaturas](docs/api/candidature_recipe.md)).
- **Nueve apps tuyas más como plugins**: Echo's (el historial de tu portapapeles, buscable, con las contraseñas y tokens ocultos antes de guardarse; el asistente también puede ponerte texto en el portapapeles), Hypatia's (tarjetas de repaso espaciado que el asistente escribe de lo que lees y con las que te examina en el chat), Vulcan's (tus modelos imprimibles: medidas, miniaturas, duplicados y fichas de tienda que escribe el asistente), Ledger's (dinero de casa), Links (guardar para luego con el texto extraído), People's (quién es quién, último contacto, cumpleaños), Argus's (memoria de pantalla privada con OCR local), Borges's (tus documentos indexados con embeddings locales + BM25, responde con fichero y página) y Scribe's Hoard (grabadora de reuniones: micro y sonido del sistema como dos pistas, transcripción local). Cada una trae su `faustus-plugin.json`, se reconoce en su puerto y se ha usado de punta a punta desde un chat con un 27B local («crea dos cuentas y apunta 12,50 € de taxi», «guarda este enlace y dime de qué va», «¿qué estaba haciendo hace diez minutos?», «añade esta carpeta a mi biblioteca y cítame qué dice de plugins»). Tres skills de biblioteca las leen juntas: `hoard-daily-digest` («¿qué he hecho hoy?»: pantalla, dinero, lectura, gente, reuniones, estudio), `hoard-meeting-prep` («tengo una reunión con X»: quién es, la última conversación, lo guardado, la cita exacta de la biblioteca, cuándo salió en pantalla) y `hoard-study-cards` («hazme tarjetas de mis apuntes sobre X» y «examíname»).
- Mira **qué se está ejecutando por culpa de Faustus y páralo**: un centro de control en `/processes` con los puertos que escuchan y el proceso detrás, los shells y servidores MCP del agente, los trabajos en segundo plano, los perfiles lanzados y las apps vigiladas (Cursor, ChatGPT, node…), cada uno con su origen y un Stop que solo puede pulsar una persona (el token del agente se rechaza, el tiempo de creación del pid es la prueba, el sistema y el propio Faustus nunca son objetivo). La misma pantalla empieza con **Apps**: tus proyectos locales como tarjetas con su icono, estado en vivo, Start / Stop / Restart, cola de consola y Abrir en una ventana de escritorio propia (una shell Electron genérica con el icono e identidad de la app en la barra) — añádelos, edítalos y quítalos libremente; las apps sin servidor MCP se vuelven conectores con un adaptador REST→MCP genérico alimentado por su OpenAPI o un manifiesto pequeño.
- **Revisa tu correo y actualiza tu búsqueda de empleo**: `review_candidature_mail` lee los últimos N días de correo (sin marcar nada como leído), encuentra las respuestas de las empresas (rechazos, entrevistas, ofertas), casa o crea la candidatura en Jobhunter's Hoard, registra allí la respuesta (idempotente por id de mensaje) y pone cada entrevista con fecha en el calendario (un evento por entrevista); las empresas ambiguas y las entrevistas sin fecha se informan para revisión manual, nunca se adivinan. Una sola llamada, así que un 27B local lo hace en un turno.
- **Pide cosas que se repitan y velas en Inicio**: «¿qué tiempo hace en mi ciudad mañana? cada día», «un briefing diario de noticias sobre X», «avísame cuando esta tienda vuelva a tener stock», «resúmeme el correo cada mañana»: acciones vigilantes integradas (tiempo de Open-Meteo, vigilancia de página que solo informa al cambiar, briefing de noticias con fuentes, resumen de correo) corren en el planificador sin que un modelo tenga que navegar, y cualquier automatización se puede fijar como tarjeta en Inicio con su último resultado, un botón de actualizar y su próxima ejecución.
- **Tu WhatsApp, leído y contestado desde aquí**: empareja tu cuenta por QR (protocolo multidispositivo de WhatsApp Web, un puente Node local en el loopback con token) y pide «qué me han escrito hoy», «resume lo que me ha dicho Ana» o «dile a Ana que llego diez minutos tarde» — cada envío lo apruebas tú; las notas de voz llegan transcritas; un resumen diario de WhatsApp puede ser una tarjeta de Inicio; Tools → WhatsApp es un cliente de chat de verdad (fotos de perfil, grupos, mensajes antiguos, respuestas citadas, reacciones, reenvío, edición, borrado, ticks, «escribiendo…», búsqueda, adjuntos, notas de voz grabadas, dictado) con un panel «Ask Faustus» que resume, redacta una respuesta en tu voz o traduce — solo texto, nunca envía por su cuenta.
- Sincroniza **Google Calendar como Google lo permite**: OAuth2 («Connect with Google», el mismo cliente que Gmail) y la Calendar API v3, descargas incrementales con `syncToken`, recurrencias y cancelaciones, escritura de vuelta protegida por etag; iCloud, Nextcloud y cualquier otro CalDAV siguen con el formulario de contraseña. El cliente OAuth de Google se configura desde Ajustes: el asistente te da las redirect URIs exactas, acepta el `client_secret.json` que descarga Google y lo comprueba contra Google, sin editar `.env` ni reiniciar ([Google Calendar](docs/api/google_calendar.md), [guía](docs/api/google_oauth_setup.md)).
- **Maneja Faustus desde tu propio código**: `sdk/ts` es un cliente TypeScript sin dependencias (ESM + CJS, eventos tipados generados desde el catálogo SSE versionado de `docs/api/sse_events.json`): crea una sesión, sigue un turno en streaming, contesta preguntas y aprobaciones en turno, cancela con el token de fencing del run y reanuda por cursor tras una desconexión sin repetir nunca un POST. Los tokens de API tienen el scope `sessions` para exactamente esa superficie (`docs/api/sdk_surface.md`); la prueba de aceptación A20 instala el tarball en dos proyectos limpios (CJS y ESM) y los ejecuta contra un servidor real con auth activada.
- **Paridad con el harness de referencia medida, no proclamada**: la matriz de paridad y las 36 recetas de aceptación de una auditoría independiente viven en `docs/spec/paridad/`; `scripts/acceptance_run.py` ejecuta las que tienen test real y escribe una fila de evidencia por caso (commit, hash de configuración, modelo, resultado, estado del coste). Las 36 están en verde a fecha de 16 de septiembre de 2026 — descubrimiento de herramientas, ejecución de código, offload de artefactos, sandboxing, OAuth de MCP, el componente embebible, login OIDC, identidad de servicio, programación de tareas, orígenes de skills, freno de bucles, aprendizaje con reversión, benchmark, migración y ciclo de vida de distribución — con las reservas honestas que quedan (publicación del SDK en un registro, instalación en máquina limpia, compartir sesión, sandbox git-backed para skills) nombradas en la matriz, no ocultas ([ejecutor de aceptación](docs/api/acceptance_runner.md)).
- **Mira cuánto tardó todo de verdad**: cada resultado de herramienta empieza con el tiempo de pared (esta llamada, lo que lleva el turno, el reloj) para que un comando lento sea visible para el modelo, no solo para ti; los mismos números viajan en el evento `tool_output` y en la tarjeta de paso del Studio.
- **Reach — ojos en internet en 9 canales** (web, YouTube, GitHub, Reddit, X, Hacker News, RSS, arXiv, Wikipedia), cada uno con una cadena de respaldo ordenada y real, sin depender de ningún CLI de terceros.
- **Grafo de código** responde preguntas de arquitectura y de trazado de llamadas — traza un camino entre dos símbolos, ve quién llama a lo que cambió un diff, obtén el mapa de rutas/puntos calientes de un repo — sin leer ficheros enteros. **Consultas de impacto** (`code_graph_impact`) recorren las llamadas entrantes desde un símbolo o desde todo lo que toca un diff, siguen los alias de import, y devuelven los ficheros de test afectados con un comando `pytest` listo en vez de una suposición.
- **Ranking de búsqueda explicable** — cada resultado web lleva una puntuación y los motivos detrás de ella: un impulso acotado cuando varios motores coinciden en un resultado, y una penalización (nunca un descarte) para resultados sin ningún término de contenido en común con la consulta, resultados de tipo tienda en una consulta corta sin intención de compra, o resultados obsoletos en una consulta sensible al tiempo.
- **Fan-out** corre un mismo prompt contra N modelos candidatos (locales y de pago mezclados libremente), cada uno en su propio worktree aislado, y puntúa los resultados automáticamente en vez de pedirte que compares N diffs a ojo.
- **Operaciones PDF** transforman un PDF ya existente — fusionar, dividir por rango arbitrario, rotar, reordenar, comprimir, marca de agua, rasterizar, OCR — lo único que el resto de la capa PDF nunca cubrió.
- **Navegación por estructura de PDF** (`pdf_outline`/`pdf_find_section`/`pdf_read_section`) construye un árbol de tabla de contenidos a partir de los marcadores propios del PDF (o detección de encabezados, o trozos fijos como último recurso) para saltar a una sección de un PDF largo en vez de leerlo página a página, con números de página que siempre vienen del árbol, nunca adivinados por el modelo.
- **Localizadores precisos de RAG para PDFs** — cada fragmento indexado de un PDF lleva un localizador estable y determinista `p12#b3` (o `p12-13#b3` cuando cruza páginas) que apunta a su página y bloque exactos, mostrado junto a las citas en el contexto recuperado para que una respuesta pueda señalar el sitio exacto de donde salió. Ajuste opcional `rag_pii_redaction` (apagado por defecto) sanitiza correos, teléfonos, IBAN, números de tarjeta (con checksum de Luhn), DNI/NIE español (letra de control verificada) e IPv4 del texto del fragmento antes de embeberlo/indexarlo, sin tocar el fichero original subido.
- **Personas de agente** — 16 identidades integradas (backend, seguridad, escritura, producto, ML…) que encajan en el propio AGENT.md de un agente sin sustituir sus permisos de herramientas.
- **Code Mode** (`run_code`) compone llamadas a herramientas dentro de un subproceso aislado bajo la misma puerta de política que una llamada directa, con cuotas de tiempo, número de llamadas y tamaño de salida.
- **Incrusta Faustus en tu propia página** con `<faustus-chat>` (`studio/embed/`), un Web Component con Shadow DOM y su propio cliente SDK, sin router ni CSS global, y un token que nunca toca `localStorage` ni el DOM.
- **Inicia sesión con tu proveedor de identidad corporativo** — OIDC con Authorization Code + PKCE, `state`/`nonce` de un solo uso y mapeo de cuenta con email verificado.
- **Creator** (ajuste `creator_enabled`, apagado por defecto) — un dominio de producción multimedia con 27 de sus 43 paquetes implementados y montados: modelo de documentos con revisiones, biblioteca y linaje sobre el catálogo de artefactos existente, identidad de modelo frente a despliegue, contratos de capacidad y parámetros, preflight puro, un puerto de adaptadores con harness de contrato genérico, operaciones no destructivas sobre un reloj racional exacto, timeline y relojes, un grafo de render FFmpeg determinista, transcripción ASR alineada, editor de subtítulos con exportadores SRT/VTT/ASS propios, casting de voces con registro de consentimiento, canvas de imagen por capas, storyboard y plan de producción (DAG validado), estudio de música, explorador de modelos, recetas ComfyUI expresivas, objetivos con criterios tipados, inventario físico de recursos, ciclo de vida seguro de plugins y evaluación/descubrimiento de presets. No trae ningún motor: cada adaptador reporta `available=False` honesto hasta que se instale uno.
- **Un harness para implementaciones largas** — lo que enseñaron 24 chats reales de un proyecto desde cero (FAUSTUS.md §95): un turno con tests rojos, un smoke de UI fallido o un changeset contradicho nunca cierra como completo; el propio harness arranca el servidor web del proyecto y comprueba cada página y recurso (status *y* Content-Type, errores de consola con Playwright) en vez de fiarse de que el modelo abra un navegador; las dependencias que faltan se avisan antes del primer comando; un worker delegado que vuelve vacío se reintenta una vez y luego se reporta, nunca se acepta en silencio; la segunda reescritura completa de un fichero grande se rechaza a favor de una edición dirigida; los tests perdonados como preexistentes pasan a todos de alta prioridad a los tres turnos; y un plan de implementación grande adjunto se parsea una sola vez en un tracker por tarea (`plan_status`/`plan_task`/`plan_done`…) para que el modelo vea la tarea actual y sus criterios de aceptación, no 172 KB de plan otra vez — y un «sigue implementando el plan» que acaba con cero tool calls se rechaza, no se acepta como respuesta.
- **Auditoría de accesibilidad y rendimiento en la UI** — el smoke headless que ya cazaba errores de consola ahora también avisa de texto alternativo ausente, botones/enlaces sin nombre, campos sin etiqueta, `lang`/`title` ausentes, ids duplicados, saltos en la jerarquía de encabezados y contraste WCAG AA, además de tiempos de navegación, LCP y bytes de JS/CSS. Los hallazgos llegan como avisos de calidad y solo bloquean el turno con `agent_ui_smoke_a11y_blocking` activado; los de rendimiento nunca bloquean.

### Excursos y cables de contexto

Lo que el modelo ve a continuación es exactamente lo que está conectado a la conversación: una regla tomada de [ThoughtDAG](https://github.com/chenxiachan/thoughtdag) y aplicada a chats lineales normales. El humano dibuja el grafo; ningún agente crea cables por su cuenta.

- **Explorar aparte:** selecciona un pasaje de una respuesta y abre un excurso que ve la conversación hasta ese turno más el pasaje, y a partir de ahí crece por su cuenta. La conversación original no cambia. La lista de hilos sangra los excursos bajo su conversación padre, y un mapa muestra el árbol.
- **Traer de vuelta:** conecta el excurso a su padre como un bloque de referencia explícito —el último intercambio, o el excurso entero—, cambia su profundidad, retíralo o elimínalo. Retirar un cable conserva el excurso.
- **Materiales:** fija una selección del documento (o el documento en vivo) y notas libres como contexto que permanece hasta que lo retires. El panel *Cables de contexto* previsualiza el siguiente turno capa por capa con recuentos de tokens, y dice qué no cuenta.
- **Repetir bajo un botón:** cada respuesta registra contra qué cables se escribió. Cuando un excurso o un documento avanza, las respuestas afectadas lo indican y ofrecen *Regenerar con la versión actual*; nada se regenera por sí solo.
- **Condensar a mano:** elige un rango de turnos asentados y pliégalos en una fila-resumen usando el mismo resumidor que la compactación automática, con tu propio modelo. *Expandir* restaura los originales byte a byte; el turno en curso nunca es elegible.

![Panel de cables de contexto sobre una conversación](assets/screens/studio-wires.png)

### Programación, herramientas y equipos de agentes

- Definir un **orquestador y sus minions desde el chat**, eligiendo modelos, roles y restricciones de herramientas.
- Delegar en conversaciones hijas con propiedad de archivos, progreso, cancelación y dirección durante la tarea.
- Utilizar **Consejo** para rondas iniciales ciegas, crítica y síntesis entre varios modelos, con un ejecutor designado para las acciones.
- Usar skills, herramientas MCP, herramientas de archivos, ejecución de shell y capacidades del navegador bajo los permisos configurados.
- Conectar los workers oficiales de **Codex CLI y Claude Code** mediante el sistema de runners y dispatch existente. Ambos disponen también de conexiones privadas de texto para el bucle de chat de Faustus, con una elección explícita de suscripción/API. El chat de Codex usa un hilo efímero de App Server sin entorno de ejecución; Faustus conserva la ejecución de herramientas y las aprobaciones.
- Mantener explícitas las vías de suscripción y de API. La autenticación del cliente oficial se comprueba antes de trabajar; un error de suscripción no recurre en silencio a la API de pago.
- Consultar evidencias de herramientas, comprobaciones de sintaxis, tests del proyecto y resultados de revisión. Los checkpoints con git sombra permiten ver diferencias y restaurar sin sustituir el repositorio propio del proyecto.
- Reabrir la evidencia original de un cambio desde el resumen de un turno. Los recibos, aislados por propietario, sobreviven a los reinicios, distinguen la evidencia guardada del éxito verificado, y alimentan el Espejo de estado del proyecto; los turnos incógnito quedan excluidos. [Historial de evidencias](docs/design/durable-change-evidence.md).
- **Control de versiones** sin salir del workspace: repositorios detectados en las carpetas vinculadas, identidades desde `~/.ssh/config`, entradas manuales y cuentas de `gh`, crear/clonar/publicar en GitHub, ramas, fusiones y pushes desde un panel con un borde arrastrable, y una política por repositorio que las herramientas `git_*` del agente obedecen (el shell rechaza `git commit`/`push` en su nombre). [API de Git](docs/api/git.md).
- **Code Mode se pausa para pedir aprobación y sigue.** Una llamada de script que choca con una puerta de aprobación de escritorio por llamada abre una pregunta, espera a la persona sin cargar esa espera al tiempo de reloj del script, y al aprobarla ejecuta esa llamada exacta una vez con una aprobación sellada antes de continuar.
- **Decisiones tipadas por probabilidad de opción**: el código interno puede pedirle a un modelo local que elija entre unas pocas opciones fijas en una sola pasada acotada (probabilidades de opción) en vez de generar y luego parsear texto; se usa como una capa extra opcional de juez para verificar afirmaciones.
- **Servidor MCP de navegador con DevTools, opcional** (apagado por defecto): un segundo servidor de navegador integrado para trazas de rendimiento, inspección de red/consola y auditorías de página, junto al servidor de navegación y control ya existente.
- **Política de herramientas por argumento**, más allá del interruptor por herramienta: una regla de administrador restringe un argumento (con ruta de puntos) de una herramienta (nombre exacto o comodín, p. ej. `mcp__github__*`) — una lista de dominios permitidos para `web_fetch`, un prefijo de ruta para una escritura, un patrón o un tope de longitud en cualquier campo — y deniega la llamada o la enruta al mismo flujo de aprobación humana que ya usan otras acciones con puerta. Se gestionan las reglas, y se puede probar una herramienta con argumentos contra ellas, desde Ajustes → Tools. [tool_arg_policy.py](src/tool_arg_policy.py).
- **Pide herramientas en español o inglés llano.** Cada herramienta integrada lleva frases de ejemplo y sinónimos del dominio, las herramientas que nombras o insinúas siempre se ofrecen, y un mensaje vago con un workspace nunca obtiene `bash` o `write` solo por la fuerza de un embedding.
- **Se ejecuta donde se ejecuta tu proyecto.** En Windows el shell del agente es Git Bash, una herramienta `powershell` cubre los lanzadores `.bat`/`.cmd`, `winget`, servicios y registro, y la herramienta `python` usa el `.venv` del propio proyecto en vez del de Faustus. El sandbox de Docker es una opción, no una puerta: `agent_sandbox_mode` viene en `auto`, que ejecuta el comando en el host siempre que el contenedor no pueda servirlo — en Windows nativo siempre, porque una imagen Linux no tiene nada de ese toolchain — y lo dice en el resultado; `strict` conserva la regla original de rechazar en vez de caer al host. El prompt del turno dice en qué máquina está, construido desde el propio ejecutor y no escrito a mano.
- **Alternativas:** prueba varios enfoques para el mismo cambio en worktrees aislados o copias congeladas, compáralos con la base señalando los archivos en disputa, y aplica uno con una fusión a tres bandas: una edición manual hecha mientras tanto sobrevive. Las alternativas de documento funcionan igual sobre versiones de documento. [API de alternativas](docs/api/alternatives.md).
- **Control semántico del escritorio** a través del árbol de accesibilidad en primer lugar, y de píxeles solo como alternativa explícita y más arriesgada; recuperar el control del escritorio invalida las referencias obsoletas del agente. [Semántica de escritorio](docs/api/desktop_semantics.md).
- **Búsqueda y reescritura estructural de código**: patrones de AST con metavariables (`$VAR`, `$$$ARGS`), previsualizados como diff y aplicados por el propio Faustus dentro del confinamiento del workspace, no por el CLI subyacente.
- **Puntuación de riesgo de un cambio** antes de tocarlo: una nota de 0 a 100 con sus motivos principales (llamadores, tests, churn, acoplamiento, tamaño del diff) desde el grafo de código y su historial de co-cambios, junto a la consulta de impacto ya existente.
- **Un turno de agente nunca termina en silencio.** Con progreso real (llamadas a herramienta que avanzan la tarea), el presupuesto de rondas se extiende solo hasta un techo configurable, con una línea de progreso visible en vez de una tarjeta vacía; sin progreso, o ante una ronda realmente vacía, el turno termina con una pregunta concreta al usuario. En tareas largas y repetitivas, un bloque de estrategia permanente en el prompt (solo en modo agente) guía a trocear en unidades, guardar un cursor de progreso y reanudar desde ahí.

Estas comprobaciones aportan evidencia sobre acciones compatibles; no son una prueba de que toda afirmación del modelo sea cierta. El acceso a herramientas, la verificación automática y los ejecutores externos son configurables, no se activan implícitamente al elegir un modelo.

### Conocimiento del proyecto y contexto ampliado

La identidad del proyecto se guarda separada del nombre de su carpeta en la barra lateral. Un agente puede añadir un documento generado u otra fuente compatible al contexto de su proyecto actual mediante una referencia.

El motor de contexto recupera y distribuye material relevante entre las fuentes del proyecto, el historial y la memoria. Registra procedencia, conflictos y cápsulas de contexto compactas, en lugar de intentar meter un disco entero en el prompt de un modelo. El almacenamiento persistente amplía lo que se puede recuperar, **no la ventana de contexto nativa del modelo**.

Usa **Sin recuerdos automáticos** en el cuadro de mensaje para suprimir la recuperación automática de memoria personal en los mensajes siguientes, incluida la compilación de contexto en vivo. El historial de chat existente y las fuentes del proyecto siguen disponibles. Esto no desactiva las herramientas de memoria ni el guardado del chat; usa Incógnito para su comportamiento de privacidad independiente.

En modo Agente, **Contexto del agente** también te permite omitir las skills automáticas y elegir un presupuesto orientativo de tokens de entrada antes de enviar. Estos controles viajan con el turno sin cambiar los ajustes globales. El presupuesto es una estimación, sigue limitado por la ventana de contexto del modelo elegido, y no es un límite de facturación. Las herramientas explícitas y las instrucciones del proyecto siguen disponibles aunque se omitan las skills automáticas.

Un proyecto también incluye:

- un **tablero** de incidencias tipadas (identificadores `KEY-N`, prioridades, etiquetas, enlaces, comentarios) con vista kanban y de tabla, importación/exportación en Markdown, y mensajes de commit que cierran incidencias con *fixes*/*closes*/*cierra*/*arregla*; el agente recibe herramientas `board_*` y un resumen del trabajo abierto en su prompt ([API del tablero](docs/api/board.md));
- una **especificación de requisitos con versiones**: cada requisito registra quién lo propuso, quién —siempre una persona— lo aceptó o lo rechazó, un historial de revisiones inmutable, y evidencia tipada que lo vincula con código, tests y ejecuciones, con una matriz de cobertura y un «contexto para esta tarea» acotado que el agente puede solicitar ([API de requisitos](docs/api/requirements.md));
- un **vecindario de conocimiento** que une requisitos, decisiones, símbolos, tests y ejecuciones en un único grafo tipado con relaciones `declared`/`located`/`verified` y un indicador `stale` honesto, más recibos de contexto por turno que dicen qué archivos alimentaron de verdad una respuesta ([API de conocimiento](docs/api/knowledge.md)).

**Atención** responde a «¿qué me necesita ahora?» en todas las conversaciones, ejecuciones y flujos de trabajo a lo largo de cuatro ejes —ciclo de vida, causa de la espera, salud de la conexión y siguiente acción—, de modo que una aprobación pendiente, una cola de GPU y una conexión caída son una sola lista, no tres pestañas. [API de atención](docs/api/attention.md).

**Segundo cerebro**: una bóveda markdown que Faustus crea y de la que es dueño — sin app de notas de terceros, sin cuenta aparte — bajo su propia carpeta de datos, en ficheros de texto plano con `[[enlaces wiki]]` que cualquier editor de markdown externo también puede abrir. Las memorias, las notas personales y las entidades tipadas (personas, lugares, herramientas, proyectos) se espejan en notas editables; editar el texto de una nota corrige la memoria que hay detrás, y borrar una la silencia (suprimida, no borrada) en vez de olvidarla — reversible desde una papelera. Un analizador determinista lee marcadores temporales («desde marzo de 2025», «hasta junio», «ya no») para que un hecho lleve su ventana de validez, y un hecho más nuevo del mismo tipo (trabaja en / vive en) cierra la ventana del anterior en vez de dar los dos por vigentes a la vez; una pasada en segundo plano (por reglas, más una pasada opcional con el modelo local que sólo conserva lo que el propio texto dice) extrae entidades y sus relaciones, y puede escribir resúmenes breves y citados cuando el modelo de utilidad está inactivo. La pantalla `/brain` («Cerebro») ofrece un explorador, una vista de lectura y edición, un panel de entidad con relaciones vigentes frente a su historial, y un grafo de fuerza dirigida de toda la bóveda o del entorno de una nota — sin ninguna librería de terceros. El agente recibe una herramienta `brain` (buscar, leer, escribir, añadir, entidad, línea de tiempo, vecinos, nota diaria), un servidor MCP a juego, y una fuente del Motor de contexto que aporta tarjetas de entidad y notas relevantes igual que la memoria aprendida. [API del cerebro](docs/api/brain.md).

![Requisitos con versiones y evidencia](assets/screens/requirements.png)

### Sistemas conectados

| Sistema | Para qué sirve | Implementación |
| --- | --- | --- |
| Enlaces de contexto del proyecto | Vincular chats y fuentes reutilizables a proyectos estables; añadir y resolver contexto por referencia. | [project_context](src/project_context/) |
| Motor de contexto | Recuperar, clasificar, distribuir y explicar el contexto reunido para un turno. | [context_engine](src/context_engine/) |
| Excursos y cables de contexto | Ramificar una conversación desde un pasaje, conectar referencias, documentos y notas de forma explícita, repetir respuestas obsoletas cuando se solicite, condensar a mano. | [side_threads.py](src/side_threads.py), [condense.py](src/condense.py) |
| Router de modelos | Elegir un modelo local o remoto en cada turno a partir de la calibración, la velocidad medida y el historial; nunca escala a una vía de pago sin un permiso explícito; explica el ajuste de cada candidato. | [model_router.py](src/model_router.py), [API del router](docs/api/model_router.md) |
| Política de proveedores y admisión | Decisiones de solo-local y suscripción-frente-a-API tomadas una vez y aplicadas en cada llamada saliente; admisión agrupada para GPU, trabajo intensivo de CPU y trabajo en primer plano. | [provider_policy.py](src/provider_policy.py), [resource_admission.py](src/resource_admission.py) |
| Perfiles de agentes y modos de completado | Perfiles de especialista reutilizables y políticas de completado según la tarea, dentro de los permisos existentes; un lint que detecta ciclos y roles inalcanzables. | [agent_profiles](src/agent_profiles/), [agent_profile_lint.py](src/agent_profile_lint.py) |
| Estrategia y recetas | Una estrategia observable por turno que solo escala ante un fallo observado, y recetas reutilizables construidas a partir de ejecuciones reales. | [strategy_policy.py](src/strategy_policy.py), [recipes.py](src/recipes.py) |
| Requisitos | Una especificación con versiones y aceptación humana, con evidencia tipada y cobertura. | [requirements](src/requirements/) |
| Segundo cerebro | Una bóveda markdown propia de notas y entidades tipadas con relaciones con ventanas de tiempo, sincronizada en ambos sentidos con el sistema de ficheros, más una vista de grafo. | [brain](src/brain/), [API del cerebro](docs/api/brain.md) |
| Decisiones tipadas | Preguntas cerradas respondidas con un solo prefill leyendo las probabilidades del siguiente token de las respuestas permitidas; consultivas, con presupuesto, nunca cargan un modelo. | [typed_decision.py](src/typed_decision.py), [API](docs/api/typed_decision.md) |
| Tablero del proyecto | Incidencias tipadas, kanban, enlaces que cierran incidencias desde el commit y herramientas de agente. | [project_board.py](src/project_board.py) |
| Panel de Git | Repositorios, identidades, GitHub, ramas y una política por repositorio que el agente obedece. | [git_panel.py](src/git_panel.py) |
| Modo Enséñame | Capturar demostraciones como procedimientos reutilizables, con revisión y controles de ejecución. | [rutas de Enséñame](routes/teach_mode_routes.py) |
| Sistema inmunitario | Registrar incidentes, evidencias y reglas correctivas para fallos recurrentes. | [immune_system](src/immune_system/) |
| Branching Futures y alternativas | Explorar enfoques alternativos en ramas aisladas, worktrees o versiones de documento antes de elegir uno. | [branching_futures](src/branching_futures/), [alternatives.py](src/alternatives.py) |
| Consejo | Organizar discusión entre varios modelos, crítica, decisiones y ejecución controlada. | [council](src/council/) |
| Flujos de trabajo | Grafos duraderos con simulación estructural, preflight, estimaciones de coste con recuentos separados para activaciones, llamadas a modelos y operaciones externas, comparación de planes y un lienzo. | [workflows](src/workflows/), [workflow_cost_estimate.py](src/workflow_cost_estimate.py) |
| Atención | Una sola lista de lo que necesita a una persona, en todo lo que se está ejecutando. | [attention.py](src/attention.py) |
| Semántica de escritorio | Control de escritorio mediante el árbol de accesibilidad, con elección explícita de canal y evidencia. | [desktop_semantics](src/desktop_semantics/) |
| Espejo de estado | Mantener observaciones fechadas con vigencia y procedencia; verificar y restaurar el estado materializado desde su historial confirmado. | [Recuperación](docs/design/state-mirror-recovery.md) |
| Universal Delta Engine | Comparar los cambios previstos y los observados en los ámbitos compatibles. | [delta_engine](src/delta_engine/) |
| Greedy Completion Engine | Descubrir y valorar trabajo de seguimiento útil según el modo, el alcance y el presupuesto elegidos. | [completion_engine](src/completion_engine/) |
| Voz Jarvis | Interacción de voz en español e inglés, respuestas habladas y una esfera reactiva. | [guía de voz](docs/design/voice-jarvis.md) |

Para detalles de implementación, consulta [FAUSTUS.md](FAUSTUS.md). El trabajo de verificación actual se recoge en [PENDIENTES.md](PENDIENTES.md).

### Imágenes, vídeo y audio

- Marcar imágenes adjuntas como referencias de sujeto/personaje, estilo o composición. Los papeles elegidos se incluyen como indicaciones visibles en el mensaje enviado; no garantizan condicionamiento a nivel de píxel. Una imagen de galería puede iniciar otro chat de referencia sin perder el enlace a su conversación original.
- Abrir la miniatura de un adjunto en el editor completo, con máscaras e inpainting, sin perder el borrador del chat. **Adjuntar resultado al chat** guarda el borrador de capas y máscaras y devuelve una copia PNG sin enviar un mensaje. El inpainting requiere un servicio de imagen compatible configurado.
- Planificar y ejecutar **recetas aprobadas de ComfyUI** para imágenes, edición con referencias y vídeo corto. Comprobar los modelos necesarios antes de poner el trabajo en cola.
- El selector **Receta multimedia** del chat muestra las recetas instaladas y sus parámetros, comprueba los requisitos del motor sin poner trabajos en cola y añade una petición editable al borrador. Las recetas de edición con referencias permiten variantes con intensidad y semilla; los nombres de imagen del motor se distinguen de los adjuntos del chat.
- Con varios motores configurados, elegir según disponibilidad, cola y capacidad, conservando el motivo de la elección.
- Guardar receta, versión, semilla, licencia del modelo, trabajo del motor y huella de las entradas junto con los artefactos generados.
- Recoger los renders enviados en el servidor incluso sin tener un chat abierto. Las descargas interrumpidas siguen siendo reintentables; los resultados completados aparecen en Actividad.
- Inspeccionar propiedades de imágenes, audio y vídeo antes de decidir cómo procesarlos.
- Convertir y redimensionar imágenes PNG/JPEG/WebP y extraer audio WAV/MP3 con herramientas multimedia acotadas, progreso y cancelación.
- Descargar resultados mediante enlaces de artefacto con propietario. Los mismos bytes pueden compartirse físicamente sin mezclar propietarios ni procedencia.
- Desplegar **Procedencia del archivo** junto a las descargas de Actividad para consultar tamaño, tipo, SHA-256, estado parcial/completo y la ejecución, proyecto o conversación de origen.

ComfyUI es un servicio separado; los pesos de los modelos, los nodos personalizados y sus licencias no se incluyen. Usa las [recetas multimedia](config/media_workflows/) y la [documentación de workers](website/fable-workers.md) para configurar los motores.

### Investigación, documentos y trabajo cotidiano

- **Búsqueda web explicable** — cada resultado lleva una puntuación y sus motivos: un impulso acotado cuando varios motores coinciden, y una democión (nunca una desaparición) para resultados sin término de contenido compartido con la consulta, de tipo tienda en consultas cortas sin intención de compra, u obsoletos en consultas sensibles al tiempo.
- **Tipo de fuente y calidad de extracción** en cada resultado web y página descargada — oficial, docs, académica, referencia, foro, tienda y más por una heurística de URL, más avisos de páginas delgadas, con boilerplate, con paywall o que requieren JavaScript, que alimentan el empujón de ranking anterior.
- Investigar con seguimiento de fuentes, comprobación de citas y exportación de informes.
- **Planificación de investigación por perspectivas** (opcional): antes de buscar, un puñado de perspectivas distintas sobre el tema (practicante, escéptico, regulador...) aportan cada una unas pocas preguntas, fusionadas con las subpreguntas habituales y acotadas al presupuesto de consultas de siempre.
- **Revisión ciega de un informe de investigación terminado** (opcional): un segundo modelo puntúa el informe solo a partir de la pregunta, el informe sin sus secciones de proceso y las fuentes citadas — nunca el plan ni la identidad de quien lo escribió — y registra afirmaciones sin respaldo y la brecha con la nota de evidencia propia de quien escribió.
- **Comprobador de afirmaciones de la documentación**: extrae afirmaciones de ruta, símbolo, clave de ajuste y ruta de API de los docs en Markdown, las contrasta con el workspace real, y marca referencias rotas y secciones cuyo código citado cambió después de escribirse.
- **Conceptos de proyecto**: un grafo persistente, por proyecto, de conceptos de arquitectura (feature/módulo/patrón/config/decisión/componente) que el propio agente escribe a medida que aprende el código, consultable semánticamente (`concepts_understand`) para que el conocimiento sobreviva entre sesiones — con relaciones tipadas, detección de obsolescencia contra el workspace real, inyección de contexto opcional y una vista de grafo dirigido por fuerzas en Studio (Contexto → Conceptos).
- Conservar extractos originales identificados cuando falla la extracción, mantener el informe anterior si la generación final queda vacía y aportar evidencia acotada junto a los resúmenes. Las búsquedas programadas compartidas se recuperan de una cancelación sin dejar bloqueadas otras tareas. [Adaptaciones de Diogenes](docs/design/diogenes-adaptations.md).
- Escribir y editar documentos; exportar contenido compatible a Markdown, texto, HTML, PDF, DOCX o JSON.
- Buscar en historiales importados de ChatGPT, Claude, LM Studio y Faustus junto al conocimiento local.
- Organizar notas, tareas y calendarios; conectar correo con IMAP/SMTP y calendarios mediante CalDAV.
- Comparar modelos, ejecutar revisiones de expertos y consultar procedencia y reglas aprendidas. La memoria avisa cuando una regla nueva contradice una antigua — el ítem antiguo pierde prioridad y queda marcado como contradicho, y se resuelve (quedarse con el nuevo / con el antiguo / con ambos) desde una sección Conflicts en la pantalla Memoria; nada se descarta en silencio.
- **Lint de respaldo (Grounding)**: cada ítem activo de memoria con evidencia citada se comprueba, de forma determinista, en busca de datos concretos que su evidencia no contiene — números, fechas, cadenas entre comillas y nombres propios se extraen y se contrastan contra el extracto citado. Los ítems marcados y sus datos sin respaldo aparecen en una sección Grounding junto a Conflicts en la pantalla Memoria; los ítems sin ninguna evidencia se cuentan aparte y nunca se acusan.
- Vigilar la memoria de los modelos locales, la colocación en GPU, las estimaciones de capacidad, las descargas y la salud de los servicios desde Cookbook. El informe de runners inactivos distingue un proceso abandonado de un motor que estás usando: un runner que escucha en el puerto de uno de tus endpoints configurados está poseído, lo arrancara quien lo arrancara, así que nunca se informa como abandonado ni se ofrece para liberar.
- Arrancar servidores locales con una configuración verificada: la arquitectura del modelo (denso/MoE, predicción multi-token) se lee de los metadatos y no del nombre, cada opción de arranque se comprueba contra un manifiesto de capacidades por implementación antes de construir el comando, y un recibo de arranque muestra lo solicitado, lo aplicado y lo que el servidor confirmó. Cada respuesta lleva sus fases medidas (cola, carga, prefill, generación, herramientas) con su fuente bajo «¿Por qué ha tardado tanto?»; una fase que el motor no reporta aparece como ausente, nunca como cero.
- **Los motores llama.cpp gestionados se arrancan solos en la primera llamada de chat y se apagan al quedarse quietos** (`engine_autostart`, `engine_idle_ttl_minutes`), así que un motor parado no hay que arrancarlo a mano y uno inactivo deja de ocupar la GPU. Los motores que lo soportan pueden activar **decodificación especulativa MTP** (detectada del propio GGUF, nunca adivinada por el nombre del fichero), con un aviso cuando los slots paralelos por defecto del servidor cancelarían la mayor parte de la ganancia de velocidad.
- Medir una configuración en marcha en tu propio equipo desde Cookbook → *Optimizar para mi equipo*: plan y presupuesto explícitos antes de ejecutar nada, un solo modelo a la vez, comprobaciones de calidad deterministas (sin juez LLM) y un comparador que solo marca un perfil como recomendado cuando la velocidad mejora por encima del ruido observado sin perder calidad.

### Flujos de trabajo duraderos

Combina disparadores, condiciones, esperas, aprobaciones humanas, pasos multimedia e informes guardados. La continuación del servidor hace avanzar los flujos de trabajo iniciados y despierta las esperas vencidas. Los intentos tienen reservas temporales y escrituras condicionales para que una respuesta tardía no pise una cancelación ni un intento más nuevo.

Dos pasos de espera más sondean en vez de dormir hasta un instante fijo: `wait_until` reevalúa el mismo lenguaje de condiciones que `condition`, a intervalos, hasta que se cumple, con un timeout acotado que hace fallar el nodo o lo completa por una rama de timeout declarada; `wait_for_event` vigila una fuente de eventos (trae de serie un escáner `file_change`, y se pueden conectar más) y solo completa tras una ventana de quietud desde el último evento que encaje, con su propio timeout total y una lista acotada de eventos recogidos. Ambos guardan su plazo y su progreso en el propio estado del nodo pausado, así que un reinicio del proceso a mitad de la espera continúa exactamente donde se quedó, y un run atascado pasado su plazo tras una caída lo encuentra y resuelve la misma continuación periódica que ya despierta un `wait` normal.

Los informes generados pueden tomar texto de las entradas de la ejecución o de resultados anteriores y guardarlo como un artefacto propio. Los resultados del flujo de trabajo heredan el ámbito verificado de proyecto y conversación. Actividad muestra las dependencias, los motivos de espera y los enlaces a los archivos de salida.

Los flujos de trabajo de proyecto pueden ejecutar scripts declarados de skills en Python, JavaScript y Bash dentro de Docker, con una instantánea acotada del código fuente, permisos vinculados exactamente al código y al comando, y recogida de los archivos de salida. La [guía de scripts de skills](docs/design/workflow-script-skills.md) describe el contrato y los requisitos previos. La salida de los procesos se drena de forma continua y se conserva como colas acotadas, para que un script verboso no agote la memoria del equipo anfitrión.

Cancelar un flujo de trabajo de scripts en ejecución también detiene su contenedor. El worker comprueba el intento y la reserva exactos que siguen vivos, y cancelar durante la preparación del contenedor impide que el script llegue a arrancar. Los efectos externos parciales quedan explícitos y no se reintentan automáticamente.

Las credenciales de scripts se cifran por propietario y se vinculan explícitamente por nombre y revisión a las aprobaciones. Rotarlas invalida una ejecución pendiente; los flujos de trabajo nunca heredan claves de otros proveedores. Los endpoints de credenciales exclusivos para humanos y su configuración se describen en la guía de scripts de skills.

Un pase de sueño de skills (`src/skills_runtime/sleep_optimize.py`, a demanda desde la pestaña "Proposals" de la skill o vía `POST /api/skills/{id}/sleep-pass`) mina sesiones recientes buscando turnos donde una skill se consultó (actividad de `manage_skills` grabada en el turno), clasifica la siguiente respuesta del usuario con tablas deterministas de frases en/es (negativas: "no funciona", "sigue roto", señales de deshacer/reintentar; positivas: "perfecto", "funciona", "gracias") más cualquier error de herramienta de ese turno, y le pide a un modelo local UNA revisión propuesta del `SKILL.md`. Nunca se aplica nada automáticamente: la propuesta se valida (frontmatter/nombre conservados, tamaño acotado, sin eliminar la sección de Pitfalls/Verification, escaneo de seguridad) y queda pendiente, y aprobarla pasa por la misma puerta de gobernanza/promoción de skills más un historial de versiones reversible byte a byte.

Los objetivos de proyecto admiten cambios tipados de agentes sin perder actualizaciones simultáneas. Se protegen las ediciones humanas frente a cambios de agentes basados en una versión anterior, incluso dentro del mismo segundo, y se conservan los archivos de estado dañados durante su recuperación.

El [paso de envío de correo](docs/design/workflow-email-delivery.md) envía texto con HTML opcional, CC/BCC y adjuntos acotados del propio flujo mediante una cuenta SMTP del propietario. La aprobación vincula todos los destinatarios y el contenido exacto, incluidas las huellas de los adjuntos. Los permisos se consumen una vez al ejecutar, las esperas aprobadas continúan automáticamente y un efecto externo incierto no se reintenta por sí solo. La aceptación del servidor SMTP se distingue de la entrega en la bandeja de entrada.

Los nodos del flujo de trabajo que ejecutan acciones externas siguen necesitando la capacidad configurada y la autorización correspondientes. Cancelar impide el trabajo posterior; no puede deshacer una acción externa que ya ocurrió.

La pantalla **Flujos de trabajo** dibuja una definición como un grafo por capas con tres modos explícitos —diseño, simulación estructural, ejecución real autorizada— sobre la misma definición en todo momento. La simulación recorre el grafo ronda a ronda sin efectos secundarios, deja las condiciones no decididas como no decididas en lugar de suponerlas, y nunca se salta una dependencia AND porque una segunda vía resultara limpia. Las definiciones se importan desde la exportación canónica de este módulo o desde un grafo con forma de aigraphstudio, se exportan de vuelta, y enlazan directamente con una ejecución.

![Lienzo de flujos de trabajo en simulación estructural](assets/screens/workflows.png)

### Historial de adaptación y demos

Tres recorridos comprobables sin conexión —un documento con revisión real, un agente supervisado de principio a fin, control semántico de escritorio— se describen en [docs/showcase.md](docs/showcase.md), cada uno con las referencias a los archivos que lo implementan y el test que lo cubre, más un pequeño proyecto de ejemplo en `examples/showcase/`. El backlog de adaptación detrás de estas funciones, auditado fila por fila contra el código real en lugar de simplemente afirmado, está en [docs/adaptations/](docs/adaptations/) (clasificación de referencia y registros de decisión por función).

### Diseñar antes de codificar, y un turno que cuesta solo lo que necesita

La herramienta `design_canvas` obliga al modelo a declarar el diseño antes de
tocar un fichero: requisitos, entidades, el enfoque con la alternativa que
descartó y por qué, los ficheros que va a cambiar, operaciones, normas y
salvaguardas. El resultado se guarda en el grafo de conceptos del proyecto como
una decisión cuyas referencias son esos ficheros, de modo que cuando uno deja de
existir la comprobación de obsolescencia encuentra el diseño que ya no cuadra.
Lo escribe el modelo con el que ya corre el turno, no el que sea el por defecto
global.

En el otro extremo, un turno que es claramente conversación se responde sin el
razonamiento del modelo y sin el juego de herramientas adjunto. Medido en un 27B
local con la caché de prompt fría: 7,5 s llevando las herramientas contra
2,2 s sin ellas. Una
conversación que ya ha llamado a una herramienta lo conserva todo, diga lo que
diga su último mensaje.

La memoria se niega a archivar una foto fija del workspace como un hecho sobre
ti. Un recuento de los ficheros de una carpeta deja de ser verdad en cuanto
añades uno, y un hecho guardado que contradice la realidad es peor que no tener
ninguno.

**Decisiones tipadas.** Donde una regla de palabras clave no está segura,
Faustus puede hacer una pregunta cerrada en vez de adivinar: las opciones van
etiquetadas con una sola letra, el modelo de utilidad ya cargado lee el texto
una vez, y la probabilidad que pone en cada letra para su siguiente token es la
respuesta, con una confianza y una «masa» (¿quería siquiera contestar esto?), sin
generar razonamiento. Varias preguntas sobre el mismo texto comparten el prefijo
del prompt, así que la caché del servidor paga el texto una sola vez. Nunca carga
un modelo, tiene un presupuesto de latencia duro y es sólo consultiva: la regla
decide siempre que está segura y siempre que el modelo no está disponible o duda.
Hoy resuelve los casos dudosos de «¿esta pregunta necesita buscar en la web?», da
tipo a las entidades del segundo cerebro que quedaron como «otro» y sugiere (nunca
resuelve) contradicciones de memoria que las reglas fijas no ven. Una evaluación
compara la precisión de la regla, la decisión y la combinación, con calibración y
tiempos de caché. [API de decisiones tipadas](docs/api/typed_decision.md),
[evaluación](docs/evals/typed-decisions.md).

## Voz

Jarvis ofrece una sesión de voz con reconocimiento español/inglés, respuestas habladas, controles de interrupción y esfera visual reactiva. Configura los servicios de transcripción y síntesis disponibles en la app; el navegador necesita permiso para usar el micrófono. Las voces instaladas y los motores locales de voz determinan los idiomas y la reproducción disponibles.

Proveedores de voz: navegador (Web Speech API), sistema (voces de Windows instaladas, sin conexión), local (Kokoro TTS / faster-whisper STT), **Piper** (síntesis neuronal totalmente local, licencia MIT, con voces en español — instala el motor y descarga una voz desde Configuración → Voz, sin modelo incluido de fábrica; la síntesis siempre corre en un proceso hijo aislado, nunca dentro del servidor), **command** (ejecuta tu propio ejecutable local de TTS/STT mediante una plantilla configurable), o un endpoint de API configurado. Ver [docs/ui/voice.md](docs/ui/voice.md).

La voz entra en la misma conversación y el mismo flujo de permisos de herramientas que el texto. Una sesión de voz no autoriza de forma general acciones sobre archivos, escritorio o servicios externos.

Toda transcripción —tanto la voz de entrada como las notas de reunión— pasa por una limpieza determinista (`src/stt_cleanup.py`) antes de llegar a la conversación o a las notas: los segmentos repetidos se colapsan en uno, las frases conocidas de silencio/alucinación (español/inglés) y los marcadores de música se descartan, y un bucle de palabra o frase dentro de un segmento ("la la la la…") se colapsa, manteniendo coherentes las marcas de tiempo.

App de escritorio de Windows: un atajo global de «dictar en cualquier lugar» (desactivado por defecto; Configuración → Voz) transcribe en la app que tenga el foco —el correo, una terminal, otro programa— no solo en el cuadro de texto de Studio. Captura la ventana objetivo antes de grabar y entrega el texto pegándolo (el portapapeles se guarda y se restaura después) o escribiéndolo directamente, para apps que bloquean el pegado (`src/dictation_paste.py`, `routes/dictation_routes.py`).

## Arquitectura

| Área | Código |
| --- | --- |
| Aplicación HTTP y persistencia | [app.py](app.py), [routes](routes/), [core](core/) |
| Interfaz Studio y estado | [studio/src](studio/src/) |
| Transporte de modelos y puente de clientes oficiales | [llm_core.py](src/llm_core.py), [cli_model.py](src/cli_model.py), [runner_billing.py](src/runner_billing.py) |
| Delegación y supervisión de procesos | [dispatch.py](src/dispatch.py), [external_worker.py](src/external_worker.py) |
| Flujos de trabajo duraderos | [workflows](src/workflows/) |
| Motores multimedia y continuación | [media_runs.py](src/media_runs.py), [media_scheduler.py](src/media_scheduler.py), [media_backends](src/media_backends/) |
| Artefactos, identidad y migración | [artifact_store.py](src/artifact_store.py), [artifact_identity.py](src/artifact_identity.py), [artifact_migration.py](src/artifact_migration.py) |
| Regresiones y comprobaciones de interfaz | [tests](tests/), [studio/checks](studio/checks/) |

El almacén de artefactos separa los bytes identificados por contenido de las ocurrencias de resultados con propietario. La migración aditiva conserva identificadores y metadatos históricos; las marcas de eliminación impiden recrear ocurrencias borradas por la migración.

Se mantienen nombres internos como `odysseus`, `ODYSSEUS_*`, rutas de API existentes y claves de almacenamiento por compatibilidad. El nombre del producto es Faustus.

## Desarrollo y verificación

Usa el entorno Python configurado del proyecto e instala las dependencias de desarrollo indicadas en [tests/README.md](tests/README.md).

```bash
python -m pytest
npm ci
npx tsc --noEmit
npm run build
python -m src.doctor
```

La suite incluye aislamiento entre propietarios, persistencia, concurrencia, cancelación, migración, integración HTTP y comprobaciones de interfaz sin navegador. Los motores físicos y las integraciones de cuentas requieren además pruebas en su entorno configurado; un fixture HTTP no demuestra la calidad de un modelo ni el acceso a la cuenta de un proveedor.

Medido el 12 de septiembre de 2026: pasan 16.993 tests en Linux (`-m "not slow"`, unos diez minutos en dos workers), y el mismo árbol pasa en Windows con Docker Desktop ejecutando los casos que dependen de contenedores; 295 módulos bajo `src/`, 116 módulos de rutas, 149 pantallas de Studio, 21 documentos de API bajo `docs/api/`, y 76 secciones fechadas en `FAUSTUS.md` que registran para qué sirvió cada cambio y cómo se verificó. Las comprobaciones de interfaz ejecutan el TypeScript real a través de esbuild bajo Node, en lugar de reimplementarlo en Python.

`python scripts/acceptance_run.py` ejecuta los 36 casos de aceptación de paridad (A01-A36) y escribe una fila de evidencia por caso; `python scripts/benchmark_matrix.py` corre una matriz de benchmark caso × sistema × modelo que reporta una celda ausente como `MISSING` en vez de reducir el denominador, y un coste no priceable como `"unknown"`, nunca `0`.

Las verificaciones más recientes y las comprobaciones que faltan están en [PENDIENTES.md](PENDIENTES.md).

## Capturas

<details>
<summary>Actividad, agentes y automatizaciones</summary>

Sigue tareas y aprobaciones en Actividad.

![Actividad: estados y resultados](assets/screens/activity.png)

Configura agentes, clientes y sus comprobaciones.

![Agentes: configuración de workers](assets/screens/agents.png)

Revisa y controla el trabajo recurrente.

![Automatizaciones: tareas activas y pausadas](assets/screens/automations.png)

</details>

<details>
<summary>Segundo cerebro</summary>

`/brain` («Cerebro» en Herramientas) es una bóveda markdown de tres paneles: un explorador de ficheros a la izquierda (carpetas, buscador, etiquetas, papelera), la nota actual en el centro (una vista de lectura con `[[enlaces wiki]]` pulsables —uno sin resolver crea la nota al pulsarlo— y una vista de edición con autoguardado y autocompletar `[[`), y a la derecha sus backlinks, enlaces salientes, etiquetas y, para una nota de entidad, sus relaciones vigentes frente a su historial con un selector de fecha «as of». Un interruptor cambia a un grafo de fuerza dirigida de toda la bóveda o del entorno local de una nota.

</details>

## Seguridad y datos

El resumen de cada paquete de contexto incluye un registro de selección de memoria: entradas incluidas, omisiones y motivos, caracteres utilizados y degradación. Describe el paquete final del Motor de contexto, sin volver a consultar la memoria ni añadir una segunda pasada de selección.

Mantén `AUTH_ENABLED=true` en cualquier despliegue accesible por red.
Mantén `LOCALHOST_BYPASS=false` fuera del desarrollo local.

Mantén la autenticación activada. No publiques puertos sin autenticación de modelos, ComfyUI ni servicios internos. No subas credenciales ni datos privados de `data/` a Git.

Las sesiones de Bitwarden se guardan cifradas y caducan tras una hora, tanto en ajustes como en las herramientas del agente. Las sesiones antiguas en claro se eliminan al acceder y requieren desbloquear de nuevo. Protege `data/.app_key`: el cifrado no protege frente a un equipo comprometido.

Aprueba las herramientas e instrucciones del proyecto de forma deliberada. Las restricciones por agente, las fuentes propias, las aprobaciones, la configuración del sandbox y la supervisión de procesos son controles distintos. Si falta un sandbox requerido, la ruta de sandbox configurada no debe ejecutar silenciosamente el comando en el anfitrión.

Un servidor MCP de terceros o una skill importada pasan por un pre-escaneo estático de seguridad (sin red, sin LLM) antes de poder confiar en ellos: ejecución de script remoto, payload ofuscado, acceso a credenciales, exfiltración, persistencia, comandos destructivos, código dinámico, listener de red abierto, e indicios de inyección de instrucciones en las descripciones de herramientas o en el texto de la skill. Un hallazgo crítico lo pone en cuarentena — la misma vía que ya usa una escalada de permisos — y aprobarlo exige una confirmación explícita, mostrada junto al recuento de hallazgos, nunca un bloqueo silencioso.

Una batería repetible de sondas de inyección de instrucciones (`python -m src.security_probes`) comprueba las defensas de extremo a extremo, no una función aislada: 12 sondas basadas en datos cubren cada vector no confiable (páginas descargadas, documentos, cuerpos de correo, resultados de herramientas, descripciones de herramientas MCP, ficheros del workspace) y cada estilo de payload (anulación directa, mensajes de sistema falsificados, comentarios HTML ocultos, contrabando con caracteres de ancho cero/etiqueta Unicode, URLs de exfiltración en markdown, jailbreaks de rol, instrucciones de "llama a esta herramienta", instrucciones para desactivar ajustes, y disparadores diferidos entre turnos). Un modo determinista corre en CI sin modelo — un modelo "siempre comprometido" guionizado que intenta la llamada maliciosa exacta en cuanto ve el contenido inyectado — y un modo en vivo ejecuta las mismas sondas contra un modelo local real y el bucle de agente real; los resultados y un resumen en markdown se guardan bajo `DATA_DIR/security_probes/` para seguimiento de regresiones.

Una puerta de trayectoria (`src/trajectory_gate.py`, `GET`/`POST /api/agent-runs/{run_id}/gate`) comprueba una ejecución de agente grabada contra aserciones declarativas estilo CI — tasa máxima de error, topes de pasos/llamadas a herramienta/tokens/duración, herramientas prohibidas/requeridas, reglas de orden ("`write_file` necesita una lectura previa", "las pruebas deben correr después de la última edición"), sin llamadas idénticas repetidas, respuesta final requerida — así que una regresión en el comportamiento del agente se detecta mecánicamente, no a ojo. `python -m src.trajectory_gate --run <id> --spec spec.json` (o `--recent 20` para agregar la tasa de aciertos por aserción) corre en CI con código de salida distinto de cero si falla; un panel compacto "Gate a run" en Actividad muestra las mismas aserciones para cualquier id de ejecución.

Revisión con duda (`src/doubt_review.py`, apagada por defecto: `agent_doubt_review`) le pide a un revisor de contexto fresco y sin herramientas — sin historial de conversación, solo la tarea, el resumen de riesgo `code_graph_risk` de un fichero de riesgo ALTO y el diff propuesto — que busque razones por las que un cambio no trivial está mal, con sesgo hacia refutar en vez de aprobar. Informativo por defecto: el edit se aplica y el veredicto se añade al resultado de la herramienta como "Second look..."; `agent_doubt_review_block` puede en cambio rechazar la escritura ante un veredicto "concerns", reversible por llamada con `confirm_risky: true`. Barato: una caché por turno deja el cálculo de riesgo y la revisión en una sola llamada por fichero/diff, y `agent_doubt_review_model` apunta a un modelo local pequeño en vez de gastar el modelo principal del turno en cada edición.

Consulta el [modelo de amenazas](THREAT_MODEL.md), la [política de seguridad](SECURITY.md) y las [notas de seguridad de instalación](website/setup.md#security-notes).

## Créditos y licencia

Faustus parte de [Odysseus](https://github.com/odysseus-dev/odysseus). Más créditos en [ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md), contribuciones en [CONTRIBUTING.md](CONTRIBUTING.md) y licencia **AGPL-3.0-or-later** en [LICENSE](LICENSE).
