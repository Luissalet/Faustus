# Faustus Creator

## Decisión principal

**Construir Creator como un espacio de producción multimedia sobre Faustus, no como una segunda aplicación ni como un simple prompt especializado.** Chat, canvas, timeline, transcripción, música y biblioteca deben compartir proyecto, versiones, artefactos, permisos y ejecuciones. El resultado de un trabajo será editable y trazable: no sólo un mensaje y una carpeta de archivos.

La oportunidad no está únicamente en sumar funciones. Muchas de las mejoras más valiosas profundizan capacidades que ya existen: fichas de modelos realmente operativas, referencias que se aplican técnicamente, edición no destructiva coherente, compacción sin pérdida de restricciones, plugins con actualización segura y renders que sobreviven a errores sin duplicarse.

El paquete completo contiene **156 features y 43 paquetes de trabajo**, una arquitectura de referencia, 14 decisiones propuestas, 12 recetas de producto, contratos JSON, ejemplos sintéticos y handoffs para Claude, Codex y un revisor. El catálogo detallado y las fichas están en Markdown y JSON para poder delegar por partes sin cargar todo el informe en el contexto de un agente.

La baseline de Faustus es el commit `fa567977a00505b18a0fdf1b6e4d1a83f56270c6`. Se inspeccionó documentación y código de los subsistemas relevantes; no se ejecutó la aplicación, su suite ni modelos. Los 15 posts de X no dieron contenido recuperable y permanecen identificados sin proyectos atribuidos. Los tres repositorios nombrados sí se leyeron. La investigación multimedia adicional no se presenta como si proviniera de esos posts.

## Lo que Faustus ya aporta

El punto de partida es considerable: proyectos y contexto compartido, artefactos con bytes deduplicados y occurrences por propietario, recetas ComfyUI, MediaRuns durables, edición por capas/máscaras, transcripción y SRT/VTT, voz, equipos de agentes, workflows, aprobaciones y metadatos/calibración de modelos. Esas autoridades deben mantenerse.[^F01]

La revisión de código permite concretar dónde ampliar, pero no justificar la frase «todo está resuelto». Los segmentos y exportadores de subtítulos no equivalen a un estudio de localización. La narración corta descrita en el README reemplaza el audio original; no es clonación, doblaje multihablante o lip-sync. El compositor por capas tiene un alcance limitado y su orden de exportación merece tests antes de añadir operaciones más complejas.[^F05] [^F06]

Tampoco sería correcto proponer un nuevo catálogo de modelos ignorando el existente. Faustus ya tiene vocabularios amplios de capacidades y fuentes, controles y calibración. La mejora es usar esa base de manera más precisa, especialmente separando lo que pertenece a los pesos de lo que sólo está probado en una configuración de servidor.[^F08] [^F09]

**ProjectStore e identidad estable.** Perfil Creator y documentos editoriales revisionados.

**Artifact store y ownership.** Biblioteca multimedia, derivados y linaje temporal.

**MediaRun y workflows.** Adapters especializados y producciones con dependencias.

**Sistema de modelos.** Deployment identity, constraints y evidencia por tarea.

**Permisos y Attention.** Las mismas políticas para UI, tools, voz y clientes externos.

## Qué conviene extraer de los proyectos

**Chat On Steroids** es el donante más interesante para continuidad y lifecycle. Su Goal valida decisiones y deduplica borradores por generación; su handoff separa preparar el resumen de publicarlo como reanudable. Su documentación de plugins describe instalación aislada, schemas íntegros, readiness y rollback. La adaptación debe conservar estos invariantes dentro de Faustus, no importar su dependencia del DOM de ChatGPT.[^D02] [^D03] [^D04]

Hay una mejora adicional importante: el evaluador Goal descrito por CoS no recibe tool results y puede tratar una afirmación de finalización como autoritativa. Faustus puede hacerlo mejor: un «terminado» propone cerrar; el sistema comprueba los entregables, revisiones y evidencias requeridos. La fluidez de la conversación no sustituye la ejecución.

**SteroidChat** aporta una referencia pequeña de ergonomía, adjuntos, cambio de proveedor y streaming. Su README enumera cinco proveedores, mientras el archivo de servicio leído declara nueve endpoints y un parser incremental atento a strings y escapes. Interesa añadir casos de prueba y reducir fricción, no reemplazar el transporte y políticas de Faustus por su implementación.[^D06] [^D07]

**Killer ChatGPT Prompts** aporta el patrón de comandos/especialidades, no una prueba de mayor competencia. Los prompts leídos incluyen instrucciones de ignorar contexto previo y supuestas décadas de experiencia. Se proponen presets originales con entradas, salidas, versiones y evaluación, sin trasladar esa teatralización ni alterar permisos.[^D09]

Para multimedia, la investigación complementaria orienta decisiones específicas: Invoke para canvas y controles visuales; WhisperX y VideoLingo para alineación/localización; Chatterbox para voces por variante; ACE-Step para Music Studio; Wan/LTX para tareas de vídeo. OpenCut es referencia y spike: su reescritura está en curso y classic está archivado. OTIO sirve para intercambio editorial, no para renderizar.[^A06] [^A04] [^A03] [^A05] [^A01] [^A07] [^A08] [^A11]

## La experiencia Creator

El usuario debe poder entregar un guion, referencias y voz, pedir un tráiler con dos formatos e idiomas, y obtener primero un plan inspeccionable y luego una producción que pueda modificar. El espacio presenta preview/editor central, biblioteca, inspector y chat; el timeline aparece cuando el medio lo requiere. Los layouts cambian la distribución, no las entidades.

La unidad de producción incluye brief, escena, plano, asset, toma, versión de montaje y entrega. Una imagen con máscara es un documento editorial. Una canción tiene letra, secciones y tomas. Una localización separa texto fuente, traducción, adaptación y texto narrado. No guardar todo eso como un prompt opaco que desaparece al terminar el turno.

Una selección de región o tiempo lleva ID y revisión. «Rehaz sólo esta frase» se convierte en una propuesta sobre un cue concreto. «Prueba tres fondos sin cambiar el personaje» crea capas candidatas. «Rehaz el puente de la canción» fija un intervalo y conserva lo aceptado. La edición precisa y el lenguaje natural se complementan.

El flujo recomendado es proponer, previsualizar, aceptar y ejecutar cuando proceda. Las pequeñas ediciones manuales no necesitan una tormenta de modales; los efectos facturables, públicos o sensibles conservan sus aprobaciones. Cambiar de vista no debe perder el borrador, la máscara o la selección. Volver de otro chat no inicia un segundo render.

La cola del estudio proyecta MediaRun, workflows y Attention. Debe distinguir esperar GPU, esperar permiso, cargar un modelo, renderizar, recoger output y perder conexión. Si el motor no proporciona porcentaje, no se inventa. Una imagen mostrada en preview tampoco equivale a un export final guardado.

El primer éxito de Creator no debe exigir diez instalaciones. Puede ser un vídeo corto con subtítulos corregidos y export real, o una imagen con máscara y variantes. Música, vídeo generativo y 3D se añaden mediante capacidades opcionales.

## Modelos: de ficha informativa a contrato operativo

**La ficha debe responder a una tarea en un deployment, no sólo a un nombre de modelo.** El mismo modelo puede tener controles, límites y rendimiento distintos según motor, revisión, template y configuración. Un dato declarado por un registro no se convierte en una medición de la instalación del usuario.

Se proponen tres niveles: ModelSpec para pesos/componentes; DeploymentManifest para cómo se sirven; CapabilityEvidence para lo probado bajo condiciones concretas. Compartir identidad de pesos por digest sigue siendo útil. Compartir automáticamente resultados de un probe entre dos servidores puede no serlo. La migración conserva datos históricos y marca el scope insuficiente, sin inventar condiciones que no se guardaron.[^F09]

La ficha completa contempla tareas, formatos, límites combinados, idiomas, referencias/máscaras, parámetros, componentes, memoria, rendimiento, precios y derechos. La vista simple muestra lo necesario; la avanzada deja inspeccionar la evidencia de cada campo. Un desconocido sigue visible como desconocido, no se traduce a cero, falso o gratuito.

Los formularios, el API y las tools deben consumir el mismo schema de controles. Una opción define tipo, unidad, rango, default, condiciones y mecanismo de aplicación. CFG, negative prompt, seed, fps, context length y reasoning effort no se envían indiscriminadamente a todos los backends. El recibo muestra lo solicitado, normalizado, enviado y confirmado; un eco del request no demuestra aplicación efectiva.

Hay ejemplos concretos del valor de este detalle. ACE-Step documenta operaciones distintas entre base, sft y turbo; no se deben activar extract, lego o complete por compartir familia. Chatterbox diferencia variantes multilingües de Turbo/Nano centradas en inglés. Wan distingue tareas como T2V, I2V y S2V; la API y los pipelines locales de LTX tampoco se deben equiparar por nombre.[^A01] [^A05] [^A10] [^A09]

El Model Explorer permitirá comparar despliegues, filtrar por tarea, idioma, privacidad, coste y cabida, y explicar candidatos descartados. Antes de cambiar modelo a mitad de proyecto mostrará las capacidades perdidas y las evidencias que deben recalibrarse. Una degradación a resumen textual no sustituye ocultamente una entrada visual.

El router debe conservar la intención auto separada del último modelo concreto seleccionado. Los requisitos duros se comprueban antes de puntuar velocidad o coste. Un ranking no compensa la falta de una capacidad obligatoria ni autoriza pasar de local a remoto o de suscripción a API.

## Producción multimedia por dominios

**Imagen.** Profundizar canvas por capas, máscaras, grupos, transforms, texto editable y control regional. Componer logos y textos críticos desde assets exactos, en lugar de pedir al generador que los reproduzca fielmente. Vincular referencias de personaje/estilo/composición a inputs reales y distinguirlas de orientación textual. Comparar variantes antes de aceptar; mantener original y undo.

**Vídeo.** Timeline multipista con clocks racionales, trims, splits, ripple, snapping y retiming map. Storyboard y animatic antes de generar tomas caras. Genere o importe el material, editar un plano no debe regenerar todos los demás. Añadir formatos vertical/horizontal con encuadres y safe areas propios. La exportación compila una revisión fijada y valida el archivo.

**Subtítulos.** WhisperX orienta la alineación por palabra y diarización opcional. El contrato debe aceptar precisión ausente y speaker IDs sin nombres inferidos. VideoLingo orienta glosarios y etapas de traducir/revisar/adaptar, pero su README declara límites de mezcla de idiomas y doblaje por personajes: esas capacidades más profundas serán trabajo propio, no algo que ya venga resuelto.[^A04] [^A03]

**Voz y audio.** Casting por personaje/idioma, pronunciación, tomas por frase y mezcla de diálogo, música y ambiente. Elegir explícitamente si se reemplaza todo el audio o se preservan pistas. Medir loudness y clipping del archivo real, no deducirlos de ajustes. Una traducción demasiado larga debe producir opciones de texto o montaje, no una compresión de voz extrema presentada como correcta.

**Música.** Music Studio con letra estructurada, BPM, tonalidad, compás, instrumental, referencias autorizadas y variantes. En una segunda etapa: repaint de regiones, acompañamiento, stems, loops y LRC según la variante. Entrenamiento LoRA queda separado de generación, con dataset, derechos, recursos y evaluación propios.

**3D y publicación.** Blender y training son extensiones posteriores, no dependencias del estudio inicial. Publicar también es otra capacidad: aprobar un render no autoriza a subirlo a todas las cuentas. Los campaign packs pueden preparar muchos medios relacionados, pero cada entrega conserva su contenido y destino aprobado.

## Arquitectura que evita duplicaciones

Faustus mantiene el plano de control: proyecto, autenticación, políticas, contexto, preflight, presupuesto, autorización y registros. Los modelos multimedia se ejecutan en procesos o entornos opcionales aislados. Studio, MCP y agentes usan el mismo API de dominio; ningún cliente llama al motor por una ruta privilegiada.

CreatorDocument representa canvas, storyboard, timeline, transcript o canción con revisión y contenido tipado. Los blobs permanecen en artifact occurrences. Las operaciones tienen command_id para deduplicación y expected_revision para concurrencia. Un conflicto produce 409 o una rama revisable, nunca último escritor gana sobre un cambio humano.

ProductionPlan fija brief, entradas, DAG, criterios, adapters y budget y se apoya en workflows. MediaRun conserva la autoridad del efecto multimedia. Un nodo puede enlazar un MediaRun y proyectarlo, pero no mantener otro status independiente sobre el mismo efecto. La baseline ya contiene outbox y reconciliación; esa es la base que se amplía.[^F02] [^F03]

El puerto de adapters incluye describir capacidades, planificar, enviar, consultar, cancelar, recoger y reconciliar. Debe clasificar rechazo antes de cola frente a aceptación incierta. Un timeout no autoriza un retry automático facturable. Un engine terminado no significa que el archivo ya se descargó y validó. Un resultado tardío no puede vencer el fencing de un intento cancelado.

La caché usa entradas y revisiones exactas, receta, parámetros efectivos, motor y componentes. Un cambio en captions invalida voz o exports dependientes, no necesariamente el vídeo fuente. La UI muestra el subgrafo afectado antes de gastar recursos. Reusar un resultado no garantiza que otra generación con la misma seed sea idéntica en cualquier hardware.

Los cuatro schemas y siete ejemplos adjuntos son referencia de diseño. Sus validaciones no sustituyen autorización, transacciones, sandbox ni pruebas reales de motor. Tampoco son contratos que se afirme que Faustus ya tenga desplegados.

## Orquestación y continuidad

Goal debe tener suelo y techo: no abandonar entregables pendientes, pero tampoco inventar nuevas tareas para seguir indefinidamente. Representa scope, criterios, presupuesto y política de interrupción. La evaluación puede continuar, completar, necesitar ayuda, detectar estancamiento o agotar presupuesto. Esos estados no sustituyen los estados individuales de los renders.

La finalización usa evidencia adecuada al criterio. Un vídeo debe existir y decodificar; una revisión artística necesita aceptación o evaluación identificada. Un evaluador puede sugerir otra estrategia, pero no aumentar permisos ni presupuesto. Dos vueltas con iguales acciones y fallos no cuentan como progreso sólo porque haya más texto.

El handoff conserva decisiones, pendientes, refs de documentos, estado externo incierto y límites. Su resumen explica el estado; no se convierte en autoridad de owner o permiso. Preparar y publicar son transiciones durables separadas. El receptor revalida revisiones y capacidades y no recibe aprobaciones reutilizables dentro de un bloque de texto.

Los especialistas creativos viven sobre los equipos actuales: guion, visual, edición, localización, música y QA cuando aporten. Declarar contexto por invocación, objetivo o proyecto. Las propuestas de dos agentes sobre la misma escena deben tener ownership y CAS; para código, usar alternativas/worktrees existentes donde corresponda. No copiar GB de medios por cada especialista.

Una corrección del usuario retira trabajo futuro obsoleto, pero no finge deshacer un efecto remoto ya enviado. Las pausas deben preceder efectos sensibles cuando sea posible. La reanudación no supone continuar en la misma línea de ejecución: los efectos necesitan idempotencia y reconciliación, una precaución también documentada en las interrupciones de LangGraph.[^W06]

## Hardware, seguridad y derechos

El repositorio reporta una RTX 4070 Ti de 12 GB y dos RTX 5060 Ti de 16 GB, además de 128 GB de RAM e incidentes por cargas simultáneas. Son datos de contexto, no un inventario medido aquí. Tres GPUs no constituyen una GPU única de 44 GB: repartir tareas independientes es distinto de repartir un solo modelo.[^F14]

El scheduler debe representar GPU UUID, host, RAM comprometida, CPU y scratch. Dos URLs pueden compartir una GPU. Preservar la serialización conservadora y habilitar concurrencia sólo tras pruebas de recursos disjuntos, con rollback. Priorizar el chat no significa matar a mitad una operación que no puede pausarse.

Local-only debe cubrir toda la cadena, incluidos plugins, TTS, embeddings, custom nodes y recursos externos, no sólo el LLM. Un proceso aislado en un venv no es automáticamente un sandbox. Una recipe importada es datos no confiables hasta revisar nodos, código y entradas. Si falta el sandbox requerido, no se ejecuta silenciosamente en el host.

Conservar y ampliar el gate existente de consentimiento para clonación de identidad real, con alcance y vigencia. Una voz sintética genérica y una persona real son casos distintos. Guardar una casilla no verifica por sí solo una identidad legal; registra la evidencia y decisión disponibles.

La licencia del código no resuelve por sí sola pesos, LoRA, fuentes, grabaciones y samples. Se verificó MIT en los archivos de CoS y ACE-Step; SteroidChat declara Apache-2.0 en README sin archivo raíz recuperado, y el repositorio de prompts carece de licencia explícita localizada. No copiar contenido sin resolver el permiso aplicable. El resto de integraciones requieren revisión legal de la versión concreta antes de redistribuir.[^D05] [^A02] [^D06]

## Roadmap y delegación

**Fase 0.** HEAD auditado, autoridades claras y observaciones estáticas convertidas en tests.

**Fase 1.** Dominio, biblioteca, shell, detalle de modelos, preflight y puerto de adapters.

**Fase 2.** Imagen por regiones, timeline, renderer y subtítulos; Goal acotado.

**Fase 3.** Localización, voces/mezcla, música inicial, storyboard, caché y handoff.

**Fase 4.** Vídeo especializado, edición musical, equipos, concurrencia y portabilidad.

**Fase 5.** Blender, training y entrega/publicación avanzada como extensiones opcionales.

La secuencia inicial es WP00/WP01, luego los contratos WP02 y WP06/WP07. A continuación se separan biblioteca, UI y runtime sobre interfaces fijadas. La primera experiencia completa combina vídeo/subtítulos o imagen; no espera a integrar todos los modelos.

Los 43 paquetes tienen dependencias, archivos iniciales, objetivo, pasos y aceptación. Un paquete grande puede necesitar varios PR homogéneos. Claude y Codex pueden asumir UI o backend por tarea; un integrador controla archivos compartidos, migraciones, agregadores de rutas, tool schemas y el cierre global. La asignación se hace por write set, no sólo por títulos de feature.

Cada PR demuestra comportamiento integrado, pruebas realmente ejecutadas, límites pendientes, documentación, feature flag y rollback. No declarar soporte de un motor por haber creado un endpoint, un botón o una fixture. No resolver regresiones con skip/xfail para aparentar cierre.

## Gates de calidad y forma de empezar

Los primeros golden paths son: vídeo local a subtítulos y export; imagen con máscara, variantes y undo; canción desde letra con escucha y export; continuidad con fallo y cambio de modelo; bundle portable tras habilitar intercambio. Cada uno tiene inputs conocidos, outputs que se pueden abrir y recibos de ejecución.

El harness de fallos cubre pérdida de respuesta tras submit, cancelación simultánea a completar, reinicio de worker, disco lleno, archivo corrupto, conflictos UI/agente, datos de otro owner y salida de red en local-only. Las pruebas con adapters falsos sirven para el contrato; las pruebas de modelos reales son una lane opt-in separada.

La calidad técnica comprueba archivos, schemas, tiempo y sincronía. La calidad artística se evalúa con criterios humanos o asistidos claramente identificados. Un modelo que se felicita por el resultado no es un test. Tampoco el número de tests o proveedores es una medida suficiente de madurez.

El paquete incluye un validador offline que comprueba 156 IDs, 43 paquetes con sus dependencias sin ciclos, cuatro schemas, siete ejemplos y casos negativos. No ejecuta Faustus ni instala nada. Su PASS significa que el plan es estructuralmente consistente, no que la aplicación esté implementada.

Para empezar, entregar a Claude o Codex `README_START_HERE.md`, el handoff correspondiente y la ficha WP00. Después asignar un paquete con dependencias cerradas, HEAD conocido, write set y presupuesto de pruebas. Usar el maestro como referencia, no como lectura obligatoria completa en cada turno. El objetivo del sistema es precisamente evitar que todos los agentes tengan que releer todo para hacer un cambio acotado.

**La diferencia buscada es que Faustus sepa producir, editar, verificar y continuar trabajo multimedia con control. Añadir modelos y botones es sólo una parte de ese producto.**

## Fuentes principales

Las notas enlazan las páginas y archivos utilizados. `sources.json` y `docs/14_FUENTES_COMPLETAS.md` contienen el inventario completo, alcance de lectura y revisiones; `docs/12_COBERTURA_Y_LIMITES.md` conserva todos los enlaces aportados y los posts no recuperados. Las referencias a ramas móviles son de navegación: fijarlas al implementar.

Faustus · README.md. [Enlace](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/README.md).

Faustus · src/media_edit_projects.py. [Enlace](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_edit_projects.py).

Faustus · src/media_subtitles.py. [Enlace](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_subtitles.py).

Faustus · src/model_capabilities.py. [Enlace](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/model_capabilities.py).

Faustus · src/model_calibration.py. [Enlace](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/model_calibration.py).

totec448-spec/chat-on-steroids · src/main/goal.ts. [Enlace](https://github.com/totec448-spec/chat-on-steroids/blob/99ec069cfbe7aae01e2ba9ab234c237ffbf609ff/src/main/goal.ts).

totec448-spec/chat-on-steroids · src/main/session/handoff.ts. [Enlace](https://github.com/totec448-spec/chat-on-steroids/blob/99ec069cfbe7aae01e2ba9ab234c237ffbf609ff/src/main/session/handoff.ts).

totec448-spec/chat-on-steroids · docs/plugins.md. [Enlace](https://github.com/totec448-spec/chat-on-steroids/blob/main/docs/plugins.md).

buluma/steroid-chat · README.md. [Enlace](https://github.com/buluma/steroid-chat/blob/master/README.md).

buluma/steroid-chat · steroid-chat-web/src/services/aiService.ts. [Enlace](https://github.com/buluma/steroid-chat/blob/master/steroid-chat-web/src/services/aiService.ts).

calin-ciobanu/killer_chatgpt_prompts · user_custom.json. [Enlace](https://github.com/calin-ciobanu/killer_chatgpt_prompts/blob/master/user_custom.json).

invoke-ai/InvokeAI · README.md. [Enlace](https://github.com/invoke-ai/InvokeAI/blob/main/README.md).

m-bain/whisperX · README.md. [Enlace](https://github.com/m-bain/whisperX/blob/main/README.md).

Huanshere/VideoLingo · README.md. [Enlace](https://github.com/Huanshere/VideoLingo/blob/main/README.md).

resemble-ai/chatterbox · README.md. [Enlace](https://github.com/resemble-ai/chatterbox/blob/master/README.md).

ace-step/ACE-Step-1.5 · README.md. [Enlace](https://github.com/ace-step/ACE-Step-1.5/blob/main/README.md).

OpenCut-app/OpenCut · README.md. [Enlace](https://github.com/OpenCut-app/OpenCut/blob/main/README.md).

OpenCut-app/opencut-classic · README.md. [Enlace](https://github.com/OpenCut-app/opencut-classic/blob/main/README.md).

AcademySoftwareFoundation/OpenTimelineIO · README.md. [Enlace](https://github.com/AcademySoftwareFoundation/OpenTimelineIO/blob/main/README.md).

Wan-Video/Wan2.2 · README.md. [Enlace](https://github.com/Wan-Video/Wan2.2/blob/main/README.md).

Lightricks/LTX-2 · README.md. [Enlace](https://github.com/Lightricks/LTX-2/blob/main/README.md).

Faustus · src/media_runs.py. [Enlace](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_runs.py).

Faustus · src/media_scheduler.py. [Enlace](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_scheduler.py).

LangGraph · interrupts. [Enlace](https://docs.langchain.com/oss/python/langgraph/interrupts).

Faustus · PENDIENTES.md. [Enlace](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/PENDIENTES.md).

totec448-spec/chat-on-steroids · LICENSE. [Enlace](https://github.com/totec448-spec/chat-on-steroids/blob/main/LICENSE).

ace-step/ACE-Step-1.5 · LICENSE. [Enlace](https://github.com/ace-step/ACE-Step-1.5/blob/main/LICENSE).

[^F01]: Faustus · README.md. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/README.md). Consulta: 13-09-2026.

[^F05]: Faustus · src/media_edit_projects.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_edit_projects.py). Consulta: 13-09-2026.

[^F06]: Faustus · src/media_subtitles.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_subtitles.py). Consulta: 13-09-2026.

[^F08]: Faustus · src/model_capabilities.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/model_capabilities.py). Consulta: 13-09-2026.

[^F09]: Faustus · src/model_calibration.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/model_calibration.py). Consulta: 13-09-2026.

[^D02]: totec448-spec/chat-on-steroids · src/main/goal.ts. [Fuente](https://github.com/totec448-spec/chat-on-steroids/blob/99ec069cfbe7aae01e2ba9ab234c237ffbf609ff/src/main/goal.ts). Consulta: 13-09-2026.

[^D03]: totec448-spec/chat-on-steroids · src/main/session/handoff.ts. [Fuente](https://github.com/totec448-spec/chat-on-steroids/blob/99ec069cfbe7aae01e2ba9ab234c237ffbf609ff/src/main/session/handoff.ts). Consulta: 13-09-2026.

[^D04]: totec448-spec/chat-on-steroids · docs/plugins.md. [Fuente](https://github.com/totec448-spec/chat-on-steroids/blob/main/docs/plugins.md). Consulta: 13-09-2026.

[^D06]: buluma/steroid-chat · README.md. [Fuente](https://github.com/buluma/steroid-chat/blob/master/README.md). Consulta: 13-09-2026.

[^D07]: buluma/steroid-chat · steroid-chat-web/src/services/aiService.ts. [Fuente](https://github.com/buluma/steroid-chat/blob/master/steroid-chat-web/src/services/aiService.ts). Consulta: 13-09-2026.

[^D09]: calin-ciobanu/killer_chatgpt_prompts · user_custom.json. [Fuente](https://github.com/calin-ciobanu/killer_chatgpt_prompts/blob/master/user_custom.json). Consulta: 13-09-2026.

[^A06]: invoke-ai/InvokeAI · README.md. [Fuente](https://github.com/invoke-ai/InvokeAI/blob/main/README.md). Consulta: 13-09-2026.

[^A04]: m-bain/whisperX · README.md. [Fuente](https://github.com/m-bain/whisperX/blob/main/README.md). Consulta: 13-09-2026.

[^A03]: Huanshere/VideoLingo · README.md. [Fuente](https://github.com/Huanshere/VideoLingo/blob/main/README.md). Consulta: 13-09-2026.

[^A05]: resemble-ai/chatterbox · README.md. [Fuente](https://github.com/resemble-ai/chatterbox/blob/master/README.md). Consulta: 13-09-2026.

[^A01]: ace-step/ACE-Step-1.5 · README.md. [Fuente](https://github.com/ace-step/ACE-Step-1.5/blob/main/README.md). Consulta: 13-09-2026.

[^A07]: OpenCut-app/OpenCut · README.md. [Fuente](https://github.com/OpenCut-app/OpenCut/blob/main/README.md). Consulta: 13-09-2026.

[^A08]: OpenCut-app/opencut-classic · README.md. [Fuente](https://github.com/OpenCut-app/opencut-classic/blob/main/README.md). Consulta: 13-09-2026.

[^A11]: AcademySoftwareFoundation/OpenTimelineIO · README.md. [Fuente](https://github.com/AcademySoftwareFoundation/OpenTimelineIO/blob/main/README.md). Consulta: 13-09-2026.

[^A10]: Wan-Video/Wan2.2 · README.md. [Fuente](https://github.com/Wan-Video/Wan2.2/blob/main/README.md). Consulta: 13-09-2026.

[^A09]: Lightricks/LTX-2 · README.md. [Fuente](https://github.com/Lightricks/LTX-2/blob/main/README.md). Consulta: 13-09-2026.

[^F02]: Faustus · src/media_runs.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_runs.py). Consulta: 13-09-2026.

[^F03]: Faustus · src/media_scheduler.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_scheduler.py). Consulta: 13-09-2026.

[^W06]: LangGraph · interrupts. [Fuente](https://docs.langchain.com/oss/python/langgraph/interrupts). Consulta: 13-09-2026.

[^F14]: Faustus · PENDIENTES.md. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/PENDIENTES.md). Consulta: 13-09-2026.

[^D05]: totec448-spec/chat-on-steroids · LICENSE. [Fuente](https://github.com/totec448-spec/chat-on-steroids/blob/main/LICENSE). Consulta: 13-09-2026.

[^A02]: ace-step/ACE-Step-1.5 · LICENSE. [Fuente](https://github.com/ace-step/ACE-Step-1.5/blob/main/LICENSE). Consulta: 13-09-2026.
