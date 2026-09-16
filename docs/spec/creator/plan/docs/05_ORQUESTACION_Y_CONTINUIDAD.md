# Orquestación y continuidad de trabajo

## Un objetivo ejecutable, no un loop que responde «continúa»

La ampliación debe partir de completion, strategy, workflows, budgets y dispatch existentes. El objetivo persistente representa entregables y criterios, no sólo un texto que se reinyecta cada vez que el modelo calla. El patrón de Goal de Chat On Steroids es útil como referencia de continuidad, pero Faustus puede hacer algo más fuerte porque controla sus propias herramientas y sus recibos.[^F01][^D01][^D02]

Un `GoalContract` propuesto contiene: versión del brief aceptado, scope, entregables, criterios de cierre, presupuesto de llamadas/tiempo/coste, herramientas y proveedores permitidos, política de interrupción y condiciones para pedir ayuda. Los cambios materiales crean otra revisión; no mutan retroactivamente lo que una ejecución tenía autorizado.

Los estados semánticos de evaluación son `continue`, `complete`, `needs_human`, `stalled`, `budget_exhausted` y `cancelled`. No sustituyen el estado de los runs individuales. Una producción puede necesitar intervención aunque un render haya terminado bien. Un modelo puede proponer `complete`; el sistema comprueba los criterios técnicos y la aceptación humana donde proceda.

## Qué cuenta como evidencia de finalización

Para «entrega un vídeo subtitulado», existen criterios verificables: occurrence accesible por el owner, formato declarado, decodificación, duración, streams y pista/quemado de subtítulos correspondiente a la revisión elegida. Para «que tenga un estilo más melancólico», la condición es editorial: revisión humana o una evaluación asistida identificada como tal. No equiparar una opinión estética a una prueba determinista.

Cada criterio registra clase, método, revisión de entrada, expected y observed, evidencia y estado. Un resultado viejo no satisface un criterio nuevo porque el título del archivo coincida. Una ejecución exitosa del proceso no basta si el archivo no existe o está corrupto. La palabra «terminado» en el chat tampoco basta.

El evaluador sólo recibe la evidencia mínima necesaria y enlaces para ampliar. No necesita volcar todos los logs, toda la biblioteca ni el repositorio completo en cada iteración. Las omisiones se registran con motivo para que el modelo no crea haber revisado lo que no vio.

## Continuación idempotente y ausencia de progreso

Una continuación se identifica por `(goal_id, goal_revision, completed_turn_id, decision_revision)`. El mismo evento terminal recibido dos veces no genera dos mensajes ni duplica el siguiente paso. Una corrección del usuario invalida las continuaciones aún no aplicadas de la revisión antigua.

El detector de falta de progreso usa deltas reales: artefactos nuevos, criterios resueltos, errores distintos o decisiones aceptadas. Repetir el mismo tool con iguales inputs y obtener el mismo fallo no cuenta como progreso. Los límites son configurables por tipo de trabajo; la parada debe explicar qué sigue faltando y qué decisión humana desbloquearía el siguiente paso.

Una revisión crítica puede sugerir otra estrategia, pero no aumenta el presupuesto ni amplía el scope por sí sola. El sistema no considera tarea pendiente cualquier mejora imaginable. El criterio de cierre protege al usuario tanto del abandono prematuro como de una producción que nunca termina porque siempre encuentra algo más que mejorar.

## Handoff entre sesiones y modelos

El patrón de handoff leído prepara el resumen antes de publicarlo y conserva un vínculo con el plan original. La propuesta adopta esa separación y añade un contrato estructurado. Comprobar longitud evita truncamientos obvios; no demuestra suficiencia semántica. La validación debe verificar que figuren restricciones, pendientes y refs de evidencia que el receptor pueda resolver.[^D03]

La cápsula incluye objetivo/revisión, resumen de decisiones aceptadas, hechos con referencias, tareas incompletas, ramas y documentos relevantes, últimos errores, estado externo incierto, límites vigentes y perfil de contexto de especialistas. No incluye credenciales, cookies, autorizaciones reutilizables ni un snapshot de todo el disco.

Hay dos capas distintas: **estado autoritativo** se consulta al servicio existente; **resumen** explica por qué se llegó a ese estado. Un workspace, owner o permiso no se deduce del texto de la cápsula. Antes de reanudar, verificar que los documentos siguen en esas revisiones y que el modelo destino tiene las capacidades necesarias. Las diferencias producen un plan de reconciliación.

Publicar la cápsula requiere una frontera durable. Preparar archivo/registro, validar referencias, registrar la transición y anunciar el handoff. Cada ventana de crash tiene recuperación idempotente. La cápsula anterior permanece disponible mientras la nueva no sea válida. No compactar el turno vivo ni eliminar el transcript original sólo para ahorrar tokens.

## Especialistas con vida de contexto explícita

El equipo creativo propuesto contiene roles de guion, dirección visual, edición temporal, localización, sonido/música y QA. Son perfiles sobre el sistema de agentes actual, no seis servicios obligatorios. Para trabajos pequeños puede bastar un único agente y un renderer. La estrategia debe justificar delegación y coste.[^F01]

Cada especialista declara si su memoria es por invocación, por objetivo o por proyecto. El estado compartido entre ejecuciones debe tener namespace y revisión; los inputs de un proyecto no se mezclan con otro. La distinción entre persistencia por hilo y por invocación, documentada por LangGraph, sirve como referencia de fallos de diseño; no es motivo para introducir otro framework.[^W07]

Los especialistas producen propuestas tipadas sobre escenas/documentos. El integrador aplica una propuesta aceptada mediante CAS. Para código, usar worktrees o copias aisladas existentes cuando sea necesario; para audio y vídeo, ramas de documento y occurrences son más apropiadas que copiar GB de medios por cada agente.

## Steering y concurrencia editorial

Una corrección del usuario es un evento duradero con número de secuencia. El runtime distingue lo aún planificado de lo ya enviado a un proveedor. «No uses esa voz» puede retirar los siguientes renders y bloquear una nueva autorización; no puede borrar un efecto remoto ya producido mediante una cancelación puramente local.

Los resultados tardíos se almacenan como evidencia del intento, si la política lo permite, pero no se aplican al documento actual cuando su revisión cambió. La UI ofrece comparar o descartar. La propiedad de escena/documento no sustituye CAS: un lock expirado no da permiso para sobrescribir trabajo humano.

## Interrupciones y reintentos

No asumir que reanudar un nodo continúa exactamente en la línea donde se detuvo. LangGraph documenta que al reanudar un interrupt puede volver a ejecutarse el nodo desde el comienzo. El aprendizaje general es que cualquier efecto anterior a la pausa debe quedar registrado o ser idempotente; Faustus debe aplicar esta disciplina en sus propios workflows.[^W06]

Las pausas human-in-the-loop se hacen antes de efectos sensibles cuando sea posible. Reintentar una etapa de traducción puede ser razonable; volver a enviar un correo o un render facturable cuya aceptación es desconocida no lo es sin reconciliación. El contrato de medios ya contiene esa distinción y debe mantenerse.[^F02]

## Contexto eficiente para modelos locales

Usar manifiestos de producción, notas de decisión, excerpts y referencias seleccionadas en lugar de conversaciones completas indiscriminadas. Cada task recibe pocos schemas y ejemplos pertinentes. Los fallos de parser o salida estructurada deben devolver feedback específico y un retry acotado, no un prompt cada vez más largo.

La compacción y la memoria persistente no amplían la ventana nativa del modelo. Permiten elegir mejor qué recuperar. Cambiar a un modelo pequeño puede requerir dividir el objetivo en tareas más concretas o solicitar revisión en otro motor; el sistema lo debe explicar sin fingir igualdad de competencia.


[^F01]: Faustus · README.md. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/README.md). Consultado el 13 de septiembre de 2026. Capacidades generales, arquitectura y licencia declarada AGPL-3.0-or-later; resultados de pruebas reportados por el repositorio, no reproducidos aquí.

[^D01]: totec448-spec/chat-on-steroids · README.md. [Fuente](https://github.com/totec448-spec/chat-on-steroids/blob/99ec069cfbe7aae01e2ba9ab234c237ffbf609ff/README.md). Consultado el 13 de septiembre de 2026. MCP local, Chrome, workers persistentes, steering, Goal/Loop y Compact & Resume.

[^D02]: totec448-spec/chat-on-steroids · src/main/goal.ts. [Fuente](https://github.com/totec448-spec/chat-on-steroids/blob/99ec069cfbe7aae01e2ba9ab234c237ffbf609ff/src/main/goal.ts). Consultado el 13 de septiembre de 2026. Lectura 1–150: decisión validada, borrador por generación y autodeclaración final como señal autoritativa; no inspecciona tool results.

[^D03]: totec448-spec/chat-on-steroids · src/main/session/handoff.ts. [Fuente](https://github.com/totec448-spec/chat-on-steroids/blob/99ec069cfbe7aae01e2ba9ab234c237ffbf609ff/src/main/session/handoff.ts). Consultado el 13 de septiembre de 2026. Lectura 1–170: prepare/publish, identidad, truncamiento, referencias al plan y bootstrap consistente.

[^W07]: LangGraph · subgraphs. [Fuente](https://docs.langchain.com/oss/python/langgraph/use-subgraphs). Consultado el 13 de septiembre de 2026. Vida de subagentes por invocación/hilo y aislamiento de estado.

[^W06]: LangGraph · interrupts. [Fuente](https://docs.langchain.com/oss/python/langgraph/interrupts). Consultado el 13 de septiembre de 2026. Al reanudar puede reiniciarse el nodo; los efectos necesitan idempotencia. Referencia de diseño, no propuesta de sustituir workflows.

[^F02]: Faustus · src/media_runs.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_runs.py). Consultado el 13 de septiembre de 2026. Lectura de líneas 1–250: preflight, consentimiento, outbox y estados de envío; no auditoría integral del módulo.
