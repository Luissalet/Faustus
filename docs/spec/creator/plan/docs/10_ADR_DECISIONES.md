# Decisiones arquitectónicas propuestas

Estas ADR son propuestas para aceptación en Faustus, no decisiones que se afirme que el proyecto ya haya adoptado. Cada una puede revisarse con evidencia nueva; cambiarla exige explicar impacto y migración.

## ADR-01 — Creator es una vista de proyecto

Conservar `ProjectStore`, IDs y Chat/Agent. Creator añade objetos y herramientas, no otra cuenta, biblioteca o registro de proyectos. Se descarta una aplicación paralela porque duplicaría scopes, contexto y artefactos. Reabrir sólo si una limitación demostrada impide estos invariantes.[^F12][^F13]

## ADR-02 — Un único propietario del lifecycle

MediaRun y workflows mantienen sus autoridades; Creator muestra proyecciones. Se descarta un segundo `creator_jobs` con un status independiente para el mismo efecto. Puede haber tablas auxiliares por tarea, pero deben enlazar al run y no competir por la transición terminal.[^F02][^F03]

## ADR-03 — Modelo no es deployment

Separar metadatos de pesos de configuración ejecutable y evidencia. Se conserva deduplicación por digest para pesos, no para resultados dependientes de entorno. La migración no inventa información ausente ni borra resultados históricos.[^F08][^F09]

## ADR-04 — Schema como autoridad de controles

UI, API y tools comparten constraints tipadas. No mantener catálogos separados de parámetros. Las diferencias entre motores son visibles; el mínimo común denominador no debe hacer desaparecer capacidades útiles ni crear falsas equivalencias.

## ADR-05 — Operaciones declarativas y revisiones

Toda edición se aplica a un object_id y revisión mediante command_id idempotente. Descartar shell arbitrario y manipulación directa de archivos como ruta normal de edición audiovisual. Scripts avanzados siguen la infraestructura de sandbox y permisos existente.

## ADR-06 — Motores pesados aislados

ASR, TTS, música, vídeo y 3D viven en procesos o entornos opcionales separados con contratos comunes. No hacer que instalar un modelo cambie las dependencias del servidor web. Aislamiento de dependencias no se presenta como sandbox de seguridad si no lo es.[^D04]

## ADR-07 — Reanudación no implica exactly-once externo

Persistir intención antes de submit, reconciliar aceptación incierta y aplicar fencing por intento. No reintentar automáticamente mutaciones desconocidas. Una interfaz debe admitir uncertainty como dato, no forzar éxito/fallo sin evidencia.[^F02]

## ADR-08 — Recursos por dispositivo y modo serial inicial

No sumar VRAM como memoria contigua ni tratar endpoints como GPUs independientes. Mantener defaults actuales y habilitar concurrencia tras medir y probar recursos disjuntos. Las mejoras de rendimiento no pueden borrar una protección que evita OOM.[^F11][^F14]

## ADR-09 — Referencias y canon se aceptan explícitamente

Una imagen puede orientar estilo o ser input técnico, pero ambos mecanismos se distinguen. Las referencias canónicas y decisiones humanas no se sustituyen por nuevas propuestas. No prometer consistencia perfecta de personaje por la mera existencia de una Bible.[^F01]

## ADR-10 — Calidad técnica y calidad artística separadas

Existencia, decodificación, duración y schemas son verificables de forma programática. Estética y preferencia necesitan evaluación identificada y no se confunden con esos checks. Un LLM judge no da permiso ni convierte una tarea incompleta en completada.

## ADR-11 — Interoperabilidad con pérdidas explícitas

Usar OTIO para el subconjunto editorial admitido y bundles propios para inventario/medios. No prometer roundtrip universal de efectos, keyframes y plugins. Importar datos no ejecuta hooks de terceros.[^A11]

## ADR-12 — Patrones antes que trasplantes

Adoptar continuidad y lifecycle de CoS, canvas de Invoke y localización de VideoLingo sin importar toda su plataforma. OpenCut queda como referencia/spike mientras su reescritura y classic presentan riesgos distintos. Copiar código requiere licencia y revisión concreta.[^D01][^A06][^A03][^A07][^A08]

## ADR-13 — Goal tiene techo además de suelo

La continuidad no inventa tareas fuera del objetivo. El contrato protege frente al abandono prematuro y el trabajo infinito. El cierre usa evidencia; las opiniones de otro agente no sustituyen los criterios ni aumentan su presupuesto.

## ADR-14 — Contexto y memoria no son permisos

Handoffs, presets, recuerdos y material importado son contexto, no autorización. Las decisiones de seguridad se resuelven fuera del modelo en el momento del efecto. Una cápsula conserva referencias al estado, no una copia textual de poderes reutilizables.


[^F12]: Faustus · services/projects.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/services/projects.py). Consultado el 13 de septiembre de 2026. Lectura 1–170: ProjectStore, context_items, ProjectExecutionContext e identidad estable; evitar segundo registro de proyectos.

[^F13]: Faustus · src/project_identity.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/project_identity.py). Consultado el 13 de septiembre de 2026. Lectura 1–190: marcador .faustus/project.json corroborativo, no segunda autoridad.

[^F02]: Faustus · src/media_runs.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_runs.py). Consultado el 13 de septiembre de 2026. Lectura de líneas 1–250: preflight, consentimiento, outbox y estados de envío; no auditoría integral del módulo.

[^F03]: Faustus · src/media_scheduler.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_scheduler.py). Consultado el 13 de septiembre de 2026. Continuación y reconciliación de renders ya enviados; no vuelve a enviar grafos.

[^F08]: Faustus · src/model_capabilities.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/model_capabilities.py). Consultado el 13 de septiembre de 2026. Lectura 1–190: vocabulario canónico de familias/modalidades/capacidades/fuentes y mecanismos de controles.

[^F09]: Faustus · src/model_calibration.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/model_calibration.py). Consultado el 13 de septiembre de 2026. Lectura 1–200: identidad Ollama por digest, seis probes y distinción anunciado/probado.

[^D04]: totec448-spec/chat-on-steroids · docs/plugins.md. [Fuente](https://github.com/totec448-spec/chat-on-steroids/blob/main/docs/plugins.md). Consultado el 13 de septiembre de 2026. Instalación aislada, MCPB, schemas, readiness, revocación, rollback y límites del sandbox.

[^F11]: Faustus · src/resource_admission.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/resource_admission.py). Consultado el 13 de septiembre de 2026. Lectura 1–165: pools, leases y normalización; docstring declara integración pendiente: reauditar callers porque puede estar desactualizado.

[^F14]: Faustus · PENDIENTES.md. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/PENDIENTES.md). Consultado el 13 de septiembre de 2026. Incidentes y configuración de hardware reportados; no inventario medido en este trabajo.

[^F01]: Faustus · README.md. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/README.md). Consultado el 13 de septiembre de 2026. Capacidades generales, arquitectura y licencia declarada AGPL-3.0-or-later; resultados de pruebas reportados por el repositorio, no reproducidos aquí.

[^A11]: AcademySoftwareFoundation/OpenTimelineIO · README.md. [Fuente](https://github.com/AcademySoftwareFoundation/OpenTimelineIO/blob/main/README.md). Consultado el 13 de septiembre de 2026. Intercambio editorial y referencias a medios; NO renderer ni contenedor general de medios. Plugins de formato separados.

[^D01]: totec448-spec/chat-on-steroids · README.md. [Fuente](https://github.com/totec448-spec/chat-on-steroids/blob/99ec069cfbe7aae01e2ba9ab234c237ffbf609ff/README.md). Consultado el 13 de septiembre de 2026. MCP local, Chrome, workers persistentes, steering, Goal/Loop y Compact & Resume.

[^A06]: invoke-ai/InvokeAI · README.md. [Fuente](https://github.com/invoke-ai/InvokeAI/blob/main/README.md). Consultado el 13 de septiembre de 2026. Canvas, in/outpainting, workflows, galería y gestor de modelos con diferencias de compatibilidad.

[^A03]: Huanshere/VideoLingo · README.md. [Fuente](https://github.com/Huanshere/VideoLingo/blob/main/README.md). Consultado el 13 de septiembre de 2026. WhisperX, glosario, traducción/reflexión/adaptación, reanudación; limitaciones multihablante y cambio de idioma declaradas.

[^A07]: OpenCut-app/OpenCut · README.md. [Fuente](https://github.com/OpenCut-app/OpenCut/blob/main/README.md). Consultado el 13 de septiembre de 2026. Reescritura en curso; Editor API, MCP, plugins y headless figuran como objetivos, no capacidades verificadas.

[^A08]: OpenCut-app/opencut-classic · README.md. [Fuente](https://github.com/OpenCut-app/opencut-classic/blob/main/README.md). Consultado el 13 de septiembre de 2026. Versión anterior archivada; estructura Next.js/Rust y timeline como referencia, no nueva dependencia central.
