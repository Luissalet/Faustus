# Pendientes, deuda y cosas a vigilar — UI

Rama: `feat/studio-ui`. Estado a 04-09-2026.
Compañero de `OBJETIVOS_UI.md`. Aquí va lo que puede romper algo, no lo que
falta por construir.

## Deuda legacy medida (baseline de UI-012)

Cifras del análisis estático previo, pendientes de confirmar con el auditor:

- ~92 apariciones de `transition: all`.
- >110 eliminaciones de `outline` sin comprobar reemplazo de foco.
- >300 lecturas de `getBoundingClientRect`; legítimas en ventanas y editor,
  peligrosas si se intercalan con escrituras.
- Controles interactivos construidos como `div` en menús y cabeceras.
- Iconos que dependen de `title` sin `aria-label`.
- 262 `<svg>` inline dentro de `index.html`, más estilos inline repetidos.

Regla: no se arregla la deuda antigua en bloque. Cada pantalla migrada limpia
su área y baja el baseline. Ningún módulo Studio nuevo la aumenta.

## Riesgos abiertos

1. **Fallback SPA.** Es el único cambio del incremento 1 que puede romper la
   API. Lista blanca de siete rutas, jamás `{path:path}`, y test de que un 404
   de API sigue siendo JSON.
2. **Identificadores de artefacto.** Sin espacio de IDs común entre galería,
   documentos y outputs de run, `Usar en…` entre subsistemas será frágil. Hay
   que decidirlo antes de UI-042, no durante.
3. **`style.css` de 1,52 MB.** Cualquier tentación de reescribirlo mezclada con
   features es cómo se pierde el trimestre. Se divide en el incremento 5 y a
   solas.
4. **Coste y presupuesto en ContextBar.** El backend expone coste por run en
   `agent_loop.py` y `external_worker.py`, pero falta confirmar que hay
   estimación *previa* a ejecutar. Si no la hay, el chip de presupuesto no se
   promete en UI-031.
5. **Doble atajo de teclado.** `Ctrl/Cmd+K` de la paleta contra el buscador de
   conversaciones existente. Se resuelve en UI-022 convirtiendo la búsqueda en
   comando; si algo sigue capturando la tecla, se localiza antes de cerrar.

## Entorno de pruebas

- `Start-Faustus-Dev.ps1` sigue apuntando a `D:\LocalAI\odysseus-dev`, que ya no
  existe. Para el 7001 se usa la raíz real con `ODYSSEUS_DATA_DIR` apuntando a
  `D:\LocalAI\odysseus-dev-data`, `APP_PORT=7001`, `LOCALHOST_BYPASS=true`,
  `AUTH_ENABLED=false`. Arreglar el script es un pendiente propio.
- E2E: `ODYSSEUS_E2E=1 python -m pytest tests/e2e -q` desde `venv`. Playwright
  1.62 instalado. En Windows hay un error de colección conocido en
  `tests/test_history_import.py` que no es nuestro.

## Riesgos que trae la decisión de React (04-09-2026)

6. **`[!]` Dos sistemas vivos.** Es la forma en que mueren estas migraciones:
   medio repo en React, medio en el DOM antiguo, y nadie se atreve a borrar.
   Mitigación acordada: cada pantalla migrada retira su equivalente legacy en
   el mismo incremento. Un incremento que termina sin haber borrado nada es una
   alarma, no un detalle.
7. **`[?]` Build reproducible sin red.** La primera instalación de dependencias
   necesita registro. Hay que decidir en UI-002 si se versiona el lockfile con
   caché de npm, se vendoriza `node_modules` o se acepta que clonar exige una
   instalación con red una vez. Node instalado: v22.19.0 (Vite lo soporta; la
   CLI de `skills` pide >=22.20.0 y avisa, sin romper).
8. **`[!]` Bundle obsoleto servido en silencio.** Si `Start-Faustus.ps1` sirve
   un `static/studio/` viejo sin avisar, se depuran fantasmas durante horas.
   Tiene que construir o fallar diciendo por qué.
9. **`[?]` CSP con nonce.** El bundle es un `<script>` externo y encaja, pero
   los estilos inline que inyectan algunas primitivas de Radix y las
   animaciones hay que comprobarlos contra la CSP real, no suponerlos.
10. **`[!]` Cobertura perdida al cambiar de markup.** La familia de tests que
    assertaba HTML dentro de `index.html` deja de valer. Ningún test se borra
    sin que su sustituto pase antes; lo que se quede sin cobertura se anota
    aquí con nombre y apellidos.
11. **`[~]` Peso y arranque.** Faustus es local-first: nada de CDN en tiempo de
    ejecución y presupuesto de bundle declarado en UI-002. Cada dependencia
    nueva fuera de la tabla aprobada es una decisión, no un `npm install`.

## Añadido tras el lote B (04-09-2026)

12. **`[~]` Herramientas de build en `dependencies`.** El entorno tiene
    `NODE_ENV=production` y `npm config omit=dev` globales, así que Vite,
    TypeScript y el plugin de React se declararon como dependencias normales
    para que se instalen. Es aceptable en un repo privado y local-first, pero
    cualquier despliegue futuro se los llevará puestos. Documentado en
    `docs/ui/toolchain.md`.
13. **`[~]` La galería entra en el bundle.** Los 307 KB medidos incluyen
    `studio/src/gallery/`, que ninguna pantalla real importará. Cuando el
    AppShell tenga su propia entrada (UI-021), la galería debe quedar fuera
    del bundle de producción o en un chunk perezoso; hasta entonces la cifra
    de presupuesto está inflada a nuestro favor, que es el sentido malo.
14. **`[?]` `npm install` falla en Windows.** `ERR_INVALID_ARG_TYPE` en el
    postinstall de esbuild con npm 10.9.3 y Node 22.19.0. Se arregla con
    `npm install --ignore-scripts` y luego `node node_modules/esbuild/install.js`.
    Nadie ha comprobado si se reproduce en una máquina limpia.
15. **`[!]` Accesibilidad solo verificada a ojo.** Las guardas de UI-012 son
    estáticas y las ratios de contraste están calculadas a mano. No hay
    comprobación automática sobre la página renderizada: orden de foco,
    trampas de teclado y contraste en vivo siguen sin cubrir. Va con UI-021.
16. **`[~]` El límite de uso de la cuenta puede parar el trabajo delegado.**
    El Claude Code local devolvió `success` con cero tokens y cero tiempo de
    API justo al acabar el lote A. No da error legible: hay que mirar
    `--output-format json` para verlo. Si un lote termina en segundos y sin
    escribir nada, es esto y no un fallo del encargo.

## Añadido tras Studio (04-09-2026, lote E)

17. **`[!]` Studio aún no sustituye al chat legacy.** Faltan adjuntos,
    documentos (`doc_update`, editor), comparar modelos, presets/personajes,
    incógnito, edición y borrado de mensajes, fork y compactar. Hasta que
    tenga eso, «Abrir en la interfaz anterior» se queda en la cabecera y
    `static/js/chat.js` no se toca (DECISIONES_UI.md §4).
18. **`[~]` Eventos del stream que Studio ignora a propósito.** `plan_update`,
    `browser_view`, `doc_*`, `ui_control`, `context_ledger`, `subagent_event`,
    `harness_*`, `round_info`, `progress_update`. Llegan y se descartan sin
    ruido. Cada uno es una pantalla o panel pendiente, no un bug.
19. **`[!]` Los tiempos del servidor son UTC sin zona.** `/api/sessions` y
    `/api/history` escriben `2026-08-31T15:35:30.170267` sin `Z`. El
    adaptador (`parseStamp` en `adapters/home.ts`) los lee como UTC. Si algún
    endpoint escribe hora local sin zona (`next_run` de tareas es sospechoso)
    saldrá dos horas desplazado: verificar Automatizaciones contra la hora
    real de una tarea programada.
20. **`[~]` `min-block-size: 0` del reset rompe filas con hijos `overflow`.**
    `base.css` pone `min-block-size: 0` a `button`/`a` para vencer a las
    alturas fijas del legacy; en Chrome, un enlace grid con hijos
    `overflow: hidden` colapsa a cero. Studio lo repone por clase
    (`.fs-studio__session`). Cualquier fila nueva con esa forma necesita lo
    mismo o cambiar el reset por algo más quirúrgico.
21. **`[~]` La paleta de modelos lee `/api/models` una vez por visita.** Sin
    `refresh`, así que un endpoint que se enciende después no aparece hasta
    recargar. Falta un «actualizar» en la paleta y respetar `models_extra`.
22. **`[?]` Las capturas dependen del modelo local.** `shot_studio.py` fotografía
    la sesión más reciente; con Ollama apagado el transcript de la captura
    enseña el error de conexión, que es honesto pero no representativo.
23. **`[~]` `ollama serve` lanzado a mano no lee `OLLAMA_MODELS` del usuario**
    si el shell viene del bridge: hay que exportarla antes
    (`_claude_tmp/ollama_up.ps1`). No es de Faustus, pero cuesta veinte minutos
    la primera vez.

## Añadido tras el lote F (04-09-2026, paridad del compositor)

24. **`[!]` Dos copias de React por una query de cache-busting.** Un chunk
    perezoso importa lo que comparte con la entrada como `../studio.js`
    (sin `?v=`), y el navegador lo trata como un módulo distinto de
    `studio.js?v=…`: dos React, «invalid hook call», el shell entero
    desmontado al abrir el primer diálogo perezoso. Arreglo: la entrada es
    diminuta (`main.tsx` enlaza la hoja y hace un `import()` de `app.tsx`),
    los chunks llevan hash y la hoja de estilos se importa `?url` y se
    enlaza desde la entrada. `index.html` ya no enlaza el CSS. Cualquier
    vuelta a nombres estables sin hash reabre esto.
25. **`[~]` Presupuesto en bruto superado: 373 KB / 118,7 KB gzip.** El
    gzip sigue bajo los 120 KB, que es lo que viaja; el bruto pasa de 350.
    Se propone dejar el bruto como aviso y el gzip como límite duro, con
    Proyectos, Biblioteca, Automatizaciones, Actividad, el diálogo de
    carpeta y el de sesión como chunks perezosos. Decisión pendiente en
    DECISIONES §1.
26. **`[!]` Adjuntos rechazados en el 7001, en cualquier cliente.** Con
    `AUTH_ENABLED=false` la subida guarda `owner=None` y la sesión tiene
    `owner='admin'`; `reserve_upload` exige igualdad y `document_processor`
    avisa «not found or not authorized», así que el modelo no ve el fichero.
    Studio manda exactamente lo que manda la anterior (`attachments` con los
    ids de `/api/upload`). Verificar en el 7000 con auth; si allí funciona,
    es un fallo del modo sin auth en el servidor, no de la UI.
27. **`[~]` El historial no guarda `metadata.attachments`** en este servidor
    (el mensaje del usuario vuelve sin ellos), así que tras recargar, un
    turno con adjuntos los pierde visualmente. Studio los enseña mientras la
    sesión está abierta.
28. **`[x]` `/versions` y restaurar.** Cerrado: `/versions` lista, `/restore
    <id>` restaura y `/checkpoints` lista los del workspace.
29. **`[~]` Comandos `/` que enrutan a la anterior** pierden el argumento:
    `/research tema` abre la interfaz anterior sin el tema.

## Añadido tras el lote G (04-09-2026, arnés y selector nativo)

30. **`[!]` El selector de carpeta era un diálogo propio dentro de la
    página** («esa ventana… es terrible»). Cerrado: `POST
    /api/workspace/pick` abre el diálogo del sistema en el escritorio del
    servidor desde un subproceso con tkinter (Explorador en Windows) y
    devuelve la ruta ya vetada; el chip de Studio y la pill de la anterior
    lo llaman primero y solo caen al diálogo en página si el navegador no
    está en la misma máquina (`request.client.host` fuera de loopback), no
    hay pantalla o no hay Tk (501). Un único diálogo a la vez (409), se
    cierra si el cliente se va, y queda fuera del `REQUEST_HARD_TIMEOUT`
    de 45 s porque espera a una persona. Los adjuntos ya usaban el
    `<input type=file>` nativo. Riesgo conocido: el diálogo aparece en el
    monitor donde esté la ventana activa, no siempre encima del navegador.
31. **`[~]` Al aprobar una herramienta el servidor cierra la llamada
    pendiente con un `tool_output` vacío y luego repite `tool_start`.** El
    reductor marca ese último paso como «esperando» al recibir `ask_user`
    para que la repetición lo reutilice en vez de duplicar la fila; y el
    texto de la pregunta, que el servidor también manda como `delta` y
    guarda como mensaje propio del asistente, se retira al aprobar y al
    leer el historial (mensaje corto del asistente acabado en `?` seguido
    de otro del asistente). Si el servidor cambia ese patrón, revisar
    `apply('ask_user')` y `turnsFromHistory`.
32. **`[~]` `changeset.verdict` llega como `partial` con 65 % en un turno
    de un solo fichero creado tal cual se pidió.** Es del revisor del
    servidor, no de la UI; Studio lo enseña sin maquillar.

## Añadido tras el lote H (04-09-2026, sub-agentes, panel lateral, documentos)

33. **`[~]` `qwen3.5:9b` no llama a `delegate_agents` con `/agents`.** El
    campo `delegate_tasks` viaja igual que en la anterior y el servidor
    sustituye el mensaje por la instrucción de delegación, pero el modelo de
    prueba escribió los ficheros él mismo y lo dijo. El tablero en vivo está
    verificado con el reductor (`studio/checks/model.check.mjs`, secuencia
    sintética de eventos `subagent`) y el restaurado con sesiones reales
    (`bench t3v2_q35b …`, 3 workers). Probar en vivo con un modelo que
    delegue.
34. **`[~]` Ningún `browser_view` real en el 7001.** El panel «Navegador»
    se alimenta de `browser_view` y de las capturas de `tool_output` por el
    mismo camino (`safeFrameSrc`, misma lista blanca de data: URL que la
    anterior); solo se ha visto el estado vacío y la apertura por `/browser`.
35. **`[~]` El historial guarda la traza (`tool_events`) pero no los
    fotogramas del navegador ni el documento en streaming**: tras recargar,
    el panel vuelve vacío y el documento se abre por su id desde «Abrir el
    documento» en el carril o desde la Biblioteca. Igual que la anterior.
36. **`[~]` Editor de documentos básico.** Textarea con vista previa,
    versiones, PDF, archivar y sugerencias. Lo que no tiene: diff palabra a
    palabra al aceptar/rechazar cambios del agente, visor y anotación de PDF,
    firma, borradores de correo, ordenar y limpiar la biblioteca
    (`document.js`, 11 000 líneas). Son islas candidatas (DECISIONES §10).
37. **`[!]` Trampa de herramientas: `Get-Content | Set-Content` en
    PowerShell 5.1 destroza el UTF-8** (mojibake en tildes y emoji). Para
    tocar fuentes desde el puente, `edit_block` o `[System.IO.File]` con
    `UTF8Encoding($false)`; nunca los cmdlets sin `-Encoding`.

## Añadido tras el lote I (04-09-2026, sesiones y compositor)

38. **`[!]` Perdida una conversación de prueba (`6742a8f7…`) por el
    incógnito.** Studio compartía con la anterior la clave de sesión
    `ody-incognito-sessions`; `sessions.js` sigue corriendo debajo del
    piloto y borra lo que hay en esa lista salvo la sesión que ÉL tiene en
    pantalla, que nunca es la de Studio. Además la limpieza propia listaba
    la sesión normal actual. Arreglado con clave propia
    (`faustus_studio_incognito`) y `all.filter(id => id === keep)`. Regla:
    ningún estado mutable de `sessionStorage`/`localStorage` se comparte con
    la anterior mientras siga cargada debajo.
39. **`[!]` Los atajos de la anterior actuaban dos veces.** Ctrl+Alt+N creó
    una sesión vacía propia de `keyboard-shortcuts.js` además de la de
    Studio. Ahora Studio escucha `keydown` en `window` en fase de captura y
    detiene la propagación de cualquier combinación que reconozca. Ojo si
    se añade un atajo nuevo: hay que registrarlo en `DEFAULT_KEYBINDS`.
    Y el valor por defecto de la barra es `ctrl+b`, no `ctrl+alt+b`
    (`settings.js` manda).
40. **`[~]` `/api/sessions/archived` devolvía 403 con `AUTH_ENABLED=false`**
    (la anterior tampoco podía abrir su archivo en el 7001). Corregido en
    `session_routes.py` como ya hacía `/sessions/export`: sin usuario y con
    auth desactivada, sin filtro de dueño.
41. **`[~]` Micrófono y voz sin probar con hardware.** `startDictation`
    (MediaRecorder → `/api/stt/transcribe`, o `SpeechRecognition` si el
    servidor da 503) y `speak` (`/api/tts/synthesize` o `speechSynthesis`)
    están escritos contra los mismos endpoints que la anterior; el
    navegador del puente no concede permiso de micrófono. Probar a mano.
42. **`[x]` Bundle principal en 375 KB / 117,8 KB gzip** (presupuesto 350 /
    120). Resuelto haciendo `Studio.tsx` ruta perezosa que se precarga en
    `requestIdleCallback` tras el primer pintado: `app` 260,5 / 83,4 KB gzip y
    `Studio` 111,8 / 35,1 KB. Queda margen para las pantallas que faltan;
    cada una entra como chunk propio.
43. **`[~]` «Manage Chats» solo en parte.** Archivadas con búsqueda y
    recuperar, ordenar con IA y limpiar vacías; la biblioteca de chats con
    estadísticas y filtros por modelo y fecha sigue en la anterior.
44. **`[~]` Casillas de selección múltiple**: la fila es una rejilla de dos
    columnas (`minmax(0,1fr) auto`) pensada para enlace + botón; con la
    casilla delante hacía falta `data-selecting` para invertir las columnas.
    Cualquier otro elemento que se anteponga al enlace necesita lo mismo.

## Añadido tras el lote J (05-09-2026, Notas y «Herramientas»)

45. **`[~]` Dos bucles de recordatorios posibles.** Studio solo dispara los
    suyos si no encuentra `#notes-pane` (el `notes.js` de la anterior
    cargado debajo). El día que la anterior se retire, el bucle de Studio
    pasa a ser el único y hay que probarlo con una nota con hora
    (`fire-reminder`, `Notification`, avance de la repetición).
46. **`[~]` Notas: lo que sigue en la anterior.** Dibujar (lienzo → PNG por
    `/api/upload`), foto adjunta, fondo de imagen propio, selección múltiple
    con acciones en lote, edición táctil a pantalla completa, arrastre con
    pulsación larga en móvil, insignia de vencidas en el carril. Son islas
    o una segunda pasada; ninguna cambia el modelo de datos.
47. **`[!]` Contextos de apilamiento por la animación de entrada.** Todo
    hijo directo de `.fs-screen` se anima con `fs-rise … both`; un
    desplegable que sobresale de su tarjeta cae debajo de los hermanos que
    vienen después. Regla: el contenedor de tarjetas con menús lleva
    `position: relative; z-index: 1`, y la tarjeta abierta
    `:has(.fs-qmenu__list) { z-index: 5 }`. Vale para cualquier pantalla
    nueva con menús por fila.
48. **`[~]` Estilos compartidos entre chunks.** `.fs-qmenu` estaba en
    `studio.css` (chunk de Studio) y Notas lo usaba sin cargarlo. Ahora vive
    en `styles/components.css`. Cualquier componente de `components/` que
    use una pantalla fuera de Studio necesita su CSS en `styles/`, no en
    `screens/studio.css`. Lo mismo para `.fs-studio__chip` y `.fs-select`:
    Notas lleva copias (`.fs-chip`, `.fs-field`); si una tercera pantalla
    las necesita, subirlas a `components.css`.

## Añadido tras el lote K (05-09-2026, Memoria)

49. **`[~]` Los ajustes de Skills vivían en el modal de Brain** (skills
    activas, auto-skills, auto-aprobar, confianza mínima, máximo inyectado:
    `/api/prefs/skills_enabled`, `auto_skills`, `auto_approve_skills`,
    `skill_min_confidence`, `skill_max_injected`). Van con la pantalla de
    Skills (`skills.js`, 2 000 líneas), no con Memoria. Hasta entonces, en
    la anterior.
50. **`[~]` `/api/memory/add` solo acepta JSON** (con multipart responde
    422: el parámetro Pydantic opcional se evalúa antes del `await
    request.form()`). El adaptador manda JSON; si alguien añade un formulario
    clásico que lo llame, que lo sepa.
51. **`[~]` `session_id` de recuerdos antiguos lleva el nombre de la
    conversación**, no el id (`memory_extractor` de otra época). La pantalla
    lo enseña como origen y no enlaza. Una migración de datos podría
    resolverlo por nombre; no urge.
52. **`[~]` `/api/memory/import` y `/extract` tardan más de 45 s con
    `qwen3.5:9b`** y el middleware los cortaba con 504 (también a la
    anterior). Exentos en `_TIMEOUT_EXEMPT_PREFIXES`. Queda pendiente darles
    un timeout propio como el de `/audit` (120 s de inactividad).

## Añadido tras el lote L (05-09-2026, Calendario)

53. **`[~]` Las llamadas al modelo de un solo golpe superan el timeout de
    45 s en local.** Ya son cuatro exenciones (`memory/audit`, `import`,
    `extract`, `calendar/quick-parse`). Mejor que seguir sumando prefijos:
    un timeout propio por ruta (como el de 120 s de `/audit`) o un
    `REQUEST_LLM_TIMEOUT` distinto para las rutas que llaman al modelo.
    Mientras, cada ruta nueva que llame al modelo hay que añadirla a mano.
54. **`[~]` Calendario: lo que sigue en la anterior.** Cuentas CalDAV
    (formulario, prueba de conexión, borrar cuenta: `/config/accounts`,
    `/test`), semana con horas y arrastre para mover o crear con duración,
    zoom de la semana, búsqueda de eventos, «recordar en Notas» desde el
    evento, deshacer al borrar, fondo de imagen, insignia del carril. El
    cambio de calendario de un evento existente no lo permite el servidor
    (`EventUpdate` no lleva `calendar_href`); la anterior tampoco.
55. **`[~]` `quick-parse` con `qwen3.5:9b` tarda más de un minuto** (el
    modelo de utilidad es el mismo 9B). El botón muestra el giro y el
    formulario de respaldo salta si falla, pero la espera es larga; un
    modelo de utilidad pequeño en Ajustes lo arregla sin tocar código.

## Añadido tras el lote M (05-09-2026, Correo)

56. **`[!]` Un aviso `position: fixed` dentro de una pantalla no es fijo.**
    `.fs-route` y cada hijo de `.fs-screen` llevan la animación de entrada
    (transform), así que un elemento fijo dentro se coloca respecto a ese
    ancestro y se pinta debajo del overlay de un diálogo Radix. Regla: los
    avisos van por `components/Toast` (portal al `#fs-overlay-root`); no
    escribir más `.fs-x__toast` por pantalla.
57. **`[~]` Correo: lo que sigue en la anterior.** Cuentas (IMAP/SMTP,
    Google OAuth, prueba), resumen, traducción y respuesta con IA, estilo
    de escritura, programar envíos y aprobaciones (`/schedule`, `/pending`),
    adjuntar ficheros al redactar (`/compose-upload`), unsubscribe, reglas y
    etiquetas, contactos, recordatorios desde un correo, estado de urgencia,
    imágenes en línea (`/inline-image`), ver adjunto como documento.
    `emailLibrary.js` son 8 800 líneas: segunda pasada con islas si hace
    falta (DECISIONES §10).
58. **`[~]` Verificado solo con fixtures.** El 7001 no tiene cuenta de
    correo; `DATA_DIR/fixture_email_messages.json` (fuera del repo, en
    `odysseus-dev-data`) activa el modo fixture del servidor. Antes de
    retirar el correo de la anterior hay que probar con una cuenta IMAP real
    (paginación, carpetas, mover, adjuntos, HTML de verdad).

## Añadido tras el lote N (05-09-2026, Ajustes)

59. **`[!]` `.fs-app details / summary { all: revert }` en `base.css` pisa
    cualquier clase de un solo nivel** (especificidad 0,1,1 contra 0,1,0) y
    `summary::before { content: none }` borra el chevrón. Regla: las
    disclosures propias se escriben como `.fs-app .fs-x` y el `::before`
    con `content … !important`.
60. **`[~]` Ajustes: lo que sigue en la anterior.** Modelos locales
    (descargar/servir), Integraciones (CRUD, presets, prueba), Cuentas de
    correo (IMAP/SMTP, OAuth Google, estilo), Herramientas y MCP
    (servidores, OAuth, reconectar), Cuenta (contraseña, 2FA, tokens de
    API, bóveda, cerrar sesión), Usuarios, Apariencia (editor de tema,
    fondos, opacidad). Cada uno es una pantalla o isla propia; las rutas
    ya existen (`/api/auth/integrations`, `/api/email/accounts`,
    `/api/mcp/servers`, `/api/tokens`, `/api/vault/*`, `/api/auth/2fa/*`).
61. **`[~]` Selectores de modelo por endpoint.** IA por defecto lista los
    modelos del endpoint elegido (o de todos si no hay); la anterior además
    filtraba por tipo (excluye audio, embeddings, whisper…) y avisaba si el
    modelo no soporta herramientas. Añadir ese filtro cuando `models` traiga
    metadatos.
62. **`[~]` `GET /api/auth/settings` devolvía la copia sin claves con
    `AUTH_ENABLED=false`** y el POST 403: la anterior tampoco podía guardar
    ajustes en el 7001. Arreglado con `owner_identity.auth_disabled()`. Los
    tests de `tests/` que asumen 403 sin usuario deben correr con
    `AUTH_ENABLED` activado (por defecto lo está).

## Añadido tras el lote O (05-09-2026, Agentes)

63. **`[!]` La hoja antigua se colaba en Studio por reglas de elemento.**
    `input:hover, textarea:hover, button:hover, select:hover { background:
    var(--panel); border-color: var(--fg) }` (0,1,1) pisaba `.fs-tab`,
    `.fs-chip` y cualquier control con clase de un nivel, y `pre {
    background: var(--code-bg) !important }` pintaba el bloque de contexto
    del proyecto con el color de código del tema viejo (blanco en oscuro).
    Acotadas en `static/style.css` con `:where(:not(.fs-app *))` — misma
    especificidad, la anterior no cambia — también `:root.light input…`.
    Regla: cualquier selector de elemento nuevo en `style.css` lleva ese
    `:where` o va bajo una clase.
64. **`[~]` Workers: lo que no cubre la pantalla.** El chat de Workers (el
    tablero con dirigir/parar) se abre en Studio con el `SubagentBoard`;
    `?robot=1` / `format=toon` son para el modelo, no para la pantalla. El
    campo *runner* se rechaza en el servidor si `agent_external_runners`
    está apagado (mensaje del servidor en el toast).
65. **`[~]` Expertos: `@expert:<slug>` en el compositor.** La anterior lo
    ofrecía en el popup de menciones; Studio lo acepta escrito a mano
    (fila Menciones). Cuando se hagan las fichas de mención, incluir los
    expertos activos (`listExperts`).
66. **`[~]` Agentes: sin atajo de teclado.** La anterior tampoco tenía
    (`/workers` era ruta, no atajo). Si se añade, va en `DEFAULT_KEYBINDS`
    y en `Studio.tsx` como `open_agents`.
67. **`[~]` Recompilar con la pantalla abierta rompe los chunks perezosos.**
    Un `import()` de un chunk cuyo hash ya no existe lanza «Failed to fetch
    dynamically imported module» y el shell cae a la anterior. En
    desarrollo: recargar. Para producción, un `catch` que recargue la
    página una vez (con marca en `sessionStorage`) sería lo honesto.

## Añadido tras el lote P (05-09-2026, idioma, apariencia y barra)

68. **`[!]` El shell se montaba dos veces.** `index.html` carga la entrada
    como `studio.js?v=<hash>`; el chunk de la app importa el ayudante de
    precarga de Vite desde `../studio.js` (sin query), el navegador lo toma
    por otro módulo y `main.tsx` corre dos veces: dos `createRoot` sobre el
    mismo contenedor, el segundo vacía el primero y el primero sigue
    renderizando en nodos sueltos. Cualquier borrado de nodos desde esa
    raíz («removeChild: not a child») tiraba el árbol entero y el shell
    caía a la anterior — el cambio de idioma lo hacía a la primera, y
    explica el «idioma que se reseteaba solo». Arreglo: `app.tsx` monta
    una sola vez (`mounted`). La entrada sigue corriendo dos veces; es
    inofensivo. Lo honesto a largo plazo: quitar el `?v=` y servir
    `studio.js` con `no-cache`, o un nombre con hash resuelto por el
    servidor.
69. **`[~]` Convenciones del diccionario.** La clave es el inglés literal.
    Cuando dos usos ingleses iguales piden español distinto, la clave lleva
    sufijo `#` (`All#f`, `Archive#folder`, `{n} active#`) y `en.ts` guarda
    su forma inglesa. Nunca llamar `t` a otra cosa (un `const t =
    setTimeout` o un `.map((t) =>` tapan la función y el compilador no
    avisa hasta que algo deja de traducirse). Las cadenas con `\n` van
    partidas: el salto fuera de `t()`. `Gallery.tsx` (galería de
    componentes para revisar) queda en español a propósito.
70. **`[~]` La anterior sigue en inglés.** `ui_language` solo mueve
    Studio; `style.css`/`app.js` no tienen diccionario y no lo tendrán:
    se retiran pantalla a pantalla.
71. **`[~]` Un parpadeo de tema antes de que cargue el shell.** El
    `data-theme` lo pone `theme.ts` al evaluar el chunk de la app; entre el
    HTML y ese momento manda `prefers-color-scheme`. Un `<script>` de tres
    líneas en `index.html` que lea `faustus_studio_theme` lo quitaría.
72. **`[~]` Títulos de pestaña del navegador.** Los pone el servidor
    («Memory — Faustus») en inglés; Studio no los toca aún. Cuando lo
    haga, `document.title` desde cada pantalla con `t()`.

## Añadido tras el lote Q (05-09-2026, vitales)

73. **`[!]` Escape no llegaba a los diálogos de Studio.** El listener de
    atajos de `Studio.tsx` (captura en `window`) paraba la propagación de
    `cancel` (Escape) fuera de los campos de texto, así que Radix (que
    escucha en `document`) nunca cerraba un popover, menú o diálogo con
    Escape. Ahora, con algo abierto en `#fs-overlay-root`, Escape es suyo.
74. **`[~]` Vitales: `modelControls.noteUsage`.** La anterior pasaba cada
    muestra al selector de modelo para «Ajustar a la VRAM». El selector de
    Studio no tiene ese mando todavía; cuando lo tenga, `useUsage()` ya
    expone la última muestra.

## Añadido tras el lote R (05-09-2026, Ajustes: cuenta, usuarios, herramientas)

75. **`[~]` `/api/tools` devuelve etiquetas, no ids.** La tabla `TOOL_META`
    (nombre, descripción, familia) que la anterior mantenía a mano no casa
    con `TOOL_TAGS`: todas las herramientas caen en «Otros» con su etiqueta
    cruda, en las dos interfaces. Lo honesto es que el servidor devuelva
    familia y descripción con cada etiqueta.
76. **`[~]` El 7001 corre con cuentas.** `/api/auth/status` sin cookie dice
    `authenticated: false`; las pantallas de admin piden sesión
    (`POST /api/auth/login {username, password}` desde la consola para
    probar). Studio muestra «Solo administradores» en vez de un error.

## Añadido tras el lote S (05-09-2026, Integraciones)

77. **`[~]` Tokens de agente por prefijo de nombre.** Como la anterior, un
    token es «de Claude» si su nombre empieza por `claude agent`, «de Codex»
    si por `codex agent` o si tiene alcances `todos:`/`email:`/`documents:`
    sin prefijo (compatibilidad). Renombrar un token a mano lo saca de la
    lista; lo honesto sería una columna `kind` en el servidor.
78. **`[~]` Recompilar mientras Ajustes está abierto** vuelve a tirar el
    shell (chunk con hash nuevo). Ya en 67; sigue sin el `catch` que
    recargue una vez.

## Añadido tras el lote T (05-09-2026, Modelos locales)

79. **`[~]` El legacy oculto sigue en el DOM con los mismos placeholders.**
    Un `querySelector` global desde la consola (o un test) puede pillar el
    formulario de la anterior en vez del de Studio. Se va cuando se retire
    el legacy; hasta entonces, acotar a `.fs-set__body` / `.fs-main`.

## Añadido tras el lote U (05-09-2026, Apariencia)

80. **`[~]` El difuminado de secretos actúa sobre el texto llano del lector**
    (`rich.tsx`), no dentro de los bloques de código ni del código en
    línea, donde la anterior también difuminaba. Extenderlo a `CodeBlock`
    cuando haga falta.
81. **`[~]` Los temas de la anterior eran para su disposición.** Colores de
    burbujas, del botón de enviar, del hamburguesa (`advanced` de
    `applyColors`) no tienen destino en Studio; el puente toma los cinco
    colores base y deriva el resto. Si un tema guardado trae `advanced`,
    se conserva en el JSON y se ignora.

## Añadido tras el lote V (05-09-2026, Skills)

82. **`[~]` La prueba de una skill corre con el modelo por defecto del
    servidor.** La anterior mandaba el modelo y el endpoint de la sesión
    abierta (`getCurrentModel()`); Studio no tiene sesión en `/skills`, así
    que `startTest` manda vacío y el servidor elige. Cuando la pantalla
    tenga un selector de modelo, pasarlo.
83. **`[~]` La agrupación de duplicadas es por tokens en inglés** (misma
    regla que `skills.js`): dos skills iguales en inglés y castellano
    (`date-formatting-module-creation` / `fecha-formato-m-dulo-y-test`) no
    se agrupan. Es la regla anterior; mejorarla es otra historia.
84. **`[!]` En el 7001 con `AUTH_ENABLED=false` las skills con `owner`
    responden 404 al leer o guardar su SKILL.md** (no hay usuario en la
    petición y el gestor compara dueños). `start7001.ps1` va ahora con
    `AUTH_ENABLED=true` + `LOCALHOST_BYPASS=true`, que entra como el primer
    admin. No es un fallo de Studio: la anterior hacía lo mismo.
85. **`[!]` Regresión del shell en móvil, arreglada:** por debajo de 768px
    `.fs-shell[data-nav='rail']` (lote P) pisaba la regla de una columna y
    dejaba el contenido en 72px de ancho. Ahora la media query nombra
    también `[data-nav='rail'|'wide']`. Vale para todas las pantallas.

## Añadido tras el lote W (05-09-2026, Automatizaciones)

86. **`[~]` La anterior sondeaba `/api/tasks/notifications` y lanzaba
    `Notification` del navegador al terminar una tarea.** Studio relee la
    lista cada 15 s (la fila dice «última hace un momento») pero no dispara
    avisos de escritorio. Va con el trabajo de notificaciones del shell.
87. **`[~]` Las expresiones cron se traducen solo en sus formas habituales**
    (cada N minutos/horas, a horas fijas, un día de la semana, entre
    semana); el resto se enseña tal cual, como antes.
88. **`[~]` «Detener» siempre está a la vista** porque la ficha de la tarea
    no dice si hay una ejecución en curso; el servidor contesta 404 «no
    está corriendo» y Studio lo enseña. Cuando `/api/tasks` traiga
    `is_running`, enseñarlo solo entonces.

## Añadido tras el lote Y (05-09-2026, Proyectos)

89. **`[!]` `DELETE /api/projects/{id}/session/{sid}` rompe con
    `'Session' object has no attribute 'folder'`** (routes/project_routes.py
    ~192, usa el gestor de sesiones en memoria en vez de la fila). Studio
    borra la conversación por `/api/session/{id}` como hacía la anterior.
    Arreglar la ruta o quitarla.
90. **`[~]` Los checkpoints de la actividad del agente no se restauran desde
    el proyecto**; se enseña el sha y el enlace abre la conversación, donde
    el harness sí ofrece restaurar.

## Después de la migración (decidido con Luis el 05-09, madrugada)

91. **`[→]` `inspiration/AUDITORIA_BACKEND_Y_FEATURES_FAUSTUS.md`** (25 bugs,
    13 cambios estructurales, 36 features) se acomete **después** del merge
    de Studio, en ramas propias: primero SEC-1 y los parches pequeños del
    sprint 0B; luego STATE-1, AUTH-1, ART-1/RUN-1… Studio absorbe los
    cambios de contrato en `adapters/*` (Actividad ya normaliza estados en
    el frontend, así que RunService la simplifica).
92. **`[→]` `inspiration/PLAN_VOZ_JARVIS_FAUSTUS.md`** (VoiceSessionService,
    push-to-talk, barge-in, VAD, wake word) también después del merge, sobre
    el compositor y el arnés de Studio, con su pasada de diseño: V-000 →
    V-002 → V-004 → V-003 → V-005.

## Lote Z2 (editor de imagen)

93. **`[→]`** El aviso «rembg no está instalado» enlaza a la anterior
    (`/?shell=legacy`) porque el Cookbook aún no está en Studio; cuando
    llegue, `onOpenCookbook('rembg')` debe abrir `/cookbook?install=rembg`.
94. **`[→]`** SAM (`/api/image/mask`), inpaint, armonizar y estilo no se han
    podido probar de extremo a extremo en el 7001: no hay endpoint de imagen
    ni rembg instalado (la anterior tampoco funciona sin ellos). Verificado
    el camino de error (aviso con el detalle del servidor) y el contrato.
95. **`[ ]`** Guarda de salida: el borrador se guarda 1,5 s después de cada
    cambio; si se navega antes, el navegador pregunta. Pendiente un
    «guardando…» visible en la barra en vez del punto ámbar.
96. **`[ ]`** `.fs-shell[data-nav='rail'] .fs-main` (0,2,1) pisaba el
    `padding: 0` de las pantallas a pantalla completa por debajo de 1280px;
    Studio y el editor lo nombran ahora con la misma especificidad. Revisar
    si a otras pantallas les pasa lo mismo con el relleno móvil (24px en
    vez de 16px).
97. **`[ ]`** `static/js/theme.js` lanza `Cannot read properties of undefined
    (reading 'bg')` en cada carga con Studio activo (el DOM del editor de tema
    de la anterior no existe). Desaparece al borrar la anterior.

## Lote AA (biblioteca y editor de documentos)

98. **`[→]`** PyMuPDF no estaba en el venv del 7001 (`render-pages` devolvía 503
    «PDF viewer requires PyMuPDF»); se instaló con `pip install PyMuPDF` para
    probar las páginas. Es opcional y AGPL (`requirements-optional.txt`): decidir
    si el instalador lo ofrece.
99. **`[→]`** La biblioteca de Investigación no se ha podido probar con datos (no
    hay informes en el 7001); el contrato (`/api/research/library|detail|
    spinoff|export|archive`) está tomado de la anterior. Se prueba en el lote de
    Deep Research.
100. **`[→]`** «Firmar y responder» deja el adjunto (token de `prepare-signed-
    reply`) en la entrega a Redactar, pero Redactar aún no muestra adjuntos: llega
    con el lote de Correo (adjuntar, programar, IA).
101. **`[ ]`** La vista previa Markdown del editor usa el lector reducido
    (`rich.tsx`): los títulos se ven en negrita sin jerarquía. Se arregla con el
    lote de Markdown completo.
102. **`[ ]`** La lista de chats de la biblioteca es cliente (`/api/sessions` ya
    trae todo): con miles de chats convendría paginar en el servidor.

## Lote AB (correo completo)

103. **`[→]`** En el 7001 (modo fixture, sin IMAP) solo se pueden probar de
    verdad listar, leer, resumir, traducir, redactar con IA, programar y la
    bandeja de salida; estrella, leído/no leído, hecho, archivar, mover y
    borrar responden `success:false` («Mail operation failed») y la pantalla lo
    enseña tal cual (la anterior no miraba el cuerpo y decía «Con estrella»
    aunque fallara). Se prueban con una cuenta IMAP real.
104. **`[ ]`** `PUT /api/email/config` crea una fila `EmailAccount` «Default»
    si no hay ninguna (el servidor mete las credenciales ahí): guardar los
    ajustes del correo sin cuenta deja una cuenta vacía en el selector. Es del
    servidor (`update_email_config`); la pantalla no lo ha tocado en el 7001.
105. **`[ ]`** El idioma por defecto de la traducción (`email_translate_language`)
    se lee de la configuración pero no hay ruta que lo escriba salvo
    `/api/settings`; el diálogo lo muestra, no lo edita.
106. **`[ ]`** El popover «Redactar con IA» se queda abierto mientras el modelo
    escribe (el botón muestra el giro); cerrarlo al pulsar sería más limpio,
    pero `Popover` no es controlado.
107. **`[→]`** La conversación en burbujas (`thread_turns`), la firma plegada
    (`sender_signature`), las imágenes `cid:` y los adjuntos diferidos están
    escritos contra el contrato de `/read` pero el fixture no los devuelve:
    sin probar con correo real.

## Lote AC (Deep Research)

108. **`[→]`** La investigación de prueba (rondas 1, qwen 27b q8) tardó 300 s
    (el `max_time` por defecto) y terminó con 0 fuentes: el modelo planifica
    lento y la extracción no llegó. El stream cerró antes del evento final y
    `/status` ya no la conocía; la pantalla pregunta por el informe guardado
    antes de dar la investigación por fallida. Probar con el 9b o con más
    tiempo desde la tarjeta «Ajuste».
109. **`[ ]`** `/api/research/active` lo sondea cada pocos segundos el
    `jobs.js` de la anterior, que sigue cargado aunque oculto. Desaparece
    al borrar la anterior.

## Lote AD (Compare)

110. **`[ ]`** `/api/compare/record` no existe en el servidor (la anterior lo
    llamaba y se tragaba el 404): los votos viven solo en `localStorage`. Si se
    quiere un marcador entre navegadores hace falta una ruta.
111. **`[→]`** Sin verificar con datos: modo Agente (aprobaciones dentro del
    panel), modo Investigación (minutos por panel), uno detrás de otro,
    prompts con respuesta esperada (nota Correcta / Fallada), imprimir.
112. **`[ ]`** En un solo equipo con una GPU, «todos a la vez» hace que Ollama
    descargue un modelo para cargar el otro (el 27b tardó 50 s frente a 11 s
    del 9b): el chip «Uno detrás de otro» es el honesto en local; podría
    sugerirse cuando todos los huecos son del mismo endpoint local.

## Lote AE (chat en grupo y personajes)

113. **`[ ]`** El chat en grupo manda cada turno como chat normal (igual que
    la anterior): la memoria y las reglas del usuario entran en cada
    participante (el 9b saludó a «Luis» sin que nadie lo nombrara). Si se
    quiere una mesa «limpia» habría que mandar `no_memory` como Compare.
114. **`[→]`** Sin verificar: «Todos a la vez», grupos guardados (guardar,
    cargar, borrar), personajes en la mesa (no hay plantillas en el 7001),
    «Expandir con IA» (tarda con el 27b), prefijo/sufijo del personaje propio
    en un turno real.
115. **`[ ]`** La forma del grupo (participantes y sesiones) vive en
    `localStorage` (`fs-group-states`), como en la anterior: desde otro
    navegador el padre `[GRP]` se abre como chat normal y no se puede seguir
    la mesa. Guardarla en los metadatos de la sesión padre lo arreglaría.

## Lote AF (Tournament, Procedencia, Historial importado)

116. **`[ ]`** En un solo equipo con una GPU, el torneo lanza a todos los
    participantes a la vez (así está en `src/tournament.py`): con 9b + 27b
    la respuesta a ciegas del 9b tardó 137 s por el trasiego de VRAM. Un
    modo «uno detrás de otro» sería cosa del servidor, no de la pantalla.
117. **`[ ]`** El grafo de procedencia no re-dispone al filtrar por tipo
    (cada filtro es un grafo distinto para el algoritmo, que es lo que hacía
    la anterior): un grafo grande «salta» al pulsar un chip. Fijar las
    posiciones del grafo completo y ocultar sería más estable.
118. **`[→]`** Sin verificar en el 7001: subir un archivo de exportación
    (multipart; el 7001 se probó con ruta), grafo con más de 60 nodos
    (etiquetas solo en los más conectados) y con más de 200 (tope dicho),
    pares casi duplicados (no hay ninguno en el 7001), torneo con un
    participante fallido.
119. **`[ ]`** El campo de búsqueda de Biblioteca guarda cada tecla en la
    URL y con escritura muy rápida (automatizada) pierde teclas; a mano no
    se nota. Un `useDeferredValue` en `Library.tsx` lo quitaría.

## Lote AG (Cookbook)

120. **`[→]`** Sin verificar en el 7001 (Windows local, sin remotos): servidores
    SSH (probar, preparar, clave), lanzamientos vLLM/SGLang/MLX/Diffusers,
    reintentos con flag desde el diagnóstico, cola de descargas, adopción de
    sesiones externas, programar (el diálogo se abre; no se guardó ninguna
    tarea). Verificado: ajuste, caché, descarga Ollama y GGUF, lanzamiento
    llama.cpp con diagnóstico del servidor, dependencias, formulario de
    servidores.
121. **`[ ]`** En Windows local, servir una etiqueta de Ollama ya descargada
    ejecuta `ollama show <tag>` (la anterior mandaba `docker exec` al sidecar,
    que aquí no existe): la sesión termina y queda «fallida» aunque el modelo
    esté disponible por el endpoint 11434. Lo honesto sería que el servidor
    registrara el endpoint sin lanzar nada.
122. **`[ ]`** La descarga GGUF desde Ajuste usa el patrón `*<cuant>*` de la
    fila (como la anterior): con `BF16` en bartowski no coincide ningún
    archivo y la descarga acaba «hecha» con 0 MB; el diagnóstico del servidor
    («No matching files») solo se ve al desplegar la tarjeta.
123. **`[ ]`** El tamaño de un repo GGUF recién descargado aparece «0 MB» hasta
    el siguiente escaneo completo; `gguf_files` llega vacío y el formulario no
    ofrece el selector de archivo.

