# Seguridad, derechos y evolución del producto

## Las fronteras que no deben romperse

Creator usa los mismos owners, proyectos, roots, proveedores, budgets y approvals que el resto de Faustus. Ni un perfil creativo ni un plugin obtiene automáticamente permiso para shell, red, escritorio, micrófono o publicación. Un script o nodo puede ser creativo y seguir siendo código con acceso a datos y recursos.[^F01][^D04]

No hacer del parámetro `local_only` una preferencia cosmética. Debe considerar el camino entero: modelo principal, embeddings, TTS, custom nodes, plugins, fuentes externas, telemetría y URLs embebidas en medios. Un ComfyUI local con un nodo que llama a una API es una ruta de salida y necesita política y autorización. Si no puede imponerse esa restricción en un motor, ese motor no se declara apto para el perfil estricto.

Los manifests y READMEs de terceros son datos no confiables, no instrucciones para el asistente ni para el instalador. Una recipe describe capacidades y requisitos; no autoriza a instalar ni ejecutar automáticamente lo que menciona. La importación y la aprobación son transiciones distintas.

## Ejecución y codecs

Ejecutar workers con mínimos permisos, directorios temporales propios y límites de CPU, memoria, disco y tiempo. No pasar claves de otros proveedores al entorno. Asegurar límites de stdout/stderr, protocolos y tamaño de archivos. La ausencia del sandbox requerido debe producir una negativa explícita, nunca ejecución host silenciosa.

Los parsers de imagen, vídeo, audio, subtítulos, fuentes y archivos comprimidos son superficie de ataque. Verificar tipo real, limitar dimensiones y expansión, bloquear path traversal/symlinks no permitidos y tratar SVG/HTML como contenido activo hasta sanitizar o rasterizar. No arrancar hooks OTIO o scripts incluidos en un paquete editorial por el mero hecho de importarlo.

El scheduler verifica ownership de procesos antes de cancelar. Un job_id externo no es por sí solo una autorización para matarlo; debe estar vinculado al run, owner y engine registrados. Un late result puede ser evidencia, pero no permiso de aplicar cambios sobre una revisión nueva.

## Consentimiento e identidad

Faustus ya tiene un gate de consentimiento para recetas que clonan identidad. La mejora debe ser una ampliación de esa autoridad: sujeto, owner, alcance, finalidad, referencia exacta, caducidad y revocación. No crear un registro alternativo que contradiga el existente.[^F02][^F04]

No exigir consentimiento de una persona inexistente para una voz sintética genérica ni asumir que una etiqueta de personaje corresponde a una persona real. Sí distinguir claramente la clonación de identidad real y conservar la evidencia de permiso pertinente. La aplicación no «verifica identidad legal» por guardar una casilla; registra la decisión y el material aportado con el alcance que puede demostrar.

La revocación se comprueba antes de un nuevo efecto y en trabajos aún pendientes. No afirmar que revocar borra automáticamente todo resultado exportado o publicado con anterioridad. Mostrar efectos producidos y opciones reales de retirada cuando existan.

## Licencias: estado de revisión, no asesoramiento jurídico concluyente

La política de reutilización diferencia código, pesos, adapters/LoRA, datasets, grabaciones, samples y tipografías. La licencia de una herramienta no autoriza automáticamente todo el material procesado o generado. La evaluación final depende de la revisión y del uso/distribución concretos.

| Componente | Evidencia de esta revisión | Acción antes de copiar o distribuir |
|---|---|---|
| Faustus | README declara AGPL-3.0-or-later. | Respetar la licencia del proyecto y comprobar archivos/licencias del checkout a distribuir. |
| Chat On Steroids | Archivo MIT leído. | Conservar notices y revisar dependencias/binaries incluidos. |
| SteroidChat | Apache-2.0 declarada en README; LICENSE raíz no recuperada. | Aclarar licencia de archivos/revisión antes de copiar código. |
| Killer prompts | Sin licencia explícita localizada. | Presets originales; no importar textos sin autorización aclarada. |
| ACE-Step | Archivo MIT del código leído. | Verificar además pesos, componentes, muestras y dependencias. |
| VideoLingo / OpenCut | Declaraciones de licencia en README. | Leer archivos legales de revisión exacta y dependencia antes de adoptar código. |
| Invoke / WhisperX / Chatterbox / Wan / LTX / OTIO | Lecturas técnicas; no revisión completa de todos los términos. | Resolver código, pesos, plugins y assets antes de redistribuir. |

Fuentes de las declaraciones y archivos verificados de esta tabla.[^F01][^D05][^D06][^D08][^A01][^A02][^A03][^A07][^A08]

Los estados de derechos propuestos son `reviewed_allowed`, `restricted`, `unknown` y `not_applicable`, siempre con alcance. No convertir unknown en comercial permitido. La UI debe mostrar la decisión y asuntos pendientes sin fingir una asesoría jurídica automática. Para uso personal y distribución se pueden configurar políticas distintas, sin borrar información de procedencia.

## Datos portables y borrado

Los bundles contienen sólo lo seleccionado y autorizado, con inventario de bytes y metadatos depurados. Separar un bundle completo de uno ligero con referencias externas. No llamar portable a un ZIP que falla al abrir porque dependía de una ruta privada no incluida.

El borrado distingue enlace, occurrence y bytes. Si otros objetos autorizados siguen referenciando los bytes, no se eliminan por retirar un enlace. Si se borra una occurrence, los tombstones deben seguir impidiendo que una migración la resucite. La UI ofrece un preview de impacto.[^F01]

El journal de auditoría no debe ser un segundo almacén indestructible de datos sensibles. Guardar identificadores y hashes donde baste, con retención y redacción de payloads. Los bundles para soporte o colaboración no contienen credenciales ni rutas privadas por defecto.

## Feature flags, despliegue y rollback

Cada área grande tiene una flag: Creator shell, editor temporal, nuevo esquema de modelos, music adapter, concurrency, plugins y training. Una flag desactivada no corrompe los documentos ni elimina outputs; simplemente devuelve el camino anterior o una vista de sólo lectura cuando el cliente no entiende el esquema.

Las instalaciones de motores se fijan y aíslan. Actualizar consiste en preparar, verificar, probar de forma pertinente y promover, con rollback de código/configuración y preservación de datos. Un modelo nuevo no se descarga durante el arranque normal ni porque el usuario abrió la ficha.

Antes de una migración material, backup y prueba con datos legacy representativos. Un cambio en operación_semantics o fingerprint crea versión nueva; se conservan los receipts antiguos como descripción del trabajo original. No reescribir procedencia histórica para que parezca que se usó el nuevo sistema.


[^F01]: Faustus · README.md. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/README.md). Consultado el 13 de septiembre de 2026. Capacidades generales, arquitectura y licencia declarada AGPL-3.0-or-later; resultados de pruebas reportados por el repositorio, no reproducidos aquí.

[^D04]: totec448-spec/chat-on-steroids · docs/plugins.md. [Fuente](https://github.com/totec448-spec/chat-on-steroids/blob/main/docs/plugins.md). Consultado el 13 de septiembre de 2026. Instalación aislada, MCPB, schemas, readiness, revocación, rollback y límites del sandbox.

[^F02]: Faustus · src/media_runs.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_runs.py). Consultado el 13 de septiembre de 2026. Lectura de líneas 1–250: preflight, consentimiento, outbox y estados de envío; no auditoría integral del módulo.

[^F04]: Faustus · src/media_workflows.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_workflows.py). Consultado el 13 de septiembre de 2026. Lectura 1–190: inputs tipados, requisitos y fingerprint de MediaWorkflow.

[^D05]: totec448-spec/chat-on-steroids · LICENSE. [Fuente](https://github.com/totec448-spec/chat-on-steroids/blob/main/LICENSE). Consultado el 13 de septiembre de 2026. Archivo de licencia leído.

[^D06]: buluma/steroid-chat · README.md. [Fuente](https://github.com/buluma/steroid-chat/blob/master/README.md). Consultado el 13 de septiembre de 2026. Interfaz mult proveedor, adjuntos, streaming y Cordova; README enumera cinco proveedores.

[^D08]: calin-ciobanu/killer_chatgpt_prompts · README.md. [Fuente](https://github.com/calin-ciobanu/killer_chatgpt_prompts/blob/master/README.md). Consultado el 13 de septiembre de 2026. Especializaciones reutilizables y comandos por chat.

[^A01]: ace-step/ACE-Step-1.5 · README.md. [Fuente](https://github.com/ace-step/ACE-Step-1.5/blob/main/README.md). Consultado el 13 de septiembre de 2026. Catálogo de operaciones musicales, variantes, API y recursos declarados; no benchmark propio.

[^A02]: ace-step/ACE-Step-1.5 · LICENSE. [Fuente](https://github.com/ace-step/ACE-Step-1.5/blob/main/LICENSE). Consultado el 13 de septiembre de 2026. Archivo leído.

[^A03]: Huanshere/VideoLingo · README.md. [Fuente](https://github.com/Huanshere/VideoLingo/blob/main/README.md). Consultado el 13 de septiembre de 2026. WhisperX, glosario, traducción/reflexión/adaptación, reanudación; limitaciones multihablante y cambio de idioma declaradas.

[^A07]: OpenCut-app/OpenCut · README.md. [Fuente](https://github.com/OpenCut-app/OpenCut/blob/main/README.md). Consultado el 13 de septiembre de 2026. Reescritura en curso; Editor API, MCP, plugins y headless figuran como objetivos, no capacidades verificadas.

[^A08]: OpenCut-app/opencut-classic · README.md. [Fuente](https://github.com/OpenCut-app/opencut-classic/blob/main/README.md). Consultado el 13 de septiembre de 2026. Versión anterior archivada; estructura Next.js/Rust y timeline como referencia, no nueva dependencia central.
