# Roadmap y paquetes de implementación

## Orden de entrega

La ambición se conserva en el catálogo completo; la ejecución se ordena por dependencias y productos utilizables. Los 43 paquetes no equivalen necesariamente a 43 PR: un paquete L o XL puede necesitar varios PR homogéneos. No hay estimaciones de calendario ni promesas de rendimiento sin medición.

| Fase | Entrega | Gate de salida |
|---|---|---|
| 0 | Baseline actual, fuentes y pruebas de observaciones estáticas. | Se sabe qué se reutiliza y qué sigue sin verificar. No se ejecutan pesos ni herramientas externas por investigar. |
| 1 | Dominio, biblioteca, Creator shell, model intelligence y contrato de motores. | Proyecto legacy intacto; scopes, revisiones, preflight y deployment identity probados. |
| 2 | Imagen por regiones, timeline, renderer, transcripción/subtítulos y Goal acotado. | G1/G2 técnicos pasan con fixtures; las demos reales distinguen qué motor se ha probado. |
| 3 | Localización, voces/mezcla, Music Studio inicial, storyboard, caché y handoff. | Producción completa editable; recuperación y no duplicación de efectos comprobadas. |
| 4 | Vídeo especializado, edición musical, equipos, concurrencia probada, MCP y portabilidad. | Cada adapter tiene evidencia propia y los flujos existentes no se rompen. |
| 5 | Blender, entrenamiento y publicación avanzada. | Integraciones opcionales con pregunta de producto, licencia y gate de recursos específicos. |

Las fases agrupan entregas, pero el orden válido lo define el DAG de `backlog/workpacks.json`. Un paquete puede investigarse antes; no se habilita su comportamiento sin resolver sus dependencias. WP42 (fuentes X) es una línea de investigación paralela y no bloquea los cimientos independientes.

## Primera secuencia de implementación

Comenzar por WP00 y WP01. Fijar después las interfaces de WP02 (dominio) y WP06/WP07 (modelos). A partir de esas interfaces se separan biblioteca, UI y runtime. La combinación WP03/WP04/WP05/WP09/WP10/WP30 permite construir el estudio sin cambiar todavía las reglas conservadoras del hardware.

La primera producción técnica completa combina WP12–WP16 para vídeo/subtítulos y WP11/WP20 para imagen. No esperar a integrar música, 3D y todos los modelos para cerrar una experiencia usable. WP24 produce una primera Music Studio con generación/escucha; WP25 añade edición regional después, con capacidades por variante.

## Trabajo de Claude y Codex

Asignar roles por paquete y archivos, no por una presunta superioridad fija del modelo. Una distribución útil es un implementador de contratos/backend, otro de UI/editor y un integrador/revisor que pueda rotar. Ambos leen el mismo baseline, ADR y contratos. Se usan ramas/worktrees separadas para cambios concurrentes.

Son archivos de integración sensible `app.py`, agregadores de rutas, `core/database.py`, `src/agent_loop.py`, `src/tool_schemas.py` y configuración de build. Una persona/agente integrador debe poseerlos por tanda. No editar simultáneamente un archivo compartido porque dos tareas estén en fases distintas. Antes de iniciar cada paquete, declarar read set y write set reales del HEAD.

La máquina de Luis mantiene una lane pesada: no combinar suites completas, Vite y varias cargas grandes de modelos. Los agents pueden trabajar en código o documentación en paralelo donde no compartan archivos; eso no autoriza a sus procesos de prueba a competir por toda la memoria.

## Plantilla de PR

Cada PR registra: objetivo acotado; versión de baseline y diff relevante; feature IDs; archivos tocados y autoridad conservada; cambio de contrato/migración; pruebas ejecutadas con resultado real; evidencia aún pendiente; feature flag; rollback. No declarar «soporte de modelo añadido» si sólo se agregó una entrada al catálogo o una fixture.

Cierre: comportamiento visible cuando es un cambio de producto; endpoint integrado cuando corresponda; tests negativos de permisos y fallos; outputs reales si se promete generación; documentación exacta del alcance. No añadir `skip` o `xfail` para maquillar regresiones.

## Paquetes

Los paths marcados como «nuevo» son propuestas, no archivos cuya existencia se haya comprobado. Los demás son puntos de extensión observados/documentados: revalidarlos con WP00. Cada listado de pasos es la secuencia mínima del paquete; dividirlo en PR si mezcla clases de cambios.

<a id="wp00"></a>
### WP00 — Reauditoría del HEAD y mapa de autoridades
**Fase 0 · tamaño relativo S · lane integration.** Dependencias: ninguna.
**Objetivo:** Convertir la baseline fijada en un inventario contra el HEAD de implementación.
**Puntos de extensión:** `README.md`, `FAUSTUS.md`, `PENDIENTES.md`, `src`, `routes`, `studio/src`.
**Secuencia:** 1. Leer guías y calcular diff frente al commit base; 2. Trazar callers de media/modelos/pools y pantallas reales; 3. Clasificar capacidad documentada, localizada, integrada y probada; 4. Actualizar decisiones sin borrar funciones ya existentes.
**Aceptación:** Inventario enlaza archivo, función, ruta, pantalla y prueba donde existan; Toda ausencia no demostrada queda como no localizada; No descarga modelos ni inicia inferencia.
**Features:** EXT10.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp01"></a>
### WP01 — Regresiones de contratos existentes
**Fase 0 · tamaño relativo M · lane core.** Dependencias: WP00.
**Objetivo:** Comprobar antes de modificar las observaciones estáticas.
**Puntos de extensión:** `src/media_workflows.py`, `src/media_capabilities.py`, `src/media_edit_projects.py`, `tests`.
**Secuencia:** 1. Crear pruebas de fingerprint con cambios en requisitos/outputs; 2. Probar ffmpeg -version no cero; 3. Probar orden de capas/crop/rotación con imágenes mínimas; 4. Corregir sólo comportamientos confirmados, conservar compatibilidad y versión.
**Aceptación:** Pruebas reproducen el comportamiento antes del parche; No se presentan observaciones estáticas como exploits confirmados.
**Features:** QA01.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp02"></a>
### WP02 — Dominio Creator y revisiones
**Fase 1 · tamaño relativo M · lane core.** Dependencias: WP00.
**Objetivo:** Introducir perfil Creator sin duplicar proyecto, sesión o permisos.
**Puntos de extensión:** `services/projects.py`, `src/project_identity.py`, `src/creator/ (nuevo)`, `routes/creator_routes.py (nuevo)`.
**Secuencia:** 1. Añadir configuración opcional del espacio de trabajo; 2. Definir documentos Creator y expected_revision; 3. Resolver owner/project desde auth y ProjectStore; 4. Feature flag y lectura de proyectos legacy sin escritura masiva.
**Aceptación:** Chat y Agent conservan su semántica; Conflicto de revisión produce 409 sin pérdida; No se crea un registro paralelo de proyectos.
**Features:** UX01, QA05.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp03"></a>
### WP03 — Biblioteca y linaje multimedia
**Fase 1 · tamaño relativo M · lane assets.** Dependencias: WP02.
**Objetivo:** Reutilizar occurrences para medios, proxies y derivados.
**Puntos de extensión:** `src/artifact_store.py`, `src/artifact_identity.py`, `src/provenance_graph.py`, `src/creator/assets.py (nuevo)`.
**Secuencia:** 1. Añadir roles, padres y rangos temporales en metadatos; 2. Separar identidad de bytes y pertenencia; 3. Añadir miniaturas/waveforms como derivados; 4. Respetar tombstones y alcance owner.
**Aceptación:** Bytes idénticos no comparten permisos; Eliminar un enlace no destruye un medio compartido; No se incrustan vídeos en JSON.
**Features:** AST01, AST02, AST08, AST12.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp04"></a>
### WP04 — Ingesta y proxies acotados
**Fase 1 · tamaño relativo L · lane assets.** Dependencias: WP03.
**Objetivo:** Importar medios y carpetas de forma recuperable y segura.
**Puntos de extensión:** `src/upload_handler.py`, `src/media_inspection.py`, `src/creator/ingest.py (nuevo)`.
**Secuencia:** 1. Verificar magic bytes, tamaño, codecs y PTS; 2. Ingesta por chunks con checksum y progreso; 3. Generar proxies/miniaturas bajo recursos reservados; 4. Mantener límites actuales hasta que cada ruta nueva tenga pruebas.
**Aceptación:** Interrupción conserva sólo estado verificable; Ficheros malformados no bloquean servidor; Importar no envía a nube ni concede acceso a carpetas.
**Features:** AST03, AST04, RES06, QA07.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp05"></a>
### WP05 — Shell Creator conectado
**Fase 1 · tamaño relativo L · lane ui.** Dependencias: WP02, WP03.
**Objetivo:** Crear el espacio visual sin un segundo loop de chat.
**Puntos de extensión:** `studio/src/screens/creator/ (nuevo)`, `studio/src/adapters/creator.ts (nuevo)`, `studio/src/adapters/chat.ts`.
**Secuencia:** 1. Pestañas lienzo/timeline/transcripción/audio; 2. Biblioteca e inspector compartidos; 3. Selección contextual con revisión y playhead; 4. Borradores, tamaños de panel y atajos persistentes.
**Aceptación:** Volver de otro chat restaura selección y borrador; Acciones reales desde UI usan API existente o adaptadores tipados; Modo reducido funciona con teclado.
**Features:** UX02, UX03, UX04, UX06, UX07, UX09.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp06"></a>
### WP06 — Identidad modelo frente a despliegue
**Fase 1 · tamaño relativo L · lane models.** Dependencias: WP00.
**Objetivo:** Separar metadatos de pesos y observaciones por configuración ejecutada.
**Puntos de extensión:** `src/model_capabilities.py`, `src/model_calibration.py`, `src/model_capability_readers`.
**Secuencia:** 1. Crear ModelSpec y DeploymentFingerprint compatibles con registros previos; 2. Migrar probes viejos a scope legacy_unknown; 3. Clave incluye motor, revisión, configuración y hardware; 4. Conservar aliases/digests para deduplicar pesos.
**Aceptación:** Mismo digest en dos deployments no comparte resultados de rendimiento; Un probe sin identidad suficiente no se promueve a verificado.
**Features:** MOD01, MOD02, MOD19.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp07"></a>
### WP07 — Capacidades y parámetros multimodales
**Fase 1 · tamaño relativo L · lane models.** Dependencias: WP06.
**Objetivo:** Representar tareas, límites y dependencias entre parámetros.
**Puntos de extensión:** `src/model_capabilities.py`, `src/media_workflows.py`, `src/inference_capabilities.py`.
**Secuencia:** 1. Añadir task-specific input/output constraints; 2. Evidencia por campo con fecha y conflicto; 3. Describe controles visibles, no soportados y obligatorios; 4. Registrar componentes de pipeline, LoRA y compatibilidad de familia.
**Aceptación:** No se deriva edición o generación del mero soporte de visión; Una combinación imposible se rechaza antes de submit.
**Features:** MOD03, MOD04, MOD05, MOD06, MOD08, MOD20, QA06.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp08"></a>
### WP08 — Model Explorer y comparación
**Fase 1 · tamaño relativo L · lane ui.** Dependencias: WP05, WP07.
**Objetivo:** Hacer útil el detalle técnico para elegir y configurar.
**Puntos de extensión:** `studio/src/adapters/cookbook.ts`, `studio/src/adapters/hardware.ts`, `studio/src/screens/creator/models/ (nuevo)`.
**Secuencia:** 1. Filtros por tarea, idioma, coste, privacidad y memoria; 2. Comparar variantes y despliegues con desconocidos visibles; 3. Formularios generados desde schemas; 4. Explicar pérdida de capacidades al cambiar y conservar routing intent.
**Aceptación:** Campos no soportados no se envían silenciosamente; Comparación no equipara estimado con medido; Picker conserva selección explícita.
**Features:** UX11, MOD07, MOD10, MOD11, MOD12, MOD16, MOD17.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp09"></a>
### WP09 — Preflight unificado y presupuesto
**Fase 1 · tamaño relativo L · lane core.** Dependencias: WP07, WP02.
**Objetivo:** Planificar sin side effects y reservar presupuesto sin doble gasto.
**Puntos de extensión:** `src/provider_policy.py`, `src/autonomy_budget.py`, `src/workflow_cost_estimate.py`, `src/creator/preflight.py (nuevo)`.
**Secuencia:** 1. Resolver requisitos y privacidad de todas las etapas; 2. Estimar recursos y costes con intervalos y unknown; 3. Vincular aprobación al digest exacto; 4. Reservar y liquidar consumos con estado incierto.
**Aceptación:** Dos submits concurrentes no consumen dos veces el mismo permiso; Preflight no genera ni descarga; Cambio material invalida autorización.
**Features:** QA09.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp10"></a>
### WP10 — Puerto de adaptadores sobre MediaRun
**Fase 1 · tamaño relativo L · lane runtime.** Dependencias: WP01, WP03, WP07, WP09.
**Objetivo:** Generalizar motores sin duplicar la autoridad de ejecución.
**Puntos de extensión:** `src/media_runs.py`, `src/media_backends`, `src/media_scheduler.py`.
**Secuencia:** 1. Definir capability/plan/submit/status/cancel/collect/reconcile; 2. Envolver ComfyUI en el contrato; 3. Conservar IDs y estados históricos; 4. Harness de conformidad con adaptador falso determinista.
**Aceptación:** Crash entre POST y respuesta produce submit_unknown; Resultado tardío no vence cancelación; No cambia comportamiento de renders legacy.
**Features:** VID13, RES07, RES08.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp11"></a>
### WP11 — Recetas ComfyUI más expresivas
**Fase 2 · tamaño relativo L · lane media.** Dependencias: WP10.
**Objetivo:** Convertir referencia y máscaras de adjuntos en inputs verificables.
**Puntos de extensión:** `config/media_workflows`, `src/media_workflows.py`, `src/media_backends/comfyui.py`.
**Secuencia:** 1. Resolver occurrence a upload propio del motor; 2. Schemas dependientes de tarea y variantes; 3. Catálogo de recipes con fingerprint completo; 4. Review exacta de nodos y dependencias, sin instalar arbitrariamente.
**Aceptación:** Referencia enviada consta en recibo técnico; Rol textual no se etiqueta como conditioning; No se ejecutan nodos no revisados.
**Features:** IMG04, IMG09.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp12"></a>
### WP12 — Operaciones no destructivas comunes
**Fase 2 · tamaño relativo L · lane core.** Dependencias: WP02, WP03, WP01.
**Objetivo:** Historial ordenado, revisiones y undo/redo para imagen y tiempo.
**Puntos de extensión:** `src/media_edit_projects.py`, `src/creator/operations.py (nuevo)`.
**Secuencia:** 1. Definir operaciones tipadas y versionadas; 2. Aplicar en orden semántico explícito; 3. Sustituir blobs inline por referencias mediante migración aditiva; 4. CAS para cambios UI/agente.
**Aceptación:** Deshacer no pierde originales; Capas y transforms producen el orden mostrado; Reintento de command_id no duplica operación.
**Features:** AST07, IMG01.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp13"></a>
### WP13 — Timeline y relojes
**Fase 2 · tamaño relativo XL · lane timeline.** Dependencias: WP12, WP05.
**Objetivo:** Un editor temporal correcto antes de añadir IA al montaje.
**Puntos de extensión:** `src/creator/timeline.py (nuevo)`, `studio/src/screens/creator/timeline/ (nuevo)`.
**Secuencia:** 1. Frames racionales y muestras de audio; 2. Pistas, clips, trims, splits, ripple, snapping y markers; 3. Mapas de retiming y clips anidados acotados; 4. Vista virtualizada y proxy seeking.
**Aceptación:** Cortes no acumulan error float; VFR se indexa por PTS o proxy documentado; Editar sólo cambia revisión y no renderiza sin orden.
**Features:** VID01, VID02, VID09, SUB10.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp14"></a>
### WP14 — Renderer FFmpeg reproducible
**Fase 2 · tamaño relativo L · lane media.** Dependencias: WP10, WP13, WP04.
**Objetivo:** Compilar operaciones autorizadas a un render observable.
**Puntos de extensión:** `src/media_transforms.py`, `src/creator/render.py (nuevo)`.
**Secuencia:** 1. Argv tipado y filtros permitidos, sin shell libre; 2. Progreso parseado y colas/cancelación por proceso propio; 3. Exportar perfiles explícitos y recibo de parámetros efectivos; 4. Validar decodificación, duración y streams antes de completed.
**Aceptación:** Archivo final existe y decodifica; Cancelación no mata procesos ajenos; Salida parcial nunca se etiqueta final.
**Features:** VID10, VID12.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp15"></a>
### WP15 — Transcripción alineada y workers ASR
**Fase 2 · tamaño relativo L · lane speech.** Dependencias: WP10, WP04.
**Objetivo:** Añadir WhisperX opcional sin romper el flujo corto local.
**Puntos de extensión:** `services/local_video.py`, `services/speech_runtime.py`, `src/creator/transcripts.py (nuevo)`.
**Secuencia:** 1. Worker aislado por versiones; 2. Segmentos/palabras/idioma/speaker_id con confianza nullable; 3. Alineadores por idioma y diarización opt-in; 4. Particiones con offsets y deduplicación de bordes.
**Aceptación:** Un speaker_id no se presenta como identidad humana; No se descargan pesos al importar; Regresión del exportador actual pasa.
**Features:** SUB01, SUB02, SUB03.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp16"></a>
### WP16 — Editor de subtítulos
**Fase 2 · tamaño relativo L · lane ui.** Dependencias: WP13, WP15.
**Objetivo:** Editar texto, timing y estilo con validación real.
**Puntos de extensión:** `src/media_subtitles.py`, `studio/src/screens/creator/subtitles/ (nuevo)`.
**Secuencia:** 1. Vista waveform-texto sincronizada; 2. Perfiles CPS/CPL/líneas/duración por idioma; 3. SRT/VTT/ASS con escaping y formatos de salida explícitos; 4. Subtítulo quemado/sidecar y comparación con original.
**Aceptación:** Edición humana prevalece sobre transcripción tardía; Solapes legítimos tienen representación propia; Roundtrip conserva cues admitidos.
**Features:** SUB04, SUB05, SUB08, SUB09.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp17"></a>
### WP17 — Localización y glosarios
**Fase 3 · tamaño relativo L · lane speech.** Dependencias: WP16, WP07, WP09.
**Objetivo:** Traducción revisable y alineada al proyecto.
**Puntos de extensión:** `src/creator/localization.py (nuevo)`, `src/workflows`.
**Secuencia:** 1. Glosario versionado y traducción por contexto de escena; 2. Propuesta-reflexión-adaptación con coste visible; 3. Mapas fuente/destino y flags de expansión; 4. Reintentar sólo cues invalidados.
**Aceptación:** Cambio de glosario invalida lo afectado; Números y nombres tienen comprobaciones; No se impone velocidad TTS extrema para encajar.
**Features:** SUB06, SUB07, SUB11, SUB12.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp18"></a>
### WP18 — Casting de voces y TTS
**Fase 3 · tamaño relativo L · lane speech.** Dependencias: WP10, WP07, WP09.
**Objetivo:** Voces de proyecto con capacidades y consentimiento por operación.
**Puntos de extensión:** `services/speech_runtime.py`, `src/media_consent.py`, `src/creator/voices.py (nuevo)`.
**Secuencia:** 1. Adaptar voces existentes y TTS opcional Chatterbox; 2. Casting, pronunciación y controles soportados por variante; 3. Consentimiento owner/identidad/alcance/revisión/caducidad; 4. Render por frases con pausas y regeneración local.
**Aceptación:** Revocar bloquea trabajos aún no autorizados a ejecutar; No se presenta Turbo inglés como multilingüe; Reemplazar audio original exige elección explícita.
**Features:** AUD01, AUD02, AUD03, AUD04, AUD10, MOD14.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp19"></a>
### WP19 — Edición y mezcla de audio
**Fase 3 · tamaño relativo L · lane media.** Dependencias: WP14, WP18.
**Objetivo:** Preservar música y ambiente además de narración.
**Puntos de extensión:** `src/creator/audio.py (nuevo)`, `studio/src/screens/creator/audio/ (nuevo)`.
**Secuencia:** 1. Pistas stems, ganancia, fades y ducking; 2. Loudness/true peak configurable con medidas; 3. A/B sincronizado y controles de rango; 4. Denoise/separación como adapters opcionales con licencia propia.
**Aceptación:** La mezcla conserva las pistas elegidas; Clipping y silencios inesperados se informan; El original no se sobrescribe.
**Features:** AUD05, AUD06, AUD07, AUD08.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp20"></a>
### WP20 — Canvas de imagen por capas
**Fase 2 · tamaño relativo XL · lane ui.** Dependencias: WP05, WP12, WP11.
**Objetivo:** Integrar composición manual y generación regional en el mismo lienzo.
**Puntos de extensión:** `studio/src/adapters/imageTools.ts`, `studio/src/screens/creator/canvas/ (nuevo)`, `src/media_edit_projects.py`.
**Secuencia:** 1. Capas raster/máscara/texto/control con transforms; 2. Brush/selección y arrastre con intención explícita; 3. Preview vs commit y comparación versiones; 4. Crop, outpaint y export con transparencia.
**Aceptación:** Selección corresponde a coordenadas y revisión exactas; Cerrar conserva máscaras/capas; Export coincide con operaciones soportadas.
**Features:** UX05, IMG02, IMG03, IMG05, IMG08, IMG13.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp21"></a>
### WP21 — Conditioning y variantes visuales
**Fase 3 · tamaño relativo L · lane media.** Dependencias: WP20, WP08.
**Objetivo:** Referencias de personaje/estilo/composición técnicamente trazables.
**Puntos de extensión:** `config/media_workflows`, `src/creator/references.py (nuevo)`, `src/story_bible.py`.
**Secuencia:** 1. Pesos/strength/seed por input compatible; 2. Regiones, pose/depth/edge y LoRA permitidas; 3. Matrices de variantes y selección humana; 4. Fijar canon y marcar deriva como revisión pendiente.
**Aceptación:** Ninguna promesa de identidad perfecta; Cada variante conserva manifiesto propio; Una opción no soportada bloquea o pide cambio explícito.
**Features:** AST06, IMG06, IMG07, IMG10, IMG11.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp22"></a>
### WP22 — Storyboard y plan de producción
**Fase 3 · tamaño relativo L · lane ui.** Dependencias: WP13, WP11, WP09.
**Objetivo:** Un brief produce un plan editable de planos y recursos.
**Puntos de extensión:** `src/story_bible.py`, `src/workflows`, `src/creator/storyboard.py (nuevo)`.
**Secuencia:** 1. Escenas con duración, cámara, voz, fuentes y restricciones; 2. Presupuesto por plano y preview animatic; 3. Cambios por selección sin rehacer todo; 4. Especialistas producen propuestas no autorizaciones.
**Aceptación:** Storyboard sin modelo de vídeo sigue siendo editable; Canon aceptado no se reescribe automáticamente; Aprobación vincula versión de plan.
**Features:** IMG12, VID03, VID04, VID08, VID11, AUD09, MUS11, ORC11.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp23"></a>
### WP23 — Adapters de vídeo especializados
**Fase 4 · tamaño relativo XL · lane media.** Dependencias: WP22, WP10, WP07.
**Objetivo:** Integrar familias Wan/LTX mediante recipes/versiones probadas.
**Puntos de extensión:** `src/media_backends`, `config/media_workflows`, `src/creator/video.py (nuevo)`.
**Secuencia:** 1. Separar local y hosted por deployment; 2. Matrices T2V/I2V/S2V/retake/extend reales; 3. Probe barato y prueba GPU opt-in corta por variante; 4. Exportar clips listos para timeline, sonido separable.
**Aceptación:** No se copia a local la capacidad exclusiva de API; VRAM y componentes se miden en instalación; Fallo conserva planos aprobados.
**Features:** VID05, VID06, VID07, MOD15.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp24"></a>
### WP24 — Music Studio y generación
**Fase 3 · tamaño relativo L · lane music.** Dependencias: WP10, WP08, WP09.
**Objetivo:** Usar ACE-Step detrás de un flujo musical propio.
**Puntos de extensión:** `src/media_backends`, `src/creator/music.py (nuevo)`, `studio/src/screens/creator/music/ (nuevo)`.
**Secuencia:** 1. Adaptador REST con versiones fijadas; 2. Letra estructurada, BPM, tonalidad, compás, duración e instrumental; 3. Modo sencillo y experto con mismas entidades; 4. Variantes y escucha antes de adoptar.
**Aceptación:** Generar no implica entrenar ni bajar pesos; Inputs imposibles se rechazan por variante; Canción se guarda como occurrence con recibo.
**Features:** MUS01, MUS02, MUS03, MUS04, MUS05, MOD13.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp25"></a>
### WP25 — Edición musical por regiones
**Fase 4 · tamaño relativo L · lane music.** Dependencias: WP24, WP19, WP13.
**Objetivo:** Continuar, reparar y ensamblar música sin rehacer todo.
**Puntos de extensión:** `src/creator/music.py (nuevo)`, `studio/src/screens/creator/music/ (nuevo)`.
**Secuencia:** 1. Rangos repaint y crossfades; 2. Stems/capas/acompañamiento si la variante lo permite; 3. LRC y lyric-video con revisión timing; 4. Comparación de loops y finales, export pistas.
**Aceptación:** extract/lego/complete no se habilitan indiscriminadamente en turbo/sft; Regenerar puente no reemplaza estrofas aceptadas; Rango exacto y handles figuran en plan.
**Features:** MUS06, MUS07, MUS08, MUS09, MUS10.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp26"></a>
### WP26 — Caché y reconstrucción incremental
**Fase 3 · tamaño relativo L · lane runtime.** Dependencias: WP12, WP14, WP22.
**Objetivo:** Recalcular sólo los derivados afectados, sin caché engañosa.
**Puntos de extensión:** `src/provenance_graph.py`, `src/creator/dependencies.py (nuevo)`, `src/workflows`.
**Secuencia:** 1. DAG de inputs/revisiones/modelos/recetas; 2. Fingerprint de ejecución completo; 3. Invalidación por alcance temporal; 4. Concurrencia de solicitudes iguales conserva occurrences y permisos.
**Aceptación:** Cambiar un título no regenera vídeo fuente; Un backend cambiado invalida evidencia no comparable; Cache hit explica procedencia y no cruza owners.
**Features:** AST05, ORC12.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp27"></a>
### WP27 — Goal con finalización por evidencia
**Fase 2 · tamaño relativo L · lane agents.** Dependencias: WP00, WP09.
**Objetivo:** Mejorar continuidad sobre las autoridades existentes.
**Puntos de extensión:** `src/completion_engine`, `src/autonomy_budget.py`, `src/strategy_policy.py`, `src/creator/goal_contract.py (nuevo)`.
**Secuencia:** 1. Objetivo versionado con criterios medibles; 2. Estados complete/continue/needs_human/stalled/budget_exhausted; 3. Comprobar artefactos y pruebas antes de éxito; 4. Detector de no progreso y deduplicación de continuación.
**Aceptación:** Un mensaje «terminado» no cierra entregables ausentes; El loop no inventa trabajo fuera del objetivo; Cambiar objetivo retira continuaciones obsoletas.
**Features:** ORC01, ORC02, ORC03, ORC13.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp28"></a>
### WP28 — Handoff y compactación fiables
**Fase 3 · tamaño relativo L · lane agents.** Dependencias: WP27, WP06.
**Objetivo:** Continuar con otra sesión/modelo sin confundir resumen y estado.
**Puntos de extensión:** `src/context_compactor.py`, `src/condense.py`, `src/agent_runs.py`, `src/creator/handoffs.py (nuevo)`.
**Secuencia:** 1. Capsula tipada con refs, criterios y pendientes; 2. Prepare/publish con recuperación transaccional; 3. Revalidar versiones, permisos y capacidades de destino; 4. Verificar suficiencia semántica y conservar originales.
**Aceptación:** No se publica handoff truncado; No se transfieren autorizaciones ya consumidas; Modelo receptor puede abrir evidencia exacta sin releer todo.
**Features:** ORC04, ORC05, ORC06.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp29"></a>
### WP29 — Equipos con estado y propiedad claros
**Fase 4 · tamaño relativo L · lane agents.** Dependencias: WP28, WP22.
**Objetivo:** Organizar especialistas de producción o código con vidas de contexto explícitas.
**Puntos de extensión:** `src/dispatch.py`, `src/external_worker.py`, `src/agent_profiles`, `src/subagent_permissions.py`.
**Secuencia:** 1. Per-task/per-project context y límites; 2. Propiedad de documentos/planos y worktrees para código; 3. Steering durable por revisión y feedback; 4. Único integrador aplica propuestas aceptadas.
**Aceptación:** Dos especialistas no pisan el mismo documento; Cancelar padre cancela pasos futuros y reconcilia externos; Una identidad persistente no implica permiso persistente.
**Features:** ORC07, ORC08, ORC09, ORC10.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp30"></a>
### WP30 — Recursos físicos y modo conservador
**Fase 1 · tamaño relativo L · lane runtime.** Dependencias: WP09, WP00.
**Objetivo:** Representar contención real sin relajar la seguridad actual.
**Puntos de extensión:** `src/resource_admission.py`, `src/vram_admission.py`, `src/gpu_topology.py`, `src/memory_budget.py`.
**Secuencia:** 1. Inventario GPU UUID + RAM commit + scratch + CPU; 2. Alias endpoint→recursos y avisos topología; 3. Reservas predictivas y revalidación antes de ejecución; 4. Mantener serialización actual por defecto.
**Aceptación:** Dos endpoints de la misma GPU no duplican capacidad; 12+16+16 no se muestran como dispositivo único; Un desconocido no se convierte en capacidad libre.
**Features:** MOD09, RES01, RES02, RES03.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp31"></a>
### WP31 — Concurrencia medida y multiworker
**Fase 4 · tamaño relativo XL · lane runtime.** Dependencias: WP30, WP10, WP36.
**Objetivo:** Habilitar paralelismo sólo para recursos realmente independientes.
**Puntos de extensión:** `src/resource_admission.py`, `src/media_backends/pool.py`, `src/dispatch.py`.
**Secuencia:** 1. Medir contención y memoria con plan autorizado; 2. Per-device leases y afinidad de modelo; 3. Fairness y prioridad de chat sin matar efectos activos; 4. Pools remotos separados, aislamiento y reconexión.
**Aceptación:** Activa opt-in sólo tras pruebas de dos recursos disjuntos; Fallo de worker no libera reserva externa no reconciliada; No promete pooling de VRAM por software.
**Features:** RES04, RES05, RES10.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp32"></a>
### WP32 — Lifecycle de plugins seguro
**Fase 2 · tamaño relativo L · lane extensions.** Dependencias: WP00, WP09.
**Objetivo:** Profundizar MCP existente con instalación reproducible y rollback.
**Puntos de extensión:** `src/mcp_manager.py`, `src/extension_manifest.py`, `src/skill_import_review.py`, `src/skill_governance.py`.
**Secuencia:** 1. Manifests/versiones/envs aislados y secretos por propietario; 2. Descubierto, instalado, conectado, publicado y probado separados; 3. Update staged y rollback con datos retenidos; 4. Schemas exactos, límites, conflictos y revocación inmediata.
**Aceptación:** Ready requiere prueba pertinente; Plugin no hereda sandbox ficticio; Un fallo de transporte no reintenta mutación.
**Features:** EXT01, EXT02, EXT03, EXT04.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp33"></a>
### WP33 — MCP Creator y captura opcional
**Fase 4 · tamaño relativo L · lane extensions.** Dependencias: WP32, WP10, WP05.
**Objetivo:** Exponer las mismas operaciones a clientes externos sin otra autoridad.
**Puntos de extensión:** `src/builtin_mcp.py`, `src/tool_registry.py`, `src/creator/tool_api.py (nuevo)`.
**Secuencia:** 1. Tools discover/plan/edit/render/status con scopes; 2. Mismo owner y mismo API que Studio; 3. Captura explícita de página/selección mediante extensión opcional; 4. Cambios de esquema versionados y referencias invalidadas.
**Aceptación:** MCP no recibe permisos superiores al usuario; Sin cookies exportadas ni scraping de sesiones del proveedor; Disponibilidad de cliente y cuotas no se prometen.
**Features:** EXT05, EXT06.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp34"></a>
### WP34 — Exportación portable y OTIO
**Fase 4 · tamaño relativo L · lane assets.** Dependencias: WP26, WP13, WP03.
**Objetivo:** Entregar proyectos editables y medios sin falsa completitud.
**Puntos de extensión:** `src/project_export.py`, `src/creator/portable.py (nuevo)`.
**Secuencia:** 1. Manifest inventario hashes/versiones/licencias; 2. Bundle con originales/proxies opcionales y faltantes explícitos; 3. OTIO subset y reporte de pérdidas de efectos; 4. Import seguro sin ejecutar hooks ni sobrescribir.
**Aceptación:** Bundle completo contiene todo lo declarado; Missing assets bloquea export completo; Reimport en carpeta nueva conserva relaciones autorizadas.
**Features:** AST09, AST10, AST11.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp35"></a>
### WP35 — Revisión móvil y accesibilidad
**Fase 4 · tamaño relativo M · lane ui.** Dependencias: WP05, WP16, WP20.
**Objetivo:** Revisar desde pantalla pequeña sin migrar a Cordova.
**Puntos de extensión:** `studio/src/screens/creator`, `studio/src/adapters/activity.ts`.
**Secuencia:** 1. Layout responsive y carga diferida; 2. Comentarios por tiempo/región; 3. Acciones de aprobación equivalentes y explícitas; 4. Atajos, foco, reducción de movimiento y estado offline.
**Aceptación:** No pierde borrador al reconectar; Pantalla pequeña no permite aprobar a ciegas; No carga vídeos enteros en memoria para lista.
**Features:** UX08, UX10.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp36"></a>
### WP36 — Harness de pruebas de adaptadores y fallos
**Fase 2 · tamaño relativo L · lane quality.** Dependencias: WP10, WP30.
**Objetivo:** Probar el contrato antes de ampliar motores.
**Puntos de extensión:** `tests`, `studio/checks`, `tests/helpers`.
**Secuencia:** 1. Fixtures corruptos/VFR/audio solapado; 2. Fault injection submit/collect/cancel/restart; 3. Pruebas owner/privacy/presupuesto; 4. Diferenciar mock CI de hardware opt-in.
**Aceptación:** Todos los estados inciertos tienen prueba; Un stub passing no se presenta como soporte del motor; Reporte separa niveles de evidencia.
**Features:** IMG14, MOD18, RES09, EXT08, QA02, QA03.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp37"></a>
### WP37 — Golden paths y release gates
**Fase 3 · tamaño relativo L · lane integration.** Dependencias: WP14, WP16, WP20, WP24, WP27, WP36.
**Objetivo:** Cerrar flujos reales sin maquillar lo no probado.
**Puntos de extensión:** `tests`, `studio/checks`, `docs/showcase.md`, `PENDIENTES.md`.
**Secuencia:** 1. Video local→subtítulos→montaje export; 2. Imagen con máscara→variante→comparación; 3. Canción generada con motor configurado opt-in; 4. Prueba cierre navegador/reinicio y export portable cuando WP34 esté disponible.
**Aceptación:** Cada demo tiene entrada, salida decodificable y recibo; No requiere todos los motores para usar Creator; No se declara lista una variante sin ejecución pertinente.
**Features:** VID14, QA04, QA08.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp38"></a>
### WP38 — 3D mediante Blender opcional
**Fase 5 · tamaño relativo XL · lane optional.** Dependencias: WP32, WP22, WP36.
**Objetivo:** Añadir escenas y renders 3D como extensión separada.
**Puntos de extensión:** `src/creator/blender.py (nuevo)`, `src/mcp_manager.py`.
**Secuencia:** 1. Read-only scene probe; 2. Operaciones tipadas de cámara/luces/materiales; 3. Render turntable y export de formatos admitidos; 4. Puente texturas/depth/normals a image conditioning.
**Aceptación:** No se ejecuta Python arbitrario de un archivo importado; Blender instalado no basta para ready; Sin promesa de calidad 3D desde cualquier modelo.
**Features:** ADV01, ADV02, ADV04.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp39"></a>
### WP39 — LoRA y conjuntos creativos opcionales
**Fase 5 · tamaño relativo XL · lane optional.** Dependencias: WP24, WP21, WP30, WP36.
**Objetivo:** Personalización con datos autorizados y evaluación separada.
**Puntos de extensión:** `src/creator/training.py (nuevo)`, `src/media_consent.py`, `src/bench`.
**Secuencia:** 1. Dataset lineage/licencias/consentimiento; 2. Preflight GPU/scratch y entornos separados; 3. Entrenamiento recuperable sólo si backend soporta checkpoints; 4. Comparador heldout antes de promover adaptador.
**Aceptación:** Nunca entrena como efecto de generar una imagen; No memoriza por defecto toda biblioteca; Checkpoint incompleto no se instala.
**Features:** MUS12, ADV03, ADV06.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp40"></a>
### WP40 — Publicación y entrega aprobada
**Fase 5 · tamaño relativo L · lane optional.** Dependencias: WP34, WP32, WP09.
**Objetivo:** Preparar paquetes y conectores de publicación por destino.
**Puntos de extensión:** `src/connector_outbox.py`, `src/creator/delivery.py (nuevo)`.
**Secuencia:** 1. Títulos/portadas/subtítulos/formatos por canal; 2. Aprobación de destinatario/contenido/hash/fecha; 3. Outbox, recibo remoto y reconciliación incierta; 4. Reutilizar delivery donde exista.
**Aceptación:** Renderizar no publica; Cambiar archivo invalida permiso de envío; Aceptación remota no se confunde con entrega final.
**Features:** EXT09, ADV05.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp41"></a>
### WP41 — Presets, evaluación y descubrimiento
**Fase 3 · tamaño relativo M · lane agents.** Dependencias: WP07, WP27, WP32.
**Objetivo:** Convertir prompts de especialidad en capacidades configurables y evaluadas.
**Puntos de extensión:** `src/preset_manager.py`, `src/behavior_modes.py`, `src/recipes.py`, `src/bench`.
**Secuencia:** 1. Presets propios con inputs/salidas/esquemas; 2. Versiones, procedencia y tests ES/EN; 3. A/B con budget fijo y fallos regresivos; 4. Descubrir tools por intención sin ampliar permisos.
**Aceptación:** Un preset nunca borra política superior; No se copian prompts sin licencia; Mejoría sólo se afirma con tarea y métrica documentadas.
**Features:** UX12, ORC14, EXT07.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.

<a id="wp42"></a>
### WP42 — Resolver fuentes X sin atribuciones inventadas
**Fase 0 · tamaño relativo S · lane research.** Dependencias: WP00.
**Objetivo:** Completar por separado la cobertura de enlaces inaccesibles.
**Puntos de extensión:** `docs/01_FUENTES_Y_DONANTES.md`, `sources.json`.
**Secuencia:** 1. Obtener texto o enlace canónico con acceso autorizado; 2. Verificar repo/versión/licencia real; 3. Comparar granularidad con catálogo y crear delta; 4. No bloquear los cimientos independientes de estas fuentes.
**Aceptación:** Cada uno de los 15 posts queda resuelto o explícitamente pendiente; Ninguna feature se atribuye a un proyecto adivinado.
**Features:** QA10.
**Cierre técnico:** ejecutar pruebas focalizadas del comportamiento y sus errores, comprobar integración de sus callers, mantener flag/rollback, y dejar explícitas las pruebas de motor real no ejecutadas.
