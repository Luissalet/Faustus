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

### Herramientas creativas dentro de la conversación

- **Señalar para editar:** selecciona un punto en una captura del navegador y describe el cambio. Se añaden al borrador una captura anotada y la procedencia de la captura, sin enviarla automáticamente. El agente debe inspeccionar la página actual y el proyecto; las coordenadas de la captura no son ubicaciones inventadas en el código fuente.
- **Referencias visuales de proyecto:** guarda `@referencias` con nombre como enlaces de contexto de proyecto, distingue sujeto, estilo y composición, y adjúntalas explícitamente desde el selector. Quitar el enlace de una referencia no elimina su imagen de la galería.
- **Aprender un estilo:** extrae reglas de estilo editables a partir de ejemplos en TXT/Markdown usando el modelo elegido, compara la respuesta base con la respuesta con estilo y guarda y selecciona un preset. La comparación hace dos llamadas al modelo usando la conexión seleccionada.
- **Vídeo local:** transcribe con un modelo Whisper ya instalado, edita o traduce manualmente los segmentos con marca de tiempo, exporta SRT/VTT, y renderiza narración usando las voces de Windows en inglés o español instaladas. Sin API en la nube ni descargas automáticas de modelos. Las entradas están acotadas a 64 MB, 3 minutos y 1080p; se requiere FFmpeg/FFprobe. Esto es narración local práctica, no clonación de voz ni sincronización labial. La exportación narrada sustituye el audio original.

### Chat y panel de trabajo persistente

- Alternar entre modelos locales y de API; conectar OpenAI, Claude, Gemini y OpenRouter con configuración guiada y prueba de conexión. Las llamadas a OpenRouter informan de su coste real, las preferencias por endpoint (recopilación de datos, orden de proveedores, búsqueda web solo cuando se pide) son explícitas, y un perfil de privacidad solo local rechaza las llamadas salientes en lugar de degradarlas en silencio.
- Pegar capturas directamente con **Ctrl+V**, adjuntar archivos y mencionar ficheros del workspace.
- Navegar por chats largos con la barra de mensajes: previsualización al pasar el cursor, clic o arrastre para saltar, o usar flechas, Inicio/Fin y RePág/AvPág. Consultar mensajes anteriores pausa el seguimiento automático del flujo.
- Leer el Markdown generado junto al chat, editarlo y guardarlo con detección de conflictos. Los borradores sin guardar pertenecen a su conversación y sobreviven a la navegación entre paneles. Los tres diseños de Studio (conversación, documento, revisión) comparten una misma sesión de documento, de modo que una selección en el documento se convierte en un cable de contexto en el redactor, y una sugerencia del agente aterriza en su anclaje, nunca en la primera línea que coincida.
- Mantener archivos, resultados generados, fuentes, contexto del proyecto, actividad de agentes y capturas del navegador en un panel lateral redimensionable.
- Cambiar a otra conversación mientras el servidor sigue ejecutando el turno actual. Ver la posición en cola, la actividad en curso, el uso de herramientas y las solicitudes de permiso; reconectar con el trabajo existente. Una tarjeta de aprobación cuyo permiso murió con un reinicio lo indica, en lugar de ofrecer botones muertos.
- Cada turno muestra la estrategia que eligió el agente (edición directa, planificar y ejecutar, investigación, revisión especializada, explorar alternativas) y por qué; un turno que merece repetirse puede guardarse como receta con sus entradas reales.
- Buscar y navegar con **Ctrl+K**. Usar español o inglés, temas, controles de densidad, tamaño de texto ajustable y movimiento reducido.

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
- **Pide herramientas en español o inglés llano.** Cada herramienta integrada lleva frases de ejemplo y sinónimos del dominio, las herramientas que nombras o insinúas siempre se ofrecen, y un mensaje vago con un workspace nunca obtiene `bash` o `write` solo por la fuerza de un embedding.
- **Alternativas:** prueba varios enfoques para el mismo cambio en worktrees aislados o copias congeladas, compáralos con la base señalando los archivos en disputa, y aplica uno con una fusión a tres bandas: una edición manual hecha mientras tanto sobrevive. Las alternativas de documento funcionan igual sobre versiones de documento. [API de alternativas](docs/api/alternatives.md).
- **Control semántico del escritorio** a través del árbol de accesibilidad en primer lugar, y de píxeles solo como alternativa explícita y más arriesgada; recuperar el control del escritorio invalida las referencias obsoletas del agente. [Semántica de escritorio](docs/api/desktop_semantics.md).

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

- Investigar con seguimiento de fuentes, comprobación de citas y exportación de informes.
- Conservar extractos originales identificados cuando falla la extracción, mantener el informe anterior si la generación final queda vacía y aportar evidencia acotada junto a los resúmenes. Las búsquedas programadas compartidas se recuperan de una cancelación sin dejar bloqueadas otras tareas. [Adaptaciones de Diogenes](docs/design/diogenes-adaptations.md).
- Escribir y editar documentos; exportar contenido compatible a Markdown, texto, HTML, PDF, DOCX o JSON.
- Buscar en historiales importados de ChatGPT, Claude, LM Studio y Faustus junto al conocimiento local.
- Organizar notas, tareas y calendarios; conectar correo con IMAP/SMTP y calendarios mediante CalDAV.
- Comparar modelos, ejecutar revisiones de expertos y consultar procedencia y reglas aprendidas.
- Vigilar la memoria de los modelos locales, la colocación en GPU, las estimaciones de capacidad, las descargas y la salud de los servicios desde Cookbook.
- Arrancar servidores locales con una configuración verificada: la arquitectura del modelo (denso/MoE, predicción multi-token) se lee de los metadatos y no del nombre, cada opción de arranque se comprueba contra un manifiesto de capacidades por implementación antes de construir el comando, y un recibo de arranque muestra lo solicitado, lo aplicado y lo que el servidor confirmó. Cada respuesta lleva sus fases medidas (cola, carga, prefill, generación, herramientas) con su fuente bajo «¿Por qué ha tardado tanto?»; una fase que el motor no reporta aparece como ausente, nunca como cero.
- Medir una configuración en marcha en tu propio equipo desde Cookbook → *Optimizar para mi equipo*: plan y presupuesto explícitos antes de ejecutar nada, un solo modelo a la vez, comprobaciones de calidad deterministas (sin juez LLM) y un comparador que solo marca un perfil como recomendado cuando la velocidad mejora por encima del ruido observado sin perder calidad.

### Flujos de trabajo duraderos

Combina disparadores, condiciones, esperas, aprobaciones humanas, pasos multimedia e informes guardados. La continuación del servidor hace avanzar los flujos de trabajo iniciados y despierta las esperas vencidas. Los intentos tienen reservas temporales y escrituras condicionales para que una respuesta tardía no pise una cancelación ni un intento más nuevo.

Los informes generados pueden tomar texto de las entradas de la ejecución o de resultados anteriores y guardarlo como un artefacto propio. Los resultados del flujo de trabajo heredan el ámbito verificado de proyecto y conversación. Actividad muestra las dependencias, los motivos de espera y los enlaces a los archivos de salida.

Los flujos de trabajo de proyecto pueden ejecutar scripts declarados de skills en Python, JavaScript y Bash dentro de Docker, con una instantánea acotada del código fuente, permisos vinculados exactamente al código y al comando, y recogida de los archivos de salida. La [guía de scripts de skills](docs/design/workflow-script-skills.md) describe el contrato y los requisitos previos. La salida de los procesos se drena de forma continua y se conserva como colas acotadas, para que un script verboso no agote la memoria del equipo anfitrión.

Cancelar un flujo de trabajo de scripts en ejecución también detiene su contenedor. El worker comprueba el intento y la reserva exactos que siguen vivos, y cancelar durante la preparación del contenedor impide que el script llegue a arrancar. Los efectos externos parciales quedan explícitos y no se reintentan automáticamente.

Las credenciales de scripts se cifran por propietario y se vinculan explícitamente por nombre y revisión a las aprobaciones. Rotarlas invalida una ejecución pendiente; los flujos de trabajo nunca heredan claves de otros proveedores. Los endpoints de credenciales exclusivos para humanos y su configuración se describen en la guía de scripts de skills.

Los objetivos de proyecto admiten cambios tipados de agentes sin perder actualizaciones simultáneas. Se protegen las ediciones humanas frente a cambios de agentes basados en una versión anterior, incluso dentro del mismo segundo, y se conservan los archivos de estado dañados durante su recuperación.

El [paso de envío de correo](docs/design/workflow-email-delivery.md) envía texto con HTML opcional, CC/BCC y adjuntos acotados del propio flujo mediante una cuenta SMTP del propietario. La aprobación vincula todos los destinatarios y el contenido exacto, incluidas las huellas de los adjuntos. Los permisos se consumen una vez al ejecutar, las esperas aprobadas continúan automáticamente y un efecto externo incierto no se reintenta por sí solo. La aceptación del servidor SMTP se distingue de la entrega en la bandeja de entrada.

Los nodos del flujo de trabajo que ejecutan acciones externas siguen necesitando la capacidad configurada y la autorización correspondientes. Cancelar impide el trabajo posterior; no puede deshacer una acción externa que ya ocurrió.

La pantalla **Flujos de trabajo** dibuja una definición como un grafo por capas con tres modos explícitos —diseño, simulación estructural, ejecución real autorizada— sobre la misma definición en todo momento. La simulación recorre el grafo ronda a ronda sin efectos secundarios, deja las condiciones no decididas como no decididas en lugar de suponerlas, y nunca se salta una dependencia AND porque una segunda vía resultara limpia. Las definiciones se importan desde la exportación canónica de este módulo o desde un grafo con forma de aigraphstudio, se exportan de vuelta, y enlazan directamente con una ejecución.

![Lienzo de flujos de trabajo en simulación estructural](assets/screens/workflows.png)

### Historial de adaptación y demos

Tres recorridos comprobables sin conexión —un documento con revisión real, un agente supervisado de principio a fin, control semántico de escritorio— se describen en [docs/showcase.md](docs/showcase.md), cada uno con las referencias a los archivos que lo implementan y el test que lo cubre, más un pequeño proyecto de ejemplo en `examples/showcase/`. El backlog de adaptación detrás de estas funciones, auditado fila por fila contra el código real en lugar de simplemente afirmado, está en [docs/adaptations/](docs/adaptations/) (clasificación de referencia y registros de decisión por función).

## Voz

Jarvis ofrece una sesión de voz con reconocimiento español/inglés, respuestas habladas, controles de interrupción y esfera visual reactiva. Configura los servicios de transcripción y síntesis disponibles en la app; el navegador necesita permiso para usar el micrófono. Las voces instaladas y los motores locales de voz determinan los idiomas y la reproducción disponibles.

La voz entra en la misma conversación y el mismo flujo de permisos de herramientas que el texto. Una sesión de voz no autoriza de forma general acciones sobre archivos, escritorio o servicios externos.

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

## Seguridad y datos

El resumen de cada paquete de contexto incluye un registro de selección de memoria: entradas incluidas, omisiones y motivos, caracteres utilizados y degradación. Describe el paquete final del Motor de contexto, sin volver a consultar la memoria ni añadir una segunda pasada de selección.

Mantén `AUTH_ENABLED=true` en cualquier despliegue accesible por red.
Mantén `LOCALHOST_BYPASS=false` fuera del desarrollo local.

Mantén la autenticación activada. No publiques puertos sin autenticación de modelos, ComfyUI ni servicios internos. No subas credenciales ni datos privados de `data/` a Git.

Las sesiones de Bitwarden se guardan cifradas y caducan tras una hora, tanto en ajustes como en las herramientas del agente. Las sesiones antiguas en claro se eliminan al acceder y requieren desbloquear de nuevo. Protege `data/.app_key`: el cifrado no protege frente a un equipo comprometido.

Aprueba las herramientas e instrucciones del proyecto de forma deliberada. Las restricciones por agente, las fuentes propias, las aprobaciones, la configuración del sandbox y la supervisión de procesos son controles distintos. Si falta un sandbox requerido, la ruta de sandbox configurada no debe ejecutar silenciosamente el comando en el anfitrión.

Consulta el [modelo de amenazas](THREAT_MODEL.md), la [política de seguridad](SECURITY.md) y las [notas de seguridad de instalación](website/setup.md#security-notes).

## Créditos y licencia

Faustus parte de [Odysseus](https://github.com/odysseus-dev/odysseus). Más créditos en [ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md), contribuciones en [CONTRIBUTING.md](CONTRIBUTING.md) y licencia **AGPL-3.0-or-later** en [LICENSE](LICENSE).
