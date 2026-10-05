# Capacidades de Faustus y de todos los Hoards

Actualización del 5 de octubre: la [continuación](#continuación-del-5-de-octubre-de-2026) incorpora Agency Agents y la referencia Sales CRM, 38 especialistas, 50 plantillas y 18 recorridos. La revisión del día 4 se conserva como registro de la entrega inicial.

Revisión e implementación del 4 de octubre de 2026 para el trabajo de Luis como ingeniero de IA, desarrollador, modelador 3D y usuario del PC como centro de mando. Se han identificado los 13 repositorios distintos de las capturas, inventariado los 37 hoards existentes y creado Heron: la familia actual tiene 38. La captura de Fooocus estaba repetida.

Las ampliaciones ya escritas incluyen compañeros persistentes, búsqueda de capacidades de toda la familia, quince recorridos entre hoards, edición de movimiento y exportación MP4, outpainting, un adaptador Yovoice, analítica Plausible con historial y CAD medido con exportaciones verificadas. Las capacidades anteriores se distinguen de las nuevas en las tablas. Las conexiones externas y los modelos conservan sus requisitos de configuración; un manifiesto o un catálogo conectado no demuestra que un modelo haya generado un resultado.

## Qué aportan los repositorios de las capturas

La columna de ventaja compara especialización y cobertura observables en documentación/código, no rendimiento medido entre productos. No se han comparado calidad de generación, velocidad ni experiencia de equipos reales con benchmarks comunes.

| Proyecto y fuente primaria | Qué hace y dónde destaca | Relación con la familia y resultado |
| --- | --- | --- |
| [Bitwarden clients](https://github.com/bitwarden/clients) | Bóveda de credenciales, clientes de navegador, escritorio y CLI. Su sincronización, autofill y ecosistema de clientes especializados exceden una bóveda interna de asistente. | Faustus ya tiene integración CLI `bw` y almacén cifrado de sesiones. Se mantiene esa integración; no se ha creado otra bóveda ni replicado autofill/sincronización. Las credenciales sirven a los conectores existentes sin trasladarlas a notas de compañeros. |
| [Cal.diy](https://github.com/calcom/cal.diy) y [API Cal.com](https://cal.com/docs/api-reference/v2/slots/get-available-time-slots-for-an-event-type) | Reservas públicas, disponibilidad y tipos de evento. Resuelve la entrada de citas desde fuera de una agenda mejor que un calendario personal aislado. | Faustus ya tiene calendarios y recordatorios. Se añadió preset REST para Cal.com/Cal.diy, con versiones específicas de slots y bookings. Necesita servidor compatible y credenciales; no se instaló un portal de reservas. People, Kafka y Phileas siguen siendo los propietarios de contactos, plazos y viajes. |
| [Penpot](https://github.com/penpot/penpot) y [MCP oficial](https://github.com/penpot/penpot/blob/develop/mcp/README.md) | Diseño vectorial colaborativo, componentes, prototipos y acceso al documento mediante MCP. Su lienzo editable compartido ofrece operaciones que Vitruvius no tiene. | Se añadió preset MCP HTTP en Faustus para el servidor oficial. Vitruvius mantiene criterio, tokens y crítica; su exportación de tokens ya existía. Penpot necesita servidor, plugin y un archivo abierto; no se ha sustituido su editor con una imitación. |
| [AppFlowy](https://github.com/AppFlowy-IO/AppFlowy) | Workspace de páginas, bloques, bases y vistas con colaboración. Su editor integrado y sus vistas de bases ofrecen una experiencia unificada que la familia reparte entre aplicaciones. | Los recorridos nuevos enlazan Atlas, Borges, Nightingale, Plato, Writer y los hoards de tareas/documentos. No se ha implementado el editor colaborativo completo ni un importador universal AppFlowy. El dueño de cada dato sigue siendo su hoard. |
| [Plausible](https://github.com/plausible/analytics) y [Stats API](https://plausible.io/docs/stats-api) | Analítica web agregada: tráfico, páginas, fuentes, eventos y conversiones. Ofrece datos de visitantes que no se obtienen de métricas de infraestructura o ventas. | Mercator ahora consulta Stats API v2, conserva consulta/respuesta exactas, fecha, origen e historial y permite exportarlas. Admite filtros anidados, orden y paginación. Se añadió preset en Faustus. Hace falta una instalación/cuenta y clave autorizada; la prueba utilizó un servidor de protocolo de prueba, no un sitio real del usuario. |
| [Whisper](https://github.com/openai/whisper) | Reconocimiento de voz multilingüe y traducción de audio a inglés; es un motor especializado, no un gestor completo de reuniones. | Prospero, Lumiere y Funes ya tienen transcripción y recorridos posteriores de edición, notas y compromisos. Se incorporan a la búsqueda y a los planes comunes. No se ha vuelto a descargar Whisper ni reimplementado su motor. |
| [Fooocus](https://github.com/lllyasviel/Fooocus) | Interfaz concentrada en SDXL, con ajustes automáticos de generación, inpainting/outpainting y referencias. Simplifica la operación de una familia concreta de modelos. | Prospero ya cubre generación y producciones más amplias. Se implementó el hueco de outpainting: ampliar lienzo, conservar origen, construir máscara exterior y encolar el trabajo en el motor existente. La inferencia necesita ComfyUI/modelo; no se afirma haber igualado su calidad visual. |
| [disktree](https://github.com/tobi/disktree) | Visualización del uso de disco mediante un treemap nativo y navegación por teclado. Su interacción directa es una referencia útil; su soporte en este Windows no quedó probado. | DiskHoard ya tenía treemap. Ahora las celdas son controles accesibles con flechas, Enter/Espacio y Retroceso. La prueba usó su HTML real con respuestas de directorios de prueba y no borró archivos. |
| [OpenDots](https://github.com/CopilotKit/OpenDots) | Compañeros persistentes, memoria, tareas, conversaciones/voz y ordenadores aislados OpenBot con intervención humana. La persistencia y la continuidad más allá de un chat son su aportación principal. | Faustus ahora guarda responsabilidad, notas explícitas, hoards asignados y recibos de encargos por compañero y propietario. Reutiliza su ejecutor y permisos actuales. Incluye doce plantillas para los 38 hoards. No incorpora ordenadores aislados, integración Slack ni aprendizaje automático de hechos. OpenDots pertenece a CopilotKit; no es el producto de OpenAI. |
| [Open Generative AI](https://github.com/Anil-matcha/Open-Generative-AI) | Interfaz de muchas modalidades/modelos a través de MuAPI. Destaca como catálogo unificado de proveedores. | La afirmación de la captura sobre ejecutar todos esos modelos localmente y sin facturas no describe el backend verificado: utiliza API/créditos para modelos como Sora o Veo. Se conserva el enrutamiento de motores de Prospero y Faustus; no se registraron cientos de modelos ficticios ni se contrató MuAPI. |
| [Yovoice](https://github.com/leemysw/yovoice) | Voz local apoyada en audio.cpp, catálogo de motores y HTTP/CLI/MCP. Amplía las opciones de TTS y clonación de voz local. | Prospero tiene un adaptador para IndexTTS 2/2.5, VoxCPM2, OmniVoice, Qwen3 TTS y Kokoro, con opciones por motor, jobs, cancelación de su propio trabajo y descarga validada de WAV. El catálogo del servidor distingue disponibilidad real. Necesita Yovoice y sus modelos; el protocolo se probó con un servidor controlado, sin certificar calidad de voz. |
| [HotClip](https://github.com/xixihhhh/hotclip) | Convierte vídeos largos/directos en clips: selección de momentos, reencuadre, subtítulos y montaje vertical. Destaca por una experiencia concentrada en ese recorrido. | Lumiere ya declara y expone highlights, shorts, transcripción con palabras, reencuadre, diarización y exportaciones. Se conecta con Prospero y Mercator en los planes comunes. No se duplicó un segundo editor ni se afirmó una selección de momentos superior sin comparar resultados. |
| [Null Motion](https://github.com/blixvip/NullMotion) | Escenas HTML animadas, edición con asistentes y exportación de vídeo. Combina borrador visual y resultado temporal en un mismo flujo. | Vitruvius ahora tiene composiciones versionadas con escenas/capas, tiempos, posiciones y efectos, preview HTML/SVG y MP4 real mediante Chromium y FFmpeg. La implementación es propia: el repositorio no ofrecía una licencia general clara para copiarlo. El MP4 añadido es silencioso; audio y edición avanzada pertenecen a Lumiere/Prospero. |

Las capturas también mencionan Apollo, Cloudflare, Swokei y Soro. Se añadieron presets de API para [Apollo](https://docs.apollo.io/reference/people-api-search) y [Cloudflare](https://developers.cloudflare.com/api/). Son conectores configurables, no campañas ni infraestructura publicadas. Swokei, Soro y GrokBots no quedaron identificados con suficiente documentación/repositorio verificable para incorporar APIs o afirmar funciones concretas. La preferencia por los dots de OpenAI se ha recogido como intención de compañeros persistentes; no se ha atribuido a OpenAI el código de OpenDots.

## Cambios implementados y cómo utilizarlos

| Lugar | Función nueva | Entrada para usarla |
| --- | --- | --- |
| Faustus | Compañeros con responsabilidad, notas explícitas, estado, historial, revisiones y protección frente a encargos duplicados. | Agentes → Compañeros; `coworker_list`, `coworker_save`, `coworker_run`. Crear desde doce plantillas o definir uno propio. |
| HoardLink / Hub | Búsqueda de capacidades de toda la familia, incluidas herramientas sólo stdio; comprobación acotada de catálogos conectados y sus esquemas. | Pestaña Capacidades; `hub_capability_find`, opcional `check=true`. Las herramientas stdio se ejecutan desde el MCP de Faustus. |
| HoardLink / Hub | Quince recorridos con proveedores y huecos por etapa. | `hub_recipe_plan`; son planes revisables, no jobs ejecutados automáticamente. |
| Vitruvius | Escenas, capas, geometría, tiempos, seis efectos, plantillas, revisiones, preview y exportación HTML/MP4. | Movimiento; `motion_templates`, `motion_save`, `motion_list`, `motion_get`, `motion_render`. |
| Prospero | Outpainting con fuente y máscara conservadas; botón de ampliación en la imagen. | Lightbox → ampliar lienzo; `studio_outpaint` o endpoint del asset. El original se conserva; el resultado generado por VAE puede cambiar píxeles interiores. |
| Prospero | Adaptador Yovoice con opciones validadas según motor. | Configurar `YOVOICE_URL` y token según README; elegir motor de voz. No presupone modelos instalados. |
| Mercator | Analítica Plausible persistida y exportable. | Analítica web; `plausible_query`, `plausible_history`. Configurar variables de servidor, nunca una clave en el navegador. |
| DiskHoard | Treemap accesible y navegación espacial con teclado. | Tab, flechas, Enter/Espacio y Retroceso sobre las celdas. |
| Faustus | Presets Cal, Plausible, Apollo, Cloudflare y MCP Penpot. | Integraciones REST y MCP; completar URL/autorización del servicio correspondiente. |
| Heron | CAD con medidas, recetas, booleanas y STEP/STL/vistas comprobados. | Plugin `heron`, puerto 5204; `cad_templates`, `cad_build`, `cad_get`, `cad_list`. Entorno dedicado instalado. |

La finalización de un encargo de compañero significa que el ejecutor devolvió un recibo; no convierte una respuesta del modelo en una garantía de éxito. Si el llamador se interrumpe, el estado queda pendiente de inspección y no se lanza otro trabajo encima. La interfaz permite cerrar ese estado sólo tras reconocer que se ha comprobado el trabajador. Las notas no absorben automáticamente secretos, conversaciones ni inferencias personales.

## Cobertura de los 38 hoards

Cada fila quedó incluida en el inventario común y en al menos una plantilla de compañero. En los hoards sin cambio de lógica de dominio, la mejora de esta entrega es acceso y composición comunes; sus capacidades de dominio que aparecen aquí ya existían. El inventario procede de sus manifiestos; se profundizó en el código de los servicios modificados y en los conectores relevantes, no se reprobó cada herramienta de los 38 servicios.

| Hoard | Qué aporta al trabajo de Luis | Uso en los recorridos y cambio de esta entrega |
| --- | --- | --- |
| Argus | Historia de pantalla, OCR y actividad con exclusiones. | Contexto del PC y memoria personal; búsqueda común. |
| Atlas | Archivos compartidos, proyectos y revisiones. | Procedencia y derivados entre todos los trabajos; búsqueda común. |
| Babel | Referencia de APIs, docsets, entornos y revisión de código/localización. | Ingeniería de software y compañero especializado. |
| Borges | Biblioteca, búsqueda híbrida, pasajes y citas. | Investigación, contexto y documentación con evidencia. |
| Cassandra | Salud, incidentes, logs, GPU y recuperación de servicios. | Centro de mando, IA y disponibilidad; se conservan sus políticas y pausas. |
| Cicero | Presentaciones desde fuentes, revisiones y exportación PPTX/PDF. | Investigación, diseño y producción editorial. |
| CookHoard | Recetas, despensa, menú, tickets y coste de comida. | Recorrido menú → compra → gasto y compañero de casa. |
| Daguerre | Búsqueda visual, EXIF, álbumes, duplicados y hojas de contacto. | Referencias visuales y revisión de catálogos 3D. |
| DiskHoard | Uso de disco, archivos grandes y limpieza con controles existentes. | Navegación de treemap mejorada; centro de mando. |
| Dorian | Contexto personal con evidencia y actualizaciones revisables. | Memoria personal junto a Argus, Echo y Funes. |
| Echo | Historial de portapapeles con detección sensible. | Recuperación de contexto con permisos existentes. |
| Funes | Actividad, transcripción, reuniones y recuperación de contexto. | Reunión → compromisos → plazos; memoria y centro de mando. |
| Galton | Benchmarks, comparación, rutas de modelos y regresiones. | Dataset → modelo → evaluación; compañero de IA. |
| GamerHoard | Biblioteca, sesiones, backlog e importación Steam. | Ahora aparece en búsqueda aunque sólo tenga MCP stdio; juegos → radar → presupuesto. |
| Gepetto | Proyectos y trabajos de modelado artístico. | Taller 3D; Heron cubre sólidos con medidas como complemento. |
| Heron | Sólidos medidos, booleanas, STEP/STL y vistas verificadas. | Hoard nuevo, funcional y registrado; CAD → catálogo. |
| Hoard Hub | Inventario, procesos, jobs, bus y recursos compartidos. | Descubrimiento completo y quince planes añadidos al Hub de HoardLink. |
| HomeHoard | Inventario doméstico, garantías y mantenimiento. | Compra → envío → garantía → mantenimiento. |
| Hypatia | Estudio, preguntas, repetición, evaluación y citas. | Fuentes → investigación → estudio. |
| Jobhunter | Contextos, ofertas, candidaturas y respuestas. | Empleo → contactos → documentos. |
| Kafka | Papeles, OCR, plazos, facturas, contratos y garantías. | Compromisos, compras y finanzas con sus documentos. |
| Laplace | Cálculo, unidades, estadística y SQL sobre archivos. | Verificación numérica de IA, CAD, finanzas y software. |
| Ledger | Gastos, presupuestos, suscripciones e ingresos de ventas. | Compras, juegos, casa y finanzas. |
| Links | Marcadores, extracción, feeds y seguimiento de fuentes. | Investigación, estudio y seguimiento de proyectos. |
| Lumiere | Editor de vídeo, transcripción, highlights, captions y reencuadre. | Montaje de escenas Vitruvius y medios Prospero; capacidades HotClip ya presentes. |
| Mercator | Productos, publicaciones, ventas y métricas. | Plausible añadido; cierre de recorridos de diseño/3D con estadísticas. |
| Midas | Datos de mercado, hipótesis y backtests reproducibles. | Análisis numérico y compañero financiero; sin operaciones financieras automáticas. |
| Nightingale | Ingesta, calidad, SQL, gráficos y dashboards. | Datos para IA, investigación y finanzas. |
| People | Contactos, contexto, compromisos y recordatorios. | Reuniones, empleo, regalos y contexto personal. |
| Phileas | Envíos, viajes, check-in y gastos compartidos. | Compras, empleo y agenda. |
| Plato | Editor y exportaciones de documentos/proyectos. | Taller 3D, documentación y candidaturas mediante su puente REST existente. |
| Prospero | Generación, voz, personajes y producciones multimedia. | Outpainting y Yovoice añadidos; recursos para diseño/vídeo y narrativa. |
| Pygmalion | Datasets, LoRA, mezcla, GGUF y linaje de modelos. | Dataset → modelo → evaluación con Galton. |
| Scheherazade | Estado del mundo, hechos, hilos y continuidad narrativa. | Canon → manuscrito → voz → edición. |
| Tantalus | Precios, disponibilidad, ofertas y lanzamientos. | Compras, regalos, juegos y presupuesto. |
| Vitruvius | Criterio de diseño, tokens, capturas y crítica. | Movimiento y exportaciones añadidos; Penpot externo vía MCP Faustus. |
| Vulcan | Biblioteca STL/3MF/OBJ, métricas, duplicados y fichas. | Catálogo de modelos artísticos y CAD, con derivados conservados. |
| Writer | Manuscritos, notas, capítulos y worldbuilding. | Producción editorial y narrativa con continuidad. |

Las quince recetas son diseño y movimiento, ingeniería de IA, producción 3D, fuentes y estudio, centro de mando, reunión y compromisos, ciclo de compra, producción editorial, menú y gasto, ingeniería de software, empleo, investigación numérica, CAD medido, memoria personal y biblioteca de juegos. Todos sus requisitos declarados encontraron al menos un proveedor en el inventario actual. Esto verifica el mapa de capacidades, no una ejecución completa de todas las etapas.

## Por qué se creó Heron

La familia ya tiene generación, edición audiovisual, memoria, documentos, datos, operaciones y modelado artístico. Faltaba una ruta de CAD medido con sólidos analíticos y STEP. Heron llena ese hueco con [CadQuery](https://github.com/CadQuery/cadquery) y OCCT, en CPU y en un entorno propio. Su prueba construyó piezas reales, comparó volúmenes conocidos, reimportó STEP y comprobó STL con un segundo lector. No es todavía un sistema de croquis, ensamblajes o simulación; el formato actual permite cajas, cilindros, esferas y sus operaciones.

## Validación y límites pendientes

Se ejecutaron las suites completas de Vitruvius, Prospero, Mercator y Hub, además del conjunto de Faustus afectado y las pruebas reales de Heron. Los recuentos exactos y las comprobaciones finales están en `HOARDS_VALIDACION_2026-10-04.json`; el informe distingue suites de pruebas dirigidas para no sumar dos veces el mismo test.

Se verificaron builds de las interfaces de Faustus, Vitruvius y Prospero; capturas de escritorio/móvil de compañeros, movimiento, analítica, capacidades y CAD; un MP4 real con sus frames y metadatos; una construcción CAD por API/UI y otra por stdio MCP. Se regeneraron y revisaron las cuatro capturas de documentación de Prospero. Las pruebas de APIs externas usaron fixtures de protocolo; no utilizaron cuentas ni consumieron crédito.

Quedan sin prueba real de proveedor el TTS Yovoice, la inferencia GPU de outpainting, la conexión a un archivo Penpot del usuario y las cuentas de Cal, Apollo, Plausible o Cloudflare. El preset Penpot utiliza `localhost:4401/mcp`; su plugin debe estar conectado al documento. Los presets Cal fijan versiones por endpoint, que deben corresponder al servidor usado. La exportación motion añadida no mezcla audio y no es un render de larga duración con recibo recuperable tras perder el llamador.

No se copió código con licencia dudosa ni se ocultaron los costes de APIs o las licencias de modelos. La metadata original registra revisiones y licencias: MIT, Apache, MPL, GPL/AGPL o ausencia/ambigüedad según proyecto. Un frontend abierto no convierte en locales o gratuitos todos los motores que enumera. El inventario machine-readable, las revisiones upstream y la evidencia están guardados junto a este documento y en `D:/LocalAI/_hoard_research_20261004`.

Los repositorios tenían trabajo previo; se conservó. No se han publicado cambios, enviado campañas, contratado servicios, iniciado compras ni alterado las automatizaciones de Pokémon. Las nuevas dependencias CAD están aisladas del entorno de Faustus.

## Continuación del 5 de octubre de 2026

Se integran los dos repositorios pendientes y se mantiene la cobertura de los 38 Hoards. Esta continuación añade procedimientos de dominio para todos mediante Faustus, y lógica comercial nueva en Mercator. Los doce compañeros de la entrega inicial se conservan; hay ahora **50 plantillas**, con **38 especialistas individuales**, y **18 recorridos** en el Hub. Un especialista es una definición ejecutable con método y contrato de entrega: no incorpora por sí solo un motor o una nueva herramienta al Hoard.

| Repositorio verificado | Aportación e integración | Alcance real |
| --- | --- | --- |
| [Agency Agents](https://github.com/msitarzewski/agency-agents), `83294689da3832c0a9f223221148c411fd3eacc0` | Doce métodos adaptados de evidencia, proyectos, infraestructura, IA, estadística, reuniones, narrativa, diseño, medios, ventas, finanzas y MCP. Cada Hoard recibe misión, procedimiento y comprobación específicos. | Los perfiles `hoard-<id>` cargan por el catálogo normal de agentes; aparecen como plantillas en Compañeros. Se conserva licencia MIT, fuente fijada por revisión y hash en `config/agents/agency/`. No se ejecutó su instalador ni se atribuyen memoria aprendida o métricas de calidad no medidas. |
| [Sales CRM](https://github.com/kargulstudio/sales-crm), `8954a187812285f69d954a1faf9a9773dafa20c2` | Se toma como referencia funcional el recorrido empresas → oportunidades → etapa/responsable → ficha → actividad → exportación. Implementación propia en Mercator, con filtro por proyecto para servicios de IA/desarrollo y encargos 3D. | El repositorio contiene estado de cliente y datos de demostración, con valores derivados para actividad/salud. No proporciona una API comercial que conectar. No se copiaron sus componentes ni se trasladaron empresas o cifras de ejemplo al almacenamiento del usuario; no se encontró una licencia general de reutilización. |

### Entradas de uso

- **Faustus → Agentes → Compañeros → Elegir especialidad**: buscar por tarea o filtrar por Hoard, crear la plantilla y guardar responsabilidad/contexto. Los nuevos perfiles pueden descubrir herramientas mediante `plugins_list` y `lookup_tools` y usar el MCP conectado; mantienen las restricciones de la sesión y no delegan de nuevo. No usan shell ni escriben archivos mediante herramientas locales. Las herramientas MCP pueden modificar sus propios datos cuando el encargo lo autoriza: no se presenta el perfil como un lector exclusivo. Los compañeros existentes conservan su agente guardado; las doce plantillas iniciales conservan sus agentes base, y quien necesite operaciones de Hoards puede elegir el especialista nuevo.
- **Mercator → Clientes** (`/#crm`): crear empresa, añadir oportunidad, indicar proyecto/responsable/etapa, importe y moneda, próxima acción y fecha. Una misma empresa puede tener oportunidades de IA y 3D. Los filtros se aplican a lista, resumen y exportaciones.
- **Herramientas de Mercator**: `crm_company_save`, `crm_deal_save`, `crm_list`, `crm_get`, `crm_activity_add`, `crm_summary`, `crm_export`. Las ediciones requieren `id` y `expected_revision`; una revisión antigua se rechaza. `crm_activity_add` requiere `request_id` y recupera el mismo registro en un reintento idéntico. Crear empresas/oportunidades no es una operación idempotente.
- **Referencias y agenda**: `people_ref` enlaza contactos de People; `document_refs` acepta referencias de Atlas, Kafka y Plato. Se registran relaciones con el Hub cuando está disponible. Se valida la forma y el dueño de la referencia, no la existencia del recurso remoto. Las próximas acciones fechadas de oportunidades abiertas salen en la agenda familiar como `followup`.
- **Exportaciones**: JSON incluye empresas, oportunidades, historial completo, filtros aplicados y resumen; CSV contiene oportunidades con IDs y referencias. Se neutralizan fórmulas de hoja de cálculo en campos de texto. La respuesta MCP conserva el límite compartido de 20 000 bytes y puede truncarse; la interfaz local permite la exportación completa. La ficha muestra las 200 actividades más recientes.
- **Recorridos nuevos**: `client-pipeline`, `client-ai-delivery`, `client-3d-commission`. Conservan People, Atlas, Kafka/Plato, Pygmalion/Galton, Gepetto/Heron/Vulcan y Ledger como proveedores existentes; son planes por capacidad, todavía no ejecuciones automáticas completas.

El CRM guarda datos en tres tablas nuevas de `mercator.sqlite3` mediante la migración 3, sin reemplazar publicaciones, analítica ni ventas previas. Los importes abiertos/ponderados se calculan con decimales y separados por moneda. La probabilidad es una estimación introducida por el propietario. **Ganado no significa cobrado**: no genera un ingreso en Ledger ni una venta de Cults. El historial distingue notas de interacción de cambios de ficha, con fecha observada y fecha de registro. No se inventan llamadas, emails, reuniones ni puntuaciones de salud.

### Especialista de cada Hoard

El mapa completo de misiones, fuentes, Hoards relacionados y criterios de comprobación está también en `HOARDS_INVENTARIO_2026-10-04.json` y `config/agents/agency/specialists.json`.

| Hoard | Perfil | Entrega y comprobación |
| --- | --- | --- |
| Argus | `hoard-argus` | Cronología con capturas citadas, hora, aplicación y huecos de cobertura. |
| Atlas | `hoard-atlas` | Mapa de entradas y derivados con rutas resueltas, revisiones y procedencia. |
| Babel | `hoard-babel` | Contratos y diferencias con versión exacta, ejemplos comprobados y referencias. |
| Borges | `hoard-borges` | Síntesis con documento, página o sección y evidencia a favor y en contra. |
| Cassandra | `hoard-cassandra` | Línea temporal, hipótesis ordenadas y comprobación de recuperación; no detener procesos ajenos. |
| Cicero | `hoard-cicero` | Guion y deck editable con exportación abierta y revisión visual de todas las diapositivas. |
| Cookhoard | `hoard-cookhoard` | Menú con cantidades, déficits y costes separados de estimaciones; no asumir stock. |
| Daguerre | `hoard-daguerre` | Selección con ruta y metadata, hoja de contacto revisada y candidatos a duplicado sin borrado. |
| Diskhoard | `hoard-diskhoard` | Distribución y candidatos de limpieza con tamaño y ruta; distinguir propuesta de borrado ejecutado. |
| Dorian | `hoard-dorian` | Hechos citados, conflictos y propuesta; no guardar inferencias personales como hechos. |
| Echo | `hoard-echo` | Fragmentos con fecha y procedencia, evitando trasladar secretos al contexto persistente. |
| Funes | `hoard-funes` | Decisiones, acciones, dueños, fechas y preguntas con referencias a la entrada completa. |
| Galton | `hoard-galton` | Base, candidato, métricas y regresiones con receta y muestras; no confundir catálogo con disponibilidad. |
| Gamerhoard | `hoard-gamerhoard` | Plan de juego con propiedad, instalación y precio distinguidos; no iniciar compras. |
| Gepetto | `hoard-gepetto` | Derivados editables, renders revisados y comprobación de geometría y exportaciones. |
| Heron | `hoard-heron` | Receta, unidades, volumen esperado, STEP reimportado y STL comprobado; no afirmar simulación. |
| Hoard Hub | `hoard-hoardhub` | Plan por etapa con herramienta, esquema, requisitos, huecos y recibos de lo realmente ejecutado. |
| Homehoard | `hoard-homehoard` | Ficha enlazada a compra y garantía, próximos mantenimientos y datos que faltan. |
| Hypatia | `hoard-hypatia` | Guía, preguntas y criterios de corrección sustentados en pasajes; huecos identificados. |
| Jobhunter | `hoard-jobhunter` | Adecuación a oferta, documentos y estado de candidatura; enviar sólo ante encargo explícito. |
| Kafka | `hoard-kafka` | Campos con página o fragmento, fechas verificadas y ambigüedades de OCR por revisar. |
| Laplace | `hoard-laplace` | Procedimiento reproducible, unidades y supuestos; validar resultados numéricos contra una referencia. |
| Ledger | `hoard-ledger` | Totales por moneda y periodo, duplicados y diferencias con fuente; distinguir previsto de pagado. |
| Links | `hoard-links` | Selección de fuentes primarias con cambios observados y documentos enlazados. |
| Lumiere | `hoard-lumiere` | Proyecto y vídeo reproducible con revisión de cortes, tiempos, subtítulos y formato final. |
| Mercator | `hoard-mercator` | Pipeline por moneda, próxima acción e historial real; ganado no es cobrado ni probabilidad una predicción. |
| Midas | `hoard-midas` | Periodos, costes, sesgos y sensibilidad; no garantizar rentabilidad ni ejecutar operaciones. |
| Nightingale | `hoard-nightingale` | Dataset, reglas, consulta y gráfico comprobados, denominadores y límites de inferencia. |
| People | `hoard-people` | Ficha y próxima acción con responsable y fecha explícitos; no inventar interacciones ni enviar mensajes. |
| Phileas | `hoard-phileas` | Itinerario o envío con fechas, zona horaria, estado y comprobantes; no reservar sin encargo. |
| Plato | `hoard-platos` | Documento editable y exportaciones abiertas con revisión de contenido y disposición. |
| Prospero | `hoard-prospero` | Receta, modelo disponible, archivos generados y verificación de medios; distinguir job encolado de resultado. |
| Pygmalion | `hoard-pygmalion` | Datos y licencia, receta y revisiones, exportación comprobada y comparación antes/después. |
| Scheherazade | `hoard-scheherazade` | Mapa de continuidad y contradicciones con referencias; cambios al canon siempre explícitos. |
| Tantalus | `hoard-tantalus` | Opciones con fuente, moneda, coste total conocido y disponibilidad; no asumir descuentos vigentes. |
| Vitruvius | `hoard-vitruvius` | Sistema y estados comprobados, capturas de tamaños pertinentes y exportación temporal verificada. |
| Vulcan | `hoard-vulcan` | Ficha con medidas calculadas, previews y derivados enlazados; no inventar dimensiones de catálogo. |
| Writer | `hoard-writer` | Manuscrito versionado con continuidad, cambios justificables y referencias al canon. |

### Validación de la continuación

- faustus: **92 pruebas aprobadas**, 0 omitidas; alcance: coworkers, agent library and definitions.
- mercator: **95 pruebas aprobadas**, 0 omitidas; alcance: complete suite including CRM.
- hoardlink: **5 pruebas aprobadas**, 0 omitidas; alcance: capability planner tests.

- Compilación de Faustus y comprobación TypeScript completas. Detector de las superficies modificadas sin hallazgos.
- CRM probado con servidor HTTP real, datos de prueba aislados, interfaz real y puente MCP stdio. Se crearon empresa/oportunidad, se registró interacción, se editó etapa y se filtraron los dos tipos de proyecto. Se descargaron JSON y CSV y se verificaron sus datos. En móvil se comprobó desplazamiento horizontal por teclado y foco visible en la tabla.
- Componente real de Compañeros probado con API de prueba aislada: búsqueda, filtro de Hoard y guardado del especialista seleccionado. Capturas de ambas superficies a 1440 y 390 píxeles, con comprobación de desbordamiento y errores del navegador.
- Revisión visual Impeccable: disposición final `ship` para los tres hallazgos puntuados. Se resolvieron el acceso por teclado a la tabla móvil, el contexto de producto persistido y el foco visible de los filtros de Faustus. No constituye una auditoría de las aplicaciones completas.
- Los 38 perfiles cargaron y todas las plantillas cumplen el esquema de Compañeros. Los 18 planes encontraron proveedor para cada requisito declarado. Esto verifica catálogo y composición; no es una prueba completa de cada herramienta de cada servicio ni una evaluación de respuestas de modelos.
- Una ejecución adicional de pruebas del bucle de agentes agotó el tiempo mientras una tarea existente del índice calculaba embeddings; no se cuenta como aprobada. Las suites dirigidas de compañeros/definiciones y la comprobación explícita de restricciones heredadas sí terminaron.

Los recibos de esta continuación se añadieron a `HOARDS_VALIDACION_2026-10-04.json`; los recibos de la entrega inicial permanecen intactos. La lectura acotada de salud de la familia está en `D:/LocalAI/_hoard_research_20261004/family-health-extension.json`: no arranca ni reinicia servicios, y un servicio parado no se presenta como funcionalmente comprobado.

Las interfaces quedan documentadas en [Faustus: Compañeros](ui/coworkers/DESIGN.md) y [Mercator: CRM](<C:/Users/luism/Desktop/Proyectos independientes/Mercator's Hoard/docs/ui/crm/DESIGN.md>), con sus registros de superficie y sidecars. Se mantienen los sistemas visuales existentes de ambas aplicaciones.

Siguen pendientes las configuraciones de proveedor citadas en la entrega inicial y las pruebas de inferencia real que necesitan sus modelos. Los cambios del servidor Mercator/Hub se cargan al arrancar de nuevo esos servicios; no se han reiniciado procesos ajenos. Las pruebas utilizaron almacenamiento aislado, sin datos comerciales del usuario, campañas, publicaciones ni cambios en las automatizaciones de Pokémon.
