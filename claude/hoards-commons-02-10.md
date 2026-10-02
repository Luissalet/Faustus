# HoardLink 0.8.1: validación en Windows

Registro del 03-10-2026. Los 34 paquetes del relevo están aplicados en `main`; Prospero conserva su desarrollo local mediante una integración de ambas ramas. Los cambios nuevos se guardan en commits locales. No se ha hecho push.

## Trabajo completado

- Migraciones de JobHunter, Funes, Prospero, Lumiere y Nightingale comprobadas en Windows. Copias Python y Node sincronizadas con HoardLink 0.8.1.
- Contratos comunes: errores y validación de herramientas, excepción al límite de salida por herramienta, protección opcional de rutas directas, adaptadores de seguridad para Flask y http.server, deduplicación de agenda y correcciones de rutas y procesos en Windows.
- Hub: pestaña Servicios, estado de los seis proveedores, flujo de descarga a transcripción y biblioteca, y regla recomendada desactivada inicialmente. Las pestañas se desplazan sin ensanchar la página en móvil.
- Faustus: navegación a través del Hub, transcripción con Funes, OCR acotado con Kafka y evidencia por página, políticas de URL compartidas manteniendo los modelos locales, lector RSS/Atom común, exportación ICS plegada y con DTSTAMP, y localización compartida de binarios multimedia.
- Writers de escritorio: motor de descargas compartido, adaptador tipado y comprobación de navegación, redirecciones, recursos y WebSocket durante la captura de páginas.

## Suites de las aplicaciones

| Repositorio | Resultado | Duración aproximada |
|---|---|---|
| Argus's Hoard | Correcto | 35 s |
| Babel's Hoard | Correcto | 100 s |
| Borges's Hoard | Correcto | 33 s |
| Cassandra's Hoard | Correcto | 53 s |
| Cicero's Hoard | Correcto | 21 s |
| CookHoard | Correcto | 24 s |
| Daguerre's Hoard | Correcto | 81 s |
| Disk Hoard | Correcto | 16 s |
| Dorian's Hoard | Correcto | 25 s |
| Echo's Hoard | Correcto | 14 s |
| Funes's Hoard | Correcto | 866 s |
| Galton's Hoard | Correcto | 177 s |
| Gepetto's Hoard | Correcto | 33 s |
| HoardLink | Correcto: 3470 pruebas, 78 omitidas | 361 s |
| HomeHoard | Correcto | 6 s |
| Hypatia's Hoard | Correcto | 251 s |
| JobHunter's Hoard | Correcto | 21 s |
| Kafka's Hoard | Correcto | 82 s |
| Laplace's Hoard | Correcto | 96 s |
| Ledger's Hoard | Correcto | 3 s |
| Links Hoard | Correcto | 28 s |
| Lumiere's Hoard | Correcto | 389 s |
| Mercator's Hoard | Suite final en curso | 36 s |
| Midas's Hoard | Correcto | 46 s |
| Nightingale's Hoard | Correcto | 343 s |
| People's Hoard | Correcto | 3 s |
| Phileas's Hoard | Correcto | 29 s |
| Plato's Hoard | Excepción previa: silueta estrella | 25 s |
| Prospero's Hoard | Correcto | 638 s |
| Pygmalion's Hoard | Correcto | 116 s |
| Scheherazade's Hoard | Correcto | 231 s |
| Tantalus's Hoard | Correcto | 27 s |
| Vitruvius's Hoard | Correcto | 44 s |
| Vulcan's Hoard | Correcto | 67 s |

Faustus: 277 pruebas correctas y 2 omitidas en las áreas modificadas. Writers: nueve grupos de pruebas Electron, comprobaciones de tipos, lint de distribución, conformidad, build y comprobación de la copia compartida correctos. Los builds de Babel y Prospero y la comprobación de documentación de API de Pygmalion también pasan.

En Plato queda `test_real_end_to_end[star]`: IoU 0.975507 frente al mínimo 0.98, con 310 pruebas correctas. El trazado, las máscaras y esta prueba no han cambiado respecto a la base del paquete; no se ha reducido el umbral para ocultar el fallo.

Se reiniciaron el Hub y las 23 aplicaciones que estaban en marcha, tras comprobar que no había trabajos activos ni grabación en Funes; todas volvieron a responder correctamente. Las aplicaciones que estaban paradas no se arrancaron.

## Verificación con servicios reales

| Servicio | Evidencia |
|---|---|
| Links | Descarga pública limitada a tres segundos; archivo de audio generado. |
| Funes | Transcripción del audio de prueba y de la voz sintetizada; texto y segmentos devueltos sin grabación persistente. |
| Prospero | Voz española generada con Piper y fichero WAV devuelto. |
| Kafka | OCR de un PDF de prueba sin capa de texto; página recuperada. |
| Borges | Embeddings de 384 dimensiones y documento de transcripción confirmado con `library_documents`, estado `ok`. |
| Hub Web | Página pública recuperada desde Faustus, HTTP 200. |

El flujo se verificó mediante el servidor MCP del Hub: reutilizó una descarga de Links, esperó a Funes, guardó `*.transcript.txt` y añadió/reindexó la colección en Borges. Además de `index_requested`, se confirmó el documento ya indexado. Los adaptadores reales de Faustus verificaron fetch, descarga, transcripción, extracción y OCR de adjunto con evidencia.

La repetición de la transcripción detectó una biblioteca CUDA ausente que bloqueaba un segundo intento del motor nativo. La implementación común recuerda únicamente fallos de carga de bibliotecas: los siguientes motores en modo `auto` usan CPU durante ese proceso. No convierte en permanente una falta transitoria de memoria; una elección explícita de CUDA sigue siendo explícita. Reiniciar permite volver a intentar GPU después de instalar las bibliotecas.

## Límites y conservación de cambios

- El catálogo de Servicios demuestra que existen las herramientas; los modelos se comprueban al ejecutarlas.
- El trabajo del Hub termina al solicitar la indexación, tiene un límite de espera de diez minutos y no sobrevive a su reinicio; los identificadores de los trabajos de los proveedores permiten seguirlos por separado.
- Chromium no permite fijar la IP de sus conexiones; la captura vuelve a comprobar DNS y todas las solicitudes observables. El motor externo de descargas administra sus propias conexiones.
- Las pruebas oficiales de Writers pasan. Una comprobación adicional de TypeScript sobre pruebas no incluidas en su comprobación oficial conserva un TS2367 previo en `tests/media-security.ts:203`.
- Faustus conserva su rama principal `master`; los 34 repos del paquete están en `main`.
- Se conservan fuera de estos commits los cambios previos de Faustus sobre parada, escritorio y documentación de adaptaciones.
- Las pruebas y descargas de verificación utilizan material sintético o público, separado de los datos de trabajo. Los logs detallados quedan en la carpeta temporal de validación del equipo.

El Manual de Faustus y la familia Hoard se ha actualizado con los servicios compartidos y la regla opcional, conservando sus tablas y secciones. Contrato de servicios: `HoardLink/docs/commons/services.md`.
