# Fuentes y decisiones de adaptación

## Chat On Steroids: el donante más útil para continuidad

Su valor diferencial para este plan está en la continuidad observable: workers que conservan contexto, Goal/Loop, Compact & Resume y lifecycle de plugins. No conviene copiar su dependencia del DOM de ChatGPT cuando Faustus controla su conversación y su ejecución; interesa extraer los invariantes y los fallos que evitan.[^D01]

En `goal.ts` hay una decisión estructurada, una credencial separada del navegador y un borrador por generación. El comentario explica que el evaluador ve conversación y handoff, no resultados de herramientas, y que una declaración explícita de finalización global puede ser autoritativa. Faustus debe mejorar esa debilidad: «terminado» es una propuesta de estado que se contrasta con entregables y evidencia, no el criterio que sustituye esa evidencia.[^D02]

`handoff.ts` separa preparar el documento de anunciarlo como reanudable; crea el ID fuera del modelo, conserva referencias al plan y comprueba señales básicas de truncamiento. Adoptar prepare/publish, referencias estables y bootstrap compartido. Profundizarlo con validación de criterios, versiones y autorización: un resumen largo puede seguir omitiendo una restricción importante.[^D03]

En plugins destaca la diferencia entre instalado, conectado y realmente disponible para el cliente. También la actualización staged con rollback, los entornos privados, los schemas completos, la revocación inmediata y la advertencia de que los procesos externos no heredan mágicamente el sandbox de carpetas. Esto encaja como ampliación de `mcp_manager`, manifests y gobernanza de skills existentes.[^D04]

**Decisión:** adaptar patrones concretos, no trasplantar la aplicación. Código MIT verificado en su archivo de licencia; cada reutilización conserva avisos, commit y lista de cambios. Sus dependencias tienen obligaciones propias.[^D05]

## SteroidChat: ergonomía y casos de transporte, no nuevo núcleo

El README presenta cinco proveedores, adjuntos y streaming. El archivo `aiService.ts` leído declara nueve endpoints y contiene un parser incremental que sigue profundidad JSON, strings y escapes. La diferencia entre README y código refuerza una necesidad de Faustus: generar la tabla de soporte desde contratos y pruebas versionadas, sin declarar una integración completa sólo por tener una URL en un diccionario.[^D06][^D07]

Adoptar ideas de selección rápida de proveedor, adjuntos cómodos, adaptación móvil y fixtures de streams fragmentados/cancelación. No copiar la emisión genérica de parámetros a todos los motores ni mover credenciales al cliente web como arquitectura de Faustus. Una UI pequeña es una buena referencia de fricción, no un sustituto del sistema de capacidades y políticas que ya existe.

**Decisión:** extraer UX y diseñar pruebas propias; prioridad menor que Creator y model intelligence. La licencia Apache-2.0 aparece declarada en README, pero no se recuperó `LICENSE` raíz. Copiar código queda pendiente de aclarar la licencia aplicable a la revisión y archivos concretos.[^D06]

## Killer ChatGPT Prompts: especialización sin «magia de prompt»

El repositorio usa comandos y perfiles como escritor, generador de descripciones visuales, resumen y desarrollo. En el JSON leído aparecen instrucciones de ignorar el contexto anterior y atribuciones ficticias de experiencia. Nada de eso demuestra una mejora de competencia. El patrón útil es empaquetar intención y formato; las instrucciones inseguras o teatralizadas no deben trasladarse al núcleo.[^D08][^D09]

Faustus debería ofrecer presets propios de director, editor, traductor, letrista y revisor, con variables, fuentes, ejemplos, schemas de salida y evaluación. Un preset puede cambiar estrategia o tono, nunca permisos, aislamiento o política superior. Mantener la posibilidad de desactivarlo y ver su diff.

**Decisión:** recrear el concepto mediante presets originales, no importar masivamente sus textos. No se localizó una licencia explícita en la raíz y los metadatos consultados no la identificaban. No se presume autorización de copia por ser público.

## Investigación complementaria: multimedia

| Fuente | Qué está respaldado por lo leído | Qué adaptar | Qué NO asumir |
|---|---|---|---|
| Invoke | Canvas, workflows, galería y controles diferenciados. | Capas con intención, arrastre a destinos, modelos con límites y proyectos portables. | Que todos sus modelos admiten los mismos controles o que debe sustituir ComfyUI. |
| WhisperX | Alineación por palabra, VAD y diarización opcional. | Servicio ASR/alineación aislado y editor con precisión explícita. | Nombres reales de hablantes, exactitud total o velocidad de otro hardware. |
| VideoLingo | Glosarios y pipeline de traducción/revisión/adaptación. | Etapas versionadas, terminología y reintentos por parte. | Que ya resuelve doblaje multihablante o code-switching; el README declara límites. |
| Chatterbox | Variantes con idiomas y recursos diferentes. | Voice model zoo, casting y controles por variante. | Que Turbo/Nano inglés sirven igual para español. |
| ACE-Step | Generación y edición musicales, variantes y controles. | Music Studio, letra editable, repaint, referencias y matriz de capacidades. | Todas las operaciones en turbo/sft, tiempos universales o derechos resueltos sobre referencias. |
| Wan | Variantes distintas para tareas de vídeo. | Recipes por tarea y manifests de componentes. | Reparto automático de VRAM o soporte uniforme entre checkpoints. |
| LTX | Pipelines y componentes locales; API con otras operaciones. | Video adapters, retake/extend cuando sea real y gestión explícita de componentes. | Equivalencia API/local, igual licencia o cabida en el hardware de Luis. |
| OpenCut | Reescritura declarada y versión classic archivada. | Referencia de timeline y ergonomía mediante spike. | Editor API, MCP y headless finalizados por figurar en una lista de futuro. |
| OpenTimelineIO | Modelo de intercambio editorial y referencias externas. | Export/import de un subconjunto documentado. | Renderer, motor de efectos o paquete autosuficiente de medios. |

Fuentes primarias de esta tabla: documentación de los propios proyectos y desarrolladores.[^A01][^A03][^A04][^A05][^A06][^A07][^A08][^A09][^A10][^A11][^W02][^W03][^W05]

## Estrategias de adopción

**Integrar motor como servicio** para generación, ASR, TTS o entrenamiento. Evita mezclar dependencias PyTorch/CUDA con el servidor de Faustus. La API debe envolverse en el contrato de ejecución y seguridad del proyecto, no exponerse directamente al agente.

**Adaptar código acotado** sólo cuando el algoritmo o componente reduzca realmente el mantenimiento y la licencia de la revisión esté clara. Registrar commit, archivos derivados, avisos y tests propios. Un fragmento útil no justifica una dependencia de toda la aplicación donante.

**Reimplementar patrones** para UI, continuidad, aprobaciones y presets cuando ya hay una autoridad equivalente. El valor está en conservar el comportamiento deseado y evitar los fallos conocidos, no en conservar nombres o tablas duplicadas.

**Probar y posponer** para reescrituras, plugins complejos, 3D y entrenamiento. Un spike tiene una pregunta, salida verificable y decisión; no es una integración que permanece indefinidamente habilitada a medias.

## Lecturas enlazadas para implementación

Los siguientes enlaces son destinos recomendados de lectura técnica; su existencia enlazada por un README no significa que todo su contenido se haya auditado en este trabajo. Fijar versión y leer antes de desarrollar el adapter:

- [API ACE-Step](https://github.com/ace-step/ACE-Step-1.5/blob/main/docs/en/API.md), [inferencia](https://github.com/ace-step/ACE-Step-1.5/blob/main/docs/en/INFERENCE.md) y [compatibilidad GPU](https://github.com/ace-step/ACE-Step-1.5/blob/main/docs/en/GPU_COMPATIBILITY.md).
- [Optimización LTX](https://github.com/Lightricks/LTX-2/blob/main/packages/ltx-pipelines/docs/optimization.md), [pipelines](https://github.com/Lightricks/LTX-2/blob/main/packages/ltx-pipelines/docs/pipelines.md) e [instalación](https://github.com/Lightricks/LTX-2/blob/main/packages/ltx-pipelines/docs/installation.md).
- [Código y documentación de WhisperX](https://github.com/m-bain/whisperX), [VideoLingo](https://github.com/Huanshere/VideoLingo), [Chatterbox](https://github.com/resemble-ai/chatterbox) y [Wan2.2](https://github.com/Wan-Video/Wan2.2).

No fijar en código URLs `main` como dependencia de ejecución. Estos enlaces ayudan a navegar; los manifests instalables y fixtures deben usar revisiones inmutables verificadas.


[^D01]: totec448-spec/chat-on-steroids · README.md. [Fuente](https://github.com/totec448-spec/chat-on-steroids/blob/99ec069cfbe7aae01e2ba9ab234c237ffbf609ff/README.md). Consultado el 13 de septiembre de 2026. MCP local, Chrome, workers persistentes, steering, Goal/Loop y Compact & Resume.

[^D02]: totec448-spec/chat-on-steroids · src/main/goal.ts. [Fuente](https://github.com/totec448-spec/chat-on-steroids/blob/99ec069cfbe7aae01e2ba9ab234c237ffbf609ff/src/main/goal.ts). Consultado el 13 de septiembre de 2026. Lectura 1–150: decisión validada, borrador por generación y autodeclaración final como señal autoritativa; no inspecciona tool results.

[^D03]: totec448-spec/chat-on-steroids · src/main/session/handoff.ts. [Fuente](https://github.com/totec448-spec/chat-on-steroids/blob/99ec069cfbe7aae01e2ba9ab234c237ffbf609ff/src/main/session/handoff.ts). Consultado el 13 de septiembre de 2026. Lectura 1–170: prepare/publish, identidad, truncamiento, referencias al plan y bootstrap consistente.

[^D04]: totec448-spec/chat-on-steroids · docs/plugins.md. [Fuente](https://github.com/totec448-spec/chat-on-steroids/blob/main/docs/plugins.md). Consultado el 13 de septiembre de 2026. Instalación aislada, MCPB, schemas, readiness, revocación, rollback y límites del sandbox.

[^D05]: totec448-spec/chat-on-steroids · LICENSE. [Fuente](https://github.com/totec448-spec/chat-on-steroids/blob/main/LICENSE). Consultado el 13 de septiembre de 2026. Archivo de licencia leído.

[^D06]: buluma/steroid-chat · README.md. [Fuente](https://github.com/buluma/steroid-chat/blob/master/README.md). Consultado el 13 de septiembre de 2026. Interfaz mult proveedor, adjuntos, streaming y Cordova; README enumera cinco proveedores.

[^D07]: buluma/steroid-chat · steroid-chat-web/src/services/aiService.ts. [Fuente](https://github.com/buluma/steroid-chat/blob/master/steroid-chat-web/src/services/aiService.ts). Consultado el 13 de septiembre de 2026. Lectura 1–200: nueve endpoints, parser incremental JSON con strings/escapes y AbortSignal; no prueba de todos los proveedores.

[^D08]: calin-ciobanu/killer_chatgpt_prompts · README.md. [Fuente](https://github.com/calin-ciobanu/killer_chatgpt_prompts/blob/master/README.md). Consultado el 13 de septiembre de 2026. Especializaciones reutilizables y comandos por chat.

[^D09]: calin-ciobanu/killer_chatgpt_prompts · user_custom.json. [Fuente](https://github.com/calin-ciobanu/killer_chatgpt_prompts/blob/master/user_custom.json). Consultado el 13 de septiembre de 2026. Lectura 1–100: roles de escritor, prompts visuales, resumen y desarrollo; instrucciones de ignorar contexto previo no se adoptan.

[^A01]: ace-step/ACE-Step-1.5 · README.md. [Fuente](https://github.com/ace-step/ACE-Step-1.5/blob/main/README.md). Consultado el 13 de septiembre de 2026. Catálogo de operaciones musicales, variantes, API y recursos declarados; no benchmark propio.

[^A03]: Huanshere/VideoLingo · README.md. [Fuente](https://github.com/Huanshere/VideoLingo/blob/main/README.md). Consultado el 13 de septiembre de 2026. WhisperX, glosario, traducción/reflexión/adaptación, reanudación; limitaciones multihablante y cambio de idioma declaradas.

[^A04]: m-bain/whisperX · README.md. [Fuente](https://github.com/m-bain/whisperX/blob/main/README.md). Consultado el 13 de septiembre de 2026. Alineación por palabra, VAD y diarización opcional; alineadores por idioma y acuerdo de pyannote.

[^A05]: resemble-ai/chatterbox · README.md. [Fuente](https://github.com/resemble-ai/chatterbox/blob/master/README.md). Consultado el 13 de septiembre de 2026. Variantes multilingual/Turbo/Nano, idiomas y controles diferentes; no verificación de calidad local.

[^A06]: invoke-ai/InvokeAI · README.md. [Fuente](https://github.com/invoke-ai/InvokeAI/blob/main/README.md). Consultado el 13 de septiembre de 2026. Canvas, in/outpainting, workflows, galería y gestor de modelos con diferencias de compatibilidad.

[^A07]: OpenCut-app/OpenCut · README.md. [Fuente](https://github.com/OpenCut-app/OpenCut/blob/main/README.md). Consultado el 13 de septiembre de 2026. Reescritura en curso; Editor API, MCP, plugins y headless figuran como objetivos, no capacidades verificadas.

[^A08]: OpenCut-app/opencut-classic · README.md. [Fuente](https://github.com/OpenCut-app/opencut-classic/blob/main/README.md). Consultado el 13 de septiembre de 2026. Versión anterior archivada; estructura Next.js/Rust y timeline como referencia, no nueva dependencia central.

[^A09]: Lightricks/LTX-2 · README.md. [Fuente](https://github.com/Lightricks/LTX-2/blob/main/README.md). Consultado el 13 de septiembre de 2026. Lectura 1–140: variantes y componentes LTX, distinción pipelines y requisitos; algunas secciones no leídas por truncamiento.

[^A10]: Wan-Video/Wan2.2 · README.md. [Fuente](https://github.com/Wan-Video/Wan2.2/blob/main/README.md). Consultado el 13 de septiembre de 2026. Lectura 1–145: T2V/I2V/TI2V/S2V/Animate y alternativas de ejecución; no se afirma que sea la última familia Wan.

[^A11]: AcademySoftwareFoundation/OpenTimelineIO · README.md. [Fuente](https://github.com/AcademySoftwareFoundation/OpenTimelineIO/blob/main/README.md). Consultado el 13 de septiembre de 2026. Intercambio editorial y referencias a medios; NO renderer ni contenedor general de medios. Plugins de formato separados.

[^W02]: Invoke · capas y destinos de arrastre. [Fuente](https://invoke.ai/features/canvas/layers-and-drops/). Consultado el 13 de septiembre de 2026. Patrones de capas raster, control y referencias regionales.

[^W03]: Invoke · Canvas Projects. [Fuente](https://invoke.ai/development/front-end/canvas-projects/). Consultado el 13 de septiembre de 2026. Formato portable .invk y manifiesto de proyecto como inspiración.

[^W05]: LTX · documentación API. [Fuente](https://docs.ltx.video/). Consultado el 13 de septiembre de 2026. Operaciones API de generación/retake/extend/reframe; no prueban que cada pipeline local ofrezca lo mismo.
