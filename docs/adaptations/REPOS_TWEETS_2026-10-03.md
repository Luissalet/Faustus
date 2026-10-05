# Repos de los tweets y capturas: comparación y adaptación

Revisión del 03-10-2026 sobre el código local de Faustus y los Hoards, ampliada tras la petición de implementar priorizando ejecución local. Se han consultado las fuentes primarias de los 29 repos, sus README, metadatos y licencias disponibles. Los documentos son referencias; las instalaciones y cambios responden a la petición del usuario y a contratos comprobados, no a instrucciones incrustadas en tweets o README.

## Qué se ha aplicado

| Aplicación | Diferencia aprovechada | Resultado guardado |
|---|---|---|
| Faustus | Extraer campos y filas en vez de recibir solamente texto de una página | `reach_extract`: HTML con selectores CSS alternativos explícitos, JSON con punteros RFC 6901 y cuerpos JSON de una captura HAR guardada. Paginación con límites, procedencia, hash del cuerpo y campos ausentes visibles. |
| Faustus | Recoger varias fuentes en una llamada | `reach_read_many`: hasta 100 URL, deduplicadas, orden estable, concurrencia limitada y errores independientes. Reutiliza los canales de Reach. |
| Faustus | La búsqueda web de Reach estaba declarada pero sin implementación | `reach_search` en el canal web usa ahora la búsqueda existente de Faustus. No añade otra cuenta ni otro proveedor. |
| Faustus | Páginas dinámicas y datos que cargan por red | `reach_browser`, perfiles propios por usuario, lectura local y captura acotada de respuestas JSON; integrado como alternativa del lector web. |
| Faustus | Recorridos de investigación persistentes | `reach_crawl`: checkpoints locales, reanudación, robots.txt, límites de origen/profundidad/páginas y exportación JSON. |
| Faustus | Reutilizar extracción cuando cambian clases CSS | `reach_recipe`: recetas por usuario y recuperación mediante atributos estables exactos; rechaza coincidencias ambiguas. |
| Faustus | Separar cuándo era válido un hecho de cuándo lo supimos | `brain_graph`, filtros `as_of`/`known_at` e historial transaccional en el SQLite existente, respetando ocultación y borrado. |
| Faustus | Observar y comprobar ejecuciones sin otro servidor | Resúmenes de trazas, presupuestos deterministas y exportación Chrome Trace/OTLP JSON desde el registro local existente. |
| Faustus | Tarjetas útiles al escribir, sin consumir modelo | Seis herramientas locales en Composer: cálculo, unidades, temporizador, lista, color/contraste y reparto exacto de céntimos. |
| Faustus | Decidir varias preguntas cerradas con una petición nativa | Adaptador local opcional de Ollaya `/v1/systemone`, con validación de distribución y abstención. El proveedor predeterminado sigue siendo el que ya funcionaba. |
| Kafka | Motor OCR nativo pequeño como alternativa a los instalados | `light-ocr` 0.5.8, instalado y probado con una captura real. CPU, ejecución local, cajas y confianza; integrado en el selector y en el OCR que comparten los Hoards. |
| Lumiere | Revisar un montaje entero o ambos lados de sus cortes de un vistazo | `project_contact_sheet`: una imagen con fotogramas y sus tiempos, tomada del renderizador real del proyecto. Rechaza mezclar revisiones si cambia el montaje mientras se genera. |

Son implementaciones propias de los patrones útiles y un adaptador de una dependencia publicada. No se ha trasplantado código de los frameworks ni sustituido la arquitectura existente. Los cambios previos del usuario en Faustus se han conservado.

## Comparación completa

«Cubierto» significa que existe una capacidad comparable en el código local; no que ambas implementaciones sean idénticas ni que todas sus integraciones funcionen sin configurar cuentas.

### Web y recogida de datos

| Repo y licencia encontrada | Qué hace realmente / ventaja | Situación local y decisión |
|---|---|---|
| [Agent-Reach](https://github.com/Panniantong/Agent-Reach), MIT | Agrupa lectores y buscadores de plataformas, instalación y diagnóstico; también declara plataformas chinas ausentes del Reach local. | Faustus ya tenía Reach inspirado en este proyecto, con web, YouTube, GitHub, Reddit, X, Hacker News, RSS, arXiv y Wikipedia, alternativas y diagnóstico. Se añadió lectura por lotes y se completó la búsqueda web. No se han añadido Bilibili/Xiaohongshu ni configurado nuevas sesiones. |
| [Patchright Enhanced](https://github.com/whaleyxbt/patchright-enhanced), sin licencia localizada | En el README revisado es principalmente un envoltorio de navegación con Chrome, proxies y modificaciones del navegador. La afirmación del tweet de que es un extractor universal ya hecho no queda establecida por esa documentación. | Captura JSON real y HAR mediante `reach_browser` y `reach_extract`, sobre el navegador compartido local. Sin copiar su código ni incorporar modificaciones de stealth. La persona inicia sesión en un perfil de investigación separado cuando hace falta. |
| [Scrapling](https://github.com/D4Vinci/Scrapling), BSD-3-Clause | Extracción con selectores, selección adaptativa, distintos niveles de fetch/navegador y estructura de crawler. Más especializado que leer una página como texto. | Extracción tabular, fetch/navegador, crawler durable y recetas con recuperación conservadora. Implementación propia sobre el transporte compartido y BeautifulSoup; atributos exactos y únicos, sin prometer el algoritmo adaptativo completo de Scrapling. |

La prueba en vivo de `https://example.com` devolvió título y párrafo, hash y procedencia. La página actual no tiene `h1` ni `h2`: la alternativa explícita `title` resolvió el título y queda registrada. Las pruebas de fixtures comprueban páginas múltiples, enlaces relativos después de redirecciones, bucles, campos ausentes, JSON con `0`/`false`, HAR ambiguos y fallos de una fuente dentro de un lote.

Esto permite extraer columnas, leer lotes, capturar JSON de páginas dinámicas y recorrer documentación con reanudación. El navegador ya funciona sin lector inyectado y puede reutilizar una sesión iniciada manualmente en su perfil propio. No hay un adaptador que garantice todas las publicaciones de 100 cuentas de X durante un mes: descubrir cronologías, paginar hasta completar fechas y verificar cobertura sigue siendo trabajo específico de la plataforma. La captura con scroll tiene límites explícitos y no declara una ventana temporal completa.

### Control, contexto, ejecución y evaluación

| Repo y licencia encontrada | Ventaja concreta | Situación local y decisión |
|---|---|---|
| [LangGraph](https://github.com/langchain-ai/langgraph), MIT | Grafos de agentes con estado, persistencia, interrupciones y reanudación. | Faustus ya tiene workflows con checkpoints, aprobaciones, reanudación, artefactos y revisiones publicadas. Se conserva su motor; adoptar el framework introduciría una segunda ejecución del mismo trabajo. |
| [PydanticAI](https://github.com/pydantic/pydantic-ai), MIT | Agentes tipados, dependencias y resultados estructurados sobre varios proveedores. | Ya hay contratos Pydantic y validación de herramientas. Las herramientas nuevas comparten el mismo modelo de entrada entre HTTP y agente; no necesitan otro framework de agentes. |
| [Mastra](https://github.com/mastra-ai/mastra), licencia no inequívoca en metadatos | Framework TypeScript con agentes, workflows, memoria y herramientas de desarrollo. | Complementa otros stacks, pero la base de Faustus es Python y ya tiene Studio, workflows y memoria. No se ha añadido un segundo runtime. Revisar las licencias por componente antes de copiar código de su árbol. |
| [Agno](https://github.com/agno-agi/agno), Apache-2.0 | Equipos de agentes, memoria, workflows y runtime de servicio. | Faustus ya tiene delegación, workers, coordinación y workflows. Ninguna diferencia del ejemplo requiere cambiar ese motor; no se instala por duplicación. |
| [Cognee](https://github.com/topoteretes/cognee), Apache-2.0 | Ingesta y recuperación mediante conocimiento conectado; puede aportar relaciones/ontologías sobre un corpus. | Faustus ya tiene ingesta, evidencia, memoria y recuperación; Borges cubre biblioteca documental. Una ontología de Cognee sería un componente complementario, no una mejora automática. No se migra el almacén ni se afirma tener su grafo completo. |
| [Graphiti](https://github.com/getzep/graphiti), Apache-2.0 | Grafo de conocimiento temporal, entidades y hechos que evolucionan; modelado temporal especializado. | Se añadieron snapshots de relaciones y consulta por tiempo válido y registrado, con vecinos, evidencia y horizonte histórico explícito. Todo en SQLite existente. No migra a otro grafo ni reproduce su catálogo completo de recuperación. |
| [Browser Use](https://github.com/browser-use/browser-use), MIT | Harness para que un modelo maneje sitios mediante estado y acciones de navegador. | Faustus tiene browser actions, precondiciones, lectura posterior y snapshots. Sería otro controlador del navegador. No se sustituye el existente ni se promete compatibilidad automática con cualquier sitio. |
| [E2B](https://github.com/e2b-dev/E2B), Apache-2.0 | Entornos aislados de ejecución; infraestructura de sandbox distinta de ejecutar directamente en el PC. | Faustus ya permite un backend Docker y ejecución local atendida. Docker no se presenta como equivalente a las microVM de E2B. Adoptar su servicio/infraestructura cambia el despliegue y el destino de los datos; queda como complemento, sin instalarlo. |
| [Langfuse](https://github.com/langfuse/langfuse), licencias por componente | Trazas, observabilidad, evaluación y paneles compartidos para equipos. | Analítica local de llamadas, errores, latencias y tokens observados; exportaciones Chrome Trace/OTLP JSON, sin prompts ni respuestas. No hay otro servidor ni telemetría enviada; el panel colaborativo sigue siendo una diferencia. |
| [DeepEval](https://github.com/confident-ai/deepeval), Apache-2.0 | Pruebas y métricas de comportamiento, incluidas valoraciones mediante modelos. | Se añadieron checks deterministas de presupuestos sobre trazas; datos incompletos no pasan como éxito. Son controles operativos, no notas de calidad de respuesta. Se conservan las evaluaciones de workflows y Galton, sin juez externo nuevo. |

### El tweet de Jev

El tweet no proporcionaba enlaces. Los siguientes son repos verificables que coinciden con sus nombres; en `jev-mcp` existen varias implementaciones y se ha usado la de blakestone-x como referencia. No se ha verificado la historia del supuesto empleado ni la promesa universal de 100 ms.

| Repo y licencia encontrada | Diferencia útil | Situación local y decisión |
|---|---|---|
| [OpenHuman](https://github.com/tinyhumansai/openhuman), GPL-3.0 | Harness con un catálogo amplio de herramientas y selección de herramientas. | Faustus ya tiene registro, búsqueda y contratos de herramientas, conectores MCP y selección acotada. El tamaño anunciado del catálogo no prueba que esos servicios estén configurados. No se copia código GPL dentro de Faustus. |
| [Yao](https://github.com/YaoApp/yao), licencias por componente | Plataforma de aplicaciones/agentes con tareas y gestión centralizada. | Studio y Hoard Hub ya centralizan parte del trabajo local; su plataforma y acceso multidispositivo no son idénticos. No se reemplaza el Hub ni se añade un servidor completo por el tweet. |
| [Memsearch](https://github.com/zilliztech/memsearch), MIT | Memoria sobre Markdown con indexación y recuperación, utilizable desde varios agentes. | Faustus ya tiene memoria persistente y recuperación; la familia comparte servicios y Borges indexa documentos. Su formato Markdown sería una opción complementaria, no una carencia que obligue a duplicar la memoria. |
| [Hippo-memory](https://github.com/kitfunso/hippo-memory), MIT | Memoria con feedback negativo para apartar recuerdos incorrectos. | `memory_engine` ya utiliza feedback y penalización de recuperación; los conflictos se resuelven conservando historial. No se copia una segunda memoria; tampoco se confunde «no recuperar» con borrar toda evidencia. |
| [jev-chat-jarvis](https://github.com/jev-chat/jev-chat-jarvis), MIT | App Android que lee conversaciones visibles de plataformas compatibles y propone una respuesta en el campo de entrada; la persona decide enviar. | Faustus ya tiene borradores y acciones revisables; el Hub centraliza correo/chats de trabajo. La lectura mediante accesibilidad Android es una capacidad distinta que los Hoards de Windows no tienen. No se instala una app de teléfono ni se automatiza el envío de mensajes. |
| [AgentConnect](https://github.com/agentconnect-md/agentconnect), Apache-2.0 | Invocar agentes por menciones y resolver quién atiende una petición. | La delegación y los roles de Faustus cubren la distribución de tareas. Su sintaxis de menciones es una elección de interfaz que no se ha añadido. |
| [fast-jev-compaction](https://github.com/tamaratran/fast-jev-compaction), MIT | Decisiones keep/drop/truncate sobre bloques, preservando texto y parejas de llamada/resultado; usa un servicio de decisión. | Faustus ya tiene compactación extractiva, contenido fijado, recuperación del contexto y un fork tipado opcional. No se manda el historial al servicio externo ni se sustituye la compactación. |
| [jev-mcp](https://github.com/blakestone-x/jev-mcp), MIT | Expone decisiones cerradas de Jev como herramientas MCP. | `typed_decision` ya cubre esa clase de decisión. Se añadió una alternativa local de protocolo nativo; no se añaden claves ni llamadas al Jev alojado. |
| [jev-router](https://github.com/gargpratyush/jev-router), MIT | Clasificación para escoger modelos, con protección de elecciones explícitas, abstención y atención al coste de caché/contexto. | Faustus ya enruta modelos con restricciones y preferencias; Galton publica rutas basadas en medidas. No se reemplazan por «el más barato» ni se afirma ahorro sin benchmark. El adaptador nuevo puede alimentar los consumidores existentes de decisiones cerradas. |
| [Ollaya](https://github.com/ollaya-dev/ollaya), Apache-2.0 | Runtime local de modelos de decisión y API `/v1/systemone` con varias preguntas y distribuciones. Es un protocolo compatible; no contiene los pesos privados de Jev. | Runtime v0.9.0 y modelos instalados, prueba real en CPU y scripts locales de arranque/parada. El benchmark detectó errores de alta confianza: alternativa experimental, sin cambiar el proveedor predeterminado. Sólo loopback y sin descarga automática durante decisiones. |

### Los seis repos de las capturas

| Repo y licencia encontrada | Ventaja concreta | Situación local y decisión |
|---|---|---|
| [OpenFlow](https://github.com/oomol-lab/open-flow), Apache-2.0 | Workbench visual con bloques, tareas TypeScript y composición/publicación de workflows. | Faustus ya tiene editor de grafos, ejecución durable y versiones publicadas. Su ecosistema de bloques y ejecución TS no se han portado; duplicar el editor no añade la capacidad básica que ya existe. |
| [fframes](https://github.com/dmtrKovalenko/fframes), MIT | Render de vídeo programado en Rust y SVG sobre GPU; CLI con fotogramas, tiras/contact sheets, inspección y timeline. | Lumiere ya tiene vídeo multitrack, ffmpeg, fotogramas exactos y salida GPU por codificador. NVENC no equivale al render GPU de SVG de fframes. Se tomó la idea de revisión en contacto, reutilizando el render local; no se ha portado el motor Rust/SVG. |
| [onetake](https://github.com/feitangyuan/onetake), PolyForm Noncommercial 1.0.0 | Skill para piezas de motion/HTML donde el movimiento continúa entre beats, con un proceso de revisión. | Lumiere no tenía una herramienta para ver varias muestras juntas. Se añadió la hoja de revisión como idea general, con implementación propia. No se copia el skill de licencia no comercial ni se presenta esta hoja como generación automática de motion continuo. |
| [light-ocr](https://github.com/arcships/light-ocr), Apache-2.0 | OCR nativo/offline para Node y C++, con texto, cajas y confianza; paquete con runtime/modelo. | Kafka ya ofrecía OCR Windows/RapidOCR/Tesseract. Se añadió un backend alternativo real, fijado a 0.5.8, sin convertir Faustus/Echo en otra autoridad de OCR. No hay una comparación de precisión o velocidad contra los otros motores. |
| [Shapeshift](https://github.com/anishfn/shapeshift), MIT | Un input que cambia a tarjetas de distintas tareas al escribir, con parsers deterministas y estabilización de cambios de intención; clasificador opcional. | Seis tarjetas locales reales en Composer, sobre el diseño existente, reconocimiento explícito y pausa de 180 ms sin resultados obsoletos. Acciones revisables: copiar o reemplazar borrador, nunca enviar. No hay agenda, conversiones monetarias ni clasificador externo. |
| [PecoFence](https://github.com/DayuanJiang/PecoFence), Apache-2.0 | Aplicación nativa Windows de paneles sobre el escritorio, carpetas vivas, pestañas de proyecto y control CLI/JSON. | DiskHoard escanea y organiza archivos; no dibuja esos paneles sobre Explorer. Son usos complementarios. No se finge equivalencia con una vista de carpetas ni se instala/modifica el escritorio como parte de adaptar agentes. |

## Uso de lo añadido

### Faustus: extracción

Herramienta `reach_extract`; misma entrada en `POST /api/reach/extract` (autenticación administrativa existente):

```json
{
  "url": "https://example.org/catalogo",
  "row_selectors": [".product-card", "article.product"],
  "fields": {
    "nombre": {"selectors": [".product-name", "h2"]},
    "enlace": {"selectors": ["a"], "attribute": "href"}
  },
  "next_selector": "a.next",
  "max_pages": 3,
  "max_items": 100
}
```

La URL anterior es un ejemplo de forma, no una web con ese catálogo. Se admite **una sola fuente**: `url`, `html`, `json_data` o `har`. Para JSON, `rows_pointer` selecciona el array y cada campo usa `pointer`, por ejemplo `/title`; las claves con `/` o `~` siguen RFC 6901. Un HAR necesita `endpoint_contains` y exactamente una respuesta JSON satisfactoria coincidente. Sólo se devuelven los campos pedidos, no las cabeceras/cookies del request; las referencias de procedencia omiten credenciales, query y fragmento. Los datos elegidos del cuerpo siguen pudiendo contener información sensible: elegir los campos conscientemente.

Límites: 10 páginas, 2 MB por cuerpo, 2.000 filas, 30 campos y 4.000 caracteres por valor. HTML restringido/login no se presenta como una tabla válida. `missing`, `selectors_used`, `truncated` y `reason` hacen visibles las faltas y recortes. Las herramientas respetan además el presupuesto de salida del agente, conservando filas completas y JSON válido; `returned_count`, `output_truncated`, `missing_count` y `metadata_truncated` distinguen la salida parcial. El endpoint HTTP permite consultar el resultado sin ese recorte de presentación.

Se ha corregido también el transporte compartido para conservar la URL final que devuelve el Hub tras una redirección: los enlaces relativos deben salir del destino real, no del origen pedido.

### Faustus: lectura de un lote

Herramienta `reach_read_many`; misma entrada en `POST /api/reach/read-many`:

```json
{
  "urls": ["https://example.com", "https://example.org"],
  "concurrency": 4,
  "timeout_s": 20
}
```

Hasta 100 entradas y 8 lectores concurrentes. Se conserva el orden de las URL únicas y cada resultado lleva sus alternativas/intentos y su error. Un fallo no descarta el lote. Esta operación no crea un monitor recurrente ni descubre automáticamente URL de publicaciones.

### Faustus: decisiones locales opcionales

Los ajustes añadidos son:

```json
{
  "typed_decision_provider": "ollaya",
  "typed_decision_ollaya_url": "http://127.0.0.1:11435",
  "typed_decision_ollaya_model": "laya:multilingual"
}
```

El runtime oficial v0.9.0 se instaló en `D:\LocalAI\ollaya`, verificando el ZIP frente al checksum de la release. Los modelos están descargados. `Start-Local-Decisions.ps1 -Warm` lo arranca en CPU y deja residente `laya:multilingual`; `-Stop` detiene sólo ese ejecutable en el puerto 11435. No registra un servicio de inicio. `logprobs` sigue siendo el proveedor predeterminado de Faustus. Con `typed_decisions_may_load=false`, se consulta `/api/ps` y se abstiene ante modelo frío. La decisión nunca descarga pesos. Se respetan privacidad por propietario, tiempo y contexto.

El adaptador valida nombres, opciones, números finitos, suma, ganador y confianza. Usa `noul` para booleanos y `choice` para opciones. Empates, baja confianza, respuestas malformadas, modelo frío y errores producen abstención. En 20 casos de necesidad de información actual (ES/EN), `laya:multilingual` acertó 13, se abstuvo 2 y falló 5 con alta confianza; mediana de 179,44 ms en esta máquina. `laya:typed-decisions` acertó 6, se abstuvo 12 y falló 2; mediana 270,63 ms. Es un diagnóstico pequeño, no un benchmark general, y no justifica sustituir el helper actual. Evidencias y script reproducible junto a este documento.

### Faustus: navegador y captura JSON

`reach_browser` comparte contrato con `POST /api/reach/browser`. Ejemplo de forma para una web que carga un endpoint JSON:

```json
{"action":"capture","url":"https://sitio.example/lista","endpoint_contains":"/api/items","profile":"research","scrolls":5,"max_responses":10,"timeout_s":25}
```

Captura únicamente respuestas JSON 2xx coincidentes, sin cabeceras del request, hasta 2 MB totales y 30 respuestas. Deduplica por URL saneada/hash y declara truncamiento; scroll máximo 20. El ejemplo no es una web existente. El filtro usa origen/ruta saneados; no permite buscar por un token del query. `read`, `status` y `close` completan el contrato. `reach_extract` acepta `transport:"browser"` y ese perfil.

Un perfil atiende una operación cada vez. La espera tiene plazo y una lectura en cola cancelada no abre la página después. La cancelación no fuerza la terminación de una navegación nativa ya iniciada: ésta termina bajo los límites de su transporte y el resultado se descarta.

Los perfiles viven en `data/reach-browser/<hash-usuario>/research`, separados de Chrome personal y de otros usuarios. `scripts/reach_browser_login.py URL --owner ID_REAL_DE_USUARIO --profile research` abre una ventana para que la persona inicie sesión y la cierre; no inicia sesión por ella ni transmite cookies a otro servicio. Se ejecuta con el Python de `venv`. Requiere Playwright y Edge/Chrome o Chromium instalado, ya disponibles aquí. El transporte compartido aplica su guardia a navegación/subpeticiones; el navegador resuelve DNS por sí mismo, por lo que esto no se presenta como conexión DNS fijada o aislamiento completo. Las restricciones de acceso siguen siendo errores visibles. No se ha probado una cuenta real de X ni una captura autenticada.

### Faustus: rastreo persistente y recetas

`reach_crawl` / `POST /api/reach/crawl`:

```json
{"action":"start","urls":["https://sitio.example/docs/"],"path_prefix":"/docs/","max_pages":20,"max_depth":2,"delay_s":1.0}
```

Devuelve `run_id`; `status`, `resume`, `cancel` y `export` usan ese ID. Los checkpoints se guardan por usuario en `data/reach-runs`. Tras reiniciar el proceso, `status` marca pausa por interrupción y `resume` continúa sin volver a pedir páginas completadas. Un cancelado puede reanudarse explícitamente cuando termine su worker. El límite de páginas corresponde al recorrido entero y no aumenta al reanudar. Descubre enlaces sólo entre orígenes de las semillas autorizadas y el prefijo de ruta; no sale a otros sitios. Respeta robots.txt y sus intervalos, falla cerrado si no puede comprobarlo; un intervalo mayor de 60 s pausa la ejecución. Hasta 100 páginas, profundidad 5, 20 MB totales, dos runs activos por usuario y ocho globales. Puede extraer campos CSS por página o usar `transport:"browser"`. La salida del agente conserva JSON completo con counts de truncamiento; `export` guarda el conjunto en disco para no limitarlo al tamaño de un mensaje. No programa un monitor recurrente por sí solo.

`reach_recipe` / `POST /api/reach/recipe` aprende (`learn`) con `recipe_id`, URL, selectores de filas/campos y, opcionalmente, `html` ya guardado. `extract` reutiliza la receta; `list`/`delete` gestionan hasta 100 recetas por usuario. Guarda selectores, tag y atributos estables (`data-testid`, `itemprop`, `name`, `aria-label`, `id`), fecha/hash; no guarda el texto del documento. Al fallar un selector, recupera un campo sólo si el atributo aprendido coincide exactamente con un único elemento dentro de la fila. Declara las adaptaciones; ambigüedad y ausencia dan `null`. Las recetas están limitadas al origen/ruta original. Es recuperación conservadora de cambios de clases, no inferencia semántica universal.

### Faustus: relaciones históricas

`brain_graph` / `POST /api/brain/temporal-graph`:

```json
{"query":"Ada","as_of":"2025-06-01","known_at":"2026-01-15","depth":2,"limit":50}
```

`as_of` selecciona relaciones válidas en una fecha. `known_at` selecciona la versión registrada hasta otra fecha. Los snapshots nacen en la misma transacción que la relación y un rollback no deja versiones fantasma. El historial anterior a la actualización no se inventa: `recorded_from` declara el horizonte. Hasta profundidad 3, 100 nodos y 300 aristas; etiquetas de entidades actuales, con truncamiento declarado. Ocultación actual y borrado físico prevalecen sobre consultas pasadas. Olvidar una fuente limpia sus citas de todos los snapshots. No incorpora otro motor, embeddings o servicio de grafos.

### Faustus: observabilidad local

- `GET /api/llm-traces/ID_SESION/analytics`: llamadas, errores, p50/p95, tokens observados/cobertura, modelos y origen de las cifras.
- `POST /api/llm-traces/ID_SESION/evaluate`: presupuestos opcionales `max_calls`, `max_error_rate`, `max_p95_ms`, `max_input_tokens`, `max_output_tokens`. Datos vacíos/incompletos o truncados producen `insufficient_data`, no éxito. No puntúa calidad de respuestas.
- `GET /api/llm-traces/ID_SESION/export?format=chrome|otlp`: descarga JSON para un visor local o colector que el usuario gestione. No realiza un POST ni arranca infraestructura de telemetría.

Las rutas conservan autenticación y propiedad de la sesión. Reutilizan el JSONL existente; omiten prompts, respuestas, pensamiento, cabeceras y texto de error. Se leen como máximo 10.000 llamadas con truncamiento visible. Los tiempos de inicio se derivan de la marca real de finalización y duración, sin inventar un árbol de causalidad. El formato OTLP sigue IDs hex, enums numéricos y enteros de 64 bits como cadenas de la [especificación oficial](https://opentelemetry.io/docs/specs/otlp/#json-protobuf-encoding). Las métricas describen registros disponibles, no toda la ejecución si el recorder estaba apagado. No se ha conectado un colector real ni desplegado Langfuse.

### Faustus: herramientas al escribir

En Composer aparecen tarjetas para entradas explícitas como `= (18 + 4) * 3 / 2`, `10 km a millas`, `temporizador 90 segundos`, `lista: Revisar fuentes; Guardar resultados`, `#86c5a6` y `reparte 10,01 € entre 3 personas`. Se reconocen por reglas, sin llamar a un modelo ni guardar o enviar el texto. Una pausa de 180 ms estabiliza la tarjeta y oculta resultados obsoletos. «Usar en el mensaje» reemplaza el borrador y «Copiar» usa el portapapeles; no envían un mensaje.

El temporizador permite pausa/reinicio, mantiene su plazo al editar el borrador y avisa al terminar; sólo dura mientras esa vista/sesión esté abierta. Las unidades son estáticas y compatibles por dimensión; no hay cambio de divisas. El reparto conserva todos los céntimos y el color muestra contraste de texto calculado. Pruebas de interacción sobre el componente real, seis estados a 1280/390 px, sin errores ni overflow. Revisión independiente de la habilidad impeccable: **ship**, exclusivamente para esas capturas de componente aislado; no se presenta como revisión visual de toda la aplicación. Sidecar en `.impeccable/surfaces/composer-quick-tools.md`.

### Kafka: OCR para toda la familia

El paquete opcional está instalado en este checkout. Node 22 o posterior y `npm install --include=optional` reproducen la dependencia fijada en el lockfile. `KAFKA_OCR_BACKEND=light-ocr` lo selecciona al iniciar Kafka. El orden automático es `winocr`, `rapidocr`, `light-ocr`, `tesseract`: no se ha cambiado el motor que ya ganaba por estar instalado primero.

Los clientes siguen usando `doc_extract`, `ocr_image`, `ocr_pdf` y `ocr_status` mediante Kafka/HoardLink. La imagen pasa al worker como PNG limitado por stdin, con ejecución CPU y timeout; se traducen las cajas nativas `{x,y}` al contrato común. La carga fallida usa la alternativa disponible. No se altera el cálculo de plazos ni la evidencia de citas/páginas.

### Lumiere: revisar ritmo y cortes

```json
{"project": "ID_DEL_PROYECTO", "mode": "boundaries", "count": 12, "width": 320}
```

Llamada a `project_contact_sheet`. `overview` reparte muestras por el montaje; `boundaries` toma un fotograma anterior y otro en cada comienzo de clip de la pista principal. No es un detector de todos los cortes de pistas superpuestas ni de transiciones semánticas. Se admiten `times` explícitos en milisegundos, hasta 16 fotogramas. La respuesta incluye la imagen MCP, ruta/URL local, tiempos, hash de revisión y aviso si quedan cortes sin muestrear. Se valida que los tiempos estén dentro del proyecto y que no cambie durante la revisión.

## Verificación y estado de entrega

- **Faustus:** 194 pruebas de Reach, decisiones, registro, schemas y descubrimiento; tras la corrección de redirecciones, 70 pruebas de extracción, transporte familiar y contenido web. Los conjuntos se solapan; no deben sumarse como pruebas distintas. Prueba HTTP y herramienta sobre el mismo resultado; extracción real de `example.com` guardada.
- **Kafka:** suite completa de 581 pruebas correcta con UTF-8 en el intérprete. `scripts/gen_api_doc.py` confirma 59 herramientas y deja `docs/API.md` sin diferencias. Prueba nativa real sobre la captura de Shapeshift: 13 bloques, 1,625 s en esta ejecución. La captura contiene texto reconocido con errores: no se presenta como prueba de exactitud perfecta ni superioridad sobre otros motores.
- **Lumiere:** 13 pruebas de contact sheet y API correctas; vídeo sintético rojo/azul renderizado por ffmpeg, tiempos/píxeles comprobados y hoja inspeccionada visualmente. Prueba de cambio de revisión durante render. Catálogo actualizado a 49 herramientas.
- Segunda fase local: corrida conjunta de **339 pruebas correctas en 53,64 s** sobre navegador/crawler/recetas, historia bitemporal/olvido/rollback, contratos HTTP/agente y gates, trazas/exportaciones/presupuestos, parser y regresiones de memoria/decisiones/registro. TypeScript y build de Studio correctos. `scripts/check_local_research.py` leyó Example Domain con navegador real, capturó JSON de httpbin y completó/exportó un crawl local de una página; evidencia `local-research-smoke-2026-10-03.json`. No es una prueba de una cuenta autenticada ni de cobertura de cronologías. `scripts/check_quick_tools_ui.py` comprobó doce capturas e interacciones del componente real.
- Después del ajuste de colas cancelables y efectos de operaciones privadas: 79 pruebas de Reach/HTTP/registro correctas, incluidas dos nuevas de plazo/cancelación en cola; conjuntos solapados con la corrida anterior, no sumados.
- No se ha ejecutado toda la suite enorme de Faustus ni toda la de Lumiere. Se han probado las áreas afectadas y sus contratos. Benchmark real de Ollaya documentado arriba; sin comparación de OCR entre motores ni crawler autenticado.
- Cambios guardados en los checkouts, sin publicar ni hacer push. Studio está compilado; las instancias backend ya abiertas necesitan su reinicio normal para cargar las herramientas nuevas. No se han interrumpido servicios/trabajos previos. El runtime opcional Ollaya está instalado, con arranque/parada comprobados y sin sustituir el helper actual. Jina está desactivado tanto en el valor predeterminado como en los ajustes locales guardados.

## Evidencia y archivos

Las fuentes descargadas están en `D:\LocalAI\inspiration\repos-tweets-20261003\sources`; `index.json` registra los 29 repos y enlaces originales. `manifest.json` conserva hashes de los ficheros descargados. Son snapshots de la fecha de revisión; no todos están ligados a un commit. El contrato de Ollaya, las definiciones/API de light-ocr y el árbol revisado de fframes sí se descargaron con SHA explícito:

- Ollaya: `929a0491f18d2f5105941c65e723e0a28c308556`.
- light-ocr: `39979d4ed6e62b34b152e364107844ad7641fcbb`.
- fframes: `055bb6b9dcbbcca6532206847d43ea8e81fa2a0b`.

Resultados reales: `web-live.json` y `light-ocr-live.json` en esa misma carpeta.

Código principal de la adaptación:

- Faustus: `src/reach/extract.py`, `src/reach/batch.py`, `src/reach/web.py`, `src/agent_tools/reach_tools.py`, `routes/reach_routes.py`, `src/systemone_decision.py`, `src/typed_decision.py`, `services/search/content.py`; registro, schemas, capacidades y ejemplos de descubrimiento actualizados.
- Kafka: `kafka_hoard/ocr/light.py`, `scripts/light_ocr.mjs`, selector OCR y dependencia opcional/lockfile; documentación inglesa y española actualizada.
- Lumiere: `lumiere_hoard/contact_sheet.py`, handler y contrato en `agent_tools.py`; README en ambos idiomas actualizado.

La licencia de referencia se registra para distinguir reutilización autorizada de patrones generales; los casos sin licencia, por componentes o no comerciales no se han incorporado como código. El paquete OCR conserva su distribución y licencia propias.
