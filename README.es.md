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

Local-first significa que eliges dónde se ejecuta la inferencia. Las APIs y los clientes oficiales autenticados consumen la facturación o cuota del proveedor; la inferencia local utiliza tu hardware. Elegir un proveedor remoto le envía el contexto necesario para esa petición.

## Inicio rápido

Instala Docker y Docker Compose:

```bash
git clone https://github.com/Luissalet/Faustus.git
cd Faustus
cp .env.example .env
docker compose up -d --build
```

En PowerShell puedes usar `Copy-Item .env.example .env` en lugar de `cp`.

Abre **http://localhost:7000**. La contraseña inicial de administrador aparece en `docker compose logs odysseus`. Compose incluye los servicios de búsqueda y almacenamiento vectorial; no descarga un modelo de lenguaje por ti.

1. Conecta tu servidor de modelos desde Ajustes o Cookbook. Si Ollama está en el equipo anfitrión de Docker, configura su dirección accesible siguiendo la [guía de instalación](website/setup.md).
2. Crea un proyecto y añade los archivos, documentos o fuentes que debe conocer.
3. Abre una conversación. Elige **Chat** para conversar o **Agente** para herramientas y acciones.
4. Consulta **Actividad** para seguir conversaciones, workflows, renders y aprobaciones.
5. Añade motores de imagen/vídeo, servicios de voz o clientes de agentes externos cuando los necesites.

Instalación nativa, Windows/macOS, GPU, HTTPS y configuración: [guía de instalación](website/setup.md). Conserva tu carpeta `data/` y haz una copia de seguridad antes de actualizar.

## Qué puedes hacer

### Chat y panel de trabajo persistente

- Alternar entre modelos locales y APIs; conectar OpenAI, Claude y Gemini con configuración guiada y prueba de conexión.
- Pegar capturas directamente con **Ctrl+V**, adjuntar archivos y mencionar ficheros del workspace.
- Leer el Markdown generado junto al chat, editarlo y guardarlo con detección de conflictos. Los borradores pertenecen a su conversación y se conservan al navegar por el panel.
- Reunir archivos, resultados, fuentes, contexto, actividad de agentes y capturas del navegador en un panel lateral ajustable.
- Cambiar de conversación mientras el servidor continúa el turno. Ver la posición en cola, la actividad, las herramientas y las solicitudes de permiso; reconectar con el trabajo existente.
- Buscar y navegar con **Ctrl+K**. Usar español o inglés, temas, densidad configurable, tamaño de texto y movimiento reducido.

### Programación, herramientas y equipos de agentes

- Definir un **orquestador y sus minions desde el chat**, con modelos, roles y restricciones de herramientas.
- Delegar a conversaciones hijas con propiedad de archivos, progreso, cancelación y dirección durante la tarea.
- Utilizar **Consejo** para rondas iniciales ciegas, crítica y síntesis entre varios modelos, con un ejecutor designado para las acciones.
- Usar skills, MCP, herramientas de archivos, shell y navegador bajo los permisos configurados.
- Conectar workers de **Codex CLI y Claude Code** mediante el sistema de runners y dispatch. Ambos disponen también de conexiones privadas de texto para el chat de Faustus, con elección explícita de suscripción/API. El chat de Codex usa un hilo efímero de App Server sin entorno de ejecución; Faustus conserva sus herramientas y aprobaciones.
- Elegir explícitamente suscripción o API. La autenticación del cliente oficial se comprueba antes del trabajo; un error de suscripción no activa una API de pago como alternativa silenciosa.
- Consultar evidencias de herramientas, comprobaciones de sintaxis, tests del proyecto y revisiones. Los checkpoints con git sombra permiten ver diferencias y restaurar sin sustituir el repositorio del proyecto.
- Volver a abrir la evidencia original desde el resumen del turno. Los registros aislados por propietario sobreviven a reinicios, distinguen guardar una evidencia de verificar un resultado y alimentan State Mirror; se excluyen los turnos incógnitos. [Historial de evidencias](docs/design/durable-change-evidence.md).

Estas comprobaciones aportan evidencia sobre acciones compatibles; no demuestran que toda afirmación del modelo sea verdadera. Elegir un modelo no habilita automáticamente herramientas, verificaciones ni ejecutores externos.

### Conocimiento del proyecto y contexto ampliado

La identidad del proyecto se guarda separada del nombre de su carpeta en la barra lateral. Un agente puede añadir un documento generado u otra fuente compatible al contexto de su proyecto mediante una referencia.

El motor de contexto recupera y distribuye material relevante entre fuentes del proyecto, historial y memoria. Registra procedencia, conflictos y cápsulas compactas en lugar de intentar introducir un disco entero en el prompt. El almacenamiento persistente amplía lo que se puede recuperar, **no la ventana nativa de contexto del modelo**.

### Once sistemas conectados

| Sistema | Para qué sirve | Implementación |
| --- | --- | --- |
| Project Context Links | Vincular chats y fuentes reutilizables a proyectos estables; añadir y resolver contexto por referencia. | [project_context](src/project_context/) |
| Context Engine | Recuperar, ordenar, distribuir y explicar el contexto de cada turno. | [context_engine](src/context_engine/) |
| Perfiles de agentes y modos de completado | Reutilizar especialistas y políticas según la tarea, dentro de los permisos existentes. | [agent_profiles](src/agent_profiles/) |
| Modo Enséñame | Convertir demostraciones en procedimientos reutilizables, con revisión y controles de ejecución. | [rutas de Enséñame](routes/teach_mode_routes.py) |
| Immune System | Registrar incidentes, evidencias y reglas correctivas para fallos recurrentes. | [immune_system](src/immune_system/) |
| Branching Futures | Explorar alternativas en ramas aisladas antes de elegir. | [branching_futures](src/branching_futures/) |
| Consejo | Organizar discusión, crítica, decisiones y ejecución controlada entre varios modelos. | [council](src/council/) |
| State Mirror | Mantener observaciones fechadas del proyecto, con vigencia y procedencia. | [state_mirror](src/state_mirror/) |
| Universal Delta Engine | Comparar los cambios solicitados con los observados en los ámbitos compatibles. | [delta_engine](src/delta_engine/) |
| Greedy Completion Engine | Descubrir y valorar trabajo adicional útil según modo, alcance y presupuesto. | [completion_engine](src/completion_engine/) |
| Voz Jarvis | Interacción oral en español e inglés, respuestas habladas y esfera reactiva. | [guía de voz](docs/design/voice-jarvis.md) |

Detalles de implementación: [FAUSTUS.md](FAUSTUS.md). Verificaciones actuales y cierre: [PENDIENTES.md](PENDIENTES.md).

### Imágenes, vídeo y audio

- Planificar y ejecutar **recetas aprobadas de ComfyUI** para imágenes, edición con referencias y vídeo corto. Comprobar los modelos necesarios antes de poner el trabajo en cola.
- Elegir entre varios motores configurados según disponibilidad, cola y capacidad, conservando el motivo de la elección.
- Guardar receta, versión, semilla, licencia del modelo, trabajo del motor y huella de las entradas con los artefactos generados.
- Recoger renders desde el servidor sin tener un chat abierto. Reintentar descargas interrumpidas y acceder a los resultados desde Actividad.
- Inspeccionar propiedades de imágenes, audio y vídeo antes de procesarlos.
- Convertir y redimensionar PNG/JPEG/WebP, y extraer audio WAV/MP3 con herramientas acotadas, progreso y cancelación.
- Descargar resultados mediante enlaces autorizados por propietario. Los mismos bytes pueden compartirse físicamente sin mezclar dueños ni procedencia.

ComfyUI es un servicio separado; los pesos de modelos, nodos adicionales y sus licencias no se incluyen. Consulta las [recetas multimedia](config/media_workflows/) y la [documentación de workers](website/fable-workers.md).

### Investigación, documentos y trabajo cotidiano

- Investigar con seguimiento de fuentes, comprobación de citas y exportación de informes.
- Escribir y editar documentos; exportar contenido compatible a Markdown, texto, HTML, PDF, DOCX o JSON.
- Buscar en historiales importados de ChatGPT, Claude, LM Studio y Faustus junto al conocimiento local.
- Organizar notas, tareas y calendarios; conectar correo IMAP/SMTP y calendarios CalDAV.
- Comparar modelos, ejecutar revisiones de expertos y consultar procedencia y reglas aprendidas.
- Ver memoria de modelos locales, colocación en GPU, estimaciones de capacidad, descargas y salud de servicios en Cookbook.

### Workflows duraderos

Los workflows de proyecto pueden ejecutar scripts declarados de skills en Python, JavaScript y Bash dentro de Docker, con una copia acotada del código, permisos vinculados al código y comando exactos y recogida de archivos resultantes. La [guía de scripts de skills](docs/design/workflow-script-skills.md) explica requisitos y contrato. La salida de los procesos se recoge continuamente conservando sólo el tramo final, para que un script muy verboso no agote la memoria del equipo.

Cancelar un workflow de scripts en ejecución también detiene su contenedor. El worker comprueba el intento exacto y su permiso vigente para continuar; si se cancela durante la preparación del contenedor, el script no llega a arrancar. Los efectos externos parciales quedan registrados y no se reintentan automáticamente.

Los objetivos de proyecto admiten cambios tipados de agentes sin perder actualizaciones simultáneas. Se protegen las ediciones humanas frente a cambios de agentes basados en una versión anterior, incluso dentro del mismo segundo, y se conservan los archivos dañados durante su recuperación.

El [paso de envío de correo](docs/design/workflow-email-delivery.md) envía texto con HTML opcional, CC/BCC y adjuntos acotados del propio flujo mediante una cuenta SMTP del propietario. La aprobación vincula todos los destinatarios y el contenido exacto, incluidas las huellas de los adjuntos. Los permisos se consumen una vez al ejecutar, las esperas aprobadas continúan automáticamente y un efecto externo incierto no se reintenta por sí solo. La aceptación del servidor SMTP se distingue de la entrega en la bandeja de entrada.

Combina disparadores, condiciones, esperas, aprobaciones humanas, pasos multimedia e informes guardados. La continuación del servidor hace avanzar workflows iniciados y despierta esperas vencidas. Los intentos tienen reservas temporales y escrituras condicionales para que una respuesta tardía no pise una cancelación ni un intento nuevo.

Los informes pueden tomar texto de las entradas del run o de resultados anteriores y guardarlo como artefacto propio. Los resultados heredan el proyecto y la conversación verificados. Actividad muestra dependencias, motivos de espera y enlaces a los archivos.

Los nodos con acciones externas necesitan la capacidad y autorización correspondientes. Cancelar impide el trabajo posterior; no puede deshacer una acción externa que ya ocurrió.

## Voz

Jarvis ofrece una sesión con reconocimiento español/inglés, respuestas habladas, controles de interrupción y esfera visual reactiva. Configura los servicios de transcripción y síntesis disponibles en la app; el navegador necesita permiso para usar el micrófono. Las voces instaladas y los motores locales determinan los idiomas y la reproducción disponibles.

La voz entra en la misma conversación y el mismo flujo de permisos que el texto. Abrir una sesión de voz no autoriza de forma general acciones sobre archivos, escritorio o servicios externos.

## Arquitectura

| Área | Código |
| --- | --- |
| Aplicación HTTP y persistencia | [app.py](app.py), [routes](routes/), [core](core/) |
| Interfaz Studio y estado | [studio/src](studio/src/) |
| Transporte de modelos y puente de clientes oficiales | [llm_core.py](src/llm_core.py), [cli_model.py](src/cli_model.py), [runner_billing.py](src/runner_billing.py) |
| Delegación y supervisión de procesos | [dispatch.py](src/dispatch.py), [external_worker.py](src/external_worker.py) |
| Workflows duraderos | [workflows](src/workflows/) |
| Motores multimedia y continuación | [media_runs.py](src/media_runs.py), [media_scheduler.py](src/media_scheduler.py), [media_backends](src/media_backends/) |
| Artefactos, identidad y migración | [artifact_store.py](src/artifact_store.py), [artifact_identity.py](src/artifact_identity.py), [artifact_migration.py](src/artifact_migration.py) |
| Regresiones y comprobaciones de interfaz | [tests](tests/), [studio/checks](studio/checks/) |

El almacén de artefactos separa los bytes identificados por contenido de las ocurrencias de resultados con propietario. La migración aditiva conserva identificadores y metadatos históricos; las marcas de eliminación impiden recrear ocurrencias borradas.

Se mantienen nombres internos como `odysseus`, `ODYSSEUS_*`, rutas API y claves de almacenamiento por compatibilidad. El nombre del producto es Faustus.

## Desarrollo y verificación

Usa el entorno Python del proyecto e instala las dependencias de desarrollo indicadas en [tests/README.md](tests/README.md).

```bash
python -m pytest
npm ci
npx tsc --noEmit
npm run build
python -m src.doctor
```

La suite incluye aislamiento entre propietarios, persistencia, concurrencia, cancelación, migración, integración HTTP y comprobaciones de interfaz sin navegador. Los motores físicos y las cuentas requieren además pruebas en su entorno configurado; un fixture HTTP no demuestra calidad del modelo ni acceso a una cuenta.

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

El resumen de cada paquete de contexto incluye un registro de selección de memoria: entradas incluidas, omisiones y motivos, caracteres utilizados y degradación. Describe el paquete final del Context Engine, sin volver a consultar la memoria ni añadir una segunda selección.

Mantén `AUTH_ENABLED=true` en cualquier despliegue accesible por red.
Mantén `LOCALHOST_BYPASS=false` fuera del desarrollo local.

Mantén la autenticación activada. No publiques puertos sin autenticación de modelos, ComfyUI ni servicios internos. No subas credenciales ni datos privados de `data/` a Git.

Las sesiones de Bitwarden se guardan cifradas y caducan tras una hora, tanto en ajustes como en las herramientas del agente. Las sesiones antiguas en claro se eliminan al acceder y requieren desbloquear de nuevo. Protege `data/.app_key`: el cifrado no protege frente a un equipo comprometido.

Aprueba las herramientas e instrucciones del proyecto de forma deliberada. Las restricciones de agentes, fuentes propias, aprobaciones, sandbox y supervisión de procesos son controles distintos. Si falta un sandbox requerido, la ruta configurada no debe ejecutar silenciosamente el comando en el anfitrión.

Consulta el [modelo de amenazas](THREAT_MODEL.md), la [política de seguridad](SECURITY.md) y las [notas de despliegue seguro](website/setup.md#security-notes).

## Créditos y licencia

Faustus parte de [Odysseus](https://github.com/odysseus-dev/odysseus). Más créditos en [ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md), contribuciones en [CONTRIBUTING.md](CONTRIBUTING.md) y licencia **AGPL-3.0-or-later** en [LICENSE](LICENSE).
