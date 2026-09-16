# Integración de motores multimedia

## Contrato común y especialización real

El objetivo no es envolver todas las APIs como si fueran `chat/completions`. Cada adapter describe tareas e inputs diferentes y cumple el mismo lifecycle. La normalización debe preservar capacidades, no rebajarlas al mínimo común denominador ni inventar equivalencias. Un `extension_payload` opaco no puede servir para colar parámetros, nodos o rutas que el dominio no valida.

Todos los adapters tienen tests de conformidad y una ficha de deployment. Una integración puede estar disponible en modo lectura o planificación mientras su ejecución real sigue pendiente. Esta distinción permite entregar Creator progresivamente sin botones falsos.

## ComfyUI: ampliar el camino ya existente

Faustus ya usa templates revisadas y un MediaRun durable con outbox y reconciliación. Mantenerlo como primer adapter y comprobar que la refactorización no cambie IDs, approvals o comportamientos legacy. Las recetas actuales deben seguir utilizables sin convertir todos los proyectos a Creator.[^F02][^F03][^F04][^F16]

La mejora clave es **staging de inputs**: una attachment o occurrence autorizada se transforma en un archivo propio del engine con hash, tamaño y mapping de input. «Nombre de imagen en el motor» y «adjunto del chat» son identidades diferentes. Resolverlo en código, no pedir al modelo que adivine el nombre remoto.

El catálogo de recetas expone required nodes, componentes, constraints, outputs, variantes, recursos y evidencia. La aprobación debe atarse al contrato ejecutable exacto. Incorporar cambios de nodos o pesos como actualización revisable. Un JSON importado permanece en cuarentena hasta comprobar límites, referencias y permisos; que se pueda parsear no implica que se deba ejecutar.

## FFmpeg y composición determinista

FFmpeg debe usarse como worker de composición/inspección, con build y filtros disponibles inventariados. Sus filtros documentan capacidades de tratamiento audiovisual; la disponibilidad concreta depende del build instalado. No hardcodear que cualquier máquina tiene todos los encoders, filtros o licencias de distribución resueltos.[^W08]

Compilar `trim`, `concat`, overlays, audio mix, ducking y subtítulos desde operaciones tipadas. Ejecutar argv sin interpolación de shell. Los textos y paths necesitan escaping propio del filtro además del escaping del proceso; no son el mismo problema. El usuario debe poder inspeccionar un plan legible sin tener que entender un filtergraph completo.

El progreso se obtiene del proceso con buffers acotados. Guardar tails de diagnóstico y no capturar vídeos en memoria. Asignar CPU/GPU/scratch antes de ejecutar, verificar espacio libre durante collect y conservar un temporal identificado si la recogida falla. Finalizar sólo tras inspección de output.

## WhisperX y alineación

Integrar WhisperX como worker opcional separado del servidor. Su documentación respalda ASR, alineación de palabras, VAD y diarización opcional; los alineadores dependen del idioma y la diarización requiere condiciones y modelos adicionales. Eso no equivale a atribuir nombres reales a speakers ni a garantizar todos los idiomas mezclados.[^A04]

El contrato de transcript debe aceptar palabras sin alineación y confianza nullable. No sintetizar precisión milimétrica para rellenar campos ausentes. Los modelos de alineación se eligen de forma explícita y se descargan sólo mediante un procedimiento autorizado. Un modelo instalado no implica que el worker pueda cargarlo con la memoria actual.

Para medios largos, primero introducir ingest/proxy/chunking y offsets. Los límites de 64 MB, tres minutos y 1080p descritos en la baseline no se eliminan mediante cambiar constantes sin más. Diseñar nuevas rutas acotadas, testear fronteras y mantener el camino corto existente.[^F01]

## Localización inspirada en VideoLingo

Adoptar glosario, contexto de escena y separación de traducir/revisar/adaptar. El resultado de cada etapa guarda una revisión y validación; JSON inválido nunca es una caché válida. Reintentar un cue no vuelve a ejecutar etapas intactas.

El README de VideoLingo declara límites para mezcla de idiomas y doblaje separado de personajes. Por ello el soporte multihablante de Creator es una ampliación propia que necesita fixtures y casting manual de respaldo. Tampoco se debe convertir la preferencia del proyecto por subtítulos de una línea en una norma universal de calidad.[^A03]

La salida de localización contiene fuente, traducción, adaptación, texto narrado, razones de cambios y mapping temporal. Nombres, cifras y glosarios tienen chequeos deterministas, complementados por revisión humana cuando el sentido pueda alterarse. La traducción no debe confundir omisión con concisión.

## TTS y Voice Studio

Reutilizar los servicios y voces actuales para una primera integración. Añadir Chatterbox u otros mediante versiones de adapter separadas, no mediante instalación global. La ficha debe mostrar idiomas, reference requirements, controles de estilo y coste del deployment elegido.[^F01][^F07][^A05]

Una voz sintética predefinida y la clonación de una persona real requieren datos y permisos distintos. El gate de identidad existente se amplía por scope y vigencia. Los ejemplos de voz se generan con texto de prueba proporcionado o elegido explícitamente; no activar micrófono ni capturar conversaciones para construir una muestra.

Renderizar por unidad editorial: frase, cue o párrafo, con margen para pausas y prosodia. Los ensamblados y crossfades son operaciones reproducibles. Si el diálogo traducido no cabe, proponer alternativas de texto o montaje; no comprimir brutalmente una voz y marcarlo como doblaje correcto.

## ACE-Step y Music Studio

La API REST es la primera candidata de integración, con proceso aislado y versión fijada. El LLM general de Faustus escribe o revisa el brief y la letra; el modelo musical realiza la síntesis. Una capacidad de planificación del motor musical no sustituye el objetivo autoritativo ni la política de permisos de Faustus.[^A01]

El manifest distingue modelos LM/DiT y variantes base/sft/turbo/XL. No dar por universales los controles ni las operaciones de edición. Los resultados de calidad y velocidad publicados por upstream sirven para orientar pruebas, no para ofrecer una cifra al usuario de Faustus sin medirla en su configuración.[^A01][^W09]

Primera entrega: generación, instrumental/letra, metadatos, escucha y variantes. Segunda: repaint por región, acompañamiento, stems y composición sólo donde el adapter lo soporte. Tercera opcional: entrenamiento LoRA con datasets autorizados. Mantener generación y entrenamiento como capacidades distintas con budgets diferentes. El archivo de licencia del código ACE-Step leído es MIT; pesos, muestras y dependencias requieren revisión por separado.[^A02]

## Wan y LTX: adapters por tarea y entorno

No integrar «Wan» o «LTX» como una casilla genérica. Crear recetas fijadas para la tarea y pipeline seleccionados, con su conjunto de componentes. En Wan, T2V, I2V, TI2V, S2V y animación son variantes distintas documentadas. En LTX hay diferencias entre pipeline y componentes locales; las operaciones de la API remota se declaran por otro deployment.[^A09][^A10][^W05]

Realizar primero un probe de entorno y luego una generación mínima autorizada por variante. Registrar memoria, tiempo y artefacto. No fijar mínimos de VRAM por el tamaño de descarga ni sumar tarjetas para afirmar cabida. Las optimizaciones de offload, cuantización o multiGPU necesitan soporte explícito del motor y evidencia de esa combinación.

Los planos generados se insertan como tomas candidatas en el timeline. Audio generado se conserva separable cuando el motor lo entregue así. Retake/extend/reframe deben registrar rango, handles y qué porción preservan. La continuidad de personaje y sonido se evalúa, no se da por garantizada por haber reutilizado una seed.

## OpenCut, OTIO y Blender

OpenCut se usa para estudiar interacciones de edición. Su reescritura no se elige como dependencia de producción hasta que el spike demuestre API, estabilidad, licencia y coste de integración. La versión classic está archivada; copiarla entera impondría mantenimiento ajeno y un stack distinto.[^A07][^A08]

OTIO es el formato de intercambio editorial, no el renderer. Ofrecer un subconjunto honesto y un loss report para los efectos no representables. Los plugins de import/export y hooks no se ejecutan automáticamente al importar un proyecto: un formato de datos no debe abrir una ruta de ejecución arbitraria.[^A11]

Blender queda como extensión de fase posterior, con read-only scene probe, operaciones tipadas y workers de render. El patrón de readiness del plugin descrito en Chat On Steroids es útil: tener el paquete instalado no demuestra que el add-on responda ni que la escena esté disponible.[^D04]


[^F02]: Faustus · src/media_runs.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_runs.py). Consultado el 13 de septiembre de 2026. Lectura de líneas 1–250: preflight, consentimiento, outbox y estados de envío; no auditoría integral del módulo.

[^F03]: Faustus · src/media_scheduler.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_scheduler.py). Consultado el 13 de septiembre de 2026. Continuación y reconciliación de renders ya enviados; no vuelve a enviar grafos.

[^F04]: Faustus · src/media_workflows.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_workflows.py). Consultado el 13 de septiembre de 2026. Lectura 1–190: inputs tipados, requisitos y fingerprint de MediaWorkflow.

[^F16]: Faustus · árbol de media_backends. [Fuente](https://github.com/Luissalet/Faustus/tree/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_backends). Consultado el 13 de septiembre de 2026. ComfyUIBackend y pool; sólo se inspeccionó el inventario de este directorio.

[^W08]: FFmpeg · filtros. [Fuente](https://www.ffmpeg.org/ffmpeg-filters.html). Consultado el 13 de septiembre de 2026. Filtros de audio/vídeo y loudnorm; disponibilidad real depende del build.

[^A04]: m-bain/whisperX · README.md. [Fuente](https://github.com/m-bain/whisperX/blob/main/README.md). Consultado el 13 de septiembre de 2026. Alineación por palabra, VAD y diarización opcional; alineadores por idioma y acuerdo de pyannote.

[^F01]: Faustus · README.md. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/README.md). Consultado el 13 de septiembre de 2026. Capacidades generales, arquitectura y licencia declarada AGPL-3.0-or-later; resultados de pruebas reportados por el repositorio, no reproducidos aquí.

[^A03]: Huanshere/VideoLingo · README.md. [Fuente](https://github.com/Huanshere/VideoLingo/blob/main/README.md). Consultado el 13 de septiembre de 2026. WhisperX, glosario, traducción/reflexión/adaptación, reanudación; limitaciones multihablante y cambio de idioma declaradas.

[^F07]: Faustus · src/media_capabilities.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_capabilities.py). Consultado el 13 de septiembre de 2026. Probes baratos; diferencia paquete instalado / modelo preparado; Piper no conectado al proveedor TTS en este módulo.

[^A05]: resemble-ai/chatterbox · README.md. [Fuente](https://github.com/resemble-ai/chatterbox/blob/master/README.md). Consultado el 13 de septiembre de 2026. Variantes multilingual/Turbo/Nano, idiomas y controles diferentes; no verificación de calidad local.

[^A01]: ace-step/ACE-Step-1.5 · README.md. [Fuente](https://github.com/ace-step/ACE-Step-1.5/blob/main/README.md). Consultado el 13 de septiembre de 2026. Catálogo de operaciones musicales, variantes, API y recursos declarados; no benchmark propio.

[^W09]: ACE-Step · documentación de variantes. [Fuente](https://github.com/ace-step/ACE-Step-1.5). Consultado el 13 de septiembre de 2026. Tabla Model Zoo consultada: extract/lego/complete no equivalentes entre base, sft y turbo.

[^A02]: ace-step/ACE-Step-1.5 · LICENSE. [Fuente](https://github.com/ace-step/ACE-Step-1.5/blob/main/LICENSE). Consultado el 13 de septiembre de 2026. Archivo leído.

[^A09]: Lightricks/LTX-2 · README.md. [Fuente](https://github.com/Lightricks/LTX-2/blob/main/README.md). Consultado el 13 de septiembre de 2026. Lectura 1–140: variantes y componentes LTX, distinción pipelines y requisitos; algunas secciones no leídas por truncamiento.

[^A10]: Wan-Video/Wan2.2 · README.md. [Fuente](https://github.com/Wan-Video/Wan2.2/blob/main/README.md). Consultado el 13 de septiembre de 2026. Lectura 1–145: T2V/I2V/TI2V/S2V/Animate y alternativas de ejecución; no se afirma que sea la última familia Wan.

[^W05]: LTX · documentación API. [Fuente](https://docs.ltx.video/). Consultado el 13 de septiembre de 2026. Operaciones API de generación/retake/extend/reframe; no prueban que cada pipeline local ofrezca lo mismo.

[^A07]: OpenCut-app/OpenCut · README.md. [Fuente](https://github.com/OpenCut-app/OpenCut/blob/main/README.md). Consultado el 13 de septiembre de 2026. Reescritura en curso; Editor API, MCP, plugins y headless figuran como objetivos, no capacidades verificadas.

[^A08]: OpenCut-app/opencut-classic · README.md. [Fuente](https://github.com/OpenCut-app/opencut-classic/blob/main/README.md). Consultado el 13 de septiembre de 2026. Versión anterior archivada; estructura Next.js/Rust y timeline como referencia, no nueva dependencia central.

[^A11]: AcademySoftwareFoundation/OpenTimelineIO · README.md. [Fuente](https://github.com/AcademySoftwareFoundation/OpenTimelineIO/blob/main/README.md). Consultado el 13 de septiembre de 2026. Intercambio editorial y referencias a medios; NO renderer ni contenedor general de medios. Plugins de formato separados.

[^D04]: totec448-spec/chat-on-steroids · docs/plugins.md. [Fuente](https://github.com/totec448-spec/chat-on-steroids/blob/main/docs/plugins.md). Consultado el 13 de septiembre de 2026. Instalación aislada, MCPB, schemas, readiness, revocación, rollback y límites del sandbox.
