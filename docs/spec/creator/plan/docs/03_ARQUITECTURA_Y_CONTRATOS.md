# Arquitectura y contratos de integración

## Capas y responsabilidades

La arquitectura propuesta conserva el servidor Faustus como plano de control: autenticación, proyecto, política, planificación, admisión, autorización y registro. Los motores especializados constituyen un plano de ejecución aislado. Studio, MCP y los agentes son clientes del mismo dominio; ninguno tiene una puerta trasera hacia un motor con menos validación.

```text
Studio Creator / Chat / MCP autorizado
                  |
      API de dominio y operaciones tipadas
                  |
   ProjectStore + documentos revisionados
                  |
  Policies -> Preflight -> Approval/Budget
                  |
    Workflows / MediaRun / Admission existentes
                  |
   Adapter ComfyUI | FFmpeg | ASR | TTS | Música | Vídeo
                  |
     Outputs -> validación -> artifact occurrences
                  |
       Provenance / Context / Attention
```

Este esquema es una especificación, no un diagrama de una implementación ya terminada. La baseline de MediaRun, continuador y proyectos soporta los puntos de reutilización señalados.[^F02][^F03][^F12]

## Entidades y autoridad

**Project** conserva el ID y owner resueltos por `ProjectStore`. Un `CreatorProfile` añade preferencias, defaults y referencias a documentos multimedia; no introduce un registro de pertenencia paralelo. El marcador del workspace sigue siendo corroborativo y puede regenerarse desde su autoridad.[^F12][^F13]

**CreatorDocument** es un agregado editorial con ID, project_id, schema_version, revisión y contenido tipado: canvas, timeline, transcript, song o storyboard. Los blobs viven como artefactos. Las operaciones usan `command_id` para deduplicación y `expected_revision` para concurrencia optimista. Los estados aceptado/propuesto se conservan explícitamente.

**ArtifactOccurrence** sigue siendo la unidad de autorización y procedencia de una salida. Los bytes pueden deduplicarse por hash, pero ese hash no da acceso. Proxies y miniaturas tienen relaciones `derived_from` y receta; no se transforman en una nueva identidad compartida entre propietarios. Un export de proyecto enumera occurrences concretas, no sólo hashes globales.[^F01]

**ProductionPlan** fija brief_revision, documentos/revisiones de entrada, DAG, criterios, constraints, adapters y política de presupuesto. Se compila al sistema de workflows existente. **MediaRun** es la autoridad del efecto de ejecución de cada operación multimedia. Cuando un nodo de workflow referencia un MediaRun, el nodo proyecta y enlaza su resultado; no crea otro motor de estado que pueda contradecirlo.

**ModelSpec** identifica pesos/componentes. **DeploymentManifest** identifica cómo se sirven: motor, versión, revisión del modelo, configuración efectiva y recursos. **CapabilityEvidence** registra qué se sabe de ese despliegue y bajo qué condiciones. No duplicar el vocabulario canónico de `model_capabilities`; ampliarlo y hacer que los adapters lo consuman.[^F08][^F09]

## Persistencia y migraciones

No decidir «todo SQL» o «todo JSON» por preferencia del implementador. Los proyectos ya son file-backed y los runs usan persistencia propia. Los nuevos documentos editoriales requieren CAS robusto, historial y transacciones pequeñas. WP00 debe elegir el seam de almacenamiento que permita estos invariantes dentro del patrón existente. El API de dominio no puede depender de que todos los clientes compartan un `threading.Lock` de un único proceso.

Las migraciones son aditivas: leer el esquema antiguo, conservar ID y original, escribir un documento nuevo sólo al migrar explícitamente o guardar. Un historial de imagen legacy conserva su semántica de exportación original. Introducir `operation_semantics_version` antes de aplicar orden nuevo a proyectos antiguos. No reinterpretar silenciosamente una máscara o recorte guardado.[^F05]

Los eventos son append-only para auditoría; las proyecciones se reconstruyen. «Append-only» no elimina obligaciones de borrado y retención: separar datos sensibles de IDs/tipos de eventos y prever redacción o eliminación autorizada de payloads. No hacer un journal público de datos personales.

## API propuesta

Estas rutas son nuevas propuestas; no se afirma que existan en la baseline. Ajustar prefijos al estándar real del HEAD y documentar compatibilidad:

| Método y ruta conceptual | Contrato |
|---|---|
| `GET /api/creator/capabilities?project_id=...` | Capacidades y restricciones del proyecto; no carga modelos. |
| `GET /api/creator/documents/{id}` | Documento owner-scoped, revisión y ETag. |
| `POST /api/creator/documents/{id}/commands` | command_id, expected_revision y operación tipada; 409 en conflicto. |
| `POST /api/creator/plans` | Crear plan sin efectos externos; devolver faltantes y estimación. |
| `POST /api/creator/plans/{id}/execute` | Idempotency key y aprobación ligada al digest; revalidación final. |
| `GET /api/creator/productions/{id}` | Proyección de workflows y MediaRuns canónicos. |
| `POST /api/creator/productions/{id}/cancel` | Detener pasos no iniciados y reconciliar efectos enviados. |
| `POST /api/creator/exports` | Bundle o export audiovisual desde revisiones fijadas. |

Los endpoints de lectura pueden cachear por owner/proyecto/revisión, no sólo URL. Las rutas de estado y archivos validan pertenencia en cada acceso. No aceptar owner desde el body como autoridad: resolverlo de la sesión autenticada. Los errores deben tener clase estable, mensaje humano y target que permita corregir el dato.

## Puerto de motores

Cada adapter ofrece operaciones equivalentes a `describe`, `plan`, `submit`, `status`, `cancel`, `collect` y `reconcile`. La interfaz especifica qué llamadas son puras, cuáles realizan consultas y cuáles crean efectos. Un `healthcheck` no ejecuta una generación para autodeclararse listo; una prueba de generación es un trabajo presupuestado distinto.

`submit` recibe IDs de artefactos resueltos y staging controlado, nunca rutas arbitrarias del modelo. Devuelve un ID de job y estado, o una categoría de error que distingue rechazo antes de cola de aceptación incierta. Si el motor no ofrece reconciliación, esa limitación figura en el manifest; el runtime no inventa idempotencia.

`collect` descarga en un temporal acotado, calcula hash, inspecciona y valida antes de registrar completed. El almacenamiento de output y el recibo de procedencia deben sobrevivir al reinicio. Evitar que el adapter llame directamente a la galería o actualice permisos por su cuenta.

## Ciclo de vida y semántica de errores

Conservar estados históricos de MediaRun. El código leído ya distingue `submit_pending`, `submitted`, `submit_unknown`, `queued`, `running`, `completed`, `failed`, `cancelled` y `unknown`, además de compatibilidad legacy. Las etiquetas de UI se derivan de esa autoridad; no añadir una cadena paralela que cambie el significado de `completed`.[^F02]

Para nuevos adapters, modelar por separado el efecto externo y la disponibilidad de archivos. Un engine puede decir finished mientras la descarga sigue interrumpida. Un cancellation request puede no cancelar un job ya terminado. El resultado debe describirlo: solicitado, aceptado, confirmado, demasiado tarde o desconocido. «Cancelado» no significa que se haya deshecho una publicación.

No prometer exactly-once externo de forma general. Se busca entrega interna idempotente y reconciliación del efecto; la semántica del proveedor limita lo demostrable. Los retries automáticos sólo son seguros para fallos clasificados antes del efecto o con idempotency/reconcile demostrado. El outbox existente es el fundamento de este diseño, no un sistema que deba reemplazarse.[^F02][^F03]

## Tiempo, regiones y operaciones

Almacenar tiempos como enteros con base racional. El formato de referencia adjunto usa valores enteros serializados como strings y una razón de ticks por segundo, para evitar desbordes de precisión de JavaScript. Audio conserva sample_rate y rangos de muestras; vídeo puede conservar PTS de origen. Un retiming map relaciona intervalos fuente y destino y declara cortes o zonas no mapeables.

Las regiones de imagen necesitan sistema de coordenadas: dimensiones de origen, orientación EXIF aplicada, zoom sólo de UI y transform de capa. Guardar únicamente x/y de una captura sin esos datos no permite reproducir una edición. Los cambios se expresan sobre object_id y revisión, no sobre una frase ambigua como «la imagen anterior».

La validación legacy de segmentos presupone intervalos ordenados y no solapados. No eliminar ese contrato globalmente para introducir diálogo multihablante: añadir una versión editorial que valide por pista/hablante, permita solapes explícitos y adapte el subconjunto compatible a SRT/VTT. Mantener las pruebas de la ruta corta anterior. El transcript nuevo no debe destruir información para pasar por un validador pensado para otro alcance.

## Caché y reconstrucción

La clave de resultado incluye entradas exactas, revisión de receta, parámetros efectivos, engine build y componentes. Separar una caché determinista de composición de una reutilización de un resultado generativo. Reusar el mismo resultado no demuestra que otro lanzamiento con la misma seed produciría bytes idénticos.

El grafo de dependencias distingue datos, decisiones y revisión editorial. Un cambio en la traducción invalida su voz y montaje dependiente, pero no necesariamente la transcripción fuente. Cambiar una fuente tipográfica puede invalidar exports quemados, no el vídeo subyacente. La UI muestra el subgrafo afectado y el coste estimado antes de rerun.

## Contratos adjuntos

`contracts/` contiene schemas de referencia y `examples/` ejemplos válidos para CreatorDocument, DeploymentManifest, ProductionPlan y Handoff. No son schemas de una API ya desplegada. Las pruebas incluidas validan estructura y algunas relaciones semánticas del paquete; no sustituyen auth, transacciones, sandbox, validación de medios ni pruebas de integración en Faustus.


[^F02]: Faustus · src/media_runs.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_runs.py). Consultado el 13 de septiembre de 2026. Lectura de líneas 1–250: preflight, consentimiento, outbox y estados de envío; no auditoría integral del módulo.

[^F03]: Faustus · src/media_scheduler.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_scheduler.py). Consultado el 13 de septiembre de 2026. Continuación y reconciliación de renders ya enviados; no vuelve a enviar grafos.

[^F12]: Faustus · services/projects.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/services/projects.py). Consultado el 13 de septiembre de 2026. Lectura 1–170: ProjectStore, context_items, ProjectExecutionContext e identidad estable; evitar segundo registro de proyectos.

[^F13]: Faustus · src/project_identity.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/project_identity.py). Consultado el 13 de septiembre de 2026. Lectura 1–190: marcador .faustus/project.json corroborativo, no segunda autoridad.

[^F01]: Faustus · README.md. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/README.md). Consultado el 13 de septiembre de 2026. Capacidades generales, arquitectura y licencia declarada AGPL-3.0-or-later; resultados de pruebas reportados por el repositorio, no reproducidos aquí.

[^F08]: Faustus · src/model_capabilities.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/model_capabilities.py). Consultado el 13 de septiembre de 2026. Lectura 1–190: vocabulario canónico de familias/modalidades/capacidades/fuentes y mecanismos de controles.

[^F09]: Faustus · src/model_calibration.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/model_calibration.py). Consultado el 13 de septiembre de 2026. Lectura 1–200: identidad Ollama por digest, seis probes y distinción anunciado/probado.

[^F05]: Faustus · src/media_edit_projects.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_edit_projects.py). Consultado el 13 de septiembre de 2026. Compositor no destructivo limitado, capas/máscaras inline, historial y orden de aplicación de export().
