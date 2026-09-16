# Baseline, alcance y mapa de reutilización

## Decisión de producto

Creator debe convertir Faustus en un entorno de producción multimedia conversacional, no en un chat con un prompt de «director creativo». La unidad de trabajo será una producción editable: brief, fuentes, escenas, capas, pistas, decisiones, ejecuciones y entregables. El chat y los editores operarán sobre los mismos documentos versionados. Un modelo mejor debería aprovechar esa infraestructura inmediatamente; uno más débil debería beneficiarse de instrucciones acotadas, esquemas y verificaciones, sin que la aplicación le atribuya capacidades que no tiene.

El criterio de este plan no es «¿existe una función con el mismo nombre?», sino «¿hasta qué profundidad funciona, cómo se configura, qué puede fallar y qué evidencia permite darla por terminada?». Una función presente puede necesitar una mejora más importante que una nueva. Por eso el backlog distingue reutilización, integración, profundización, introducción candidata, auditoría y expansión opcional.

## Alcance de la evidencia

La baseline se fija en `Luissalet/Faustus@fa567977a00505b18a0fdf1b6e4d1a83f56270c6`, rama de referencia `master`. Se leyeron documentación, árboles y secciones de código de los subsistemas relevantes. No se ejecutó la aplicación, su suite, inferencia ni benchmarks de hardware. El número de tests que publica el README es una declaración del repositorio, no un resultado de esta revisión.[^F01]

Se recuperaron los tres repositorios nombrados junto a los enlaces de Google. No se comprobó la redirección de esos enlaces cortos: los nombres aportados permitieron leer los repositorios canónicos. Ninguno de los 15 posts de X dio contenido recuperable; no se sabe qué proyectos muestran. Todos permanecen en `sources.json`, sin atribuciones inventadas. La investigación de ACE-Step, Invoke, WhisperX, VideoLingo, Chatterbox, LTX, Wan y OpenCut es complementaria y explícitamente independiente de esos posts.

La revisión es suficiente para diseñar una ampliación fundada en las autoridades existentes, pero no para certificar ausencia de funciones en todo el repositorio ni compatibilidad de cada motor. Antes de implementar, WP00 debe comparar el HEAD con esta baseline y resolver diferencias. Una función «no localizada» no equivale a una función inexistente.

## Mapa de autoridades que se deben conservar

| Área | Autoridad o punto de extensión existente | Qué debe hacer Creator |
|---|---|---|
| Identidad de proyecto | `services/projects.py`, `src/project_identity.py` | Añadir perfil/configuración; conservar IDs, roots y enlaces de contexto. |
| Conversación y tools | Chat/Agent, `src/agent_runs.py`, `src/tool_registry.py` | Añadir herramientas y selecciones multimedia; no crear otro agente central. |
| Artefactos | `artifact_store`, `artifact_identity`, migración y provenance | Añadir metadatos y relaciones; no crear otra galería propietaria de los mismos bytes. |
| Render generativo | `media_runs`, `media_scheduler`, `media_backends` | Generalizar el contrato de motores manteniendo una única autoridad de estado. |
| Recetas | `media_workflows`, `config/media_workflows` | Ampliar inputs/requisitos y review; no permitir grafos arbitrarios autoejecutados. |
| Modelos | `model_capabilities`, lectores, calibración, launch receipts | Profundizar por tarea y deployment, no sustituir por listas hardcoded. |
| Recursos | `resource_admission`, `vram_admission`, topology, memory budget | Añadir contención física y presupuestos vectoriales, preservar defaults conservadores. |
| Orquestación | `workflows`, completion, strategy, dispatch | Producciones, objetivos y especialistas sobre los servicios actuales. |
| Control humano | Approval stores, provider policy, owner scopes, Attention | Los mismos límites para UI, voz, tools y clientes externos. |
| Conocimiento y creatividad | Context Engine, Story Bible, requirements, board | Unir canon creativo y evidencias de producción al contexto ya compartido. |

Las identidades y artefactos tienen respaldo en documentación y código leídos; las rutas restantes se citan como puntos de extensión observados o documentados, no como garantía de integración de todas sus variantes. El marcador de carpeta es corroborativo: `ProjectStore` sigue siendo la autoridad del proyecto.[^F01][^F12][^F13][^F16][^F17]

## De presencia a madurez funcional

| Capacidad | Baseline observada | Profundización propuesta | Cierre que importa |
|---|---|---|---|
| Edición de imagen | Capas, máscaras y operaciones de transformación limitadas. | Semántica de operaciones ordenada, controles regionales y paridad preview/export. | El resultado exportado reproduce la edición aceptada y se puede deshacer. |
| Subtítulos | Segmentos y exportadores SRT/VTT. | Palabras, hablantes, glosario, estilos, retiming y QA. | El texto está vinculado al audio correcto después de editar el montaje. |
| Voz | Narración local y servicios de voz configurables. | Casting, idiomas por variante, regeneración por frase y mezcla preservando ambiente. | No confundir narración con doblaje multihablante o sincronización labial. |
| Modelos | Vocabulario amplio, metadatos, probes y controles de lanzamiento. | Evidencia por campo y deployment; límites combinados; Model Explorer operativo. | Lo que el formulario ofrece se valida y el motor lo aplica o se declara no confirmado. |
| Renders | Outbox, reconciliación y recogida tras reinicio. | Puerto de adapters y ensamblado de producciones con el mismo lifecycle. | Un timeout no causa dos renders, y un archivo parcial no se declara entregado. |
| Multiagente | Equipos y delegación con control. | Vida de contexto, propiedad de escenas/documentos y continuidad semántica. | Dos especialistas no pisan una revisión ni amplían permisos mediante un resumen. |
| Workflows | Grafos durables y simulación estructural. | DAG de producción y reconstrucción por dependencias. | Cambiar un subtítulo no obliga a regenerar un vídeo no afectado. |

Estas distinciones se apoyan en los contratos multimedia, calibración y documentación del repositorio; el salto propuesto es de producto y de integración, no una negación de lo ya construido.[^F01][^F02][^F03][^F05][^F06][^F07][^F08][^F09]

## Observaciones estáticas que merecen pruebas antes de ampliar

**Fingerprint de receta.** En el método leído de `MediaWorkflow.fingerprint()` figuran identificador, versión, motor, inputs, computed, graph y consentimiento, pero no `models`, `requires_nodes` ni `outputs`. Hay que comprobar qué otras validaciones cubren estos campos y añadir tests de cambios materiales antes de decidir una migración de fingerprints. Es una observación de ese método, no una afirmación de bypass explotable.[^F04]

**Readiness de binarios.** `_run_version()` captura stdout/stderr de un subprocess, pero en el tramo leído no comprueba `returncode` antes de devolver éxito. Añadir fixture de salida no cero y separar «binario localizado», «ejecutable» y «motor apto para la operación». La comprobación de paquete importable tampoco demuestra que un modelo pueda cargarse.[^F07]

**Orden de composición.** El exportador leído transforma primero la base a partir del historial y compone las capas después. Eso no equivale a reproducir arbitrariamente todos los gestos de edición en orden cronológico. Definir alcance de cada operación y probar ejemplos mínimos de capa, crop y rotación; no cambiar el significado de proyectos guardados sin versión de esquema.[^F05]

**Identidad de calibración.** Compartir metadatos de pesos por digest es útil. Compartir resultados de una prueba entre servidores, configuraciones y motores distintos puede ser inadecuado. La clave Ollama leída se basa en el digest; hay que verificar todos sus usos y migrar la evidencia sin scope suficiente a `legacy_unknown`, nunca declararla falsa ni globalmente válida.[^F09]

**Documentación y conexión de pools.** `resource_admission.py` contiene un docstring que habla de integración pendiente con el lock global, mientras el README describe admission por pools. Puede ser documentación histórica. La acción correcta es seguir los callers del HEAD, no concluir a partir de una frase que todo está integrado o que nada lo está.[^F01][^F11]

## Qué no construir de nuevo

No reemplazar FastAPI/React/Electron por el stack de un donante. No introducir LangGraph como segundo orquestador, Redis/Celery como nueva obligación ni otra base de datos por comodidad del adaptador. Un backend especializado puede vivir en un entorno separado y hablar HTTP o IPC, pero Faustus conserva políticas, identidad, permisos, recibos y experiencia.

No cargar todo el catálogo de herramientas en cada turno. Creator debe facilitar descubrimiento progresivo por tarea y seleccionar pocos contratos claros. No trasladar binaries, node_modules, pesos ni repositorios ajenos enteros al árbol de Faustus. Los cambios pequeños y la revisión por autoridad son parte del plan de implementación, no una renuncia a su ambición.


[^F01]: Faustus · README.md. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/README.md). Consultado el 13 de septiembre de 2026. Capacidades generales, arquitectura y licencia declarada AGPL-3.0-or-later; resultados de pruebas reportados por el repositorio, no reproducidos aquí.

[^F12]: Faustus · services/projects.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/services/projects.py). Consultado el 13 de septiembre de 2026. Lectura 1–170: ProjectStore, context_items, ProjectExecutionContext e identidad estable; evitar segundo registro de proyectos.

[^F13]: Faustus · src/project_identity.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/project_identity.py). Consultado el 13 de septiembre de 2026. Lectura 1–190: marcador .faustus/project.json corroborativo, no segunda autoridad.

[^F16]: Faustus · árbol de media_backends. [Fuente](https://github.com/Luissalet/Faustus/tree/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_backends). Consultado el 13 de septiembre de 2026. ComfyUIBackend y pool; sólo se inspeccionó el inventario de este directorio.

[^F17]: Faustus · árbol de Studio. [Fuente](https://github.com/Luissalet/Faustus/tree/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/studio/src). Consultado el 13 de septiembre de 2026. Adaptadores imageTools, gallery, chat, cookbook, hardware, activity, attention y context observados; no evaluación visual en vivo.

[^F02]: Faustus · src/media_runs.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_runs.py). Consultado el 13 de septiembre de 2026. Lectura de líneas 1–250: preflight, consentimiento, outbox y estados de envío; no auditoría integral del módulo.

[^F03]: Faustus · src/media_scheduler.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_scheduler.py). Consultado el 13 de septiembre de 2026. Continuación y reconciliación de renders ya enviados; no vuelve a enviar grafos.

[^F05]: Faustus · src/media_edit_projects.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_edit_projects.py). Consultado el 13 de septiembre de 2026. Compositor no destructivo limitado, capas/máscaras inline, historial y orden de aplicación de export().

[^F06]: Faustus · src/media_subtitles.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_subtitles.py). Consultado el 13 de septiembre de 2026. Exportadores SRT/VTT; reutilizan el contrato de segmentos del vídeo local.

[^F07]: Faustus · src/media_capabilities.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_capabilities.py). Consultado el 13 de septiembre de 2026. Probes baratos; diferencia paquete instalado / modelo preparado; Piper no conectado al proveedor TTS en este módulo.

[^F08]: Faustus · src/model_capabilities.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/model_capabilities.py). Consultado el 13 de septiembre de 2026. Lectura 1–190: vocabulario canónico de familias/modalidades/capacidades/fuentes y mecanismos de controles.

[^F09]: Faustus · src/model_calibration.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/model_calibration.py). Consultado el 13 de septiembre de 2026. Lectura 1–200: identidad Ollama por digest, seis probes y distinción anunciado/probado.

[^F04]: Faustus · src/media_workflows.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_workflows.py). Consultado el 13 de septiembre de 2026. Lectura 1–190: inputs tipados, requisitos y fingerprint de MediaWorkflow.

[^F11]: Faustus · src/resource_admission.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/resource_admission.py). Consultado el 13 de septiembre de 2026. Lectura 1–165: pools, leases y normalización; docstring declara integración pendiente: reauditar callers porque puede estar desactualizado.
