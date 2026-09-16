# Model intelligence y uso del hardware

## Por qué esta mejora es central

Tener muchos nombres de modelos no equivale a soportarlos bien. La ficha debería contestar «¿puedo hacer esta operación con esta variante, este motor, esta configuración y esta máquina?». También debe explicar qué se perdería al cambiar de modelo y qué falta por verificar. La meta no es un catálogo bonito al lado del chat: es que la planificación, los formularios y el runtime usen el mismo conocimiento operativo.

Faustus ya cuenta con vocabularios de modalidades, familias, controles, fuentes y estados de evidencia, además de pruebas de calibración y recibos de configuración. No hay que sustituirlo por un nuevo `models.json` con cuatro booleanos. La ampliación debe conservar esa riqueza y separar mejor el modelo abstracto de su despliegue concreto.[^F08][^F09][^F01]

## Tres niveles de identidad

**Modelo y componentes.** Identidad de pesos, revisión o digest, arquitectura, tokenizer/encoder/VAE, precisión de los artefactos descargados y compatibilidad de adapters. Dos tags que apuntan al mismo digest pueden compartir datos inmutables de los pesos. Un modelo de música compuesto por LM y DiT debe registrar ambos, no sólo el nombre del paquete.

**Deployment.** Endpoint autorizado, motor y build, revisión realmente cargada, cuantización, chat template, opciones aplicadas, hardware o tipo de servicio y ruta de facturación. Dos servidores del mismo modelo pueden diferir en tool calling, salida estructurada o memoria disponible. Un alias del mismo endpoint se normaliza; dos endpoints independientes no se colapsan por compartir nombre.

**Evidencia de tarea.** Resultado de un probe o trabajo sobre ese deployment, con inputs o digest de fixture, límites, fecha y entorno. Los benchmarks y verificaciones envejecen cuando cambian las condiciones. Un registro desconocido permanece desconocido, no se convierte en no soportado por falta de prueba.

La clave por digest de Ollama leída en la baseline hace conveniente esta separación. Migrar datos antiguos conservando lo que sí representan. Los metadatos de pesos pueden seguir compartidos; los resultados sin datos de deployment se muestran como históricos de scope insuficiente hasta volver a verificar.[^F09]

## Ficha de modelo propuesta

| Grupo | Datos útiles | Efecto en producto |
|---|---|---|
| Identidad | Familia, revisión, alias, archivos y componentes. | Evitar descargar lo mismo y saber qué versión generó un resultado. |
| Tareas | Lectura de imagen, edición, inpaint, T2V, I2V, ASR, align, TTS, música, etc. | Ofrecer sólo operaciones pertinentes. |
| Inputs | Tipos, formatos, dimensiones, idiomas, referencias, masks, rangos de duración. | Validación anticipada de cada trabajo. |
| Outputs | Tipos, dimensiones, audio, fps, duración, alpha, stems y datos auxiliares. | Prometer entregables que se puedan comprobar. |
| Controles | Rango, default, incompatibilidades, requisitos y mecanismo de aplicación. | Formularios generados y requests correctos. |
| Recursos | Memoria por componente/etapa, contexto, scratch, tiempo medido o estimado. | Preflight y admission, no sólo advertencias cosméticas. |
| Privacidad/coste | Local o remoto, cuenta/ruta, envío de datos, precio por unidad y fecha. | Presupuesto explícito y ausencia de fallback oculto. |
| Evidencia | Fuente por campo, condiciones, fecha, prueba, conflicto y cobertura. | Confianza calibrada y posibilidad de auditar. |
| Derechos | Código, pesos, adapters, fuentes, voz, dataset y términos pendientes. | Decisiones de uso y exportación informadas. |

Ninguno de estos grupos requiere llenar todos sus campos para mostrar un modelo. Requiere no inventarlos. La interfaz puede ofrecer una vista simple y otra exhaustiva; ambos niveles leen el mismo manifest.

## Parámetros como contrato, no como formulario universal

Cada control define clave canónica, etiqueta, tipo, unidades, rango/enum, default, mecanismo de transporte y condiciones. `negative_prompt`, CFG, seed, context length, reasoning effort, enable_thinking, duration o fps no son intercambiables entre engines. Una propiedad puede existir en un modelo y no estar expuesta por un proveedor concreto.

Proponer tres estados de UI: utilizable, no utilizable con motivo, y desconocido. Para un control desconocido, permitir un modo experto sólo si la política lo admite y el usuario ve que no se confirma su aplicación. No enviarlo silenciosamente ni asegurar que se aplicó porque el request lo incluía.

Las restricciones se validan juntas. Ejemplos de reglas propuestas: el tipo de edición exige imagen base; un control regional exige máscara y dimensiones concordantes; un TTS exige idioma admitido; un modo de música puede no admitir CFG; un pipeline de vídeo requiere un conjunto exacto de componentes. La regla se evalúa antes de reservar la GPU y otra vez al ejecutar si algo pudo cambiar.

El recibo de ejecución debe registrar **solicitado → normalizado → enviado → confirmado por motor**. Si el motor no confirma un parámetro, conservar ese límite de evidencia. Las opciones aplicadas pueden producir el mismo resultado por azar; el recibo prueba el transporte o la configuración, no causalidad experimental.

## Ejemplos concretos que justifican granularidad

ACE-Step documenta variantes base, sft y turbo con distintas operaciones. La tabla consultada no habilita `extract`, `lego` y `complete` del mismo modo en todas ellas. Por tanto, una ficha genérica «ACE-Step: edición de música» es insuficiente: cada operación debe enlazar al modelo y adapter realmente instalado. Los datos de recursos de una variante tampoco se trasladan a XL sin comprobar sus componentes.[^A01][^W09]

Chatterbox diferencia modelos multilingües de variantes Turbo y Nano centradas en inglés. Elegir una voz para español debe considerar esa distinción antes de sintetizar y gastar recursos. El nombre de familia no autoriza a marcar todos los idiomas disponibles en todas sus variantes.[^A05]

Wan presenta variantes para texto a vídeo, imagen a vídeo, texto/imagen a vídeo, voz a vídeo y animación. LTX distingue componentes y pipelines locales, mientras sus servicios API publican operaciones que deben verificarse separadamente. Son ejemplos de que task, checkpoint y deployment deben formar parte de la identidad operacional.[^A09][^A10][^W05]

Invoke muestra restricciones específicas de familias y un catálogo visual amplio. El aprendizaje útil no es perseguir cada nombre, sino que el programa explique los controles admitidos y lo que falta para la tarea elegida.[^A06][^W04]

## Fuentes de metadatos y resolución de conflictos

Conservar lectores de proveedor, metadatos de archivos, registries y probes existentes. Models.dev puede enriquecer fichas con un snapshot versionado, pero no debe convertirse en autoridad de capacidad de una cuenta o una instalación. Un precio cero o campo vacío en un registro no se interpreta automáticamente como gratuidad. El coste operativo exige unidad, moneda, condiciones y fecha.[^F08][^W01]

No basta con una precedencia global fija. Una medición negativa de un motor viejo no refuta una declaración de un motor nuevo; una declaración amplia del proveedor no demuestra que el adapter local implemente la opción. Resolver por scope y revisión antes de decidir precedencia. Mantener discrepancias, qué dato se usa y por qué.

Un override administrativo puede fijar un valor efectivo por política, pero no transformar una hipótesis en medición. Registrar autor, motivo, caducidad y alcance. La UI distingue «forzado por configuración» de «verificado con esta prueba».

La actualización de catálogo debe mostrar diff, no descargar o sustituir pesos. Los trabajos aceptados fijan una revisión del manifest; actualizar documentación no debe cambiar una producción en curso. Al actualizar un adapter o modelo, volver a probar el subconjunto de capacidades afectado.

## Calibración por niveles

**Nivel 0: localización y conexión.** Verificar ejecutable, versión, importabilidad o endpoint con coste mínimo. No cargar un modelo ni descargar pesos durante un simple healthcheck. Un `-version` debe comprobar returncode y timeout.

**Nivel 1: aceptación del protocolo.** Payload soportado, salida parseable, streaming, tool deltas y errores coherentes. Una imagen transparente de un píxel puede probar que el endpoint acepta un payload de imagen; no prueba comprensión visual. Los probes leídos de la baseline ya separan aspectos técnicos, pero sus resultados necesitan etiquetas que impidan sobreinterpretarlos.[^F07][^F09]

**Nivel 2: corrección funcional.** Fixtures deterministas con respuestas verificables: tool exacta, schema, conteo o transformación simple, dimensiones reales del output, alineación dentro de tolerancias especificadas. Usar ejemplos ES/EN cuando el producto promete ambos idiomas.

**Nivel 3: calidad de tarea y robustez.** Corpus pequeño pero representativo, resultado ciego cuando tenga sentido, criterios humanos para estética y métricas explícitas para fidelidad. No confundir un juez LLM con una prueba técnica. Un benchmark es una muestra, no una certificación universal.

**Nivel 4: rendimiento y límites.** Ejecutar sólo bajo presupuesto y reserva de recursos. Registrar cola, carga, prefill/inferencia/encode y transferencia; repetir lo suficiente para estimar variación. Separar medidas de warm y cold start. No declarar un perfil «mejor» si sólo fue más rápido a costa de calidad o fiabilidad.

## Router orientado a tarea

Mantener la intención del usuario separada del último modelo resuelto. La documentación leída describe un alcance local y una ruta que puede escribir el modelo concreto tras resolver auto; WP00 debe comprobar el estado del HEAD antes de modificarlo. El nuevo comportamiento propuesto añade `routing_intent` estable y un recibo por resolución, sin romper elecciones manuales.[^F10]

Resolver primero requisitos duros: permisos, privacidad, tarea, inputs y limitaciones. Después presupuesto y cabida; finalmente preferencias de calidad, latencia, historial y coste. No usar una puntuación agregada para compensar la falta de una capacidad obligatoria. No escalar de local a remoto ni de suscripción a API porque un ranking dé mejor puntuación.

La explicación de ruta debe incluir el ganador, candidatos descartados y motivo, evidencia usada, riesgos desconocidos y coste estimado. Un usuario puede fijar un modelo aunque no sea el más rápido; la aplicación debe respetarlo o bloquear una incompatibilidad, no sustituirlo silenciosamente.

Al cambiar de modelo a mitad de una tarea, mostrar el impacto: pérdida de visión, tools, idiomas, tamaño de contexto o controles; qué cápsulas deben reconstruirse y qué pasos necesitan otro adapter. Un sustituto basado en resumen textual es una degradación aprobada, no equivalente oculto a procesar el original.

## Hardware de Luis y límites reales de la propuesta

El repositorio reporta una RTX 4070 Ti de 12 GB y dos RTX 5060 Ti de 16 GB, además de 128 GB de RAM e incidentes de agotamiento de memoria comprometida con cargas simultáneas. Estos datos son contexto operativo reportado, no inventario medido aquí. El primer paso de una instalación es descubrir el hardware y sus servicios de nuevo.[^F14]

Tres GPUs no constituyen automáticamente una GPU de 44 GB. El scheduler debe comprobar VRAM por dispositivo y el soporte de partición del motor. Repartir distintos planos o tareas independientes entre workers es distinto de repartir los pesos y activaciones de una sola inferencia. También deben identificarse recursos compartidos entre endpoints y transferencias lentas.

El modo por defecto debe seguir siendo conservador: no arrancar otro modelo grande, build pesado y batería de tests sólo porque el total de RAM parezca alto. El presupuesto considera **commit de memoria**, no sólo RAM libre, y reserva scratch antes de descargar o renderizar. La admisión de recursos no es una promesa de que el sistema operativo nunca pueda agotar memoria; es una barrera medible con márgenes y errores explícitos.

## Concurrencia sin capacidad ficticia

La propuesta usa un grafo host/dispositivo/proceso/endpoint. Un endpoint que expone dos paths es el mismo servicio; dos servicios diferentes pueden competir por la misma GPU. Por ello la normalización de URL es necesaria, pero no suficiente para decidir independencia física. La base de pools y leases existente debe ampliarse en lugar de duplicarse.[^F11]

Para habilitar paralelismo, WP31 exige evidencia de recursos disjuntos, pruebas de carrera y una vuelta simple al modo serial. Priorizar chat y previews no implica matar un render activo con efectos externos. La preempción sólo es real si el backend la admite; en caso contrario, ordenar la cola o esperar un punto seguro.

La afinidad de modelos puede reducir recargas, pero se calcula con telemetría fresca. Una preferencia por un worker caliente nunca evita comprobar que siga cargado ni sustituye los límites de owner. Los workers remotos necesitan los mismos manifests, scopes y pruebas; una IP LAN no convierte el procesamiento en local privado de forma automática.


[^F08]: Faustus · src/model_capabilities.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/model_capabilities.py). Consultado el 13 de septiembre de 2026. Lectura 1–190: vocabulario canónico de familias/modalidades/capacidades/fuentes y mecanismos de controles.

[^F09]: Faustus · src/model_calibration.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/model_calibration.py). Consultado el 13 de septiembre de 2026. Lectura 1–200: identidad Ollama por digest, seis probes y distinción anunciado/probado.

[^F01]: Faustus · README.md. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/README.md). Consultado el 13 de septiembre de 2026. Capacidades generales, arquitectura y licencia declarada AGPL-3.0-or-later; resultados de pruebas reportados por el repositorio, no reproducidos aquí.

[^A01]: ace-step/ACE-Step-1.5 · README.md. [Fuente](https://github.com/ace-step/ACE-Step-1.5/blob/main/README.md). Consultado el 13 de septiembre de 2026. Catálogo de operaciones musicales, variantes, API y recursos declarados; no benchmark propio.

[^W09]: ACE-Step · documentación de variantes. [Fuente](https://github.com/ace-step/ACE-Step-1.5). Consultado el 13 de septiembre de 2026. Tabla Model Zoo consultada: extract/lego/complete no equivalentes entre base, sft y turbo.

[^A05]: resemble-ai/chatterbox · README.md. [Fuente](https://github.com/resemble-ai/chatterbox/blob/master/README.md). Consultado el 13 de septiembre de 2026. Variantes multilingual/Turbo/Nano, idiomas y controles diferentes; no verificación de calidad local.

[^A09]: Lightricks/LTX-2 · README.md. [Fuente](https://github.com/Lightricks/LTX-2/blob/main/README.md). Consultado el 13 de septiembre de 2026. Lectura 1–140: variantes y componentes LTX, distinción pipelines y requisitos; algunas secciones no leídas por truncamiento.

[^A10]: Wan-Video/Wan2.2 · README.md. [Fuente](https://github.com/Wan-Video/Wan2.2/blob/main/README.md). Consultado el 13 de septiembre de 2026. Lectura 1–145: T2V/I2V/TI2V/S2V/Animate y alternativas de ejecución; no se afirma que sea la última familia Wan.

[^W05]: LTX · documentación API. [Fuente](https://docs.ltx.video/). Consultado el 13 de septiembre de 2026. Operaciones API de generación/retake/extend/reframe; no prueban que cada pipeline local ofrezca lo mismo.

[^A06]: invoke-ai/InvokeAI · README.md. [Fuente](https://github.com/invoke-ai/InvokeAI/blob/main/README.md). Consultado el 13 de septiembre de 2026. Canvas, in/outpainting, workflows, galería y gestor de modelos con diferencias de compatibilidad.

[^W04]: Invoke · releases. [Fuente](https://invoke.ai/releases/). Consultado el 13 de septiembre de 2026. Restricciones por familia de modelos; no equiparar una lista de nombres con idéntico soporte.

[^W01]: Models.dev · catálogo y uso. [Fuente](https://models.dev/). Consultado el 13 de septiembre de 2026. Fuente de metadatos declarados de modelos/proveedores; no verdad operativa ni precios verificados para una cuenta.

[^F07]: Faustus · src/media_capabilities.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_capabilities.py). Consultado el 13 de septiembre de 2026. Probes baratos; diferencia paquete instalado / modelo preparado; Piper no conectado al proveedor TTS en este módulo.

[^F10]: Faustus · docs/api/model_router.md. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/docs/api/model_router.md). Consultado el 13 de septiembre de 2026. Lectura 1–240: router local medido, alcance de integración y mutación de selección auto a modelo concreto.

[^F14]: Faustus · PENDIENTES.md. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/PENDIENTES.md). Consultado el 13 de septiembre de 2026. Incidentes y configuración de hardware reportados; no inventario medido en este trabajo.

[^F11]: Faustus · src/resource_admission.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/resource_admission.py). Consultado el 13 de septiembre de 2026. Lectura 1–165: pools, leases y normalización; docstring declara integración pendiente: reauditar callers porque puede estar desactualizado.
