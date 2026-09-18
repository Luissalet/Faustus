# Pendientes de cierre

Actualizado: 18-09-2026. REGLA: nunca nombres de empresas/personas del buzón de Luis en commits, docs, tests ni comentarios — ejemplos siempre ficticios. Sólo trabajo vigente; quitar cada entrada al cerrarla.

## 18-09 (renombrado interno completo y traslado a D:\LocalAI\faustus — FAUSTUS.md §112)
- Revisar: `studio/src/shell/notifications-tray.tsx` + `notifications.ts` están implementados y con backend (`routes/notifications_routes.py`) pero no se montan en `AppShell.tsx`. Decidir: cablear o borrar.
- Revisar: los nombres de colecciones vectoriales y las columnas `odysseus_kind`/`odysseus_ref` siguen con el prefijo antiguo a propósito; si algún día se hace una migración de datos, es el momento de renombrarlos.
- Revisar: `origin/dev` sigue existiendo en GitHub y `origin/HEAD` apunta a él en el clon; si la rama remota ya no sirve, borrarla (push de Luis).
- Comprobar en el PC: los dos servidores MCP «Hoard» fallan al conectar con WinError 2 desde el 17-09 (antes del renombrado): la ruta del comando ya no existe.

## 18-09 (bloque MCP del prompt recortado a lo seleccionado — FAUSTUS.md §110)

- Todo lo de este lote está probado con un `McpManager` de mentira (3 servidores × 20 tools de prueba) y con `src.settings.get_setting` parcheado — nunca contra los ~10 servidores MCP reales de la máquina de Luis. Falta, en vivo: (1) mandar «hola» y mirar el ledger de contexto (o los logs) para confirmar que la sección `mcp` bajó de los 14.657 tokens confirmados en vivo a algo cercano al tope de 1500 (o a 0 si nada se seleccionó ese turno); (2) un turno que sí necesita un tool MCP concreto (p. ej. un servidor de archivos o de navegador) sigue pudiendo llamarlo sin fricción, con su esquema nativo intacto — no solo en test; (3) `lookup_tools` con el nombre de un tool MCP que no estuviera en el conjunto seleccionado de ese turno, y confirmar que lo sigue encontrando y cargando pese al recorte del prompt; (4) activar `agent_mcp_prompt_full_listing` desde Ajustes y confirmar que el volcado completo de siempre vuelve tal cual estaba, por si alguien lo necesita para depurar un servidor MCP concreto.
- No se ha medido el efecto real sobre un modelo local pequeño (el síntoma original: contestaba con el separador de contexto o entraba en bucle con el volcado de 14.657 tokens presente) — falta una sesión en vivo con qwen3 o similar confirmando que ya no ocurre.
- El bloque de integraciones usa el mismo criterio (nombres solo, salvo que `api_call` esté en el conjunto seleccionado) pero solo se ha probado con integraciones de mentira; falta confirmar en vivo que con integraciones reales configuradas (Gitea, Linkding, Home Assistant…) el agente sigue sabiendo qué endpoint pedir cuando `api_call` sí está seleccionado.

## 18-09 (el modelo por defecto no se descarga — FAUSTUS.md §109)

- Todo lo de este lote está probado con `/api/ps`/`/api/generate` y reservas/tickets de `vram_admission` simulados, nunca contra el Ollama real de la máquina de Luis. Falta, en vivo: (1) dejar Faustus corriendo un buen rato con la otra app local activa (la que pisaba el `keep_alive` a 5 min) y confirmar con `ollama ps`/`GET /api/local-models` que el modelo por defecto nunca queda fuera de VRAM más de un ciclo del guardián (~20 s ahora, antes 120 s); (2) varios reinicios seguidos de Faustus y confirmar que cada uno recarga el modelo por defecto sin pisar una carga de otro modelo en curso; (3) mirar la pantalla Ajustes → Local models y confirmar que la fila del modelo por defecto dice «Kept loaded by Faustus (default model)» y no el «kept loaded» genérico de antes; (4) probar el botón «Unload» explícito de esa pantalla contra el modelo por defecto — debe descargarlo igual (la protección nueva es solo contra los caminos automáticos/silenciosos) y el guardián debe recargarlo en el siguiente ciclo; (5) una pregunta del móvil llegada justo en el hueco entre una caída y el siguiente ciclo de 20 s: confirmar que no se queda sin respuesta (el propio turno debería disparar su carga vía `admit()` normal, no depender solo del guardián).
- No se ha confirmado en vivo si `src/vram_admission.py::_evict`/`restore_keep_alive` eran de verdad la tercera causa sospechada de la desaparición del modelo (junto al reinicio de Faustus y al `keep_alive` de la otra app) — el código ya protegía la mayoría de los caminos automáticos antes de este lote (pin + `_protected` en `assess()`); este lote cierra el hueco que quedaba en el modo `auto` de `admit()` y en `admit_bytes`/`_ollama_suggestion_candidates`, pero sin una sesión en vivo con las tres causas a la vez no se puede confirmar cuál pesaba más.
- `OLLAMA_KEEP_ALIVE=-1` como variable de entorno del servicio Ollama (cinturón y tirantes, documentado en FAUSTUS.md §109) no se ha aplicado en la máquina real — decisión de Luis, fuera del alcance de este repositorio.

## 18-09 noche (corrección: el modelo por defecto ya cede el sitio solo — FAUSTUS.md §109 «Corrección»)

- Regresión vista en vivo la misma noche del lote X-B: al elegir un modelo local más grande que no cabía junto al por defecto (29,4 GB necesarios, 23,1 GB libres), la tarjeta salía con «0 models loaded — tick what to unload» porque `_ollama_suggestion_candidates` excluía al por defecto del todo. Corregido: el por defecto cede solo (sin tarjeta) en todos los modos cuando eso basta; solo si no basta aparece en la tarjeta, ahora etiquetado. Falta, en vivo, contra el Ollama real: (1) reproducir el caso exacto (el quant grande de esa noche) y confirmar la línea «X steps aside for Y» en vez de la tarjeta vacía; (2) confirmar que el por defecto vuelve solo a los ~10 minutos de inactividad del otro modelo (`warm_default_model_yield_minutes`), o antes si se descarga; (3) confirmar que un modelo de embeddings activo (RAG) no retrasa la vuelta del por defecto; (4) forzar el caso insuficiente y confirmar la fila «default — kept loaded by Faustus» ticable en la tarjeta real.
- Todo lo de esta corrección está probado con `/api/ps` y `assess()`/`last_active_seconds` simulados — nunca contra el Ollama real ni la heurística de nombre de embeddings contra un modelo de embeddings real instalado en la máquina (`nomic-embed-text` u otro).

## 18-09 (sin `ctx_ack` ni bucles de ceros, + escalera de recuperación — FAUSTUS.md §108)

- Todo lo de este lote está probado con streams SSE enlatados (mismo patrón que el resto de `tests/test_agent_loop*.py`), nunca contra un Ollama real. Falta, contra el 27B que produjo los dos bugs originales: (1) confirmar que con `repeat_penalty 1.05`/`min_p 0.05` de serie ya no aparece «0000…» en una sesión recién abierta con el prompt exacto que lo disparó (PENDIENTES §104: «El modelo local 27B degeneró en un turno de prueba»); (2) confirmar en pantalla que con memoria recuperada grande (~14k tokens, el caso descrito en §107) ya no responde solo `<<faustus_ctx_ack>>`, y que si lo hace varias veces seguidas la escalera de recuperación saca una respuesta útil en vez de terminar en error; (3) vigilar si fundir el contexto no fiable y la pregunta real en un solo mensaje (`--- Your message ---`) cambia el comportamiento del modelo de alguna forma no prevista — p. ej. que empiece a citar o comentar la etiqueta del envoltorio, cosa que antes no podía pasar porque quedaban en mensajes separados.
- **Escalera de recuperación (cambio de requisito del dueño, mismo día: nunca terminar en error con un modelo cargado).** El escalón 3 depende de que Ajustes → Modelos → Endpoint de utilidad tenga configurado un endpoint DISTINTO del principal (`src.endpoint_resolver.resolve_endpoint("utility", …)`); si no lo tiene, la escalera llega al escalón 4 (error) un paso antes de lo posible — confirmar en la máquina real que ese endpoint existe, responde, y que normalmente ya está cargado (para que el escalón 3 sea rápido). No se ha visto en pantalla el evento `harness_check` `status: "recovery"` (¿Studio ya lo muestra como "Recuperando…", o hace falta cablear el frontend?) ni el tiempo real que tarda la escalera completa (hasta 3 llamadas más al modelo). El mensaje final cuando los cuatro escalones fallan («The model looped; try rephrasing or another model») tampoco se ha visto renderizado, solo comprobado contra el evento `agent_terminal` en un test.
- `local_repeat_penalty_default`/`local_min_p_default` son ajustes nuevos sin campo propio en Ajustes → Modelos locales (no llevan prefijo `agent_`/`browser_`/`desktop_`, así que tampoco entran en el esquema de `agent_settings_schema.py`); hoy solo se pueden cambiar editando `settings.json` a mano o vía `PATCH /api/auth/settings` directo. Si conviene exponerlos en la UI, hace falta decidir dónde encajan (¿junto a las opciones por modelo, o un ajuste global?).

## 18-09 madrugada — verificado en vivo tras w110/w111 (§108-§110)

- «hola» en modo agente: prompt de 19.341 → **5.184 tokens** (el bloque «MCP tools» pasó de 14.657 a 500); primer token en 4,6 s (antes 15 s). Respuesta normal, sin `<<faustus_ctx_ack>>`.
- Encargo real por chat («guarda esta oferta en el tracker de candidaturas, arráncalo si hace falta»): Faustus abrió la página con el navegador, pidió la aprobación de seguridad (una sola tarjeta: contexto externo → escritura en conector; respondida con «Allow for this task»), extrajo título y empresa, llamó `capture_job` y confirmó. Antes de w110 el mismo encargo acababa en ceros o en el marcador. Queda: probar la escalera de recuperación con un caso real que degenere (no se ha reproducido tras el cambio) y que Studio muestre «Recovering…».
- El modelo por defecto no se ha descargado en toda la pasada (`ollama ps` → Forever); pendiente ver el keeper actuar cuando el tracker de candidaturas vuelva a usar Ollama con su keep_alive de 5 min.

## 18-09 (lote S: búsqueda automática en preguntas de actualidad + favicons — FAUSTUS.md §107)

- Verificado en vivo (w107): «¿Ganó el Madrid su último partido?» busca sola y responde; ver FAUSTUS.md §107 «Pasada en vivo». Pendiente: un POST directo a `/api/chat_stream` (cookie + JSON) desde un script se queda parado tras «[doc-inject] no active doc…» sin entrar en el bucle del agente, mientras que el relé móvil funciona — averiguar qué espera ese camino (¿handshake del cliente?, ¿cola «waiting for idle»?). Y la admisión de VRAM en modo «ask» bloquea a cualquier llamador sin UI (la batería se quedó esperando la tarjeta cuando el 27B se había descargado): para peticiones por API debería decidir sola o fallar rápido.
- Otra app que use el mismo Ollama (p. ej. el tracker de candidaturas) manda su `keep_alive` por defecto (5 m) y despina el 27B; el warmup ahora re-pinea cada 2 min, pero entre medias puede caer. Alternativa: `OLLAMA_KEEP_ALIVE=-1` en el entorno del servicio Ollama.
- Un turno cuya salida es solo `<<faustus_ctx_ack>>` (visto con memoria recuperada como contexto no fiable en una pregunta simple) ya recibe un nudge; falta mirar por qué el 27B lo emite tan a menudo con contexto recuperado grande (~14k tokens) y si conviene recortar esa recuperación.
- `_fetch_favicon_bytes` en `routes/favicon_routes.py` nunca hizo una petición HTTPS real a un `favicon.ico`/`<link rel=icon>` de verdad — cubierto con `fetch` mockeado. Confirmar en vivo que el flujo de fallback (favicon.ico → homepage → placeholder) funciona contra dos o tres dominios reales conocidos.
- El aviso de una línea («This question is time-sensitive…») se añade como mensaje `role: system` justo antes del bucle de rondas; falta comprobar en un chat real que un modelo local (p. ej. qwen3.5) lo respeta y busca en la primera ronda en vez de ignorarlo.
- `_DOMAIN_RULES["web"]` reforzado con la instrucción de buscar sin pedir permiso — no se ha medido si esto hace que el modelo busque de más en preguntas límite (p. ej. «¿qué opinas del último modelo de IA?», que mezcla opinión con actualidad). Vigilar falsos positivos del heurístico en uso real más allá de los 44 casos de `tests/test_freshness.py`.

## 18-09 (lote T: Piper, proveedores de comando, frases de parada — FAUSTUS.md §106)

- Piper verificado con motor y voz reales en el scratchpad de la sesión (no en el repo ni en la máquina de Luis): falta la pasada en vivo desde Ajustes → Voz en la instancia real (botón «Instalar motor», descarga de una voz, síntesis desde el panel), y confirmar que el binario de Windows (`piper_windows_amd64.zip`) instala igual de bien — solo se probó el de Linux.
- Los proveedores «comando» se probaron con un script Python de usar y tirar, nunca con un ejecutable de terceros real — el operador que los use es quien primero validará una plantilla concreta.
- Frases de parada: el textarea nuevo en Ajustes → Voz no se ha visto en el navegador (sin sesión admin en este entorno para llegar a Ajustes en vivo); la lógica está cubierta por `voice-jarvis.check.mjs` y por `capabilities()` devolviendo `stop_phrases`.

## Noche del 17-09 (voz manos libres, FAUSTUS.md §105)

- Sin micrófono en el entorno: probar en vivo la interrupción (¿se pierde la primera palabra?), la guarda de eco (¿se traga una réplica rápida como «sí»?), las frases de parada y qué transcribe Whisper cuando se dice «Faustus» (ampliar la lista de variantes en `engine.ts::stripWakeWord` si hace falta).
- Latencia «oído en»: si molesta, el siguiente paso es STT en streaming por WebSocket con parciales (decodificar cada 300-500 ms sobre ventana deslizante) en vez de esperar al silencio y subir el clip entero.
- Voz de salida: las voces de Windows suenan a Windows. Opción local con más calidad: instalar Kokoro (ya hay proveedor «Local (Kokoro)» en Ajustes → Voz) o el nuevo proveedor Piper (FAUSTUS.md §106) — instalar el motor y descargar una voz desde Ajustes → Voz.
- Voz desde el móvil: el bucle es el mismo (HTTPS por la VPN de malla), pero la pestaña tiene que estar en primer plano; la palabra de activación no funciona con la pantalla apagada.

## Noche del 17-09 (móvil, lotes P-A/P-B — PWA instalable + push, ver `docs/api/mobile.md`, `docs/ui/pwa.md`, FAUSTUS.md §104)

- Verificado en vivo en :7000 (w100): `/sw.js` con `Service-Worker-Allowed: /` y el worker activo con ámbito `/`; `/manifest.webmanifest`; «Instalar Faustus» aparece (el navegador acepta el manifest); permiso de notificación concedido desde el diálogo del navegador; vista móvil a 500 px con cinco pestañas inferiores y sin desbordamiento horizontal; un turno enviado desde esa vista emite `turn_finished` en el bus.
- **Pendiente de verdad**: recibir un push real. El navegador de escritorio con el que se probó trae desactivado su servicio de push (`AbortError: Registration failed - push service error` al suscribirse). Ahora la sección muestra ese error de forma persistente y explica qué hacer; la prueba completa (suscribir → `Enviar notificación de prueba` → notificación con la pestaña cerrada) hay que hacerla desde el móvil o desde un navegador con el servicio de push activo, con el servidor expuesto por HTTPS (VPN de malla + `serve` del 443 al 7000).
- `pushsubscriptionchange` en `static/sw.js` nunca se ha disparado de verdad (el navegador lo dispara raramente, cuando el servicio de push rota las claves de una suscripción); su lógica de re-suscripción está escrita según la spec pero sin caso de prueba real que la dispare.
- El modelo local 27B degeneró en un turno de prueba («0000…», `prediction aborted, token repeat limit reached`) con un prompt trivial. No es de este lote, pero conviene mirar `repeat_penalty`/opciones por defecto del endpoint local.

## Noche del 17-09 (móvil, lote M-A — servidor, ver `docs/api/mobile.md`)

- Servidor listo y probado (`tests/test_notifications.py`, `tests/test_mobile_routes.py`, 34 tests): bus de eventos (`src/notifications.py`), `/api/mobile/*` (bootstrap, notifications, sessions, messages, WS, shim de envío) y los cuatro enganches (fin de turno, aprobaciones, tareas, recordatorio). Falta el lote M-B (`mobile/android/**`, `scripts/mobile_*.py`) — otro agente lo estaba haciendo en paralelo; una vez esté, probar el emparejamiento real desde un móvil (QR → `GET /api/mobile/bootstrap` → WS) y no solo con pytest.
- Dos huecos del contrato original que NO eran ciertos y quedaron corregidos en el propio código (no son deuda, ya están resueltos, pero vale la pena que quien mire el histórico lo sepa): (a) `require_admin` no aceptaba un token bearer de un usuario real (siempre pisa `current_user="api"`) — `_mobile_owner` en `routes/mobile_routes.py` lo soluciona; (b) el token de emparejamiento solo tiene el scope `chat`, no `sessions`, así que `POST /api/chat_stream` directo le daba 403 — de ahí el shim `POST /api/mobile/session/{sid}/send` (loopback interno con impersonación de owner, no duplica la lógica de streaming). `core/authz.py` ganó una regla nueva (`/api/mobile/*` → scope `chat`) sin la cual NINGÚN endpoint de este lote sería alcanzable con un token bearer — quien toque `core/authz.py` en el futuro debe saber que el test `tests/test_auth1_token_matrix.py::test_the_reachable_surface_is_exactly_this` fija la superficie completa a mano.
- `turn_error` está declarado en `src/notifications.KINDS` pero sin enganche — ningún punto de `chat_routes.py` mapea tan limpio a "una sola llamada, una vez por fallo" como `save_assistant_response` para el éxito. Si se quiere, revisar `_safe_stream()`/los branches de error alrededor de la línea 4148 de `routes/chat_routes.py`.
- El WS (`GET /api/mobile/ws`) no reenvía histórico al conectar (solo `hello.last_id`); un cliente que reconecta debe pedir `GET /api/mobile/notifications?since_id=` una vez y luego fiarse del socket. Documentado en `docs/api/mobile.md`, pero el lote M-B tiene que implementarlo así — no asumir que el WS manda lo perdido.
- No se ha probado en vivo (navegador/MCP) porque no hay móvil ni build Android en este lote — verificado con pytest (34 tests nuevos + 221 existentes de alrededor sin romper) y arrancando `app.py` completo en memoria para confirmar que `/api/mobile/*` queda registrado junto a `/api/chat_stream`.

## Noche del 17-09 (WhatsApp — FAUSTUS.md §100)

- Emparejado en vivo (17-09 ~17:40). La sesión actual se vinculó antes del arreglo del historial: para bajar todo el pasado de golpe hay que Unlink → Start → escanear otra vez; si no, «Load older messages» chat a chat.
- Tercera ola (respuestas citadas, reacciones, reenvío, edición/borrado, ticks, presencia, búsqueda, adjuntos, notas de voz grabadas, Ask Faustus) desplegada como w85: **falta la pasada en vivo** — sobre todo el permiso de micrófono de MediaRecorder en la app de escritorio, los popovers de reaccionar/reenviar, el salto a un mensaje citado que no está en la ventana cargada, y el 409 de citar/reenviar un mensaje anterior al arranque del puente (el mapa crudo vive en memoria).
- Un contacto que solo se conoce por LID (nunca llegó `sender_pn` ni contacto sincronizado) sale sin teléfono; se resuelve al re-emparejar (los contactos traen `lid`+`phoneNumber`) o cuando escribe.
- Hasta w88 cada reinicio de Faustus mataba el puente (y los perfiles de lanzamiento): `serve` terminaba a todos los descendientes. Ahora `spawn_detached` apunta los hijos en `data/runtime/detached.json` y el apagado los respeta — comprobar en el siguiente reinicio que el puente sigue vivo y que Jobhunter no hay que relanzarlo.
- Mensajes propios enviados desde el móvil mientras el puente estaba parado fallan al descifrar (`MessageCounterError`) y llegan por reintento; no es un bug nuestro.
- Ideas siguientes: reglas automáticas («si escribe X avísame por…»), crear grupos/participantes, estados, transcripción en segundo plano de audios largos, y que `/chats` sirva `archived` fiable sin re-emparejar (hoy solo llega con el historial inicial o `chats.update`).

## Noche del 17-09 (vigilantes y tarjetas de Inicio — FAUSTUS.md §99)

- Verificado en vivo: tiempo (desde el chat), correo, noticias y vigilancia de texto (por API), tarjetas en Inicio con Refresh. Queda probar el caso real de reposición en una tienda (el modelo pedirá la URL): si la tienda pinta el stock solo con JavaScript, `watch_page` dirá «sin señal clara» — entonces habría que capturar con el navegador integrado.
- El modelo eligió `when: today` para «mañana» al programarlo a las 8:00 (razonable, pero no literal): valorar que la tarjeta muestre hoy y mañana a la vez.
- `watch_page` decide disponibilidad por palabras («Add to cart», «Agotado», «Notify me»…); una tienda con render solo por JavaScript no muestra esas palabras en el HTML — en ese caso valorar el navegador integrado para la captura.
- El briefing de noticias depende del proveedor de búsqueda configurado (SearXNG por defecto): si no está levantado, la tarjeta dirá «search failed».
- Ideas siguientes (no hechas): tarjeta de calendario del día, tarjeta de «candidaturas» (Jobhunter: pendientes/entrevistas), precios (vigilar un número en la página y avisar por debajo de un umbral), RSS por URL de feed, y elegir tamaño/orden de las tarjetas arrastrando.

## Tarde del 17-09 (candidaturas por correo — FAUSTUS.md §98)

- `review_candidature_mail` verificado en vivo tres veces (rechazos, entrevistas, ambas): Jobhunter y calendario correctos e idempotentes. Desde que se leen todos los correos no-bulk de la ventana (no solo los de asunto «candidatura»), la entrevista con confirmación de hora también sale; queda valorar un evento de día completo para entrevistas a demanda con fecha límite («complete by …»); y el modelo dijo «en menos de una hora» de una entrevista ya pasada (13:00 vs 15:07): revisar cómo llega la hora actual al prompt del 27B.
- La receta `review-candidature-responses` decía «nunca crear candidaturas»; la herramienta las crea con `create_missing` (por defecto) porque es lo que Luis pidió. Actualizar el texto de la receta/`docs/api/candidature_recipe.md` para que lo diga.
- El 27B con `num_ctx` 199.680 iba a 2 tok/s con el diálogo de VRAM en cada turno; ahora 65.536. Si Luis quiere más, hay que medir la KV cache primero (la admisión dice «never measured»).
- La UI muestra «Loading the model into memory · No server signal» mientras una herramienta larga (2–3 min leyendo correos) trabaja: el estado debería decir «Running review_candidature_mail».

## Noche del 17-09 (Apps — FAUSTUS.md §101)

- Modelo por defecto precargado al arrancar (`src/model_warmup.py`, ajustes `warm_default_model*`): comprobar en el 7000 que tras el reinicio `ollama ps` muestra el 27B con expiración «Forever» y que sigue cargado tras un chat (el re-pin cada 10 min debe devolverlo a -1).

- Perfiles reales en el 7000: Dorian's (python de su venv, `-m selfhoard`, stop por su `scripts/stop.ps1`), Gepetto's (su propio Electron: `node_modules/electron/dist/electron.exe desktop/main.cjs`, que levanta el servidor 8767; `desktop=false` porque ya es ventana), Plato's (`app.py`, 5000), más Jobhunter's y Writer's con icono. Conectores `dorian` (credencial creada con `scripts/connect_faustus.py` en su repo) y `platos` conectados; `gepetto` creado pero sin probar `connect` con la app arriba (el OpenAPI de FastAPI debería dar más tools que el manifiesto).
- Un terminal de Windows quedó abierto de un arranque anterior al arreglo de `CREATE_NO_WINDOW`; cerrarlo a mano. Comprobar en el próximo Start que no aparece ninguno.
- `GET /api/launch-profiles/status` tarda ~2 s (la tabla de puertos/procesos); si molesta en la pantalla, cachear la tabla 2–3 s en `process_center`.
- La extensión de Chrome no acierta a pulsar Start en la tarjeta (un `click()` por JS sí): mirar si la rejilla desplaza el botón durante el polling de 5 s.
- Ideas: autostart de perfiles al arrancar Faustus, grupos por proyecto, importar/exportar perfiles, herramienta de solo lectura para el agente, consola en vivo por SSE en vez de cola de log.

## Tarde del 17-09 (centro de control — FAUSTUS.md §97)

- `/processes` en el Studio: falta cruzar los hijos MCP con la tabla `McpServer` para mostrar el nombre del servidor en vez de la línea de comandos, y decidir si el agente recibe una herramienta de **solo lectura** sobre la lista (nunca el Stop).
- Las apps que el asistente abre por Windows-MCP (Cursor, ChatGPT) salen en «Other apps» por nombre; no hay forma de saber quién las abrió. Si hace falta, el asistente puede anotar en Faustus lo que lanza (`POST` a un registro) — no hecho.

## Mediodía del 17-09 (conectores — FAUSTUS.md §96)

- Conectores de Luis dados de alta en el 7000: Jobhunter's Hoard (:5178, 15 tools, perfil `node server/index.js` con `PORT=5178`) y Writer's Hoard (:8766, 126 tools, perfil `open_exe` sobre `release/win-unpacked/Writers Hoard.exe`). Falta probar en vivo «seguir a la app»: arrancar un segundo Jobhunter (caería en 5179), parar el de 5178 y pulsar Check — el conector debe mudarse y decir «Followed the app from … to …».
- Un `stop` del servidor 7000 mata también las apps lanzadas desde un perfil (hijas del proceso): Writer's Hoard se cerró al reiniciar el 7000. Valorar lanzar los perfiles desacoplados (`CREATE_NEW_PROCESS_GROUP` + `DETACHED_PROCESS`) para que sobrevivan a un reinicio de Faustus.
- La app de escritorio sigue sin devtools ni menú: para diagnosticar otra pantalla en blanco vale `Ctrl+Shift+I` (el menú por defecto de Electron sigue activo aunque no se vea).

## Madrugada del 17-09 (harness para implementaciones largas — FAUSTUS.md §95, OBJ-16)

### Qué mirar en el 7001

- **Repetido el chat #14 (17-09, 03:00): OK.** Plan de 4 WP + «Sigue implementando el plan» con el 27B: tracker parseado, 4/4 implementados con tests reales, `ui_smoke` cazó un 404 real de assets que los tests no veían y la ronda de arreglo lo cerró. Tres fallos del harness corregidos sobre la marcha (FAUSTUS.md §95, «Verificado en vivo»). Repetido también el chat #15 (segundo chat, mismo adjunto): 4/4 cerradas con `plan_done` y evidencia, 0 mutaciones. Queda por repetir con un plan GRANDE (≥ 60 KB, 20+ tareas) y en un segundo chat SIN adjunto («Continua»).
- **`ui_smoke` con Flask real en Windows: OK** (`flask run --port`, `app.mjs` → `text/javascript`, `style.css` → `text/css`, Playwright sin errores de consola). Falta verlo con un proyecto FastAPI y con `npm start`.
- **Puerta dura.** Un turno con un test rojo nuevo: la tarjeta `verified` trae `gate: tests_failed` y el resumen `complete_unverified` («Not sealed as complete» en el Studio).
- **Deriva de dependencias.** Quitar `shapely` del venv del proyecto y abrir un turno: la nota de sistema con «falta shapely — instálala con install_dependencies» tiene que llegar antes de cualquier bash.

### Limitaciones honestas

- Turno de SOLO verificación sobre un plan ya hecho (chat #15 controlado): `check_completion` rechazó dos veces («creado» sobre ficheros solo leídos → correcto; «el servicio está listo» → `claims_without_mutation`, discutible). Afinar: cuando el tracker cierra 4/4 con evidencia y no hay claims de cambio, un «verificado, sin cambios» no debería costar una ronda.
- La puerta dura solo impide sellar `complete`; no abre una ronda de arreglo propia (las de tests/smoke/review siguen siendo las que arreglan).
- `plan_done` informa de los ficheros de la tarea que el turno no tocó, pero no ejecuta criterios tipados; el `Goal` de WP27 (`test_passes`, `http_ok`) aún no está enganchado al tracker.
- El parser de planes es heurístico (encabezados/listas/checkboxes): un plan en prosa pura da 0 tareas y cae al recorte a TOC de §90.
- `delegation_receipts` reintenta una vez con orden corta; si el segundo intento vuelve vacío, el padre recibe el aviso explícito y sigue — no hay tercer intento.

## Noche del 16-09 (auditoría Cursor, paridad 36/36, Reach, Creator fase 1-2)

Contexto completo en FAUSTUS.md §90-94 y OBJETIVOS.md OBJ-11/13/14/15.

### Qué mirar en el 7001

- **Creator con el flag.** `creator_enabled` sigue OFF por defecto en `src/settings.py`. Para ver algo hay que activarlo a mano y comprobar por pantalla: `/creator` (WP05), la biblioteca (WP03), un preflight real contra un motor instalado (WP09) y el lifecycle de un plugin (WP32) de principio a fin — nada de esto se ha probado con navegador real en este lote, solo con TestClient/pytest.
- **Personas.** `GET /api/personas` y el render de sistema (`render_system_block`) están probados por HTTP; falta comprobar en el Studio que un AGENT.md con `persona: security-auditor` realmente antepone el bloque al prompt en un turno de verdad, no solo en el test unitario de `agent_defs.py`.
- **Reach.** `reach_doctor` con `live=1` no se ha corrido contra la red real en esta sesión — solo contra `httpx.MockTransport`. Antes de dar los 9 canales por buenos en producción, correr `GET /api/reach/doctor?live=1` una vez con las credenciales que haya configuradas y mirar cuántos de los 9 responden `ready` de verdad.

### Los fallos heredados de la suite

Pasada del 17-09 sobre `89740d02` (+ arreglos): **18.946 verdes**; 46 fallos listados, de los que 7 eran nuevos y se corrigieron en el mismo día (`test_l62_edit03_bom_full_overwrite` ×4 por un contextvar que `test_H45_wiring` dejaba puesto; `test_studio_guards` ×2 por colores literales de WP13/WP20; `test_worker_refusals` por el reintento H3 sobre un worker con todas las llamadas rechazadas), 2 son de carga (`test_p1_eval_02_ablation` con un worker xdist caído, `test_browser_mcp_reconnect` pasa en solitario) y el resto son los heredados de abajo. Con 8 GB de RAM la suite tumba un worker y un `playwright-mcp` colgado deja al controlador esperando: correr con `-v --max-worker-restart=6` y leer los FAILED del log. La pasada anterior (16-09, `6c5bfd7`-`3a7d4a3`) daba **17.951 verdes / 38 fallos**, ninguno introducido ese día: los mismos ids fallan en una worktree de `bce096a8` con la misma carpeta (`comm` de las dos listas). Ficheros (por número de tests): `test_version_build.py` (3, reflog/git build info del entorno), `test_foreground_model_routing.py` (3), `test_agent_harness_loop.py` (3), `test_windows_native_execution.py` (2, solo Windows), `test_two_tier_search.py` (2), `test_l91_domain_synonyms.py` (2), `test_l68_desk02_ui_journeys_live.py` (2), y uno en cada uno de `test_w4a_requirements_js`, `test_w3a_composer_js`, `test_studio_vram_live_js`, `test_studio_saved_evidence_js`, `test_studio_research_restart_js`, `test_studio_ask_user_options_js`, `test_studio_approval_errors_js`, `test_l86_source_control_panel_js`, `test_cmp01_doc_session_js`, `test_cmp05_attention` (todos los `_js` fallan en la worktree porque no tiene `node_modules` — en la copia principal pasan), `test_research_resume`, `test_rerank`, `test_l67_exec_target_and_preview`, `test_docs_no_orphan_images` (`docs/spec/superpowers/*` de Cursor fuera de las carpetas permitidas; `LIFECYCLE.md` ya movido a `docs/spec/distribution/`), `test_desktop_tools::test_settings_defaults`, `test_asset_versioning`, `test_agent_profiles_builtin`, `test_agent_bash_windows`, `test_agent_asks_before_a_new_system`, `eval/test_baseline_match`, `acceptance/test_a04_replay_cursor::test_resume_without_an_active_run_is_still_a_404` (solo bajo xdist: «no such table: sessions», pasa en serie) y `acceptance/test_a21_embed_two_sessions` (2, sin `node_modules`/`happy-dom` en la worktree). Regla: un fallo nuevo se demuestra con la misma comparación, no se supone.

### xfail y limitaciones honestas que cada lote dejó

- **A31** — el coste de un delegate sin tabla de precios reporta siempre `unpriced_usage`, nunca `0.0` falso.
- **A29** — `loop_breaker.py` cableado en `agent_loop.py` (observa cada (tool, args, resultado), `observe_skipped` en las recuperaciones, `stop_reason=non_progressing_loop`).
- **Creator** — 27 de 43 paquetes implementados y montados detrás de `creator_enabled` (OFF); ningún motor real (ComfyUI, faster-whisper, TTS, ACE-Step/MusicGen) instalado en este entorno, así que todo adapter reporta `available=False` honesto. Quedan WP17/19/21/23/25/26/28/29/31/33/34/35/37/38/39/40/42.
- **Reach** — el backend de sesión de navegador (`x`/`reddit`) es un seam real sin lector conectado; búsqueda en `x` sin navegador ni `reach_nitter_base` responde `unavailable` honesto.
- **Code graph** — sin edge `INHERITS`; no recorre cadenas de herencia.
- **Fan-out** — sin SSE de progreso, solo poll.
- **PDF ops** — `compress` es deflate + dedup de `pypdf`, no re-muestreo de imágenes; `PATH_ARGUMENT_FIELDS` no se amplió con las rutas de `pdf_ops`, así que el reparador de argumentos (JSON-string→objeto) no las cubre todavía.

### Trampas nuevas de hoy

- **Reserva de VRAM compitiendo consigo misma.** `try_reserve`/`reserved_bytes` contaban la reserva SOBRANTE de un modelo que se está recargando como si fuera hueco ocupado por otro trabajo. Arreglado con `exclude_model=` — un modelo nunca compite con su propia reserva pendiente, otros modelos sí la siguen viendo. Visto en vivo en el turno de humo del 7001 con un 27B que se descargó entre la tarjeta de aprobación y la reanudación del turno (§94).
- **El ping de restore de `run_model_pin` carga el modelo si estaba descargado.** Un `generate` con prompt vacío contra Ollama para "restaurar" el `keep_alive` original CARGA el modelo si no estaba residente — 18 GB por nada. El restore ahora comprueba que el modelo sigue cargado antes de tocarlo (§90, §94).
- **`moduleResolution=Node10` vs TypeScript 6.** `sdk/ts` es `"type": "module"` pero sus builds CJS necesitan `Node10`; TypeScript 6 convirtió esa opción en error duro salvo `ignoreDeprecations: "6.0"`, y TypeScript 5.9 RECHAZA ese mismo valor (TS5103). `sdk/ts/scripts/tsc-node10.mjs` detecta la versión del compilador instalado y pasa el flag solo cuando hace falta — no asumir una versión fija de `tsc` en ningún script nuevo que compile ese paquete.
- **`.git` de solo lectura en Windows.** Un `git clone` deja ficheros pack de `.git` en modo solo lectura en Windows; `shutil.rmtree` normal falla al borrar el directorio de trabajo temporal. `_rmtree_force` (en `src/skill_sources.py` y replicado en `src/creator/plugins.py` con el mismo nombre y el mismo comentario "mirrors skill_sources._rmtree_force") quita el atributo de solo lectura antes de borrar. Cualquier código nuevo que clone un repo a un directorio temporal y lo borre después necesita la misma función, no `shutil.rmtree` a secas.
- **`subprocess.list2cmdline` para comandos de test en Windows.** `src/fanout/runner.py` arma el comando del test del proyecto con `list2cmdline(argv)` en Windows (`os.name == "nt"`) en vez de `shlex.join`, que asume comillas POSIX y rompe rutas con espacios o backslashes en cmd.exe.
- **`ast.literal_eval` sobre `FUNCTION_TOOL_SCHEMAS`.** Varios tests de paridad (`test_objective_tool_schema.py`, `test_tool_index_schema_parity.py`, `test_t8_wiring.py`) no importan `src/tool_schemas.py` — parsean su AST y hacen `ast.literal_eval` sobre el nodo de la asignación. Esto exige que `FUNCTION_TOOL_SCHEMAS` (y cualquier estructura que estos tests lean) siga siendo un **literal de dict/list en el código fuente**, nunca el resultado de una función o una comprensión que solo se evalúa en tiempo de ejecución — un tool nuevo añadido con `.append()` o construido dinámicamente no lo verán estos tests y darán una paridad falsa.
- **La unión de tools incluidas+diferidas decide las reglas de dominio, no solo las incluidas.** `src/agent_loop.py` calcula `_domain_union = included | deferred` antes de derivar qué bloques de reglas (entorno, permisos por dominio) entran en el prompt — una tool todavía diferida (no promovida por `lookup_tools`) YA cuenta para decidir si, por ejemplo, entra el bloque de entorno de shell. Cualquier lote que añada un dominio de reglas nuevo tiene que unirse a este cálculo, no solo a `included`, o una tool ofrecida-pero-diferida se queda sin su bloque de reglas hasta que se promueve.

## Ejecución nativa en Windows y el permiso por carpeta (14-09-2026)

Contexto completo en FAUSTUS.md §85. Lo que queda abierto:

- **Pantalla para las concesiones por carpeta.** `src/tool_approval_grants.py` guarda
  la respuesta «Siempre en esta carpeta» de la tarjeta de permiso y ya tiene
  `list_for(owner)` y `revoke(owner, workspace)`, pero no hay dónde verlas ni
  quitarlas. Sitio natural: Settings › Security, junto a las aprobaciones. Hasta
  entonces la única forma de revocar es borrar la fila de
  `<ODYSSEUS_DATA_DIR>/tool_approval_grants.json`.
- **La herencia es por subárbol y eso hay que verlo.** Conceder sobre
  `C:\Users\luism\Desktop\Proyectos independientes` cubre TODO lo que cuelgue de
  ahí. La tarjeta lo dice, pero la pantalla de arriba debería enseñar la ruta
  exacta concedida.
- **`powershell` no tiene `#!bg`.** Los trabajos en segundo plano siguen siendo de
  `bash` (`src/bg_jobs.py` lanza con el shell de bash). Un `.bat` largo que haga
  falta detached hoy se envuelve desde `bash` con `#!bg`, y eso **funciona a
  propósito**: el marcador `#!bg` se parte en `src/tool_execution.py` antes de
  llegar a `BashTool`, así que la guarda que enruta `powershell`/`cmd` al tool
  nuevo no lo ve. Es la única vía para detached en Windows y está bien que lo
  sea, pero conviene que `powershell` tenga su propio `#!bg` y que entonces la
  guarda cubra también ese camino.
- **POSIX con Docker caído cambia de comportamiento.** `agent_sandbox_mode` es
  `auto` por defecto: donde antes se rechazaba el comando, ahora lo corre el host
  y el resultado lo dice (`sandbox_skipped`). Quien quiera la puerta dura tiene
  que poner `strict` a mano. Decidir si el valor por defecto debería depender del
  sistema operativo en vez de ser global.
- **Suite entera sin pasar.** Este frente pasó 813 tests en los 30 ficheros que
  tocan sandbox, aprobaciones, validación de argumentos, selección de tools y la
  puerta de contexto externo, más `tsc`/`vite` y los checks del Studio. La suite
  completa (~14 min) no se ha corrido desde estos cambios.

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
- Verificado en vivo en el 7001 (11-09, d360f4a): commit + merge
  fast-forward de `pruebas` en `main` desde el diálogo Merge…, push, borrado
  de la rama (todo contrastado con `git` a mano); tablero kanban (crear
  LOC-1, arrastrar a «In progress»); barra lateral nueva; Ajustes → Router
  de modelos («Probar decisión» ahora descubre los modelos de Ollama).
- **Tab de Chrome colgado (una vez, no reproducido).** Un tab quedó en
  «page still loading» durante horas tras un turno con tarjeta de
  aprobación pendiente y un reinicio del 7001. Reproducir el corte de
  stream con reinicio NO lo provoca (el turno cierra con «The connection to
  the server dropped mid-turn»). Lo que sí era un bug y está arreglado: la
  tarjeta de aprobación restaurada tras el reinicio con botones vivos y el
  composer bloqueado (ahora se sirve `resolved: expired`).

## ADP/CMP (11-09)

Lo que queda abierto tras la ola ADP (`95747d9`), la ola CMP (`757262e`),
la ola W3 de cableado (`23f418a`) y el lote W4-A (`2da388a`, pestaña
Requisitos) — extraído de los límites que las propias fichas
`docs/adaptations/decisions/CMP-*.md` y `docs/api/*.md` declaran. Detalle
fila a fila en `docs/adaptations/baseline.md`. Lo que la ola W3 cerró
(chip de contexto en el compositor, `anchor` en sugerencias, evento
`strategy` en vivo, `WorktreeIsolator`/`SnapshotDirIsolator`, alternativas
sobre `DocumentVersion`, `desktop_control_session` → `invalidate_generation`,
`skill_call_history.json`, `calls_profile` en el frontmatter, export/layout/
deep-link de workflows, recetas desde un run real) ya no aparece aquí.

- **Validación física Windows UIA (ADP-09).** `src/desktop_semantics/windows_uia.py`
  está construido, aislado y probado contra fakes; nadie lo ha ejecutado
  todavía contra una sesión Windows real con UIA activo.
- **Herdr contra una instancia real (ADP-13/CMP-06).** `src/external_runtimes/herdr.py`
  infiere el contrato de cable (`/version`, `/sessions`) del texto del
  informe — nunca se llamó a un Herdr real. Solo lectura, por diseño; la
  pestaña «Externos» de Actividad lo muestra como «no configurado» hasta
  que exista una URL.
- **`workflow_iteration` sin cablear al motor (ADP-31/CMP-07).** Diseño y
  dataclasses existen (`src/contracts/workflow_iteration.py`,
  `docs/design/bounded-workflow-iterations.md`) sin tipo de nodo `"loop"`
  en el esquema — activar un bucle real es decisión de producto.
- **`_usage_bucket` sin `RouteDecision` (ADP-22).** El motivo de ruteo viaja
  en el evento SSE `model_router` y en `record_outcome`, no en
  `src/agent_loop.py::_usage_bucket` — falta pasar `RouteDecision.to_dict()`
  como kwargs con default `None`, mismo patrón que `cost_usd`.
- **MOD-05/`execution_router.py` sin reconciliar del todo (ADP-22).**
  `model_router.choose()` solo decide para `model=='auto'`; una sesión con
  modelo explícito sigue decidiendo por `execution_router.py`.
- **Medir los pools de admisión (ADP-32).** `src/resource_admission.py`
  define pools y prioridad de primer plano, pero no se ha medido en
  producción si `llm_core._LOCAL_MODEL_LOCK` sigue limitando tareas reales.
- **Vista móvil / disposición por debajo de 1280px (CMP-01-layout).** Las
  tres disposiciones no tienen efecto de rejilla en pantalla estrecha; el
  panel sigue siendo capa superpuesta. Documentado, no construido.
- **Estimador: `local_latency` parcial (CMP-08).** W3 rellena el campo desde
  `resource_admission.status()`/`llm_core.local_speed()` cuando existen;
  sin GPU medida el estimador lo declara `unknown`, nunca 0.
- **`capability_pricing` de OpenRouter sin contrastar contra un payload
  real (CMP-08).** Probado solo contra el shape documentado.
- **Canal `app_api`/`dom_cdp` sin llamador real (CMP-10).** `choose_channel`
  los admite como lógica pura; solo `native_a11y`/`pixels` tienen consumidor.
- **Alternativas: diff por pares (CMP-13).** `compare()` da diff contra la
  base + `contested_files`, no un diff ALTERNATIVA-vs-ALTERNATIVA.
- **Importación real de un export de aigraphstudio (ADP-17/CMP-07).** El
  formato de `src/workflows/interchange.py` nunca se contrastó contra el
  exportador real.
- **Requisitos (W4-A): sin importación masiva ni edición desde el
  editor de documentos.** La pestaña crea/edita/acepta/rechaza, enlaza y
  quita enlaces y consulta matriz/contexto; importar el fichero sidecar
  entero o crear un requisito desde una selección del documento no existe.

## Excursos y cables de contexto (11-09)

- Hecho y verificado en vivo (FAUSTUS.md §75-76): excursos, materiales
  cableados (documentos y notas), replay bajo botón y condensar a mano.
- **Condensar con un modelo local frío es lento** (visto: 27B con «PCIe
  spill» a ~6 tok/s → más de dos minutos; el diálogo lo dice y espera hasta
  300 s). Con un modelo «utility» configurado en Ajustes lo usa en su lugar
  (misma resolución que la compactación automática). No se cambia solo.
- Queda por decidir, no por codificar: enlace del material de documento al
  documento desde el panel (el panel no conoce la ruta del documento);
  el **fork clásico** sigue copiando mensajes — podría pasar a ser un
  excurso sin pasaje si nadie echa en falta la copia.

## Inferencia local (spec INF, 12-09)

- Hecho: INF-00 auditoría, INF-01 veracidad, INF-02 evidencia, INF-03
  visibilidad, INF-04 banco explícito y perfiles (FAUSTUS.md §77). Todo
  probado **solo con fixtures y motores falsos**: ninguna prueba arranca un
  modelo real. Falta verlo en vivo en el 7001 (chip de arquitectura y
  «Capabilities» en el formulario de serve, `ReceiptPanel` en una tarea,
  cronología bajo una respuesta, pestaña «Optimize for my machine»).
- **Primeros benchmarks reales (12-09, autorizados por Luis: «quédalo
  resuelto»)**: cinco runs de `es_conversation` contra el qwen3.8 27B ya
  residente (nada nuevo cargado, sin descargas). `engine_timings` de Ollama
  llega con la forma esperada (prefill/generación `reported_engine`, carga,
  tokens). Cinco cosas salieron SOLO al correrlo de verdad y están
  arregladas: el razonamiento del modelo se evaluaba como respuesta; el
  runner iba por `host:port` (API nativa, thinking activado) en vez de por
  la URL `/v1` del chat (thinking suprimido) — ahora el plan guarda la URL;
  un caso cuyo presupuesto se va entero en pensar es `error`, no respuesta
  fallida; `language_es` con respuestas de una palabra y `contains` con
  mayúsculas; cada plan guardaba un perfil «Current» nuevo. Resultado
  final: 7/7 casos, 100 % calidad, 20,4 tok/s mediana, TTFT 242 ms;
  comparador con dos runs comparables → `no_change` (−2,0 %). Lo que sigue
  sin poder verse con fixtures: llama-server (`/props`, `/slots`, `timings`)
  contra una versión concreta del servidor.
- **El único slot de Ollama es compartido**: durante el run 5 otra petición
  (14k tokens, no era del 7001) ocupó el slot 3 min 47 s y el caso 7 esperó
  detrás. Ahora `ExecutionMetrics.notes` lo nombra («N s unaccounted: the
  engine served something else first») y no se funde con la generación;
  el comparador lo ve como dispersión. Regla práctica: no lanzar un
  benchmark mientras otro cliente usa el mismo Ollama.
- INF-05 hecho (FAUSTUS.md §78): identidad física, presupuesto por GPU,
  admisión con latido y puerta para Cookbook serve, `activate_profile`.
  Sólo con fixtures; queda ver en vivo Servers › Physical GPUs (¿uuid y
  enlace de las tres tarjetas? ¿la 5060 Ti externa sale como enlace
  estrecho?), el bloque «Memory estimate» del formulario de serve y un
  `serve.vram_blocked` real con el diálogo. `KV_RATES` guarda una sola
  observación por modelo: el ajuste `fitted` no se disparará hasta que
  acumule varias (cambio pequeño en `vram_fit.remember_kv_rate`, pendiente).
- Candidatos que requieren reinicio del motor: `activate_profile` ya
  prepara el plan de relanzamiento (`deferred`, `requires_restart`); el
  botón «Relaunch with this profile» en Cookbook › Running NO existe aún.
- INF-06/07 (laboratorio: especulación, reparto entre GPUs, comparación
  de motores) solo con autorización explícita para cada tanda de medidas.
- El comparador usa `p95−mediana` como proxy de dispersión y `n ≥ 3`; el
  doc lo dice: no es un test estadístico. Si se quiere rigor, hay que subir
  repeticiones, no cambiar el umbral.
- Arquitectura de un repo GGUF (sin `config.json` en HF): hoy `unknown`.
  Leer la cabecera GGUF del fichero cacheado (`general.architecture`,
  `*.expert_count`) daría `dense|moe` sin red; pendiente, con su test.
- Respuesta en italiano a un prompt en español cuando el prompt trae un
  bloque de memoria (visto una vez con qwen3.8 27B): ver
  `src/reply_language.py` si se repite.

## Modos de comportamiento (12-09) — HECHO

- Implementado y visto en vivo (FAUSTUS.md §79): ocho modos integrados
  (`default`, `adversarial` con el prompt de Luis, `socratic`, `terse`,
  `mentor`, `red_team`, `observer`, `editor`), modos propios, chip en el
  compositor, `/mode`, chip y aviso «Mode not fully honoured» en cada
  respuesta, Ajustes › Behaviour modes. El 27B q8 siguió `adversarial` y
  `terse` a la primera.
- Queda por ver con modelos pequeños (9B y menos) si respetan las etiquetas
  [Certain]/[Likely]/[Guessing]: `mode_check` lo dirá por turno; si fallan
  sistemáticamente, un modo «adversarial-lite» sin etiquetas.
- El chip y el aviso aparecen al refrescar el historial tras el turno (no
  hay evento SSE en vivo para `behavior_mode`/`mode_check`); si molesta,
  emitirlos en el evento `metrics`.

## Conectores Hoard (13-09) — HECHO en nube, pendiente en vivo

- Implementado (FAUSTUS.md §80, OBJ-10): `/connectors`, presets Jobhunter y
  Writer, sidecar, estados reales, perfiles de arranque, `connector_ids`
  por sesión/proyecto/tarea cumplidos en el despachador, `tool-support`,
  `external_ref` en calendario, filtros de correo, clasificador + receta
  «Revisar respuestas de candidaturas» con fixtures; en Jobhunter (rama
  `claude/conectores`) health, biblioteca de respuestas con ids/revisión,
  recuperación con backup y `record_employer_response`.
- HECHO el 13-09 por la tarde con el Jobhunter REAL (Luis arrancó el 5178
  con el código nuevo; `claude/conectores` `947b7c6`): el conector de
  Faustus apunta al 5178 real → «Available · 15 tools»; «Abrir la app»
  desde Faustus abre Jobhunter (su guarda `sec-fetch-site` rechaza la
  navegación de la extensión de Chrome pero acepta el `window.open` desde
  el 7001, que es same-site). `dist/` estaba sin reconstruir (el
  `npm run build` del script de aplicación no llegó a ejecutarse) →
  reconstruido a mano. «Recuperar de borradores» sobre sus datos: 1
  contexto, 81 ofertas, 30 borradores con datos → **76 respuestas nuevas,
  24 ya existían, 0 borradas repuestas**, copia previa
  `data/db.json.bak-2026-09-13T10-40-41-732Z`; ahora 121 respuestas, 1
  pendiente, variantes agrupadas (p. ej. 6 de una misma pregunta de
  idioma). Visto: las 45 respuestas antiguas salían como «Candidatura»
  porque no tenían `scope` → la migración de arranque las marca `profile`
  (efectivo cuando Luis reinicie su 5178). Quedan: revisar pendientes y
  variantes a mano; probar `remember_answer` en un contexto de prueba con
  el Qwen; Writer en un puerto distinto del 8766 (`WH_AIBRIDGE_PORT`),
  nunca cerrar Relief Studio.
- Límites conocidos y documentados: `external_ref` es check-then-insert
  (sin UNIQUE porque `CalendarEvent` no tiene owner); la política gobierna
  solo `mcp__<server>__<tool>` (las built-in de correo quedan fuera);
  `GET /api/connectors` no filtra por owner porque `/api/mcp/servers`
  tampoco lo hace (McpServer no tiene owner).

## Correo (13-09, tarde) — HECHO en nube, pendiente ver en el 7001

- Luis: «el side panel de pick a message debería aparecer cuando abres un
  correo; la lista es demasiado estrecha» y «no se carga ninguna imagen y
  se ve de culo». Lista a ancho completo hasta que abres un mensaje o el
  compositor (entonces tres columnas: carril · lista 300–420 px · lector);
  la pista de teclas pasa al pie de la lista. Imágenes: el sanitizador
  corría hasta punto fijo y reiniciaba el contador en cada pasada → el
  botón «Show N remote images» no salía nunca (las imágenes se retienen en
  la primera pasada y en la segunda ya no hay `src`); ahora cuenta el
  máximo, las retenidas conservan su caja (width/height) y no pintan el
  alt como píldora. Botón con menú «esta vez» / «siempre de este
  remitente» (localStorage) y ajuste global «Load remote images in every
  mail» (`email_remote_images`, apagado por defecto: cargar imágenes avisa
  al remitente de que has abierto el correo).
- Google Calendar: me equivoqué — Google rechaza Basic Auth en CalDAV
  (Luis lo trajo con la doc). HECHO como él propuso (FAUSTUS.md §81):
  proveedor Google con OAuth2 + Calendar API v3, selector de proveedor en
  Integrations (Google · iCloud · Nextcloud · CalDAV). Para probarlo en
  vivo falta el cliente OAuth en su `.env` (no existe ni para el correo):
  crear cliente web en Google Cloud, habilitar Calendar API, registrar
  las redirect URIs; desde §82 ya no hace falta `.env` ni reiniciar: el
  asistente de Integrations › Calendar › Google da las URIs exactas con
  botón de copiar, acepta el `client_secret_*.json` y comprueba el cliente
  contra Google («Check»). Luis solo tiene que crear el cliente en Google
  Cloud (guía `docs/api/google_oauth_setup.md`) y pegarlo.

## Paridad de aceptación (13-09, noche) — PR1 + A01–A07 + PR2/A20 HECHO; el resto abierto

- Paquete de Luis en el scratchpad de la sesión y en `docs/spec/paridad/`
  (manifiesto, 36 recetas, estado, backlog). Ejecutor:
  `python3 scripts/acceptance_run.py` → `data/acceptance/<run_id>.jsonl`
  (hoy `passed=8, NOT_EXECUTED=28`).
- PR2 hecho (FAUSTUS §84): `sdk/ts` + scope `sessions` + `docs/api/sse_events.json`.
  Queda de PR2: publicar `faustus-sdk` en un registro (decisión de Luis: npm
  público o GitHub Packages), generar el cliente desde OpenAPI en vez de a
  mano, y un job de CI que haga `npm run build && npm test && npm run check`
  en `sdk/ts`. A21 (UI embebible) es PR6.
- Siguiente por el blueprint:
  PR4 artefactos/carga diferida (A08/A09/A12/A13), PR5 Code Mode (A10/A11/
  A31), PR6 UI embebible (A21), PR7 OIDC/Team (A22/A23), PR8 evolución con
  rollback (A26–A30), PR9 migración/benchmark (A32–A34). Compacción
  intra-turno (A14/A15). Licencia TF17: decisión de Luis.
- Trampas de este lote: `consume_with_reason` lo escribieron T2 y T3 a la
  vez (se quedó la forma `(reason, approval)` de T3 + `retire_for_session_ids`
  de T2); el engine de workflows solo admite cuatro estados de handler, por
  eso `fenced` viaja como `failed` + `fenced: True` en nodos (en tareas
  programadas sí es un estado propio).

## Última evidencia

- **13-09-2026 madrugada, paridad incremento 2 (SDK + A20).** Suite nube
  entera tras S1–S3: 17.775 correctas, 49 saltadas, 3 fallos: uno real (la
  matriz exacta de tokens no listaba la ruta de export, arreglado) y dos de
  `tests/test_caldav_writeback_route.py` que pasaban solos. Bisecado por
  mitades sobre el orden real del worker: la causa era
  `tests/test_acceptance_index.py` (PR1), que importaba TODOS los
  `tests/test_*.py` en el mismo proceso bajo otro nombre de módulo
  (`tests.test_x`) para leer los markers, y así ejecutaba dos veces los
  efectos de importación (p. ej. `croutes.SessionLocal = <BD temporal>`),
  dejando la ruta apuntando a una BD que el módulo de pytest nunca escribía.
  Ahora el índice lee los decoradores con `ast` (sin importar nada) y cachea
  la pasada (4 s en vez de 36×4 s).
  `scripts/acceptance_run.py`: `passed=8`, `NOT_EXECUTED=28`; A20 ~40 s.
- **13-09-2026 noche, paridad de aceptación incremento 1 (master `c7df980`+ =
  Windows).** Suite nube entera tras fusionar T1–T3: 17.659 correctas, 49
  saltadas, 0 fallos (11 min 39 s). `scripts/acceptance_run.py`:
  `passed=7` (A01–A07), `NOT_EXECUTED=29`.
- **13-09-2026 noche, cliente OAuth de Google desde la app (master `ead8879`+ =
  Windows).** Suite nube entera tras G3: 17.593 correctas, 49 saltadas, 1
  fallo que era la guía en `docs/guides/` (la guarda de docs solo admite
  Markdown en subárboles de ingeniería) → movida a
  `docs/api/google_oauth_setup.md`.
- **13-09-2026 tarde, correo + Google Calendar (master `9dd3a8c`+ = Windows).**
  Suite nube entera tras los lotes G1/G2: 17.558 correctas, 49 saltadas,
  0 fallos (9 min 41 s). En vivo en el 7001: Integrations › Add › Calendar
  → selector Google · iCloud · Nextcloud · CalDAV; sin cliente OAuth en el
  `.env` el formulario de Google lo dice y el botón Connect queda
  deshabilitado (estado honesto, no probado más allá hasta que Luis cree
  el cliente).
- **13-09-2026, conectores (FAUSTUS §80; master `a3b68b4`+ = Windows).** Suite
  nube entera tras integrar F1–F4: 17.525 correctas, 49 saltadas, 0 fallos
  (10 min 7 s). En vivo en el 7001: `/connectors` con el preset Jobhunter,
  «App not running · Adapter: 15 tools» con la app apagada, perfil de
  arranque → instancia de PRUEBA en 5179 (`JOBHUNT_DATA_DIR` en
  `D:\LocalAI\_claude_tmp\jh_testdata`; la mató después mi
  `restart7001.ps1`, que mata los HIJOS del 7001 — `Stop-Faustus.ps1` no
  lo hace —, «Iniciar la app» la vuelve a levantar), «Available», qwen3.8 27B q4 llamando
  `list_contexts`/`list_jobs` por MCP con tarjeta de permiso, y con
  «ningún conector» el modelo dice que no tiene la herramienta. Jobhunter
  en Windows: `claude/conectores` `f08fd93`, 28/28.

- **12-09-2026 noche, INF-05 + benchmarks reales (master `4a9d21d`,
  Windows `e1efe93`+).** Suite nube entera: 17.367 correctas, 49 saltadas,
  0 fallos (11 min 8 s). Visto en vivo: Physical GPUs, presupuesto por GPU,
  cinco runs de banco y el comparador con datos reales (§78).
- **12-09-2026, INF-00…04 (master `3a405f2`, Windows igual).** Suite nube
  entera tras INF-04: 17.228 correctas, 49 saltadas, 0 fallos (9 min 19 s).
  Visto en vivo en el 7001 (Chrome): pestaña Optimize (endpoint Ollama →
  modelos instalados, suite filtrada por objetivo, plan «3 casos ·
  estimación unknown until a first run · procesos afectados: none»; el
  botón Start NO se pulsó: ningún benchmark real ejecutado, §01), formulario
  de serve con «Architecture: unknown (metadata unavailable)» para un repo
  GGUF sin `config.json` (honesto, pero mejorable leyendo la cabecera GGUF),
  «Implementation: llama-server» y lista Capabilities (ctx/ngl/flash_attn
  supported con su nota), y «Why did it take this long?» bajo una respuesta
  real de qwen3.8 27B: cola 293 ms observed, carga 3 ms engine, prefill
  1,0 s engine, generación 5,1 s engine, total 7,3 s observed, tools «the
  engine does not expose this metric», tokens 545/118 engine. Vistos y
  arreglados: `/api/model/cached` daba 500 por un `scan_cache.py` viejo no
  escribible (ahora fichero único por llamada + fallback a temp); un test
  INF-02 lanzaba un proceso real en Windows (`IS_WINDOWS` fijado); «Di
  solo: seis» recuperaba la memoria «…café solo por la mañana» por la
  palabra «solo» y el modelo contestó (en italiano) sobre el bloque de
  memoria en vez de decir «seis» — stopwords en español y tokens con
  acentos; tras el arreglo: «Seis.». Al aplicar w39 se borró
  `D:\LocalAI\odysseus\data\settings.json` (el `data/` del repo, no el
  `odysseus-dev-data` del 7001): si ese fichero importaba, está en los
  backups de `backups/` anteriores al 12-09; los scripts de transferencia ya
  no lo tocan.
- **11-09-2026, QA en vivo de las olas ADP/CMP/W3 + W4-A (master
  `e075293`, Windows `2da388a`+).** Suite nube tras los arreglos de
  entorno: 16.913 correctas, 0 fallos (antes: fallos preexistentes en
  chat_helpers/chatgpt_subscription/session_image_cleanup/git_invariants/
  qa_26/sandbox_exec/docker/markitdown, todos corregidos en `29d99fc` y
  `966b647`; `terminate_tree` suspende la raíz antes de matar hojas). Vistos
  y arreglados en pantalla (7001, Chrome): fila de Actividad con el título
  aplastado (`flex-wrap`), barra del compositor desbordada a 1920px (media
  query insuficiente → `@container` sobre `.fs-studio__bar`), cabecera del
  Studio oculta tras la columna de documento en las disposiciones
  documento/revisión, «Aplicar» de Alternativas fusionaba con un clic
  (ahora dos pasos), `suggest_document` fallaba con «No active document»
  con un turno en español (puerta de relevancia bilingüe +
  `active_document_pinned` cuando el chip de contexto apunta a ese doc),
  ReviewPane creaba comentarios vacíos (ahora pide el texto),
  `alternatives.run_tests` en Windows comía barras invertidas
  (`shlex` solo en POSIX). Verificado en vivo: `/workflows`
  (cargar/simular/inspector/lint), `/alternatives` (crear → worktree →
  comparar → aplicar, fichero cambiado en disco), atención en Actividad,
  tres disposiciones, documento → selección → chip → sugerencia con
  `anchor` → aplicar (v2), comentario en ReviewPane, Ajustes → OpenRouter,
  vecindario en Contexto, y la pestaña Requisitos (crear REQ-1, enlazar
  `store.py@remove_link`, «Quitar» → «Enlace quitado», campos de edición
  apilados tras verlos en línea). Suite Windows entera (`suite_m1.ps1`,
  19 min) sobre `ac9fb88`: 16.878 correctas, 6 fallos, todos de entorno y
  todos corregidos en `32723fe` (PATH mínimo sin `venv\Scripts` frente al
  PATH crudo; `futures.db` abierto hasta el GC bloqueaba el borrado del
  temporal — ahora `_connect()` cierra; test de cancelación que reescribía
  `report.py` sin re-aprobar la skill (puerta ADP-25); la política real de
  Settings con el navegador apagado cortaba `browser_pid` en los tests de
  reconexión; dos `SyntaxWarning`). Los ocho ficheros afectados: 86 correctas
  en Windows (Docker encendido, el test de cancelación real incluido).
  Segunda pasada entera sobre `71fd5ca`: 16.883 correctas, 1 fallo que NO
  estaba en la primera y pasa solo (`test_cookbook_shell_uses_bash_syntax_on_windows_too`:
  `_run_shell` devolvió «command could not be completed» bajo carga `-n 6`).
  La excepción se tragaba en un logger de debug; ahora `routes/codex_routes.py`
  la nombra en `stderr` para que la próxima vez se pueda bisecar en vez de
  encogerse de hombros. **Pendiente: si vuelve a salir, leer la clase.**
