# Pendientes de cierre

Actualizado: 11-09-2026 (00:30). Sólo trabajo vigente; quitar cada entrada al cerrarla.

## Comprobaciones pendientes

- **Voz física:** conversación completa con micrófono en español e inglés. No activar grabación ni permisos para cerrar esta casilla sin intervención del usuario.

- **Docker Desktop y el entorno del puente MCP.** Toda la noche del 08-09 se cayó al arrancar con `unable to get 'ProgramData'` cuando lo lanzaba una sesión a través del puente (cuatro vías probadas: directa, con el entorno repuesto, vía `explorer.exe` y como tarea programada interactiva). Lanzado por Luis a las 22:12 arrancó a la primera. El puente entrega un PowerShell **sin `ProgramData` ni `ALLUSERSPROFILE`**; anotado por si vuelve a aparecer con otro programa.

- **`OLLAMA_MAX_LOADED_MODELS=1` en el entorno de Ollama** para lo que no pasa por Faustus (`ollama run` en una terminal). No es código nuestro: es una variable en el servicio de Ollama de la máquina de Luis.

## Lista de Luis, 09-09 de madrugada (estado a las 23:10)

1. ~~Research con Docker levantado → 0 fuentes.~~ Cerrado. No era la búsqueda: `rp-…` de las 22:13 corrió con `q8_0` a 131k de contexto derramando a RAM, todas las llamadas al modelo caducaron a los 90 s, 0 rondas, y el handler lo registró como «completed successfully». Ahora un run sin rondas ni fuentes es un **fallo con causa** (`ResearchFailed` en `src/deep_research.py`: «el modelo no respondió», «la búsqueda no devolvió nada», «cancelado»). SearXNG devuelve 20 resultados por consulta; el 0 de la noche anterior era el contenedor calentando.
2. ~~El Retry de una research no responde bien.~~ Cerrado: `launch()` no limpiaba `sessionId` y el seguidor se reenganchaba al stream ya terminado. Ahora aborta el seguidor viejo y parte de cero.
3. ~~El texto del cuadro del chat se pierde al cambiar de chat.~~ Cerrado: borrador por sesión (y uno para «conversación nueva») en `localStorage`; se vacía al enviar. Comprobado por pantalla: dos chats, dos borradores, cada uno vuelve intacto.
4. ~~«Waiting for the model» con el modelo cargado.~~ Cerrado (59436f6d): el latido de 10 s pregunta a Ollama `/api/ps` y la línea en vivo dice «Cargando el modelo en memoria», «El modelo está leyendo {n} tokens de contexto» o «El modelo se está desbordando a RAM (X de Y GB en VRAM): va lento». Lo que Luis vio a la 01:28 (35,3/44 GB, PCIe spill) era la tercera.
5. ~~Descargar modelos sólo llega a Qwen 3.5.~~ Cerrado: catálogo con `qwen3.8` (27b 16,5 GB; 27b-q8_0 27,9 GB) en cabeza y `qwen3-coder-next` (q4_K_M 48,2 GB; q8_0 79 GB); tamaños de los manifiestos de registry.ollama.ai. Comprobado por pantalla.
6. ~~Cargar un modelo desde Modelos locales no enseña nada.~~ Cerrado: el botón pasa a «Loading…» con spinner, los demás Cargar se deshabilitan, aviso de inicio y de fin con segundos. Comprobado por pantalla con `qwen3.8:27b-q4_K_M` (solo, según la regla).
7. ~~Un proyecto con el mismo nombre que su carpeta no se deja crear.~~ Cerrado: sí se dejaba; el mensaje «Folder 'X' already belongs to project 'X'» hacía creer que no. Se refería a la carpeta de chats del panel (que toma el nombre del proyecto). Ahora: «A project called 'X' already exists — open it, or choose another name.» Comprobado por pantalla: creado `Nombreigual` en `…\Nombreigual`; el segundo intento enseña el mensaje nuevo.
8. ~~Personalización de modelos.~~ Cerrado en lo que Ollama permite: en Opciones de cada modelo hay un cuadro **Other options** (JSON) que se envía como `options` en cada petición (`num_batch`, `num_thread`, `min_p`, `top_k`, `repeat_penalty`, `seed`, `stop`, `use_mmap`, `low_vram`…; lista blanca en `src/model_load_options.EXTRA_OPTION_KEYS`). Los flags de llama-server (`-jinja`, `--spec-*`, `--cache-type-*`, `-np`) **no son opciones por petición en Ollama**: el formulario lo dice y el servidor los rechaza nombrándolos. Comprobado por pantalla: `{"-jinja": true}` → error en línea; `{"num_batch": 512, "min_p": 0.05}` → guardado, «+2 options» en la fila, y vuelve al formulario.
9. **Verificar todo por MCP y por pantalla.** Hecho para 2, 3, 5, 6, 7 y 8 en la instancia 7001 con el bundle recién compilado. La research de WAD de Luis **completó de principio a fin en local** (`rp-81c930d5f7eb`, 23:15–23:37: 1.320 s, 4 rondas, 10 consultas, 22 URLs, 11 fuentes leídas, informe de 3.200 palabras en español con citas). Al mirarla de cerca salieron cuatro cosas más, ya corregidas (commits 3fa86d80, 0ce4aaae, a315a822): (a) `llm_call_async` no hace streaming, así que su read timeout es la generación completa: los 180 s de síntesis e informe final caducaban siempre con un 27B local (203 s medidos), se reintentaba y se tiraban los hallazgos de la ronda → ahora el presupuesto de una llamada local es 120 s + max_tokens / `research_local_tokens_per_second` (8); (b) SearXNG sólo tenía a bing respondiendo y bing devuelve los mismos diez resultados para cualquier redacción de «whiplash» (la película, IMDb, Mayo) → motores por defecto `bing,yandex,openalex,mojeek` (yandex trae la CPG de fisioterapia en WAD primero; openalex trae DOIs), y una ronda «todo ya leído» ya no se cuenta como «búsqueda caída»; (c) el informe final es UNA generación de 8.192 tokens y la guía pide ~30 apartados: cubrió 14 y paró en «Movilidad cervical» → a partir de 6 apartados el informe se escribe por partes con la misma evidencia numerada; (d) qwen3.8 devolvió las consultas como objeto `{"query_1": …}` y la ronda 4 murió por «no queries» → el parser lee objetos. El tope de reloj (1.800 s) se multiplica por 3 en endpoints locales (`research_local_time_multiplier`). Segunda pasada (`rp-1e827335af69`, 23:47–00:25): 9 rondas, 25 consultas, 81 URLs, 23 fuentes, 4.449 palabras, 63 % de frases citadas, informe en 2 partes sin ningún timeout. Y la **causa de fondo del corte en «Movilidad cervical»**: `_extract_subquestions` aplanaba las 44 viñetas del brief y **cortaba la lista en 12** — la viñeta 12 es «Movilidad cervical»; fases, pronóstico, errores, algoritmo y tabla nunca se pedían. Ahora el brief se lee como esquema (`_outline_sections`, commit c669c33f): cada encabezado (Tratamiento, Ejercicio y progresión, Pronóstico y seguimiento, Qué evitar, Algoritmo clínico, Tabla final) es UNA sección con sus viñetas dentro y las 7 preguntas finales otra → 15 secciones, iguales con o sin líneas en blanco. Las costuras entre partes (título «Parte 1», numeración en los ## de la parte 2) se limpian en código (d06123b8). **Tercera pasada completa** (`rp-24f96d5f222d`, 00:31–01:15): 5 rondas, 13 consultas, 45 URLs, 16 fuentes, **7.445 palabras, las 15 secciones** con fases (### por fase), pronóstico, errores, algoritmo, tabla final de 8 columnas y las 7 preguntas; 179 marcadores de cita, 52 % de frases citadas. Entregada a Luis como `D:\LocalAI\Guia_WAD_fisioterapia_faustus.md`. Único defecto visto: el modelo pegaba la lista «Título: a; b; c» en los ## → se recorta en código (fdec4914). 

Las tres GPUs son reales: RTX 4070 Ti (12 GB) + dos RTX 5060 Ti (16 GB) = 43,9 GB. La GPU 1 salía sin lectura por un `—` en lugar de «0 MB» cuando está vacía; corregido.

11. **(10-09, 01:20) Calidad de vida y fiabilidad hechas mientras corría la research** (pedido por Luis: «piensa e implementa más QoL y reliability»): (a) `GET /api/search/health` + tarjeta **Salud de la búsqueda** en Ajustes › Búsqueda con botón de prueba: qué motores responden, cuáles están suspendidos y por qué, cuáles callan; borde rojo si un solo motor lo aporta todo; la research avisa una vez en pantalla si va con un solo motor (7351ccc4, bd38760d). (b) La pantalla de Research nunca enviaba `max_time` y la ruta ponía **300 s fijos** → un 27B local hacía una ronda y escribía desde nada (la captura de Luis «Rounds: 2 · URLs Analyzed: 0»); ahora sin fijar = 60 % del reloj de la ejecución (×3 en local) y hay selector «Tiempo para las rondas» (fdec4914). (c) La síntesis de cada ronda reescribía el informe a 16k tokens → tope 6k; la ronda baja de 15 a ~8 min (edff2a32). (d) Discover marcaba `qwen3.8:27b` como no instalado teniendo `27b-q4_K_M` (9f435e34). (e) `test_research_model_load` llegaba al Ollama real por la puerta de VRAM y con un 27B dentro esperaba 10 min; parcheado `admit()`. Comprobado por pantalla: tarjeta de salud (brave 20, yandex 15, bing 10, openalex 10; duckduckgo/startpage CAPTCHA) y el selector de tiempo.

10. ~~(10-09, 00:15) El agente debe preguntar con opciones.~~ Cerrado (59436f6d): la herramienta existía y la pantalla la rompía («[object Object]» en cada botón). Tarjeta con opciones + descripción, checklist si `multi`, y línea libre siempre. Comprobado en vivo. Detalle en OBJETIVOS.md › OBJ-2.

12. **(10-09, 02:00) Segunda tanda de QoL/fiabilidad**: puerta de VRAM en el turno de chat (OBJ-1 cerrado, comprobado en vivo); modo y tiempo de la puerta y los dos ajustes de research local en Ajustes › Sistema; las advertencias de una research (un solo motor, síntesis fallida, páginas ya leídas) se quedan en la tarjeta en vez de pasar por la línea de fase y perderse; aviso rojo en Modelos locales › Cargados cuando hay dos modelos grandes (≥8 GB) residentes a la vez; «Esperando tu permiso» en el resumen de turno de una pregunta ya dice «Esperando tu respuesta».

13. **(10-09, 02:45) El agente pregunta antes de construir — comprobado en vivo y corregido.** Prueba real en la 7001 con `qwen3.8:27b-q4_K_M` y un workspace: «Impleméntame en este proyecto un sistema para guardar las preferencias de cada usuario» → el modelo leyó los cinco ficheros y escribió `preferences.py` (JSON + CLI) sin preguntar nada. La descripción de la herramienta no basta con un modelo local. Ahora las **Base rules** (las efectivas: `src/agent_loop.py` define `_AGENT_RULES` dos veces y la segunda es la que llega al modelo) y el bloque «Workspace coding mode» dicen que un sistema nuevo con varios diseños razonables (dónde viven los datos, lenguaje/framework, dónde va, alcance) se decide con `ask_user` **antes** de escribir nada; una edición pequeña se hace sin preguntar. Repetida la misma petición: «¿Cómo quieres que sea el sistema de preferencias de usuario?» con «Python + JSON (recomendado)», «Node.js + JSON», «Python + SQLite», ningún fichero escrito; «Python» como respuesta produjo `user_preferences.py`; y «Cambia el contenido de saludo.txt para que diga: hola de nuevo» fue `read_file` → `write_file` sin pregunta (573607c0). De paso: esa frase se clasificaba como *low-signal* (`.txt` no contaba como objetivo y «impleméntame» —acento + pronombre— no casaba con ningún verbo), así que el suelo de herramientas de escritura no se enviaba y el turno dependía de que el RAG trajera `write_file` por casualidad; corregidos los verbos con acento/enclítico y las extensiones `.txt/.csv/.tsv/.xml/.svg/.log`.

14. **(10-09, 02:30) Una research sobrevive al reinicio del servidor** (19030635, 486b4a10). Antes vivía sólo en memoria: tras un reinicio la tarjeta decía «La investigación ha fallado» bajo el último mensaje de ronda y una pantalla recargada no la mostraba. Ahora un marcador JSON registra la ejecución desde el primer segundo (running / cancelled / error, sin pisar nunca un informe guardado ni salir en la biblioteca); al arrancar, todo marcador «running» pasa a **interrupted**, `/api/research/active` los lista con su categoría y la pantalla los adopta como tarjeta fallida con **Reintentar** («El servidor se reinició mientras esta investigación estaba en marcha. Reintentar la vuelve a empezar»), y los descarta en el servidor (`POST /api/research/{id}/dismiss`). El seguidor aguanta un corte de red de hasta 60 s (20 × 3 s) en vez de rendirse al primer error. Comprobado en vivo: research arrancada, servidor matado a los 4 s y reiniciado → `active` = interrupted, `status` con la razón, biblioteca sin el marcador, dismiss → vacío, `ollama ps` vacío. También: `assess()` de la puerta de VRAM responde con un solo `/api/ps` cuando el modelo ya está dentro (2b1c7fc5), y el turno de chat dice «Se perdió la conexión con el servidor a mitad del turno…» en vez del «Failed to fetch» del navegador.

## Lo que rompió la máquina el 08-09 (regla, no anécdota)

Dos 27B dentro a la vez —`q8_0` residente de una prueba (33 GB, con spill) y `q4_K_M` cargado por una research (17 GB)— más un build de Vite, dos tandas de pytest y siete procesos de Docker Desktop, superaron el **commit limit** de la máquina (147,7 GB = 128 de RAM + 20 de pagefile). La cascada, en orden: `cudaMalloc failed: out of memory` en la ronda 2, `MemoryError` en el servidor, `can't start new thread`, y después ni PowerShell arrancaba (`0xC000012D`, STATUS_COMMITMENT_LIMIT). El escritorio se quedó en negro con una sola ventana de error.

**La regla: nunca dos modelos grandes cargados a la vez, y nada pesado corriendo mientras hay uno dentro.** `ollama ps` antes de cargar, `ollama stop` del anterior. La puerta de admisión que automatiza esto es OBJ-1 en OBJETIVOS.md.

Las carencias de backend del índice anterior están implementadas; se ha eliminado ese índice vacío.
Las ampliaciones acordadas viven en OBJETIVOS.md; ahora mismo, OBJ-1 (puerta de admisión de VRAM). Eliminados los índices de UI resueltos; se pueden recuperar del historial Git.
No contar planes de inspiración o notas de implementación como otra cola de tareas.

## Spec v2 (cierre lotes 60-72 — 2026-09-11)

Ya no es una rama aparte: `feat/spec-v2-m1` está fusionada a master, y los
lotes 60-70 cierran el resto del paquete P0/P1 auditado en `MAPA_REUTILIZACION.md`
y `MAPA_P1.md`. Estado real tras este cierre:

- **P0** (`docs/spec/v2/MAPA_REUTILIZACION.md`): 99 IDs P0 propios de ese
  fichero, **99 existente, 0 parcial** (PLAN-01 y PLAN-03 se detectaron al
  recontar contra `backlog.json` y los cerró el Lote 71: plan corto/expandible
  y la tarjeta «Quién edita qué» sobre los leases reales de una delegación).
- **P1/P2/LAB** (`docs/spec/v2/MAPA_P1.md`): 84 IDs, **83 existente, 1
  parcial** (HW-06, decisión de producto — ver abajo).
- `FAUSTUS_TOOL_ARG_VALIDATION` nace en `strict`. Si un modelo local empieza
  a ver «INVALID ARGUMENTS» donde antes la herramienta se apañaba, bajar a
  `warn` y anotar la forma que envía para añadirla a la reparación
  (`src/tool_schemas.py::repair_tool_arguments`).
- `question_store` y `chat_outbox` son SQLite propios en `DATA_DIR`; entran
  en el backup por defecto, sin credenciales. Purga: 24 h terminadas / 12 h
  aceptadas (outbox).
- Cerrado en este cierre (ya no son pendientes): el `ask_user` de vivo ya
  manda `revision` (`agent_loop.py:9303`); el selector de modelo marca «not
  installed» cuando el modelo por defecto de una sesión vieja ya no está en
  las rutas (`studio/src/lib/model-label.ts::isInstalled` +
  `ModelPicker.tsx`; visto en vivo en el 7001 que además el chip caía en
  silencio al primer modelo de la lista — desde el Lote 72 la ruta recordada
  se conserva como `missing`, se enseña «not installed» y enviar abre el
  picker en vez de contestar con un modelo que nadie eligió; `/api/models`
  sirve `cached_models` del endpoint, así que un modelo borrado sigue en la
  lista hasta el siguiente refresh); Playwright ya corre por sesión, no con el perfil
  global (WEB-03, Lote 63); `budget_for` ya se integra en
  `_trim_route_request_messages` (CTX-01, Lote 60, `agent_loop.py:5923`);
  EXEC-01/DESK-02 ya tienen UI/journey real (Lotes 65/68).

### Abiertos — lo que sigue genuinamente pendiente

- **HW-06 — decisión de producto.** La primitiva de nodos remotos
  (`src/remote_worker_registry.py`) está construida y probada;
  `DECLARATIONS["remote_worker"].implemented` sigue en `False` a propósito
  porque 4 ficheros de test ajenos afirman `implemented is False`
  explícitamente. Activarlo exige que Luis fije el criterio de
  fiabilidad/soporte bajo el que un nodo remoto pasa a "implementado" — no
  es una tarea de código. QA-27 ya está verde por otra vía (`reconcile`
  nunca reporta éxito falso), así que esto no bloquea nada más.
- **HW-07 (LAB)** — banco Spark+PC+eGPU: exige el rig físico real para
  validar los 5 criterios de aceptación con hardware de verdad; fuera de
  alcance sin ese hardware, por diseño (es lo que "LAB" significa aquí).
- **QA-41 (manual)** — voz física en español e inglés: micrófono/altavoz
  reales, permisos de navegador interactivos. No activar grabación ni
  permisos para cerrar esta casilla sin intervención del usuario.
- **QA-44, hueco 3** — "Escape closes the dialog" flaquea de forma
  intermitente (100%/200% según la corrida) en el entorno sandboxeado
  (Playwright + servidor real bajo carga); sin evidencia de ser un bug de UI
  real y no de temporización del entorno. Los huecos 1 y 2 del mismo
  escenario ya se cerraron en Studio (Lote 65).
- **Higiene de tests (nube)** — algún test escribe `disabled_tools` en el
  `data/settings.json` REAL del clon (no se localizó cuál); con ese fichero
  contaminado `tests/test_browser_mcp_reconnect.py` (3) falla. Borrar el
  fichero y pasan. No afecta a Windows (su settings es el de la app).
- **Docker Desktop / puente MCP en la máquina de Luis** — arranque lanzado a
  través del puente falla con `unable to get 'ProgramData'` (PowerShell
  entregado sin `ProgramData`/`ALLUSERSPROFILE`); cuatro vías probadas,
  ninguna reproducible sin acceso directo a esa máquina. Ver "Comprobaciones
  pendientes" arriba.

## OBJ-4/6/7/8 (11-09-2026)

- OBJ-4 (panel git), OBJ-6 (tablero) y OBJ-7 (lenguaje natural) cerrados
  y verificados en vivo; OBJ-8 tanda 1 hecha (`FAUSTUS.md` §69-71).
- **MOD-05, cableado al turno.** `src/model_router.choose()` existe y se
  puede probar desde Ajustes → Router de modelos, pero el turno de chat
  sigue usando `sess.model` tal cual: cablearlo exige auditar los usos de
  `sess.model` en `routes/chat_routes.py` (caché, métricas, estado de
  sesión). Es el siguiente paso natural de OBJ-8.
- **`endpoint_id` real en `apply_openrouter_payload`.** Los tres sitios de
  `llm_core` que la llaman no conocen el `endpoint_id` (solo url/modelo), así
  que las preferencias por endpoint solo aplican cuando un caller superior lo
  pase; `usage.include` y `cache_control` aplican siempre.
- **Mermaid en Studio.** Se muestra la fuente (copiar/descargar); render
  gráfico solo si se decide añadir la librería.
- **OBJ-5 (nodos remotos por grupos)** aplazado hasta tener un segundo PC.
- Verificación en vivo pendiente en el 7001: merge de rama desde el diálogo
  del panel, tablero kanban, barra lateral nueva, y el tab de Chrome que
  dejó de responder a la extensión durante un streaming largo (¿regresión de
  rendimiento del Studio?).

## Última evidencia

- **11-09-2026, cierre completo (lotes 71-72, suite y pantalla).** Lote 71
  cerró PLAN-01/PLAN-03 (99/99 P0 existente). Suite nube `-m "not slow"`
  tras el cierre: 15.800 correctas, 13 fallos todos preexistentes de entorno
  (docker, marca, markitdown, rutas Windows en Linux, dubbing) o por el
  `data/settings.json` contaminado (ver Abiertos); siete regresiones reales
  detectadas por la suite entera y corregidas antes de transferir
  (middleware con dobles de request sin `scope`/`method`, `NameError` en el
  evento de coste remoto, `reindex` con `paths=` contra dobles antiguos,
  `outline: none` en la caja de búsqueda, el filtro de logs que re-lanzaba
  si `redact_secrets` fallaba, `process` volátil en usage, campo aditivo
  `execution_target` en bash). En vivo en el 7001 (build 982fcf9+): cabecera
  `X-Faustus-Api-Version: 2.0` en todo `/api/*`, 426 comprensible con un
  cliente 0.1.0 salvo en `/api/version` y `/api/health`, Settings → Security
  (perfil de privacidad con ida y vuelta real local_only↔local_preferred,
  concesiones activas, allowlist), búsqueda en la conversación (1/3
  coincidencias), traza por call_id en Activity, «Reubicar» en cada fila de
  Projects, chaos dry-run, coste remoto, leases. Hallazgo: el chip de modelo
  caía en silencio al primer modelo cuando el recordado ya no existía →
  Lote 72.

- **11-09-2026, cierre lotes 60-70 (master cfaa30d).** Los 16 cableados de
  ficheros ajenos pendientes de la integración de los lotes 60-69b se
  aplicaron (revision en `ask_user` en vivo, `execution_target`/
  `command_preview` en tool_output/tool_approvals, `tests_status` en
  `review_state.init`, `duplicate_of`/`stale`/`age_days` en research,
  `bg_jobs.acquire_cpu_heavy` en `DeepResearcher.research()`,
  `privacy_policy.assert_outbound` en OCR/TTS/STT con sus tests-tripwire
  actualizados, VER-04 rechazando dobles literales en el export, verificación
  de artefactos en el export por lotes, umbral de degradación MCP por
  servidor, selector de modelo con chip "not installed", enlace "Ver traza"
  en la tarjeta de tool, estados extracting/ready/partial de adjuntos,
  borrador persistente server-side, fragmento exacto de contexto con botón
  "Ver fragmento", comentario desactualizado en el test de WEB-02
  actualizado). `docs/spec/v2/MAPA_REUTILIZACION.md` y `docs/spec/v2/MAPA_P1.md`
  resincronizados: solo quedan PLAN-01/PLAN-03 (P0) y HW-06 (P1) como
  "parcial", cero "ausente". `docs/spec/v2/QA_ESTADO.md`: 46 verde (QA-10 y
  QA-27 pasaron a verde), 1 xfail (QA-44, solo el hueco 3), 1 manual
  (QA-41); `tests/test_qa_index.py` en verde (50 passed).


- 10-09: Lote 55 — integración de la ola 8 (MEDIA-03/CONN-01/ACT-05
  cableados en `app.py`; UX-06 con `frameBatcher` reusado de PERF-01 +
  recorte defensivo de menciones + lista de adjuntos memoizada) y auditoría
  de los ocho P1 que llevaban desde el Lote 50 sin tocar (siete ya
  implementados por lotes anteriores sin fila en el mapa: MOD-05, IDX-02,
  IDX-03, UX-03, PERF-01, WRITE-02, WRITE-04; solo TASK-05 sigue ausente de
  verdad). `docs/spec/v2/MAPA_P1.md` recontado y corregido: el total real es
  84 IDs P1/P2/LAB, no 87 (cifra que llevaba sin recontar desde el Lote 50).
  `tsc`/`vite build`/`i18n --check` limpios. Suite entera en Windows (11-09, 00:20, `2bf5a45`): **15.234 correctas, 6 fallos** (los 3 preexistentes de siempre —marca, doblaje real, suite_collects— más tres corregidos a continuación: límites de subida troceada en los compose, `docs/api/` como material de ingeniería, y `test_session_image_cleanup`, que pasa en serie: contención de xdist). En la nube (`-m "not slow"`, `e6d15f9`): **15.270 correctas, 13 fallos**, ninguno nuevo (8 preexistentes del entorno + 4 flaky bajo xdist que pasan en serie + el de docs ya corregido).


- 10-09, 09:30 (spec v2, M1): en el 7001 con `qwen3.8:27b-q4_K_M`, sesión
  nueva en modo Agente: «Impleméntame un sistema de notificaciones…» → tarjeta
  ask_user con checklist y `question_id`/ids de opción persistidos; la
  respuesta desde la tarjeta salió con `question_id`+`option_ids`; reenviar
  la misma respuesta → **409 already_answered** sin persistir nada; una
  pregunta inexistente → **409 not_found**. Doble POST simultáneo con el mismo
  `client_message_id` → un solo mensaje de usuario, el segundo con
  `X-Faustus-Idempotent-Replay: 1`. `update_plan` de 12 pasos → tarjeta
  «Plan steps (0/12) · rev 1» igual en vivo y tras recargar, títulos
  renderizados. Diagnóstico: «Version 1.0.3 · build ae30141 · Studio servido:
  6cb8f18857», y ese hash es el `?v=` real del HTML servido. `tsc` limpio,
  i18n `--check` limpio (5.785 cadenas), bundle recompilado.

- 10-09, 02:50: bloque agent_loop/harness/deep_research/research_*/studio_*/vram/chat_vram/agent_runs/local_models/model_load/search_*: **1.270 correctas** (`pytest_final2.txt`, 4:37 min). Nuevas: `test_research_restart_survival.py` (7), `test_studio_research_restart_js.py` (2, con `studio/checks/research-restart.check.mjs`), `test_agent_asks_before_a_new_system.py` (3), `test_workspace_coding_request_spanish.py` (13). `tsc` limpio, i18n `--check` limpio (5.773 cadenas), bundle recompilado, 7001 reiniciado con todo, `ollama ps` vacío al terminar.

- 10-09, 02:00: `tests/test_chat_vram_gate.py` (8), `test_studio_ask_user_options_js.py` (3), `test_studio_vram_live_js.py` (2) nuevos; bloque agent_runs/chat/ask_user/studio: **76 correctas**; guards **11**. `tsc` limpio, i18n `--check` limpio (5.767 cadenas).

- 10-09, 01:15: bloque deep_research/research_*/search_*/local_models/model_load_options/vram_admission: **602 correctas** (`pytest_final.txt`). `tsc` limpio, i18n `--check` limpio (5.745 cadenas), bundle recompilado, 7001 reiniciado con todo.

- 09-09, 23:45: nuevas pruebas `test_deep_research_local_call_budget.py` (4), `test_deep_research_same_pages_again.py` (2), `test_deep_research_report_in_parts.py` (4), 4 casos más en `test_deep_research_parse_json_array_echo.py`; research/report/model_load/probe/service: **60 correctas**; búsqueda/report/fallback: **46 correctas**.

- 09-09, 23:05: `pytest` sobre local-models, model_load_options, projects, llm_core (ollama/streaming), deep_research y research_*: **561 correctas**. Nuevas: `tests/test_model_load_options_extra.py` (6), `tests/test_deep_research_empty_run_is_a_failure.py` (3), `tests/test_vram_admission.py` (14), `tests/test_research_model_load.py` (5), `tests/test_search_appliance.py` (7). `tsc` limpio, `scripts/i18n_es.py --check` limpio (5.730 cadenas), `build-studio --force` correcto. Instancia 7001 reiniciada con el bundle nuevo; `ollama ps` vacío al terminar.

- Cierre creativo/escritorio del 08-09: suite completa **13.236 correctas, 81 omitidas, cero fallos** (`logs/checkpoint-creative-full.xml`). Correcciones posteriores: **89 regresiones correctas**; transcripción y narración reales offline EN/ES: **4 correctas**. Adaptaciones de Diogenes: **471 regresiones correctas** (`logs/checkpoint-diogenes-regression.log`). Ventanas principal y secundaria, controles personalizados y propiedad del servidor comprobados en Electron real.

- 08-09: lanzadores web del repositorio probados con parada, arranque y reutilización sin duplicado. Ventana Electron real: minimizar, maximizar/restaurar, pantalla completa y cierre comprobados; cierra su backend propio y conserva el servidor web compartido. Barra integrada en temas y disponible también en el acceso. Regresiones de tareas/contexto: **68 correctas**; bloque creativo: **36 correctas**, incluido doblaje real local EN/ES; rutas de vídeo: **3 correctas**; controles de interfaz/escritorio/zonas horarias: **25 correctas**. TypeScript, scripts frontend, compilación y portfolio/CV correctos.

- Clientes oficiales en Brave (08-09, 02:07): Codex y Claude responden con sus sesiones de suscripción; Claude delega a un worker Codex, con resultado registrado, aprobación y cero cambios de archivos. Corregidos esquemas MCP incompatibles y descripciones ausentes de herramientas textuales. Bloque de **234 pruebas correctas**, controles de transporte de memoria y etiquetas de modelo, TypeScript y compilación correctos.

- Suite completa: **13.183 correctas, 81 omitidas y cero fallos** (`logs/checkpoint-context-client-full-20260908.xml`). Los cambios posteriores de edición/artefactos pasan **71 pruebas de regresión** (`logs/checkpoint-final-media.log`). Todos los scripts frontend, TypeScript y compilación correctos.
- Brave: Claude responde con skills automáticas desactivadas y presupuesto por turno; corregido el bloqueo de limpieza temporal de Windows. Editor→chat adjunta una copia sin enviar, conserva el borrador de capas/máscaras y abre adjuntos existentes. Compositor móvil corregido para mantener enviar/parar visible. Procedencia de artefactos comprobada con datos sintéticos, estados parciales y fallo recuperable de red.
- Navegador Brave y Qwen local: objetivo OBJ-1 creado en proyecto sin carpeta, conservado tras recarga; trabajo continuado fuera del chat. Respuesta inglesa tras herramienta y texto entre rondas completos tanto en vivo como tras recargar (08-09, 01:15).
- Objetivos y aislamiento: **225 correctas** (`logs/astra-objective-scope-focused.xml`).
- Integración: **203 correctas** (`logs/astra-objective-scope-integration.xml`).
- Servidor reiniciado el 08-09: HTTP 200.
- TypeScript y compilación de producción correctos. Separada la caché de React sin adelantar la carga del editor; ya no aparece el aviso de tamaño del bundle.
